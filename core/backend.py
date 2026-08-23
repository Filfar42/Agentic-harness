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
from dataclasses import dataclass
from typing import Any, Literal
from collections.abc import Iterator

import httpx

from .config import GenParams

EventKind = Literal["content", "reasoning", "tool_call", "usage", "error"]


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


# ---------------------------------------------------------------------------
# Conversione dei messaggi
# ---------------------------------------------------------------------------


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
                if isinstance(raw_args, str):
                    try:
                        parsed = json.loads(raw_args or "{}")
                    except json.JSONDecodeError:
                        parsed = {"_raw": raw_args}
                else:
                    parsed = raw_args
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


def forget_model_info() -> None:
    """Svuota la cache delle capability: da chiamare quando l'utente cambia
    endpoint o modello, o quando ri-lancia la sonda a mano."""
    _MODEL_INFO_CACHE.clear()


class OllamaBackend:
    """Transport nativo ``/api/chat``: options e keep_alive funzionano davvero."""

    name = "ollama"

    def __init__(
        self,
        base_url: str,
        timeout_s: float = 180.0,
        *,
        stream_with_tools: bool | None = None,
    ) -> None:
        self.base_url = normalise_base_url(base_url)
        self.timeout_s = float(timeout_s)
        # None = decidi dalla versione del server.
        self._stream_with_tools = stream_with_tools
        self._version_cache: str | None = None

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
            resp = httpx.get(f"{self.base_url}/api/version", timeout=4.0)
            resp.raise_for_status()
            self._version_cache = str(resp.json().get("version", ""))
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
            resp = httpx.get(f"{self.base_url}/api/tags", timeout=4.0)
            resp.raise_for_status()
            models = sorted(m.get("name", "") for m in resp.json().get("models", []))
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
            resp = httpx.get(f"{self.base_url}/api/ps", timeout=4.0)
            resp.raise_for_status()
            models = resp.json().get("models", [])
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
        try:
            resp = httpx.post(
                f"{self.base_url}/api/show", json={"model": model}, timeout=6.0
            )
            resp.raise_for_status()
            info = resp.json()
        except Exception:  # noqa: BLE001
            return {}       # gli errori non si memorizzano: al prossimo giro riprova
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
        return "tools" in template.lower() or None

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
            resp = httpx.post(
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
            "messages": to_ollama_messages(messages),
            "stream": stream,
            "options": params.ollama_options(),
            "keep_alive": params.keep_alive,
        }
        if tools:
            payload["tools"] = tools
        think = params.think_payload
        if think:
            payload["think"] = think
        return payload

    def _emit_message(
        self, message: dict[str, Any], tool_index: int
    ) -> tuple[list[StreamEvent], int]:
        """Traduce un ``message`` di Ollama in eventi normalizzati."""
        events: list[StreamEvent] = []
        if message.get("thinking"):
            events.append(StreamEvent("reasoning", text=message["thinking"]))
        if message.get("content"):
            events.append(StreamEvent("content", text=message["content"]))
        for call in message.get("tool_calls") or []:
            fn = call.get("function", {})
            args = fn.get("arguments", {})
            events.append(
                StreamEvent(
                    "tool_call",
                    tool_call=_new_tool_call(
                        tool_index,
                        call.get("id", "") or f"ollama_{tool_index}",
                        fn.get("name", ""),
                        args
                        if isinstance(args, str)
                        else json.dumps(args, ensure_ascii=False),
                    ),
                )
            )
            tool_index += 1
        return events, tool_index

    @staticmethod
    def _usage_event(chunk: dict[str, Any]) -> StreamEvent:
        return StreamEvent(
            "usage",
            usage={
                "prompt_tokens": chunk.get("prompt_eval_count", 0),
                "completion_tokens": chunk.get("eval_count", 0),
                "prompt_eval_ms": round(chunk.get("prompt_eval_duration", 0) / 1e6),
                "eval_ms": round(chunk.get("eval_duration", 0) / 1e6),
                "total_ms": round(chunk.get("total_duration", 0) / 1e6),
                "done_reason": chunk.get("done_reason", ""),
            },
        )

    def _blocking_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        params: GenParams,
    ) -> Iterator[StreamEvent]:
        """Richiesta non-streaming, usata quando il server non streamma i tool.

        Si perde l'effetto macchina-da-scrivere ma le tool call arrivano. Su
        Ollama < 0.8.0 e' l'unico modo per ottenerle.
        """
        payload = self.build_payload(messages, tools, params, stream=False)
        try:
            resp = httpx.post(
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=httpx.Timeout(self.timeout_s, connect=10.0),
            )
            if resp.status_code >= 400:
                yield StreamEvent(
                    "error", text=f"HTTP {resp.status_code}: {resp.text[:600]}"
                )
                return
            chunk = resp.json()
        except httpx.TimeoutException:
            yield StreamEvent("error", text=f"Timeout dopo {self.timeout_s:.0f}s.")
            return
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            yield StreamEvent("error", text=f"Errore verso Ollama: {exc}")
            return

        if chunk.get("error"):
            yield StreamEvent("error", text=str(chunk["error"]))
            return

        events, _ = self._emit_message(chunk.get("message") or {}, 0)
        yield from events
        yield self._usage_event(chunk)

    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        params: GenParams,
    ) -> Iterator[StreamEvent]:
        if tools and not self.streams_tool_calls():
            yield from self._blocking_chat(messages, tools, params)
            return

        payload = self.build_payload(messages, tools, params, stream=True)

        tool_index = 0
        try:
            with httpx.stream(
                "POST",
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=httpx.Timeout(self.timeout_s, connect=10.0),
            ) as resp:
                if resp.status_code >= 400:
                    body = resp.read().decode("utf-8", "replace")[:600]
                    yield StreamEvent("error", text=f"HTTP {resp.status_code}: {body}")
                    return

                for line in resp.iter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if chunk.get("error"):
                        yield StreamEvent("error", text=str(chunk["error"]))
                        return

                    events, tool_index = self._emit_message(
                        chunk.get("message") or {}, tool_index
                    )
                    yield from events

                    if chunk.get("done"):
                        yield self._usage_event(chunk)
        except httpx.TimeoutException:
            yield StreamEvent(
                "error",
                text=(
                    f"Timeout dopo {self.timeout_s:.0f}s. Il modello e' probabilmente "
                    "in fase di caricamento in VRAM: aumenta il timeout o alza keep_alive."
                ),
            )
        except httpx.HTTPError as exc:
            yield StreamEvent("error", text=f"Errore di rete verso Ollama: {exc}")

    # -- diagnostica -------------------------------------------------------


