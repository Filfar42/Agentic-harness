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
import logging
from dataclasses import dataclass, field, replace
from pathlib import PurePath
from typing import Any
from collections.abc import Callable, Iterator, Sequence

from .backend import PREFISSO_NOTA_HARNESS, StreamEvent
from .cruscotto import Metriche, Tachimetro

from .context import compact_result, deposit_references
from .inference import service_text
from .telemetry import track_backend
from .jsonsafe import JsonBoundaryError, loads_object
from .config import (
    Budgets,
    HISTORY_COMPACT_THRESHOLD,
    budgets_for,
    finestra_efficace,
)
from .compaction import (
    CODA_DEFAULT,
    SOGLIA_DEFAULT,
    TETTO_TOKEN_DEFAULT,
    Compattazione,
    costruisci_riassunto,
    render_messaggio,
    richieste_utente,
    taglio,
    trascrizione,
)

from . import delega as delega_mod
from . import deposito as deposito_mod
from . import libreria
from . import pensiero
from . import regia_pensiero as regia
from . import spec_delega as spec_delega_mod
from . import vault as vault_mod
from . import vault_search as vault_search_mod
from . import verifiche as verifiche_mod
from .notes import render_block as render_notes
from .plan import render_block, render_summary
from .prompts import (
    DELEGA_ESEMPIO,
    PROMPT_RIEPILOGO_FINALE,
    PROMPT_RIEPILOGO_STALLO,
    render_promemoria_batch,
    render_ripresa,
    render_verifiche_aperte,
)
from .textutils import (
    ThinkStreamParser,
    chars_for_tokens,
    estimate_messages_tokens,
    estimate_tokens,
    strip_think,
    strip_tool_wrappers,
)
from .tools import (
    ASK_USER_TOOL,
    NOTES_TOOL,
    PLAN_TOOL,
    PREVIEW_TOOL,
    ToolContext,
    dispatch,
    looks_like_readonly_request,
    normalise_question,
    preview_kind,
    preview_root,
    reset_scratch,
    validate_tool_arguments,
)

# Il ciclo a stati (25/09/2026): stato del turno, reti di sicurezza, monitor di
# avanzamento, ripresa. I segnali testuali e i rilevatori di stallo stavano qui:
# si ri-esportano con gli stessi nomi, perche' test e script li importano da
# ``core.agent``.
from .ciclo import reti as reti_mod
from .ciclo import ripresa as ripresa_mod
from . import selezione as selezione_mod
from .ciclo.avanzamento import Chiamata, leggi_esito
from .ciclo.reti import (  # noqa: F401 - ri-esportati
    LOOP_THRESHOLD,
    MAX_ESPLORAZIONI_SENZA_DELEGA,
    MAX_TRUNCATED_NUDGES,
    MAX_VERIFY_NUDGES,
)
from .ciclo.segnali import (  # noqa: F401 - ri-esportati
    MAX_PAROLA_PERCORSO,
    MIN_ARTEFATTI,
    REQUIRED_ARGS,
    _as_tool_call,
    _PAROLA_PERCORSO,
    artefatti_nominati,
    extract_json_objects,
    is_streaming_tool_json,
    looks_like_clarifying_question,
    looks_like_raw_tool_json,
    looks_like_unexecuted_action,
    looks_multi_step,
    nomi_chiamate_nel_testo,
    parse_text_tool_call,
    parse_text_tool_calls,
    user_expects_tool_use,
)
from .ciclo.stato import (  # noqa: F401 - ri-esportati
    TOOL_ESPLORATIVI,
    RipetizioniTool,
    RitorniDeiFile,
    StatoTurno,
    _firma,
)

logger = logging.getLogger(__name__)
MAX_TOOL_CALLS_PER_STEP = 32

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
    telemetry: dict[str, Any] = field(default_factory=dict)
    # Stato delle verifiche alla fine del turno. ``reason`` dice **come si e'
    # fermato il turno**, non se il lavoro e' riuscito: "completed" significava
    # anche "ha esaurito i solleciti con due test rossi aperti", e da fuori le
    # due cose erano indistinguibili. Qui c'e' l'altra meta': cosa risulta
    # verificato, cosa e' rimasto rosso e cosa e' stato giustificato per
    # iscritto. Vedi ``core/verifiche.py``.
    qualita: dict[str, Any] = field(default_factory=dict)
    # L'istantanea del turno per il turno dopo (``StatoTurno.istantanea``):
    # perche' si e' fermato, le verifiche rimaste rosse, le chiamate fallite
    # piu' volte, i file scritti. Il server la salva nella sessione e la
    # ripassa a ``run_turn`` al messaggio successivo (``core/ciclo/ripresa.py``).
    checkpoint: dict[str, Any] = field(default_factory=dict)
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
    # Questo errore indica un backend da ricostruire?
    #
    # ``AgentError`` non e' un errore fatale: e' il canale con cui il ciclo
    # racconta all'utente cosa sta succedendo. Lo emettono il watchdog del
    # pensiero, la risposta troncata, la finestra quasi piena, il finto
    # ``<tool_response>`` -- tutte cose in cui il backend sta benissimo. Il
    # server buttava via l'istanza su **tutti**, e con essa le capability
    # rilevate e la memoria dei fallimenti di ``model_info``: un watchdog del
    # pensiero rimetteva in conto sei secondi di timeout al turno dopo.
    #
    # Il flag e' un dato e non una parola da cercare nel testo: chi emette
    # l'evento sa se il backend e' rotto, chi lo legge no.
    guasto_backend: bool = False


# Tutto cio' che ``run_turn`` puo' emettere, e serve che sia **tutto**: e' il
# contratto su cui il server scrive ``event_to_sse``, e un evento che non
# compare qui e' un evento che chi legge non sa di dover gestire.
# ``HistoryCompacted`` e ``NotesUpdated`` mancavano pur essendo emessi da anni.
AgentEvent = (
    StepStarted
    | ReasoningDelta
    | ContentDelta
    | AssistantTurn
    | ToolStarted
    | ToolFinished
    | AwaitingUserInput
    | HistoryCompacted
    | NotesUpdated
    | PlanUpdated
    | PreviewUpdated
    | TurnFinished
    | AgentError
    | Metriche
)


# ---------------------------------------------------------------------------
# Costruzione del contesto
# ---------------------------------------------------------------------------


def _compact_tool_result(
    raw: str, *, full: bool, budgets: Budgets | None = None
) -> str:
    """Riduce i corpi conservando esiti, JSON e riferimenti recuperabili."""
    return compact_result(raw, full=full, budgets=budgets or Budgets())


# Argomenti che contengono il *corpo* di qualcosa, non un riferimento. Sono
# l'unica parte di una tool call che vale la pena potare: filepath, command e
# pattern costano decine di token e servono a capire cosa e' successo, mentre
# `content` di un write_file puo' valerne diecimila e, a chiamata eseguita, e'
# una copia di un file che sta sul disco.
_ARGOMENTI_PESANTI = {
    "write_file": ("content",),
    "edit_file": ("old_string", "new_string"),
    # Gli script inline (``python3 - <<'EOF' ... EOF``): nei log del 25/09 il
    # 34,9% degli argomenti inviati era ``run_command.command``, la voce piu'
    # grossa dopo i corpi dei file -- e la potatura non la toccava. Si tiene la
    # prima riga, che dice *cosa* e' stato lanciato; il resto e' gia' eseguito
    # e il suo esito sta nel risultato.
    "run_command": ("command",),
}
# Sotto questa lunghezza un corpo non si pota: il risparmio non ripaga la
# riscrittura del messaggio, che fa divergere il prefisso.
MIN_CORPO_DA_POTARE = 400


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
        if not isinstance(corpo, str) or len(corpo) < MIN_CORPO_DA_POTARE:
            # Sotto la soglia il risparmio non ripaga la riscrittura del
            # messaggio, che fa divergere il prefisso e costa un prompt eval.
            continue
        righe = corpo.count("\n") + 1
        if nome == "run_command":
            prima = corpo.split("\n", 1)[0][:160]
            args[chiave] = (
                f"{prima} <omesso: script di {righe} righe, {len(corpo)} caratteri, "
                "gia' eseguito: l'esito e' nel risultato di questa chiamata.>"
            )
        else:
            args[chiave] = (
                f"<omesso: {righe} righe, {len(corpo)} caratteri di una modifica "
                "eseguita. read_file restituisce la versione attuale del file, "
                "che puo' differire da questo contenuto storico.>"
            )
        potato = True

    if not potato:
        return call
    return {
        **call,
        "function": {**fn, "arguments": json.dumps(args, ensure_ascii=False)},
    }


def _costo_argomenti_pesanti(call: dict[str, Any]) -> int:
    """Token dei corpi che ``_prune_tool_call`` toglierebbe da questa chiamata."""
    fn = call.get("function") or {}
    chiavi = _ARGOMENTI_PESANTI.get(str(fn.get("name") or ""))
    if not chiavi:
        return 0
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except (json.JSONDecodeError, TypeError):
        return 0
    if not isinstance(args, dict):
        return 0
    return sum(
        estimate_tokens(corpo)
        for corpo in (args.get(k) for k in chiavi)
        if isinstance(corpo, str) and len(corpo) >= MIN_CORPO_DA_POTARE
    )


def risultati_integrali(
    ui_messages: Sequence[dict[str, Any]],
    tool_positions: Sequence[int],
    budgets: Budgets,
) -> set[int]:
    """Quali risultati di tool restano integrali, con isteresi.

    La finestra "ultimi N integrali" scorreva di un risultato a ogni passo:
    a ogni passo il risultato che ne usciva veniva compattato, cioe' si
    riscriveva un messaggio a N risultati dalla fine, e il server doveva
    ricalcolare da li' in poi -- anche gli N risultati che aveva gia' in cache.
    Misurato con ``scripts/sonda_prefisso.py`` su una sessione sintetica da 40
    passi con le proporzioni dei tool del 23/08/2026: 245.000 token ricalcolati
    su 490.000 inviati, mediana 5.700 per passo, senza che nulla fosse cambiato
    in quei messaggi.

    Qui il confine fra compattati e integrali si sposta **a scatti**: resta
    fermo finche' la zona integrale sta nella quota di token
    (``budgets.tool_result_full_tokens``, la stessa da cui ``budgets_for``
    ricava N), e quando la sfora salta in un colpo solo agli ultimi N. Il
    prefisso diverge una volta per scatto invece che a ogni passo.

    L'invariante di ``budgets_for`` regge: al momento dell'invio la zona
    integrale o sta nella quota, o contiene esattamente N risultati -- che per
    costruzione ci stanno. Nel costo entrano anche i corpi di ``write_file`` /
    ``edit_file`` che la potatura toglierebbe dal messaggio assistant: seguono
    la stessa finestra, e contarli zero terrebbe integrali dieci scritture da
    diecimila caratteri perche' i loro risultati sono ``{"status": "ok"}``.

    Pura funzione della cronologia: ricostruita a ogni passo dal primo
    risultato, da' lo stesso confine che aveva dato al passo prima.
    """
    return zona_integrale(ui_messages, tool_positions, budgets)[0]


def zona_integrale(
    ui_messages: Sequence[dict[str, Any]],
    tool_positions: Sequence[int],
    budgets: Budgets,
) -> tuple[set[int], int]:
    """``risultati_integrali`` piu' la posizione dell'ultimo scatto.

    Lo scatto e' l'indice (in ``ui_messages``) del risultato che ha fatto
    saltare il confine l'ultima volta, -1 se non e' mai saltato. Tutto cio' che
    si decide "allo scatto" -- le copie superate, A7b -- si decide guardando
    la cronologia fino a li': fra uno scatto e l'altro la vista non cambia.
    """
    finestra = max(1, int(budgets.tool_result_full_window))
    quota = int(getattr(budgets, "tool_result_full_tokens", 0) or 0)
    if quota <= 0:
        zona = tool_positions[-finestra:]
        scatto = zona[0] if len(tool_positions) > finestra and zona else -1
        return set(zona), scatto
    pesanti: dict[str, int] = {}
    for msg in ui_messages:
        if msg.get("role") == "assistant":
            for call in msg.get("tool_calls") or []:
                costo = _costo_argomenti_pesanti(call)
                if costo:
                    pesanti[str(call.get("id") or "")] = costo
    confine = 0
    zona = 0
    scatto = -1
    costi: list[int] = []
    for j, pos in enumerate(tool_positions):
        msg = ui_messages[pos]
        costo = estimate_tokens(
            _compact_tool_result(str(msg.get("content", "")), full=True, budgets=budgets)
        ) + pesanti.get(str(msg.get("tool_call_id") or ""), 0)
        costi.append(costo)
        zona += costo
        if j - confine + 1 > finestra and zona > quota:
            nuovo = j - finestra + 1
            zona -= sum(costi[confine:nuovo])
            confine = nuovo
            scatto = pos
    return set(tool_positions[confine:]), scatto


