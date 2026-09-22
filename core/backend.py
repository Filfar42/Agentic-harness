"""Astrazione del backend di inferenza.

Il punto centrale e' il **transport nativo Ollama**, e il motivo per cui esiste
va saputo prima di toccarlo: l'endpoint OpenAI-compatible di Ollama
(``/v1/chat/completions``) **ignora silenziosamente** il campo ``options``.
Nessun errore, nessun warning, e il modello continua a girare con il
``num_ctx`` del Modelfile -- tipicamente 2048 o 4096. Con un system prompt da
~1,5k token piu' gli schemi dei tool la finestra si satura subito, e il modello
smette di emettere tool call pur essendo capacissimo di farlo.

Il transport ``ollama`` parla quindi con ``POST /api/chat``, dove ``options`` e
``keep_alive`` hanno effetto reale, ed espone anche il canale ``thinking``
nativo dei modelli di ragionamento. Il transport ``openai`` resta come ripiego
per vLLM, llama.cpp server, LM Studio o qualunque endpoint compatibile.
"""

from __future__ import annotations

import json
import math
import random
import re
import threading
import time
from datetime import UTC, datetime
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Literal
from collections.abc import Callable, Iterator, Sequence

import httpx

from .config import GenParams
from .jsonsafe import JsonBoundaryError, loads_object

EventKind = Literal["content", "reasoning", "tool_call", "usage", "error"]

MAX_FRAME_BYTES = 1_048_576
MAX_STREAM_BYTES = 16_777_216
MAX_TOOL_CALLS = 64
MAX_ATTEMPTS = 3
# I cataloghi dei modelli sono l'unica risposta non generativa che cresce con
# il provider, non con noi: OpenRouter elenca centinaia di modelli, ognuno con
# prezzi, architettura, parametri supportati -- ben oltre i 10.000 nodi che
# ``loads_object`` concede di default a un argomento di tool. Il tetto resta
# (una risposta ostile non deve poter crescere senza limite), ma misurato sul
# catalogo e non sugli argomenti.
MAX_CATALOG_NODES = 1_000_000
MAX_CATALOG_CHARS = MAX_STREAM_BYTES
RETRY_BASE_S = 0.25
RETRY_CAP_S = 4.0
RETRY_AFTER_CAP_S = 30.0
# Durata massima di UNA generazione, dal primo tentativo all'ultimo byte.
#
# ``timeout_seconds`` delle impostazioni ("Timeout richieste") faceva da
# scadenza totale: un passo che pensava 6.000 token a 25 tok/s, piu' il prefill
# di un contesto da 30k, superava i 180 s di serie e veniva troncato con
# "Budget temporale della generazione esaurito" -- con output gia' emesso,
# quindi senza retry nel transport, e il ciclo lo riprendeva da capo fino a due
# volte, ogni volta tagliato allo stesso punto. Su un 27B locale non e' un
# caso limite: e' un passo di pianificazione normale.
#
# Ora ``timeout_seconds`` e' quello che l'etichetta promette, un timeout di
# **inattivita'**: quanto si aspetta un byte (il prefill ne fa parte, perche'
# llama-server tace finche' non ha finito il prompt). Questa costante resta
# come tetto assoluto, perche' un server che sgocciola un byte ogni tanto non
# deve poter tenere un worker per sempre.
DURATA_MAX_GENERAZIONE_S = 3600.0
StopCheck = Callable[[], bool] | None


class TransportProtocolError(ValueError):
    """A peer sent an incomplete, ambiguous or oversized response."""


class TransportCancelled(Exception):
    """Cooperative cancellation; never retry a cancelled generation."""


class _HTTPFailure(Exception):
    """An HTTP rejection with bounded retry metadata and diagnostic text."""

    def __init__(self, status: int, body: str, retry_after: str = "") -> None:
        super().__init__(f"HTTP {status}: {_messaggio_dal_corpo(body)[:600]}")
        self.status = status
        self.body = body
        self.retry_after = retry_after


def _messaggio_dal_corpo(body: str) -> str:
    """Il motivo scritto dal server, senza l'involucro JSON quando c'e'.

    llama-server risponde ``{"error": {"code": 500, "message": "...",
    "type": "server_error"}}``, Ollama ``{"error": "..."}``: il testo utile
    -- l'eccezione del chat template, per esempio -- sta li' dentro, e
    mostrare all'utente l'involucro intero lo tagliava a meta' dopo 600
    caratteri di punteggiatura. Un corpo che non e' JSON torna com'e'.
    """
    try:
        dati = json.loads(body)
    except (ValueError, TypeError):
        return body
    errore = dati.get("error") if isinstance(dati, dict) else None
    if isinstance(errore, dict):
        errore = errore.get("message")
    return errore if isinstance(errore, str) and errore.strip() else body


# Un 500 del chat template non e' un guasto passeggero: lo stesso payload
# produce lo stesso errore, sempre. Riprovarlo tre volte con il backoff voleva
# dire solo far aspettare l'utente prima di mostrargli il motivo.
_RX_ERRORE_TEMPLATE = re.compile(
    r"jinja|templateerror|chat[ _]template|Supported types are"
    r"|must be at the beginning",
    re.IGNORECASE,
)


def errore_del_template(body: str) -> bool:
    """Il corpo di un errore HTTP viene dal rendering del chat template?"""
    return bool(body) and bool(_RX_ERRORE_TEMPLATE.search(body))


def _checkpoint(should_stop: StopCheck, deadline: float) -> None:
    if should_stop is not None and should_stop():
        raise TransportCancelled("Generazione annullata.")
    if time.monotonic() >= deadline:
        raise httpx.ReadTimeout("Budget temporale della generazione esaurito.")


def _request_timeout(deadline: float, inattivita: float | None = None) -> httpx.Timeout:
    """Timeout di httpx: la lettura e' per singolo ``read``, cioe' inattivita'."""
    remaining = max(0.001, deadline - time.monotonic())
    lettura = remaining if not inattivita or inattivita <= 0 else min(remaining, inattivita)
    return httpx.Timeout(lettura, connect=min(10.0, remaining), pool=min(5.0, remaining))


def _retry_delay(attempt: int, retry_after: str = "") -> float:
    """Full jitter; a server Retry-After is a minimum, never an unbounded sleep."""
    delay = random.uniform(0.0, min(RETRY_CAP_S, RETRY_BASE_S * 2**attempt))
    if retry_after:
        try:
            seconds = float(retry_after)
        except ValueError:
            try:
                target = parsedate_to_datetime(retry_after)
                if target.tzinfo is None:
                    target = target.replace(tzinfo=UTC)
                seconds = (target - datetime.now(UTC)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                seconds = 0.0
        if math.isfinite(seconds):
            delay = max(delay, min(RETRY_AFTER_CAP_S, max(0.0, seconds)))
    return delay


def _wait_retry(delay: float, should_stop: StopCheck, deadline: float) -> None:
    until = min(deadline, time.monotonic() + delay)
    while time.monotonic() < until:
        _checkpoint(should_stop, deadline)
        time.sleep(min(0.05, max(0.0, until - time.monotonic())))
    _checkpoint(should_stop, deadline)


def _stream_with_retries(
    attempt_stream: Callable[[float], Iterator[StreamEvent]],
    timeout_s: float,
    should_stop: StopCheck,
) -> Iterator[StreamEvent]:
    """Retry transient transport failures only before any observable output.

    Tools remain buffered by the protocol parser until completion. Once text
    or reasoning is visible a replay would duplicate content, so it fails the
    current generation explicitly. The caller's worker owns this sync iterator.
    """
    # ``timeout_s`` e' l'inattivita' (lo applica httpx a ogni lettura, vedi
    # ``_request_timeout``); la scadenza totale e' il tetto assoluto.
    deadline = time.monotonic() + max(float(timeout_s), DURATA_MAX_GENERAZIONE_S)
    emitted = False
    try:
        for attempt in range(MAX_ATTEMPTS):
            _checkpoint(should_stop, deadline)
            try:
                yield_from = attempt_stream(deadline)
                try:
                    for event in yield_from:
                        _checkpoint(should_stop, deadline)
                        emitted = True
                        yield event
                finally:
                    yield_from.close()
                return
            except (_HTTPFailure, httpx.TransportError) as exc:
                retryable = (
                    exc.status in {408, 429, 500, 502, 503, 504}
                    and not errore_del_template(exc.body)
                    if isinstance(exc, _HTTPFailure)
                    else isinstance(exc, (httpx.TimeoutException, httpx.NetworkError,
                                          httpx.RemoteProtocolError))
                )
                if emitted or not retryable or attempt == MAX_ATTEMPTS - 1:
                    raise
                after = exc.retry_after if isinstance(exc, _HTTPFailure) else ""
                _wait_retry(_retry_delay(attempt, after), should_stop, deadline)
    except TransportCancelled as exc:
        yield StreamEvent("error", text=str(exc))
    except (httpx.HTTPError, _HTTPFailure, TransportProtocolError,
            JsonBoundaryError, UnicodeError, TypeError, ValueError, OverflowError) as exc:
        yield StreamEvent("error", text=f"{type(exc).__name__}: {str(exc)[:600]}")


def _response_bytes(
    response: httpx.Response, should_stop: StopCheck, deadline: float
) -> Iterator[bytes]:
    """Bound decoded response bytes, including responses with compression."""
    total = 0
    for block in response.iter_bytes():
        _checkpoint(should_stop, deadline)
        total += len(block)
        if total > MAX_STREAM_BYTES:
            raise TransportProtocolError("Risposta oltre il limite di 16 MiB.")
        yield block


def _bounded_lines(
    response: httpx.Response, should_stop: StopCheck, deadline: float
) -> Iterator[str]:
    """Incremental UTF-8 framing, including CRLF split across socket reads."""
    pending = b""
    for block in _response_bytes(response, should_stop, deadline):
        pending += block
        start = 0
        offset = 0
        while offset < len(pending):
            byte = pending[offset]
            if byte not in (10, 13):
                offset += 1
                continue
            if byte == 13 and offset == len(pending) - 1:
                break
            if offset - start > MAX_FRAME_BYTES:
                raise TransportProtocolError("Frame oltre il limite di 1 MiB.")
            yield pending[start:offset].decode("utf-8", errors="strict")
            offset += 2 if byte == 13 and pending[offset + 1] == 10 else 1
            start = offset
        pending = pending[start:]
        if len(pending) > MAX_FRAME_BYTES:
            raise TransportProtocolError("Frame oltre il limite di 1 MiB.")
    if pending:
        yield pending.removesuffix(b"\r").decode("utf-8", errors="strict")


def _sse_data(lines: Iterator[str]) -> Iterator[str]:
    """Emit complete SSE data events. EOF never completes a partial event."""
    data: list[str] = []
    size = 0
    for line in lines:
        if line == "":
            if data:
                yield "\n".join(data)
                data = []
                size = 0
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if field != "data":
            continue
        value = value.removeprefix(" ") if separator else ""
        size += len(value) + 1
        if size > MAX_FRAME_BYTES:
            raise TransportProtocolError("Evento SSE oltre il limite di 1 MiB.")
        data.append(value)
    if data:
        raise TransportProtocolError("Evento SSE troncato prima della riga vuota.")


def _http_status(response: httpx.Response, should_stop: StopCheck, deadline: float) -> None:
    if response.status_code < 400:
        return
    body = bytearray()
    for block in _response_bytes(response, should_stop, deadline):
        body.extend(block[: max(0, 2400 - len(body))])
        if len(body) >= 2400:
            break
    raise _HTTPFailure(response.status_code, body.decode("utf-8", "replace")[:600],
                       response.headers.get("retry-after", ""))


def _text_field(value: Any, field: str, *, limit: int = MAX_FRAME_BYTES) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        raise TransportProtocolError(f"Campo {field} non valido o troppo lungo.")
    return value


def _counter(value: Any, field: str) -> int | float:
    if value is None:
        return 0
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or (isinstance(value, float) and not math.isfinite(value))
            or value < 0 or value > 9_223_372_036_854_775_807):
        raise TransportProtocolError(f"Contatore {field} non valido.")
    return value


