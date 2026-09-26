"""Il cruscotto: quanto va il modello, adesso e passo per passo.

Prima la colonna di destra sapeva una cosa sola sulla velocita': un numero a
fine turno, calcolato su tutto il turno, che arrivava quando non serviva piu'.
Durante un passo da due minuti non c'era modo di sapere se il modello stava
generando a 30 token al secondo o se era fermo nel prefill di un contesto da
50k, ne' se il draft dello speculative decoding stava rendendo.

Qui ci sono tre pezzi, e nessuno sa niente dell'interfaccia:

* ``Tachimetro`` guarda lo stream di **un passo** e produce ``Metriche``: la
  velocita' adesso e media, la fase (attesa, prefill, pensiero, risposta,
  chiamata), il tempo al primo token, il prefill, la cache, il draft. Al piu'
  ``1 / INTERVALLO_S`` volte al secondo, e una volta **definitiva** a fine
  passo con i numeri che il server dichiara.
* ``Registro`` raccoglie i passi di un turno in righe compatte -- le stesse
  che l'interfaccia costruisce dal vivo -- e il server le salva con la
  telemetria del turno.
* ``storico`` ricava dalla telemetria salvata cio' che serve a chi riapre una
  conversazione: la timeline dell'ultimo turno, i punti velocita'/contesto di
  tutti i passi, un riassunto per turno. Funziona anche sui turni registrati
  prima che esistesse il ``Registro``: li ricostruisce dalle chiamate.

Da dove vengono i numeri, in ordine di fiducia:

1. i ``timings`` di llama.cpp, token per token (``StreamEvent("battito")``):
   esatti anche dal vivo, e non risentono della rete;
2. i contatori di fine generazione (``eval_count``/``eval_duration`` di
   Ollama, ``usage`` dello standard OpenAI): esatti, ma solo alla fine;
3. il conteggio dei pezzi dello stream sull'orologio dell'harness: Ollama e
   llama.cpp spediscono un token per pezzo, gli altri si stimano dai
   caratteri. E' una stima, e ``fonte`` lo dice.
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .textutils import _CHARS_PER_TOKEN

# Aggiornamenti al secondo, al massimo: quattro bastano a un numero che
# l'occhio legge, e ogni evento in piu' e' un frame da serializzare, spedire e
# ridisegnare. Il rubinetto del testo (``agent.STREAM_INTERVALLO_S``) va a
# dieci perche' li' si legge un flusso; qui si legge una cifra.
INTERVALLO_S = 0.25
# La velocita' "adesso" e' misurata sugli ultimi 1,5 s di generazione: piu'
# corto, lo speculative decoding (token che arrivano a grappoli) la farebbe
# ballare; piu' lungo, un rallentamento si vedrebbe in ritardo.
FINESTRA_MS = 1500.0
# Sotto questa distanza fra il primo e l'ultimo campione la velocita' non si
# dichiara: due token a 10 ms di distanza non sono "100 tok/s".
MIN_FINESTRA_MS = 250.0

FASI = ("attesa", "prefill", "pensiero", "risposta", "chiamata", "fine")


@dataclass(slots=True)
class Metriche:
    """Cosa sta facendo il modello in questo passo, e a che velocita'.

    ``definitivo`` e' vero una volta sola, a fine passo: e' il frame che resta
    nell'arretrato del turno (chi si riattacca ricostruisce la timeline da
    questi), mentre quelli dal vivo sono volatili -- il server tiene solo
    l'ultimo. ``None`` vuol dire "non si sa", mai zero: un numero inventato
    in un cruscotto e' peggio di un trattino.
    """

    passo: int
    fase: str = "attesa"
    definitivo: bool = False
    # "server": token e tempi dai contatori del server; "stima": dai pezzi
    # dello stream sull'orologio dell'harness.
    fonte: str = "stima"
    durata_ms: int = 0              # dall'invio della richiesta
    attesa_ms: int | None = None    # fino al primo token (prefill compreso)
    generazione_ms: int | None = None
    generati: int = 0               # token generati nel passo
    # Ripartizione dei token per canale. Contati sui pezzi dello stream: la
    # somma puo' scostarsi un poco da ``generati``, la proporzione no.
    pensiero: int = 0
    risposta: int = 0
    chiamate: int = 0
    tok_s: float | None = None      # adesso (ultimi FINESTRA_MS)
    tok_s_passo: float | None = None
    picco: float | None = None
    prompt: int | None = None       # token del prompt, se il server li dichiara interi
    prompt_stimato: int = 0
    cache: int | None = None        # token del prompt presi dalla cache
    prefill_token: int | None = None  # token del prompt davvero calcolati
    prefill_ms: int | None = None
    prefill_fatti: int | None = None  # progresso del prefill (llama.cpp)
    prefill_totale: int | None = None
    draft_n: int | None = None
    draft_accettati: int | None = None
    finestra: int = 0
    # Quando e' cominciato il turno, in secondi epoch del server. Serve a chi
    # si riattacca a meta': l'arretrato arriva tutto insieme, e l'orologio
    # del browser non puo' sapere da quanto il turno gira.
    turno_inizio: float | None = None


def _arrotonda(valore: float | None, cifre: int = 1) -> float | None:
    if valore is None or not math.isfinite(valore):
        return None
    return round(valore, cifre)


class Tachimetro:
    """Le misure di un passo, dai pezzi dello stream.

    Un passo puo' fare piu' richieste al server -- la continuazione del
    pensiero (``regia_pensiero``) ne apre una seconda -- e i contatori del
    server ripartono da zero a ogni richiesta: ``nuova_richiesta`` mette da
    parte quelli della precedente, cosi' il passo resta uno.
    """

    def __init__(
        self,
        passo: int,
        *,
        prompt_stimato: int = 0,
        finestra: int = 0,
        un_token_per_chunk: bool = False,
        prompt_intero: bool = False,
        turno_inizio: float | None = None,
        orologio: Callable[[], float] = time.monotonic,
    ) -> None:
        self.passo = int(passo)
        self.turno_inizio = turno_inizio
        self.prompt_stimato = max(0, int(prompt_stimato or 0))
        self.finestra = max(0, int(finestra or 0))
        self._per_chunk = bool(un_token_per_chunk)
        self._prompt_intero = bool(prompt_intero)
        self._orologio = orologio
        self._t0 = orologio()
        self._t_primo: float | None = None
        self.fase = "attesa"
        # Pezzi dello stream per canale, in token (stimati se serve).
        self._conti = {"pensiero": 0.0, "risposta": 0.0, "chiamate": 0.0}
        # Contatori del server della richiesta in corso, e la somma di quelle
        # gia' chiuse nello stesso passo.
        self._srv: dict[str, Any] = {}
        self._fine: dict[str, Any] = {}
        self._base: dict[str, float] = {"generati": 0.0, "generazione_ms": 0.0,
                                        "draft_n": 0.0, "draft_accettati": 0.0}
        self._campioni: deque[tuple[float, float]] = deque()
        self._ultimo_invio: float | None = None
        self._picco: float | None = None
        self._sporco = True

    # -- ingresso ------------------------------------------------------
    def evento(self, ev: Any) -> None:
        """Un evento dello stream del backend (``StreamEvent``)."""
        kind = getattr(ev, "kind", "")
        if kind in ("reasoning", "content"):
            testo = getattr(ev, "text", "") or ""
            if not testo:
                return
            canale = "pensiero" if kind == "reasoning" else "risposta"
            self._conta(canale, 1.0 if self._per_chunk else len(testo) / _CHARS_PER_TOKEN)
        elif kind == "battito":
            dati = getattr(ev, "usage", None) or {}
            if not isinstance(dati, dict):
                return
            caratteri = dati.get("chiamata")
            if isinstance(caratteri, (int, float)) and caratteri > 0:
                self._conta("chiamate", 1.0 if self._per_chunk else caratteri / _CHARS_PER_TOKEN)
            for chiave in ("generati", "generazione_ms", "prompt_calcolati", "prefill_ms",
                           "cache", "draft_n", "draft_accepted", "prefill_totale",
                           "prefill_cache", "prefill_fatti", "prefill_trascorsi_ms"):
                valore = dati.get(chiave)
                if isinstance(valore, (int, float)) and not isinstance(valore, bool):
                    self._srv[chiave] = valore
            if self._t_primo is None and "prefill_totale" in dati:
                self.fase = "prefill"
            self._campiona()
            self._sporco = True
        elif kind == "usage":
            dati = getattr(ev, "usage", None) or {}
            if isinstance(dati, dict):
                self._fine.update({k: v for k, v in dati.items()
                                   if isinstance(v, (int, float)) and not isinstance(v, bool)})
                self._sporco = True
        elif kind == "tool_call":
            # Su Ollama gli argomenti non arrivano a pezzi: la chiamata compare
            # intera alla fine. Si conta almeno che c'e' stata.
            if self.fase in ("attesa", "prefill"):
                self._primo_token()
            self.fase = "chiamata"
            self._sporco = True

    def nuova_richiesta(self) -> None:
        """La continuazione apre una seconda richiesta nello stesso passo."""
        self._base["generati"] += self._generati_richiesta() or 0
        self._base["generazione_ms"] += self._generazione_ms_richiesta() or 0
        self._base["draft_n"] += self._numero("draft_n") or 0
        self._base["draft_accettati"] += self._numero("draft_accepted") or 0
        self._srv = {}
        self._fine = {}
        self._campioni.clear()

    # -- uscita --------------------------------------------------------
    def aggiorna(self, *, forza: bool = False) -> Metriche | None:
        """Le metriche da spedire adesso, o None se e' troppo presto."""
        adesso = self._orologio()
        if not forza:
            if not self._sporco:
                return None
            if self._ultimo_invio is not None and adesso - self._ultimo_invio < INTERVALLO_S:
                return None
        self._ultimo_invio = adesso
        self._sporco = False
        return self._metriche(adesso)

    def chiudi(self) -> Metriche:
        """Il frame definitivo del passo, coi numeri finali del server."""
        adesso = self._orologio()
        metriche = self._metriche(adesso, finale=True)
        metriche.definitivo = True
        metriche.fase = "fine"
        self.fase = "fine"
        return metriche

    # -- interni -------------------------------------------------------
    def _primo_token(self) -> None:
        if self._t_primo is None:
            self._t_primo = self._orologio()

    def _conta(self, canale: str, token: float) -> None:
        self._primo_token()
        self._conti[canale] += token
        self.fase = canale if canale != "chiamate" else "chiamata"
        self._campiona()
        self._sporco = True

    def _numero(self, *chiavi: str) -> float | None:
        for chiave in chiavi:
            for fonte in (self._fine, self._srv):
                valore = fonte.get(chiave)
                if isinstance(valore, (int, float)) and not isinstance(valore, bool):
                    return float(valore)
        return None

    def _generati_richiesta(self) -> float | None:
        fine = self._fine.get("completion_tokens")
        if isinstance(fine, (int, float)) and fine > 0:
            return float(fine)
        vivo = self._srv.get("generati")
        if isinstance(vivo, (int, float)):
            return float(vivo)
        return None

    def _generazione_ms_richiesta(self) -> float | None:
        fine = self._fine.get("eval_ms")
        if isinstance(fine, (int, float)) and fine > 0:
            return float(fine)
        vivo = self._srv.get("generazione_ms")
        if isinstance(vivo, (int, float)):
            return float(vivo)
        return None

    def _contati(self) -> float:
        return sum(self._conti.values())

    def _campiona(self) -> None:
        """Un punto (millisecondi di generazione, token) per la velocita' adesso.

        Con i ``timings`` l'orologio e' quello del server: il ritmo con cui i
        pezzi attraversano la rete non entra nella misura. Senza, e' quello
        dell'harness dal primo token.
        """
        generati = self._generati_richiesta()
        ms = self._generazione_ms_richiesta()
        if generati is not None and ms is not None:
            punto = (self._base["generazione_ms"] + ms, self._base["generati"] + generati)
        elif self._t_primo is not None:
            punto = ((self._orologio() - self._t_primo) * 1000.0, self._contati())
        else:
            return
        if self._campioni and punto[0] < self._campioni[-1][0]:
            # Orologio che torna indietro: una richiesta nuova non annunciata.
            self._campioni.clear()
        self._campioni.append(punto)
        while self._campioni and self._campioni[0][0] < punto[0] - FINESTRA_MS * 2:
            self._campioni.popleft()

    def _velocita_adesso(self, adesso: float) -> float | None:
        if not self._campioni:
            return None
        ultimo_ms, ultimo_n = self._campioni[-1]
        server = self._generazione_ms_richiesta() is not None
        if not server and self._t_primo is not None and self.fase != "fine":
            # Sull'orologio dell'harness il tempo passa anche senza token: un
            # modello fermo deve vedersi scendere, non restare all'ultimo
            # valore. Col server no -- fra due pezzi il suo orologio e' fermo.
            ultimo_ms = max(ultimo_ms, (adesso - self._t_primo) * 1000.0)
        inizio = ultimo_ms - FINESTRA_MS
        primo = None
        for ms, n in self._campioni:
            if ms >= inizio:
                primo = (ms, n)
                break
        if primo is None:
            # Nessun token nell'ultima finestra: sull'orologio dell'harness
            # vuol dire fermo, e fermo e' zero. Sul server non succede.
            return None if server else 0.0
        durata = ultimo_ms - primo[0]
        if durata < MIN_FINESTRA_MS:
            return None
        return (ultimo_n - primo[1]) / durata * 1000.0

    def _metriche(self, adesso: float, *, finale: bool = False) -> Metriche:
        generati_srv = self._generati_richiesta()
        ms_srv = self._generazione_ms_richiesta()
        fonte = "server" if generati_srv is not None and ms_srv is not None else "stima"
        if fonte == "server":
            generati = self._base["generati"] + (generati_srv or 0)
            generazione_ms: float | None = self._base["generazione_ms"] + (ms_srv or 0)
        else:
            generati = self._contati()
            generazione_ms = (
                (adesso - self._t_primo) * 1000.0 if self._t_primo is not None else None
            )
        tok_s_passo = (
            generati / generazione_ms * 1000.0
            if generazione_ms and generazione_ms >= MIN_FINESTRA_MS and generati else None
        )
        tok_s = None if finale else self._velocita_adesso(adesso)
        if tok_s is not None:
            self._picco = tok_s if self._picco is None else max(self._picco, tok_s)

        prefill_token = self._numero("prompt_processed_tokens", "prompt_calcolati")
        cache = self._numero("cached_tokens", "cache")
        prompt: float | None = None
        if prefill_token is not None and cache is not None and "prompt_calcolati" in self._srv:
            # llama.cpp: calcolati + dalla cache = il prompt intero.
            prompt = prefill_token + cache
        elif self._prompt_intero and self._numero("prompt_tokens") is not None:
            prompt = self._numero("prompt_tokens")
        if prefill_token is None and not self._prompt_intero:
            # Ollama: ``prompt_eval_count`` conta i token davvero calcolati.
            prefill_token = self._numero("prompt_tokens")
        prefill_ms = self._numero("prompt_eval_ms", "prefill_ms")
        draft_n = self._numero("draft_n")
        draft_ok = self._numero("draft_accepted")
        if draft_n is not None or self._base["draft_n"]:
            draft_n = (draft_n or 0) + self._base["draft_n"]
            draft_ok = (draft_ok or 0) + self._base["draft_accettati"]

        attesa = (self._t_primo - self._t0) * 1000.0 if self._t_primo is not None else None
        return Metriche(
            passo=self.passo,
            fase=self.fase,
            fonte=fonte,
            durata_ms=round((adesso - self._t0) * 1000.0),
            attesa_ms=None if attesa is None else round(attesa),
            generazione_ms=None if generazione_ms is None else round(generazione_ms),
            generati=round(generati),
            pensiero=round(self._conti["pensiero"]),
            risposta=round(self._conti["risposta"]),
            chiamate=round(self._conti["chiamate"]),
            tok_s=_arrotonda(tok_s),
            tok_s_passo=_arrotonda(tok_s_passo),
            picco=_arrotonda(self._picco),
            prompt=None if prompt is None else round(prompt),
            prompt_stimato=self.prompt_stimato,
            cache=None if cache is None else round(cache),
            prefill_token=None if prefill_token is None else round(prefill_token),
            prefill_ms=None if prefill_ms is None else round(prefill_ms),
            prefill_fatti=_intero(self._srv.get("prefill_fatti")),
            prefill_totale=_intero(self._srv.get("prefill_totale")),
            draft_n=None if draft_n is None else round(draft_n),
            draft_accettati=None if draft_ok is None else round(draft_ok),
            finestra=self.finestra,
            turno_inizio=self.turno_inizio,
        )


