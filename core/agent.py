"""Ciclo agentico, costruzione del contesto e parser di fallback.

Il loop e' un **generatore di eventi** e non sa niente dell'interfaccia: la UI
consuma gli eventi e decide come disegnarli. Cosi' un problema di rendering e
uno di orchestrazione restano due problemi distinti, e il ciclo si prova a
secco senza un browser.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any
from collections.abc import Callable, Iterator, Sequence

from .backend import StreamEvent
from .config import (
    HISTORY_COMPACT_THRESHOLD,
    Budgets,
    budgets_for,
)
from .compaction import (
    CODA_DEFAULT,
    SOGLIA_DEFAULT,
    TETTO_TOKEN_DEFAULT,
    Compattazione,
    costruisci_riassunto,
    finestra_efficace,
    render_messaggio,
    richieste_utente,
    taglio,
    trascrizione,
)
from pathlib import PurePath

from . import delega as delega_mod
from . import deposito as deposito_mod
from . import libreria
from . import pensiero
from . import spec_delega as spec_delega_mod
from . import vault as vault_mod
from . import vault_search as vault_search_mod
from .notes import render_block as render_notes
from .plan import render_block, render_summary
from .prompts import (
    ASK_NUDGE,
    COVERAGE_NUDGE,
    DELEGA_ESEMPIO,
    DELEGA_NUDGE,
    FAILED_SUMMARY_NUDGE,
    JSON_LEAK_NUDGE,
    LOOP_NUDGE,
    PLAN_NUDGE,
    PLAN_SUMMARY_NUDGE,
    PROMPT_RIEPILOGO_FINALE,
    RIPETIZIONE_NUDGE,
    SUMMARY_NUDGE,
    THINK_WATCHDOG_NUDGE,
    TOOL_NUDGE,
    TRUNCATED_NUDGE,
    VERIFY_NUDGE,
)
from .textutils import (
    ThinkStreamParser,
    chars_for_tokens,
    estimate_messages_tokens,
    estimate_tokens,
    smart_truncate,
    strip_think,
    strip_tool_wrappers,
)
from .tools import (
    ASK_USER_TOOL,
    NOTES_TOOL,
    PLAN_TOOL,
    PREVIEW_TOOL,
    TOOL_NAMES,
    ToolContext,
    dispatch,
    looks_like_readonly_request,
    looks_like_server,
    normalise_question,
    preview_kind,
    preview_root,
    reset_scratch,
    uncovered_symbols,
)

# ---------------------------------------------------------------------------
# Eventi
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StepStarted:
    step: int
    total: int


# Il pensiero e la risposta in corso viaggiano a **incrementi**, non piu' come
# testo cumulativo, e questo e' il difetto piu' caro che l'harness abbia avuto.
#
# Com'era: ad ogni token generato si spediva tutto il testo accumulato fino a
# li'. Un passo da 20.000 token di ragionamento sono 20.000 eventi, il primo da
# pochi byte e l'ultimo da 80 kB: nell'ordine dei **centinaia di megabyte** per
# un solo passo, tutti serializzati in JSON, spediti sul filo, tenuti in RAM nel
# buffer del turno (che serve a chi si riattacca) e ridisegnati dal browser una
# volta per token. Il sintomo era il popup di Chrome: "la pagina non risponde".
#
# Com'e': ``append`` porta i soli caratteri nuovi e il client li accoda;
# ``text`` porta il testo completo e il client **sostituisce**. Ne arriva uno
# solo dei due. La regola per chi legge questi eventi e' quindi: se c'e'
# ``append``, accoda; altrimenti sostituisci con ``text``.
#
# Il testo completo continua ad arrivare alla fine di ogni passo, ed e' voluto:
# e' la rete di sicurezza. Un abbonato lento puo' vedersi scartare dei frame
# (``_SUBSCRIBER_QUEUE_MAX``), e con soli incrementi resterebbe con un testo
# sbagliato fino alla fine del turno.
@dataclass(slots=True)
class ReasoningDelta:
    text: str = ""      # testo completo: sostituisce
    append: str = ""    # soli caratteri nuovi: si accoda


@dataclass(slots=True)
class ContentDelta:
    text: str = ""
    append: str = ""


# Quanti aggiornamenti al secondo, al massimo, del testo in corso. Dieci sono
# il punto in cui l'occhio legge ancora un flusso continuo e il browser smette
# di soffrire: il freno non perde niente -- l'aggiornamento saltato viene
# incluso nel successivo, che porta tutto quello che nel frattempo e' arrivato.
STREAM_INTERVALLO_S = 0.1


class Rubinetto:
    """Trasforma il testo accumulato dal parser in incrementi, a passo d'uomo.

    Tiene due cose sole: cosa e' gia' stato spedito, e quando. Da quelle ricava
    se c'e' qualcosa di nuovo da dire e se e' il momento di dirlo.

    Il caso in cui il testo nuovo **non** comincia con quello gia' spedito
    esiste: il parser puo' riscrivere all'indietro quando un tag ``<think>``
    arriva spezzato fra due chunk. Li' non si puo' accodare, e si manda il
    testo intero -- e' raro, e sbagliare da questa parte significa mostrare
    testo doppio.
    """

    __slots__ = ("_pensiero", "_risposta", "_ultimo")

    def __init__(self) -> None:
        self._pensiero = ""
        self._risposta = ""
        self._ultimo = 0.0

    @staticmethod
    def _campi(gia_spedito: str, adesso: str) -> dict[str, str] | None:
        if adesso == gia_spedito:
            return None
        if gia_spedito and adesso.startswith(gia_spedito):
            return {"append": adesso[len(gia_spedito):]}
        return {"text": adesso}

    def aggiorna(self, pensiero: str, risposta: str) -> Iterator[Any]:
        """Gli eventi da mandare adesso: nessuno, se e' troppo presto."""
        adesso = time.monotonic()
        if adesso - self._ultimo < STREAM_INTERVALLO_S:
            return
        campi = self._campi(self._pensiero, pensiero)
        if campi is not None:
            self._pensiero = pensiero
            yield ReasoningDelta(**campi)
        campi = self._campi(self._risposta, risposta)
        if campi is not None:
            self._risposta = risposta
            yield ContentDelta(**campi)
        self._ultimo = adesso


@dataclass(slots=True)
class AssistantTurn:
    content: str
    reasoning: str
    has_tool_calls: bool
    # True quando le tool call sono state estratte dal testo invece di arrivare
    # dal function calling nativo: la UI lo segnala e non stampa il JSON.
    recovered: bool = False


@dataclass(slots=True)
class ToolStarted:
    call_id: str
    name: str
    args: dict[str, Any]


@dataclass(slots=True)
class ToolFinished:
    call_id: str
    name: str
    args: dict[str, Any]
    result: str
    duration_s: float
    ok: bool


@dataclass(slots=True)
class AwaitingUserInput:
    """Il ciclo si e' fermato: l'agente ha chiesto qualcosa e aspetta."""

    call_id: str
    question: str
    options: list[dict[str, str]] = field(default_factory=list)
    allow_multiple: bool = False


@dataclass(slots=True)
class TurnFinished:
    reason: str  # "completed" | "max_steps" | "error" | "awaiting_user"
    steps: int
    usage: dict[str, Any] = field(default_factory=dict)
    # I file creati o modificati nel turno non passano di qui: la UI li ricava
    # dai risultati di write_file/edit_file che gia' riceve (vedi fileTocca in
    # web/app.js). Cosi' la stessa regola vale sia in diretta sia quando una
    # sessione salvata viene riaperta, invece di esistere in due copie che poi
    # divergono. E in nessuno dei due casi la lista la scrive il modello.


@dataclass(slots=True)
class HistoryCompacted:
    """La cronologia e' uscita dalla vista del modello, riassunta.

    Va detto all'utente invece che fatto di nascosto: e' l'unico momento in cui
    l'agente smette di avere davanti quello che ha visto, e se due passi dopo
    dimentica un dettaglio, la spiegazione e' questa e non un capriccio del
    modello.
    """

    messages: int
    tokens_before: int
    tokens_after: int
    summary: str = ""


@dataclass(slots=True)
class NotesUpdated:
    """Il foglio di note e' cambiato: la UI ridisegna la scheda."""

    notes: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class PlanUpdated:
    """Il piano e' cambiato: la UI ridisegna il pannello, niente altro.

    Evento a se' e non un campo di ToolFinished perche' il piano cambia anche
    quando a cambiarlo non e' una tool call visibile, e perche' il pannello
    deve poter reagire senza dover ispezionare il JSON dei risultati.
    """

    steps: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""


@dataclass(slots=True)
class PreviewUpdated:
    """C'e' qualcosa da guardare: un file prodotto o un'applicazione avviata.

    ``payload`` e' None quando l'anteprima va chiusa (processo fermato).
    """

    payload: dict[str, Any] | None = None


@dataclass(slots=True)
class AgentError:
    message: str


AgentEvent = (
    StepStarted
    | ReasoningDelta
    | ContentDelta
    | AssistantTurn
    | ToolStarted
    | ToolFinished
    | AwaitingUserInput
    | PlanUpdated
    | PreviewUpdated
    | TurnFinished
    | AgentError
)


# ---------------------------------------------------------------------------
# Costruzione del contesto
# ---------------------------------------------------------------------------


def _compact_tool_result(
    raw: str, *, full: bool, budgets: Budgets | None = None
) -> str:
    """Riduce un risultato di tool gia' consumato dal modello.

    Un risultato di ``read_file`` vecchio di 5 passi e' morto: il modello ha
    gia' estratto quello che gli serviva, ma continua a costare token in ogni
    richiesta successiva. Lo si riduce a un sommario strutturale.
    """
    budgets = budgets or Budgets()
    if full:
        return smart_truncate(
            raw, budgets.tool_result_max_chars, label="risultato tool"
        )

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return smart_truncate(raw, 400, label="risultato tool (compattato)")

    if not isinstance(payload, dict):
        return smart_truncate(raw, 400, label="risultato tool (compattato)")

    if "error" in payload:
        return json.dumps(payload, ensure_ascii=False)[:400]

    keep = {}
    for key in ("status", "action", "filepath", "returncode", "match_count", "folder"):
        if key in payload:
            keep[key] = payload[key]
    for key in ("content", "tree", "stdout", "stderr", "matches"):
        if key in payload:
            body = payload[key]
            body_str = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
            keep[key] = smart_truncate(body_str, 300, label=key)
    keep["_compacted"] = True
    return json.dumps(keep, ensure_ascii=False)


# Argomenti che contengono il *corpo* di qualcosa, non un riferimento. Sono
# l'unica parte di una tool call che vale la pena potare: filepath, command e
# pattern costano decine di token e servono a capire cosa e' successo, mentre
# `content` di un write_file puo' valerne diecimila e, a chiamata eseguita, e'
# una copia di un file che sta sul disco.
_ARGOMENTI_PESANTI = {
    "write_file": ("content",),
    "edit_file": ("old_string", "new_string"),
}


def _prune_tool_call(call: dict[str, Any]) -> dict[str, Any]:
    """Toglie il corpo da una tool call gia' eseguita e ormai vecchia.

    E' la voce di gran lunga piu' grossa del contesto e nessuno la guardava:
    su una sessione reale di questo progetto, 33.312 token su 42.126 inviati
    erano il contenuto dei file dentro gli argomenti di ``write_file``. La
    compattazione dei risultati non li toccava, perche' vivono nel messaggio
    *assistant*, non in quello *tool*.

    Toglierli non perde informazione: il file e' stato scritto, sta sul disco,
    e ``read_file`` lo rilegge quando serve. Tenerlo in cronologia significa
    pagare due volte lo stesso testo per il resto della conversazione.
    """
    fn = call.get("function") or {}
    nome = str(fn.get("name") or "")
    chiavi = _ARGOMENTI_PESANTI.get(nome)
    if not chiavi:
        return call
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except json.JSONDecodeError:
        return call
    if not isinstance(args, dict):
        return call

    potato = False
    for chiave in chiavi:
        corpo = args.get(chiave)
        if not isinstance(corpo, str) or len(corpo) < 400:
            # Sotto la soglia il risparmio non ripaga la riscrittura del
            # messaggio, che fa divergere il prefisso e costa un prompt eval.
            continue
        righe = corpo.count("\n") + 1
        args[chiave] = (
            f"<omesso: {righe} righe, {len(corpo)} caratteri gia' scritti su "
            "disco. Se ti serve il testo esatto, rileggilo con read_file.>"
        )
        potato = True

    if not potato:
        return call
    return {
        **call,
        "function": {**fn, "arguments": json.dumps(args, ensure_ascii=False)},
    }


def build_api_messages(
    ui_messages: Sequence[dict[str, Any]],
    *,
    system_prompt: str,
    env_header: str | None,
    strip_thinking: bool = True,
    compact_old_tools: bool = True,
    images: list[str] | None = None,
    budgets: Budgets | None = None,
    plan_block: str = "",
    preview_block: str = "",
    notes_block: str = "",
    skills_block: str = "",
    libreria_block: str = "",
    vault_notes_block: str = "",
    vault_state_block: str = "",
    delega_block: str = "",
) -> list[dict[str, Any]]:
    """Costruisce l'array da inviare al modello a partire dal log della UI.

    Ordine dei blocchi scelto per massimizzare il riuso del KV cache:
    ``[system statico] [environment] [cronologia append-only]``.
    I primi due blocchi sono byte-identici fra un passo e l'altro, quindi il
    prefisso e' riutilizzabile e il prompt eval si riduce ai soli token nuovi.

    ``images`` (base64, per i modelli multimodali) viene agganciato all'ultimo
    messaggio **visibile** dell'utente: i solleciti che l'harness accoda sono
    nascosti e non devono portarsi dietro le immagini, o l'agente le rivedrebbe
    ad ogni passo come se fossero nuove.
    """
    budgets = budgets or Budgets()
    api: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    if env_header:
        api.append({"role": "system", "content": env_header})
    last_visible_user = -1

    # Se la cronologia e' stata compattata, il modello riparte dall'ultimo
    # riassunto. La UI invece li tiene tutti i messaggi: la compattazione serve
    # a non far pagare al modello ad ogni passo un contesto che non gli serve
    # piu', non a cancellare la conversazione all'utente. ``ui_messages`` resta
    # append-only, che e' il motivo per cui una sessione salvata si puo' ancora
    # rileggere per intero.
    inizio = 0
    for i, msg in enumerate(ui_messages):
        if msg.get("role") == "summary":
            inizio = i
    ui_messages = ui_messages[inizio:]

    # Indice del "recente": gli ultimi N risultati di tool restano integrali.
    # N cresce con la finestra, e non solo per tenere piu' roba: compattare un
    # risultato **riscrive un messaggio in mezzo alla cronologia**, quindi il
    # prefisso diverge da quello del passo precedente e il KV cache va
    # ricalcolato da li' in avanti. Su una finestra stretta il baratto conviene
    # lo stesso; su una larga si pagherebbe un prompt eval per risparmiare
    # token che non mancavano a nessuno.
    tool_positions = [
        i for i, m in enumerate(ui_messages) if m.get("role") == "tool"
    ]
    recent_tools = (
        set(tool_positions[-budgets.tool_result_full_window:])
        if compact_old_tools
        else set(tool_positions)
    )
    # Gli id delle chiamate ancora "recenti". La potatura degli argomenti segue
    # la stessa finestra dei risultati, e non e' una comodita': se si potasse
    # l'argomento di una chiamata il cui risultato e' ancora integrale, il
    # modello si troverebbe davanti l'esito completo di un'azione di cui non
    # vede piu' la richiesta -- il modo migliore per fargli rifare il lavoro.
    recent_call_ids = {
        str(ui_messages[i].get("tool_call_id") or "") for i in recent_tools
    }

    for idx, msg in enumerate(ui_messages):
        role = msg.get("role")

        if role == "user":
            api.append({"role": "user", "content": msg.get("content", "")})
            if images and not msg.get("hidden"):
                last_visible_user = len(api) - 1

        elif role == "summary":
            # Ruolo 'user' e non 'system': ``to_ollama_messages`` fonde i
            # blocchi di sistema in testa, e un riassunto che finisce nel
            # prefisso lo fa divergere ad ogni compattazione -- proprio la
            # spesa che la compattazione doveva evitare.
            api.append({"role": "user", "content": msg.get("content", "")})

        elif role == "assistant":
            content = msg.get("content") or ""
            if strip_thinking:
                content = strip_think(content)
            entry: dict[str, Any] = {"role": "assistant", "content": content}
            if msg.get("tool_calls"):
                entry["tool_calls"] = [
                    call
                    if not compact_old_tools
                    or str(call.get("id") or "") in recent_call_ids
                    else _prune_tool_call(call)
                    for call in msg["tool_calls"]
                ]
            # Un assistant senza contenuto ne' tool call non aggiunge nulla.
            if entry["content"] or entry.get("tool_calls"):
                api.append(entry)

        elif role == "tool":
            api.append(
                {
                    "role": "tool",
                    "tool_call_id": msg.get("tool_call_id", ""),
                    "name": msg.get("name", ""),
                    "content": _compact_tool_result(
                        str(msg.get("content", "")),
                        full=idx in recent_tools,
                        budgets=budgets,
                    ),
                }
            )

        # 'pending_question' e' un record puramente visuale: esiste finche'
        # l'utente non risponde, poi resume_with_answer lo converte in un
        # normale messaggio 'tool'. Non deve mai raggiungere il modello.

    if images and last_visible_user >= 0:
        api[last_visible_user]["images"] = list(images)

    # Il piano va **in coda**, dopo tutta la cronologia, e con ruolo 'user'.
    # Le due scelte hanno la stessa ragione: non toccare il prefisso. Il
    # prefisso (system + environment) e' byte-identico fra un passo e l'altro
    # ed e' cio' che rende riusabile il KV cache di Ollama; il piano invece
    # cambia proprio mentre il modello lavora. Con ruolo 'system' finirebbe
    # comunque in testa, perche' ``to_ollama_messages`` fonde tutti i blocchi
    # di sistema in uno solo davanti -- e ogni 'complete' costerebbe un prompt
    # eval completo per aggiornare tre parole.
    coda = [
        blocco
        # La memoria del vault sta **prima** del foglio della chat: dal piu'
        # vecchio e stabile al piu' fresco, e il piu' fresco resta attaccato
        # al piano, che e' l'ultima cosa che il modello legge prima di agire.
        for blocco in (
            skills_block,
            preview_block,
            libreria_block,
            # Lo stato della wiki -- indice e coda del log -- sta qui e non nel
            # prompt di sistema: cambia a ogni ingest, e in testa invalidava il
            # prefisso proprio nell'operazione per cui la modalita' wiki
            # esiste. Prima della memoria del vault perche' e' piu' volatile.
            vault_state_block,
            vault_notes_block,
            notes_block,
            plan_block,
            # Ultimo perche' e' l'unico che il figlio della delega ha: per lui
            # e' quello che il piano e' per il padre, cioe' l'ultima cosa che
            # legge prima di muoversi. Sul padre e' sempre vuoto.
            delega_block,
        )
        if blocco
    ]
    if coda:
        api.append({"role": "user", "content": "\n\n".join(coda)})
    return api