# ---------------------------------------------------------------------------
# Il client condiviso
# ---------------------------------------------------------------------------
#
# Tutte le richieste di questo modulo passano di qui. Prima erano dodici
# ``httpx.get``/``httpx.post``/``httpx.stream`` di modulo, e ognuna di quelle
# funzioni costruisce un client usa-e-getta: apre una connessione TCP, manda
# una richiesta, chiude. Sembra irrilevante perche' il backend e' su localhost,
# ed e' esattamente il contrario -- su localhost la richiesta non costa quasi
# niente, quindi *tutto* il tempo e' l'apertura. Misurato su 200 richieste a un
# server locale:
#
#     senza client condiviso: 80.12 ms l'una
#     con client condiviso:    1.51 ms l'una
#
# 78.6 ms di handshake pagati a ogni sonda, e le sonde non sono rare:
# ``props()`` gira da ``gen_params()``, cioe' a ogni turno; ``version()`` e
# ``status()`` a ogni apertura di pannello; ``model_info`` a ogni lettura di
# sessione quando la cache e' fredda.
#
# Il client e' di **modulo, non di istanza**, per la stessa ragione della cache
# di ``model_info`` due sezioni piu' sotto: il server ricostruisce il backend a
# ogni richiesta HTTP, quindi un pool di istanza non verrebbe riusato mai --
# cioe' non sarebbe un pool.
_client: httpx.Client | None = None
# I turni girano in thread separati (un worker per conversazione): senza lock
# due turni che partono insieme costruirebbero due client, e uno dei due
# resterebbe orfano con le sue connessioni aperte. ``httpx.Client`` e' invece
# thread-safe *nell'uso*, che e' la parte che conta.
_client_lock = threading.Lock()


def get_client() -> httpx.Client:
    """Il client condiviso del processo, costruito alla prima richiesta."""
    global _client  # noqa: PLW0603 - singleton di modulo, vedi sopra
    if _client is None:
        with _client_lock:
            if _client is None:
                # Ogni richiesta esplicita il suo budget; il default finito
                # protegge anche i futuri call site che lo dimenticassero.
                _client = httpx.Client(
                    timeout=httpx.Timeout(30.0, connect=10.0, pool=5.0),
                    limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
                )
    return _client


def chiudi_client() -> None:
    """Chiude il client e le sue connessioni. La chiama la lifespan del server."""
    global _client  # noqa: PLW0603 - l'altra meta' di ``get_client``
    with _client_lock:
        if _client is not None:
            _client.close()
            _client = None


@dataclass(slots=True)
class StreamEvent:
    kind: EventKind
    text: str = ""
    tool_call: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None


def normalise_base_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url.endswith("/v1"):
        url = url[: -len("/v1")]
    if not url:
        url = "http://localhost:11434"
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    return url


def _new_tool_call(index: int, call_id: str, name: str, arguments: str) -> dict[str, Any]:
    return {
        "index": index,
        "id": call_id or f"call_{index}",
        "name": name,
        "arguments": arguments,
    }


def _reasoning_report(params: GenParams, payload: dict[str, Any], support: str) -> dict[str, Any]:
    """Describe the request, never claim that a server applied its controls."""
    return {"requested": params.think, "payload": payload, "support": support, "verified": False}


def _known_thinking_key(model: str) -> str | None:
    """Documented template switches; an endpoint/model alias remains unknown.

    https://docs.vllm.ai/en/latest/features/reasoning_outputs/
    These are template conventions, not proof that an OpenAI-compatible peer
    implements vLLM extensions. Never send both keys hoping that one works.
    """
    name = model.lower().replace("\\", "/").rsplit("/", 1)[-1]
    if re.match(r"(?:qwen3|gemma-?4)(?=[.:-]|$)", name):
        # The 2507 Qwen3 instruct/thinking-only releases are not hybrids.
        if "2507" in name or "thinking" in name:
            return None
        return "enable_thinking"
    if re.match(r"(?:deepseek-?v3\.1|granite-?3\.2)(?=[.:-]|$)", name):
        return "thinking"
    return None


def _template_reasoning_keys(template: str) -> set[str]:
    """Inspect Jinja variables without evaluating untrusted server templates.

    Tags/prose containing 'thinking' are not evidence of a template switch.
    Strip quoted literals and comments before checking expression identifiers.
    """
    template = re.sub(r"{#.*?#}", "", template, flags=re.DOTALL)
    expressions = "\n".join(re.findall(r"{[{%](.*?)[}%]}", template, re.DOTALL))
    expressions = re.sub(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"", "", expressions)
    return set(re.findall(r"\b(?:enable_thinking|thinking|reasoning_effort)\b", expressions))


# ---------------------------------------------------------------------------
# Livelli di pensiero ammessi dal template
# ---------------------------------------------------------------------------
#
# L'harness ragiona su una scala sua (``low``/``medium``/``high``/``max``, vedi
# ``GenParams.LIVELLI_PENSIERO``), ma chi decide cosa si puo' mandare e' il
# chat template del modello. Quello ufficiale di Qwen3.8 accetta solo
# ``xhigh``/``medium``/``low`` e per tutto il resto -- ``high`` compreso --
# solleva un'eccezione Jinja: llama-server e vLLM la trasformano in un 500 e
# il turno muore sul primo messaggio. Il template unsloth usato prima non
# controllava, ed e' per questo che il guasto e' comparso solo cambiando GGUF.
#
# I valori ammessi non si scrivono qui: si **leggono** dal template (GET
# /props -> chat_template) o, se non c'e', dal messaggio d'errore del server.
# Il livello richiesto si traduce nel piu' vicino ammesso su questa scala; a
# parita' di distanza vince il piu' alto (``high`` -> ``xhigh``, non
# ``medium``): abbassare il pensiero di default e' una decisione gia' scartata.
ORDINE_LIVELLI: tuple[str, ...] = ("minimal", "low", "medium", "high", "xhigh", "max")

_RX_TIPI_AMMESSI = re.compile(r"Supported types are\s+([^.\n\"'\\]+)", re.IGNORECASE)
_RX_ELENCO_NEL_TEMPLATE = re.compile(
    r"reasoning_effort\w*\s+(?:not\s+)?in\s*[\(\[]([^\)\]]*)[\)\]]", re.IGNORECASE
)


def livelli_dal_testo(testo: str) -> tuple[str, ...]:
    """I livelli elencati nella frase "Supported types are ...".

    La frase e' la stessa nel template (dentro ``raise_exception``) e nel
    messaggio d'errore che il server ne ricava, quindi un solo lettore serve
    entrambi: "xhigh (default), medium, and low" -> ("xhigh", "medium", "low").
    """
    if not isinstance(testo, str):
        return ()
    trovato = _RX_TIPI_AMMESSI.search(testo)
    if not trovato:
        return ()
    elenco = re.sub(r"\([^)]*\)", " ", trovato.group(1))
    livelli: list[str] = []
    for pezzo in re.split(r",|\band\b|\bor\b", elenco):
        parola = pezzo.strip().lower()
        if re.fullmatch(r"[a-z][a-z0-9_-]*", parola) and parola not in livelli:
            livelli.append(parola)
    return tuple(livelli)


def livelli_dal_template(template: Any) -> tuple[str, ...]:
    """I livelli che il chat template accetta, se li dichiara.

    Prima la frase dell'eccezione, poi la tupla del controllo
    (``reasoning_effort not in ('xhigh', 'medium', 'low')``). Il template non
    si esegue: e' codice del server, qui si legge soltanto.
    """
    if not isinstance(template, str) or "reasoning_effort" not in template:
        return ()
    livelli = livelli_dal_testo(template)
    if livelli:
        return livelli
    trovato = _RX_ELENCO_NEL_TEMPLATE.search(template)
    if not trovato:
        return ()
    return tuple(
        dict.fromkeys(v.lower() for v in re.findall(r"['\"]([^'\"]+)['\"]", trovato.group(1)))
    )


def livello_piu_vicino(richiesto: str, ammessi: Sequence[str]) -> str | None:
    """Il livello ammesso piu' vicino a quello richiesto.

    ``None`` quando non si sa tradurre (livello fuori scala, o nessun ammesso
    che stia sulla scala): chi chiama omette il campo e lascia il default del
    template, invece di mandare un valore che sa gia' rifiutato.
    """
    if richiesto in ammessi:
        return richiesto
    if richiesto not in ORDINE_LIVELLI:
        return None
    posizione = ORDINE_LIVELLI.index(richiesto)
    candidati = [a for a in ammessi if a in ORDINE_LIVELLI]
    if not candidati:
        return None
    return min(
        candidati,
        key=lambda a: (abs(ORDINE_LIVELLI.index(a) - posizione), -ORDINE_LIVELLI.index(a)),
    )


