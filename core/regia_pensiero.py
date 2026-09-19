"""Regia del pensiero: quanto ragionare in un passo, e come smettere.

Da dove viene
-------------
Misura del 23/08/2026 su quattro sessioni ``qwen3.8:27b``: 22 caratteri pensati
per ogni carattere scritto, il pensiero che **non** si accorcia col passo, e che
va al contrario del lavoro -- ``edit_file`` 5.253 caratteri di mediana,
``manage_plan`` 693. Il problema non era ripetere, era **deliberare**: i
"wait / actually / hmm" sono zero sotto i 2.000 caratteri e fino a 81 sopra i
15.000.

Tre fatti del codice decidono la forma di questo modulo:

1. **Su Qwen i livelli non sono una manopola.** Il template conosce solo
   ``enable_thinking``: sul transport OpenAI-compatibile il livello diventa
   ``bool(think)``, e su Ollama un ``"medium"`` a un modello senza scala e'
   nel migliore dei casi un "acceso". L'unica manopola vera, per un modello
   cosi', e' un **budget in token** fatto rispettare dall'harness sullo stream.
   I livelli restano modulati per i modelli che li capiscono.

2. **Il livello si sceglie prima della generazione** (regola del progetto):
   quindi puo' dipendere solo da cio' che e' gia' successo. Il tipo del punto
   aperto e' noto prima -- l'ha scritto il modello quando ha fatto il piano --
   e i fallimenti del passo precedente pure.

3. **Interrompere e ricominciare non cambia niente** (46 interruzioni del
   watchdog, zero effetti): il pensiero buttato si ripaga da capo. Qui la
   chiusura *continua* la stessa generazione: al pensiero parziale si accoda
   una frase che chiude, e il modello riparte da li' con la sola azione. Vedi
   ``agent.run_turn`` e ``OllamaBackend.supports_think_continuation``.

Questo modulo non parla con il backend e non conosce la UI: sono funzioni pure
e un osservatore dello stream, per poterli provare senza un modello.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Tipi di punto
# ---------------------------------------------------------------------------

ESEGUI = "esegui"
INDAGA = "indaga"
DIAGNOSI = "diagnosi"
PROGETTA = "progetta"
TIPI = (ESEGUI, INDAGA, DIAGNOSI, PROGETTA)

# Le parole con cui il modello scrive davvero il tipo. Qwen pensa in inglese
# (344 blocchi su 350) e il piano lo scrive nella lingua della richiesta:
# accettare entrambe costa una riga, rifiutare costerebbe un round-trip.
_SINONIMI = {
    ESEGUI: ("esegui", "esecuzione", "eseguire", "execute", "exec", "fare", "implementa", "implement"),
    INDAGA: ("indaga", "indagine", "ricerca", "ricerca", "analisi", "esplora", "research", "explore", "investigate"),
    DIAGNOSI: ("diagnosi", "debug", "debugging", "diagnose", "correggi-bug"),
    PROGETTA: ("progetta", "progettazione", "progetto", "pianifica", "design", "plan"),
}
_PER_PAROLA = {parola: tipo for tipo, parole in _SINONIMI.items() for parola in parole}

# "esegui: testo", "[esegui] testo", "(esegui) testo", "esegui - testo".
_PREFISSO = re.compile(
    r"^\s*(?:\[\s*(?P<a>[a-z\-]+)\s*\]|\(\s*(?P<b>[a-z\-]+)\s*\)|(?P<c>[a-z\-]+)\s*[:—\-]\s+)\s*",
    re.I,
)


def normalizza_tipo(valore: Any) -> str | None:
    """Il tipo canonico, o ``None`` se la parola non ne e' uno."""
    parola = str(valore or "").strip().lower()
    return _PER_PAROLA.get(parola)