# ---------------------------------------------------------------------------
# Domande all'utente: sospensione e ripresa
# ---------------------------------------------------------------------------


def pending_question(ui_messages: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """La domanda in attesa di risposta, se il turno e' sospeso.

    E' l'unico stato che serve per riprendere: essendo scritto dentro
    ``ui_messages``, viene salvato su disco e sopravvive al riavvio dell'app o
    al passaggio a un'altra conversazione e ritorno.
    """
    if not ui_messages:
        return None
    last = ui_messages[-1]
    if last.get("role") == "pending_question":
        return last
    return None


def resume_with_answer(
    ui_messages: list[dict[str, Any]], answer: str | list[str]
) -> bool:
    """Converte la domanda in sospeso nel risultato del tool.

    Dopo questa chiamata basta rilanciare :func:`run_turn`: la risposta e' gia'
    in cronologia come risultato di ``ask_user_question`` e il modello riparte
    da li'.
    """
    question = pending_question(ui_messages)
    if question is None:
        return False

    if isinstance(answer, list):
        text = ", ".join(str(a).strip() for a in answer if str(a).strip())
    else:
        text = str(answer).strip()
    if not text:
        return False

    ui_messages.pop()
    if question.get("kind") == GATE_PIANO:
        # Il cancello sul piano non nasce da una chiamata del modello: non c'e'
        # nessuna tool_call scoperta da chiudere, e inventarle un risultato
        # lascerebbe in cronologia un tool_call_id che nessuno ha mai emesso --
        # cosa che diversi chat template rifiutano. La risposta rientra come
        # messaggio dell'utente, che e' quello che e'.
        ui_messages.append(
            {
                "role": "user",
                # La marca resta sul messaggio di risposta: al turno successivo
                # la domanda in sospeso non c'e' piu', ed e' l'unico modo di
                # sapere che il cancello si e' gia' aperto una volta.
                "kind": GATE_PIANO,
                "content": (
                    f"Ho letto il piano. {text}\n"
                    "Se ti ho chiesto una correzione, riscrivilo con "
                    "manage_plan action='set' prima di lavorare; altrimenti "
                    "vai avanti senza ripianificare."
                ),
                "ts": time.time(),
            }
        )
        return True
    ui_messages.append(
        {
            "role": "tool",
            "tool_call_id": question.get("tool_call_id", ""),
            "name": ASK_USER_TOOL,
            "content": json.dumps(
                {"user_answer": text, "question": question.get("question", "")},
                ensure_ascii=False,
            ),
            "args": {"question": question.get("question", "")},
            "duration_s": 0.0,
            "ok": True,
            "answered": True,
            "ts": time.time(),
        }
    )
    return True


# Marca la domanda che apre il cancello sul piano. Serve alla ripresa, che deve
# trattarla diversamente da una ask_user_question vera.
GATE_PIANO = "gate_piano"

# Da quanti punti in su il piano vale una lettura prima di partire. Sotto, il
# lavoro e' abbastanza corto che rifarlo costa meno che interromperlo; sopra,
# un piano storto si paga per venti o trenta passi, e correggerlo adesso costa
# dieci secondi. La soglia sta qui e non nelle impostazioni perche' non e' una
# preferenza: e' il punto in cui l'aritmetica cambia segno.
PUNTI_PER_IL_CANCELLO = 6


def context_pressure(api_messages: Sequence[dict], num_ctx: int) -> float:
    if num_ctx <= 0:
        return 0.0
    return estimate_messages_tokens(api_messages) / num_ctx


def compatta_cronologia(
    ui_messages: list[dict[str, Any]],
    *,
    backend: Any,
    params: Any,
    budgets: Budgets,
    strip_thinking: bool,
    soglia_coda: float = CODA_DEFAULT,
    finestra: int | None = None,
    schedario: Any = None,
) -> Compattazione | None:
    """Riassume il tratto vecchio della cronologia e ne toglie la vista al modello.

    Non cancella niente: inserisce un messaggio di ruolo ``summary``, e
    ``build_api_messages`` riparte da li'. I messaggi precedenti restano in
    ``ui_messages`` -- quindi nella sessione salvata e nella chat -- perche' a
    dover risalire e' l'utente, mentre a doverlo ripagare ad ogni passo sarebbe
    il modello. Sono due esigenze diverse e non c'e' motivo di sacrificare la
    prima alla seconda.

    Ritorna ``None`` quando non c'e' niente da guadagnare o il riassunto non e'
    riuscito: in quel caso e' meglio un contesto pieno che una cronologia
    buttata via senza averla riassunta.
    """
    # La coda si misura sulla stessa finestra su cui e' scattata la soglia:
    # con il tetto assoluto attivo quella non e' ``num_ctx`` ma la finestra
    # efficace, e usare qui quella vera terrebbe una coda piu' grande della
    # soglia che ha appena fatto compattare -- cioe' compattare per niente.
    num_ctx = int(finestra or 0) or int(getattr(params, "num_ctx", 0) or 0)
    if num_ctx <= 0:
        return None

    def costo(pezzo: Sequence[dict[str, Any]]) -> int:
        # ``compact_old_tools=False`` di proposito: il tratto che si sta
        # misurando e' la *coda*, cioe' esattamente la parte che il modello
        # riceve integrale. Misurarla compattata direbbe che ci sta comoda e
        # ne farebbe tenere troppa.
        return estimate_messages_tokens(
            build_api_messages(
                pezzo,
                system_prompt="",
                env_header=None,
                strip_thinking=strip_thinking,
                compact_old_tools=False,
                budgets=budgets,
            )
        )

    ultimo_riassunto = max(
        (i for i, m in enumerate(ui_messages) if m.get("role") == "summary"),
        default=-1,
    )
    cut = taglio(
        ui_messages,
        costo=costo,
        budget_coda=int(num_ctx * soglia_coda),
    )
    # Tagliare a monte di un riassunto gia' esistente non libererebbe niente:
    # quel tratto e' gia' fuori dalla vista del modello.
    if cut <= ultimo_riassunto + 1:
        return None

    vecchi = ui_messages[:cut]
    prima = costo(vecchi)
    richieste = richieste_utente(vecchi)
    riassunto = costruisci_riassunto(
        trascrizione(vecchi, archiviato=schedario is not None),
        backend=backend,
        params=params,
    )
    if not riassunto:
        # Senza riassunto non si compatta, nemmeno tenendo le richieste
        # dell'utente: resterebbe la specifica e sparirebbe tutto il lavoro
        # fatto per soddisfarla, che e' il modo peggiore di liberare contesto.
        # Se la finestra sfonda davvero interviene ``drop_oldest_turns``, che
        # almeno lo dichiara.
        return None

    # Archiviare **prima** di inserire il messaggio: se il disco rifiuta, la
    # compattazione avviene lo stesso (``archivia`` torna None e basta), ma il
    # riassunto non deve mai finire in cronologia credendo di essere al sicuro
    # su disco quando non c'e'.
    voce = libreria.archivia(schedario, riassunto=riassunto, richieste=richieste) if schedario else None

    testo = render_messaggio(riassunto, richieste, voce=voce)
    ui_messages.insert(
        cut,
        {
            "role": "summary",
            "content": testo,
            "replaced": cut - (ultimo_riassunto + 1),
            "ts": time.time(),
        },
    )
    return Compattazione(
        messaggi_prima=cut,
        messaggi_dopo=1,
        token_prima=prima,
        token_dopo=estimate_tokens(testo),
        riassunto=riassunto,
        richieste=richieste,
    )


def drop_oldest_turns(
    api_messages: list[dict],
    num_ctx: int,
    soglia: float = HISTORY_COMPACT_THRESHOLD,
) -> list[dict]:
    """Sliding window: elimina i turni piu' vecchi mantenendo system + coda.

    Rimuove sempre gruppi completi (assistant + relativi tool) per non lasciare
    ``tool_call_id`` orfani, che fanno fallire il template di chat.

    Lineare, non quadratica. Prima ogni ``pop(0)`` ricalcolava la pressione
    sull'**intera** lista: misurato, 80 ms su 800 messaggi, con il costo per
    messaggio che cresce con la dimensione -- cioe' proprio quando questa
    funzione serve, che e' quando la cronologia e' enorme. Qui il costo di ogni
    messaggio si calcola una volta sola e si sottrae.
    """
    head = [m for m in api_messages if m.get("role") == "system"]
    body = [m for m in api_messages if m.get("role") != "system"]

    costi = [estimate_messages_tokens([m]) for m in body]
    totale = estimate_messages_tokens(head) + sum(costi)
    tetto = num_ctx * soglia
    i = 0
    while i < len(body) and totale > tetto and len(body) - i > 2:
        totale -= costi[i]
        i += 1
        # I risultati seguono la chiamata che li ha chiesti: un ``tool`` senza
        # l'``assistant`` che lo precede e' un messaggio che il backend rifiuta.
        while i < len(body) and body[i].get("role") == "tool":
            totale -= costi[i]
            i += 1
    body = body[i:]

    if len(body) < len([m for m in api_messages if m.get("role") != "system"]):
        head.append(
            {
                "role": "system",
                "content": (
                    "[Nota: i turni piu' vecchi di questa conversazione sono stati "
                    "rimossi dal contesto per rientrare nella finestra. Se ti serve "
                    "un'informazione precedente, rileggila dal disco con read_file.]"
                ),
            }
        )
    return head + body


# ---------------------------------------------------------------------------
# Parser di fallback (sostituisce il vecchio parser a regex)
# ---------------------------------------------------------------------------

REQUIRED_ARGS: dict[str, set[str]] = {
    "read_file": {"filepath"},
    "write_file": {"filepath", "content"},
    "edit_file": {"filepath", "old_string"},
    "search_files": {"pattern"},
    "run_command": {"command"},
    "manage_memory": {"action"},
    "list_files": set(),
    ASK_USER_TOOL: {"question"},
}

_DECODER = json.JSONDecoder()


def extract_json_objects(text: str) -> list[tuple[int, int, dict[str, Any]]]:
    """Trova tutti gli oggetti JSON di primo livello in un testo.

    Usa ``JSONDecoder.raw_decode`` invece di una regex a graffe bilanciate.
    Motivo: una regex non puo' distinguere le graffe *di struttura* da quelle
    *dentro una stringa*. Il caso reale che rompeva il recupero era una
    ``write_file`` il cui ``content`` conteneva codice Python::

        {"name": "write_file", "arguments": {"filepath": "x.py",
         "content": "shopping_lists = {}\\n..."}}

    Le graffe di ``{}`` nel codice facevano fallire il match, il recupero non
    scattava e il JSON finiva stampato in chat come testo. ``raw_decode``
    conosce la grammatica JSON, quindi gestisce annidamento, stringhe con
    graffe, escape e virgolette senza casi particolari.

    Restituisce ``(inizio, fine, oggetto)`` per ogni oggetto trovato, in ordine.
    """
    results: list[tuple[int, int, dict[str, Any]]] = []
    index = 0
    length = len(text)
    while index < length:
        start = text.find("{", index)
        if start == -1:
            break
        try:
            obj, end = _DECODER.raw_decode(text, start)
        except ValueError:
            index = start + 1
            continue
        if isinstance(obj, dict):
            results.append((start, end, obj))
            index = end
        else:
            index = start + 1
    return results


def _as_tool_call(data: dict[str, Any], seq: int) -> dict[str, Any] | None:
    """Normalizza un dict JSON in una tool call, o ``None`` se non lo e'.

    Accetta le forme che i modelli locali producono davvero:
      ``{"name": ..., "arguments": {...}}``
      ``{"name": ..., "parameters": {...}}``
      ``{"function": {"name": ..., "arguments": {...}}}``
      ``{"tool": ..., "args": {...}}``
    """
    inner = data.get("function")
    if isinstance(inner, dict):
        data = inner

    name = data.get("name") or data.get("tool") or data.get("tool_name")
    if not isinstance(name, str) or name not in TOOL_NAMES:
        return None

    args: Any = data.get("arguments")
    if args is None:
        args = data.get("parameters")
    if args is None:
        args = data.get("args")
    if args is None:
        args = {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return None
    if not isinstance(args, dict):
        return None

    # Nessun default inventato: se manca un parametro obbligatorio si rinuncia.
    # E' la regola che impedisce il ritorno dei file spuri creati dalla v1.
    if REQUIRED_ARGS.get(name, set()) - set(args):
        return None

    return {
        "id": f"recovered_{int(time.time() * 1000) % 100000}_{seq}",
        "name": name,
        "arguments": json.dumps(args, ensure_ascii=False),
    }


def parse_text_tool_calls(text: str) -> tuple[list[dict[str, Any]], str]:
    """Recupera le tool call che il modello ha stampato come testo.

    Restituisce ``(chiamate, testo_residuo)``: il residuo e' il messaggio
    ripulito dai blocchi JSON consumati, cosi' l'eventuale prosa attorno resta
    visibile in chat mentre la chiamata finisce nella tendina del tool.
    """
    if not text or "{" not in text:
        return [], text

    calls: list[dict[str, Any]] = []
    spans: list[tuple[int, int]] = []
    for seq, (start, end, obj) in enumerate(extract_json_objects(text)):
        call = _as_tool_call(obj, seq)
        if call:
            calls.append(call)
            spans.append((start, end))

    if not calls:
        return [], text

    leftover_parts: list[str] = []
    cursor = 0
    for start, end in spans:
        leftover_parts.append(text[cursor:start])
        cursor = end
    leftover_parts.append(text[cursor:])
    leftover = "".join(leftover_parts)
    # Ripulisce i resti delle recinzioni markdown rimaste orfane.
    leftover = re.sub(r"```(?:json|tool_call|tool_code)?\s*```", "", leftover)
    leftover = re.sub(r"\n{3,}", "\n\n", leftover).strip()

    return calls, leftover


def parse_text_tool_call(text: str) -> dict[str, Any] | None:
    """Compatibilita': la prima tool call recuperata, o ``None``."""
    calls, _ = parse_text_tool_calls(text)
    return calls[0] if calls else None


def is_streaming_tool_json(text: str) -> bool:
    """Il testo *in arrivo* e', molto probabilmente, una tool call in JSON.

    Controllo volutamente grezzo perche' gira ad ogni refresh dello stream: la
    prosa non comincia mai con una graffa o con una recinzione ```json. Serve
    alla UI per sostituire il JSON con un segnaposto gia' mentre scorre,
    invece di riversarlo in chat e toglierlo dopo.
    """
    stripped = (text or "").lstrip()
    if not stripped:
        return False
    if stripped.startswith("```"):
        newline = stripped.find("\n")
        stripped = stripped[newline + 1:].lstrip() if newline != -1 else ""
    return stripped.startswith("{") and '"' in stripped


def looks_like_raw_tool_json(text: str) -> bool:
    """Il testo e' (anche) un blob JSON di tool call non eseguibile?

    Serve alla UI: un output del genere non va mai riversato in chat come
    markdown, nemmeno quando il recupero fallisce.
    """
    if not text or "{" not in text:
        return False
    for _, _, obj in extract_json_objects(text):
        candidate = obj.get("function") if isinstance(obj.get("function"), dict) else obj
        name = candidate.get("name") or candidate.get("tool") or candidate.get("tool_name")
        if isinstance(name, str) and name in TOOL_NAMES:
            return True
    return False


# Segnali che il testo non e' un'azione mancata ma una domanda: il modello si
# e' fermato perche' la richiesta non basta a decidere. Osservato in sessione:
# qwen chiedeva correttamente cosa intendesse l'utente e l'harness gli
# rispondeva "hai risposto a parole, esegui adesso l'azione", spingendolo a
# inventarsi la specifica invece di aspettarla. Il nudge combatteva contro la
# regola del system prompt che gli dice di chiedere.
_QUESTION_MARKERS = (
    "quale preferisci", "come preferisci", "cosa intendi", "che cosa intendi",
    "vuoi che", "preferisci che", "ho bisogno di sapere", "mi serve sapere",
    "prima di procedere ho bisogno", "prima di procedere mi serve",
    "non e' chiaro se", "non è chiaro se", "non ho abbastanza",
    "puoi chiarire", "puoi confermare", "confermi che", "fammi sapere",
    "ci sono due possibili", "due interpretazioni", "ambiguo",
    "which would you", "could you clarify", "do you want me to",
)


def looks_like_clarifying_question(text: str) -> bool:
    """Il modello sta chiedendo un chiarimento, non rimandando un'azione?

    Due indizi indipendenti, perche' uno solo sbaglia troppo: un marcatore
    esplicito fra quelli sopra, oppure una densita' di punti interrogativi che
    in una risposta operativa non ci sarebbe.
    """
    body = strip_think(text or "").strip()
    if not body:
        return False
    lowered = body.lower()
    if any(marker in lowered for marker in _QUESTION_MARKERS):
        return True
    # Almeno due domande, o una sola domanda in una risposta corta: e' una
    # richiesta di chiarimento, non un preambolo prima di agire.
    domande = body.count("?")
    return domande >= 2 or (domande == 1 and len(body) < 400 and body.rstrip().endswith("?"))


def looks_like_unexecuted_action(text: str) -> bool:
    """Euristica: il modello promette un'azione invece di eseguirla."""
    if not text:
        return False
    lowered = strip_think(text).lower()
    if len(lowered) < 15:
        return False
    intents = (
        "creo il file", "creero", "ora scrivo", "adesso scrivo", "procedo a",
        "vado a creare", "vado a modificare", "eseguo il comando", "lancio il comando",
        "ti creo", "scrivero", "modifichero", "posso creare", "dovrei leggere",
        "let me create", "i will create", "i'll write", "i will run",
    )
    return any(token in lowered for token in intents)


# Verbi che, nella richiesta dell'utente, implicano un'operazione sul disco.
_ACTION_VERBS = (
    "crea", "creare", "genera", "generare", "scrivi", "scrivere", "aggiungi",
    "aggiungere", "modifica", "modificare", "correggi", "correggere", "sistema",
    "rinomina", "rifattorizza", "refactor", "implementa", "implementare",
    "cancella", "elimina", "leggi", "leggere", "apri", "mostrami il file",
    "elenca", "lista", "guarda", "ispeziona", "analizza il", "controlla",
    "verifica", "esegui", "eseguire", "lancia", "avvia", "installa", "testa",
    "trova", "cerca", "dove si trova", "dove sta", "quali file", "che file",
    "compila", "committa", "ricordati", "memorizza",
)

# Sostantivi che ancorano la richiesta al workspace.
_WORKSPACE_NOUNS = (
    "file", "cartella", "directory", "progetto", "repo", "repository", "codice",
    "script", "modulo", "funzione", "classe", "test", "workspace", "comando",
    ".py", ".js", ".ts", ".json", ".md", ".txt", ".toml", ".yml", ".yaml",
)


# Radici di verbi che, ripetute, segnalano una richiesta con piu' obiettivi.
# Sono radici e non parole intere per contare "correggi" e "correggere" una
# volta sola: con le forme complete il conteggio si gonfiava da solo e
# qualunque frase lunga sembrava multi-step.
_MULTI_STEP_STEMS = (
    "crea", "genera", "scriv", "aggiung", "modific", "corregg", "implement",
    "rimuov", "elimin", "esegu", "lanci", "verific", "analizz", "document",
    "rifattorizz", "estend", "ottimizz", "riscriv", "sposta", "rinomin",
)

# "STEP 1", "1.", "2)" a inizio riga: la firma tipografica di un elenco di
# compiti. Due occorrenze bastano -- una sola puo' essere un esempio.
_STEP_MARKER = re.compile(r"(?:^|\n)\s*(?:step\s*\d+|\d+[.)])\s", re.I)

# Un file con estensione nota, oppure un percorso di cartella. Le estensioni
# sono un elenco chiuso e non `\w+` perche' altrimenti "3.12" e "art. 81" e
# ogni frase che finisce con un punto diventerebbero file.
_ARTEFATTO = re.compile(
    r"[\w./-]*\w[\w-]*\.(?:py|md|txt|csv|tsv|json|ya?ml|toml|ini|cfg|sql|sh|"
    r"bat|ps1|html?|css|jsx?|tsx?|rs|go|java|c|h|cpp|rb|php|xml|lock)\b"
    r"|[\w-]+(?:/[\w-]+)*/",
)

# Quanti artefatti distinti fanno una richiesta strutturata. Quattro: sotto,
# "scrivi il modulo e il suo test" ne conta due o tre ed e' un compito solo.
MIN_ARTEFATTI = 4

# Le radici d'azione cercate a inizio parola. Il confronto per sottostringa
# contava "gestendo" come "estend" e "documento" come "document": rumore che
# faceva scattare il sollecito su richieste con un obiettivo solo.
_RADICI_AZIONE = re.compile(
    r"\b(?:" + "|".join(_MULTI_STEP_STEMS) + r")", re.I
)


def artefatti_nominati(text: str) -> set[str]:
    """I file e le cartelle distinti che la richiesta nomina.

    E' il terzo segnale, aggiunto dopo una prova andata male: una richiesta
    scritta in prosa -- "dentro `cantiere/grezzi/` quattro file di testo...
    `cantiere/misura.py` ne ricava `cantiere/misure.csv`..." -- non ha nessun
    elenco numerato e nessun verbo all'imperativo, quindi passava sotto i primi
    due segnali senza toccarli. Ma nominava otto artefatti da produrre, e otto
    artefatti sono otto lavori: il modello e' partito senza piano e ha finito i
    passi. Contare le cose da consegnare coglie la struttura anche quando chi
    scrive descrive un risultato invece di ordinare delle azioni.
    """
    trovati = {m.group(0).lower().lstrip("./") for m in _ARTEFATTO.finditer(text or "")}
    # `cantiere/` e `cantiere/grezzi/` sono due contenitori diversi, ma
    # `cantiere/` e `cantiere/misura.py` non vanno contati due volte: la
    # cartella che contiene una cosa gia' contata non e' un lavoro in piu'.
    return {
        a for a in trovati
        # Via i contenitori gia' rappresentati da qualcosa che sta dentro
        # (`cantiere/` accanto a `cantiere/misura.py`) e le citazioni in forma
        # breve della stessa cosa (`lunghi/` accanto a `cantiere/lunghi/`):
        # nominare un artefatto due volte non lo raddoppia.
        if not any(b != a and (b.startswith(a) or b.endswith(a)) for b in trovati)
    }


def looks_multi_step(text: str) -> bool:
    """La richiesta contiene piu' obiettivi distinti?

    Serve a decidere se pretendere un piano. Volutamente conservativa: un
    falso positivo costa un giro di manage_plan su un compito semplice
    (fastidioso), un falso negativo riporta al problema di partenza -- il
    modello che prova a fare tutto in un ragionamento solo (grave). La soglia
    sulla lunghezza esiste perche' "leggi il file e dimmi cosa fa" ha due verbi
    ma un obiettivo solo.

    Tre segnali indipendenti, in ordine di quanto sono difficili da sbagliare:
    la tipografia di un elenco, il numero di verbi d'azione, il numero di
    artefatti nominati. Basta uno.
    """
    if not text or len(text) < 220:
        return False
    if len(_STEP_MARKER.findall(text)) >= 2:
        return True
    if len(_RADICI_AZIONE.findall(text)) >= 3:
        return True
    return len(artefatti_nominati(text)) >= MIN_ARTEFATTI


def user_expects_tool_use(text: str) -> bool:
    """La richiesta dell'utente implica un'operazione sul workspace?

    Serve al nudge automatico: se l'utente ha chiesto di *fare* qualcosa e il
    modello ha risposto solo a parole, l'harness lo sollecita da solo invece di
    lasciare che sia l'utente a doverlo fare ogni volta -- che era esattamente
    il sintomo riportato ("i tool li usa solo se glielo dico esplicitamente").
    """
    if not text:
        return False
    lowered = " " + text.lower().strip() + " "
    has_verb = any(v in lowered for v in _ACTION_VERBS)
    has_noun = any(n in lowered for n in _WORKSPACE_NOUNS)
    return has_verb and has_noun


# Livelli di pensiero, dal piu' economico al piu' caro. Solo questi si possono
# abbassare: un ``think`` booleano non ha una manopola da girare.
_THINK_LEVELS = ("low", "medium", "high", "max")


# Da questo passo in poi, con un punto del piano aperto, si e' dentro
# l'esecuzione da un pezzo: la mossa e' gia' stata scelta due volte, e il
# pensiero scende di **due** scalini invece di uno. Il numero viene dalla
# sessione del 19/08/2026 (Qwen3.8:27b, ctx 98k): al passo 3 di un punto aperto
# il modello produceva ancora 41.000 e 49.000 caratteri di solo pensiero prima
# di una edit_file -- non stava decidendo, stava rileggendosi.
DEEP_STEP = 3


# Riserva fra il prompt e il tetto di generazione, in token.
#
# La stima dei token non e' il tokenizer: e' una regola su caratteri e parole,
# e sbaglia in entrambe le direzioni. Un margine serve perche' l'errore che
# conta e' solo uno dei due -- sottostimare il prompt vuol dire promettere al
# modello uno spazio che il server non ha, ed e' esattamente la condizione che
# taglia le tool call a meta'.
MARGINE_FINESTRA = 768

# Sotto questo tetto un passo agentico non ha piu' senso: non ci sta un
# pensiero, una risposta e una chiamata. Meglio saperlo prima di generare.
TETTO_INUTILE = 512


def tetto_per_la_finestra(
    api_messages: Sequence[dict[str, Any]], num_ctx: int, max_tokens: int
) -> tuple[int, int]:
    """Il tetto di generazione che ci sta **davvero**, e lo spazio rimasto.

    ``max_tokens`` e ``num_ctx`` sono due impostazioni indipendenti, e nessuno
    le confrontava: con una finestra da 32k e un tetto da 16k basta un prompt
    di 24k -- cioe' il livello a cui la compattazione lascia le cose -- perche'
    la somma sfondi. Il server non rifiuta: genera finche' la finestra e'
    piena e poi si ferma, dove capita. Se capita dentro gli argomenti di una
    tool call, quello che arriva qui e' JSON monco.

    Non e' un problema di llama.cpp -- vale per qualunque endpoint -- ma li' e'
    peggiore, perche' la finestra la fissa ``-c`` al lancio e non si allarga
    chiedendo, e perche' il server puo' rispondere con un *context shift*
    silenzioso invece che con un errore.

    Ritorna ``(tetto, spazio)``. ``spazio`` viaggia a parte perche' e' il
    numero che vale la pena raccontare: dice quanto manca alla parete, non
    quanto si e' deciso di chiedere.
    """
    if num_ctx <= 0 or max_tokens <= 0:
        return max_tokens, 0
    spazio = num_ctx - estimate_messages_tokens(api_messages) - MARGINE_FINESTRA
    return min(max_tokens, max(spazio, 0)), spazio


def argomenti_illeggibili(
    arguments: str, done_reason: str, max_tokens: int
) -> dict[str, Any]:
    """Il risultato da restituire quando gli argomenti di una tool call non
    sono JSON valido.

    Due cause diversissime, finora raccontate con la stessa frase:

    * il modello ha scritto male il JSON -- succede, si riemette e passa;
    * la **generazione e' stata tagliata** a meta' della chiamata, perche' il
      tetto di ``max_tokens`` o la finestra del server sono finiti prima.

    Nel secondo caso "Argomenti JSON malformati / riemetti un oggetto JSON
    valido" e' un consiglio che non puo' funzionare: riemettere la stessa
    chiamata la fa finire nello stesso punto, e il ciclo si ripete finche' i
    passi non sono esauriti. Il server lo dice -- ``finish_reason: length`` --
    e da oggi lo dice anche a noi (vedi ``done_reason`` nel transport
    OpenAI-compatibile, dove per anni non e' stato letto).

    La coda degli argomenti viaggia comunque: e' l'unico modo, per chi legge
    il pannello, di vedere *dove* si e' fermata.
    """
    coda = (arguments or "")[:300]
    if done_reason != "length":
        return {
            "error": "Argomenti JSON malformati.",
            "hint": "Riemetti la chiamata con un oggetto JSON valido.",
            "received": coda,
        }
    return {
        "error": "Chiamata troncata: la generazione si e' fermata a meta' degli argomenti.",
        "causa": (
            "Il server ha chiuso con 'length'. O e' finito il tetto di "
            f"max_tokens ({max_tokens} token), o e' finito lo spazio nella "
            "finestra di contesto: in entrambi i casi la chiamata non e' mai "
            "stata emessa per intero."
        ),
        "hint": (
            "Riemetterla identica finira' nello stesso punto. Spezzala: meno "
            "contenuto per chiamata, piu' chiamate -- per esempio una scrittura "
            "corta e poi un edit_file che aggiunge il resto."
        ),
        "received": coda,
    }


def watchdog_chars_for_step(max_tokens: int, step: int) -> int:
    """Caratteri di ragionamento oltre i quali il passo viene interrotto.

    Due limiti, e vince il piu' stretto:

    * la **quota** del budget di generazione (``WATCHDOG_RATIO``), che protegge
      dal caso "il pensiero si mangia tutto e non resta niente per l'azione";
    * un **tetto assoluto** (``THINK_CEILING_*``), che protegge dal caso in cui
      il budget e' cosi' largo che la quota non limita piu' niente.

    Il secondo e' quello nuovo, e il motivo e' aritmetico: la quota e' una
    frazione di ``max_tokens``, quindi cresce con la finestra. Passando da 16k
    a 98k di contesto la soglia e' passata da 4.500 a 17.600 token senza che
    nessuno l'avesse deciso. Un tetto in token non si muove quando si cambia
    macchina.

    Con ``max_tokens`` piccolo il minimo resta la quota: sulle finestre strette
    il comportamento e' identico a prima.
    """
    if max_tokens <= 0:
        return 0
    tetto = THINK_CEILING_FIRST if step <= 1 else THINK_CEILING_STEP
    return chars_for_tokens(min(max_tokens * WATCHDOG_RATIO, tetto))


def think_for_step(configured: Any, plan: Any, step: int) -> Any:
    """Livello di pensiero da usare in questo passo.

    L'idea: **pensare a lungo serve a decidere, non a eseguire**. Quando c'e'
    un punto del piano gia' aperto, la decisione e' stata presa -- al passo in
    cui e' stato aperto, pagando il pensiero pieno -- e ripensarla ad ogni tool
    e' tempo di GPU speso per riottenere la stessa risposta. Il primo passo di
    ogni turno resta al livello configurato: e' li' che si legge la richiesta
    nuova e si decide come muoversi.

    Si scende di uno appena il punto e' aperto e di **due** da ``DEEP_STEP`` in
    poi. Il pavimento resta 'low' e non si azzera mai: su un modello che ragiona
    togliere del tutto il pensiero peggiora le tool call, che e' esattamente il
    problema che si voleva evitare.

    ## Quando non puo' fare niente, e perche' non si vedeva

    Con ``native_think`` impostato su **'auto'** -- che e' il default --
    ``AppState.think_setting`` ritorna un **booleano** (la capability rilevata
    dal modello), non un livello: qui non c'e' nessuna scala su cui scendere e
    la funzione restituisce il valore intatto. La modulazione, cioe', esiste
    solo se l'utente ha scelto un livello esplicito.

    Non e' un difetto di questa funzione ed e' un fatto che va **visto**: nelle
    quattro sessioni qwen3.8 misurate il 23/08/2026 il pensiero non si
    accorciava mai col passo, e dai `<think>` salvati non c'era modo di capire
    se lo scalino non si applicasse o non si notasse. Adesso la traccia sul
    messaggio dell'assistente scrive sia il livello configurato sia quello
    davvero chiesto: ``configurato: "on"`` vuol dire esattamente questo --
    booleano, niente da modulare.
    """
    if not isinstance(configured, str):
        return configured          # booleano: niente da modulare
    livello = configured.strip().lower()
    if livello not in _THINK_LEVELS:
        return configured
    if step <= 1 or plan is None or getattr(plan, "current", None) is None:
        return configured
    scalini = 2 if step >= DEEP_STEP else 1
    return _THINK_LEVELS[max(0, _THINK_LEVELS.index(livello) - scalini)]


# Sentinella: "questa chiamata non ha niente a che vedere con l'anteprima".
# Serve perche' ``None`` significa gia' qualcosa di preciso -- chiudi
# l'anteprima -- e i due casi non vanno confusi.
_NO_PREVIEW = object()


def render_preview_note(preview: dict[str, Any] | None) -> str:
    """Cosa l'utente ha davanti nel pannello, detto al modello.

    Il pannello si apre **da solo** quando l'agente scrive un file visuale, e
    quello e' un fatto che lui non puo' dedurre da nessuna parte: l'ha aperto
    l'harness, non lui. Senza questa riga il comportamento osservato e'
    inevitabile -- prova a mostrare una cosa che e' gia' sullo schermo, o a
    riavviare un server che sta gia' girando.
    """
    if not preview:
        return ""
    if preview.get("kind") == "app":
        # Terminale e schermo sono due porte come le altre, ma per chi guarda
        # sono cose diverse -- e la mossa sbagliata e' diversa: riavviare un
        # server e' inutile, riavviare il terminale dell'utente gli butta via
        # quello che ci stava facendo.
        modo = preview.get("mode")
        if modo == "terminal":
            cosa = f"un terminale della sandbox, aperto sulla porta {preview.get('port')}"
            coda = (
                "E' suo, non tuo: i comandi continua a darli con run_command. "
                "Non riaprirlo e non fermarlo senza che te lo chieda -- "
                "potrebbe averci qualcosa a meta'."
            )
        elif modo == "gui":
            cosa = f"{preview.get('title') or 'una finestra'}, sulla porta {preview.get('port')}"
            coda = (
                "La finestra e' gia' davanti a lui e ci puo' cliccare dentro. "
                "Non rilanciarla: se hai cambiato il codice, fermala con "
                "action='stop' e riaprila con action='gui'."
            )
        else:
            cosa = (
                f"un'applicazione che hai avviato tu, in esecuzione sulla porta "
                f"{preview.get('port')} ({preview.get('url_path') or '/'})"
            )
            coda = (
                "E' gia' in esecuzione e gia' sul suo schermo: non riavviarla. "
                "Usa preview action='stop' per fermarla, o action='serve' su "
                "un'altra porta solo se ti serve una seconda applicazione."
            )
    else:
        percorso = str(preview.get("path") or "")
        radice = preview.get("root")
        if percorso.lower().endswith((".html", ".htm")):
            dove = f"la cartella {radice}" if radice else "la cartella del workspace"
            cosa = (
                f"la pagina {percorso}, servita insieme a tutta {dove}: fogli "
                "di stile, script e immagini ci sono gia'"
            )
        else:
            cosa = f"il file {percorso}"
        # Il pannello si ricarica da solo ad ogni write_file dentro la cartella
        # mostrata. Senza dirglielo il modello richiama preview dopo ogni
        # correzione -- un passo intero per non fare niente, e la stessa
        # ragione per cui questo blocco esiste.
        coda = (
            "E' gia' sul suo schermo e si ricarica da sola ogni volta che "
            "scrivi un file di quella cartella: non richiamare preview dopo "
            "una modifica. Chiamalo solo per mostrare qualcosa di diverso."
        )
        if percorso.lower().endswith((".html", ".htm")):
            # L'unico motivo legittimo per richiamarlo sulla stessa pagina, e
            # va detto qui: il pannello si e' aperto da solo su un write_file,
            # e quella strada mostra il file senza accendere niente.
            coda += (
                " L'unica eccezione: se la pagina ha bisogno del suo backend, "
                "chiama preview action='file' su di lei -- l'harness riconosce "
                "il progetto e lo avvia."
            )
    return f"<anteprima>\nL'utente sta guardando {cosa}.\n</anteprima>\n{coda}"


def preview_from_result(
    name: str, args: dict[str, Any], result: str, ok: bool, *, auto: bool = True
) -> Any:
    """L'anteprima implicata da una tool call, se ce n'e' una.

    Due sorgenti, e la distinzione e' il cuore del progetto di questa funzione:

    * il tool ``preview``, quando il modello **dichiara** cosa mostrare. Qui il
      payload lo costruisce lui e noi lo passiamo;
    * un ``write_file``/``edit_file`` su un file con una resa visiva, dove
      l'anteprima si **deduce**. Chiedere al modello di annunciare ogni file
      che scrive costerebbe un round-trip a testa e verrebbe dimenticato: se
      l'informazione ce l'abbiamo gia', deve essere gratis.

    Il dedotto si ferma ai tipi renderizzabili (.md, .html, .svg, immagini,
    .pdf). Su un .py no: dieci file di codice scritti di fila sarebbero dieci
    anteprime che nessuno ha chiesto, e il pannello diventerebbe rumore.
    """
    if not ok:
        return _NO_PREVIEW

    if name == PREVIEW_TOOL:
        try:
            payload = json.loads(result)
        except json.JSONDecodeError:
            return _NO_PREVIEW
        if not isinstance(payload, dict) or "preview" not in payload:
            return _NO_PREVIEW
        return payload["preview"]          # None = chiudi (action='stop')

    if auto and name in {"write_file", "edit_file"}:
        percorso = str(args.get("filepath") or "")
        if percorso and preview_kind(percorso) == "render":
            return {
                "kind": "render",
                "path": percorso,
                "title": percorso.rsplit("/", 1)[-1],
                "auto": True,
            }

    return _NO_PREVIEW


def ultima_risposta(ui_messages: Sequence[dict[str, Any]]) -> str:
    """Il testo dell'ultima risposta dell'assistente, dopo l'ultimo messaggio
    dell'utente. Serve a sapere se il turno ha detto qualcosa o no.

    Si guarda solo da li' in poi: la risposta del turno **precedente** e' in
    cronologia e non conta -- e' proprio l'errore che farebbe passare per
    "gia' risposto" un turno rimasto muto.
    """
    fuori = []
    for msg in reversed(ui_messages):
        if msg.get("role") == "user" and not msg.get("hidden"):
            break
        if msg.get("role") == "assistant":
            fuori.append(strip_think(str(msg.get("content") or "")).strip())
    return next((t for t in fuori if t), "")


# Tetto al riepilogo forzato: e' un referto su lavoro gia' fatto, non un
# ragionamento nuovo. Stessa taglia del riassunto di compattazione.
MAX_TOKEN_RIEPILOGO = 700


def riepilogo_finale(
    ui_messages: Sequence[dict[str, Any]],
    *,
    backend: Any,
    params: Any,
    budgets: Any,
    strip_thinking: bool,
) -> str:
    """Chiede al modello cosa e' successo, quando i passi sono gia' finiti.

    Una chiamata sola, senza tool e senza pensiero: qui non c'e' niente da
    decidere, c'e' da raccontare. Un riepilogo mancato non deve peggiorare le
    cose -- si torna stringa vuota e il turno si chiude come prima.
    """
    api = build_api_messages(
        ui_messages,
        system_prompt=PROMPT_RIEPILOGO_FINALE,
        env_header=None,
        strip_thinking=strip_thinking,
        compact_old_tools=True,
        budgets=budgets,
    )
    if len(api) < 2:
        return ""
    p = replace(
        params,
        think=False,
        temperature=0.2,
        max_tokens=min(
            int(getattr(params, "max_tokens", 2048) or 2048), MAX_TOKEN_RIEPILOGO
        ),
    )
    pezzi: list[str] = []
    try:
        for evento in backend.stream(api, None, p):
            if evento.kind == "content":
                pezzi.append(evento.text)
            elif evento.kind == "error":
                return ""
    except Exception:  # noqa: BLE001
        return ""
    return strip_think("".join(pezzi)).strip()


def _nome_livello(think: Any) -> str:
    """Il livello di pensiero in forma leggibile, qualunque cosa sia.

    ``think`` e' una stringa sui modelli che accettano i livelli e un booleano
    su tutti gli altri: una traccia che scrivesse ``true`` in un campo e
    ``high`` nell'altro sarebbe illeggibile fra un mese.
    """
    if isinstance(think, str):
        return think.strip().lower() or "?"
    if think is True:
        return "on"
    if think is False:
        return "off"
    return "?"


def params_for_step(params: Any, plan: Any, step: int) -> Any:
    """I parametri di generazione di questo passo, col pensiero modulato.

    Ritorna l'oggetto originale quando non c'e' niente da cambiare: cosi' i
    backend e i test che confrontano l'identita' dei parametri continuano a
    vedere esattamente quello che hanno passato.
    """
    configurato = getattr(params, "think", None)
    livello = think_for_step(configurato, plan, step)
    if livello == configurato:
        return params
    try:
        return replace(params, think=livello)
    except TypeError:      # non e' una dataclass: si lascia stare
        return params


def last_user_request(ui_messages: Sequence[dict[str, Any]]) -> str:
    for msg in reversed(ui_messages):
        if msg.get("role") == "user" and not msg.get("hidden"):
            return str(msg.get("content") or "")
    return ""


def answered_question_pending(ui_messages: Sequence[dict[str, Any]]) -> bool:
    """L'utente ha risposto a un ask_user_question dopo l'ultimo suo messaggio?

    Serve al vincolo di sola lettura: la sua uscita di sicurezza e' chiedere, e
    quell'uscita deve poter cambiare la risposta. Se il modello chiede "vuoi che
    la implementi?" e l'utente dice di si', il divieto non ha piu' ragione di
    esistere -- ma il messaggio visibile dell'utente e' ancora quello analitico
    di prima, quindi senza questo controllo il vincolo resterebbe in piedi.
    """
    for msg in reversed(ui_messages):
        if msg.get("role") == "user" and not msg.get("hidden"):
            return False
        if msg.get("role") == "tool" and msg.get("answered"):
            return True
    return False


# ---------------------------------------------------------------------------
# Loop agentico
# ---------------------------------------------------------------------------


def run_turn(
    *,
    backend: Any,
    params: Any,
    tools_schema: list[dict[str, Any]],
    tool_ctx: ToolContext,
    ui_messages: list[dict[str, Any]],
    system_prompt: str,
    env_header: str | None,
    max_steps: int = 12,
    strip_thinking: bool = True,
    compact_old_tools: bool = True,
    budgets: Any | None = None,
    enable_nudge: bool = True,
    require_summary: bool = True,
    require_plan: bool = True,
    think_watchdog: bool = True,
    auto_preview: bool = True,
    compact_history: bool = True,
    soglia: float = SOGLIA_DEFAULT,
    compact_max_tokens: int = TETTO_TOKEN_DEFAULT,
    libreria_attiva: bool = True,
    estratto_pensiero: bool = True,
    # Blocco di coda calcolato dal chiamante a ogni passo, ricevendo quanti
    # passi restano. Lo usa la delega per dire all'esploratore quanto tempo ha:
    # e' ``agent`` a conoscere ``delega``, non il contrario, quindi il testo
    # arriva da fuori come ``run_turn`` e ``build_messages``.
    blocco_coda: Callable[[int], str] | None = None,
    spec_delega: bool = True,
    plan_gate: bool = True,
    skills_block: str = "",
    abilita_delega: bool = True,
    should_stop: Callable[[], bool] | None = None,
    images: list[str] | None = None,
) -> Iterator[AgentEvent]:
    """Esegue un turno completo. Muta ``ui_messages`` in-place via append.

    ``ui_messages`` e' l'unico stato: i messaggi che vi finiscono sono
    esattamente quelli che la UI ridisegna e che vengono salvati su disco.

    ``should_stop`` viene interrogato nei tre punti in cui l'interruzione e'
    sicura: prima di chiamare il modello, mentre arrivano i token, e prima di
    eseguire ogni tool. Non si taglia mai *dentro* un tool gia' partito: un
    write_file interrotto a meta' lascerebbe un file monco sul disco.
    """
    nudged = False
    leak_nudged = False
    ripetizione_nudged = False
    ripetizione_dovuta: tuple[str, int] | None = None
    ripetizioni = RipetizioniTool()
    summary_requested = False
    coverage_nudged = False
    verify_nudges = 0
    # Tetto COMPLESSIVO ai passi che le reti di sicurezza possono consumare.
    #
    # Ogni sollecito ha gia' il suo contatore, e ognuno preso da solo e' tarato
    # bene. Ma i tetti si sommano: 2 riprese di stream + 2 watchdog + 2
    # troncamenti + 2 verifiche + 3 flag = 11, contro un ``max_agent_loops`` di
    # serie di 12. Nel caso peggiore -- un modello debole su un compito
    # difficile, cioe' esattamente quello per cui i solleciti esistono -- al
    # lavoro restava UN passo, e ogni sollecito appende anche due messaggi in
    # cronologia: si toglie tempo e spazio insieme.
    #
    # Qui i solleciti si contendono un budget unico. Quando finisce, il turno
    # smette di correggersi e usa i passi che restano per lavorare.
    passi_di_servizio = 0
    max_passi_di_servizio = max(2, max_steps // 3)
    servizio_esaurito = False
    # Quante volte ogni rete di sicurezza e' entrata in funzione. Sono stampelle
    # nate per i modelli piccoli: costano un round-trip in piu' quando scattano
    # e zero quando non scattano, quindi la domanda non e' "servono in teoria"
    # ma "questo modello le fa scattare". Contarle e' l'unico modo di decidere
    # con i dati invece che a intuito quali si possono togliere.
    nudge_counts: dict[str, int] = {}

    def count_nudge(name: str) -> None:
        nudge_counts[name] = nudge_counts.get(name, 0) + 1
        # Stesso oggetto, non una copia: aggiornarlo qui basta perche' il
        # conteggio arrivi in TurnFinished anche per i nudge contati dopo.
        total_usage["nudges"] = nudge_counts

    # I budget di troncamento seguono la finestra reale del modello, non le
    # costanti tarate su 16k. Vanno messi nel ToolContext *prima* del primo
    # passo: sono i tool a doverli rispettare, e li leggono da li'.
    # Chi chiama puo' imporne uno suo (la delega passa al figlio i budget
    # stretti): se non arriva, si ricalcola dalla finestra come sempre.
    if budgets is None:
        budgets = budgets_for(int(getattr(params, "num_ctx", 0) or 0))
    tool_ctx.budgets = budgets

    # La delega si monta qui e non nel ToolContext perche' backend e parametri
    # li conosce il ciclo, non i tool. Il figlio non riceve il tool di delega:
    # un esploratore che delega e' una ricorsione che nessuno ha chiesto.
    if abilita_delega and tool_ctx.on_delega is None:
        def _delega(compito: str) -> dict[str, Any]:
            return delega_mod.esegui(
                compito,
                backend=backend,
                params=params,
                tools_schema=tools_schema,
                tool_ctx=tool_ctx,
                env_header=env_header,
                run_turn=run_turn,
                registra_esiti=spec_delega,
                # Serve al referto di chiusura: quando il figlio finisce i
                # passi senza rispondere, si rilegge il suo contesto per
                # ricavarne almeno un referto parziale.
                build_messages=build_api_messages,
            )

        tool_ctx.on_delega = _delega

    # vault_search si monta come la delega: backend e parametri li conosce il
    # ciclo, non i tool. Il cercatore non riceve ne' delega ne' vault_search
    # (lo schema del figlio e' filtrato a tre tool di lettura), quindi non c'e'
    # ricorsione possibile.
    if tool_ctx.on_vault_search is None:
        def _vault_search(vault: str, query: str) -> dict[str, Any]:
            return vault_search_mod.cerca_nel_vault(
                vault,
                query,
                backend=backend,
                params=params,
                tools_schema=tools_schema,
                tool_ctx=tool_ctx,
                env_header=env_header,
                run_turn=run_turn,
                registri=getattr(tool_ctx, "registri_vault", None),
            )

        tool_ctx.on_vault_search = _vault_search

    plan_nudged = False
    # Il cancello si apre una volta per turno: dopo che l'utente ha visto il
    # piano, ogni action='set' successiva e' una revisione fatta lavorando, e
    # fermare il lavoro ad ogni revisione lo renderebbe insopportabile.
    gate_mostrato = any(m.get("kind") == GATE_PIANO for m in ui_messages)
    truncated_nudges = 0
    watchdog_fires = 0
    riprese_stream = 0
    # Letture esplorative di fila, cioe' passi in cui il modello ha solo
    # guardato. Si azzera appena tocca il disco o delega: quello che si vuole
    # riconoscere e' l'esplorazione lunga, non la lettura prima di una modifica.
    esplorazioni_di_fila = 0
    delega_nudged = False

    # --- libreria dei concetti -------------------------------------------
    # L'indice sta in coda, e il precarico d'ufficio ci si appoggia sopra: il
    # recupero lo fa l'harness incrociando le parole del punto di piano aperto
    # con i titoli, perche' la meta' che rilegge, se lasciata a un invito, non
    # parte -- su questo progetto e' misurato (vedi ``core/libreria.py``).
    schedario = (
        tool_ctx.base if (libreria_attiva and getattr(tool_ctx, "base", None)) else None
    )

    def _blocco_libreria() -> str:
        if schedario is None:
            return ""
        elenco = libreria.voci(schedario)
        if not elenco:
            return ""
        indice = libreria.render_block(elenco)
        aperto = tool_ctx.plan.current if tool_ctx.plan else None
        contesto = (aperto.text if aperto else "") or last_user_request(ui_messages)
        ripescato = libreria.precarico(schedario, elenco, contesto)
        return "\n\n".join(p for p in (ripescato, indice) if p)

    blocco_libreria = _blocco_libreria()

    def _esempio_delega() -> str:
        """L'ultima delega che in questo workspace ha funzionato al primo colpo.

        Attaccata al sollecito invece che messa in coda al contesto: li'
        sarebbe un costo a ogni passo per un'informazione che serve nel momento
        in cui una delega si scrive, cioe' di rado. Un esempio vero vale piu'
        di tre righe su come formulare bene una domanda.
        """
        if not spec_delega or not getattr(tool_ctx, "base", None):
            return ""
        domanda = spec_delega_mod.esempio_riuscito(tool_ctx.base)
        return DELEGA_ESEMPIO.format(domanda=domanda) if domanda else ""

    def blocco_stato_vault() -> str:
        """Indice e coda del log della wiki, per i vault in modalita' wiki.

        Stava nel prompt di **sistema** (misurato: ~1.670 token) e conteneva
        ``wiki/index.md``, cioe' il file che ogni ingest riscrive: il prefisso
        si invalidava sull'operazione per cui la modalita' wiki esiste, e non
        di 1.670 token ma dal token zero.
        """
        cartella = getattr(tool_ctx, "vault_dir", "") or ""
        if not cartella or not vault_mod.is_modalita_vault(cartella):
            return ""
        return vault_mod.blocco_stato(cartella)

    def blocco_memoria_vault() -> str:
        """La memoria del vault, se si sta lavorando dentro uno.

        Si ricostruisce ad ogni passo da ``tool_ctx.vault_notes`` e non dal
        disco: il file cambia solo quando lo cambia il modello, e in quel caso
        il tool aggiorna anche la lista. Rileggerlo ad ogni passo sarebbe una
        stat e un ``json.load`` per un contenuto che quasi sempre e' lo stesso.
        """
        if not tool_ctx.vault_dir or not tool_ctx.vault_notes:
            return ""
        return vault_mod.blocco_note(
            vault_mod.VaultConfig(
                nome=PurePath(tool_ctx.vault_dir).name,
                note=tuple(tool_ctx.vault_notes),
            )
        )

    # La richiesta ha piu' obiettivi: allora il piano non e' un suggerimento.
    multi_step = looks_multi_step(last_user_request(ui_messages))
    # Soglia del watchdog sul ragionamento, in caratteri. Deve scattare
    # **prima** di num_predict, altrimenti non serve a niente: se aspettassimo
    # il tetto duro il modello avrebbe gia' speso tutto e non gli resterebbe
    # budget per agire. Con il 55% il pensiero ha spazio per essere serio e ne
    # resta abbastanza per emettere la tool call.
    max_tokens_turno = int(getattr(params, "max_tokens", 0) or 0)
    # L'avviso di finestra quasi piena si da' una volta per turno.
    avvisato_finestra = False
    # Una richiesta di sola lettura vale per il turno intero. Se pero' l'utente
    # ha appena risposto a un ask_user_question, ha parlato lui dopo: il
    # vincolo decade, altrimenti l'unica uscita di sicurezza sarebbe murata.
    tool_ctx.readonly_request = looks_like_readonly_request(
        last_user_request(ui_messages)
    ) and not answered_question_pending(ui_messages)
    tool_ctx.new_symbols.clear()
    if not answered_question_pending(ui_messages):
        # Il banco di prova riparte vuoto ad **ogni** turno, non solo davanti a
        # una richiesta di analisi. Il prompt dice al modello che `.analisi/`
        # "viene svuotata" e che non fa parte del progetto: finche' lo svuotava
        # solo il ramo di sola lettura, su una richiesta di costruzione quella
        # frase era falsa -- e il modello ci credeva. Osservato: ha lasciato
        # `.analisi/caso_negativo/` nella radice del workspace ragionando
        # "which gets cleared on every analysis anyway", violando il vincolo
        # dell'utente sui confini del lavoro. Le cartelle col punto non
        # compaiono in nessun elenco, quindi nemmeno lui poteva accorgersene.
        #
        # Non si svuota a fine turno di proposito: se una proposta e' stata
        # provata, il codice della prova resta li' da guardare fino alla mossa
        # successiva. E non si svuota quando l'utente ha appena risposto a una
        # domanda: quel turno e' la continuazione del precedente, e la prova
        # appena impostata dal modello e' esattamente cio' su cui deve tornare.
        reset_scratch(tool_ctx)
    # La potatura del deposito sta qui e non a ogni scrittura: e' una stat per
    # file su una cartella piccola, e farla dodici volte per turno cambierebbe
    # solo il numero di syscall. Il deposito **non** si svuota come il banco di
    # prova: un handle imbucato in un turno deve restare valido per tutta la
    # conversazione, o la cronologia porterebbe il puntatore a un file che non
    # esiste piu'.
    if getattr(tool_ctx, "deposito_attivo", False) and getattr(tool_ctx, "base", None):
        deposito_mod.pota(tool_ctx.base, int(getattr(tool_ctx, "deposito_max_mb", 0)))
    verification = VerificationTracker()
    tool_ctx.verification = verification
    tools_used = False
    total_usage: dict[str, Any] = {}
    stopped = should_stop or (lambda: False)

    # --- traccia del pensiero ---------------------------------------------
    # Misurato il 23/08/2026 sulle quattro sessioni qwen3.8: il pensiero **non**
    # si accorcia col passo -- mediana 3.327 caratteri al passo 1 e 3.452 al
    # passo 8+ -- benche' ``think_for_step`` debba scendere di uno scalino col
    # punto di piano aperto e di due dal terzo passo. Dai soli `<think>` salvati
    # non si puo' dire se non si applica o se uno scalino non si vede, perche'
    # il livello *chiesto* non era scritto da nessuna parte. Adesso lo e'.
    #
    # Sta sul messaggio dell'assistente e non in un file di log a parte: cosi'
    # e' gia' persistito nella sessione, gia' isolato nei test, e si legge
    # accanto al pensiero che descrive. Al modello non arriva mai --
    # ``build_api_messages`` guarda solo ruolo, contenuto e tool_calls.
    traccia: dict[str, Any] = {}
    stato_traccia = {"da": -1}

    def marca_pensiero() -> None:
        """Attacca la traccia al messaggio dell'assistente che l'ha prodotta.

        Si cerca all'indietro e solo fra i messaggi nati **dopo** la fine dello
        stream: un passo che non ha prodotto nessun messaggio (errore, stop)
        non deve marcare quello del passo precedente con numeri che non sono
        suoi.
        """
        if not traccia:
            return
        for i in range(len(ui_messages) - 1, stato_traccia["da"] - 1, -1):
            if ui_messages[i].get("role") == "assistant":
                ui_messages[i].setdefault("think", dict(traccia))
                break
        traccia.clear()

    def fine(reason: str, passi: int) -> TurnFinished:
        """Chiude il turno. Passa da qui per non perdere la traccia dell'ultimo
        passo, che e' proprio quello dei turni che finiscono male."""
        marca_pensiero()
        return TurnFinished(reason=reason, steps=passi, usage=total_usage)

    def halt(step: int, reasoning: str = "", answer: str = "") -> Iterator[AgentEvent]:
        """Chiude il turno salvando quel che il modello aveva gia' prodotto."""
        text = _wrap(reasoning.strip(), answer.strip())
        if text.strip():
            ui_messages.append(
                {"role": "assistant", "content": text, "stopped": True, "ts": time.time()}
            )
            yield AssistantTurn(
                content=answer.strip(), reasoning=reasoning.strip(), has_tool_calls=False
            )
        yield fine("stopped", step)

    for step in range(1, max_steps + 1):
        if stopped():
            yield from halt(step - 1)
            return
        # I tool sanno a che passo siamo: serve alla cache delle riletture,
        # che puo' rispondere "invariato" solo finche' il contenuto precedente
        # e' ancora nel contesto e non e' stato compattato via.
        tool_ctx.step = step
        # La traccia del passo precedente si attacca adesso: il suo messaggio
        # e' stato appeso da uno qualsiasi dei rami che chiudono un passo, e
        # farlo qui e' l'unico punto che li copre tutti senza toccarne otto.
        marca_pensiero()
        yield StepStarted(step=step, total=max_steps)

        api_messages = build_api_messages(
            ui_messages,
            system_prompt=system_prompt,
            env_header=env_header,
            strip_thinking=strip_thinking,
            compact_old_tools=compact_old_tools,
            images=images,
            budgets=budgets,
            # ``max_steps - step`` e non ``- step + 1``: e' quanti passi
            # restano *dopo* questo, cioe' quelli su cui puo' contare.
            plan_block=render_block(tool_ctx.plan, steps_left=max_steps - step),
            delega_block=blocco_coda(max_steps - step) if blocco_coda else "",
            preview_block=render_preview_note(tool_ctx.preview),
            notes_block=render_notes(tool_ctx.notes),
            skills_block=skills_block,
            libreria_block=blocco_libreria,
            vault_state_block=blocco_stato_vault(),
            vault_notes_block=blocco_memoria_vault(),
        )

        # Compattazione a soglia, *fra un passo e l'altro*. E' qui che il
        # contesto esplode davvero: un compito da venti passi puo' saturare la
        # finestra senza che l'utente scriva una riga, e aspettare la fine del
        # turno vorrebbe dire aiutarlo dopo che e' morto.
        # La pressione si misura sulla **finestra efficace**: alla finestra
        # vera di oggi (131k) la soglia in percentuale non e' raggiungibile in
        # pratica -- vedi il commento a ``TETTO_TOKEN_DEFAULT``.
        finestra_compat = finestra_efficace(params.num_ctx, compact_max_tokens)
        if compact_history and context_pressure(api_messages, finestra_compat) > soglia:
            prima_tok = estimate_messages_tokens(api_messages)
            esito = compatta_cronologia(
                ui_messages,
                backend=backend,
                params=params,
                budgets=budgets,
                strip_thinking=strip_thinking,
                finestra=finestra_compat,
                schedario=schedario,
            )
            if esito is not None:
                count_nudge("compattazione")
                # L'indice si rilegge dal disco solo qui: e' l'unico momento in
                # cui puo' essere cambiato, e rileggerlo ad ogni passo sarebbe
                # un glob per niente.
                blocco_libreria = _blocco_libreria()
                api_messages = build_api_messages(
                    ui_messages,
                    system_prompt=system_prompt,
                    env_header=env_header,
                    strip_thinking=strip_thinking,
                    compact_old_tools=compact_old_tools,
                    images=images,
                    budgets=budgets,
                    plan_block=render_block(tool_ctx.plan, steps_left=max_steps - step),
                    delega_block=blocco_coda(max_steps - step) if blocco_coda else "",
                    preview_block=render_preview_note(tool_ctx.preview),
                    notes_block=render_notes(tool_ctx.notes),
                    skills_block=skills_block,
                    libreria_block=blocco_libreria,
                    vault_notes_block=blocco_memoria_vault(),
                )
                # I due numeri si misurano qui e non dentro la compattazione:
                # sono il contesto che il modello pagava davvero prima e quello
                # che paga adesso, prefisso e blocco di coda compresi. Dentro
                # si conoscono solo i messaggi, non la richiesta intera.
                yield HistoryCompacted(
                    messages=esito.messaggi_prima,
                    tokens_before=prima_tok,
                    tokens_after=estimate_messages_tokens(api_messages),
                    summary=esito.riassunto,
                )

        # Ultima spiaggia: se anche dopo il riassunto (o senza, perche' non e'
        # riuscito) il contesto sfonda, si buttano i turni piu' vecchi dalla
        # sola vista API. Perde informazione e va detto, ma e' pur sempre
        # meglio di una richiesta che il server rifiuta.
        #
        # Il margine e' fisso e si misura su ``num_ctx``: e' un problema diverso
        # dalla compattazione -- qui si tratta di non farsi rifiutare la
        # richiesta -- e non deve seguire una preferenza dell'utente.
        #
        # Ma deve restare l'ULTIMA spiaggia, e non lo era. Con una soglia utente
        # sopra 0,75 (il campo arriva a 0,95) e una finestra sotto i 32k, questo
        # scattava PRIMA della compattazione: chi alzava la soglia per tenersi
        # piu' cronologia se la vedeva buttare via senza che nessuno l'avesse
        # riassunta, cioe' l'esatto contrario di quello che aveva chiesto.
        margine_sfondamento = max(HISTORY_COMPACT_THRESHOLD, float(soglia))
        if context_pressure(api_messages, params.num_ctx) > margine_sfondamento:
            api_messages = drop_oldest_turns(
                api_messages, params.num_ctx, soglia=margine_sfondamento
            )

        # Il budget di servizio e' finito: si dice, invece di smettere in
        # silenzio. Da qui in poi i solleciti automatici non scattano piu' e
        # tutti i passi che restano vanno al lavoro.
        if passi_di_servizio >= max_passi_di_servizio and not servizio_esaurito:
            servizio_esaurito = True
            yield AgentError(
                f"Passi di servizio esauriti ({max_passi_di_servizio} di "
                f"{max_steps}): i solleciti automatici si spengono e i passi "
                f"restanti vanno tutti al lavoro."
            )

        parser = ThinkStreamParser()
        tool_calls: list[dict[str, Any]] = []
        stream_error: str | None = None
        interrupted = False
        watchdog_hit = False
        step_done_reason = ""
        # La soglia si ricalcola ad ogni passo: il primo di un turno ha diritto
        # a piu' pensiero degli altri, e su una finestra larga la sola quota del
        # budget non e' piu' un limite (vedi ``watchdog_chars_for_step``).
        watchdog_chars = (
            watchdog_chars_for_step(max_tokens_turno, step) if think_watchdog else 0
        )

        params_passo = params_for_step(params, tool_ctx.plan, step)
        # Il tetto si taglia **dopo** la compattazione e il drop dei turni
        # vecchi: prima di quelli il prompt non e' ancora quello che partira'.
        tetto_passo, spazio_finestra = tetto_per_la_finestra(
            api_messages, params.num_ctx, max_tokens_turno
        )
        if tetto_passo != max_tokens_turno:
            try:
                params_passo = replace(params_passo, max_tokens=tetto_passo)
            except TypeError:      # non e' una dataclass: si lascia stare
                pass
        # Una volta sola per turno: ripeterlo ad ogni passo sarebbe rumore, e
        # il primo passo che ci arriva e' gia' quello che spiega tutti i
        # successivi. Va detto perche' e' l'unica cosa che l'utente puo'
        # sistemare -- alzare -c sul server, abbassare max_tokens, o cominciare
        # una chat nuova -- e perche' senza, un turno che si ferma a meta'
        # sembra un capriccio del modello.
        if tetto_passo < TETTO_INUTILE and not avvisato_finestra:
            avvisato_finestra = True
            yield AgentError(
                f"Finestra quasi piena: restano {max(spazio_finestra, 0)} token "
                f"su {params.num_ctx}. Da qui in avanti la generazione verra' "
                "tagliata a meta' -- anche dentro una chiamata a un tool. "
                "Alza la finestra del server, abbassa max_tokens, o comincia "
                "una conversazione nuova."
            )
        # ...e sotto il tetto inutile il turno **finisce**, invece di generare
        # lo stesso. L'avviso qui sopra era informativo e il ciclo tirava
        # dritto: con ``tetto_passo`` a zero si chiedeva al modello di produrre
        # zero token, si otteneva una risposta vuota, e quella risposta vuota
        # faceva scattare i solleciti -- che aggiungono altri messaggi, cioe'
        # riducono ancora lo spazio. Un giro a vuoto che si stringe da solo.
        #
        # La soglia e' la stessa dell'avviso: sotto TETTO_INUTILE non ci sta un
        # pensiero, una risposta e una chiamata, quindi non c'e' niente da
        # tentare. Meglio chiudere dicendo perche'.
        if tetto_passo < TETTO_INUTILE:
            yield AgentError(
                f"Turno interrotto al passo {step}: nella finestra non resta "
                f"spazio per generare ({max(spazio_finestra, 0)} token liberi "
                f"su {params.num_ctx}, ne servono almeno {TETTO_INUTILE}). "
                "Il lavoro fatto finora e' salvo: comincia una conversazione "
                "nuova, o alza la finestra del modello."
            )
            yield fine("finestra_piena", step)
            return

        # Un rubinetto per passo: cosa e' gia' arrivato alla UI vale per questa
        # generazione e non per la prossima, che riparte da testo vuoto.
        rubinetto = Rubinetto()
        try:
            events: Iterator[StreamEvent] = backend.stream(
                api_messages, tools_schema, params_passo
            )
            for ev in events:
                if stopped():
                    # Chiudere il generatore fa cadere la connessione HTTP
                    # verso Ollama: senza, la generazione continuerebbe a
                    # occupare la GPU anche dopo che l'utente ha premuto stop.
                    interrupted = True
                    close = getattr(events, "close", None)
                    if callable(close):
                        close()
                    break
                if ev.kind == "reasoning":
                    if parser.feed_reasoning(ev.text):
                        yield from rubinetto.aggiorna(parser.reasoning, parser.answer)
                elif ev.kind == "content":
                    r_changed, a_changed = parser.feed(ev.text)
                    if r_changed or a_changed:
                        yield from rubinetto.aggiorna(parser.reasoning, parser.answer)
                elif ev.kind == "tool_call" and ev.tool_call:
                    tool_calls.append(ev.tool_call)
                elif ev.kind == "usage" and ev.usage:
                    step_done_reason = str(ev.usage.get("done_reason") or "")
                    for key, value in ev.usage.items():
                        if isinstance(value, (int, float)):
                            total_usage[key] = total_usage.get(key, 0) + value
                        else:
                            total_usage[key] = value
                elif ev.kind == "error":
                    stream_error = ev.text
                    break

                # Watchdog sul ragionamento. Sta qui e non dentro il ramo
                # "reasoning" perche' il pensiero puo' arrivare da due canali:
                # quello nativo di Ollama e i tag <think> nel testo, che il
                # parser riconosce lo stesso. Il controllo deve valere per
                # entrambi, o sui modelli senza thinking nativo non scatterebbe
                # mai -- proprio quelli che ne avrebbero piu' bisogno.
                if (
                    watchdog_chars
                    and not tool_calls
                    and watchdog_fires < MAX_WATCHDOG_FIRES
                    and step < max_steps
                    and len(parser.reasoning) > watchdog_chars
                ):
                    # Chiudere il generatore fa cadere la connessione verso
                    # Ollama: la GPU smette subito di produrre un ragionamento
                    # che sappiamo gia' non arrivera' da nessuna parte. E' lo
                    # stesso meccanismo del pulsante stop, puntato contro un
                    # modo di fallire invece che contro l'utente.
                    watchdog_hit = True
                    close = getattr(events, "close", None)
                    if callable(close):
                        close()
                    break
        except Exception as exc:  # noqa: BLE001
            stream_error = f"{type(exc).__name__}: {exc}"

        parser.finish()

        # La traccia di questo passo: cosa era configurato, cosa e' stato
        # davvero chiesto al modello, e quanto ha pensato. Vedi ``marca_pensiero``.
        traccia.clear()
        traccia.update(
            {
                "passo": step,
                "configurato": _nome_livello(getattr(params, "think", None)),
                "usato": _nome_livello(getattr(params_passo, "think", None)),
                "punto_aperto": bool(getattr(tool_ctx.plan, "current", None)),
                # **Quale** punto, non solo se ce n'era uno. Il booleano
                # sopra resta perche' e' quello che leggono gli script di
                # analisi gia' scritti; questo campo e' cio' che rende
                # possibile raccogliere, alla chiusura di un punto, il
                # pensiero dei passi che gli sono appartenuti (vedi
                # ``core/pensiero.py``). Senza, l'unico legame fra un
                # ragionamento e il lavoro che stava servendo non esiste.
                "punto": (
                    tool_ctx.plan.current.id
                    if getattr(tool_ctx.plan, "current", None)
                    else None
                ),
                "pensato": len(parser.reasoning),
                "risposto": len(parser.answer),
                "chiamate": len(tool_calls),
                "watchdog": bool(watchdog_hit),
                # Quanto spazio restava nella finestra prima di generare, e
                # con che tetto si e' partiti. Sono i due numeri che spiegano
                # una generazione tagliata: senza, in una sessione salvata non
                # resta traccia di quanto poco margine ci fosse.
                "spazio": int(spazio_finestra),
                "tetto": int(tetto_passo),
            }
        )
        stato_traccia["da"] = len(ui_messages)

        if interrupted or stopped():
            yield from halt(step, parser.reasoning, parser.answer)
            return

        if stream_error:
            # Una chiamata caduta non e' un turno perso. La cronologia e i file
            # gia' scritti sono intatti: quello che manca e' solo la risposta a
            # questo passo, e la si puo' richiedere. Vedi ``errore_riprovabile``
            # per il confine fra "riprova" e "e' inutile insistere".
            if (
                step < max_steps
                and riprese_stream < MAX_RIPRESE_STREAM
                and errore_riprovabile(stream_error)
                and passi_di_servizio < max_passi_di_servizio
            ):
                riprese_stream += 1
                count_nudge("ripresa_stream")
                passi_di_servizio += 1
                yield AgentError(
                    f"Chiamata al modello interrotta ({stream_error}) — riprendo "
                    f"da dove eravamo, tentativo {riprese_stream} di "
                    f"{MAX_RIPRESE_STREAM}."
                )
                # Una pausa breve, non per educazione: il caso piu' frequente e'
                # il modello che sta entrando in VRAM, e ripartire nello stesso
                # millisecondo trova la stessa porta chiusa.
                if PAUSA_RIPRESA_S:
                    time.sleep(PAUSA_RIPRESA_S)
                continue
            yield AgentError(stream_error)
            yield fine("error", step)
            return

        # Il testo completo, una volta per passo: e' la rete di sicurezza degli
        # incrementi. Chi si e' perso un frame (abbonato lento, riattacco a
        # meta') qui torna in pari, e chi non si e' perso niente riscrive lo
        # stesso testo che ha gia'.
        if parser.reasoning:
            yield ReasoningDelta(text=parser.reasoning)
        if parser.answer:
            yield ContentDelta(text=parser.answer)

        if watchdog_hit and passi_di_servizio < max_passi_di_servizio:
            watchdog_fires += 1
            count_nudge("think_watchdog")
            passi_di_servizio += 1
            spesi = estimate_tokens(parser.reasoning)
            # Il ragionamento interrotto **non** finisce in cronologia. Sono
            # migliaia di token di un pensiero a meta': rimetterli nel contesto
            # significherebbe pagarli ad ogni passo successivo per rileggere
            # proprio il giro di pensieri che stiamo cercando di spezzare. Al
            # modello resta il sollecito, che gli dice cosa fare adesso.
            ui_messages.append(
                {
                    "role": "user",
                    "content": THINK_WATCHDOG_NUDGE.format(tokens=spesi),
                    "hidden": True,
                }
            )
            yield AgentError(
                f"Ragionamento interrotto a ~{spesi} token senza azione: "
                "l'agente e' stato riportato sul piano."
            )
            continue

        answer, faked_tool_output = strip_tool_wrappers(parser.answer.strip())
        reasoning = parser.reasoning.strip()
        if faked_tool_output:
            # Il modello ha scritto un <tool_response> di sua invenzione: non
            # e' una risposta, e non deve impedire il riepilogo finale.
            yield AgentError(
                "Il modello ha inventato un risultato di tool "
                f"(<tool_response>): {faked_tool_output[:200]}"
            )

        # --- recupero delle tool call stampate come testo ------------------
        # Molti modelli locali "intendono" chiamare il tool ma emettono il JSON
        # nel canale testuale invece che con il function calling nativo. Qui lo
        # si esegue comunque, e il JSON esce dal flusso della chat.
        recovered = False
        if not tool_calls and answer:
            candidates, leftover = parse_text_tool_calls(answer)
            if candidates:
                tool_calls = candidates
                answer = leftover
                recovered = True

        # --- nessuna tool call: fine turno, salvo nudge --------------------
        if not tool_calls:
            # Generazione tagliata dal tetto di num_predict. Va guardato per
            # primo perche' cambia il significato di tutto il resto: senza,
            # "niente tool call e niente risposta" veniva letto come "ha
            # risposto a parole" e riceveva un sollecito che davanti a una
            # risposta vuota non vuol dire niente. Il done_reason arrivava gia'
            # da Ollama fin dal primo giorno: mancava solo qualcuno che lo
            # leggesse.
            if (
                step_done_reason == "length"
                and not answer
                and truncated_nudges < MAX_TRUNCATED_NUDGES
                and step < max_steps
                and passi_di_servizio < max_passi_di_servizio
            ):
                truncated_nudges += 1
                count_nudge("truncated")
                passi_di_servizio += 1
                ui_messages.append(
                    {
                        "role": "user",
                        "content": TRUNCATED_NUDGE.format(
                            tokens=int(getattr(params, "max_tokens", 0) or 0)
                        ),
                        "hidden": True,
                    }
                )
                yield AgentError(
                    "Il modello ha esaurito i token generabili mentre "
                    "ragionava, senza arrivare a un'azione."
                )
                continue

            # Ciclo di self-correction: se una verifica e' rossa il turno non
            # e' finito, per quanto il modello si sia convinto del contrario.
            red = verification.unresolved
            if (
                red
                and verify_nudges < MAX_VERIFY_NUDGES
                and step < max_steps
                and passi_di_servizio < max_passi_di_servizio
            ):
                command, attempts, code = red
                verify_nudges += 1
                count_nudge("loop" if attempts >= LOOP_THRESHOLD else "verify")
                passi_di_servizio += 1
                ui_messages.append(
                    {"role": "assistant", "content": _wrap(reasoning, answer), "ts": time.time()}
                )
                template = LOOP_NUDGE if attempts >= LOOP_THRESHOLD else VERIFY_NUDGE
                ui_messages.append(
                    {
                        "role": "user",
                        "content": template.format(
                            command=command, code=code, count=attempts
                        ),
                        "hidden": True,
                    }
                )
                yield AssistantTurn(
                    content=answer, reasoning=reasoning, has_tool_calls=False
                )
                continue

            # Verifica verde che non misura il codice nuovo. Il caso reale:
            # aggiunge `media_mobile_pesata_stream`, non le scrive un test,
            # lancia `pytest test_media_mobile.py` -- che esercita tutt'altra
            # funzione -- ottiene 10 passed e dichiara "lavoro completato". La
            # funzione lasciata sul disco era rotta su tutti e tre i casi di
            # riferimento. Il tracker delle verifiche non se ne accorge: per
            # lui il rosso non c'e', quindi va tutto bene.
            if (
                enable_nudge
                and not coverage_nudged
                and not red
                and step < max_steps
            ):
                scoperti = uncovered_symbols(tool_ctx)
                if scoperti and passi_di_servizio < max_passi_di_servizio:
                    coverage_nudged = True
                    count_nudge("coverage")
                    passi_di_servizio += 1
                    elenco = ", ".join(f"{n} (in {f})" for n, f in scoperti[:5])
                    ui_messages.append(
                        {
                            "role": "assistant",
                            "content": _wrap(reasoning, answer),
                            "ts": time.time(),
                        }
                    )
                    ui_messages.append(
                        {
                            "role": "user",
                            "content": COVERAGE_NUDGE.format(symbols=elenco),
                            "hidden": True,
                        }
                    )
                    yield AssistantTurn(
                        content=answer, reasoning=reasoning, has_tool_calls=False
                    )
                    continue

            # Il turno ha toccato il workspace ma finisce senza una parola:
            # l'utente resterebbe a guardare delle tendine chiuse senza sapere
            # cosa e' successo. Si chiede il riepilogo, una volta sola.
            # Il riepilogo e' l'unico sollecito **esente** dal budget di
            # servizio, e non e' un'eccezione di comodo: gli altri sette sono
            # tentativi di correzione -- riprova, ripensa, verifica -- e quando
            # il budget finisce e' giusto che smettano. Questo invece e' la
            # parola di chiusura del turno, ed e' cio' che impedisce a un turno
            # che ha lavorato di finire in silenzio davanti all'utente. Proprio
            # un turno che ha bruciato il budget in correzioni e' quello che
            # rischia di piu' di chiudersi senza dire com'e' andata.
            if (
                require_summary
                and not summary_requested
                and tools_used
                and not answer
                and step < max_steps
            ):
                summary_requested = True
                count_nudge("summary_failed" if red else "summary")
                # Il ragionamento di questo passo va conservato, come in tutti
                # gli altri rami: senza, un turno che si e' fermato a meta'
                # sparisce dalla cronologia e dal file di sessione, e capire
                # *perche'* si e' fermato diventa impossibile.
                if reasoning or answer:
                    ui_messages.append(
                        {
                            "role": "assistant",
                            "content": _wrap(reasoning, answer),
                            "ts": time.time(),
                        }
                    )
                if red:
                    testo = FAILED_SUMMARY_NUDGE
                elif tool_ctx.plan:
                    # Con un piano il riepilogo non si fa a memoria: c'e' gia'
                    # scritto cosa e' stato chiuso e cosa no, e un riassunto
                    # che contraddice il piano si vede subito.
                    testo = PLAN_SUMMARY_NUDGE.format(
                        summary=render_summary(tool_ctx.plan)
                    )
                else:
                    testo = SUMMARY_NUDGE
                ui_messages.append(
                    {"role": "user", "content": testo, "hidden": True}
                )
                continue

            # Due condizioni fanno scattare il sollecito automatico:
            #  a) il modello ha *annunciato* un'azione senza eseguirla;
            #  b) siamo al primo passo, l'utente ha chiesto un'operazione sul
            #     workspace e il modello ha risposto solo a parole.
            # La (b) e' la novita': prima toccava all'utente insistere a mano.
            needs_push = looks_like_unexecuted_action(answer) or (
                step == 1 and user_expects_tool_use(last_user_request(ui_messages))
            )
            # Chiedere non e' tergiversare: se il modello si e' fermato per un
            # chiarimento, il sollecito giusto non e' "esegui", e' "usa il tool
            # apposta cosi' la domanda arriva davvero all'utente".
            if needs_push and looks_like_clarifying_question(answer):
                needs_push = False
                if enable_nudge and not nudged and step < max_steps and passi_di_servizio < max_passi_di_servizio:
                    nudged = True
                    count_nudge("ask")
                    passi_di_servizio += 1
                    ui_messages.append(
                        {
                            "role": "assistant",
                            "content": _wrap(reasoning, answer),
                            "ts": time.time(),
                        }
                    )
                    ui_messages.append(
                        {"role": "user", "content": ASK_NUDGE, "hidden": True}
                    )
                    yield AssistantTurn(
                        content=answer, reasoning=reasoning, has_tool_calls=False
                    )
                    continue
            if (
                enable_nudge
                and not nudged
                and step < max_steps
                and needs_push
                and passi_di_servizio < max_passi_di_servizio
            ):
                nudged = True
                count_nudge("tool")
                passi_di_servizio += 1
                ui_messages.append(
                    {
                        "role": "assistant",
                        "content": _wrap(reasoning, answer),
                        "ts": time.time(),
                    }
                )
                ui_messages.append({"role": "user", "content": TOOL_NUDGE, "hidden": True})
                yield AssistantTurn(content=answer, reasoning=reasoning, has_tool_calls=False)
                continue

            ui_messages.append(
                {"role": "assistant", "content": _wrap(reasoning, answer), "ts": time.time()}
            )
            yield AssistantTurn(content=answer, reasoning=reasoning, has_tool_calls=False)
            yield fine("completed", step)
            return

        # --- esecuzione dei tool -------------------------------------------
        assistant_record: dict[str, Any] = {
            "role": "assistant",
            "content": _wrap(reasoning, answer),
            "tool_calls": [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {"name": call["name"], "arguments": call["arguments"]},
                }
                for call in tool_calls
            ],
            "recovered": recovered,
            "ts": time.time(),
        }
        ui_messages.append(assistant_record)
        yield AssistantTurn(
            content=answer,
            reasoning=reasoning,
            has_tool_calls=True,
            recovered=recovered,
        )

        # ...il sollecito viene accodato DOPO i risultati dei tool (vedi in
        # fondo al passo): infilato qui spezzerebbe l'adiacenza fra il
        # messaggio assistant con le tool_calls e i relativi risultati, che
        # diversi chat template si aspettano contigui.
        leak_nudge_due = recovered and not leak_nudged

        # ask_user_question sospende il turno, quindi va eseguito per ultimo:
        # cosi' tutti gli altri tool dello stesso passo hanno gia' il loro
        # risultato in cronologia e non restano tool_call_id scoperti.
        asks = [c for c in tool_calls if c["name"] == ASK_USER_TOOL]
        ordered = [c for c in tool_calls if c["name"] != ASK_USER_TOOL] + asks

        for call in ordered:
            if call["name"] == ASK_USER_TOOL:
                try:
                    raw_args = json.loads(call["arguments"] or "{}")
                except json.JSONDecodeError:
                    raw_args = {}
                question = normalise_question(raw_args if isinstance(raw_args, dict) else {})

                if not question["question"]:
                    ui_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "name": ASK_USER_TOOL,
                            "content": json.dumps(
                                {
                                    "error": "Parametro 'question' mancante.",
                                    "hint": "Riformula la domanda in una frase.",
                                },
                                ensure_ascii=False,
                            ),
                            "args": raw_args if isinstance(raw_args, dict) else {},
                            "duration_s": 0.0,
                            "ok": False,
                            "ts": time.time(),
                        }
                    )
                    continue

                # Il risultato di questo tool arriva dall'utente: lo si scrive
                # in cronologia solo alla ripresa (vedi resume_with_answer).
                ui_messages.append(
                    {
                        "role": "pending_question",
                        "tool_call_id": call["id"],
                        "name": ASK_USER_TOOL,
                        **question,
                        "ts": time.time(),
                    }
                )
                yield AwaitingUserInput(
                    call_id=call["id"],
                    question=question["question"],
                    options=question["options"],
                    allow_multiple=question["allow_multiple"],
                )
                yield fine("awaiting_user", step)
                return

            try:
                args = json.loads(call["arguments"] or "{}")
                if not isinstance(args, dict):
                    args = {}
            except json.JSONDecodeError:
                args = {}
                bad_args = True
            else:
                bad_args = False

            if stopped():
                # Il tool non parte, ma la sua tool_call e' gia' in cronologia:
                # va comunque chiusa con un risultato, altrimenti resta un
                # tool_call_id scoperto e il prossimo turno parte con una
                # cronologia che diversi chat template rifiutano.
                cancelled = json.dumps(
                    {"error": "Interrotto dall'utente prima dell'esecuzione."},
                    ensure_ascii=False,
                )
                ui_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "name": call["name"],
                        "content": cancelled,
                        "args": args,
                        "duration_s": 0.0,
                        "ok": False,
                        "ts": time.time(),
                    }
                )
                yield ToolFinished(
                    call_id=call["id"],
                    name=call["name"],
                    args=args,
                    result=cancelled,
                    duration_s=0.0,
                    ok=False,
                )
                continue

            yield ToolStarted(call_id=call["id"], name=call["name"], args=args)

            started = time.monotonic()
            if bad_args:
                result = json.dumps(
                    # ``params_passo`` e non ``params``: il tetto puo' essere
                    # stato abbassato per questo passo, ed e' quello vero che
                    # ha tagliato la generazione.
                    argomenti_illeggibili(
                        call["arguments"],
                        step_done_reason,
                        int(getattr(params_passo, "max_tokens", 0) or 0),
                    ),
                    ensure_ascii=False,
                )
            else:
                result = dispatch(tool_ctx, call["name"], args)
            duration = time.monotonic() - started

            ok = _esito_del_tool(result)
            # La ripetizione si registra **dopo** l'esecuzione e solo se e'
            # andata bene: rifare una chiamata che era fallita e' legittimo.
            if ok:
                if call["name"] in ("write_file", "edit_file"):
                    # Una scrittura invalida le letture: dopo, rileggere lo
                    # stesso file ha senso e non e' una ripetizione.
                    ripetizioni.dimentica_letture()
                else:
                    quante = ripetizioni.registra(call["name"], args)
                    if quante >= RipetizioniTool.SOGLIA and not ripetizione_nudged:
                        ripetizione_nudged = True
                        ripetizione_dovuta = (call["name"], quante)
            verification.record(call["name"], result)
            # Il guard sui file di test ha bisogno di sapere se c'e' una
            # verifica rossa aperta: qui e' l'unico punto che lo sa.
            red_now = verification.unresolved
            tool_ctx.red_command = red_now[0] if red_now else None
            if call["name"] == "run_command" and '"esito": "ok"' not in result[:200]:
                # Rossa sia per una verifica fallita sia per un comando che non
                # esiste: sono problemi diversi, ma entrambi l'utente li vuole
                # vedere senza aprire la tendina.
                ok = False
            tools_used = True
            ui_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "name": call["name"],
                    "content": result,
                    "args": args,
                    "duration_s": round(duration, 2),
                    "ok": ok,
                    "ts": time.time(),
                }
            )
            yield ToolFinished(
                call_id=call["id"],
                name=call["name"],
                args=args,
                result=result,
                duration_s=duration,
                ok=ok,
            )
            if call["name"] == PLAN_TOOL and ok:
                yield PlanUpdated(
                    steps=tool_ctx.plan.to_list(),
                    summary=render_summary(tool_ctx.plan),
                )
                # Il cancello: su un piano lungo l'utente lo legge prima che
                # parta il lavoro. Non e' una conferma di cortesia -- e' che
                # correggere un piano costa dieci secondi e valutare trenta
                # passi no, e su questo modello il piano e' la cosa che
                # riesce peggio. Scatta una volta sola: una ripianificazione a
                # meta' lavoro e' un aggiustamento, non una nuova partenza.
                if (
                    plan_gate
                    and not gate_mostrato
                    and str(args.get("action") or "").strip().lower() == "set"
                    and len(tool_ctx.plan.steps) >= PUNTI_PER_IL_CANCELLO
                ):
                    gate_mostrato = True
                    domanda = {
                        "question": (
                            f"Il piano ha {len(tool_ctx.plan.steps)} punti e il "
                            "lavoro sta per cominciare. Lo leggi prima?"
                        ),
                        "options": [
                            {
                                "label": "Procedi",
                                "description": "Il piano va bene: comincia dal primo punto.",
                            },
                            {
                                "label": "Correggi",
                                "description": (
                                    "Scrivi cosa cambiare e il piano viene "
                                    "riscritto prima che parta il lavoro."
                                ),
                            },
                        ],
                        "allow_multiple": False,
                    }
                    ui_messages.append(
                        {
                            "role": "pending_question",
                            "kind": GATE_PIANO,
                            "tool_call_id": "",
                            "name": ASK_USER_TOOL,
                            **domanda,
                            "ts": time.time(),
                        }
                    )
                    yield AwaitingUserInput(
                        call_id="",
                        question=domanda["question"],
                        options=domanda["options"],
                        allow_multiple=False,
                    )
                    yield fine("awaiting_user", step)
                    return
            if call["name"] == NOTES_TOOL and ok:
                yield NotesUpdated(notes=tool_ctx.notes.to_list())
            anteprima = preview_from_result(
                call["name"], args, result, ok, auto=auto_preview
            )
            if anteprima is not _NO_PREVIEW:
                # La cartella servita si sa solo qui, dove c'e' il workspace:
                # ``preview_from_result`` guarda una tool call e basta. Senza,
                # la nota al modello direbbe "la cartella del workspace" anche
                # per una pagina che sta in sito/ -- una frase falsa su cui poi
                # ragiona.
                if (
                    isinstance(anteprima, dict)
                    and anteprima.get("path")
                    and "root" not in anteprima
                ):
                    anteprima["root"] = preview_root(
                        tool_ctx.workspace, str(anteprima["path"])
                    )
                # Ricordarlo nel contesto del turno e' cio' che permette al
                # passo successivo di sapere che il pannello e' gia' aperto.
                tool_ctx.preview = anteprima
                yield PreviewUpdated(payload=anteprima)

        # --- estratto del pensiero dei punti appena chiusi -----------------
        # La casella postale che ``manage_plan`` riempie chiudendo un punto. Si
        # svuota **sempre**, anche quando non si estrae: un punto chiuso al
        # turno scorso e distillato al prossimo archivierebbe il ragionamento
        # sbagliato, e ``ToolContext`` sopravvive ai turni.
        chiusi = list(tool_ctx.punti_chiusi)
        tool_ctx.punti_chiusi.clear()
        if chiusi and estratto_pensiero and schedario is not None and not stopped():
            # La traccia di questo passo si attacca adesso invece che
            # all'inizio del prossimo: il pensiero del passo che ha chiuso il
            # punto appartiene a quel punto, ed e' spesso quello che contiene
            # la conclusione. Il ``marca_pensiero`` in cima al ciclo restera'
            # muto, perche' la traccia e' gia' stata consumata.
            marca_pensiero()
            for chiuso in chiusi:
                blocchi = pensiero.blocchi_del_punto(ui_messages, chiuso["id"])
                if not blocchi:
                    continue
                distillato = pensiero.estrai(
                    blocchi,
                    punto=chiuso["text"],
                    backend=backend,
                    params=params,
                )
                if not distillato:
                    continue
                voce = libreria.archivia_estratto(
                    schedario,
                    punto=chiuso["text"],
                    estratto=distillato,
                    saltato=bool(chiuso.get("saltato")),
                )
                if voce is not None:
                    # L'indice in coda cambia adesso, non al turno prossimo:
                    # il passo successivo deve poter vedere che quella voce
                    # esiste, altrimenti il precarico la ignora per un intero
                    # turno. L'estratto **non** torna nel risultato del tool:
                    # ``manage_plan`` e' stato snellito apposta perche' valeva
                    # il 13,5% dei token di risultato.
                    blocco_libreria = _blocco_libreria()

        if stopped():
            # Fermarsi qui, a tool conclusi e risultati in cronologia, e' il
            # punto piu' pulito: la conversazione resta valida e riprendibile.
            yield fine("stopped", step)
            return

        if leak_nudge_due:
            leak_nudged = True
            count_nudge("json_leak")
            ui_messages.append(
                {"role": "user", "content": JSON_LEAK_NUDGE, "hidden": True}
            )

        # La stessa chiamata per la terza volta. Non consuma un passo di
        # servizio -- non fa ripartire il passo, si accoda ai risultati che il
        # modello sta gia' per leggere -- ma va detta, perche' e' l'unico modo
        # di guasto agentico comune che nessuna delle altre difese vedeva.
        if ripetizione_dovuta is not None:
            nome_tool, quante = ripetizione_dovuta
            ripetizione_dovuta = None
            count_nudge("ripetizione")
            ui_messages.append(
                {
                    "role": "user",
                    "content": RIPETIZIONE_NUDGE.format(tool=nome_tool, quante=quante),
                    "hidden": True,
                }
            )

        # Il modello si e' messo a lavorare su una richiesta con piu' obiettivi
        # senza scrivere un piano. Il sollecito va **dopo** i risultati dei
        # tool, come tutti gli altri: infilato prima spezzerebbe l'adiacenza
        # fra l'assistant con le tool_calls e i suoi risultati. Le letture
        # iniziali restano quindi valide -- non si butta via lavoro utile, si
        # pretende solo che da qui in poi ci sia una traccia.
        if (
            require_plan
            and enable_nudge
            and multi_step
            and not plan_nudged
            and not tool_ctx.plan
            and step < max_steps
        ):
            plan_nudged = True
            count_nudge("plan")
            ui_messages.append(
                {"role": "user", "content": PLAN_NUDGE, "hidden": True}
            )

        # Esplorazione lunga senza mai delegare. Il tool `esplora` c'e' e
        # funziona: misurato il 23/08/2026 compare in 4 sessioni su 24, mentre
        # l'esplorazione fatta a mano vale il 46% dei token di risultato che
        # restano in contesto per sempre. Non manca lo strumento, manca che
        # qualcuno lo nomini nel momento in cui servirebbe.
        #
        # Si conta per passi interi e non per singole chiamate: un read_file
        # dentro un passo che scrive anche e' il ciclo normale leggi-modifica,
        # e sollecitare li' sarebbe rumore. Il segnale e' il passo che *solo*
        # guarda, ripetuto.
        nomi_del_passo = {c["name"] for c in tool_calls}
        if nomi_del_passo and nomi_del_passo <= set(TOOL_ESPLORATIVI):
            # Un passo, un punto -- come dice il commento qui sopra da sempre.
            # La riga sommava ``len(tool_calls)``: un passo solo con cinque
            # read_file in parallelo, che e' il comportamento *buono* di un
            # modello capace di chiamate multiple, arrivava a cinque al primo
            # passo e si prendeva il sollecito di delega senza aver esplorato
            # niente.
            esplorazioni_di_fila += 1
        else:
            esplorazioni_di_fila = 0
        if (
            enable_nudge
            and abilita_delega
            and not delega_nudged
            and esplorazioni_di_fila >= MAX_ESPLORAZIONI_SENZA_DELEGA
            and step < max_steps
        ):
            delega_nudged = True
            count_nudge("delega")
            ui_messages.append(
                {
                    "role": "user",
                    "content": DELEGA_NUDGE.format(quante=esplorazioni_di_fila)
                    + _esempio_delega(),
                    "hidden": True,
                }
            )

    # I passi sono finiti. Se il turno ha lavorato e non ha detto niente,
    # l'utente ha davanti delle tendine di tool e nient'altro: una chiamata
    # sola, senza tool, glielo racconta.
    #
    # Non e' un sollecito e non poteva esserlo: ``SUMMARY_NUDGE`` chiede al
    # modello di scrivere al passo **successivo**, e qui un passo successivo
    # non c'e' piu' -- ha in guardia ``step < max_steps`` esattamente per
    # questo. E' lo stesso baratto di ``delega.referto_di_chiusura``: una
    # generazione in piu' contro un turno intero buttato.
    marca_pensiero()
    if require_summary and tools_used and not ultima_risposta(ui_messages):
        count_nudge("riepilogo_forzato")
        testo = riepilogo_finale(
            ui_messages,
            backend=backend,
            params=params,
            budgets=budgets,
            strip_thinking=strip_thinking,
        )
        if testo:
            ui_messages.append(
                {
                    "role": "assistant",
                    "content": testo,
                    # Detto, perche' non e' una risposta come le altre: e' un
                    # referto chiesto dall'harness quando il turno era gia'
                    # finito. La UI puo' segnalarlo, e chi rilegge la sessione
                    # non deve credere che il modello si sia fermato da solo.
                    "forzato": True,
                    "ts": time.time(),
                }
            )
            yield AssistantTurn(content=testo, reasoning="", has_tool_calls=False)

    yield fine("max_steps", max_steps)