# Livelli imparati dai messaggi d'errore, per (endpoint, modello). Di modulo e
# non di istanza per la stessa ragione di ``_MODEL_INFO_CACHE``: il server
# ricostruisce il backend, e un elenco d'istanza si perderebbe al turno dopo --
# cioe' si ripagherebbe il 500 a ogni messaggio. Si svuota con
# ``forget_model_info()``, che e' il gesto di chi cambia endpoint o modello.
_LIVELLI_APPRESI: dict[tuple[str, str], tuple[str, ...]] = {}


def _effort_nel_payload(payload: dict[str, Any]) -> str | None:
    """Il livello che questo payload manda, dovunque stia."""
    for sorgente in (payload, payload.get("chat_template_kwargs") or {}):
        valore = sorgente.get("reasoning_effort") if isinstance(sorgente, dict) else None
        if isinstance(valore, str) and valore != "none":
            return valore
    return None


def _traduci_effort(payload: dict[str, Any], ammessi: Sequence[str]) -> dict[str, Any]:
    """Riscrive ``reasoning_effort`` (in testa e nei kwargs) sul livello ammesso.

    Senza elenco non tocca niente: e' il caso di OpenRouter e di ogni endpoint
    che non si e' mai lamentato. ``"none"`` resta com'e': e' lo spegnimento,
    non un livello, e llama.cpp lo documenta a parte.
    """
    if not ammessi:
        return payload
    fuori = dict(payload)
    for dove in ("top", "kwargs"):
        sorgente = fuori if dove == "top" else fuori.get("chat_template_kwargs")
        if not isinstance(sorgente, dict):
            continue
        valore = sorgente.get("reasoning_effort")
        if not isinstance(valore, str) or valore == "none" or valore in ammessi:
            continue
        sorgente = dict(sorgente)
        nuovo = livello_piu_vicino(valore, ammessi)
        if nuovo is None:
            sorgente.pop("reasoning_effort")
        else:
            sorgente["reasoning_effort"] = nuovo
        if dove == "top":
            fuori = {**sorgente}
        elif sorgente:
            fuori["chat_template_kwargs"] = sorgente
        else:
            fuori.pop("chat_template_kwargs")
    return fuori


# ---------------------------------------------------------------------------
# Conversione dei messaggi
# ---------------------------------------------------------------------------


PREFISSO_NOTA_HARNESS = "[Nota dell'harness]"


def messaggi_per_il_filo(messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Un solo messaggio ``system``, e solo in testa: la forma che i template
    severi pretendono.

    Il template ufficiale di Qwen3.8 solleva "System message must be at the
    beginning." per qualunque system che non sia il primo, e l'harness ne
    manda sempre almeno due in testa (prompt di sistema ed environment) piu'
    la nota di ``trim_to_window`` quando taglia. Qui, **al confine**, e non
    nella cronologia: la sessione su disco resta com'e', cambia solo cio' che
    viaggia.

    * I system contigui in testa si fondono nel primo -- quello che
      ``to_ollama_messages`` faceva gia' per Ollama: stessa forma su tutti i
      transport, e il prefisso resta byte-identico fra un passo e l'altro.
    * Un system piu' avanti diventa un ``user`` con il prefisso
      ``[Nota dell'harness]``, nella sua posizione. Se cadrebbe fra un
      assistant con ``tool_calls`` e i suoi risultati, scivola dopo l'ultimo
      ``tool`` del gruppo: in mezzo spezzerebbe l'abbinamento chiamata-esito,
      che i template rifiutano.

    Non modifica i dizionari ricevuti.
    """
    elenco = list(messages)
    inizio = 0
    while inizio < len(elenco) and elenco[inizio].get("role") == "system":
        inizio += 1
    if inizio <= 1 and not any(m.get("role") == "system" for m in elenco[inizio:]):
        return elenco
    out: list[dict[str, Any]] = []
    if inizio == 1:
        out.append(elenco[0])
    elif inizio > 1:
        testa = dict(elenco[0])
        parti = [str(m.get("content") or "").strip() for m in elenco[:inizio]]
        testa["content"] = "\n\n".join(p for p in parti if p)
        out.append(testa)
    rinviate: list[dict[str, Any]] = []
    in_gruppo = False
    for msg in elenco[inizio:]:
        ruolo = msg.get("role")
        if ruolo == "system":
            testo = str(msg.get("content") or "").strip()
            if testo:
                nota = {"role": "user", "content": f"{PREFISSO_NOTA_HARNESS} {testo}"}
                (rinviate if in_gruppo else out).append(nota)
            continue
        if ruolo == "tool":
            out.append(msg)
            continue
        out.extend(rinviate)
        rinviate = []
        out.append(msg)
        in_gruppo = ruolo == "assistant" and bool(msg.get("tool_calls"))
    out.extend(rinviate)
    return out


def to_ollama_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Converte i messaggi in formato OpenAI nel dialetto di ``/api/chat``.

    Formato richiesto da Ollama (docs.ollama.com/capabilities/tool-calling):

      * ``tool_calls[].function.arguments`` e' un **oggetto**, non una stringa
        JSON come nel dialetto OpenAI;
      * ogni tool call porta ``"type": "function"`` e un ``index``;
      * il risultato di un tool e' ``{"role": "tool", "tool_name": ...,
        "content": ...}``. Il campo si chiama ``tool_name``: ``name`` e
        ``tool_call_id`` non fanno parte dello schema e vengono scartati.

    Un solo messaggio di sistema: diversi chat template (Llama 3.x, alcune
    varianti Qwen) iniettano gli schemi dei tool **nel primo messaggio system**
    e ignorano o gestiscono male i successivi. Piu' blocchi system vengono
    quindi fusi in uno solo, preservando l'ordine.
    """
    out: list[dict[str, Any]] = []
    system_parts: list[str] = []

    for msg in messages:
        role = msg.get("role")

        if role == "system":
            text = str(msg.get("content") or "").strip()
            if text:
                system_parts.append(text)
            continue

        if role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_name": msg.get("name", ""),
                    "content": str(msg.get("content", "")),
                }
            )
            continue

        converted: dict[str, Any] = {
            "role": role,
            "content": msg.get("content") or "",
        }
        # Il pensiero precompilato di una continuazione (vedi
        # ``OllamaBackend.modo_continuazione``). Solo su un assistant e solo se
        # c'e': i messaggi della cronologia non lo portano mai, il pensiero dei
        # passi vecchi resta scartato come prima.
        if role == "assistant" and msg.get("thinking"):
            converted["thinking"] = str(msg["thinking"])
        # Immagini per i modelli multimodali: Ollama le vuole come lista di
        # stringhe base64 sul messaggio, senza prefisso data:.
        if msg.get("images"):
            converted["images"] = list(msg["images"])
        calls = msg.get("tool_calls")
        if calls:
            converted["tool_calls"] = []
            for index, call in enumerate(calls):
                fn = call.get("function", {})
                raw_args = fn.get("arguments", "{}")
                parsed = loads_object(raw_args)
                converted["tool_calls"].append(
                    {
                        "type": "function",
                        "function": {
                            "index": index,
                            "name": fn.get("name", ""),
                            "arguments": parsed,
                        },
                    }
                )
        out.append(converted)

    if system_parts:
        out.insert(0, {"role": "system", "content": "\n\n".join(system_parts)})
    return out


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


# Versione di Ollama a partire dalla quale ``stream: true`` emette le tool call
# man mano che arrivano. Prima di questa, una richiesta con `tools` e
# `stream: true` restituisce il testo ma **nessuna** tool_calls: il modello
# sembra "aver smesso di usare i tool" pur essendo perfettamente capace.
MIN_VERSION_STREAMING_TOOLS = (0, 8, 0)


def parse_version(raw: str) -> tuple[int, ...]:
    numbers: list[int] = []
    for part in (raw or "").split("-")[0].split("."):
        try:
            numbers.append(int(part))
        except ValueError:
            break
    return tuple(numbers) or (0,)


# Scheda dei modelli, condivisa fra tutte le istanze di backend: vedi
# ``OllamaBackend.model_info``. Chiave: (base_url, nome del modello).
_MODEL_INFO_CACHE: dict[tuple[str, str], dict[str, Any]] = {}

# Anche i **fallimenti** vanno ricordati, per un po'. Non ricordarli sembrava
# prudente ("al prossimo giro riprova") ed era la voce piu' cara dell'apertura
# di una chat: ``session_stats`` chiede le capability del modello ad ogni
# lettura di sessione, e con il modello su un'altra macchina spenta ogni
# lettura pagava i sei secondi di timeout, per intero. Aprire una chat, finire
# un turno, riallinearsi: sei secondi ognuno.
#
# Trenta secondi sono la misura di un endpoint che sta tornando su: abbastanza
# per non ripagare il timeout dieci volte in un minuto, abbastanza poco perche'
# accendere il server del modello si veda quasi subito.
_MODEL_INFO_FALLITI: dict[tuple[str, str], float] = {}
MODEL_INFO_FALLIMENTO_TTL_S = 30.0


def forget_model_info() -> None:
    """Svuota la cache delle capability: da chiamare quando l'utente cambia
    endpoint o modello, o quando ri-lancia la sonda a mano.

    Svuota anche i fallimenti ricordati: chi ri-lancia la sonda a mano lo fa
    proprio perche' ha appena acceso qualcosa, e fargli aspettare il TTL
    sarebbe rispondergli con la fotografia di prima."""
    _MODEL_INFO_CACHE.clear()
    _MODEL_INFO_FALLITI.clear()
    # Anche i livelli di pensiero imparati da un errore: un altro modello, o
    # lo stesso con un altro GGUF, puo' avere un altro template.
    _LIVELLI_APPRESI.clear()