def separa_tipo(testo: str) -> tuple[str | None, str]:
    """``"diagnosi: capire perche' il test fallisce"`` -> ``("diagnosi", "capire...")``.

    Un prefisso che non e' un tipo **resta nel testo**: "Nota: ..." o
    "README - sezione install" sono testi, non etichette sbagliate.
    """
    grezzo = str(testo or "")
    m = _PREFISSO.match(grezzo)
    if not m:
        return None, grezzo.strip()
    tipo = normalizza_tipo(m.group("a") or m.group("b") or m.group("c"))
    if tipo is None:
        return None, grezzo.strip()
    return tipo, grezzo[m.end():].strip()


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

# Fasi, come le chiama gia' ``agent.run_turn``: il primo passo di un punto
# decide, i successivi eseguono, un passo dopo un fallimento recupera.
DECISIONE = "decision"
ESECUZIONE = "execution"
RECUPERO = "recovery"

# Token di pensiero per (tipo, fase). ``None`` = punto senza tipo: restano i
# tetti storici del watchdog, cosi' chi non tipizza non vede cambiare niente.
#
# I numeri partono dalle misure del 23/08: la mediana di un blocco e' circa
# 850 token (3.394 caratteri), il 10% piu' lungo vale il 40% del pensiero. Il
# budget di ``esegui`` non taglia il passo normale, taglia la coda. Quelli di
# ``diagnosi`` e ``progetta`` sono **piu' larghi** dei tetti di prima: e' li'
# che pensare serve davvero.
#
# Valgono solo quando la chiusura puo' *continuare* la generazione. Se il
# backend non lo sa fare, l'unico taglio disponibile butta il pensiero, e
# allora non si scende mai sotto i tetti storici: abbassare una soglia il cui
# prezzo e' ripagare tutto sarebbe peggiorare, non regolare.
BUDGET_TOKEN: dict[str | None, dict[str, int]] = {
    ESEGUI:   {DECISIONE: 1500, ESECUZIONE: 1000, RECUPERO: 3000},
    INDAGA:   {DECISIONE: 2500, ESECUZIONE: 1500, RECUPERO: 3000},
    DIAGNOSI: {DECISIONE: 6000, ESECUZIONE: 4000, RECUPERO: 6000},
    PROGETTA: {DECISIONE: 6000, ESECUZIONE: 3000, RECUPERO: 6000},
    None:     {DECISIONE: 5000, ESECUZIONE: 2000, RECUPERO: 5000},
}
# Il passo che segue una serie di sole letture in un punto ``indaga``: e' il
# momento in cui con buona probabilita' si tirano le somme, e la sintesi e' la
# parte dell'indagine che merita pensiero -- non la scelta del prossimo file.
BUDGET_SINTESI = 3000
LETTURE_PER_SINTESI = 4

# Fallimenti nello stesso punto oltre i quali il punto si tratta come una
# diagnosi, qualunque tipo gli abbia dato il modello.
FALLIMENTI_PER_DIAGNOSI = 2

TOOL_DI_LETTURA = frozenset({"read_file", "search_files", "list_files", "esplora", "web_search", "web_fetch"})
TOOL_DI_SCRITTURA = frozenset({"write_file", "edit_file"})


def budget_token(tipo: str | None, fase: str) -> int:
    tabella = BUDGET_TOKEN.get(tipo) or BUDGET_TOKEN[None]
    return tabella.get(fase, tabella[ESECUZIONE])


@dataclass
class StatoPunto:
    """Cosa e' successo finora nel punto (o nel turno, senza piano).

    Si azzera quando cambia il punto: i fallimenti di un punto chiuso non
    devono far pensare a lungo quello dopo.
    """

    chiave: Any = None
    fallimenti: int = 0
    letture_di_fila: int = 0
    passi: int = 0
    sintesi_suggerita: bool = False

    def nuovo_punto(self, chiave: Any) -> None:
        if chiave != self.chiave:
            self.chiave = chiave
            self.fallimenti = 0
            self.letture_di_fila = 0
            self.passi = 0
            self.sintesi_suggerita = False

    def registra_passo(self, chiamate: Sequence[tuple[str, bool]]) -> None:
        """I tool del passo appena chiuso, con il loro esito."""
        self.passi += 1
        nomi = [n for n, _ in chiamate if n not in ("manage_plan", "manage_notes")]
        if any(not ok for _, ok in chiamate):
            self.fallimenti += 1
        if nomi and all(n in TOOL_DI_LETTURA for n in nomi):
            self.letture_di_fila += 1
        elif nomi:
            self.letture_di_fila = 0