# Quante volte l'harness insiste perche' una verifica rossa venga riparata,
# prima di lasciar chiudere il turno con un rapporto onesto. Due spinte bastano:
# oltre, si consumerebbe l'intero budget di passi su un problema che il modello
# evidentemente non sa risolvere da solo.
MAX_VERIFY_NUDGES = 2
# I tool con cui si guarda e basta. Un passo fatto solo di questi e' una mossa
# di esplorazione, e l'esplorazione e' esattamente cio' che si puo' delegare.
TOOL_ESPLORATIVI = ("read_file", "search_files", "list_files")
# Quante letture di fila prima di ricordare che esiste `esplora`. Cinque perche'
# quattro sono ancora un giro di orientamento onesto: la delega conviene quando
# la domanda e' aperta, non quando si stanno controllando due file noti. Il
# sollecito scatta **una volta per turno** e si puo' ignorare.
MAX_ESPLORAZIONI_SENZA_DELEGA = 5
# Quota del budget di generazione oltre la quale un ragionamento senza nessuna
# tool call viene interrotto. Il 55% non e' un numero tondo per caso: sotto, si
# taglia un pensiero che stava per concludere; sopra, non resta abbastanza
# budget per emettere l'azione, che e' il fallimento che si vuole evitare.
# Errori della chiamata al modello che valgono una ripresa. Il confine non e'
# "grave / non grave": e' **la conversazione e' ancora valida?**. Un timeout, una
# connessione caduta, un 502 del reverse proxy davanti a Ollama lasciano intatti
# la cronologia, il piano e i file gia' scritti: manca solo la risposta di
# questo passo, e chiederla di nuovo costa un passo. Un 404 sul modello o un
# payload rifiutato invece si ripresenteranno identici, e riprovare vuol dire
# solo far aspettare l'utente prima di dirgli la stessa cosa.
#
# Perche' serve: un turno lungo puo' durare mezz'ora e aver gia' prodotto
# venti file. Buttarlo perche' una singola HTTP e' caduta significa perdere il
# lavoro **e** far ricominciare l'utente da capo, che e' il costo piu' alto
# possibile per l'errore piu' banale.
_ERRORI_RIPROVABILI = (
    re.compile(r"\btimeout\b", re.I),
    re.compile(r"errore di rete verso", re.I),
    re.compile(r"errore verso ollama", re.I),
    re.compile(r"\bHTTP 5\d\d\b"),
    re.compile(r"connection (reset|aborted|refused|error)", re.I),
    re.compile(r"(remote ?protocol|read|write|connect|pool) ?error", re.I),
    re.compile(r"(peer closed connection|server disconnected|incomplete (read|chunked))", re.I),
    re.compile(r"\btemporarily unavailable\b", re.I),
    re.compile(r"(apiconnectionerror|apitimeouterror|internalservererror|serviceunavailable|readtimeout|connecttimeout|writetimeout)", re.I),
)
# Due riprese e poi si dice com'e' andata. Oltre, si trasformerebbe un servizio
# spento in un turno che gira a vuoto consumando passi senza dirlo a nessuno.
MAX_RIPRESE_STREAM = 2
# Pausa fra una ripresa e l'altra. Piccola: e' un respiro, non un backoff.
PAUSA_RIPRESA_S = 1.0