class OllamaBackend:
    """Transport nativo ``/api/chat``: options e keep_alive funzionano davvero."""

    name = "ollama"
    supports_cancellation = True
    manages_retries = True
    # ``prompt_eval_count`` di Ollama, con la cache del prefisso attiva, non e'
    # garantito essere il prompt intero: puo' contare solo i token ricalcolati.
    # Il ciclo non ci calibra sopra la stima (``agent.calibra_stima``).
    prompt_tokens_is_total = False

    def __init__(
        self,
        base_url: str,
        timeout_s: float = 180.0,
        *,
        stream_with_tools: bool | None = None,
    ) -> None:
        self.base_url = normalise_base_url(base_url)
        self.timeout_s = float(timeout_s)
        if not math.isfinite(self.timeout_s) or self.timeout_s <= 0:
            raise ValueError("timeout_s deve essere finito e positivo")
        # None = decidi dalla versione del server.
        self._stream_with_tools = stream_with_tools
        self._version_cache: str | None = None
        # I livelli di pensiero ("low"/"medium"/"high"/"max") li accettano solo
        # le versioni recenti di Ollama; le altre rifiutano la richiesta con un
        # 400. None = non si sa ancora e si prova; False = provato e rifiutato,
        # da qui in poi si manda il booleano. Si impara dal server invece di
        # dichiararlo con una tabella di versioni che invecchia da sola.
        self._think_levels_ok: bool | None = None
        # Chiusura del pensiero per continuazione. Il messaggio finale e' un
        # assistant col solo ``thinking`` (il pensiero parziale piu' la frase che
        # lo chiude) e il template lo rende senza <|im_end|>: il modello
        # riprende da dopo </think>, cioe' dall'azione.
        #
        # Quale ``think`` mandare con la continuazione non e' scritto da nessuna
        # parte che valga per tutte le versioni. ``False`` e' il primo tentativo
        # perche' il parser di Ollama allora tratta l'uscita come contenuto e
        # tool call; ``True`` il secondo. Chi fallisce (400, oppure il modello
        # che ricomincia a pensare da capo) esce dalla lista, e con la lista
        # vuota la continuazione si spegne per questa istanza: si torna al
        # watchdog che interrompe. Stessa filosofia di ``_think_levels_ok``:
        # imparato dal server, non dichiarato. ``scripts/sonda_continuazione.py``
        # verifica sul server vero anche la cosa che da qui non si vede, cioe'
        # che il modello il pensiero precompilato lo legga davvero.
        self._modi_continuazione: list[bool] = [False, True]
        self._continuazione_riuscita = False

    supports_think_continuation = True

    def modo_continuazione(self) -> bool | None:
        """Il ``think`` da usare per continuare, o ``None`` se non si puo'."""
        return self._modi_continuazione[0] if self._modi_continuazione else None

    def esito_continuazione(self, modo: bool, riuscita: bool) -> None:
        """Una continuazione fallita toglie il suo modo, se non ha mai funzionato.

        Un solo fallimento dopo dei successi non spegne niente: un modello che
        una volta ricomincia a pensare non dice che il server non sa
        continuare.
        """
        if riuscita:
            self._continuazione_riuscita = True
            return
        if self._continuazione_riuscita:
            return
        if self._modi_continuazione and self._modi_continuazione[0] == modo:
            self._modi_continuazione.pop(0)

    # -- introspezione ----------------------------------------------------

    def version(self, *, refresh: bool = False) -> str:
        """Versione del server, interrogata una volta sola per istanza.

        La versione di un server locale non cambia durante una sessione:
        rifarne la GET ad ogni passo agentico aggiungerebbe un round-trip
        (e fino a 4s di timeout se il server e' appena caduto) prima di ogni
        singola generazione.
        """
        if self._version_cache is not None and not refresh:
            return self._version_cache
        try:
            resp = get_client().get(f"{self.base_url}/api/version", timeout=4.0)
            resp.raise_for_status()
            data = loads_object(resp.json())
            self._version_cache = _text_field(data.get("version"), "version", limit=128)
        except Exception:  # noqa: BLE001
            self._version_cache = ""
        return self._version_cache

    def streams_tool_calls(self) -> bool:
        """True se conviene tenere ``stream: true`` quando si inviano tool."""
        if self._stream_with_tools is not None:
            return self._stream_with_tools
        raw = self.version()
        if not raw:
            return False  # in dubbio, la modalita' sicura e' non-streaming
        return parse_version(raw) >= MIN_VERSION_STREAMING_TOOLS

    def status(self) -> tuple[bool, str, list[str]]:
        """Stato del server e catalogo dei modelli in **una sola** richiesta.

        ``ping`` e ``list_models`` interrogano lo stesso ``/api/tags``: usarli
        in coppia, come faceva l'avvio della UI, raddoppiava l'attesa senza
        aggiungere informazione.
        """
        try:
            resp = get_client().get(f"{self.base_url}/api/tags", timeout=4.0)
            resp.raise_for_status()
            data = loads_object(
                resp.json(), max_chars=MAX_CATALOG_CHARS, max_nodes=MAX_CATALOG_NODES
            )
            raw_models = data.get("models", [])
            if not isinstance(raw_models, list) or any(not isinstance(m, dict) for m in raw_models):
                raise TransportProtocolError("models deve essere una lista di oggetti.")
            models = sorted(_text_field(m.get("name"), "model.name") for m in raw_models)
            return True, f"{len(models)} modelli disponibili", models
        except Exception as exc:  # noqa: BLE001 - diagnostica per la UI
            return False, f"{type(exc).__name__}: {exc}", []

    def ping(self) -> tuple[bool, str]:
        online, detail, _ = self.status()
        return online, detail

    def list_models(self) -> list[str]:
        return self.status()[2]

    def loaded_models(self) -> list[dict[str, Any]]:
        """Modelli attualmente in memoria, con la VRAM che occupano.

        E' l'unico modo di sapere qualcosa sull'hardware quando Ollama gira su
        un'altra macchina: ``nvidia-smi`` misurerebbe la scheda locale, che li'
        e' semplicemente quella sbagliata. ``/api/ps`` non dice quanto e'
        grande la scheda -- quel dato l'API non lo espone -- ma dice quanto e'
        occupato, e con la capienza dichiarata una volta dall'utente basta.

        Non solleva: e' diagnostica, e un server spento non deve impedire di
        aprire il pannello delle impostazioni.
        """
        try:
            resp = get_client().get(f"{self.base_url}/api/ps", timeout=4.0)
            resp.raise_for_status()
            data = loads_object(resp.json())
            models = data.get("models", [])
            if not isinstance(models, list):
                raise TransportProtocolError("models deve essere una lista.")
        except Exception:  # noqa: BLE001
            return []
        return [m for m in models if isinstance(m, dict)]

    def model_info(self, model: str, *, refresh: bool = False) -> dict[str, Any]:
        """Scheda del modello (capabilities, template), messa in cache.

        La cache e' **di modulo, non di istanza**: il server ricostruisce il
        backend ad ogni richiesta HTTP, quindi una cache di istanza non
        verrebbe mai riusata. Le capability di un modello non cambiano finche'
        non lo si ri-scarica, mentre questa POST costa un round-trip che, se
        Ollama sta generando, resta in coda dietro alla generazione: era il
        motivo per cui aprire una chat poteva richiedere secondi.
        """
        key = (self.base_url, model)
        if not refresh:
            cached = _MODEL_INFO_CACHE.get(key)
            if cached is not None:
                return cached
            fallito = _MODEL_INFO_FALLITI.get(key)
            if fallito is not None and time.monotonic() - fallito < MODEL_INFO_FALLIMENTO_TTL_S:
                # Ha appena fallito: non si ripaga il timeout adesso.
                return {}
        try:
            resp = get_client().post(
                f"{self.base_url}/api/show", json={"model": model}, timeout=6.0
            )
            resp.raise_for_status()
            info = loads_object(resp.json())
        except Exception:  # noqa: BLE001
            _MODEL_INFO_FALLITI[key] = time.monotonic()
            return {}
        _MODEL_INFO_FALLITI.pop(key, None)
        _MODEL_INFO_CACHE[key] = info
        return info

    def supports_tools(self, model: str) -> bool | None:
        """None = impossibile determinarlo."""
        info = self.model_info(model)
        if not info:
            return None
        caps = info.get("capabilities")
        if isinstance(caps, list):
            return "tools" in caps
        template = str(info.get("template", ""))
        # ``None`` e non ``False``: il template e' un indizio, non una
        # dichiarazione. Uno che non nomina i tool puo' semplicemente non
        # nominarli, e rispondere "non li supporta" toglierebbe gli schemi a
        # un modello capacissimo di usarli. ``x or None`` diceva gia' questo,
        # ma si legge come una svista.
        return True if "tools" in template.lower() else None

    def supports_thinking(self, model: str) -> bool | None:
        """Il modello ha un canale di ragionamento separato? None = ignoto.

        Ollama espone la capability ``thinking`` per i modelli reasoning
        (qwen3, deepseek-r1, ...). Vale la pena rilevarla invece di lasciarla
        a un interruttore manuale: con quei modelli il pensiero va chiesto col
        parametro ``think``, e i tag ``<think>`` nel prompt diventano
        istruzioni ridondanti che competono con le tool call.
        """
        info = self.model_info(model)
        if not info:
            return None
        caps = info.get("capabilities")
        if isinstance(caps, list):
            return "thinking" in caps
        return None

    def preload(self, model: str, keep_alive: str = "30m") -> bool:
        """Carica il modello in VRAM senza generare nulla.

        Una ``/api/chat`` con ``messages: []`` fa esattamente questo. Serve
        all'avvio dell'app: senza, i 4-15 s di caricamento li paga l'utente sul
        primo messaggio, quando sta gia' aspettando una risposta.
        """
        try:
            resp = get_client().post(
                f"{self.base_url}/api/chat",
                json={"model": model, "messages": [], "keep_alive": keep_alive},
                timeout=self.timeout_s,
            )
            return resp.status_code == 200
        except Exception:  # noqa: BLE001 - un preload fallito non e' un errore
            return False

    def supports_vision(self, model: str) -> bool | None:
        """Il modello legge le immagini? None = impossibile determinarlo.

        Mandare un'immagine a un modello che non la capisce non da' errore:
        Ollama la accetta e il modello la ignora, l'utente vede una risposta
        vaga e non sa perche'. Meglio chiederlo prima.
        """
        info = self.model_info(model)
        if not info:
            return None
        caps = info.get("capabilities")
        if isinstance(caps, list):
            return "vision" in caps
        return None

    # -- generazione ------------------------------------------------------

    def reasoning_control(self, params: GenParams) -> dict[str, Any]:
        """Pure diagnostic of the native payload, including learned fallback.

        Un livello che il template ha gia' rifiutato una volta si traduce nel
        piu' vicino fra quelli che ha elencato (vedi ``livello_piu_vicino``);
        senza elenco imparato il payload e' quello di sempre.
        """
        think = params.think_payload
        support = "native_unverified"
        if isinstance(think, str) and self._think_levels_ok is False:
            think = True
            support = "boolean_only"
        elif isinstance(think, str):
            ammessi = _LIVELLI_APPRESI.get((self.base_url, params.model), ())
            if ammessi and think not in ammessi:
                # Fuori scala: acceso e basta, il livello lo sceglie il template.
                think = livello_piu_vicino(think, ammessi) or True
                support = "translated_from_error"
        return _reasoning_report(
            params, {"think": think} if think is not None else {},
            support if think is not None else "omitted",
        )

    def livello_inviato(self, params: GenParams) -> Any:
        """Il valore di ``think`` che parte davvero, dopo le traduzioni."""
        return self.reasoning_control(params)["payload"].get("think")

    def _impara_livelli(self, params: GenParams, payload: dict[str, Any], corpo: str) -> bool:
        """Il template ha rifiutato il livello ed elenca quelli buoni? Allora
        si impara l'elenco e si riprova una volta sola.

        Stesso schema di ``_livello_rifiutato``: tre condizioni insieme --
        avevamo mandato un livello, il server ne elenca altri, e l'elenco non
        contiene quello mandato. Un errore diverso non tocca niente.
        """
        mandato = payload.get("think")
        ammessi = livelli_dal_testo(corpo)
        if not isinstance(mandato, str) or not ammessi or mandato in ammessi:
            return False
        _LIVELLI_APPRESI[(self.base_url, params.model)] = ammessi
        return True

    def build_payload(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        params: GenParams,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": params.model,
            "messages": to_ollama_messages(messaggi_per_il_filo(messages)),
            "stream": stream,
            "options": params.ollama_options(),
            "keep_alive": params.keep_alive,
        }
        if tools:
            payload["tools"] = tools
        payload.update(self.reasoning_control(params)["payload"])
        return payload

    def _livello_rifiutato(self, payload: dict[str, Any], corpo: str) -> bool:
        """Il 400 e' colpa del livello di pensiero? Allora si riprova.

        Due condizioni insieme, perche' una sola sbaglierebbe: che avessimo
        davvero mandato un livello, e che il server nomini ``think`` nel
        motivo. Un 400 per un altro motivo non deve spegnere una manopola che
        funzionava.
        """
        if not isinstance(payload.get("think"), str):
            return False
        if "think" not in corpo.lower():
            return False
        self._think_levels_ok = False
        return True

    def _emit_message(
        self, message: dict[str, Any], tool_index: int
    ) -> tuple[list[StreamEvent], int]:
        """Validate the Ollama envelope; argument semantics belong to the dispatcher."""
        if not isinstance(message, dict):
            raise TransportProtocolError("message deve essere un oggetto.")
        events: list[StreamEvent] = []
        for field, kind in (("thinking", "reasoning"), ("content", "content")):
            value = _text_field(message.get(field), field)
            if value:
                events.append(StreamEvent(kind, text=value))
        calls = message.get("tool_calls")
        if calls is None:
            calls = []
        if not isinstance(calls, list) or len(calls) + tool_index > MAX_TOOL_CALLS:
            raise TransportProtocolError("Lista tool_call non valida o troppo lunga.")
        for call in calls:
            if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                raise TransportProtocolError("Envelope tool_call non valido.")
            fn = call["function"]
            name = _text_field(fn.get("name"), "function.name", limit=256)
            if not name:
                raise TransportProtocolError("Nome tool mancante.")
            call_id = _text_field(call.get("id"), "tool_call.id", limit=256)
            args = fn.get("arguments", {})
            # Preserve malformed argument strings for the dispatcher's structured
            # self-correction feedback, but never accept arbitrary decoded values.
            if isinstance(args, str):
                args_text = _text_field(args, "function.arguments")
            else:
                args_text = json.dumps(loads_object(args), ensure_ascii=False, allow_nan=False)
            events.append(StreamEvent("tool_call", tool_call=_new_tool_call(
                tool_index, call_id or f"ollama_{tool_index}", name, args_text,
            )))
            tool_index += 1
        return events, tool_index

    @staticmethod
    def _usage_event(chunk: dict[str, Any]) -> StreamEvent:
        """Normalize counters after validating all untrusted scalar types."""
        usage: dict[str, Any] = {
            "done_reason": _text_field(chunk.get("done_reason"), "done_reason", limit=128),
        }
        for target, source, scale in (
            ("prompt_tokens", "prompt_eval_count", 1),
            ("completion_tokens", "eval_count", 1),
            ("prompt_eval_ms", "prompt_eval_duration", 1e6),
            ("eval_ms", "eval_duration", 1e6),
            ("total_ms", "total_duration", 1e6),
        ):
            value = chunk.get(source)
            if value is not None:
                usage[target] = round(_counter(value, source) / scale)
        return StreamEvent("usage", usage=usage)

    def _ollama_attempt(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        params: GenParams,
        *,
        streaming: bool,
        should_stop: StopCheck,
        deadline: float,
    ) -> Iterator[StreamEvent]:
        """One request, plus at most one explicit unsupported-think fallback.

        Tool calls are translated as they arrive but released only after
        ``done: true``. EOF, invalid JSON or cancellation discards the buffer.
        """
        # Al piu' due ripieghi, uno per specie: il livello tradotto sull'elenco
        # del template, e il livello spento in booleano da un 400 di Ollama
        # (che puo' seguire il primo, se Ollama non conosce il livello tradotto).
        livelli_imparati = False
        livello_booleano = False
        for _compatibility_attempt in range(3):
            _checkpoint(should_stop, deadline)
            payload = self.build_payload(messages, tools, params, stream=streaming)
            pending_tools: list[StreamEvent] = []
            tool_index = 0
            emesso = False
            try:
                with get_client().stream(
                    "POST", f"{self.base_url}/api/chat", json=payload,
                    timeout=_request_timeout(deadline, self.timeout_s),
                ) as response:
                    _http_status(response, should_stop, deadline)
                    if streaming:
                        chunks = (
                            loads_object(line)
                            for line in _bounded_lines(response, should_stop, deadline)
                            if line.strip()
                        )
                    else:
                        raw = b"".join(_response_bytes(response, should_stop, deadline))
                        chunks = iter([loads_object(raw.decode("utf-8"), max_chars=MAX_STREAM_BYTES)])
                    for chunk in chunks:
                        _checkpoint(should_stop, deadline)
                        if chunk.get("error"):
                            motivo = _text_field(chunk["error"], "error")
                            if (not emesso and not livelli_imparati
                                    and self._impara_livelli(params, payload, motivo)):
                                raise _HTTPFailure(500, motivo)
                            raise TransportProtocolError(f"Ollama: {motivo[:600]}")
                        done = chunk.get("done", False)
                        if not isinstance(done, bool):
                            raise TransportProtocolError("done deve essere booleano.")
                        message = chunk.get("message")
                        events, tool_index = self._emit_message(
                            {} if message is None else message, tool_index
                        )
                        for event in events:
                            if event.kind == "tool_call":
                                pending_tools.append(event)
                            else:
                                emesso = True
                                yield event
                        if done:
                            usage = self._usage_event(chunk)
                            yield usage
                            # A token-limit finish is complete on the wire but
                            # its tool arguments may be incomplete: never dispatch.
                            if usage.usage and usage.usage["done_reason"] != "length":
                                yield from pending_tools
                            return
                    raise TransportProtocolError("Stream Ollama incompleto: manca done=true.")
            except _HTTPFailure as exc:
                if not livelli_imparati and self._impara_livelli(params, payload, exc.body):
                    livelli_imparati = True
                    continue
                if (not livello_booleano and exc.status == 400
                        and self._livello_rifiutato(payload, exc.body)):
                    livello_booleano = True
                    continue
                raise

    def _blocking_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        params: GenParams,
        *,
        should_stop: StopCheck = None,
    ) -> Iterator[StreamEvent]:
        """Bounded non-streaming fallback for Ollama versions without streaming tools."""
        yield from _stream_with_retries(
            lambda deadline: self._ollama_attempt(
                messages, tools, params, streaming=False,
                should_stop=should_stop, deadline=deadline,
            ), self.timeout_s, should_stop,
        )

    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        params: GenParams,
        *,
        should_stop: StopCheck = None,
    ) -> Iterator[StreamEvent]:
        """Stream on the caller's worker, with bounded retries and cancellation.

        Cancellation is cooperative between network reads. A silent socket
        remains interruptible by its configured read timeout, not by this callback.
        """
        if should_stop is not None and should_stop():
            yield StreamEvent("error", text="Generazione annullata.")
            return
        streaming = not tools or self.streams_tool_calls()
        yield from _stream_with_retries(
            lambda deadline: self._ollama_attempt(
                messages, tools, params, streaming=streaming,
                should_stop=should_stop, deadline=deadline,
            ), self.timeout_s, should_stop,
        )

    # -- diagnostica -------------------------------------------------------