def _intero(valore: Any) -> int | None:
    if isinstance(valore, bool) or not isinstance(valore, (int, float)):
        return None
    if isinstance(valore, float) and not math.isfinite(valore):
        return None
    return round(valore)


# ---------------------------------------------------------------------------
# Registro: le righe della timeline, per il disco
# ---------------------------------------------------------------------------

# Quanti nomi di tool diversi si ricordano per passo: la riga ne mostra due o
# tre, e un passo con venti tool diversi e' gia' un'eccezione.
MAX_TOOL_PER_PASSO = 8


def riga_da_metriche(m: Metriche) -> dict[str, Any]:
    """La riga compatta di un passo, com'e' salvata e com'e' disegnata."""
    return {
        "passo": m.passo,
        "attesa_ms": m.attesa_ms,
        "generazione_ms": m.generazione_ms,
        "tool_ms": 0,
        "durata_ms": m.durata_ms,
        "generati": m.generati,
        "pensiero": m.pensiero,
        "risposta": m.risposta,
        "chiamate": m.chiamate,
        "tok_s": m.tok_s_passo,
        "picco": m.picco,
        "prompt": m.prompt if m.prompt is not None else m.prompt_stimato or None,
        "prompt_esatto": m.prompt is not None,
        "cache": m.cache,
        "prefill_token": m.prefill_token,
        "prefill_ms": m.prefill_ms,
        "draft_n": m.draft_n,
        "draft_accettati": m.draft_accettati,
        "finestra": m.finestra,
        "fonte": m.fonte,
        "tool": {},
        "compattato": False,
    }