def errore_riprovabile(testo: str) -> bool:
    """L'errore di stream lascia la conversazione utilizzabile?

    Si guarda il testo perche' e' quello che i backend producono: ``StreamEvent
    ("error", text=...)`` non porta un codice. Il criterio e' volutamente
    stretto -- si riprova solo su quello che si riconosce -- cosi' un errore
    nuovo o inatteso continua a chiudere il turno e a finire sotto gli occhi
    dell'utente invece di essere riprovato due volte in silenzio.
    """
    if not testo:
        return False
    return any(rx.search(testo) for rx in _ERRORI_RIPROVABILI)


WATCHDOG_RATIO = 0.55
# Tetti assoluti al pensiero di un singolo passo, in token. La sola quota del
# budget bastava finche' il budget era piccolo: con ``num_ctx`` a 16k il tetto
# di generazione e' 8.192 token e il 55% fa 4.500, che e' una soglia vera. Con
# la finestra a 98k e ``max_tokens`` a 32k lo stesso 55% diventa 17.600 token
# di solo pensiero prima che qualcuno intervenga -- non e' piu' un guard-rail,
# e' un permesso. La sessione del 19/08/2026 lo mostra: 60.000 caratteri di
# ``<think>`` in un passo, e il watchdog che scatta quando il danno e' fatto.
# Il primo passo del turno ha piu' spazio perche' e' l'unico che deve davvero
# decidere; dal secondo in poi si sta eseguendo, e per eseguire bastano poche
# centinaia di token.
THINK_CEILING_FIRST = 5000
THINK_CEILING_STEP = 2000
# Due interruzioni per turno. Alla terza il modello ha evidentemente bisogno di
# quel pensiero: lo si lascia finire e, se sfora, ci pensa il rilevamento del
# troncamento -- meglio un turno lento di un turno in cui l'harness e il modello
# si contendono il controllo all'infinito.
MAX_WATCHDOG_FIRES = 2
# Stessa logica per il troncamento: due tentativi di riportarlo all'azione.
MAX_TRUNCATED_NUDGES = 2
# Stesso comando fallito questo numero di volte = il modello sta girando a
# vuoto e va dirottato invece che spronato.
LOOP_THRESHOLD = 3
# Exit code che segnalano un comando sbagliato, non un progetto rotto.
SHELL_NOT_A_VERIFICATION = frozenset({126, 127})