class OpenAICompatBackend:
    """Fallback ``/v1/chat/completions`` per vLLM, LM Studio, llama.cpp server."""

    name = "openai"
    supports_cancellation = True
    manages_retries = True
    # Nello standard OpenAI ``usage.prompt_tokens`` e' il prompt intero, parte
    # in cache compresa (che sta in ``prompt_tokens_details.cached_tokens``).
    prompt_tokens_is_total = True


    def __init__(self, base_url: str, api_key: str, timeout_s: float = 180.0) -> None:
        base = normalise_base_url(base_url)
        self.base_url = f"{base}/v1"
        self.api_key = api_key or "not-needed"
        self.timeout_s = float(timeout_s)
        if not math.isfinite(self.timeout_s) or self.timeout_s <= 0:
            raise ValueError("timeout_s deve essere finito e positivo")

    def _auth_headers(self) -> dict[str, str]:
        """Header di autenticazione, uno solo per tutte le sonde.

        Vale anche per le rotte *native* dei dialetti: llama-server lanciato
        con ``--api-key`` protegge tutto, ``/props`` compreso, e a mani vuote
        risponde 401 senza spiegare. Dalla UI quel 401 si legge "endpoint non
        raggiungibile" mentre il server sta benissimo -- e la goccia rossa su
        un server vivo e' il caso peggiore, perche' non sembra un errore.
        """
        return {"Authorization": f"Bearer {self.api_key}"}

    def _fetch_models(self) -> tuple[bool, str, list[str]]:
        """``GET /v1/models``. L'unico posto in cui quella rotta si interroga.

        Sta a se' perche' ``status`` e ``list_models`` la vogliono entrambi, e
        farli chiamare a vicenda -- con le sottoclassi che ne ridefiniscono uno
        dei due -- e' il modo piu' rapido di scrivere una ricorsione infinita
        senza accorgersene.
        """
        try:
            resp = get_client().get(
                f"{self.base_url}/models", headers=self._auth_headers(), timeout=4.0
            )
            resp.raise_for_status()
            data = loads_object(
                resp.json(), max_chars=MAX_CATALOG_CHARS, max_nodes=MAX_CATALOG_NODES
            )
            raw_models = data.get("data", [])
            if not isinstance(raw_models, list) or any(not isinstance(m, dict) for m in raw_models):
                raise TransportProtocolError("data deve essere una lista di oggetti.")
            models = sorted(_text_field(m.get("id"), "model.id") for m in raw_models)
            return True, f"{len(models)} modelli disponibili", models
        except Exception as exc:  # noqa: BLE001 - diagnostica per la UI
            return False, f"{type(exc).__name__}: {exc}", []

    def status(self) -> tuple[bool, str, list[str]]:
        """Stato del server e catalogo dei modelli in **una sola** richiesta.

        Stessa ragione della gemella su Ollama: ``ping`` e ``list_models``
        interrogavano lo stesso ``/v1/models``, e chiamarli in coppia --
        come faceva l'avvio della UI -- raddoppiava l'attesa, e con un
        endpoint spento raddoppiava i 4s di timeout, senza aggiungere niente.
        """
        return self._fetch_models()

    def ping(self) -> tuple[bool, str]:
        online, detail, _ = self._fetch_models()
        return online, detail

    def list_models(self) -> list[str]:
        return self._fetch_models()[2]

    def supports_tools(self, model: str) -> bool | None:  # noqa: ARG002
        return None

    def reasoning_control(self, params: GenParams) -> dict[str, Any]:
        """Translate explicit effort and known hybrid-model template switches.

        The generic endpoint cannot advertise universal boolean support. The
        diagnostic is intentionally unverified, and does no HTTP introspection.
        Chat Completions uses reasoning_effort; model-specific accepted levels
        remain the server's responsibility (including explicit 'max').
        https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create
        """
        think = params.think_payload
        payload: dict[str, Any] = {}
        support = "omitted" if think is None else "unknown"
        if isinstance(think, str):
            payload["reasoning_effort"] = think
            support = "effort_unverified"
        key = _known_thinking_key(params.model)
        if key is not None and think is not None:
            payload["chat_template_kwargs"] = {key: bool(think)}
            support = "template_unverified" if isinstance(think, bool) else "effort_template_unverified"
        return self._con_livelli_ammessi(params, payload, support)

    # -- livelli di pensiero ammessi ---------------------------------------

    def livelli_ammessi(self, model: str) -> tuple[str, ...]:
        """Quelli imparati da un errore di questo endpoint. Qui non si sonda
        niente: il generico non ha un ``/props`` da leggere."""
        return _LIVELLI_APPRESI.get((self.base_url, model), ())

    def _con_livelli_ammessi(
        self, params: GenParams, payload: dict[str, Any], support: str
    ) -> dict[str, Any]:
        """Applica la traduzione del livello e la scrive nel referto."""
        richiesto = _effort_nel_payload(payload)
        tradotto = _traduci_effort(payload, self.livelli_ammessi(params.model))
        report = _reasoning_report(params, tradotto, support)
        inviato = _effort_nel_payload(tradotto)
        if richiesto is not None and inviato != richiesto:
            report["translated"] = {"from": richiesto, "to": inviato}
        return report

    def livello_inviato(self, params: GenParams) -> Any:
        """Il livello che parte davvero, dopo la traduzione.

        Per la traccia ``think`` del messaggio: "configurato" e "usato" dicono
        cosa voleva l'harness, questo cosa ha ricevuto il template. ``None``
        vuol dire: nessun livello nel payload, decide il template.
        """
        think = params.think_payload
        if not isinstance(think, str):
            return think
        return _effort_nel_payload(self.reasoning_control(params)["payload"])

    def _impara_livelli(self, params: GenParams, payload: dict[str, Any], corpo: str) -> bool:
        """Ripiego quando il template non si e' potuto leggere: l'elenco lo
        da' il messaggio del 500 ("Supported types are ..."). Si impara e si
        riprova una volta sola; un errore diverso non tocca niente."""
        mandato = _effort_nel_payload(payload)
        ammessi = livelli_dal_testo(corpo)
        if mandato is None or not ammessi or mandato in ammessi:
            return False
        _LIVELLI_APPRESI[(self.base_url, params.model)] = ammessi
        return True

    # -- ganci per i dialetti (llama.cpp, vLLM, ...) -----------------------
    #
    # Esistono perche' l'alternativa era una seconda copia di ``stream``: la
    # differenza fra un endpoint compatibile e l'altro sta in due punti soli,
    # cosa si manda fuori standard e cosa si legge fuori standard.

    def _extra_body(self, params: GenParams) -> dict[str, Any]:
        """Campi fuori dallo standard OpenAI da mettere in ``extra_body``."""
        # vLLM legge questo campo; Ollama lo ignora (per quello esiste il
        # transport nativo qui sopra). Va a **tutti** gli endpoint compatibili,
        # OpenRouter compreso, che non lo conosce: gli endpoint che non lo
        # conoscono lo ignorano, ed e' il comportamento normale per un campo
        # extra. Resta perche' l'alternativa e' peggio: senza, su vLLM la
        # finestra torna quella del modello e l'harness tara i budget su un
        # numero che il server non rispetta -- lo stesso guasto silenzioso per
        # cui esiste ``clamp_num_ctx`` su llama.cpp. Se un giorno un endpoint
        # lo rifiuta con un 400, il posto dove toglierlo e' una sottoclasse
        # come ``LlamaCppBackend``, non questo metodo.
        return {"max_model_len": params.num_ctx, **self.reasoning_control(params)["payload"]}

    def _extra_usage(self, chunk: Any) -> dict[str, Any]:  # noqa: ARG002
        """Numeri fuori standard letti dal chunk. Qui non ce ne sono."""
        return {}

    def _openai_attempt(
        self,
        payload: dict[str, Any],
        should_stop: StopCheck,
        deadline: float,
    ) -> Iterator[StreamEvent]:
        """Parse SSE without an SDK hiding the mandatory end-of-stream sentinel."""
        buffers: dict[int, dict[str, str]] = {}
        extra_usage: dict[str, Any] = {}
        usage: dict[str, Any] = {}
        done_reason = ""
        with get_client().stream(
            "POST", f"{self.base_url}/chat/completions", json=payload,
            headers=self._auth_headers(), timeout=_request_timeout(deadline, self.timeout_s),

        ) as response:
            _http_status(response, should_stop, deadline)
            for data in _sse_data(_bounded_lines(response, should_stop, deadline)):
                _checkpoint(should_stop, deadline)
                if data == "[DONE]":
                    if not done_reason:
                        raise TransportProtocolError("Stream OpenAI senza finish_reason.")
                    completed_tools: list[StreamEvent] = []
                    identifiers: set[str] = set()
                    if done_reason in {"stop", "tool_calls", "function_call"}:
                        for index in sorted(buffers):
                            slot = buffers[index]
                            if not slot["name"]:
                                raise TransportProtocolError("Nome tool mancante.")
                            tool = _new_tool_call(
                                index, slot["id"], slot["name"], slot["arguments"],
                            )
                            if tool["id"] in identifiers:
                                raise TransportProtocolError("ID tool_call duplicato.")
                            identifiers.add(tool["id"])
                            completed_tools.append(StreamEvent("tool_call", tool_call=tool))
                    yield StreamEvent("usage", usage={
                        **usage, "done_reason": done_reason, **extra_usage,
                    })
                    yield from completed_tools
                    return
                chunk = loads_object(data)
                if chunk.get("error"):
                    raise TransportProtocolError("Il backend ha restituito un errore nello stream.")
                extra_usage.update(self._extra_usage(chunk))
                raw_usage = chunk.get("usage")
                if raw_usage is not None:
                    if not isinstance(raw_usage, dict):
                        raise TransportProtocolError("usage deve essere un oggetto.")
                    for field in ("prompt_tokens", "completion_tokens"):
                        value = raw_usage.get(field)
                        if value is not None:
                            usage[field] = _counter(value, field)
                    # Standard OpenAI (e OpenRouter, vLLM, llama.cpp recenti):
                    # quanti token del prompt il server ha preso dalla cache.
                    # E' l'unica misura diretta di quanto il prefisso regge.
                    dettagli = raw_usage.get("prompt_tokens_details")
                    if isinstance(dettagli, dict) and dettagli.get("cached_tokens") is not None:
                        usage["cached_tokens"] = _counter(
                            dettagli["cached_tokens"], "prompt_tokens_details.cached_tokens"
                        )
                choices = chunk.get("choices", [])
                if not isinstance(choices, list) or len(choices) > 1:
                    raise TransportProtocolError("Attesa una sola choice nello stream.")
                if not choices:
                    continue
                choice = choices[0]
                if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                    raise TransportProtocolError("Indice choice non valido.")
                if done_reason:
                    raise TransportProtocolError("Delta ricevuto dopo finish_reason.")
                finish = _text_field(choice.get("finish_reason"), "finish_reason", limit=128)
                delta = choice.get("delta")
                if delta is None:
                    delta = {}
                if not isinstance(delta, dict):
                    raise TransportProtocolError("delta deve essere un oggetto.")
                for field, kind in (("reasoning_content", "reasoning"),
                                    ("reasoning", "reasoning"), ("content", "content")):
                    value = _text_field(delta.get(field), field)
                    if value and not (field == "reasoning" and delta.get("reasoning_content")):
                        yield StreamEvent(kind, text=value)
                calls = delta.get("tool_calls")
                if calls is None:
                    calls = []
                if not isinstance(calls, list) or len(calls) > MAX_TOOL_CALLS:
                    raise TransportProtocolError("tool_calls deve essere una lista limitata.")
                for call in calls:
                    if not isinstance(call, dict):
                        raise TransportProtocolError("Envelope tool_call non valido.")
                    index = call.get("index")
                    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < MAX_TOOL_CALLS:
                        raise TransportProtocolError("Indice tool_call non valido.")
                    slot = buffers.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    identifier = _text_field(call.get("id"), "tool_call.id", limit=256)
                    if identifier:
                        if slot["id"] and slot["id"] != identifier:
                            raise TransportProtocolError("ID tool_call cambiato durante lo stream.")
                        slot["id"] = identifier
                    function = call.get("function")
                    if function is None:
                        function = {}
                    if not isinstance(function, dict):
                        raise TransportProtocolError("function deve essere un oggetto.")
                    for field, limit in (("name", 256), ("arguments", MAX_FRAME_BYTES)):
                        slot[field] += _text_field(function.get(field), field, limit=limit)
                        if len(slot[field]) > limit:
                            raise TransportProtocolError(f"Buffer tool {field} oltre il limite.")
                if finish:
                    done_reason = finish
            raise TransportProtocolError("Stream OpenAI incompleto: manca [DONE].")

    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        params: GenParams,
        *,
        should_stop: StopCheck = None,
    ) -> Iterator[StreamEvent]:
        """Stream validated SSE with pooled HTTP connections and bounded retries."""
        yield from _stream_with_retries(
            lambda deadline: self._tentativo(messages, tools, params, should_stop, deadline),
            self.timeout_s, should_stop,
        )

    def _payload(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        params: GenParams,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": params.model,
            "messages": messaggi_per_il_filo(messages),
            "temperature": params.temperature,
            "top_p": params.top_p,
            "max_tokens": params.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
            **self._extra_body(params),
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return payload

    def _tentativo(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        params: GenParams,
        should_stop: StopCheck,
        deadline: float,
    ) -> Iterator[StreamEvent]:
        """Una richiesta, piu' al massimo un secondo giro col livello tradotto.

        Il 500 del template arriva prima di qualunque byte di stream, quindi
        riprovare non duplica niente. Il secondo giro ricostruisce il payload:
        ``reasoning_control`` ora trova l'elenco imparato.
        """
        for giro in range(2):
            payload = self._payload(messages, tools, params)
            try:
                yield from self._openai_attempt(payload, should_stop, deadline)
                return
            except _HTTPFailure as exc:
                if giro == 0 and self._impara_livelli(params, payload, exc.body):
                    continue
                raise


class LlamaCppBackend(OpenAICompatBackend):
    """``llama-server`` di llama.cpp: OpenAI-compatibile, ma con ``/props``.

    Perche' non basta il transport ``openai`` generico -- tre motivi, tutti
    silenziosi, cioe' della specie peggiore:

    * **Il contesto non e' piu' un parametro della richiesta.** llama-server lo
      fissa al lancio con ``-c`` e ignora quello che gli mandi, senza dirlo:
      stessa classe di trappola di ``repeat_penalty`` su Ollama. Peggio -- se
      l'harness ne crede uno piu' grande di quello vero, il server fa *context
      shift* e riscrive la conversazione a meta' senza segnalare niente. Qui il
      numero vero si **legge** da ``/props`` (``server_num_ctx``) e chi lo usa
      si adegua: il server vince sull'impostazione.
    * **I sampler hanno altri nomi.** ``top_k`` e ``repeat_penalty`` non sono
      nello standard OpenAI: vanno in ``extra_body``, e sbagliarne il nome non
      da' errore -- da' una manopola finta.
    * **I tempi e i contatori del draft model.** Con ``timings_per_token``
      llama.cpp mette nello stream quanto ha impiegato e, quando gira con uno
      speculative decoder, quanti token il draft ha proposto e quanti ne sono
      stati accettati. E' l'unica misura onesta di quanto rende il draft, e
      arriva gratis dentro il turno invece che da un benchmark a parte.

    Nota su ``--jinja``: senza quel flag llama-server non emette tool call.
    Non lo dice; ``supports_tools`` che risponde ``False`` su un modello che i
    tool li sa usare vuol dire quasi sempre quello.
    """

    name = "llamacpp"

    # ``/props`` non cambia mentre il server e' vivo -- ma il server si
    # riavvia, ed e' proprio riavviandolo che si cambia ``-c``. Una cache
    # eterna farebbe credere all'harness una finestra che non esiste piu'.
    PROPS_TTL_S = 60.0
    # Quanto si aspetta prima di riprovare dopo un fallimento. Piu' corto del
    # TTL dei successi: un server che torna su deve essere visto in fretta,
    # ma non al prezzo di un timeout da quattro secondi ad ogni turno.
    PROPS_FALLIMENTO_TTL_S = 30.0

    # Nomi possibili dei contatori del draft. La README di llama.cpp non li
    # documenta e la PR che porta DFlash2 e' ancora aperta: si accettano gli
    # alias invece di scommettere su uno solo. Se un giorno non combacia
    # nessuno, la riga sparisce dal pannello -- non si inventa un numero.
    ALIAS_DRAFT_N = ("draft_n", "n_draft", "n_draft_total", "draft_n_total")
    ALIAS_DRAFT_OK = ("draft_n_accepted", "n_draft_accepted", "draft_accepted")

    def __init__(
        self, base_url: str, api_key: str = "", timeout_s: float = 180.0
    ) -> None:
        super().__init__(base_url, api_key, timeout_s)
        # ``self.base_url`` del padre finisce gia' per "/v1"; le rotte native
        # di llama.cpp (/props, /slots) stanno sulla radice.
        self.root_url = self.base_url[: -len("/v1")]
        self._props: dict[str, Any] = {}
        self._props_at: float = 0.0
        self._props_falliti_at: float = 0.0
        # Memo della lettura dei livelli: si rifa' solo se il template cambia.
        self._livelli_template: tuple[str, ...] = ()
        self._livelli_template_da: Any = None

    # -- introspezione ----------------------------------------------------

    def props(self, *, refresh: bool = False) -> dict[str, Any]:
        """``GET /props``, con una cache a scadenza. Non solleva mai.

        Memorizza anche i **fallimenti**, con un TTL piu' corto. Prima la cache
        teneva solo i successi: con llama-server spento ogni chiamata riprovava
        e aspettava il timeout, e ``clamp_num_ctx`` chiama ``props`` da
        ``gen_params()``, cioe' a ogni turno. Sono i quattro secondi che
        l'utente vede prima che parta qualsiasi cosa. E' la stessa correzione
        gia' fatta per ``model_info`` (``_MODEL_INFO_FALLITI``): qui mancava.
        """
        adesso = time.monotonic()
        fresca = self._props and (adesso - self._props_at) < self.PROPS_TTL_S
        if fresca and not refresh:
            return self._props
        if (
            not refresh
            and self._props_falliti_at
            and (adesso - self._props_falliti_at) < self.PROPS_FALLIMENTO_TTL_S
        ):
            # Ha appena fallito: non si riprova subito, si torna l'ultimo noto
            # (che all'avvio e' vuoto, ed e' l'informazione giusta).
            return self._props
        try:
            resp = get_client().get(
                f"{self.root_url}/props", headers=self._auth_headers(), timeout=4.0
            )
            resp.raise_for_status()
            dati = loads_object(resp.json())
        except Exception:  # noqa: BLE001 - diagnostica, non un errore di turno
            self._props_falliti_at = adesso
            return self._props      # meglio l'ultimo noto che niente
        self._props_falliti_at = 0.0
        if isinstance(dati, dict):
            self._props = dati
            self._props_at = adesso
        return self._props

    # Le tre sonde, in ordine di specificita'. La prima che risponde vince.
    #
    # ``/props`` resta in testa perche' e' anche il modo di **riconoscere**
    # llama.cpp fra gli endpoint compatibili: ``/v1/models`` risponde a tutti,
    # ``/props`` solo a lui -- ed e' su quella distinzione che ``auto`` sceglie
    # il dialetto. Ma "non risponde /props" non e' un buon modo di dichiarare
    # spento un server: certe build lo tengono dietro l'autenticazione, un
    # reverse proxy davanti puo' non inoltrarlo, e le build vecchie non ce
    # l'hanno affatto. Fermarsi li' voleva dire la goccia rossa su un server
    # che stava rispondendo alle generazioni -- il caso peggiore, perche' non
    # sembra un errore della UI: sembra che il server sia giu'.
    SONDE = (
        ("/props", "llama-server raggiungibile"),
        ("/health", "llama-server raggiungibile (/health)"),
        ("/v1/models", "endpoint raggiungibile (/v1/models)"),
    )

    def parla_llamacpp(self) -> bool:
        """C'e' davvero llama-server dietro questo indirizzo?

        Sonda **stretta**: solo ``/props``, che risponde soltanto a lui. E'
        una domanda diversa da "e' acceso?" e va tenuta separata, o ``auto``
        finirebbe a parlare il dialetto di llama.cpp a un vLLM qualunque --
        che a ``/health`` risponde volentieri. ``ping`` puo' permettersi di
        essere generoso perche' accende una goccia; questa no, perche' sceglie
        come si formano le richieste per tutta la sessione.
        """
        try:
            resp = get_client().get(
                f"{self.root_url}/props", headers=self._auth_headers(), timeout=4.0
            )
            resp.raise_for_status()
        except Exception:  # noqa: BLE001
            return False
        return True

    def ping(self) -> tuple[bool, str]:
        """Prova le sonde in ordine; il dettaglio dice **quale** ha risposto.

        Quando falliscono tutte, il dettaglio le elenca tutte e tre con il
        loro errore: e' l'unica riga che l'utente ha per capire se il server
        e' spento, se e' l'indirizzo sbagliato o se e' la chiave che manca.
        """
        errori: list[str] = []
        for rotta, detail in self.SONDE:
            try:
                resp = get_client().get(
                    f"{self.root_url}{rotta}", headers=self._auth_headers(), timeout=4.0
                )
                resp.raise_for_status()
                return True, detail
            except Exception as exc:  # noqa: BLE001
                errori.append(f"{rotta} {type(exc).__name__}")
        return False, "nessuna rotta risponde: " + ", ".join(errori)

    def status(self) -> tuple[bool, str, list[str]]:
        """Come il padre, ma passando dalla catena di sonde.

        Ereditare ``status`` dal generico avrebbe scavalcato ``ping``: la UI
        avrebbe di nuovo giudicato llama-server dal solo ``/v1/models``, che
        e' proprio la rotta che qui e' l'ultima scelta e non la prima.
        """
        online, detail = self.ping()
        return online, detail, (self.list_models() if online else [])

    def list_models(self) -> list[str]:
        """Il modello caricato, anche quando ``/v1/models`` non dice niente.

        llama-server ne serve **uno solo** e ignora il campo ``model`` della
        richiesta: qualunque nome funzionerebbe. Ma un elenco vuoto lascia
        l'utente a indovinare cosa scrivere nelle impostazioni -- e a chiedersi
        se debba mettere il modello grande o il draft (mai il draft: quello non
        e' indirizzabile, vive dentro il server). Meglio dire il nome vero,
        preso dal percorso del file in ``/props`` quando l'endpoint standard
        non risponde.
        """
        elenco = super().list_models()
        if elenco:
            return elenco
        percorso = str(self.props().get("model_path") or "")
        if not percorso:
            return []
        nome = percorso.replace("\\", "/").rsplit("/", 1)[-1]
        if nome.lower().endswith(".gguf"):
            nome = nome[: -len(".gguf")]
        return [nome] if nome else []

    def server_num_ctx(self) -> int | None:
        """Finestra **vera** del server, o ``None`` se non la dichiara."""
        props = self.props()
        gen = props.get("default_generation_settings")
        for sorgente in (gen if isinstance(gen, dict) else {}, props):
            valore = sorgente.get("n_ctx")
            if type(valore) is int and valore > 0:
                return valore
        # Ripiego: /slots dichiara n_ctx per slot. Esiste solo se il server e'
        # partito con --slots, quindi puo' mancare senza che sia un problema.
        try:
            resp = get_client().get(
                f"{self.root_url}/slots",
                headers=self._auth_headers(),   # come /props: stessa regola
                timeout=4.0,
            )
            resp.raise_for_status()
            slots = resp.json()
        except Exception:  # noqa: BLE001
            return None
        if isinstance(slots, list) and slots and isinstance(slots[0], dict):
            valore = slots[0].get("n_ctx")
            if type(valore) is int and valore > 0:
                return valore
        return None

    def clamp_num_ctx(self, richiesto: int) -> int:
        """Il minimo fra quello che si vuole e quello che il server ha.

        Chiedere di piu' non allarga niente: fa solo credere all'harness di
        avere spazio che non c'e', e i budget di troncamento si tarano su un
        numero falso.
        """
        vero = self.server_num_ctx()
        if vero and richiesto > vero:
            return vero
        return richiesto

    def _caps(self) -> dict[str, Any]:
        caps = self.props().get("chat_template_caps")
        return caps if isinstance(caps, dict) else {}

    def supports_tools(self, model: str) -> bool | None:  # noqa: ARG002
        """``False`` qui vuol dire quasi sempre: manca ``--jinja``."""
        segnali = [v for k, v in self._caps().items() if "tool" in k.lower()]
        if segnali:
            return any(bool(v) for v in segnali)
        template = str(self.props().get("chat_template", ""))
        if template:
            return True if "tools" in template.lower() else None
        return None

    def supports_thinking(self, model: str) -> bool | None:  # noqa: ARG002
        """Rimpiazza la capability ``thinking`` di ``/api/show`` di Ollama.

        Serve a ``native_think: auto``, che senza questo metodo non rilevava
        piu' niente sul transport compatibile e restava spento per sempre.
        """
        chiavi = ("reason", "think")
        segnali = [
            v
            for k, v in self._caps().items()
            if any(c in k.lower() for c in chiavi)
        ]
        if segnali:
            return any(bool(v) for v in segnali)
        template = str(self.props().get("chat_template", ""))
        if template:
            return True if "think" in template.lower() else None
        return None

    # -- dialetto ---------------------------------------------------------

    def reasoning_control(self, params: GenParams) -> dict[str, Any]:
        """Use the template already fetched by context/capability inspection.

        No network request in the generation hot path. /props is populated by
        the normal clamp_num_ctx/supports_thinking calls; absent metadata stays
        unknown. llama.cpp documents top-level 'none' to disable reasoning,
        but other top-level effort values can be ignored. Send effort inside
        chat_template_kwargs only when the cached template references it.
        https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md
        """
        think = params.think_payload
        if think is None:
            return _reasoning_report(params, {}, "omitted")
        template = self._props.get("chat_template")
        keys = _template_reasoning_keys(template) if isinstance(template, str) else set()
        kwargs: dict[str, Any] = {}
        # Prefer the actual server template over model-name heuristics. Both
        # variables can occur in a template and are then explicit controls.
        for key in ("enable_thinking", "thinking"):
            if key in keys:
                kwargs[key] = bool(think)
        body: dict[str, Any] = {}
        if think is False:
            body["reasoning_effort"] = "none"
            support = "disable_unverified"
        elif isinstance(think, str) and "reasoning_effort" in keys:
            kwargs["reasoning_effort"] = think
            support = "template_unverified"
            if self.livelli_ammessi(params.model):
                support = "template_levels"
        elif kwargs:
            support = "boolean_only" if isinstance(think, str) else "template_unverified"
        else:
            support = "unknown"
        if kwargs:
            body["chat_template_kwargs"] = kwargs
        return self._con_livelli_ammessi(params, body, support)

    def livelli_ammessi(self, model: str) -> tuple[str, ...]:
        """Letti dal template di ``/props``; se non li dichiara, quelli
        imparati da un 500. Nessuna richiesta di rete: ``_props`` lo riempiono
        ``clamp_num_ctx``/``supports_thinking`` a ogni turno, come per le
        chiavi del template qui sopra."""
        template = self._props.get("chat_template")
        if template is not self._livelli_template_da:
            self._livelli_template = livelli_dal_template(template)
            self._livelli_template_da = template
        return self._livelli_template or super().livelli_ammessi(model)

    def _extra_body(self, params: GenParams) -> dict[str, Any]:
        """Sampler nel dialetto di llama.cpp.

        ``max_model_len`` non c'e' apposta: qui il contesto lo comanda la riga
        di lancio del server, e mandarlo sarebbe esattamente la manopola finta
        descritta sopra.
        """
        body: dict[str, Any] = {
            "top_k": int(params.top_k),
            "repeat_penalty": float(params.repetition_penalty),
            # Fa arrivare i timings -- e con essi i contatori del draft --
            # dentro lo stream, invece di doverli chiedere a /slots dopo.
            "timings_per_token": True,
            # llama-server tiene le chiamate multiple **spente** se non glielo
            # si chiede (docs/function-calling.md: "disabled by default"),
            # mentre il prompt e il ciclo le trattano come il comportamento
            # buono -- cinque read_file in un passo invece che in cinque. Con
            # la grammatica pigra attiva, spente vuol dire che il modello non
            # puo' emetterne una seconda. Il ciclo ne regge fino a
            # ``MAX_TOOL_CALLS_PER_STEP``.
            "parallel_tool_calls": True,
            **self.reasoning_control(params)["payload"],
        }

        # Fuori si chiama repetition_penalty, sul filo di llama.cpp
        # repeat_penalty: stesso cambio di nome che c'e' su Ollama. Anche
        # 1.0 va inviato: ometterlo conserverebbe il default del server.
        if params.presence_penalty:
            body["presence_penalty"] = float(params.presence_penalty)
        if params.seed is not None:
            body["seed"] = int(params.seed)
        if params.stop:
            body["stop"] = list(params.stop)
        return body

    @staticmethod
    def _timings(chunk: Any) -> dict[str, Any]:
        """Il blocco ``timings``, che nello standard OpenAI non esiste."""
        grezzo = chunk.get("timings") if isinstance(chunk, dict) else getattr(chunk, "timings", None)
        if grezzo is None:
            extra = getattr(chunk, "model_extra", None) or {}
            grezzo = extra.get("timings")
        if hasattr(grezzo, "model_dump"):
            grezzo = grezzo.model_dump()
        return grezzo if isinstance(grezzo, dict) else {}

    def _extra_usage(self, chunk: Any) -> dict[str, Any]:
        """Tempi e contatori del draft, nei nomi che il pannello gia' usa.

        I tempi valgono da soli: sul transport compatibile ``eval_ms`` non
        arrivava mai, e la riga "Velocita'" mostrava 0,0 tok/s su qualunque
        turno. Qui il numero c'e' davvero, ed e' quello che serve per
        confrontare una run con draft e una senza **dall'harness**, senza
        montare un benchmark a parte.
        """
        t = self._timings(chunk)
        if not t:
            return {}
        fuori: dict[str, Any] = {}
        prompt_ms = t.get("prompt_ms")
        eval_ms = t.get("predicted_ms")
        if prompt_ms is not None:
            prompt_ms = _counter(prompt_ms, "timings.prompt_ms")
            fuori["prompt_eval_ms"] = round(prompt_ms)
        if eval_ms is not None:
            eval_ms = _counter(eval_ms, "timings.predicted_ms")
            fuori["eval_ms"] = round(eval_ms)
        if prompt_ms is not None and eval_ms is not None:
            fuori["total_ms"] = round(prompt_ms + eval_ms)
        # ``cache_n``: token del prompt riusati dal KV cache dello slot;
        # ``prompt_n``: token del prompt davvero calcolati in questa richiesta.
        # La loro somma e' il prompt intero. Sono i due numeri che dicono se
        # il prefisso regge davvero, passo per passo, invece di stimarlo.
        for chiave, nome in (("cached_tokens", "cache_n"),
                             ("prompt_processed_tokens", "prompt_n")):
            valore = t.get(nome)
            if valore is not None:
                fuori[chiave] = int(_counter(valore, f"timings.{nome}"))
        coppie = (
            ("draft_n", self.ALIAS_DRAFT_N),
            ("draft_accepted", self.ALIAS_DRAFT_OK),
        )

        for chiave, alias in coppie:
            for nome in alias:
                valore = t.get(nome)
                if valore is not None:
                    fuori[chiave] = int(_counter(valore, f"timings.{nome}"))
                    break
        return fuori


_STREAM_TOOLS_MODES = {"auto": None, "sempre": True, "mai": False}


def build_backend(
    *,
    transport: str,
    base_url: str,
    api_key: str,
    timeout_s: float,
    stream_tools: str = "auto",
) -> OllamaBackend | OpenAICompatBackend:
    """Seleziona il transport.

    ``auto`` prova Ollama, poi llama.cpp, poi il generico compatibile. La
    sonda di mezzo esiste perche' ``/v1/models`` risponde a tutti mentre
    ``/props`` risponde solo a llama-server: senza quel passaggio si finirebbe
    a parlargli il dialetto sbagliato -- niente ``top_k``, niente timings, e
    un ``num_ctx`` creduto invece che letto.
    """
    transport = (transport or "auto").lower()
    swt = _STREAM_TOOLS_MODES.get(stream_tools)

    if transport == "ollama":
        return OllamaBackend(base_url, timeout_s, stream_with_tools=swt)
    if transport == "llamacpp":
        return LlamaCppBackend(base_url, api_key, timeout_s)
    if transport == "openai":
        return OpenAICompatBackend(base_url, api_key, timeout_s)

    candidate = OllamaBackend(base_url, timeout_s, stream_with_tools=swt)
    ok, _ = candidate.ping()
    if ok:
        return candidate
    llama = LlamaCppBackend(base_url, api_key, timeout_s)
    # ``parla_llamacpp`` e non ``ping``: qui la domanda e' "chi sei", non "ci
    # sei". ``ping`` risponde di si' anche a un vLLM raggiungibile, e sceglierlo
    # qui vorrebbe dire mandargli i sampler nel dialetto sbagliato.
    if llama.parla_llamacpp():
        return llama
    return OpenAICompatBackend(base_url, api_key, timeout_s)