@dataclass
class Registro:
    """Le righe della timeline di un turno, raccolte dagli eventi del ciclo.

    Il server lo nutre con gli stessi eventi che spedisce al browser, e a
    fine turno salva ``righe`` insieme alla telemetria: chi riapre la
    conversazione vede la timeline come l'aveva vista dal vivo.
    """

    orologio: Callable[[], float] = time.monotonic
    righe: list[dict[str, Any]] = field(default_factory=list)
    _inizio_passo: float | None = None
    _compattato: bool = False

    def osserva(self, evento: Any) -> None:
        # Import qui dentro: agent importa questo modulo.
        from .agent import HistoryCompacted, StepStarted, ToolFinished

        if isinstance(evento, StepStarted):
            self._chiudi_passo()
            self._inizio_passo = self.orologio()
        elif isinstance(evento, Metriche) and evento.definitivo:
            riga = riga_da_metriche(evento)
            if self._compattato:
                riga["compattato"] = True
                self._compattato = False
            self.righe.append(riga)
        elif isinstance(evento, ToolFinished) and self.righe:
            riga = self.righe[-1]
            riga["tool_ms"] = round(riga["tool_ms"] + max(0.0, float(evento.duration_s)) * 1000)
            nomi = riga["tool"]
            if evento.name in nomi or len(nomi) < MAX_TOOL_PER_PASSO:
                nomi[evento.name] = nomi.get(evento.name, 0) + 1
        elif isinstance(evento, HistoryCompacted):
            # La compattazione succede in PREPARA, prima delle metriche del
            # passo: il segno va sulla riga che nasce dopo.
            self._compattato = True

    def chiudi(self) -> list[dict[str, Any]]:
        self._chiudi_passo()
        return self.righe

    def _chiudi_passo(self) -> None:
        """La durata vera del passo: dall'inizio al passo dopo, tool compresi."""
        if self._inizio_passo is None or not self.righe:
            return
        riga = self.righe[-1]
        durata = round((self.orologio() - self._inizio_passo) * 1000)
        riga["durata_ms"] = max(int(riga.get("durata_ms") or 0), durata)
        self._inizio_passo = None