class RipetizioniTool:
    """Chiamate identiche ripetute nello stesso turno.

    Il modo di guasto piu' comune di un agente non e' il comando che fallisce
    -- per quello c'e' ``VerificationTracker`` -- ma la chiamata che **riesce**
    e che il modello rifa' perche' non ha usato il risultato. Costa un passo e
    una seconda copia dello stesso contenuto in contesto, e nessuna delle altre
    difese la vede: il tracker guarda solo ``run_command``, e
    ``esplorazioni_di_fila`` conta le esplorazioni senza accorgersi che sono la
    stessa.
    """

    __slots__ = ("_viste",)

    # Alla terza, non alla seconda: rileggere un file dopo averlo modificato e'
    # legittimo, e sollecitare li' sarebbe rumore su un comportamento corretto.
    SOGLIA = 3

    def __init__(self) -> None:
        self._viste: dict[tuple[str, str], int] = {}

    def registra(self, name: str, args: dict[str, Any] | None) -> int:
        """Quante volte questa esatta chiamata e' gia' stata fatta nel turno."""
        # Gli argomenti si normalizzano ordinandoli: ``{"a":1,"b":2}`` e
        # ``{"b":2,"a":1}`` sono la stessa chiamata, e un modello che rigenera
        # il JSON non li mette sempre nello stesso ordine.
        try:
            firma = (name, json.dumps(args or {}, sort_keys=True, ensure_ascii=False))
        except (TypeError, ValueError):
            firma = (name, repr(args))
        self._viste[firma] = self._viste.get(firma, 0) + 1
        return self._viste[firma]

    def dimentica_letture(self) -> None:
        """Una scrittura invalida le letture: dopo, rileggere ha senso."""
        self._viste = {
            k: v for k, v in self._viste.items() if k[0] not in TOOL_ESPLORATIVI
        }