def copie_superate(
    ui_messages: Sequence[dict[str, Any]],
    zona: set[int],
    fino_a: int,
) -> set[int]:
    """Le letture integrali rese vecchie da un evento successivo, fino allo scatto (A7b).

    Una ``read_file`` nella zona integrale e' superata se, dopo di lei e non
    oltre ``fino_a``, lo stesso file e' stato riletto per intero o scritto con
    successo: il suo contenuto non e' piu' quello del disco, e tenerlo
    integrale costa token e confonde (nel replay delle 54 sessioni erano il
    5% dei token inviati). Solo fino allo scatto, e non a ogni passo: compattare
    un messaggio a meta' cronologia fa ricalcolare il prefisso da li', e allo
    scatto il prefisso diverge gia' al confine -- piu' indietro -- quindi la
    compattazione e' gratis. Fra uno scatto e l'altro la vista resta ferma.
    """
    if fino_a < 0 or not zona:
        return set()
    esiti = {str(m.get("tool_call_id") or ""): (i, m) for i, m in enumerate(ui_messages)
             if m.get("role") == "tool"}
    letture: list[tuple[int, str, bool]] = []      # (pos esito, file, intera)
    eventi: list[tuple[int, str, str, bool]] = []  # (pos esito, file, tool, intera)
    for m in ui_messages:
        if m.get("role") != "assistant":
            continue
        for call in m.get("tool_calls") or []:
            fn = call.get("function") or {}
            nome = str(fn.get("name") or "")
            if nome not in ("read_file", "write_file", "edit_file"):
                continue
            trovato = esiti.get(str(call.get("id") or ""))
            if trovato is None:
                continue
            pos, esito = trovato
            if esito.get("ok") is False or not _esito_del_tool(str(esito.get("content") or "")):
                continue
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(args, dict) or not args.get("filepath"):
                continue
            fp = str(args["filepath"]).replace("\\", "/").lstrip("./")
            intera = not (args.get("start_line") or args.get("end_line"))
            eventi.append((pos, fp, nome, intera))
            if nome == "read_file" and pos in zona:
                letture.append((pos, fp, intera))
    superate: set[int] = set()
    for pos, fp, intera in letture:
        for pos_e, fp_e, nome_e, intera_e in eventi:
            if not (pos < pos_e <= fino_a) or fp_e != fp:
                continue
            # Una lettura parziale dopo non supera una lettura intera prima.
            if nome_e != "read_file" or intera_e or not intera:
                superate.add(pos)
                break
    return superate


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
    verifiche_block: str = "",
    avanzamento_block: str = "",
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
    # Compattazione selettiva (``core/selezione.py``): le decisioni fissate
    # nello scatto valgono per id di chiamata. "elimina" toglie la coppia
    # chiamata+risultato dalla vista (mai uno senza l'altro); "tronca" accorcia
    # il risultato anche se sarebbe ancora nella finestra integrale.
    decisioni_sel = selezione_mod.decisioni_attive(ui_messages)
    tool_positions = [
        i for i, m in enumerate(ui_messages) if m.get("role") == "tool"
        and decisioni_sel.get(str(m.get("tool_call_id") or "")) != "elimina"
    ]
    if compact_old_tools:
        recent_tools, scatto = zona_integrale(ui_messages, tool_positions, budgets)
        # A7b: le copie superate escono dalla zona integrale nello stesso scatto.
        recent_tools -= copie_superate(ui_messages, recent_tools, scatto)
    else:
        recent_tools = set(tool_positions)

    # Gli id delle chiamate ancora "recenti". La potatura degli argomenti segue
    # la stessa finestra dei risultati, e non e' una comodita': se si potasse
    # l'argomento di una chiamata il cui risultato e' ancora integrale, il
    # modello si troverebbe davanti l'esito completo di un'azione di cui non
    # vede piu' la richiesta -- il modo migliore per fargli rifare il lavoro.
    recent_call_ids = {
        str(ui_messages[i].get("tool_call_id") or "") for i in recent_tools
    }
    successful_call_ids = {
        str(message.get("tool_call_id") or "")
        for message in ui_messages
        if message.get("role") == "tool" and message.get("ok") is not False
        and _esito_del_tool(str(message.get("content") or ""))
        # Uno script finito con exit != 0 non e' "eseguito e chiuso": e' la
        # cosa da correggere, e il modello deve poterlo rileggere intero.
        and not (message.get("name") == "run_command"
                 and '"esito": "FALLITO"' in str(message.get("content") or ""))
    }
    invalid_json_ids: set[str] = set()
    for message in ui_messages:
        if message.get("role") == "tool" and message.get("ok") is False:
            try:
                failure = loads_object(message.get("content", ""))
            except JsonBoundaryError:
                continue
            if failure.get("error_code") == "invalid_json":
                invalid_json_ids.add(str(message.get("tool_call_id") or ""))

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
            chiamate_vive = [
                call for call in (msg.get("tool_calls") or [])
                if decisioni_sel.get(str(call.get("id") or "")) != "elimina"
            ]
            if chiamate_vive:
                entry["tool_calls"] = [
                    call
                    if not compact_old_tools
                    or str(call.get("id") or "") in recent_call_ids
                    or str(call.get("id") or "") not in successful_call_ids
                    else _prune_tool_call(call)
                    for call in chiamate_vive
                ]
                # Ollama requires decoded objects even in historical calls.
                # An invalid attempt gets an inert placeholder only in this
                # API view; its exact input and failure remain in the tool result
                # and original UI log. It has never been executed.
                entry["tool_calls"] = [
                    {**call, "function": {**call["function"], "arguments": "{}"}}
                    if str(call.get("id") or "") in invalid_json_ids else call
                    for call in entry["tool_calls"]
                ]
            # Un assistant senza contenuto ne' tool call non aggiunge nulla.
            if entry["content"] or entry.get("tool_calls"):
                api.append(entry)

        elif role == "tool":
            decisione = decisioni_sel.get(str(msg.get("tool_call_id") or ""))
            if decisione == "elimina":
                continue
            if decisione == "tronca":
                api.append({
                    "role": "tool",
                    "tool_call_id": msg.get("tool_call_id", ""),
                    "name": msg.get("name", ""),
                    "content": selezione_mod.testo_troncato(_compact_tool_result(
                        str(msg.get("content", "")), full=False, budgets=budgets,
                    )),
                })
                continue
            api.append(
                {
                    "role": "tool",
                    "tool_call_id": msg.get("tool_call_id", ""),
                    "name": msg.get("name", ""),
                    # Una risposta umana e' un requisito, anche quando viaggia
                    # come risultato di ask_user_question. Non la riduciamo
                    # insieme ai log dei tool quando diventa meno recente.
                    "content": str(msg.get("content", ""))
                    if msg.get("answered") and msg.get("name") == ASK_USER_TOOL
                    else _compact_tool_result(
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

    # Il modello vede gia' il testo prodotto nei passi precedenti, ma in una
    # cronologia agentica e' separato dall'ultima posizione da risultati di
    # tool e solleciti nascosti. Qwen tende allora a trattarlo come materiale
    # da riassumere di nuovo. Questa nota non duplica il contenuto: rende
    # esplicito, nel punto piu' recente del prompt, che quelle risposte sono un
    # registro di comunicazione e non una bozza da parafrasare.
    gia_comunicato = False
    for msg in reversed(ui_messages):
        if msg.get("role") == "user" and not msg.get("hidden"):
            break
        if msg.get("role") == "assistant" and strip_think(
            str(msg.get("content") or "")
        ).strip():
            gia_comunicato = True
            break

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
            # Le verifiche rosse subito prima del piano: e' il cammino critico,
            # e in coda -- l'ultima cosa letta -- non si perde in mezzo alla
            # cronologia ne' dentro un riassunto (vedi ``render_verifiche_aperte``).
            verifiche_block,
            plan_block,
            # Il promemoria di batch dopo il piano: e' un suggerimento sulla
            # forma della prossima mossa, non sullo stato del lavoro.
            avanzamento_block,
            # Ultimo perche' e' l'unico che il figlio della delega ha: per lui
            # e' quello che il piano e' per il padre, cioe' l'ultima cosa che
            # legge prima di muoversi. Sul padre e' sempre vuoto.
            delega_block,
            (
                "<comunicazione_turno>\n"
                "Hai gia' mostrato testo all'utente in questo turno. Le "
                "risposte assistant precedenti sono il registro di cio' che "
                "e' gia' stato detto: non ripeterle e non parafrasarle. Se il "
                "lavoro continua, preferisci la sola tool call; se chiudi, "
                "aggiungi soltanto risultati, verifiche o residui nuovi.\n"
                "</comunicazione_turno>"
                if gia_comunicato else ""
            ),
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


# Nota di misura (30/08/2026), per chi fosse tentato di metterci una cache.
#
# Un passo agentico esegue tre stime complete della richiesta: la pressione per
# la compattazione, quella per lo sfondamento, e il tetto di generazione. Su una
# cronologia da 241 messaggi con un prompt da 24.000 caratteri, ``build_api_
# messages`` costa **2,4 ms** e una stima dei token **0,3 ms**: circa 7 ms per
# passo, contro secondi di generazione. Memoizzarle vorrebbe dire una cache da
# invalidare a ogni messaggio aggiunto -- cioe' un difetto silenzioso in
# cambio di niente.
def context_pressure(
    api_messages: Sequence[dict], num_ctx: int, *, reserved_tokens: int = 0,
) -> float:
    if num_ctx <= 0:
        return 0.0
    return (estimate_messages_tokens(api_messages) + max(0, reserved_tokens)) / num_ctx


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
    should_stop: Callable[[], bool] | None = None,
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
    from .compaction import limita_stato_attivo, riassunto_strutturato, stato_attivo

    if should_stop is not None and should_stop():
        return None
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
    # La UI conserva gli eventi originali. Il riassuntore vede soltanto il
    # tratto successivo al checkpoint, non tutta la cronologia dall'origine.
    inizio_attivo = ultimo_riassunto + 1
    cut_delta = taglio(
        ui_messages[inizio_attivo:],
        costo=costo,
        budget_coda=int(num_ctx * soglia_coda),
    )
    if not cut_delta:
        return None
    cut = inizio_attivo + cut_delta
    vecchi = ui_messages[inizio_attivo:cut]
    prima = costo(vecchi)
    # La specifica originale resta testuale anche al secondo checkpoint.
    richieste = richieste_utente(ui_messages[:cut])
    da_riassumere = list(vecchi)
    stato_precedente = ""
    fonte_precedente = ""
    if ultimo_riassunto >= 0:
        precedente = ui_messages[ultimo_riassunto]
        testo_precedente = str(precedente.get("content") or "")
        stato_precedente = stato_attivo(str(precedente.get("active_state", testo_precedente) or ""))
        percorso = precedente.get("archive_path")
        archiviato = False
        if schedario and isinstance(percorso, str):
            # Stesso criterio del recupero: un file dietro symlink/junction o
            # fuori dalla libreria non rende il checkpoint recuperabile.
            archiviato = any(v.percorso == percorso.replace("\\", "/")
                             for v in libreria.voci(schedario))
            if archiviato:
                fonte_precedente = percorso
        # Vecchie sessioni o archiviazione fallita: non perdere il solo
        # riassunto disponibile. Il fallback riassume quello, mai i raw vecchi.
        if not archiviato or not riassunto_strutturato(testo_precedente):
            da_riassumere.insert(0, precedente)
    trascritto = trascrizione(da_riassumere)
    if stato_precedente:
        trascritto = (
            "[STATO ATTIVO PRECEDENTE: confronta con i nuovi eventi]\n"
            + limita_stato_attivo(stato_precedente, fonte_precedente)
            + "\n\n[NUOVI EVENTI]\n" + trascritto
        )
    riassunto = costruisci_riassunto(
        trascritto,
        backend=backend,
        params=params,
        should_stop=should_stop,
    )
    if not riassunto or (should_stop is not None and should_stop()):
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
    stato_completo = stato_attivo(stato_precedente, riassunto)
    archivio = riassunto
    if stato_completo:
        archivio += "\n\n<stato_attivo>\n" + stato_completo + "\n</stato_attivo>"
    if fonte_precedente:
        # Catena recuperabile anche quando uno stato eccedente il budget e'
        # stato abbreviato nel checkpoint precedente.
        archivio += f"\n[Stato precedente: read_file {fonte_precedente}]"
    voce = libreria.archivia(
        schedario, riassunto=archivio, richieste=richieste_utente(vecchi)
    ) if schedario else None
    stato = limita_stato_attivo(stato_completo, voce.percorso if voce else "")
    if stato != stato_completo and voce is None:
        # Senza un archivio vero non si puo' promettere di recuperare le
        # verifiche tagliate: conserviamo la vista precedente.
        return None

    testo = render_messaggio(riassunto, richieste, voce=voce, stato=stato)
    ui_messages.insert(
        cut,
        {
            "role": "summary",
            "content": testo,
            "replaced": cut - (ultimo_riassunto + 1),
            "source_start": inizio_attivo,
            "source_end": cut,
            "archive_path": voce.percorso if voce is not None else None,
            "active_state": stato,
            "ts": time.time(),
        },
    )
    return Compattazione(
        messaggi_prima=cut_delta,
        messaggi_dopo=1,
        token_prima=prima,
        token_dopo=estimate_tokens(testo),
        riassunto=riassunto,
        richieste=richieste,
    )


def proponi_selezione(
    ui_messages: list[dict[str, Any]],
    *,
    budgets: Budgets,
    strip_thinking: bool,
    finestra: int,
    valutatore: Any = None,
    soglia_coda: float = CODA_DEFAULT,
    piano: str = "",
    pressione: Callable[[list[dict[str, Any]]], float] | None = None,
    max_caratteri_stato: int | None = None,
) -> tuple[int, selezione_mod.Selezione] | None:
    """Prepara una compattazione selettiva sullo stesso tratto del riassunto.

    Il tratto e' quello che ``compatta_cronologia`` riassumerebbe (stesso
    ``taglio``, stessa coda tenuta intera): le due strade si confrontano sullo
    stesso materiale. Non tocca ``ui_messages``: torna il punto d'inserimento e
    la selezione, e decide il chiamante se basta (``QUOTA_OBIETTIVO``).

    ``pressione`` (la pressione della vista costruita su una cronologia data)
    serve a non disturbare il valutatore quando e' inutile: prima si prova il
    tetto -- tutte le chiamate candidate tolte, il massimo che qualunque
    valutatore potrebbe fare -- e se nemmeno quello scende sotto la quota si
    torna None subito. Nel replay delle 54 sessioni era il caso di meta' delle
    compattazioni a 32k: domande a Laya che non avrebbero evitato niente.
    """
    if finestra <= 0:
        return None

    def costo(pezzo: Sequence[dict[str, Any]]) -> int:
        return estimate_messages_tokens(
            build_api_messages(
                pezzo, system_prompt="", env_header=None, strip_thinking=strip_thinking,
                compact_old_tools=False, budgets=budgets,
            )
        )

    ultimo_riassunto = max(
        (i for i, m in enumerate(ui_messages) if m.get("role") == "summary"), default=-1,
    )
    inizio = ultimo_riassunto + 1
    delta = taglio(ui_messages[inizio:], costo=costo, budget_coda=int(finestra * soglia_coda))
    if not delta:
        return None
    fine = inizio + delta
    if pressione is not None:
        tetto = selezione_mod.record_tutto_via(ui_messages, inizio, fine)
        if not tetto["decisioni"]:
            return None
        prova = list(ui_messages)
        prova.insert(fine, tetto)
        if pressione(prova) > selezione_mod.QUOTA_OBIETTIVO:
            return None
    # fast-jev usa come obiettivo le ultime tre richieste dell'utente.
    obiettivo = "\n".join(richieste_utente(ui_messages)[-3:])
    sel = selezione_mod.seleziona(
        ui_messages, inizio, fine, obiettivo=obiettivo, piano=piano, valutatore=valutatore,
        max_caratteri_stato=max_caratteri_stato or selezione_mod.MAX_CARATTERI_STATO,
    )
    if not sel.record["decisioni"]:
        return None
    sel.record["descrizione"] = selezione_mod.descrivi(sel)
    return fine, sel


# Di quanto scendere sotto la soglia quando si buttano turni: senza margine si
# tornava esattamente alla parete, il passo dopo la superava di nuovo e si
# buttava un altro turno. Ogni scarto sposta l'inizio della cronologia, cioe'
# il server ricalcola tutto il prompt: a finestra piena, a ogni passo.
MARGINE_SCARTO = 0.10


def drop_oldest_turns(
    api_messages: list[dict],
    num_ctx: int,
    soglia: float = HISTORY_COMPACT_THRESHOLD,
    *,
    reserved_tokens: int = 0,
    fattore: float = 1.0,
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

    # L'ancora: il primo messaggio della cronologia visibile al modello, se e'
    # dell'utente. E' la richiesta che ha aperto la conversazione o il
    # riassunto che la porta con se' (``render_messaggio`` ci scrive le
    # richieste dell'utente). Buttarla lasciava il modello a lavorare su una
    # coda di tool senza sapere piu' per chi: la prima cosa che usciva dalla
    # finestra era proprio il compito.
    ancora = body[:1] if body and body[0].get("role") == "user" else []
    resto = body[len(ancora):]

    scala = max(1.0, fattore)
    costi = [estimate_messages_tokens([m]) * scala for m in resto]
    totale = (
        (estimate_messages_tokens(head) + estimate_messages_tokens(ancora)
         + max(0, reserved_tokens)) * scala
        + sum(costi)
    )
    tetto = num_ctx * soglia
    obiettivo = num_ctx * max(0.0, soglia - MARGINE_SCARTO)
    i = 0
    if totale > tetto:
        while i < len(resto) and totale > obiettivo and len(resto) - i > 2:
            totale -= costi[i]
            i += 1
            # I risultati seguono la chiamata che li ha chiesti: un ``tool``
            # senza l'``assistant`` che lo precede e' un messaggio che il
            # backend rifiuta.
            while i < len(resto) and resto[i].get("role") == "tool":
                totale -= costi[i]
                i += 1
    if not i:
        return head + body

    # La nota sta dopo l'ancora e con ruolo 'user', non in testa come
    # 'system': ``messaggi_per_il_filo`` fonde i system iniziali nel prompt di
    # sistema, e una frase in piu' li' cambia il prefisso dal primo messaggio.
    nota = {
        "role": "user",
        "content": (
            f"{PREFISSO_NOTA_HARNESS} i turni piu' vecchi di questa conversazione "
            "sono stati rimossi dal contesto per rientrare nella finestra. Se ti "
            "serve un'informazione precedente, rileggila dal disco con read_file."
        ),
    }
    return head + ancora + [nota] + resto[i:]



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


# Estremi del fattore con cui la stima dei token si corregge sulla misura del
# server (vedi ``calibra_stima``). Sotto 1 non si scende: sovrastimare costa un
# tetto di generazione un po' piu' basso, sottostimare costa la chiamata
# tagliata a meta'. Sopra 2 la misura e' piu' probabilmente sbagliata della
# stima (un prompt con immagini, un server che conta altro).
FATTORE_STIMA_MIN = 1.0
FATTORE_STIMA_MAX = 2.0


def calibra_stima(prompt_reale: Any, stimati: int) -> float | None:
    """Rapporto fra i token del prompt contati dal server e quelli stimati.

    La stima e' una regola sui caratteri (3,6 per token). Su JSON con molti
    escape, codice indentato o testo con accenti il tokenizer di Qwen ne fa di
    piu': a 60.000 token stimati un errore del 15% sono 9.000 token, contro un
    ``MARGINE_FINESTRA`` di 768. E' la stessa specie di guasto della chiamata
    tagliata del 29/08: il tetto di generazione promette spazio che non c'e'.

    Il server il numero vero lo dice gia', in ``usage.prompt_tokens``, a ogni
    passo. Qui lo si usa per correggere i passi successivi dello stesso turno.
    ``None`` quando la misura non e' utilizzabile.
    """
    if type(prompt_reale) not in (int, float) or prompt_reale <= 0 or stimati <= 0:
        return None
    return max(FATTORE_STIMA_MIN, min(FATTORE_STIMA_MAX, float(prompt_reale) / float(stimati)))


def tetto_per_la_finestra(
    api_messages: Sequence[dict[str, Any]], num_ctx: int, max_tokens: int,
    *, reserved_tokens: int = 0, fattore: float = 1.0,
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
    # ``fattore`` e' la correzione misurata dal server (``calibra_stima``):
    # 1.0 finche' non c'e' una misura, cioe' il comportamento di sempre.
    stimati = (estimate_messages_tokens(api_messages) + reserved_tokens) * max(1.0, fattore)
    spazio = int(num_ctx - stimati - MARGINE_FINESTRA)
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


def think_for_step(configured: Any, _plan: Any, step: int, tipo: str | None = None) -> Any:
    """Livello di pensiero da usare in questo passo.

    L'idea: **pensare a lungo serve a decidere, non a eseguire**. Il primo
    passo di una fase resta al livello configurato: e' li' che si legge la
    richiesta o il nuovo punto del piano. I passi successivi hanno gia' la
    decisione e i risultati dei tool; ripagarla per intero e' tempo di GPU
    speso per riottenere la stessa risposta. Vale anche senza piano: un lavoro
    semplice non deve restare per sempre al livello massimo solo perche' non
    aveva bisogno di manage_plan.

    Si scende di uno dal secondo passo della fase e di **due** da
    ``DEEP_STEP`` in poi. Il pavimento resta 'low' e non si azzera mai: su un modello che ragiona
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
    # Il tipo del punto decide di quanto scendere (``regia.scalini_livello``):
    # su un punto di diagnosi non si scende al secondo passo come su una
    # modifica gia' decisa. Senza tipo il comportamento e' quello di prima.
    scalini = regia.scalini_livello(tipo, step, DEEP_STEP)
    if not scalini:
        return configured
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
    should_stop: Callable[[], bool] | None = None,
    prompt: str = PROMPT_RIEPILOGO_FINALE,
) -> str:
    """Chiede al modello cosa e' successo, quando i passi sono gia' finiti.

    Una chiamata sola, senza tool e senza pensiero: qui non c'e' niente da
    decidere, c'e' da raccontare. Un riepilogo mancato non deve peggiorare le
    cose -- si torna stringa vuota e il turno si chiude come prima.
    """
    api = build_api_messages(
        ui_messages,
        system_prompt=prompt,
        env_header=None,
        strip_thinking=strip_thinking,
        compact_old_tools=True,
        budgets=budgets,
    )
    if len(api) < 2:
        return ""
    # Il tetto passa da ``tetto_per_la_finestra`` come i passi normali.
    #
    # Questa chiamata arriva a passi esauriti, cioe' nel momento in cui la
    # cronologia e' al suo massimo: chiedere ``max_tokens`` pieni sulla finestra
    # piu' piena del turno e' esattamente il caso per cui quella funzione
    # esiste. Senza, il server tagliava il riepilogo dove capitava -- e un
    # riepilogo tagliato a meta' frase e' peggio di nessun riepilogo, perche'
    # sembra completo.
    tetto, _spazio = tetto_per_la_finestra(
        api,
        int(getattr(params, "num_ctx", 0) or 0),
        min(int(getattr(params, "max_tokens", 2048) or 2048), MAX_TOKEN_RIEPILOGO),
    )
    if tetto < TETTO_INUTILE:
        # Non c'e' spazio nemmeno per un riepilogo: meglio niente che una
        # frase mozzata.
        return ""
    p = replace(params, think=False, temperature=0.2, max_tokens=tetto)
    return service_text(backend, api, p, should_stop=should_stop)


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


def _livello_inviato(backend: Any, params: Any) -> str:
    """Il livello che il template ha ricevuto davvero, in forma leggibile.

    Diverso da "usato" quando il backend l'ha tradotto sull'elenco del
    template (``high`` -> ``xhigh`` su Qwen3.8). "default" vuol dire che il
    payload non portava nessun livello e ha deciso il template. Un backend
    senza traduzione manda quello che riceve.
    """
    leggi = getattr(backend, "livello_inviato", None)
    if not callable(leggi):
        return _nome_livello(getattr(params, "think", None))
    try:
        valore = leggi(params)
    except Exception:  # diagnostica: non deve fermare il turno
        logger.exception("livello_inviato fallito")
        return "?"
    return "default" if valore is None else _nome_livello(valore)


def registra_errore(ui_messages: list[dict[str, Any]], messaggio: str) -> None:
    """Scrive nella cronologia l'errore che ha chiuso il turno.

    Senza, l'errore viveva solo nello stream: a fine turno la pagina rilegge
    la conversazione dal disco (evento ``turn`` del bus) e il riquadro rosso
    spariva un istante dopo essere comparso -- con dentro l'unica riga utile,
    l'eccezione del chat template. Il record ``error`` non raggiunge mai il
    modello: ``build_api_messages`` conosce solo user/summary/assistant/tool.
    """
    testo = str(messaggio or "").strip()
    if testo:
        ui_messages.append({"role": "error", "content": testo, "ts": time.time()})


def params_for_step(params: Any, plan: Any, step: int, tipo: str | None = None) -> Any:
    """I parametri di generazione di questo passo, col pensiero modulato.

    Ritorna l'oggetto originale quando non c'e' niente da cambiare: cosi' i
    backend e i test che confrontano l'identita' dei parametri continuano a
    vedere esattamente quello che hanno passato.
    """
    configurato = getattr(params, "think", None)
    livello = think_for_step(configurato, plan, step, tipo)
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
    allow_text_tool_calls: bool = False,
    initialize_workspace: bool = True,
    # Il monitor di avanzamento (``core/ciclo/avanzamento.py``): misura se i
    # passi producono qualcosa di verificabile e, se no, riorienta e poi chiude.
    monitor_avanzamento: bool = True,
    # L'istantanea con cui si e' chiuso il turno precedente (``TurnFinished.
    # checkpoint``, salvata dal server nella sessione). Serve alla ripresa dopo
    # un turno rimasto a meta': vedi ``core/ciclo/ripresa.py``.
    checkpoint_precedente: dict[str, Any] | None = None,
    # Compattazione selettiva (``core/selezione.py``), tentata prima del
    # riassunto: "spenta" | "regole" | "valutatore". Con "valutatore" serve
    # ``valutatore_selezione`` (client /v1/systemone: Laya locale o Jev).
    selezione: str = "spenta",
    valutatore_selezione: Any = None,
) -> Iterator[AgentEvent]:
    """Esegue un turno completo. Muta ``ui_messages`` in-place via append.

    ``ui_messages`` e' l'unico stato: i messaggi che vi finiscono sono
    esattamente quelli che la UI ridisegna e che vengono salvati su disco.

    ``should_stop`` viene interrogato nei tre punti in cui l'interruzione e'
    sicura: prima di chiamare il modello, mentre arrivano i token, e prima di
    eseguire ogni tool. Non si taglia mai *dentro* un tool gia' partito: un
    write_file interrotto a meta' lascerebbe un file monco sul disco.

    Il ciclo e' una macchina a stati con le fasi che il 22/09 erano solo
    implicite -- **prepara** (contesto, compattazione, finestra), **genera**
    (stream, watchdog, continuazione), **valuta** (le reti di sicurezza in
    ``core/ciclo/reti.py``), **esegui** (i tool), **dopo i tool** (note,
    monitor di avanzamento) -- e lo stato del turno sta in un oggetto solo
    (``core/ciclo/stato.StatoTurno``). Vedi il referto del 25/09/2026.
    """
    turno = _Turno(
        backend=backend, params=params, tools_schema=tools_schema, tool_ctx=tool_ctx,
        ui_messages=ui_messages, system_prompt=system_prompt, env_header=env_header,
        max_steps=max_steps, strip_thinking=strip_thinking, compact_old_tools=compact_old_tools,
        budgets=budgets, enable_nudge=enable_nudge, require_summary=require_summary,
        require_plan=require_plan, think_watchdog=think_watchdog, auto_preview=auto_preview,
        compact_history=compact_history, soglia=soglia, compact_max_tokens=compact_max_tokens,
        libreria_attiva=libreria_attiva, estratto_pensiero=estratto_pensiero,
        blocco_coda=blocco_coda, spec_delega=spec_delega, plan_gate=plan_gate,
        skills_block=skills_block, abilita_delega=abilita_delega, should_stop=should_stop,
        images=images, allow_text_tool_calls=allow_text_tool_calls,
        initialize_workspace=initialize_workspace, monitor_avanzamento=monitor_avanzamento,
        checkpoint_precedente=checkpoint_precedente, selezione=selezione,
        valutatore_selezione=valutatore_selezione,
    )
    yield from turno.esegui()


# Le transizioni fra le fasi di un passo. ``AVANTI`` va alla fase successiva
# dello stesso passo; ``PROSSIMO`` chiude il passo e ne comincia un altro;
# ``FINE`` chiude il turno (il ``TurnFinished`` e' gia' stato emesso).
AVANTI = "avanti"
PROSSIMO = "prossimo"
FINE = "fine"


@dataclass
class _Preparazione:
    """Cio' che la fase PREPARA consegna a GENERA."""

    api_messages: list[dict[str, Any]]
    params_passo: Any
    tetto_passo: int
    spazio_finestra: int
    stima_passo: int
    watchdog_chars: int
    soglia_interrompi: int
    puo_continuare: bool
    regia_passo: Any
    reasoning_phase: str


@dataclass
class _Generazione:
    """Cio' che la fase GENERA consegna a VALUTA."""

    parser: Any
    tool_calls: list[dict[str, Any]]
    stream_error: str | None = None
    interrupted: bool = False
    watchdog_hit: bool = False
    done_reason: str = ""
    usage_passo: dict[str, Any] = field(default_factory=dict)
    chiusura_passo: str | None = None
    continuazione_rifiutata: bool = False
    osservatore: Any = None
    params_passo: Any = None
    # Riempiti da VALUTA: la risposta ripulita e se le chiamate vengono dal testo.
    answer: str = ""
    reasoning: str = ""
    recovered: bool = False


class _Turno:
    """Un'esecuzione di ``run_turn``: dipendenze, stato e fasi.

    Una classe e non un generatore unico per una ragione sola: ogni fase e'
    una funzione con un nome, e le cose che le fasi si passano sono campi di un
    oggetto invece di variabili che attraversano mille righe. Le decisioni --
    quale rete si applica, se il turno sta avanzando -- stanno fuori, in
    ``core/ciclo/``, come funzioni pure.
    """

    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)
        backend = track_backend(kw["backend"])
        self.backend = backend
        self.telemetry_offset = backend.collector.offset()
        self.turn_started = time.monotonic()
        # Lo stesso istante sull'orologio di parete: viaggia nelle metriche del
        # cruscotto per chi si riattacca a turno in corso.
        self.turn_started_epoch = time.time()
        self.allowed_tools = {schema["function"]["name"] for schema in self.tools_schema}
        self.schema_tokens = (
            estimate_tokens(json.dumps(self.tools_schema, ensure_ascii=False))
            if self.tools_schema else 0
        )
        # Tetto COMPLESSIVO ai passi che le reti di sicurezza possono consumare.
        #
        # Ogni sollecito ha gia' il suo contatore, e ognuno preso da solo e'
        # tarato bene. Ma i tetti si sommano, e nel caso peggiore -- un modello
        # debole su un compito difficile, cioe' esattamente quello per cui i
        # solleciti esistono -- al lavoro restava un passo. Qui i solleciti si
        # contendono un budget unico; quando finisce, il turno smette di
        # correggersi e usa i passi che restano per lavorare.
        self.st = StatoTurno(
            max_passi=self.max_steps,
            max_passi_di_servizio=max(2, self.max_steps // 3),
        )
        self.total_usage: dict[str, Any] = {}
        self.stopped = self.should_stop or (lambda: False)
        tool_ctx = self.tool_ctx
        ui_messages = self.ui_messages

        # I budget di troncamento seguono la finestra reale del modello, non le
        # costanti tarate su 16k. Vanno messi nel ToolContext *prima* del primo
        # passo: sono i tool a doverli rispettare, e li leggono da li'.
        if self.budgets is None:
            self.budgets = budgets_for(
                int(getattr(self.params, "num_ctx", 0) or 0), self.compact_max_tokens
            )
        tool_ctx.budgets = self.budgets
        self._monta_servizi()

        # Il cancello si apre una volta per turno: dopo che l'utente ha visto
        # il piano, ogni action='set' successiva e' una revisione fatta
        # lavorando.
        self.st.gate_mostrato = any(m.get("kind") == GATE_PIANO for m in ui_messages)

        # --- libreria dei concetti ------------------------------------------
        self.schedario = (
            tool_ctx.base
            if (self.libreria_attiva and getattr(tool_ctx, "base", None)) else None
        )
        self.blocco_libreria = self._blocco_libreria()

        # La richiesta ha piu' obiettivi: allora il piano non e' un suggerimento.
        self.st.multi_step = looks_multi_step(last_user_request(ui_messages))
        self.max_tokens_turno = int(getattr(self.params, "max_tokens", 0) or 0)
        # Una richiesta di sola lettura vale per il turno intero. Se pero'
        # l'utente ha appena risposto a un ask_user_question, ha parlato lui
        # dopo: il vincolo decade, altrimenti l'unica uscita di sicurezza
        # sarebbe murata.
        tool_ctx.readonly_request = looks_like_readonly_request(
            last_user_request(ui_messages)
        ) and not answered_question_pending(ui_messages)
        tool_ctx.new_symbols.clear()
        if self.initialize_workspace and not answered_question_pending(ui_messages):
            # Il banco di prova riparte vuoto ad ogni turno (non solo davanti a
            # una richiesta di analisi): il prompt dice che `.analisi/` "viene
            # svuotata", e una frase del prompt deve essere vera. Non si svuota
            # quando l'utente ha appena risposto a una domanda: quel turno e'
            # la continuazione del precedente.
            reset_scratch(tool_ctx)
        # La potatura del deposito sta qui e non a ogni scrittura. Il deposito
        # **non** si svuota come il banco di prova: un handle imbucato in un
        # turno deve restare valido per tutta la conversazione.
        if (
            self.initialize_workspace and getattr(tool_ctx, "deposito_attivo", False)
            and getattr(tool_ctx, "base", None)
        ):
            deposito_mod.pota(tool_ctx.base, int(getattr(tool_ctx, "deposito_max_mb", 0)),
                              protetti=deposit_references(ui_messages))
        self.verification = VerificationTracker()
        tool_ctx.verification = self.verification

        # --- chiamate orfane e ripresa (``core/ciclo/ripresa.py``) ---------
        # Una chiamata senza risultato in cronologia rompe i template severi e
        # lascia il modello senza sapere se l'effetto c'e' stato. Il server le
        # ripara gia' prima di chiamarci (e riscrive la coda); questo e' per chi
        # chiama ``run_turn`` direttamente.
        self.orfani_riparati = ripresa_mod.ripara_orfani(
            ui_messages, str(getattr(tool_ctx, "workspace", "") or "")
        )
        self.ripreso = False
        if ripresa_mod.va_ripreso(self.checkpoint_precedente, last_user_request(ui_messages)):
            self.ripreso = True
            ripresa_mod.ripristina_verifiche(self.verification, self.checkpoint_precedente)
            red_now = self.verification.unresolved
            tool_ctx.red_command = red_now[0] if red_now else None
            aperti = [f"{p.id}. {p.text}" for p in (tool_ctx.plan.open_steps if tool_ctx.plan else [])]
            ui_messages.append({
                "role": "user", "hidden": True, "kind": "ripresa",
                "content": render_ripresa(self.checkpoint_precedente, aperti),
                "ts": time.time(),
            })

        # --- traccia del pensiero -------------------------------------------
        # Sta sul messaggio dell'assistente e non in un file di log a parte:
        # e' gia' persistita nella sessione, gia' isolata nei test, e si legge
        # accanto al pensiero che descrive. Al modello non arriva mai.
        self.traccia: dict[str, Any] = {}
        self.stato_traccia = {"da": -1}
        # --- regia del pensiero (``core/regia_pensiero.py``) ------------------
        self.stato_punto = regia.StatoPunto()
        self.chiamate_passo: list[tuple[str, bool]] = []
        self.nomi_tool_schema = [
            str((t.get("function") or {}).get("name") or "")
            for t in (self.tools_schema or [])
            if isinstance(t, dict)
        ]

    # ------------------------------------------------------------------
    # Servizi montati sul ToolContext (delega, ricerca nel vault)
    # ------------------------------------------------------------------
    def _monta_servizi(self) -> None:
        tool_ctx = self.tool_ctx
        backend = self.backend
        # La delega si monta qui e non nel ToolContext perche' backend e
        # parametri li conosce il ciclo, non i tool. Il figlio non riceve il
        # tool di delega: un esploratore che delega e' una ricorsione che
        # nessuno ha chiesto.
        if self.abilita_delega and (
            tool_ctx.on_delega is None or getattr(tool_ctx.on_delega, "_harness_owned", False)
        ):
            def _delega(compito: str) -> dict[str, Any]:
                return delega_mod.esegui(
                    compito,
                    backend=backend.scope("delegate"),
                    params=self.params,
                    tools_schema=self.tools_schema,
                    tool_ctx=tool_ctx,
                    env_header=self.env_header,
                    run_turn=run_turn,
                    registra_esiti=self.spec_delega,
                    build_messages=build_api_messages,
                    should_stop=self.should_stop,
                )

            _delega._harness_owned = True
            tool_ctx.on_delega = _delega

        # vault_search si monta come la delega. Il cercatore non riceve ne'
        # delega ne' vault_search, quindi non c'e' ricorsione possibile.
        if tool_ctx.on_vault_search is None or getattr(
            tool_ctx.on_vault_search, "_harness_owned", False
        ):
            def _vault_search(vault: str, query: str) -> dict[str, Any]:
                return vault_search_mod.cerca_nel_vault(
                    vault,
                    query,
                    backend=backend.scope("vault_search"),
                    params=self.params,
                    tools_schema=self.tools_schema,
                    tool_ctx=tool_ctx,
                    env_header=self.env_header,
                    run_turn=run_turn,
                    registri=getattr(tool_ctx, "registri_vault", None),
                    should_stop=self.should_stop,
                )

            _vault_search._harness_owned = True
            tool_ctx.on_vault_search = _vault_search

    # ------------------------------------------------------------------
    # Blocchi di coda
    # ------------------------------------------------------------------
    def _blocco_libreria(self) -> str:
        if self.schedario is None:
            return ""
        elenco = libreria.voci(self.schedario)
        if not elenco:
            return ""
        indice = libreria.render_block(elenco)
        aperto = self.tool_ctx.plan.current if self.tool_ctx.plan else None
        contesto = (aperto.text if aperto else "") or last_user_request(self.ui_messages)
        ripescato = libreria.precarico(self.schedario, elenco, contesto)
        return "\n\n".join(p for p in (ripescato, indice) if p)

    def _esempio_delega(self) -> str:
        """L'ultima delega che in questo workspace ha funzionato al primo colpo."""
        if not self.spec_delega or not getattr(self.tool_ctx, "base", None):
            return ""
        domanda = spec_delega_mod.esempio_riuscito(self.tool_ctx.base)
        return DELEGA_ESEMPIO.format(domanda=domanda) if domanda else ""

    def _blocco_stato_vault(self) -> str:
        """Indice e coda del log della wiki, per i vault in modalita' wiki.

        Il **workspace**, non ``vault_dir``: la modalita' wiki si accende anche
        sui vault nati prima di ``.vault.json``, riconosciuti dalla struttura.
        """
        cartella = getattr(self.tool_ctx, "workspace", "") or ""
        if not cartella or not vault_mod.is_modalita_vault(cartella):
            return ""
        return vault_mod.blocco_stato(cartella)

    def _blocco_memoria_vault(self) -> str:
        tool_ctx = self.tool_ctx
        if not tool_ctx.vault_dir or not tool_ctx.vault_notes:
            return ""
        return vault_mod.blocco_note(
            vault_mod.VaultConfig(
                nome=PurePath(tool_ctx.vault_dir).name,
                note=tuple(tool_ctx.vault_notes),
            )
        )

    def _costruisci(self, step: int, blocco_piano: str,
                    messaggi: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        tool_ctx = self.tool_ctx
        return build_api_messages(
            self.ui_messages if messaggi is None else messaggi,
            system_prompt=self.system_prompt,
            env_header=self.env_header,
            strip_thinking=self.strip_thinking,
            compact_old_tools=self.compact_old_tools,
            images=self.images,
            budgets=self.budgets,
            # ``max_steps - step`` e non ``- step + 1``: e' quanti passi
            # restano *dopo* questo, cioe' quelli su cui puo' contare.
            plan_block=blocco_piano,
            delega_block=self.blocco_coda(self.max_steps - step) if self.blocco_coda else "",
            preview_block=render_preview_note(tool_ctx.preview),
            notes_block=render_notes(tool_ctx.notes),
            skills_block=self.skills_block,
            libreria_block=self.blocco_libreria,
            vault_state_block=self._blocco_stato_vault(),
            vault_notes_block=self._blocco_memoria_vault(),
            verifiche_block=render_verifiche_aperte(self.verification.pendenti),
            avanzamento_block=(
                render_promemoria_batch(self.st.letture_singole_di_fila)
                if self.st.letture_singole_di_fila >= PASSI_PER_IL_PROMEMORIA_BATCH else ""
            ),
        )

    # ------------------------------------------------------------------
    # Telemetria, chiusura, conteggi
    # ------------------------------------------------------------------
    def conta(self, nome: str) -> None:
        """Un sollecito in piu'. Lo stesso dizionario arriva in TurnFinished."""
        self.st.conta(nome)
        self.total_usage["nudges"] = self.st.solleciti

    def marca_pensiero(self) -> None:
        """Attacca la traccia al messaggio dell'assistente che l'ha prodotta.

        Si cerca all'indietro e solo fra i messaggi nati **dopo** la fine dello
        stream: un passo che non ha prodotto nessun messaggio (errore, stop)
        non deve marcare quello del passo precedente con numeri non suoi.
        """
        if not self.traccia:
            return
        ui_messages = self.ui_messages
        for i in range(len(ui_messages) - 1, self.stato_traccia["da"] - 1, -1):
            if ui_messages[i].get("role") == "assistant":
                ui_messages[i].setdefault("think", dict(self.traccia))
                break
        self.traccia.clear()

    def fine(self, reason: str, passi: int) -> TurnFinished:
        """Chiude il turno. Passa da qui per non perdere la traccia dell'ultimo
        passo, che e' proprio quello dei turni che finiscono male."""
        self.marca_pensiero()
        backend = self.backend
        telemetry = backend.collector.snapshot(since=self.telemetry_offset)
        telemetry["turn_wall_ms"] = round((time.monotonic() - self.turn_started) * 1000, 3)
        totals = telemetry["totals"]
        usage = dict(self.total_usage)
        for key in ("prompt_eval_ms", "eval_ms", "draft_n", "draft_accepted",
                    "cached_tokens", "prompt_processed_tokens"):
            if totals.get(key) is not None:
                usage[key] = totals[key]
        for source, target in (("usage_input_tokens", "prompt_tokens"),
                               ("usage_output_tokens", "completion_tokens"),
                               ("usage_total_tokens", "total_tokens")):
            if totals.get(source) is not None:
                usage[target] = totals[source]
        usage["model_wall_ms"] = totals["wall_time_ms"]
        usage["total_ms"] = telemetry["turn_wall_ms"]
        return TurnFinished(
            reason=reason, steps=passi, usage=usage, telemetry=telemetry,
            qualita=self.verification.riepilogo(),
            checkpoint=self.st.istantanea(motivo=reason, passi=passi, verifiche=self.verification),
        )

    def halt(self, step: int, reasoning: str = "", answer: str = "") -> Iterator[AgentEvent]:
        """Chiude il turno salvando quel che il modello aveva gia' prodotto."""
        text = _wrap(reasoning.strip(), answer.strip())
        if text.strip():
            self.ui_messages.append(
                {"role": "assistant", "content": text, "stopped": True, "ts": time.time()}
            )
            yield AssistantTurn(
                content=answer.strip(), reasoning=reasoning.strip(), has_tool_calls=False
            )
        yield self.fine("stopped", step)

    # ------------------------------------------------------------------
    # Il ciclo
    # ------------------------------------------------------------------
    def esegui(self) -> Iterator[AgentEvent]:
        for step in range(1, self.max_steps + 1):
            if self.stopped():
                yield from self.halt(step - 1)
                return
            # I tool sanno a che passo siamo: serve alla cache delle riletture.
            self.tool_ctx.step = step
            # La traccia del passo precedente si attacca adesso: il suo
            # messaggio e' stato appeso da uno qualsiasi dei rami che chiudono
            # un passo, e farlo qui e' l'unico punto che li copre tutti.
            self.marca_pensiero()
            yield StepStarted(step=step, total=self.max_steps)

            prep = yield from self._prepara(step)
            if prep is None:
                return
            gen = yield from self._genera(step, prep)
            transizione = yield from self._valuta(step, gen)
            if transizione == FINE:
                return
            if transizione == PROSSIMO:
                continue
            transizione = yield from self._esegui_tool(step, gen)
            if transizione == FINE:
                return
            transizione = yield from self._dopo_tool(step, gen)
            if transizione == FINE:
                return

        yield from self._passi_finiti()

    # ------------------------------------------------------------------
    # PREPARA
    # ------------------------------------------------------------------
    def _prepara(self, step: int) -> Iterator[AgentEvent]:
        st = self.st
        tool_ctx = self.tool_ctx
        params = self.params
        current = tool_ctx.plan.current if tool_ctx.plan else None
        new_phase = (str(current.id), current.text) if current else None
        phase_changed = new_phase != st.phase_key
        st.phase_step = 1 if step == 1 or phase_changed or st.retry_reasoning else st.phase_step + 1
        reasoning_phase = "recovery" if st.retry_reasoning else (
            "decision" if st.phase_step == 1 else "execution"
        )
        st.phase_key = new_phase
        st.retry_reasoning = False
        if phase_changed:
            self.blocco_libreria = self._blocco_libreria()

        # I tool del passo appena chiuso contano per il punto a cui
        # appartenevano: si registrano **prima** di passare al punto nuovo.
        ultimo_passo, self.chiamate_passo = self.chiamate_passo, []
        if ultimo_passo:
            self.stato_punto.registra_passo(ultimo_passo)
        self.stato_punto.nuovo_punto(new_phase)
        regia_passo = regia.decidi(
            getattr(current, "tipo", None) if current else None,
            reasoning_phase,
            self.stato_punto,
            ha_piano=current is not None,
            passo_nel_turno=step,
            verifica_rossa=bool(getattr(tool_ctx, "red_command", None)),
            ultimo_passo=ultimo_passo,
        )
        blocco_piano = render_block(tool_ctx.plan, steps_left=self.max_steps - step)
        if regia_passo.sintesi and not self.stato_punto.sintesi_suggerita:
            # Una volta per punto: ripeterlo ad ogni lettura diventerebbe il
            # rumore di fondo che il modello impara a ignorare.
            self.stato_punto.sintesi_suggerita = True
            blocco_piano = "\n\n".join(
                b for b in (
                    blocco_piano,
                    regia.SUGGERIMENTO_SINTESI.format(n=self.stato_punto.letture_di_fila),
                ) if b
            )

        api_messages = self._costruisci(step, blocco_piano)

        # Compattazione a soglia, *fra un passo e l'altro*, misurata sulla
        # **finestra efficace** (vedi ``TETTO_TOKEN_DEFAULT``).
        finestra_compat = finestra_efficace(params.num_ctx, self.compact_max_tokens)
        if self.compact_history and step >= st.compattazione_ferma_fino_a and context_pressure(
            api_messages, finestra_compat, reserved_tokens=self.schema_tokens,
        ) > self.soglia:
            prima_tok = estimate_messages_tokens(api_messages)
            selezionata = None
            if self.selezione in ("regole", "valutatore"):
                selezionata, api_messages = yield from self._selezione(
                    step, blocco_piano, api_messages, finestra_compat, prima_tok,
                )
            esito = None if selezionata is not None else compatta_cronologia(
                self.ui_messages,
                backend=self.backend.scope("compaction"),
                params=params,
                budgets=self.budgets,
                strip_thinking=self.strip_thinking,
                finestra=finestra_compat,
                schedario=self.schedario,
                should_stop=self.stopped,
            )
            if esito is None and selezionata is None and not self.stopped():
                st.compattazione_ferma_fino_a = step + PAUSA_COMPATTAZIONE_FALLITA
                self.conta("compattazione_rinviata")
            if esito is not None:
                self.conta("compattazione")
                # L'indice si rilegge dal disco solo qui: e' l'unico momento in
                # cui puo' essere cambiato.
                self.blocco_libreria = self._blocco_libreria()
                api_messages = self._costruisci(step, blocco_piano)
                yield HistoryCompacted(
                    messages=esito.messaggi_prima,
                    tokens_before=prima_tok,
                    tokens_after=estimate_messages_tokens(api_messages),
                    summary=esito.riassunto,
                )

        # Ultima spiaggia: se anche dopo il riassunto il contesto sfonda, si
        # buttano i turni piu' vecchi dalla sola vista API. Resta l'ULTIMA
        # spiaggia: la soglia e' la maggiore fra quella dello scarto e quella
        # dell'utente, cosi' non scatta mai prima della compattazione.
        if self.stopped():
            yield from self.halt(step - 1)
            return None
        margine_sfondamento = max(HISTORY_COMPACT_THRESHOLD, float(self.soglia))
        if context_pressure(
            api_messages, params.num_ctx, reserved_tokens=self.schema_tokens,
        ) * st.fattore_stima > margine_sfondamento:
            api_messages = drop_oldest_turns(
                api_messages, params.num_ctx, soglia=margine_sfondamento,
                reserved_tokens=self.schema_tokens, fattore=st.fattore_stima,
            )
        # Quanto pesa, secondo la stima, la richiesta che parte davvero: e' il
        # denominatore della calibrazione, a fine stream.
        stima_passo = estimate_messages_tokens(api_messages) + self.schema_tokens

        # Il budget di servizio e' finito: si dice, invece di smettere in
        # silenzio.
        if not st.servizio_disponibile and not st.servizio_esaurito:
            st.servizio_esaurito = True
            yield AgentError(
                f"Passi di servizio esauriti ({st.max_passi_di_servizio} di "
                f"{self.max_steps}): i solleciti automatici si spengono e i passi "
                f"restanti vanno tutti al lavoro."
            )

        # La soglia si ricalcola ad ogni passo: il primo di un turno ha diritto
        # a piu' pensiero degli altri.
        params_passo = params_for_step(
            params, tool_ctx.plan, st.phase_step, regia_passo.tipo_effettivo
        )
        # Il tetto si taglia **dopo** la compattazione e il drop dei turni
        # vecchi: prima di quelli il prompt non e' ancora quello che partira'.
        tetto_passo, spazio_finestra = tetto_per_la_finestra(
            api_messages, params.num_ctx, self.max_tokens_turno,
            reserved_tokens=self.schema_tokens, fattore=st.fattore_stima,
        )
        # Due modi di chiudere un pensiero troppo lungo: **continuare** (il
        # pensiero resta, gli si accoda una frase che chiude) o
        # **interrompere** (il pensiero si butta e il passo si rifa'). Vedi
        # ``core/regia_pensiero.py``.
        backend = self.backend
        puo_continuare = bool(
            self.think_watchdog
            and getattr(backend, "supports_think_continuation", False) is True
            and callable(getattr(backend, "modo_continuazione", None))
            and backend.modo_continuazione() is not None
        )
        if self.think_watchdog and tetto_passo > 0:
            storico = watchdog_chars_for_step(tetto_passo, st.phase_step)
            per_tipo = min(
                chars_for_tokens(regia_passo.budget),
                chars_for_tokens(tetto_passo * WATCHDOG_RATIO),
            )
            # La soglia per *buttare* il pensiero non scende mai sotto lo
            # storico, nemmeno quando il backend saprebbe continuare.
            soglia_interrompi = max(storico, per_tipo)
            watchdog_chars = per_tipo if puo_continuare else soglia_interrompi
        else:
            watchdog_chars = 0
            soglia_interrompi = 0
        if tetto_passo != self.max_tokens_turno:
            try:
                params_passo = replace(params_passo, max_tokens=tetto_passo)
            except TypeError:      # non e' una dataclass: si lascia stare
                pass
        # Una volta sola per turno: e' l'unica cosa che l'utente puo' sistemare
        # (alzare -c, abbassare max_tokens, chat nuova).
        if tetto_passo < TETTO_INUTILE and not st.avvisato_finestra:
            st.avvisato_finestra = True
            yield AgentError(
                f"Finestra quasi piena: restano {max(spazio_finestra, 0)} token "
                f"su {params.num_ctx}. Da qui in avanti la generazione verra' "
                "tagliata a meta' -- anche dentro una chiamata a un tool. "
                "Alza la finestra del server, abbassa max_tokens, o comincia "
                "una conversazione nuova."
            )
        # ...e sotto il tetto inutile il turno **finisce**, invece di generare
        # lo stesso: una risposta vuota farebbe scattare i solleciti, che
        # riducono ancora lo spazio. Un giro a vuoto che si stringe da solo.
        if tetto_passo < TETTO_INUTILE:
            yield AgentError(
                f"Turno interrotto al passo {step}: nella finestra non resta "
                f"spazio per generare ({max(spazio_finestra, 0)} token liberi "
                f"su {params.num_ctx}, ne servono almeno {TETTO_INUTILE}). "
                "Il lavoro fatto finora e' salvo: comincia una conversazione "
                "nuova, o alza la finestra del modello."
            )
            yield self.fine("finestra_piena", step)
            return None
        return _Preparazione(
            api_messages=api_messages, params_passo=params_passo, tetto_passo=tetto_passo,
            spazio_finestra=spazio_finestra, stima_passo=stima_passo,
            watchdog_chars=watchdog_chars, soglia_interrompi=soglia_interrompi,
            puo_continuare=puo_continuare, regia_passo=regia_passo,
            reasoning_phase=reasoning_phase,
        )

    # ------------------------------------------------------------------
    # GENERA
    # ------------------------------------------------------------------
    def _genera(self, step: int, prep: _Preparazione) -> Iterator[AgentEvent]:
        st = self.st
        backend = self.backend
        stopped = self.stopped
        parser = ThinkStreamParser()
        gen = _Generazione(parser=parser, tool_calls=[], params_passo=prep.params_passo)
        tool_calls = gen.tool_calls
        # Un rubinetto per passo: cosa e' gia' arrivato alla UI vale per questa
        # generazione e non per la prossima, che riparte da testo vuoto.
        rubinetto = Rubinetto()
        events: Iterator[StreamEvent] | None = None
        osservatore = regia.OsservatorePensiero(
            prep.watchdog_chars,
            tipo=prep.regia_passo.tipo_effettivo,
            nomi_tool=self.nomi_tool_schema,
            # Chiudere su "decisione gia' scritta" ha senso solo se chiudere
            # costa poco: col solo watchdog che butta il pensiero, no.
            rileva_decisione=prep.puo_continuare,
        )
        gen.osservatore = osservatore
        modo_continuazione: bool | None = None
        pensiero_al_taglio = 0
        pensiero_nativo = False
        messaggi_stream = prep.api_messages
        params_stream = prep.params_passo
        tetto_passo = prep.tetto_passo
        # Il cruscotto del passo: velocita', fase, prefill, draft. Vede gli
        # stessi eventi del ciclo e non ne cambia nessuno (``core/cruscotto``).
        tachimetro = Tachimetro(
            step,
            prompt_stimato=prep.stima_passo,
            finestra=int(getattr(prep.params_passo, "num_ctx", 0) or 0),
            un_token_per_chunk=getattr(backend, "un_token_per_chunk", False) is True,
            prompt_intero=getattr(backend, "prompt_tokens_is_total", False) is True,
            turno_inizio=round(self.turn_started_epoch, 3),
        )
        # Il primo frame parte subito: dice "attesa" e quanto e' grosso il
        # prompt, che durante il prefill e' l'unica cosa che si sa.
        yield tachimetro.aggiorna(forza=True)
        prima_richiesta = True
        try:
            stream_options = {"should_stop": stopped} if getattr(
                backend, "supports_cancellation", False
            ) else {}
            while True:
                if not prima_richiesta:
                    tachimetro.nuova_richiesta()
                prima_richiesta = False
                events = backend.stream(messaggi_stream, self.tools_schema, params_stream,
                                        **stream_options)
                taglio: str | None = None
                for ev in events:
                    tachimetro.evento(ev)
                    metriche = tachimetro.aggiorna()
                    if metriche is not None:
                        yield metriche
                    if stopped():
                        # Chiudere il generatore fa cadere la connessione HTTP:
                        # senza, la generazione continuerebbe a occupare la GPU.
                        gen.interrupted = True
                        close = getattr(events, "close", None)
                        if callable(close):
                            close()
                        break
                    if ev.kind == "battito":
                        continue
                    if ev.kind == "reasoning":
                        pensiero_nativo = True
                        if parser.feed_reasoning(ev.text):
                            yield from rubinetto.aggiorna(parser.reasoning, parser.answer)
                    elif ev.kind == "content":
                        r_changed, a_changed = parser.feed(ev.text)
                        if r_changed or a_changed:
                            yield from rubinetto.aggiorna(parser.reasoning, parser.answer)
                    elif ev.kind == "tool_call" and ev.tool_call:
                        if len(tool_calls) >= MAX_TOOL_CALLS_PER_STEP:
                            gen.stream_error = "Protocol error: too many tool calls in one step"
                            break
                        tool_calls.append(ev.tool_call)
                    elif ev.kind == "usage" and ev.usage:
                        gen.done_reason = str(ev.usage.get("done_reason") or "")
                        gen.usage_passo = dict(ev.usage)
                        for key, value in ev.usage.items():
                            if isinstance(value, (int, float)):
                                self.total_usage[key] = self.total_usage.get(key, 0) + value
                            else:
                                self.total_usage[key] = value
                    elif ev.kind == "error":
                        gen.stream_error = ev.text
                        break

                    if gen.chiusura_passo is not None:
                        # Dentro una continuazione. Se il modello, invece di
                        # agire, ricomincia a pensare da capo, il server non ha
                        # rispettato il pensiero precompilato: si smette subito
                        # e si ricade sul watchdog che interrompe.
                        if (
                            not tool_calls
                            and not parser.answer.strip()
                            and len(parser.reasoning) - pensiero_al_taglio
                            > regia.CONTINUAZIONE_MAX_PENSIERO
                        ):
                            gen.continuazione_rifiutata = True
                            taglio = "rifiutata"
                            close = getattr(events, "close", None)
                            if callable(close):
                                close()
                            break
                        continue

                    # Watchdog sul ragionamento: vale per il canale nativo e
                    # per i tag <think> nel testo. La continuazione invece vale
                    # solo per il canale nativo.
                    if prep.watchdog_chars and not tool_calls:
                        ragione = osservatore.aggiorna(parser.reasoning)
                        if (
                            ragione
                            and prep.puo_continuare
                            and pensiero_nativo
                            and not parser.answer.strip()
                            and tetto_passo - estimate_tokens(parser.reasoning)
                            >= regia.CONTINUAZIONE_MIN_TOKEN
                        ):
                            taglio = ragione
                        elif (
                            len(parser.reasoning) > prep.soglia_interrompi
                            and st.watchdog_fires < MAX_WATCHDOG_FIRES
                            and step < self.max_steps
                        ):
                            taglio = "interrompi"
                        if taglio:
                            close = getattr(events, "close", None)
                            if callable(close):
                                close()
                            break

                if taglio in regia.CHIUSURE and not (gen.interrupted or gen.stream_error):
                    modo_continuazione = backend.modo_continuazione()
                    rimasti = tetto_passo - estimate_tokens(parser.reasoning)
                    params_continua = None
                    if (
                        modo_continuazione is not None
                        and rimasti >= regia.CONTINUAZIONE_MIN_TOKEN
                    ):
                        try:
                            params_continua = replace(
                                prep.params_passo, think=modo_continuazione, max_tokens=rimasti
                            )
                        except TypeError:      # non e' una dataclass
                            params_continua = None
                    if params_continua is not None:
                        # La frase entra nel pensiero che l'utente vede: la
                        # chiusura non e' un segreto, e' scritta dove e' successa.
                        parser.feed_reasoning(regia.chiusura(taglio))
                        yield from rubinetto.aggiorna(parser.reasoning, parser.answer)
                        pensiero_al_taglio = len(parser.reasoning)
                        gen.chiusura_passo = taglio
                        messaggi_stream = [
                            *prep.api_messages,
                            {"role": "assistant", "content": "", "thinking": parser.reasoning},
                        ]
                        params_stream = params_continua
                        continue
                    # Continuare non si puo': resta il vecchio modo, e l'unico
                    # esito onesto e' il sollecito.
                    modo_continuazione = None
                    gen.watchdog_hit = True
                elif taglio in ("interrompi", "rifiutata"):
                    gen.watchdog_hit = True
                break
        except Exception as exc:
            logger.exception("Model stream failed at step %s", step)
            gen.stream_error = f"{type(exc).__name__}: {exc}"
        finally:
            close = getattr(events, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    logger.exception("Failed to close model stream")

        if (
            gen.chiusura_passo is not None and modo_continuazione is not None
            and not gen.interrupted
        ):
            esito_c = getattr(backend, "esito_continuazione", None)
            if callable(esito_c):
                esito_c(
                    modo_continuazione,
                    bool(
                        not gen.continuazione_rifiutata
                        and not gen.stream_error
                        and (tool_calls or parser.answer.strip())
                    ),
                )
        if gen.continuazione_rifiutata:
            self.conta("continuazione_rifiutata")

        parser.finish()
        # Il frame definitivo del passo, anche se il passo e' finito male: e'
        # proprio li' che serve sapere quanto aveva generato e in quanto.
        yield tachimetro.chiudi()

        # La calibrazione vale solo se la richiesta misurata e' quella stimata
        # e se il server dichiara il prompt intero (``prompt_tokens_is_total``).
        if (
            gen.chiusura_passo is None
            and getattr(backend, "prompt_tokens_is_total", False) is True
        ):
            misurato = calibra_stima(gen.usage_passo.get("prompt_tokens"), prep.stima_passo)
            if misurato is not None:
                st.fattore_stima = misurato
        self._traccia(step, prep, gen)
        return gen

    def _traccia(self, step: int, prep: _Preparazione, gen: _Generazione) -> None:
        """La traccia di questo passo sul messaggio dell'assistente (``think``)."""
        params = self.params
        tool_ctx = self.tool_ctx
        parser = gen.parser
        usage_passo = gen.usage_passo
        traccia = self.traccia
        traccia.clear()
        traccia.update(
            {
                "passo": step,
                "fase": prep.reasoning_phase,
                "passo_nel_punto": self.st.phase_step,
                "configurato": _nome_livello(getattr(params, "think", None)),
                "usato": _nome_livello(getattr(prep.params_passo, "think", None)),
                "inviato": _livello_inviato(self.backend, prep.params_passo),
                "punto_aperto": bool(getattr(tool_ctx.plan, "current", None)),
                "punto": (
                    tool_ctx.plan.current.id
                    if getattr(tool_ctx.plan, "current", None)
                    else None
                ),
                "pensato": len(parser.reasoning),
                "risposto": len(parser.answer),
                "chiamate": len(gen.tool_calls),
                "watchdog": bool(gen.watchdog_hit),
                "tipo": prep.regia_passo.tipo,
                "tipo_effettivo": prep.regia_passo.tipo_effettivo,
                "budget": int(prep.regia_passo.budget),
                "motivo_budget": prep.regia_passo.motivo,
                "soglia": int(prep.watchdog_chars),
                "chiusura": gen.chiusura_passo,
                "continuata": bool(gen.chiusura_passo) and not gen.continuazione_rifiutata,
                "ripensamenti": int(gen.osservatore.ripensamenti),
                "oscillazione": bool(gen.osservatore.oscillazione),
                "spazio": int(prep.spazio_finestra),
                "tetto": int(prep.tetto_passo),
                "prompt_stimato": int(prep.stima_passo),
                "prompt_reale": usage_passo.get("prompt_tokens"),
                "cache": usage_passo.get("cached_tokens"),
                "ricalcolati": usage_passo.get("prompt_processed_tokens"),
                "fattore_stima": round(self.st.fattore_stima, 3),
                "senza_progresso": self.st.avanzamento.senza_progresso,
            }
        )
        if traccia["inviato"] not in (traccia["usato"], "?"):
            traccia["traduzione_livello"] = f"{traccia['usato']} -> {traccia['inviato']}"
        self.stato_traccia["da"] = len(self.ui_messages)

    # ------------------------------------------------------------------
    # VALUTA
    # ------------------------------------------------------------------
    def _valuta(self, step: int, gen: _Generazione) -> Iterator[AgentEvent]:
        st = self.st
        parser = gen.parser
        ui_messages = self.ui_messages
        if gen.interrupted or self.stopped():
            yield from self.halt(step, parser.reasoning, parser.answer)
            return FINE

        if gen.stream_error:
            # Una chiamata caduta non e' un turno perso: la cronologia e i file
            # sono intatti, manca solo la risposta a questo passo.
            if (
                step < self.max_steps
                and st.riprese_stream < MAX_RIPRESE_STREAM
                and errore_riprovabile(gen.stream_error)
                and not getattr(self.backend, "manages_retries", False)
                and st.servizio_disponibile
            ):
                st.riprese_stream += 1
                self.conta("ripresa_stream")
                st.passi_di_servizio += 1
                yield AgentError(
                    f"Chiamata al modello interrotta ({gen.stream_error}) — riprendo "
                    f"da dove eravamo, tentativo {st.riprese_stream} di "
                    f"{MAX_RIPRESE_STREAM}.",
                    guasto_backend=True,
                )
                # Il caso piu' frequente e' il modello che sta entrando in VRAM.
                if PAUSA_RIPRESA_S:
                    time.sleep(PAUSA_RIPRESA_S)
                return PROSSIMO
            registra_errore(ui_messages, gen.stream_error)
            yield AgentError(gen.stream_error, guasto_backend=True)
            yield self.fine("error", step)
            return FINE

        # Il testo completo, una volta per passo: la rete di sicurezza degli
        # incrementi per chi si e' perso un frame.
        if parser.reasoning:
            yield ReasoningDelta(text=parser.reasoning)
        if parser.answer:
            yield ContentDelta(text=parser.answer)

        contesto = self._contesto_reti(step, gen, parser.answer, parser.reasoning)
        if gen.watchdog_hit:
            contesto.watchdog = True
            contesto.pensiero_token = estimate_tokens(parser.reasoning)
            intervento = reti_mod.rete_pensiero(contesto)
            st.avanzamento.passo_a_vuoto("pensiero interrotto")
            return (yield from self._applica(step, intervento, parser.answer, parser.reasoning))

        answer, faked_tool_output = strip_tool_wrappers(parser.answer.strip())
        reasoning = parser.reasoning.strip()
        if faked_tool_output:
            # Il modello ha scritto un <tool_response> di sua invenzione: non
            # e' una risposta, e non deve impedire il riepilogo finale.
            yield AgentError(
                "Il modello ha inventato un risultato di tool "
                f"(<tool_response>): {faked_tool_output[:200]}"
            )

        # --- recupero delle tool call stampate come testo --------------------
        # Solo se chi chiama lo chiede: di serie un esempio mostrato resta testo.
        gen.recovered = False
        if self.allow_text_tool_calls and not gen.tool_calls and answer:
            candidates, leftover = parse_text_tool_calls(answer)
            if candidates:
                gen.tool_calls[:] = candidates
                answer = leftover
                gen.recovered = True
        gen.answer = answer
        gen.reasoning = reasoning

        if gen.tool_calls:
            return AVANTI

        # --- nessuna tool call: le reti di sicurezza, in ordine ---------------
        contesto = self._contesto_reti(step, gen, answer, reasoning)
        contesto.chiamate_nel_testo = nomi_chiamate_nel_testo(answer)
        contesto.risposta_inventata = faked_tool_output or ""
        intervento = reti_mod.decidi_senza_tool(contesto, reti_mod.RETI_SENZA_TOOL[1:])
        if intervento is not None:
            st.avanzamento.passo_a_vuoto(f"sollecito {intervento.rete}")
            return (yield from self._applica(step, intervento, answer, reasoning))

        ui_messages.append(
            {"role": "assistant", "content": _wrap(reasoning, answer), "ts": time.time()}
        )
        yield AssistantTurn(content=answer, reasoning=reasoning, has_tool_calls=False)
        yield self.fine("completed", step)
        return FINE

    def _contesto_reti(self, step: int, gen: _Generazione, answer: str,
                       reasoning: str) -> reti_mod.Contesto:
        return reti_mod.Contesto(
            stato=self.st,
            passo=step,
            max_passi=self.max_steps,
            risposta=answer,
            ragionamento=reasoning,
            done_reason=gen.done_reason,
            richiesta=last_user_request(self.ui_messages),
            piano=self.tool_ctx.plan,
            verifiche=self.verification,
            tool_ctx=self.tool_ctx,
            enable_nudge=self.enable_nudge,
            require_summary=self.require_summary,
            max_tokens=int(getattr(self.params, "max_tokens", 0) or 0),
        )

    def _applica(self, step: int, iv: reti_mod.Intervento, answer: str,
                 reasoning: str) -> Iterator[AgentEvent]:
        """Il punto unico in cui una rete diventa messaggi ed eventi."""
        st = self.st
        ui_messages = self.ui_messages
        if iv.errore_prima:
            yield AgentError(iv.errore_prima)
        if iv.registra == "sempre":
            ui_messages.append(
                {"role": "assistant", "content": _wrap(reasoning, answer), "ts": time.time()}
            )
        elif iv.registra == "se_detto":
            _registra_il_detto(ui_messages, reasoning, answer)
        if iv.sollecito:
            ui_messages.append({"role": "user", "content": iv.sollecito, "hidden": True})
        if iv.turno_assistente:
            yield AssistantTurn(content=answer, reasoning=reasoning, has_tool_calls=False)
        if iv.errore_dopo and iv.chiudi == "error":
            registra_errore(ui_messages, iv.errore_dopo)
        if iv.errore_dopo:
            yield AgentError(iv.errore_dopo)
        if iv.conteggio:
            self.conta(iv.conteggio)
        st.segna(iv.rete)
        if iv.rete == "pensiero" and not iv.chiudi:
            st.watchdog_fires += 1
        if iv.servizio:
            st.passi_di_servizio += 1
        if iv.chiudi:
            yield self.fine(iv.chiudi, step)
            return FINE
        return PROSSIMO

    # ------------------------------------------------------------------
    # ESEGUI
    # ------------------------------------------------------------------
    def _esegui_tool(self, step: int, gen: _Generazione) -> Iterator[AgentEvent]:
        st = self.st
        tool_ctx = self.tool_ctx
        ui_messages = self.ui_messages
        tool_calls = gen.tool_calls
        answer, reasoning, recovered = gen.answer, gen.reasoning, gen.recovered
        self.chiamate_eseguite: list[Chiamata] = []
        # Validate envelopes as a batch before any effect; duplicate ids make
        # results ambiguous and malformed envelopes must never reach indexing.
        seen_ids: set[str] = set()
        malformed = False
        for call in tool_calls:
            if (
                not isinstance(call, dict)
                or not isinstance(call.get("id"), str)
                or not call["id"]
                or call["id"] in seen_ids
                or not isinstance(call.get("name"), str)
                or not isinstance(call.get("arguments"), str)
            ):
                malformed = True
                break
            seen_ids.add(call["id"])
        if malformed:
            registra_errore(
                ui_messages, "Protocol error: malformed or duplicate tool-call envelope"
            )
            yield AgentError("Protocol error: malformed or duplicate tool-call envelope")
            yield self.fine("error", step)
            return FINE

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

        # ask_user_question sospende il turno, quindi va eseguito per ultimo:
        # cosi' tutti gli altri tool dello stesso passo hanno gia' il loro
        # risultato in cronologia e non restano tool_call_id scoperti.
        asks = [c for c in tool_calls if c["name"] == ASK_USER_TOOL]
        ordered = [c for c in tool_calls if c["name"] != ASK_USER_TOOL] + asks

        def reject_call(call: dict[str, Any], error: dict[str, Any]) -> ToolFinished:
            """Close a call without execution, preserving the protocol pairing."""
            result = json.dumps(error, ensure_ascii=False)
            ui_messages.append({
                "role": "tool", "tool_call_id": call["id"], "name": call["name"],
                "content": result, "args": {}, "duration_s": 0.0,
                "ok": False, "ts": time.time(),
            })
            return ToolFinished(call_id=call["id"], name=call["name"], args={},
                                result=result, duration_s=0.0, ok=False)

        def close_remaining(
            calls: list[dict[str, Any]], *, per_stop: bool = False
        ) -> Iterator[ToolFinished]:
            # Due cause diverse, due frasi diverse: lo stop dell'utente non e'
            # una domanda in sospeso.
            errore = ({
                "error": "Chiamata non eseguita: il turno e' stato interrotto dall'utente.",
                "error_code": "turn_stopped", "retryable": False,
                "hint": "Se serve ancora, rivalutala alla prossima richiesta.",
            } if per_stop else {
                "error": "Chiamata non eseguita: il turno e' sospeso.",
                "error_code": "turn_suspended", "retryable": False,
                "hint": "Attendi la risposta dell'utente e rivaluta questa azione.",
            })
            for pending in calls:
                yield reject_call(pending, dict(errore))

        for call_index, call in enumerate(ordered):
            if call["name"] not in self.allowed_tools:
                yield reject_call(call, {
                    "error": f"Tool '{call['name']}' non disponibile in questo turno.",
                    "error_code": "tool_not_allowed", "retryable": False,
                    "hint": "Usa solo i tool presenti nello schema della richiesta corrente.",
                })
                continue
            if self.stopped():
                yield from close_remaining(ordered[call_index:], per_stop=True)
                yield from self.halt(step)
                return FINE
            try:
                validated_args = loads_object(call["arguments"])
            except JsonBoundaryError as exc:
                error = argomenti_illeggibili(
                    call["arguments"], gen.done_reason,
                    int(getattr(gen.params_passo, "max_tokens", 0) or 0),
                )
                error.update(error_code="invalid_json", retryable=False,
                             details=[{"path": "$", "message": str(exc)}])
            else:
                error = validate_tool_arguments(call["name"], validated_args)
            if error:
                yield reject_call(call, error)
                continue
            if call["name"] == ASK_USER_TOOL:
                raw_args = validated_args
                question = normalise_question(raw_args if isinstance(raw_args, dict) else {})

                if not question["question"]:
                    yield reject_call(call, {
                        "error": "Parametro 'question' mancante.",
                        "error_code": "invalid_arguments", "retryable": False,
                        "hint": "Riformula la domanda in una frase.",
                    })
                    continue

                # Il risultato di questo tool arriva dall'utente: lo si scrive
                # in cronologia solo alla ripresa (vedi resume_with_answer).
                yield from close_remaining(ordered[call_index + 1:])
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
                yield self.fine("awaiting_user", step)
                return FINE

            args = validated_args

            if self.stopped():
                # Il tool non parte, ma la sua tool_call e' gia' in cronologia:
                # va comunque chiusa con un risultato.
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

            # Diario degli effetti: l'intento si scrive **prima** dell'effetto.
            # Il server salva su ToolStarted dei tool con effetti, quindi un
            # crash durante l'esecuzione lascia su disco la traccia che la
            # chiamata era partita (vedi ``core/ciclo/ripresa.py``).
            # Niente ``hidden``: quel flag vuol dire "sollecito dell'harness al
            # modello"; l'intento non arriva ne' al modello ne' alla UI, che
            # ignorano il ruolo ``intento``.
            if call["name"] in ripresa_mod.EFFETTI:
                ui_messages.append(ripresa_mod.intento(
                    call["id"], call["name"], args,
                    str(getattr(tool_ctx, "workspace", "") or ""),
                ))
            yield ToolStarted(call_id=call["id"], name=call["name"], args=args)

            started = time.monotonic()
            result = dispatch(tool_ctx, call["name"], args)
            duration = time.monotonic() - started

            ok = _esito_del_tool(result)
            # La ripetizione si registra **dopo** l'esecuzione e solo se e'
            # andata bene: rifare una chiamata che era fallita e' legittimo.
            if ok:
                if call["name"] in ("write_file", "edit_file"):
                    # Una scrittura invalida le letture e i fallimenti...
                    st.ripetizioni.dimentica_letture()
                    st.ripetizioni.dimentica_fallimenti()
                    # ...a meno che il "qualcosa" sia tornare indietro.
                    ritornato = st.ritorni.registra(result)
                    if (
                        ritornato is not None
                        and ritornato[1] >= RitorniDeiFile.SOGLIA
                        and not st.scattata("oscillazione")
                    ):
                        st.segna("oscillazione")
                        st.oscillazione_dovuta = ritornato
                else:
                    quante = st.ripetizioni.registra(call["name"], args)
                    if quante >= RipetizioniTool.SOGLIA and not st.scattata("ripetizione"):
                        st.segna("ripetizione")
                        st.ripetizione_dovuta = (call["name"], quante)
            else:
                # Lo stallo piu' comune -- stesso rifiuto, stessa riga, tre
                # volte -- non lo vedeva nessuna delle altre difese.
                quante = st.ripetizioni.registra_fallita(call["name"], args)
                if quante >= RipetizioniTool.SOGLIA_STALLO and not st.scattata("stallo"):
                    st.segna("stallo")
                    st.stallo_dovuto = (call["name"], quante)
            self.verification.record(call["name"], result)
            # Il guard sui file di test ha bisogno di sapere se c'e' una
            # verifica rossa aperta: qui e' l'unico punto che lo sa.
            red_now = self.verification.unresolved
            tool_ctx.red_command = red_now[0] if red_now else None
            if call["name"] == "run_command" and not _comando_riuscito(result):
                ok = False
            if not ok:
                st.retry_reasoning = True
            if reti_mod.e_una_possibile_scrittura(call["name"], args, ok):
                st.scritture_riuscite += 1
            self.chiamate_passo.append((str(call["name"]), bool(ok)))
            self.chiamate_eseguite.append(Chiamata(str(call["name"]), dict(args), bool(ok),
                                                   leggi_esito(result)))
            st.tools_used = True
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
                # parta il lavoro. Scatta una volta sola.
                if (
                    self.plan_gate
                    and not st.gate_mostrato
                    and str(args.get("action") or "").strip().lower() == "set"
                    and len(tool_ctx.plan.steps) >= PUNTI_PER_IL_CANCELLO
                ):
                    st.gate_mostrato = True
                    yield from close_remaining(ordered[call_index + 1:])
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
                    yield self.fine("awaiting_user", step)
                    return FINE
            if call["name"] == NOTES_TOOL and ok:
                yield NotesUpdated(notes=tool_ctx.notes.to_list())
            anteprima = preview_from_result(
                call["name"], args, result, ok, auto=self.auto_preview
            )
            if anteprima is not _NO_PREVIEW:
                # La cartella servita si sa solo qui, dove c'e' il workspace.
                if (
                    isinstance(anteprima, dict)
                    and anteprima.get("path")
                    and "root" not in anteprima
                ):
                    anteprima["root"] = preview_root(
                        tool_ctx.workspace, str(anteprima["path"])
                    )
                tool_ctx.preview = anteprima
                yield PreviewUpdated(payload=anteprima)
        return AVANTI

    # ------------------------------------------------------------------
    # DOPO I TOOL
    # ------------------------------------------------------------------
    def _dopo_tool(self, step: int, gen: _Generazione) -> Iterator[AgentEvent]:
        st = self.st
        tool_ctx = self.tool_ctx
        ui_messages = self.ui_messages
        # --- estratto del pensiero dei punti appena chiusi ------------------
        # La casella postale si svuota **sempre**, anche quando non si estrae.
        chiusi = list(tool_ctx.punti_chiusi)
        tool_ctx.punti_chiusi.clear()
        if chiusi and self.estratto_pensiero and self.schedario is not None and not self.stopped():
            # La traccia di questo passo si attacca adesso: il pensiero del
            # passo che ha chiuso il punto appartiene a quel punto.
            self.marca_pensiero()
            for chiuso in chiusi:
                blocchi = pensiero.blocchi_del_punto(ui_messages, chiuso["id"])
                if not blocchi:
                    continue
                distillato = pensiero.estrai(
                    blocchi,
                    punto=chiuso["text"],
                    backend=self.backend.scope("memory_extract"),
                    params=self.params,
                    should_stop=self.stopped,
                )
                if not distillato:
                    continue
                voce = libreria.archivia_estratto(
                    self.schedario,
                    punto=chiuso["text"],
                    estratto=distillato,
                    saltato=bool(chiuso.get("saltato")),
                )
                if voce is not None:
                    # L'indice in coda cambia adesso, non al turno prossimo.
                    self.blocco_libreria = self._blocco_libreria()

        if self.stopped():
            # A tool conclusi e risultati in cronologia: il punto piu' pulito.
            yield self.fine("stopped", step)
            return FINE

        # --- conteggi del passo ---------------------------------------------
        # Un passo, un punto: il segnale e' il passo che *solo* guarda.
        nomi_del_passo = {c["name"] for c in gen.tool_calls}
        if nomi_del_passo and nomi_del_passo <= set(TOOL_ESPLORATIVI):
            st.esplorazioni_di_fila += 1
        else:
            st.esplorazioni_di_fila = 0
        if len(gen.tool_calls) == 1 and gen.tool_calls[0]["name"] == "read_file":
            st.letture_singole_di_fila += 1
        else:
            st.letture_singole_di_fila = 0
        st.avanzamento.registra_passo(step, self.chiamate_eseguite)

        # --- note in coda ai risultati (``core/ciclo/reti.note_dopo_tool``) --
        # Dopo i risultati, mai in mezzo: spezzerebbero l'adiacenza fra
        # l'assistant con le tool_calls e i suoi risultati.
        for nota in reti_mod.note_dopo_tool(reti_mod.ContestoDopo(
            stato=st, passo=step, max_passi=self.max_steps, recuperate=gen.recovered,
            tool_ctx=tool_ctx, require_plan=self.require_plan, enable_nudge=self.enable_nudge,
            abilita_delega=self.abilita_delega, monitor=self.monitor_avanzamento,
            esempio_delega=self._esempio_delega,
        )):
            self.conta(nota.conteggio)
            ui_messages.append({"role": "user", "content": nota.testo, "hidden": True})

        # --- monitor di avanzamento: la chiusura ---------------------------
        if (
            self.monitor_avanzamento
            and st.avanzamento.da_chiudere
            and step < self.max_steps
        ):
            self.conta("stallo_chiuso")
            yield AgentError(
                f"Turno fermato: {st.avanzamento.senza_progresso} passi di fila senza "
                "niente di verificabile (nessun file nuovo, nessuna scrittura andata "
                "a segno, nessuna verifica passata, nessun punto chiuso)."
            )
            yield from self._riepilogo_forzato(PROMPT_RIEPILOGO_STALLO)
            yield self.fine("stallo", step)
            return FINE
        return AVANTI

    # ------------------------------------------------------------------
    # COMPATTAZIONE SELETTIVA (``core/selezione.py``)
    # ------------------------------------------------------------------
    def _selezione(
        self, step: int, blocco_piano: str, api_messages: list[dict[str, Any]],
        finestra: int, prima_tok: int,
    ) -> Iterator[AgentEvent]:
        """Prova a liberare contesto togliendo chiamate vecchie; None se non basta.

        Generatore con valore di ritorno (``yield from``): emette l'evento di
        compattazione solo se la selezione resta, e torna la selezione (o
        None) con la vista da usare.
        """
        piano = "; ".join(
            f"{p.id}. {p.text}" for p in (self.tool_ctx.plan.open_steps if self.tool_ctx.plan else [])
        )
        valutatore = self.valutatore_selezione if self.selezione == "valutatore" else None

        def pressione(messaggi: list[dict[str, Any]]) -> float:
            return context_pressure(self._costruisci(step, blocco_piano, messaggi), finestra,
                                    reserved_tokens=self.schema_tokens)

        proposta = proponi_selezione(
            self.ui_messages, budgets=self.budgets, strip_thinking=self.strip_thinking,
            finestra=finestra, valutatore=valutatore, piano=piano, pressione=pressione,
        )
        if proposta is None:
            self.conta("selezione_impossibile")
            return None, api_messages
        fine, sel = proposta
        self.ui_messages.insert(fine, sel.record)
        prova = self._costruisci(step, blocco_piano)
        if context_pressure(prova, finestra, reserved_tokens=self.schema_tokens) > (
            selezione_mod.QUOTA_OBIETTIVO
        ):
            # Non basta: si toglie e si riassume come prima. Stessa regola di
            # fast-jev (``minReductionRatio``): meglio un riassunto che una
            # selezione che riscatta al passo dopo.
            del self.ui_messages[fine]
            self.conta("selezione_insufficiente")
            return None, api_messages
        self.conta("compattazione_selettiva")
        if sel.errore:
            self.conta("valutatore_giu")
        yield HistoryCompacted(
            messages=sel.candidati, tokens_before=prima_tok,
            tokens_after=estimate_messages_tokens(prova),
            summary=sel.record["descrizione"],
        )
        return sel, prova

    # ------------------------------------------------------------------
    # CHIUSURA A PASSI FINITI
    # ------------------------------------------------------------------
    def _riepilogo_forzato(self, prompt: str) -> Iterator[AgentEvent]:
        """Una chiamata sola, senza tool, per dire all'utente cosa e' successo."""
        if not (self.require_summary and self.st.tools_used
                and not ultima_risposta(self.ui_messages)):
            return
        self.conta("riepilogo_forzato")
        testo = riepilogo_finale(
            self.ui_messages,
            backend=self.backend.scope("final_summary"),
            params=self.params,
            budgets=self.budgets,
            strip_thinking=self.strip_thinking,
            should_stop=self.stopped,
            prompt=prompt,
        )
        if testo:
            self.ui_messages.append(
                {
                    "role": "assistant",
                    "content": testo,
                    # Detto, perche' non e' una risposta come le altre: e' un
                    # referto chiesto dall'harness a turno gia' finito.
                    "forzato": True,
                    "ts": time.time(),
                }
            )
            yield AssistantTurn(content=testo, reasoning="", has_tool_calls=False)

    def _passi_finiti(self) -> Iterator[AgentEvent]:
        # I passi sono finiti. Non e' un sollecito e non poteva esserlo:
        # ``SUMMARY_NUDGE`` chiede di scrivere al passo **successivo**, e qui
        # un passo successivo non c'e' piu'.
        self.marca_pensiero()
        if self.stopped():
            yield from self.halt(self.max_steps)
            return
        yield from self._riepilogo_forzato(PROMPT_RIEPILOGO_FINALE)
        yield self.fine("stopped" if self.stopped() else "max_steps", self.max_steps)


# ``MAX_VERIFY_NUDGES``, ``MAX_TRUNCATED_NUDGES``, ``LOOP_THRESHOLD`` e
# ``MAX_ESPLORAZIONI_SENZA_DELEGA`` vivono in ``core/ciclo/reti.py`` con le reti
# che li usano; ``TOOL_ESPLORATIVI`` in ``core/ciclo/stato.py``. Qui restano
# importati con gli stessi nomi.

# Passi di attesa prima di ritentare una compattazione che non e' riuscita.
PAUSA_COMPATTAZIONE_FALLITA = 3
# Passi di fila con una sola lettura prima del promemoria di batch in coda.
# Due: il primo passo a lettura singola e' normale (si guarda una cosa e si
# decide), il secondo di fila e' gia' una serie che si poteva chiedere insieme.
PASSI_PER_IL_PROMEMORIA_BATCH = 2

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
# Exit code che segnalano un comando sbagliato, non un progetto rotto.
# La definizione vive nel registro, insieme al resto delle regole su cosa conta
# come verifica; qui resta il nome con cui il ciclo e i test la chiamano.
SHELL_NOT_A_VERIFICATION = verifiche_mod.SHELL_NON_E_UNA_VERIFICA


# Il registro delle verifiche vive in ``core/verifiche.py``. Stava qui dentro

# come ``VerificationTracker``: un dizionario ``comando -> (tentativi, codice)``
# che sapeva dire qual era il rosso piu' insistente e dimenticarli tutti insieme.
# Erano due limiti. L'identita' per stringa faceva di ``pytest x -q`` e
# ``python -m pytest x -q`` due verifiche diverse -- la seconda verde e la prima
# rossa per sempre -- e ``clear()`` cancellava l'intero registro, cosi' che
# dichiarare non pertinente **una** verifica ne faceva sparire anche altre.
#
# Il nome resta qui perche' e' quello che il ciclo e i test chiamano.
VerificationTracker = verifiche_mod.RegistroVerifiche


def _comando_riuscito(result: str) -> bool:
    """Il comando e' andato a buon fine? Come ``_esito_del_tool``, sulla busta.

    Prima: ``'"esito": "ok"' not in result[:200]``. Due modi di sbagliare, e
    tutti e due dipingono di rosso una tendina verde. Il campo ``esito`` viene
    dopo ``command`` e ``stdout`` nella busta di ``run_command``: con un
    comando lungo o un output che comincia subito, a 200 caratteri non ci si
    arriva e ogni comando riuscito veniva contato come fallito. E al contrario,
    un output che contiene ``"esito": "ok"`` per conto suo -- il log di un
    altro turno, un JSON di prova -- lo faceva passare per riuscito.
    """
    try:
        payload = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return '"esito": "ok"' in result[:200]
    if not isinstance(payload, dict):
        return True
    return payload.get("esito") == "ok"


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


def _registra_il_detto(
    ui_messages: list[dict[str, Any]], reasoning: str, answer: str
) -> None:
    """Mette in cronologia quello che il modello ha detto, **se** ha detto qualcosa.

    I tre rami dei solleciti (coverage, ask, tool) lo accodavano senza guardare:
    quando il modello non produce ne' pensiero ne' risposta -- e succede, e' il
    caso stesso che fa scattare il sollecito -- la sessione si riempiva di
    messaggi ``assistant`` con contenuto vuoto. Al modello non arrivano
    (``build_api_messages`` scarta un assistant senza contenuto ne' tool call),
    ma restano nel file e nella UI, dove somigliano a risposte perdute.

    Il ramo del riepilogo la guardia ce l'aveva gia': questa funzione e' quella
    guardia, scritta una volta sola.
    """
    if reasoning or answer:
        ui_messages.append(
            {"role": "assistant", "content": _wrap(reasoning, answer), "ts": time.time()}
        )


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
