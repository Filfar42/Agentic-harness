"""Utility di testo: parsing incrementale della Chain-of-Thought e troncamento intelligente.

Modulo puro: nessuna dipendenza dal resto dell'harness, quindi testabile a se stante.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"

# Alcuni modelli usano marker alternativi (Qwen3, DeepSeek-R1 via template custom).
_ALT_TAG_PAIRS: tuple[tuple[str, str], ...] = (
    ("<think>", "</think>"),
    ("<thinking>", "</thinking>"),
    ("<reasoning>", "</reasoning>"),
)

_ANY_THINK_RE = re.compile(
    r"<(think|thinking|reasoning)\b[^>]*>.*?(?:</\1>|\Z)",
    flags=re.DOTALL | re.IGNORECASE,
)


def _partial_suffix_len(buffer: str, tag: str) -> int:
    """Lunghezza del piu' lungo prefisso di ``tag`` che coincide con la coda di ``buffer``.

    Serve per non emettere caratteri che potrebbero essere l'inizio di un tag
    spezzato tra due chunk di streaming (es. ``"<thi"`` + ``"nk>"``).
    """
    max_k = min(len(tag) - 1, len(buffer))
    for k in range(max_k, 0, -1):
        if buffer.endswith(tag[:k]):
            return k
    return 0


class ThinkStreamParser:
    """Separa in tempo reale il canale "ragionamento" dal canale "risposta".

    Uso tipico::

        parser = ThinkStreamParser()
        for delta in stream:
            changed = parser.feed(delta)
            if changed.reasoning: render(parser.reasoning)
            if changed.answer:    render(parser.answer)
        parser.finish()

    Caratteristiche:
      * gestisce tag spezzati fra chunk consecutivi;
      * gestisce **piu'** blocchi di pensiero nella stessa risposta;
      * gestisce un blocco mai chiuso (stream interrotto o max_tokens raggiunto);
      * accetta anche un canale di reasoning nativo separato
        (``delta.reasoning_content`` di vLLM, ``message.thinking`` di Ollama)
        tramite :meth:`feed_reasoning`.
    """

    __slots__ = ("reasoning", "answer", "_buf", "_in_think", "_open", "_close", "_saw_any")

    def __init__(self, open_tag: str = THINK_OPEN, close_tag: str = THINK_CLOSE) -> None:
        self.reasoning: str = ""
        self.answer: str = ""
        self._buf: str = ""
        self._in_think: bool = False
        self._open = open_tag
        self._close = close_tag
        self._saw_any = False

    # -- input ------------------------------------------------------------

    def feed_reasoning(self, text: str) -> bool:
        """Aggiunge testo proveniente da un canale di reasoning nativo."""
        if not text:
            return False
        self.reasoning += text
        self._saw_any = True
        return True

    def feed(self, delta: str) -> tuple[bool, bool]:
        """Consuma un chunk di testo.

        Restituisce ``(reasoning_changed, answer_changed)`` cosi' la UI puo'
        ridisegnare solo il pannello che e' effettivamente cambiato.
        """
        if not delta:
            return (False, False)

        r_before, a_before = len(self.reasoning), len(self.answer)
        self._buf += delta

        while True:
            if not self._in_think:
                i = self._buf.find(self._open)
                if i >= 0:
                    self.answer += self._buf[:i]
                    self._buf = self._buf[i + len(self._open) :]
                    self._in_think = True
                    self._saw_any = True
                    continue

                # Un tag di chiusura orfano non deve finire nella risposta.
                j = self._buf.find(self._close)
                if j >= 0:
                    self.answer += self._buf[:j]
                    self._buf = self._buf[j + len(self._close) :]
                    continue

                hold = max(
                    _partial_suffix_len(self._buf, self._open),
                    _partial_suffix_len(self._buf, self._close),
                )
                if hold:
                    self.answer += self._buf[:-hold]
                    self._buf = self._buf[-hold:]
                else:
                    self.answer += self._buf
                    self._buf = ""
                break

            j = self._buf.find(self._close)
            if j >= 0:
                self.reasoning += self._buf[:j]
                self._buf = self._buf[j + len(self._close) :]
                self._in_think = False
                continue

            hold = _partial_suffix_len(self._buf, self._close)
            if hold:
                self.reasoning += self._buf[:-hold]
                self._buf = self._buf[-hold:]
            else:
                self.reasoning += self._buf
                self._buf = ""
            break

        return (len(self.reasoning) != r_before, len(self.answer) != a_before)

    def finish(self) -> None:
        """Svuota il buffer residuo nel canale corrente (stream troncato)."""
        if not self._buf:
            return
        if self._in_think:
            self.reasoning += self._buf
        else:
            self.answer += self._buf
        self._buf = ""

    # -- output -----------------------------------------------------------

    @property
    def saw_think_tag(self) -> bool:
        return self._saw_any


def strip_think(text: str | None) -> str:
    """Rimuove ogni blocco di pensiero da un testo gia' completo.

    Usata prima di rimandare la cronologia al modello: i blocchi ``<think>``
    non devono mai rientrare nel contesto (bruciano token e degradano la
    qualita' delle chiamate successive).
    """
    if not text:
        return ""
    return _ANY_THINK_RE.sub("", text).strip()


def estrai_think(text: str | None) -> str:
    """Il contrario di ``strip_think``: **solo** i blocchi di pensiero.

    Serve a una cosa sola, e va detto qui perche' altrove sembrerebbe un
    controsenso: il pensiero non deve mai rientrare nel contesto, ma resta
    scritto nel messaggio in sessione (lo strip avviene in
    ``build_api_messages``, non in scrittura). E' quindi l'unica traccia di
    cosa il modello ha capito lungo un punto di piano, e alla chiusura del
    punto vale la pena distillarla prima che il turno la porti via.

    I tag di apertura e chiusura si tolgono: quello che torna e' il testo.
    """
    if not text:
        return ""
    pezzi = []
    for m in _ANY_THINK_RE.finditer(text):
        corpo = m.group(0)
        corpo = re.sub(r"^<[^>]*>", "", corpo)
        corpo = re.sub(r"</[^>]*>$", "", corpo).strip()
        if corpo:
            pezzi.append(corpo)
    return "\n\n".join(pezzi)


# Involucri del template Qwen che il modello a volte scrive **da solo** nel
# canale testuale invece di lasciarli generare al runtime. Vanno rimossi:
# sono rumore per l'utente e, nel caso di <tool_response>, sono un risultato
# di tool *inventato* che non deve essere scambiato per vero.
_TOOL_WRAPPER_RE = re.compile(
    r"<(tool_response|tool_result|tool_output|tool_call)\b[^>]*>(.*?)(?:</\1>|\Z)",
    flags=re.DOTALL | re.IGNORECASE,
)


def strip_tool_wrappers(text: str | None) -> tuple[str, str]:
    """Rimuove gli involucri ``<tool_response>`` & c. dal testo.

    Osservato con qwen2.5-coder: il modello emette

        <tool_response> {"status": "error", "message": "Non ho informazioni..."} </tool_response>

    cioe' **finge** che un tool abbia risposto. Se lo si lascia passare,
    l'utente legge un errore che nessun tool ha mai prodotto, e il turno
    sembra concluso quando non ha fatto nulla.

    Restituisce ``(testo_pulito, contenuto_scartato)``: il secondo elemento,
    se non vuoto, e' la prova che il modello ha inventato un output.
    """
    if not text or "<tool" not in text.lower():
        return (text or "", "")
    dropped: list[str] = []

    def collect(match: re.Match[str]) -> str:
        body = (match.group(2) or "").strip()
        if body:
            dropped.append(body)
        return ""

    clean = _TOOL_WRAPPER_RE.sub(collect, text)
    # Restano a volte tag di chiusura orfani.
    clean = re.sub(r"</?(tool_response|tool_result|tool_output|tool_call)\b[^>]*>", "", clean, flags=re.I)
    return (clean.strip(), "\n".join(dropped))


def split_think(text: str | None) -> tuple[str, str]:
    """Divide un testo completo in ``(reasoning, answer)``.

    Gestisce piu' blocchi concatenandone il contenuto.
    """
    if not text:
        return ("", "")
    parser = ThinkStreamParser()
    parser.feed(text)
    parser.finish()
    return (parser.reasoning.strip(), parser.answer.strip())


# ---------------------------------------------------------------------------
# Troncamento intelligente
# ---------------------------------------------------------------------------


def smart_truncate(
    text: str,
    max_chars: int,
    *,
    head_ratio: float = 0.62,
    label: str = "output",
    consiglio: str | None = None,
) -> str:
    """Tronca al centro preservando testa e coda, allineando ai confini di riga.

    Nei log dei comandi l'informazione critica sta agli estremi: le prime righe
    (comando, versioni, primo errore) e le ultime (traceback finale, exit code).
    Tagliare solo la coda -- come faceva la versione precedente con ``[:4000]`` --
    e' il modo migliore per perdere proprio il messaggio d'errore.
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text

    marker_budget = 120
    usable = max(max_chars - marker_budget, 200)
    head_n = int(usable * head_ratio)
    tail_n = usable - head_n

    head = text[:head_n]
    tail = text[-tail_n:]

    # Allinea ai confini di riga per non spezzare a meta' un token/parola.
    nl = head.rfind("\n")
    if nl > head_n * 0.5:
        head = head[: nl + 1]
    nl = tail.find("\n")
    if nl != -1 and nl < tail_n * 0.5:
        tail = tail[nl + 1 :]

    omitted_chars = len(text) - len(head) - len(tail)
    omitted_lines = text.count("\n") - head.count("\n") - tail.count("\n")

    # Il consiglio di default e' vero per un file (``read_file`` con
    # start_line/end_line rilegge la porzione) ma **falso** per lo stdout di un
    # comando, che dopo il taglio non sta piu' da nessuna parte: rilanciare il
    # comando e' l'unica strada, e non sempre e' ripetibile. Chi ha depositato
    # il testo intero passa il proprio consiglio e la frase torna vera. Vedi
    # ``core/deposito.py``.
    consiglio = consiglio or (
        "Usa un comando piu' mirato (grep/head/tail) per vedere questa porzione"
    )
    marker = (
        f"\n\n[... {label}: omessi {omitted_chars:,} caratteri "
        f"(~{max(omitted_lines, 0):,} righe) dal centro. "
        f"{consiglio} ...]\n\n"
    )
    return head + marker + tail


def truncate_lines(text: str, max_lines: int, *, label: str = "elenco") -> str:
    """Tronca per numero di righe mantenendo testa e coda."""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    head_n = int(max_lines * 0.7)
    tail_n = max_lines - head_n
    omitted = len(lines) - head_n - tail_n
    return "\n".join(
        lines[:head_n]
        + [f"[... {label}: {omitted:,} righe omesse ...]"]
        + lines[-tail_n:]
    )


# ---------------------------------------------------------------------------
# Stima token
# ---------------------------------------------------------------------------

# ~3.6 char/token e' una stima realistica per un mix di italiano + codice con
# tokenizer BPE tipo Qwen. Il vecchio //4 sottostimava del ~10%, e ignorava
# completamente system prompt, schemi tool e risultati dei tool.
_CHARS_PER_TOKEN = 3.6


def estimate_tokens(text: str | None) -> int:
    if not text:
        return 0
    return int(len(text) / _CHARS_PER_TOKEN) + 1


def chars_for_tokens(tokens: float) -> int:
    """L'inverso di :func:`estimate_tokens`, per le soglie.

    Serve dove un budget e' espresso in token ma il controllo va fatto su una
    stringa che cresce token per token: contare i caratteri costa niente,
    ristimare i token ad ogni delta dello stream costerebbe una divisione e una
    allocazione per ogni frammento ricevuto. Stessa costante di conversione,
    cosi' le due stime non divergono.
    """
    return max(0, int(float(tokens) * _CHARS_PER_TOKEN))


def estimate_messages_tokens(messages: Iterable[dict]) -> int:
    """Stima i token di un array di messaggi in formato OpenAI, tool call incluse."""
    total = 0
    for msg in messages:
        total += 4  # overhead per messaggio (role, delimitatori del template)
        content = msg.get("content")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):  # contenuto multimodale/segmentato
            for part in content:
                if isinstance(part, dict):
                    total += estimate_tokens(str(part.get("text", "")))
        for call in msg.get("tool_calls") or []:
            fn = call.get("function", {}) if isinstance(call, dict) else {}
            total += estimate_tokens(str(fn.get("name", "")))
            total += estimate_tokens(str(fn.get("arguments", "")))
            total += 8
    return total