class OpenAICompatBackend:
    """Fallback ``/v1/chat/completions`` per vLLM, LM Studio, llama.cpp server."""

    name = "openai"

    def __init__(self, base_url: str, api_key: str, timeout_s: float = 180.0) -> None:
        base = normalise_base_url(base_url)
        self.base_url = f"{base}/v1"
        self.api_key = api_key or "not-needed"
        self.timeout_s = float(timeout_s)

    def ping(self) -> tuple[bool, str]:
        try:
            resp = httpx.get(
                f"{self.base_url}/models",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=4.0,
            )
            resp.raise_for_status()
            return True, "endpoint raggiungibile"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    def list_models(self) -> list[str]:
        try:
            resp = httpx.get(
                f"{self.base_url}/models",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=4.0,
            )
            resp.raise_for_status()
            return sorted(m.get("id", "") for m in resp.json().get("data", []))
        except Exception:  # noqa: BLE001
            return []

    def supports_tools(self, model: str) -> bool | None:  # noqa: ARG002
        return None

    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        params: GenParams,
    ) -> Iterator[StreamEvent]:
        from openai import OpenAI  # import locale: avvio dell'app piu' rapido

        client = OpenAI(
            base_url=self.base_url, api_key=self.api_key, timeout=self.timeout_s
        )
        kwargs: dict[str, Any] = {
            "model": params.model,
            "messages": messages,
            "temperature": params.temperature,
            "top_p": params.top_p,
            "max_tokens": params.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
            # vLLM legge questi campi; Ollama li ignora (per quello esiste il
            # transport nativo qui sopra).
            "extra_body": {"max_model_len": params.num_ctx},
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        buffers: dict[int, dict[str, str]] = {}
        try:
            stream = client.chat.completions.create(**kwargs)
            for chunk in stream:
                if getattr(chunk, "usage", None):
                    usage = chunk.usage
                    yield StreamEvent(
                        "usage",
                        usage={
                            "prompt_tokens": getattr(usage, "prompt_tokens", 0),
                            "completion_tokens": getattr(usage, "completion_tokens", 0),
                        },
                    )
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta

                reasoning = getattr(delta, "reasoning_content", None) or getattr(
                    delta, "reasoning", None
                )
                if reasoning:
                    yield StreamEvent("reasoning", text=str(reasoning))

                if delta.content:
                    yield StreamEvent("content", text=delta.content)

                for tcd in delta.tool_calls or []:
                    idx = tcd.index or 0
                    slot = buffers.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                    if tcd.id:
                        slot["id"] = tcd.id
                    if tcd.function and tcd.function.name:
                        slot["name"] += tcd.function.name
                    if tcd.function and tcd.function.arguments:
                        slot["arguments"] += tcd.function.arguments

            for idx in sorted(buffers):
                slot = buffers[idx]
                if slot["name"]:
                    yield StreamEvent(
                        "tool_call",
                        tool_call=_new_tool_call(
                            idx, slot["id"], slot["name"], slot["arguments"] or "{}"
                        ),
                    )
        except Exception as exc:  # noqa: BLE001
            yield StreamEvent("error", text=f"{type(exc).__name__}: {exc}")


_STREAM_TOOLS_MODES = {"auto": None, "sempre": True, "mai": False}


def build_backend(
    *,
    transport: str,
    base_url: str,
    api_key: str,
    timeout_s: float,
    stream_tools: str = "auto",
) -> OllamaBackend | OpenAICompatBackend:
    """Seleziona il transport. ``auto`` prova Ollama e ripiega su OpenAI."""
    transport = (transport or "auto").lower()
    swt = _STREAM_TOOLS_MODES.get(stream_tools)

    if transport == "ollama":
        return OllamaBackend(base_url, timeout_s, stream_with_tools=swt)
    if transport == "openai":
        return OpenAICompatBackend(base_url, api_key, timeout_s)

    candidate = OllamaBackend(base_url, timeout_s, stream_with_tools=swt)
    ok, _ = candidate.ping()
    if ok:
        return candidate
    return OpenAICompatBackend(base_url, api_key, timeout_s)