class VerificationTracker:
    """Tiene il conto delle verifiche rosse ancora aperte nel turno.

    Un ciclo di self-correction si riconosce da questo: dopo un ``run_command``
    con exit code diverso da zero, il turno non puo' considerarsi concluso
    finche' lo stesso comando non torna verde. Senza qualcuno che lo verifichi,
    un modello piccolo riassume e chiude come se avesse finito.
    """

    def __init__(self) -> None:
        # comando -> (numero di fallimenti consecutivi, ultimo exit code)
        self.failing: dict[str, tuple[int, int]] = {}

    def clear(self) -> None:
        """Dimentica tutte le verifiche rosse aperte.

        La chiama ``ToolContext.clear_red_command`` quando l'utente-modello
        chiude un punto del piano dichiarando la verifica non pertinente
        (``ignore_red``) o lo salta: da quel momento quel rosso non deve piu'
        far scattare i solleciti, o il turno resta appeso a un fallimento che
        e' gia' stato giudicato.
        """
        self.failing.clear()

    def record(self, name: str, result: str) -> None:
        if name != "run_command":
            return
        try:
            payload = json.loads(result)
        except json.JSONDecodeError:
            return
        command = str(payload.get("command") or "").strip()
        if not command:
            # Le buste d'errore (``{"error": ..., "hint": ...}``) non portano
            # il comando, ed e' cosi' che i guasti d'ambiente -- Docker spento,
            # binario fuori dal PATH, comando bloccato dal recinto -- restano
            # fuori dal tracker: per struttura del dato, non riconoscendo il
            # testo del messaggio. Il fork ci aveva aggiunto un
            # ``"SandboxError" in result`` qui sotto, che non e' mai potuto
            # scattare perche' questo ritorno viene prima.
            return
        if looks_like_server(command):
            # Un server non ha un "esito": non termina per progetto. Se compare
            # qui e' perche' il timeout l'ha ucciso, e trattarlo come verifica
            # rossa produce il ciclo osservato dal vivo: l'harness ordina di
            # "riseguire ESATTAMENTE lo stesso comando", il comando riparte,
            # viene ucciso di nuovo, e il turno gira a vuoto finche' l'utente
            # non interviene. Il messaggio giusto glielo da' gia' run_command,
            # e dice di usare il tool preview.
            self.failing.pop(command, None)
            return
        code = payload.get("returncode")
        if code == 0:
            self.failing.pop(command, None)          # riparato
        elif code in SHELL_NOT_A_VERIFICATION:
            # 127 = comando inesistente, 126 = trovato ma non eseguibile.
            # Non dicono niente sul codice del progetto: dicono che il comando
            # era sbagliato. Trattarli come verifiche rosse produceva un
            # sollecito assurdo ("rileggi lo stderr, correggi con edit_file")
            # su un comando che l'agente aveva gia' giustamente abbandonato per
            # una variante funzionante -- e siccome quella riga non veniva piu'
            # rieseguita, il rosso restava aperto per sempre.
            self.failing.pop(command, None)
        elif isinstance(code, int):
            previous = self.failing.get(command, (0, code))[0]
            self.failing[command] = (previous + 1, code)

    @property
    def unresolved(self) -> tuple[str, int, int] | None:
        """(comando, tentativi, exit code) della verifica rossa piu' insistente."""
        if not self.failing:
            return None
        command, (count, code) = max(self.failing.items(), key=lambda kv: kv[1][0])
        return (command, count, code)


