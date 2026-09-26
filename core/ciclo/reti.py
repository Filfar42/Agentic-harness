"""Le reti di sicurezza del ciclo, come tabella ordinata.

Cosa c'era prima
----------------
Sei rami in fila dentro ``run_turn``, ognuno con la sua guardia, il suo flag,
la sua regola sul budget di servizio e le sue quattro righe per registrare il
detto, accodare il sollecito ed emettere gli eventi. L'ordine -- chi vince se
due reti si applicano nello stesso passo -- era l'ordine in cui erano scritti
gli ``if``. Funzionava; ma aggiungerne una voleva dire trovare il punto giusto
in mille righe, e provarne una voleva dire far girare un turno intero.

Cosa c'e' adesso
----------------
Ogni rete e' una **funzione pura** ``(Contesto) -> Intervento | None``. Le
reti della famiglia "senza tool" (il modello ha chiuso il passo senza chiamate)
stanno in ``RETI_SENZA_TOOL``, in ordine di precedenza: la prima che si
applica vince, e un solo punto del ciclo applica l'intervento. Le note "dopo i
tool" (si accodano ai risultati senza costare un passo) stanno in
``note_dopo_tool``.

L'ordine, e perche'::

    pensiero      il watchdog ha chiuso lo stream: il passo non e' finito
    troncato      finiti i token prima dell'azione: cambia il senso di tutto il resto
    canale        chiamata scritta come testo: non e' una risposta, e' un errore di protocollo
    verifica      verifica rossa aperta
    piano_aperto  risposta finale con punti del piano ancora aperti
    copertura     verifica verde che non tocca il codice nuovo
    riepilogo     tool usati e nessuna parola
    spinta        domanda fatta a parole / azione promessa e non eseguita
    senza_prova   "fatto" senza nessuna scrittura riuscita
    (nessuna)     risposta accettata

Tre reti sono nuove (25/09/2026): ``canale``, ``piano_aperto``,
``senza_prova``. Le altre sono quelle di prima, con le stesse condizioni e gli
stessi testi: le 1.491 prove che c'erano lo verificano.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..plan import render_summary
from ..prompts import (
    ASK_NUDGE,
    CANALE_NUDGE,
    COVERAGE_NUDGE,
    DELEGA_NUDGE,
    FAILED_SUMMARY_NUDGE,
    JSON_LEAK_NUDGE,
    LOOP_NUDGE,
    OSCILLAZIONE_NUDGE,
    PIANO_APERTO_NUDGE,
    PLAN_NUDGE,
    PLAN_SUMMARY_NUDGE,
    RIORIENTA_NUDGE,
    RIPETIZIONE_NUDGE,
    SENZA_PROVA_NUDGE,
    STALLO_NUDGE,
    SUMMARY_NUDGE,
    THINK_WATCHDOG_NUDGE,
    TOOL_NUDGE,
    TOOL_RESPONSE_NUDGE,
    TRUNCATED_NUDGE,
    VERIFY_NUDGE,
)
from ..tools import looks_like_verification, uncovered_symbols
from .segnali import (
    chiede_modifiche,
    dichiara_completamento,
    looks_like_clarifying_question,
    looks_like_unexecuted_action,
    user_expects_tool_use,
)
from .stato import StatoTurno

# Quante volte l'harness insiste perche' una verifica rossa venga riparata,
# prima di lasciar chiudere il turno con un rapporto onesto. Due spinte bastano:
# oltre, si consumerebbe l'intero budget di passi su un problema che il modello
# evidentemente non sa risolvere da solo.
MAX_VERIFY_NUDGES = 2
# Stessa logica per il troncamento: due tentativi di riportarlo all'azione.
MAX_TRUNCATED_NUDGES = 2
# Stesso comando fallito questo numero di volte = il modello sta girando a
# vuoto e va dirottato invece che spronato.
LOOP_THRESHOLD = 3
# Chiamate scritte come testo: due solleciti, poi il turno si chiude con un
# errore di protocollo invece di passare il JSON per una risposta.
MAX_CANALE = 2


@dataclass(frozen=True, slots=True)
class Intervento:
    """Cosa fa il ciclo quando una rete si applica. Lo applica un punto solo.

    ``registra``: ``"sempre"`` accoda il messaggio dell'assistente anche vuoto
    (e' il comportamento storico della verifica rossa), ``"se_detto"`` solo se
    c'e' pensiero o risposta, ``"mai"`` non lo accoda (troncamento e watchdog:
    un pensiero spezzato non si rimette in contesto).
    """

    rete: str
    conteggio: str = ""
    sollecito: str = ""
    registra: str = "se_detto"
    turno_assistente: bool = True
    errore_prima: str = ""
    errore_dopo: str = ""
    servizio: bool = True
    # Se valorizzato, il turno si chiude con questo motivo invece di continuare.
    chiudi: str = ""


@dataclass
class Contesto:
    """Quello che le reti possono leggere. Niente di cio' che c'e' qui si muta."""

    stato: StatoTurno
    passo: int
    max_passi: int
    risposta: str
    ragionamento: str
    done_reason: str
    richiesta: str
    piano: Any
    verifiche: Any
    tool_ctx: Any
    enable_nudge: bool = True
    require_summary: bool = True
    max_tokens: int = 0
    watchdog: bool = False
    pensiero_token: int = 0
    chiamate_nel_testo: list[str] = field(default_factory=list)
    risposta_inventata: str = ""

    @property
    def rossa(self) -> tuple[str, int, int] | None:
        return self.verifiche.unresolved if self.verifiche is not None else None

    @property
    def c_e_ancora_un_passo(self) -> bool:
        return self.passo < self.max_passi


# ---------------------------------------------------------------------------
# Famiglia "senza tool"
# ---------------------------------------------------------------------------


def rete_pensiero(c: Contesto) -> Intervento | None:
    """Il watchdog ha chiuso lo stream a meta' ragionamento."""
    if not c.watchdog:
        return None
    if not c.stato.servizio_disponibile:
        return Intervento(
            rete="pensiero", errore_prima="Ragionamento interrotto: budget dei recuperi esaurito.",
            registra="se_detto", turno_assistente=True, servizio=False, chiudi="reasoning_budget",
        )
    # Il ragionamento interrotto **non** finisce in cronologia: rimetterlo nel
    # contesto significherebbe pagarlo a ogni passo per rileggere proprio il
    # giro di pensieri che si sta cercando di spezzare.
    return Intervento(
        rete="pensiero", conteggio="think_watchdog",
        sollecito=THINK_WATCHDOG_NUDGE.format(tokens=c.pensiero_token),
        registra="mai", turno_assistente=False,
        errore_dopo=(
            f"Ragionamento interrotto a ~{c.pensiero_token} token senza azione: "
            "l'agente e' stato riportato sul piano."
        ),
    )


def rete_troncato(c: Contesto) -> Intervento | None:
    if (
        c.done_reason == "length"
        and not c.risposta
        and c.stato.scattata("troncato") < MAX_TRUNCATED_NUDGES
        and c.c_e_ancora_un_passo
        and c.stato.servizio_disponibile
    ):
        return Intervento(
            rete="troncato", conteggio="truncated",
            sollecito=TRUNCATED_NUDGE.format(tokens=c.max_tokens),
            registra="mai", turno_assistente=False,
            errore_dopo=(
                "Il modello ha esaurito i token generabili mentre ragionava, "
                "senza arrivare a un'azione."
            ),
        )
    return None


def rete_canale(c: Contesto) -> Intervento | None:
    """La chiamata e' finita nel testo, o il modello ha scritto un risultato di tool.

    Il recupero dal testo e' spento di serie (scelta di sicurezza del 6/09: un
    esempio mostrato non deve eseguire niente), quindi qui non si esegue: si
    dice la cosa vera. Prima il JSON passava per una risposta -- dal secondo
    passo in poi il turno si chiudeva ``completed`` col file intatto.
    """
    if not (c.chiamate_nel_testo or c.risposta_inventata):
        return None
    esauriti = (
        c.stato.scattata("canale") >= MAX_CANALE
        or not c.stato.servizio_disponibile
        or not c.c_e_ancora_un_passo
    )
    if esauriti:
        return Intervento(
            rete="canale", conteggio="canale_esaurito", registra="se_detto",
            turno_assistente=True, servizio=False, chiudi="error",
            errore_dopo=(
                "Il modello continua a scrivere le chiamate come testo invece di usare il "
                "function calling nativo: nessuna e' stata eseguita. Con Ollama succede "
                "quando il template del modello non emette tool call; con llama-server "
                "controlla --jinja."
            ),
        )
    if c.chiamate_nel_testo:
        testo = CANALE_NUDGE.format(tool=", ".join(f"`{n}`" for n in c.chiamate_nel_testo[:3]))
    else:
        testo = TOOL_RESPONSE_NUDGE
    return Intervento(rete="canale", conteggio="canale", sollecito=testo,
                      registra="se_detto", turno_assistente=True)


def rete_verifica(c: Contesto) -> Intervento | None:
    rossa = c.rossa
    if (
        rossa
        and c.stato.scattata("verifica") < MAX_VERIFY_NUDGES
        and c.c_e_ancora_un_passo
        and c.stato.servizio_disponibile
        and not c.stato.uscita_concessa
    ):
        comando, tentativi, codice = rossa
        loop = tentativi >= LOOP_THRESHOLD
        return Intervento(
            rete="verifica", conteggio="loop" if loop else "verify",
            sollecito=(LOOP_NUDGE if loop else VERIFY_NUDGE).format(
                command=comando, code=codice, count=tentativi
            ),
            registra="sempre", turno_assistente=True,
        )
    return None


def _aperti(piano: Any) -> list[Any]:
    if not piano:
        return []
    return list(getattr(piano, "open_steps", []) or [])


def rete_piano_aperto(c: Contesto) -> Intervento | None:
    """Risposta finale con il piano ancora aperto: la vittoria dichiarata troppo presto.

    E' il guasto che Anthropic descrive per i compiti lunghi ("una sessione
    vede che del lavoro e' stato fatto e dichiara finito"), ed e' curato con
    lo stesso rimedio: uno stato leggibile a macchina che conta alla chiusura.
    Il piano c'e' gia'; mancava che pesasse.
    """
    aperti = _aperti(c.piano)
    if not (
        aperti
        and c.risposta
        and c.enable_nudge
        and c.stato.scattata("piano_aperto") < 1
        and c.c_e_ancora_un_passo
        and c.stato.servizio_disponibile
        and not c.stato.uscita_concessa
        and not looks_like_clarifying_question(c.risposta)
    ):
        return None
    corrente = getattr(c.piano, "current", None) or aperti[0]
    elenco = "; ".join(f"{p.id}. {p.text}" for p in aperti[:4])
    return Intervento(
        rete="piano_aperto", conteggio="piano_aperto",
        sollecito=PIANO_APERTO_NUDGE.format(quanti=len(aperti), elenco=elenco, punto=corrente.id),
        registra="se_detto", turno_assistente=True,
    )


def rete_copertura(c: Contesto) -> Intervento | None:
    if not (
        c.enable_nudge
        and not c.stato.scattata("copertura")
        and not c.rossa
        and c.c_e_ancora_un_passo
        and not c.stato.uscita_concessa
    ):
        return None
    scoperti = uncovered_symbols(c.tool_ctx)
    if not scoperti or not c.stato.servizio_disponibile:
        return None
    elenco = ", ".join(f"{n} (in {f})" for n, f in scoperti[:5])
    return Intervento(rete="copertura", conteggio="coverage",
                      sollecito=COVERAGE_NUDGE.format(symbols=elenco),
                      registra="se_detto", turno_assistente=True)


def rete_riepilogo(c: Contesto) -> Intervento | None:
    """Tool usati e nessuna parola. Esente dal budget: e' la parola di chiusura."""
    if not (
        c.require_summary
        and not c.stato.scattata("riepilogo")
        and c.stato.tools_used
        and not c.risposta
        and c.c_e_ancora_un_passo
    ):
        return None
    rossa = c.rossa
    if rossa:
        righe = "\n".join(
            f"  - `{v.comando}` (exit {v.returncode}, "
            f"{v.tentativi} tentativ{'o' if v.tentativi == 1 else 'i'})"
            for v in c.verifiche.pendenti
        ) or f"  - `{rossa[0]}` (exit {rossa[2]})"
        testo = FAILED_SUMMARY_NUDGE.format(verifiche=righe)
    elif c.piano:
        testo = PLAN_SUMMARY_NUDGE.format(summary=render_summary(c.piano))
    else:
        testo = SUMMARY_NUDGE
    return Intervento(rete="riepilogo", conteggio="summary_failed" if rossa else "summary",
                      sollecito=testo, registra="se_detto", turno_assistente=False,
                      servizio=False)


def rete_spinta(c: Contesto) -> Intervento | None:
    """Una domanda fatta a parole, o un'azione promessa e non eseguita."""
    spingere = looks_like_unexecuted_action(c.risposta) or (
        c.passo == 1 and user_expects_tool_use(c.richiesta)
    )
    disponibile = (
        c.enable_nudge and not c.stato.scattata("spinta") and c.c_e_ancora_un_passo
        and c.stato.servizio_disponibile
    )
    if spingere and looks_like_clarifying_question(c.risposta):
        if disponibile:
            return Intervento(rete="spinta", conteggio="ask", sollecito=ASK_NUDGE,
                              registra="se_detto", turno_assistente=True)
        return None
    if spingere and disponibile:
        return Intervento(rete="spinta", conteggio="tool", sollecito=TOOL_NUDGE,
                          registra="se_detto", turno_assistente=True)
    return None


def rete_senza_prova(c: Contesto) -> Intervento | None:
    """"Fatto" su una richiesta di modifica, senza nessuna scrittura riuscita."""
    if not (
        c.enable_nudge
        and c.risposta
        and not c.stato.scattata("senza_prova")
        and c.c_e_ancora_un_passo
        and c.stato.servizio_disponibile
        and not c.stato.uscita_concessa
        and c.stato.scritture_riuscite == 0
        and not getattr(c.tool_ctx, "readonly_request", False)
        and chiede_modifiche(c.richiesta)
        and dichiara_completamento(c.risposta)
        and not looks_like_clarifying_question(c.risposta)
    ):
        return None
    return Intervento(rete="senza_prova", conteggio="senza_prova", sollecito=SENZA_PROVA_NUDGE,
                      registra="se_detto", turno_assistente=True)


RETI_SENZA_TOOL: tuple[Callable[[Contesto], Intervento | None], ...] = (
    rete_pensiero,
    rete_troncato,
    rete_canale,
    rete_verifica,
    rete_piano_aperto,
    rete_copertura,
    rete_riepilogo,
    rete_spinta,
    rete_senza_prova,
)


def decidi_senza_tool(c: Contesto, reti: tuple[Callable[[Contesto], Intervento | None], ...]
                      = RETI_SENZA_TOOL) -> Intervento | None:
    """La prima rete che si applica, o ``None``: la risposta e' accettata."""
    for rete in reti:
        intervento = rete(c)
        if intervento is not None:
            return intervento
    return None


# ---------------------------------------------------------------------------
# Famiglia "dopo i tool": note accodate ai risultati, senza costare un passo
# ---------------------------------------------------------------------------

# Quante letture di fila prima di ricordare che esiste `esplora`.
MAX_ESPLORAZIONI_SENZA_DELEGA = 5


@dataclass(frozen=True, slots=True)
class Nota:
    conteggio: str
    testo: str


@dataclass
class ContestoDopo:
    stato: StatoTurno
    passo: int
    max_passi: int
    recuperate: bool
    tool_ctx: Any
    require_plan: bool = True
    enable_nudge: bool = True
    abilita_delega: bool = True
    monitor: bool = True
    esempio_delega: Callable[[], str] = lambda: ""


def note_dopo_tool(c: ContestoDopo) -> list[Nota]:
    """Le note da accodare dopo i risultati, nell'ordine in cui vanno lette."""
    st = c.stato
    note: list[Nota] = []
    ancora = c.passo < c.max_passi
    if c.recuperate and not st.scattata("json_leak"):
        st.segna("json_leak")
        note.append(Nota("json_leak", JSON_LEAK_NUDGE))
    if st.ripetizione_dovuta is not None:
        nome_tool, quante = st.ripetizione_dovuta
        st.ripetizione_dovuta = None
        note.append(Nota("ripetizione", RIPETIZIONE_NUDGE.format(tool=nome_tool, quante=quante)))
    if st.stallo_dovuto is not None:
        nome_tool, quante = st.stallo_dovuto
        st.stallo_dovuto = None
        note.append(Nota("stallo", STALLO_NUDGE.format(tool=nome_tool, quante=quante)))
    if st.oscillazione_dovuta is not None:
        nome_file, quante = st.oscillazione_dovuta
        st.oscillazione_dovuta = None
        note.append(Nota("oscillazione", OSCILLAZIONE_NUDGE.format(file=nome_file, quante=quante)))
    if (
        c.require_plan and c.enable_nudge and st.multi_step and not st.scattata("piano")
        and not c.tool_ctx.plan and ancora
    ):
        st.segna("piano")
        note.append(Nota("plan", PLAN_NUDGE))
    if (
        c.enable_nudge and c.abilita_delega and not st.scattata("delega")
        and st.esplorazioni_di_fila >= MAX_ESPLORAZIONI_SENZA_DELEGA and ancora
    ):
        st.segna("delega")
        note.append(Nota("delega", DELEGA_NUDGE.format(quante=st.esplorazioni_di_fila)
                         + c.esempio_delega()))
    if c.monitor and st.avanzamento.da_riorientare and ancora:
        st.avanzamento.riorientato = True
        st.uscita_concessa = True
        ultime = "; ".join(st.avanzamento.ultime[-4:]) or "nessuna"
        note.append(Nota("riorienta", RIORIENTA_NUDGE.format(
            passi=st.avanzamento.senza_progresso, ultime=ultime)))
    return note


def e_una_possibile_scrittura(nome: str, args: dict[str, Any], ok: bool) -> bool:
    """La chiamata puo' aver cambiato il disco? Serve a ``senza_prova``.

    ``write_file``/``edit_file`` riusciti si', ovviamente. Un ``run_command``
    riuscito che non e' una verifica si': un ``sed -i`` o uno script che scrive
    sono modifiche vere, e contarle zero farebbe sollecitare un lavoro fatto.
    """
    if not ok:
        return False
    if nome in ("write_file", "edit_file"):
        return True
    if nome == "run_command":
        comando = str(args.get("command") or "")
        primo = comando.strip().split(" ", 1)[0].rsplit("/", 1)[-1] if comando.strip() else ""
        muti = {"ls", "dir", "cat", "head", "tail", "echo", "pwd", "which", "find", "grep", "rg",
                "wc", "tree", "git"}
        return not looks_like_verification(comando) and primo not in muti
    return False
