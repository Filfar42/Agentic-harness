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
import time
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
        # I livelli di pensiero ("low"/"medium"/"high"/"max") li accettano solo
        # le versioni recenti di Ollama; le altre rifiutano la richiesta con un
        # 400. None = non si sa ancora e si prova; False = provato e rifiutato,
        # da qui in poi si manda il booleano. Si impara dal server invece di
        # dichiararlo con una tabella di versioni che invecchia da sola.
        self._think_levels_ok: bool | None = None

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
            fallito = _MODEL_INFO_FALLITI.get(key)
            if fallito is not None and time.monotonic() - fallito < MODEL_INFO_FALLIMENTO_TTL_S:
                # Ha appena fallito: non si ripaga il timeout adesso.
                return {}
        try:
            resp = httpx.post(
                f"{self.base_url}/api/show", json={"model": model}, timeout=6.0
            )
            resp.raise_for_status()
            info = resp.json()
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
            # Su un server che ha gia' rifiutato i livelli si manda il
            # booleano: il pensiero resta acceso, si perde solo la manopola.
            if isinstance(think, str) and self._think_levels_ok is False:
                think = True
            payload["think"] = think
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
                if self._livello_rifiutato(payload, resp.text[:600]):
                    yield from self._blocking_chat(messages, tools, params)
                    return
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
                    # Un server troppo vecchio per i livelli di pensiero: si
                    # riprova subito col booleano invece di far fallire il
                    # passo. Una volta sola e non di piu': ``_livello_rifiutato``
                    # ha appena messo il flag a False, quindi il payload del
                    # secondo giro non ha piu' una stringa e la condizione non
                    # puo' ripresentarsi.
                    if self._livello_rifiutato(payload, body):
                        yield from self.stream(messages, tools, params)
                        return
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
            resp = httpx.get(
                f"{self.base_url}/models", headers=self._auth_headers(), timeout=4.0
            )
            resp.raise_for_status()
            models = sorted(m.get("id", "") for m in resp.json().get("data", []))
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

    # -- ganci per i dialetti (llama.cpp, vLLM, ...) -----------------------
    #
    # Esistono perche' l'alternativa era una seconda copia di ``stream``: la
    # differenza fra un endpoint compatibile e l'altro sta in due punti soli,
    # cosa si manda fuori standard e cosa si legge fuori standard.

    def _extra_body(self, params: GenParams) -> dict[str, Any]:
        """Campi fuori dallo standard OpenAI da mettere in ``extra_body``."""
        # vLLM legge questo campo; Ollama lo ignora (per quello esiste il
        # transport nativo qui sopra).
        return {"max_model_len": params.num_ctx}

    def _extra_usage(self, chunk: Any) -> dict[str, Any]:  # noqa: ARG002
        """Numeri fuori standard letti dal chunk. Qui non ce ne sono."""
        return {}

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
            "extra_body": self._extra_body(params),
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        buffers: dict[int, dict[str, str]] = {}
        fuori_standard: dict[str, Any] = {}
        usage_emesso = False
        # Perche' la generazione si e' fermata. Nello standard OpenAI e'
        # ``finish_reason``, su Ollama ``done_reason``: qui si traduce nel
        # secondo, che e' il nome con cui il ciclo agentico lo conosce gia'.
        #
        # Nessuno lo leggeva. Una generazione tagliata a meta' -- tetto di
        # ``max_tokens`` raggiunto, o finestra del server esaurita -- arrivava
        # quindi indistinguibile da una finita bene, e se il taglio cadeva
        # dentro gli argomenti di una tool call il modello si prendeva la colpa
        # con un "Argomenti JSON malformati" che non descriveva niente di
        # quello che era successo.
        done_reason = ""
        try:
            stream = client.chat.completions.create(**kwargs)
            for chunk in stream:
                # I numeri fuori standard si leggono **prima** dell'usage: su
                # llama.cpp i timings viaggiano nello stesso chunk finale e
                # devono poter entrare nello stesso evento.
                fuori_standard.update(self._extra_usage(chunk))
                # ...e cosi' il motivo dello stop: arriva sull'ultimo chunk con
                # un ``choices`` pieno, mentre ``usage`` arriva su quello dopo,
                # che di ``choices`` non ne ha. Leggerlo solo dentro il ramo
                # dell'usage vorrebbe dire non leggerlo mai.
                scelte = getattr(chunk, "choices", None) or []
                if scelte and getattr(scelte[0], "finish_reason", None):
                    done_reason = str(scelte[0].finish_reason)
                if getattr(chunk, "usage", None):
                    usage = chunk.usage
                    usage_emesso = True
                    yield StreamEvent(
                        "usage",
                        usage={
                            "prompt_tokens": getattr(usage, "prompt_tokens", 0),
                            "completion_tokens": getattr(usage, "completion_tokens", 0),
                            "done_reason": done_reason,
                            **fuori_standard,
                        },
                    )
                if not scelte:
                    continue
                delta = scelte[0].delta

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
            # Un server che non manda ``usage`` non deve far sparire anche i
            # numeri che ha mandato: i tempi e i contatori del draft valgono
            # da soli, ed e' su quelli che si decide se lo speculative
            # decoding sta rendendo.
            if not usage_emesso and (fuori_standard or done_reason):
                yield StreamEvent(
                    "usage", usage={"done_reason": done_reason, **fuori_standard}
                )
        except Exception as exc:  # noqa: BLE001
            yield StreamEvent("error", text=f"{type(exc).__name__}: {exc}")


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
            resp = httpx.get(
                f"{self.root_url}/props", headers=self._auth_headers(), timeout=4.0
            )
            resp.raise_for_status()
            dati = resp.json()
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
            resp = httpx.get(
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
                resp = httpx.get(
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
            if isinstance(valore, int) and valore > 0:
                return valore
        # Ripiego: /slots dichiara n_ctx per slot. Esiste solo se il server e'
        # partito con --slots, quindi puo' mancare senza che sia un problema.
        try:
            resp = httpx.get(
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
            if isinstance(valore, int) and valore > 0:
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
            return "tools" in template.lower() or None
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
            return "think" in template.lower() or None
        return None

    # -- dialetto ---------------------------------------------------------

    def _extra_body(self, params: GenParams) -> dict[str, Any]:
        """Sampler nel dialetto di llama.cpp.

        ``max_model_len`` non c'e' apposta: qui il contesto lo comanda la riga
        di lancio del server, e mandarlo sarebbe esattamente la manopola finta
        descritta sopra.
        """
        body: dict[str, Any] = {
            "top_k": int(params.top_k),
            # Fa arrivare i timings -- e con essi i contatori del draft --
            # dentro lo stream, invece di doverli chiedere a /slots dopo.
            "timings_per_token": True,
        }
        # Fuori si chiama repetition_penalty, sul filo di llama.cpp
        # repeat_penalty: stesso cambio di nome che c'e' su Ollama. 1.0 e' il
        # neutro e non si manda.
        if params.repetition_penalty != 1.0:
            body["repeat_penalty"] = float(params.repetition_penalty)
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
        grezzo = getattr(chunk, "timings", None)
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
        if isinstance(prompt_ms, (int, float)):
            fuori["prompt_eval_ms"] = round(prompt_ms)
        if isinstance(eval_ms, (int, float)):
            fuori["eval_ms"] = round(eval_ms)
        if isinstance(prompt_ms, (int, float)) and isinstance(eval_ms, (int, float)):
            fuori["total_ms"] = round(prompt_ms + eval_ms)
        coppie = (
            ("draft_n", self.ALIAS_DRAFT_N),
            ("draft_accepted", self.ALIAS_DRAFT_OK),
        )
        for chiave, alias in coppie:
            for nome in alias:
                valore = t.get(nome)
                if isinstance(valore, (int, float)):
                    fuori[chiave] = int(valore)
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