@dataclass
class Decisione:
    """Cosa si e' deciso per il pensiero di questo passo, e perche'."""

    tipo: str | None            # quello scritto dal modello
    tipo_effettivo: str | None  # dopo le correzioni dell'harness
    fase: str
    budget: int                 # token
    motivo: str = ""
    sintesi: bool = False
    note: list[str] = field(default_factory=list)


def decidi(
    tipo_punto: str | None,
    fase: str,
    stato: StatoPunto,
    *,
    ha_piano: bool,
    passo_nel_turno: int,
    verifica_rossa: bool,
    ultimo_passo: Sequence[tuple[str, bool]] = (),
) -> Decisione:
    """Il budget di pensiero del passo, **solo** da cio' che e' gia' accaduto.

    Il modello dice che tipo di lavoro e' un punto; l'harness corregge
    l'etichetta con i fatti, perche' un modello tende a dichiarare
    "esecutivo" un punto che poi fallisce tre volte:

    * troppi fallimenti nel punto, o una verifica rossa appena prodotta:
      il punto si tratta come ``diagnosi``;
    * senza piano, il tipo si ricava dal passo precedente: sole letture ->
      ``indaga``, un fallimento -> ``diagnosi``, una scrittura riuscita ->
      ``esegui``. Il primo passo del turno resta senza tipo: e' quello che
      legge la richiesta.
    """
    tipo_eff = tipo_punto
    motivo = "tipo del punto" if tipo_punto else "nessun tipo"

    if not ha_piano and passo_nel_turno > 1 and ultimo_passo:
        nomi = [n for n, _ in ultimo_passo if n not in ("manage_plan", "manage_notes")]
        if any(not ok for _, ok in ultimo_passo):
            tipo_eff, motivo = DIAGNOSI, "senza piano: il passo precedente e' fallito"
        elif nomi and all(n in TOOL_DI_LETTURA for n in nomi):
            tipo_eff, motivo = INDAGA, "senza piano: il passo precedente ha solo letto"
        elif any(n in TOOL_DI_SCRITTURA for n in nomi):
            tipo_eff, motivo = ESEGUI, "senza piano: il passo precedente ha scritto"

    if stato.fallimenti >= FALLIMENTI_PER_DIAGNOSI and tipo_eff != DIAGNOSI:
        tipo_eff, motivo = DIAGNOSI, f"{stato.fallimenti} fallimenti nel punto"
    elif fase == RECUPERO and verifica_rossa and tipo_eff in (ESEGUI, INDAGA, None):
        tipo_eff, motivo = DIAGNOSI, "verifica rossa appena prodotta"

    budget = budget_token(tipo_eff, fase)
    sintesi = False
    if (
        tipo_eff == INDAGA
        and fase != RECUPERO
        and stato.letture_di_fila >= LETTURE_PER_SINTESI
    ):
        budget = max(budget, BUDGET_SINTESI)
        sintesi = True
        motivo = f"{stato.letture_di_fila} passi di sole letture: tempo di sintesi"

    return Decisione(
        tipo=tipo_punto, tipo_effettivo=tipo_eff, fase=fase,
        budget=budget, motivo=motivo, sintesi=sintesi,
    )


def scalini_livello(tipo: str | None, passo_nel_punto: int, deep_step: int) -> int:
    """Di quanti livelli scendere, per i modelli che i livelli li capiscono.

    ``diagnosi`` e ``progetta`` non scendono mai piu' di uno: pensare e' il
    lavoro. ``esegui`` scende di uno anche al primo passo: la decisione di
    un punto esecutivo e' gia' nel suo testo. Gli altri come prima.
    """
    if tipo in (DIAGNOSI, PROGETTA):
        return 0 if passo_nel_punto <= 1 else 1
    if tipo == ESEGUI:
        return 1 if passo_nel_punto <= 1 else 2
    if passo_nel_punto <= 1:
        return 0
    return 2 if passo_nel_punto >= deep_step else 1