# ---------------------------------------------------------------------------
# Storico: quello che serve a chi riapre la conversazione
# ---------------------------------------------------------------------------

# Punti del grafico velocita'/contesto: abbastanza per vedere una tendenza,
# pochi abbastanza da non pesare su ogni apertura di chat.
MAX_PUNTI = 400
MAX_TURNI = 24


def _num(valore: Any) -> float | None:
    if isinstance(valore, bool) or not isinstance(valore, (int, float)):
        return None
    if not math.isfinite(valore) or valore < 0:
        return None
    return float(valore)


def _istante(testo: Any) -> float | None:
    if not isinstance(testo, str):
        return None
    try:
        return datetime.fromisoformat(testo).timestamp()
    except ValueError:
        return None


def righe_dalle_chiamate(turno: dict[str, Any]) -> list[dict[str, Any]]:
    """Le righe di un turno registrato prima del ``Registro``.

    Ogni chiamata ``main`` della telemetria e' una richiesta di un passo:
    l'attesa e' il tempo al primo output, la generazione il resto, e il tempo
    fino alla chiamata dopo e' quello dei tool (e dell'harness). La
    ripartizione pensiero/risposta non c'e': la riga lo dice con ``fonte``.
    """
    chiamate = [c for c in turno.get("calls") or []
                if isinstance(c, dict) and c.get("purpose") == "main"]
    righe: list[dict[str, Any]] = []
    for i, chiamata in enumerate(chiamate):
        usage = chiamata.get("usage") if isinstance(chiamata.get("usage"), dict) else {}
        durata = _num(chiamata.get("wall_time_ms")) or 0.0
        attesa = _num(chiamata.get("first_output_ms"))
        generati = _num(usage.get("completion_tokens"))
        eval_ms = _num(usage.get("eval_ms"))
        tok_s = generati / eval_ms * 1000.0 if generati and eval_ms else None
        prompt = _prompt_della_chiamata(chiamata, usage)
        tool_ms = 0.0
        if i + 1 < len(chiamate):
            fine = _istante(chiamata.get("started_at"))
            dopo = _istante(chiamate[i + 1].get("started_at"))
            if fine is not None and dopo is not None:
                tool_ms = max(0.0, (dopo - fine) * 1000.0 - durata)
        righe.append({
            "passo": i + 1,
            "attesa_ms": None if attesa is None else round(attesa),
            "generazione_ms": round(max(0.0, durata - (attesa or 0.0))),
            "tool_ms": round(tool_ms),
            "durata_ms": round(durata + tool_ms),
            "generati": round(generati or 0),
            "pensiero": 0, "risposta": 0, "chiamate": 0,
            "tok_s": _arrotonda(tok_s),
            "picco": None,
            "prompt": None if prompt is None else round(prompt),
            "prompt_esatto": chiamata.get("backend") != "OllamaBackend",
            "cache": _intero(usage.get("cached_tokens")),
            "prefill_token": _intero(usage.get("prompt_processed_tokens")),
            "prefill_ms": _intero(usage.get("prompt_eval_ms")),
            "draft_n": _intero(usage.get("draft_n")),
            "draft_accettati": _intero(usage.get("draft_accepted")),
            "finestra": _intero((chiamata.get("config") or {}).get("num_ctx")) or 0,
            "fonte": "telemetria",
            "tool": {},
            "compattato": False,
        })
    return righe