def _esito_del_tool(result: str) -> bool:
    """Il tool e' andato bene? Leggendo la busta, non cercando una parola.

    Prima: ``ok = '"error"' not in result[:200]``. Un ``read_file`` su un
    sorgente che contiene la stringa ``"error"`` nei primi 200 caratteri -- un
    modulo di gestione errori, un JSON di configurazione, un test -- veniva
    marcato fallito: la tendina si colorava di rosso e il modello leggeva un
    esito che non corrispondeva a quello che era successo.

    I risultati dei tool sono tutti JSON con una busta nota (``_ok`` e ``_err``
    in ``tools.py``): l'errore e' una **chiave**, non una sottostringa.
    """
    try:
        payload = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        # Non e' JSON: e' il caso dei risultati gia' troncati o di un tool che
        # ritorna testo. Si ricade sul vecchio criterio, che li' e' l'unico
        # possibile -- ma solo li', non su tutto.
        return '"error"' not in result[:200]
    if not isinstance(payload, dict):
        return True
    return "error" not in payload


def _wrap(reasoning: str, answer: str) -> str:
    """Ricompone il messaggio salvato: pensiero fra tag + risposta.

    Il tag resta nel log della UI (serve a ridisegnare la tendina quando si
    ricarica una sessione) ma viene rimosso da ``build_api_messages`` prima di
    tornare al modello.
    """
    if reasoning and answer:
        return f"<think>{reasoning}</think>\n{answer}"
    if reasoning:
        return f"<think>{reasoning}</think>"
    return answer