# ---------------------------------------------------------------------------
# Osservatore dello stream
# ---------------------------------------------------------------------------

# Frasi di ripensamento. Il 10,1% dei caratteri pensati stava in queste.
_RIPENSAMENTO = re.compile(
    r"\b(?:wait|actually|hmm+|hold on|on second thought|but then|alternatively|"
    r"aspetta|anzi|in realt[aà]|oppure no|ripensandoci)\b",
    re.I,
)
OSCILLAZIONE_MIN_CARATTERI = 2000
OSCILLAZIONE_MIN_RIPENSAMENTI = 6
# Un ripensamento ogni tanti caratteri, o piu' fitto: e' la densita' dei blocchi
# lunghi misurati (81 in circa 50.000 caratteri sta sotto; 37 blocchi sopra i
# 15.000 con mediana alta stanno sopra). Oltre, il budget si dimezza.
OSCILLAZIONE_CARATTERI_PER_RIPENSAMENTO = 450

# Una decisione scritta nel pensiero: un verbo di intenzione e il nome di un
# tool a poca distanza. Dopo ``GRAZIA_DECISIONE`` caratteri senza che il pensiero
# si chiuda da solo, lo si chiude: la mossa e' decisa, il resto e' ricontrollo.
_INTENZIONE = (
    r"(?:i'll|i will|let me|let's|i need to|i should|now i|next,? i|so i|"
    r"devo|uso|chiamo|procedo|faccio|user[oò])"
)
GRAZIA_DECISIONE = 700
DECISIONE_MIN_CARATTERI = 600


def _pattern_decisione(nomi_tool: Iterable[str]) -> re.Pattern[str] | None:
    nomi = sorted({n for n in nomi_tool if n}, key=len, reverse=True)
    if not nomi:
        return None
    alternativa = "|".join(re.escape(n) for n in nomi)
    return re.compile(
        rf"\b{_INTENZIONE}\b[^.\n]{{0,80}}?\b(?:{alternativa})\b", re.I
    )