def _prompt_della_chiamata(chiamata: dict[str, Any], usage: dict[str, Any]) -> float | None:
    """Il prompt intero di una chiamata: dichiarato dove e' intero, stimato su Ollama."""
    if chiamata.get("backend") == "OllamaBackend":
        return _num(chiamata.get("input_tokens_estimated"))
    return _num(usage.get("prompt_tokens")) or _num(chiamata.get("input_tokens_estimated"))


def _righe_del_turno(turno: dict[str, Any]) -> list[dict[str, Any]]:
    righe = turno.get("cruscotto")
    if isinstance(righe, list) and righe:
        return [r for r in righe if isinstance(r, dict)]
    return righe_dalle_chiamate(turno)


def storico(turni: Sequence[Any] | None) -> dict[str, Any]:
    """Timeline dell'ultimo turno, punti velocita'/contesto, riassunto per turno."""
    validi = [t for t in (turni or []) if isinstance(t, dict)][-MAX_TURNI:]
    punti: list[list[float | int]] = []
    riassunti: list[dict[str, Any]] = []
    ultimo: dict[str, Any] | None = None
    for indice, turno in enumerate(validi):
        righe = _righe_del_turno(turno)
        generati = 0.0
        generazione = 0.0
        for riga in righe:
            prompt = _num(riga.get("prompt"))
            tok_s = _num(riga.get("tok_s"))
            if prompt and tok_s:
                punti.append([round(prompt), round(tok_s, 1), indice - len(validi) + 1])
            n = _num(riga.get("generati")) or 0.0
            ms = _num(riga.get("generazione_ms")) or 0.0
            if n and ms and tok_s:
                generati += n
                generazione += ms
        durata = _num(turno.get("turn_wall_ms"))
        riassunti.append({
            "tok_s": _arrotonda(generati / generazione * 1000.0) if generazione else None,
            "generati": round(generati),
            "durata_ms": None if durata is None else round(durata),
            "passi": _intero(turno.get("steps")),
            "motivo": str(turno.get("turn_reason") or "")[:32],
        })
        if indice == len(validi) - 1:
            ultimo = {
                "righe": righe,
                "durata_ms": None if durata is None else round(durata),
                "motivo": str(turno.get("turn_reason") or "")[:32],
                "quando": str(turno.get("recorded_at") or "")[:32],
            }
    return {"punti": punti[-MAX_PUNTI:], "turni": riassunti, "ultimo": ultimo}


def storico_della_sessione(sessione: dict[str, Any]) -> dict[str, Any]:
    """``storico`` memorizzato nella sessione, rifatto solo se la telemetria cambia.

    ``session_stats`` gira a ogni apertura, invio e fine turno: rifare il
    conto su cinquanta turni di chiamate ogni volta sarebbe lavoro buttato.
    La chiave sta nel dict della sessione ma non finisce su disco (``save``
    scrive chiavi esplicite), come ``_contesto_memo``.
    """
    turni = sessione.get("turn_telemetry")
    chiave = (id(turni), len(turni) if isinstance(turni, list) else -1,
              id(turni[-1]) if isinstance(turni, list) and turni else None)
    memo = sessione.get("_cruscotto_memo")
    if isinstance(memo, tuple) and len(memo) == 2 and memo[0] == chiave:
        return memo[1]
    valore = storico(turni if isinstance(turni, list) else [])
    sessione["_cruscotto_memo"] = (chiave, valore)
    return valore