class OsservatorePensiero:
    """Legge il pensiero mentre arriva e dice quando chiuderlo.

    Tre ragioni di chiusura, in quest'ordine:

    * ``budget``: il pensiero ha superato il budget del passo;
    * ``oscillazione``: tanti ripensamenti fitti -- il budget effettivo
      si dimezza, perche' un pensiero che gira in tondo non converge
      spendendo di piu';
    * ``decisione``: nel pensiero c'e' gia' "uso read_file su ...", e sono
      passati ``GRAZIA_DECISIONE`` caratteri senza che si chiudesse.

    ``decisione`` non scatta su ``diagnosi`` e ``progetta``: li' scrivere
    "I'll run the test" e poi continuare a ragionare e' il lavoro, non un
    ricontrollo.
    """

    def __init__(
        self,
        soglia_caratteri: int,
        *,
        tipo: str | None = None,
        nomi_tool: Iterable[str] = (),
        rileva_decisione: bool = True,
    ) -> None:
        self.soglia = max(0, int(soglia_caratteri))
        self.tipo = tipo
        self.ripensamenti = 0
        self.oscillazione = False
        self.decisione_a: int | None = None
        self._letto = 0
        self._pattern = (
            _pattern_decisione(nomi_tool)
            if rileva_decisione and tipo not in (DIAGNOSI, PROGETTA)
            else None
        )

    def soglia_effettiva(self) -> int:
        if self.oscillazione and self.soglia:
            return max(OSCILLAZIONE_MIN_CARATTERI, self.soglia // 2)
        return self.soglia

    def aggiorna(self, pensiero: str) -> str | None:
        """Ritorna la ragione per chiudere, o ``None`` per lasciar pensare."""
        if not self.soglia:
            return None
        n = len(pensiero)
        if n <= self._letto:
            return None
        # Si rilegge una coda sovrapposta: un "actually" spezzato fra due
        # frammenti dello stream non deve sparire.
        inizio = max(0, self._letto - 40)
        nuovo = pensiero[inizio:]
        gia_contati = len(_RIPENSAMENTO.findall(pensiero[inizio:self._letto]))
        self.ripensamenti += len(_RIPENSAMENTO.findall(nuovo)) - gia_contati
        if self._pattern is not None and self.decisione_a is None and n >= DECISIONE_MIN_CARATTERI:
            finestra = pensiero[max(0, self._letto - 120):]
            m = self._pattern.search(finestra)
            if m:
                self.decisione_a = max(0, self._letto - 120) + m.end()
        self._letto = n

        if (
            not self.oscillazione
            and n >= OSCILLAZIONE_MIN_CARATTERI
            and self.ripensamenti >= OSCILLAZIONE_MIN_RIPENSAMENTI
            and n / max(1, self.ripensamenti) <= OSCILLAZIONE_CARATTERI_PER_RIPENSAMENTO
        ):
            self.oscillazione = True

        if n > self.soglia_effettiva():
            return "oscillazione" if self.oscillazione and n <= self.soglia else "budget"
        if self.decisione_a is not None and n - self.decisione_a >= GRAZIA_DECISIONE:
            return "decisione"
        return None


# Le frasi che chiudono il pensiero prima della continuazione. In inglese
# perche' e' la lingua in cui Qwen pensa: una frase italiana in mezzo a un
# ragionamento inglese e' un cambio di registro che il modello tende a
# commentare, cioe' altro pensiero.
CHIUSURE = {
    "budget": (
        "\n\nI have spent my reasoning budget for this step. I already know enough "
        "to make the next concrete move, so I stop deliberating and act now.\n"
    ),
    "oscillazione": (
        "\n\nI keep going back and forth. Re-checking will not settle it: I pick the "
        "most reasonable option and let the tool result tell me if it was wrong.\n"
    ),
    "decisione": (
        "\n\nThe next move is decided. No more re-checking: I act on it now.\n"
    ),
}


def chiusura(ragione: str) -> str:
    return CHIUSURE.get(ragione, CHIUSURE["budget"])


# Quanto pensiero *nuovo* si tollera nella continuazione prima di concludere
# che il server non ha rispettato il pensiero precompilato e il modello ha
# ricominciato a pensare da capo.
CONTINUAZIONE_MAX_PENSIERO = 1200
# Token minimi per la continuazione: una tool call con argomenti veri.
CONTINUAZIONE_MIN_TOKEN = 768


# ---------------------------------------------------------------------------
# Istruzioni per punto
# ---------------------------------------------------------------------------

ISTRUZIONI_TIPO = {
    ESEGUI: (
        "Punto esecutivo: la decisione e' gia' nel testo del punto. Pensa il "
        "minimo per scegliere gli argomenti giusti e agisci."
    ),
    INDAGA: (
        "Punto di indagine: tra una lettura e l'altra non serve ragionare a "
        "lungo, serve leggere. Il pensiero lungo tienilo per la sintesi."
    ),
    DIAGNOSI: (
        "Punto di diagnosi: prima di ogni tentativo scrivi un'ipotesi con "
        "manage_plan action='ipotesi' (note='ipotesi -> prova -> esito'), e "
        "aggiorna l'esito quando arriva. Non riprovare una prova gia' scritta "
        "nel registro qui sotto."
    ),
    PROGETTA: (
        "Punto di progettazione: decidi l'approccio una volta sola. Quando "
        "l'hai scelto, chiudi il punto con una nota di una riga e aggiungi "
        "con action='add' i punti esecutivi che ne derivano, ognuno col suo "
        "tipo ('esegui: ...'). L'esecuzione poi va a pensiero basso."
    ),
}

SUGGERIMENTO_SINTESI = (
    "Hai letto per {n} passi di fila in questo punto. Se hai abbastanza, "
    "tira le somme e chiudi il punto con una nota che dica cosa hai capito."
)
