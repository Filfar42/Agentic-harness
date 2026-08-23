"""Backend HTTP dell'harness: FastAPI + Server-Sent Events.

    python run.py            # http://127.0.0.1:8123

La decisione portante: **il turno non vive dentro la richiesta HTTP**.

    POST /api/chat        avvia il turno e ritorna subito
    GET  /api/stream/{id} si attacca al turno (arretrato + flusso dal vivo)

Il turno e' un lavoro in background legato al suo ``session_id``
(``server/runner.py``). Il browser puo' staccarsi, cambiare conversazione,
ricaricare la pagina: il lavoro prosegue e riattaccandosi si rivede tutto,
pensiero e tool compresi.

``core/`` resta indipendente dal livello di presentazione: qui si consumano gli
eventi del ciclo agentico e si serializzano, niente di piu'.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import platform
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.datastructures import MutableHeaders

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import agent as agent_mod  # noqa: E402
from core import memory as memory_mod  # noqa: E402
from core import notes as notes_mod  # noqa: E402
from core import skills as skills_mod  # noqa: E402
from core import plan as plan_mod  # noqa: E402
from core import profiles  # noqa: E402
from core import sandbox as sandbox_mod  # noqa: E402
from core import session as session_mod  # noqa: E402
from core import settings as settings_mod  # noqa: E402
from core.backend import (  # noqa: E402
    build_backend,
    forget_model_info,
    normalise_base_url,
)
from core.config import (  # noqa: E402
    SKILLS_DIR,
    SKILLS_SUBDIR_WORKSPACE,
    APP_NAME,
    APP_VERSION,
    GenParams,
    resolve_think,
    resolve_tristate,
)
from core.prompts import (  # noqa: E402
    SYSTEM_PROMPT,
    build_attachments_block,
    build_env_header,
    build_system_prompt,
    is_stock_prompt,
    pick_system_prompt,
)
from core.textutils import estimate_messages_tokens, estimate_tokens  # noqa: E402
from core.tools import (  # noqa: E402
    ATTACHMENTS_DIR,
    TOOLS_SCHEMA,
    TOOLS_SCHEMA_LEAN,
    ToolContext,
    WorkspaceError,
    is_image,
    load_images_b64,
    resolve_path,
    store_attachment,
)
from server.nativedialog import DialogUnavailable, pick_folder  # noqa: E402
from server.prep import Prep, image_needed  # noqa: E402
from server.runner import RunnerRegistry, TurnRunner  # noqa: E402

WEB_DIR = Path(__file__).resolve().parents[1] / "web"
# Il conto dei token degli schemi dipende da quale versione si sta mandando.
TOOL_SCHEMA_TOKENS_FULL = estimate_tokens(json.dumps(TOOLS_SCHEMA))
TOOL_SCHEMA_TOKENS_LEAN = estimate_tokens(json.dumps(TOOLS_SCHEMA_LEAN))

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """All'avvio: accendi Docker se serve e se l'utente lo vuole.

    Le funzioni sono definite piu' sotto e si risolvono al momento della
    chiamata, quando ``STATE`` e ``PREP`` esistono gia'. Il lavoro vero sta in
    un thread: il server deve poter rispondere subito, mentre Docker Desktop
    impiega dai venti ai sessanta secondi ad alzarsi, e chi guarda la pagina
    vede la spia passare da "avvio in corso" a "pronto" da sola.
    """
    # La cartella corrente entra fra i recenti anche se non l'ha appena scelta
    # nessuno: al primo avvio l'elenco e' vuoto, e una tendina "Recenti" che si
    # apre su niente sembra rotta.
    remember_workspace()
    autostart_docker()
    yield


app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan)
RUNNERS = RunnerRegistry()
# Lavori di preparazione dell'ambiente (accendere Docker, costruire
# l'immagine). Sono dell'applicazione, non di una conversazione.
PREP = Prep()


class ServerTiming:
    """Quanto ci ha messo il server, misurato dal server.

    Serve a distinguere i tre pezzi di una lentezza percepita: se qui il numero
    e' piccolo ma in DevTools la richiesta dura molto, il tempo se ne va nella
    rete o nel rendering del browser, e cercarlo nel backend e' fatica sprecata.
    Visibile in Chrome sotto Network -> la richiesta -> Timing.

    **Middleware ASGI puro, non ``@app.middleware("http")``.** Quel decoratore
    monta ``BaseHTTPMiddleware``, che per esporre un oggetto ``Response`` al
    codice utente si mette in mezzo al corpo della risposta e lo ri-emette. Se
    il browser stacca a meta' -- si chiude la scheda, si ricarica la pagina
    mentre una POST e' in volo, si cambia conversazione mentre lo stream SSE e'
    aperto -- quel rimbalzo chiude comunque il corpo con un frammento vuoto, e
    uvicorn si ritrova meno byte di quanti ne aveva annunciati::

        RuntimeError: Response content shorter than Content-Length

    Il traceback finiva nel log ad ogni disconnessione, lungo venti righe di
    stack dentro starlette, e sembrava un errore dell'applicazione: non lo era,
    ma per saperlo bisognava leggerlo tutto. Qui il corpo non lo tocca nessuno:
    si aggiunge un'intestazione al momento in cui parte ``http.response.start``
    e per il resto la richiesta passa liscia.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or not scope.get("path", "").startswith("/api/"):
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                elapsed = (time.perf_counter() - started) * 1000
                MutableHeaders(scope=message).append(
                    "Server-Timing", f"app;dur={elapsed:.1f}"
                )
            await send(message)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(ServerTiming)


# ---------------------------------------------------------------------------
# Stato del processo
# ---------------------------------------------------------------------------


class AppState:
    """Impostazioni, memorie e conversazioni caricate.

    Le conversazioni sono indirizzate per id, non c'e' piu' una "sessione
    corrente" lato server: e' il client a dire su quale sta lavorando. Cosi'
    un turno in corso su A non viene disturbato se la UI passa a guardare B.
    """

    def __init__(self) -> None:
        # Default con sopra le scelte dell'ultimo avvio. Il system prompt entra
        # nel file solo se l'utente l'ha riscritto: vedi core/settings.py.
        self.settings: dict[str, Any] = settings_mod.load_settings()
        self.settings.setdefault("system_prompt", SYSTEM_PROMPT)
        self.memories = memory_mod.load_memories()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        # Cache dei derivati costosi: vedi backend(), env_header() e
        # thinking_enabled(). Tutti e tre finivano per fare I/O (rete o disco)
        # ad ogni apertura di chat.
        self._backend: Any = None
        self._backend_key: tuple[Any, ...] | None = None
        self._env_header: str | None = None
        self._env_header_for: tuple[str, str] | None = None
        self._think: tuple[tuple[Any, ...], bool] | None = None
        self._vision: tuple[tuple[Any, ...], bool] | None = None
        bootstrap: dict[str, Any] = {}
        session_mod.bootstrap_session(bootstrap)
        self._sessions[bootstrap["current_session_id"]] = bootstrap
        self.last_opened: str = bootstrap["current_session_id"]

    def persist(self) -> None:
        """Scrive le preferenze su disco. Va chiamata dopo ogni modifica.

        Non e' un salvataggio periodico di proposito: l'utente cambia
        un'impostazione e si aspetta che valga anche domani, non che valga se
        chiude l'app nel momento giusto.
        """
        settings_mod.save_settings(self.settings)

    # -- conversazioni -----------------------------------------------------

    def session(self, session_id: str) -> dict[str, Any]:
        """Carica (o recupera dalla cache) una conversazione."""
        with self._lock:
            cached = self._sessions.get(session_id)
            if cached is not None:
                return cached
        loaded: dict[str, Any] = {}
        if not session_mod.load_session(loaded, session_id):
            raise HTTPException(404, "Conversazione non trovata.")
        with self._lock:
            self._sessions.setdefault(session_id, loaded)
            return self._sessions[session_id]

    def messages(self, session_id: str) -> list[dict[str, Any]]:
        return self.session(session_id).setdefault("messages", [])

    def _string_set(self, session_id: str, key: str) -> set[str]:
        session = self.session(session_id)
        current = session.get(key)
        if not isinstance(current, set):
            current = set(current or [])
            session[key] = current
        return current

    def touched(self, session_id: str) -> set[str]:
        return self._string_set(session_id, "touched_files")

    def attachments(self, session_id: str) -> list[dict[str, Any]]:
        session = self.session(session_id)
        current = session.get("attachments")
        if not isinstance(current, list):
            current = []
            session["attachments"] = current
        return current

    def known(self, session_id: str) -> set[str]:
        """File gia' letti (o scritti) in questa conversazione."""
        return self._string_set(session_id, "known_files")

    def preview_ports(self) -> tuple[int, int] | None:
        """Intervallo di porte da pubblicare, o None se non ha senso.

        Tre condizioni, tutte necessarie: l'utente le vuole, i comandi girano
        davvero in un container (sull'host non c'e' niente da pubblicare) e il
        container ha rete (senza, docker rifiuterebbe la pubblicazione).
        """
        s = self.settings
        if not s.get("preview_ports_enabled"):
            return None
        if str(s.get("sandbox")) != "docker" or not s.get("sandbox_network"):
            return None
        return sandbox_mod.port_range(
            int(s.get("preview_port_base") or 0), int(s.get("preview_port_count") or 0)
        )

    def preview(self, session_id: str) -> dict[str, Any] | None:
        return self.session(session_id).get("preview") or None

    def store_preview(self, session_id: str, payload: dict[str, Any] | None) -> None:
        self.session(session_id)["preview"] = payload

    def plan(self, session_id: str) -> plan_mod.Plan:
        """Piano di lavoro della conversazione, ricostruito dal disco."""
        session = self.session(session_id)
        return plan_mod.Plan.from_list(session.get("plan"))

    def store_plan(self, session_id: str, plan: plan_mod.Plan) -> None:
        self.session(session_id)["plan"] = plan.to_list()

    def notes(self, session_id: str) -> notes_mod.Notes:
        """Foglio di note della conversazione, ricostruito dal disco."""
        return notes_mod.Notes.from_list(self.session(session_id).get("notes"))

    def store_notes(self, session_id: str, notes: notes_mod.Notes) -> None:
        self.session(session_id)["notes"] = notes.to_list()

    def save(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session is not None:
            # Con cosa e' stata fatta questa conversazione. ``save_session``
            # scriveva questi due campi da sempre, ma nessuno li riempiva: il
            # modello restava "" e il workspace era la cwd del processo, che
            # coincide con la cartella di lavoro solo se l'harness e' stato
            # lanciato da li'. Su una chat riletta domani -- o mandata a
            # qualcuno per confrontare due modelli -- era proprio
            # l'informazione che mancava.
            session["model_name"] = self.settings["model_name"]
            session["workspace_dir"] = self.settings["workspace_dir"]
            session_mod.save_session(session, force=True)

    def new_session(self) -> str:
        fresh: dict[str, Any] = {}
        session_id = session_mod.new_session(fresh)
        with self._lock:
            self._sessions[session_id] = fresh
        self.last_opened = session_id
        return session_id

    def drop(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    # -- derivati ----------------------------------------------------------

    def thinking_enabled(self) -> bool:
        """Canale di ragionamento nativo: acceso solo se il modello ce l'ha.

        Con ``auto`` si guarda la capability ``thinking`` che Ollama dichiara
        (qwen3, deepseek-r1). Su un instruct normale il parametro non farebbe
        nulla, e chiedere <think> nel prompt sarebbe solo rumore che compete
        con le tool call.
        """
        setting = self.settings["native_think"]
        if isinstance(setting, bool) or str(setting).lower() != "auto":
            return bool(resolve_think(setting, None))

        # Il rilevamento costa una POST /api/show: memorizzarlo per
        # (endpoint, modello) evita di rifarla ad ogni apertura di chat.
        key = (self.settings["api_base"], self.settings["model_name"])
        if self._think is not None and self._think[0] == key:
            return self._think[1]
        backend = self.backend()
        detected = (
            backend.supports_thinking(self.settings["model_name"])
            if hasattr(backend, "supports_thinking")
            else None
        )
        value = resolve_tristate("auto", detected)
        if detected is not None:      # se Ollama non risponde, si riprova dopo
            self._think = (key, value)
        return value

    def gen_params(self) -> GenParams:
        s = self.settings
        return GenParams(
            model=s["model_name"],
            temperature=float(s["temperature"]),
            top_p=float(s["top_p"]),
            max_tokens=int(s["max_tokens"]),
            num_ctx=int(s["num_ctx"]),
            num_gpu=int(s["num_gpu"]),
            keep_alive=str(s["keep_alive"]),
            top_k=int(s["top_k"]),
            presence_penalty=float(s["presence_penalty"]),
            # Il livello ('high', 'max', ...) sopravvive fino alla richiesta:
            # thinking_enabled() lo schiaccia a booleano solo per scegliere il
            # system prompt, dove un livello non cambierebbe nulla.
            think=self.think_setting(),
        )

    def backend(self):
        """Backend riusato finche' non cambiano i parametri di connessione.

        Ricostruirlo ad ogni richiesta HTTP azzerava le sue cache interne
        (versione del server, capability del modello) e, con ``transport:
        auto``, rifaceva pure il rilevamento del transport. Aprire una chat
        costava cosi' due round-trip verso Ollama.
        """
        s = self.settings
        key = (
            s["transport"],
            s["api_base"],
            s["api_key"],
            float(s["timeout_seconds"]),
            s["stream_tools"],
        )
        with self._lock:
            if key != self._backend_key or self._backend is None:
                self._backend = build_backend(
                    transport=s["transport"],
                    base_url=s["api_base"],
                    api_key=s["api_key"],
                    timeout_s=float(s["timeout_seconds"]),
                    stream_tools=s["stream_tools"],
                )
                self._backend_key = key
            return self._backend

    def forget_backend(self) -> None:
        """Da chiamare quando l'utente tocca connessione o modello."""
        with self._lock:
            self._backend = None
            self._backend_key = None
            self._think = None
            self._vision = None
        forget_model_info()

    def skills(self) -> list[skills_mod.Skill]:
        """Skill disponibili adesso, rilette dal disco ad ogni turno.

        Rilette e non messe in cache di proposito: sono file scritti a mano,
        e la modalita' d'uso normale e' correggerne una e riprovare subito.
        Una cache costringerebbe a riavviare l'app per vedere l'effetto, che
        e' il modo migliore di far smettere di usarle.
        """
        return skills_mod.carica(
            [
                (Path(SKILLS_DIR), "harness"),
                (Path(self.settings["workspace_dir"]) / SKILLS_SUBDIR_WORKSPACE, "progetto"),
            ]
        )

    def skills_block(self, richiesta: str) -> str:
        tutte = self.skills()
        return skills_mod.render_blocco(skills_mod.scegli(tutte, richiesta), tutte)

    def system_prompt(self) -> str:
        """Prompt di sistema effettivo per il modello in uso.

        Finche' il testo e' uno dei nostri, lo sceglie l'harness in base alle
        capability: snello per i modelli che ragionano, esteso per quelli che
        hanno bisogno delle stampelle. Appena l'utente lo riscrive, la scelta
        automatica si fa da parte -- il suo testo vince sempre.
        """
        thinking = self.thinking_enabled()
        base = self.settings["system_prompt"]
        if is_stock_prompt(base):
            base = pick_system_prompt(thinking=thinking)
        return build_system_prompt(
            base, native_think=thinking
        ) + memory_mod.format_for_prompt(self.memories)

    def env_header(self, *, fresh: bool = False) -> str | None:
        """Albero del workspace da mettere in testa al contesto.

        ``fresh=True`` (inizio di un turno) rilegge il disco: e' il momento in
        cui l'agente deve vedere davvero cosa c'e'. ``fresh=False`` (la UI, che
        lo usa solo per stimare quanto contesto e' occupato) riusa l'ultimo
        albero: sfogliare le chat non deve costare una scansione di cartelle
        per click.
        """
        if not self.settings["auto_env_header"]:
            return None
        workspace = self.settings["workspace_dir"]
        key = (workspace, str(self.settings["sandbox"]), self.preview_ports())
        if not fresh and self._env_header_for == key:
            return self._env_header
        header = build_env_header(
            workspace,
            tool_names=[t["function"]["name"] for t in TOOLS_SCHEMA],
            sandbox=str(self.settings["sandbox"]),
            preview_ports=self.preview_ports(),
        )
        self._env_header = header
        self._env_header_for = key
        return header

    def think_setting(self) -> bool | str:
        """Valore di ``think`` da mandare a Ollama: booleano o livello."""
        setting = self.settings["native_think"]
        if isinstance(setting, str) and setting.strip().lower() == "auto":
            return self.thinking_enabled()
        return resolve_think(setting, None)

    def vision_enabled(self) -> bool:
        """Il modello in uso legge le immagini? Rilevato, non configurato."""
        key = (self.settings["api_base"], self.settings["model_name"])
        if self._vision is not None and self._vision[0] == key:
            return self._vision[1]
        backend = self.backend()
        detected = (
            backend.supports_vision(self.settings["model_name"])
            if hasattr(backend, "supports_vision")
            else None
        )
        if detected is not None:
            self._vision = (key, bool(detected))
        return bool(detected)

    def turn_images(self, session_id: str) -> tuple[list[str], list[str]]:
        """Immagini da mandare al modello per questo turno, e quelle escluse."""
        if not self.vision_enabled():
            return [], []
        return load_images_b64(
            self.settings["workspace_dir"], self.attachments(session_id)
        )

    def tools_schema(self) -> list[dict[str, Any]]:
        """Schemi completi o snelli, con la stessa regola del system prompt.

        Da quando il prompt snello occupa 612 token, gli schemi dei tool sono
        il blocco fisso piu' grande della richiesta: ~1.790 token pagati ad
        ogni passo agentico. Le descrizioni lunghe servono a un modello che
        fatica a collegare la richiesta al tool; a uno che ragiona sono 617
        token buttati per passo.
        """
        return TOOLS_SCHEMA_LEAN if self.thinking_enabled() else TOOLS_SCHEMA

    def tool_schema_tokens(self) -> int:
        return (
            TOOL_SCHEMA_TOKENS_LEAN if self.thinking_enabled() else TOOL_SCHEMA_TOKENS_FULL
        )

    def context_header(self, session_id: str, *, fresh: bool = False) -> str | None:
        """Header ambientale + elenco degli allegati della conversazione."""
        base = self.env_header(fresh=fresh)
        block = build_attachments_block(self.attachments(session_id))
        if not block:
            return base
        return f"{base}\n\n{block}" if base else block

    def tool_ctx(self, session_id: str) -> ToolContext:
        def persist(memories: list[dict[str, str]]) -> None:
            self.memories = memories
            memory_mod.save_memories(memories)

        def persist_plan(plan: plan_mod.Plan) -> None:
            self.store_plan(session_id, plan)

        def persist_notes(notes: notes_mod.Notes) -> None:
            self.store_notes(session_id, notes)

        return ToolContext(
            workspace=self.settings["workspace_dir"],
            timeout_s=int(self.settings["timeout_seconds"]),
            memories=self.memories,
            touched_files=self.touched(session_id),
            known_files=self.known(session_id),
            plan=self.plan(session_id),
            notes=self.notes(session_id),
            preview=self.preview(session_id),
            preview_ports=self.preview_ports(),
            on_memories_changed=persist,
            on_plan_changed=persist_plan,
            on_notes_changed=persist_notes,
            allow_dangerous_commands=bool(self.settings["confirm_commands"]),
            sandbox=str(self.settings["sandbox"]),
            docker_image=str(self.settings["docker_image"]),
            sandbox_network=bool(self.settings["sandbox_network"]),
        )


STATE = AppState()


# ---------------------------------------------------------------------------
# Serializzazione degli eventi
# ---------------------------------------------------------------------------

_EVENT_NAMES = {
    agent_mod.StepStarted: "step",
    agent_mod.ReasoningDelta: "reasoning",
    agent_mod.ContentDelta: "content",
    agent_mod.AssistantTurn: "assistant",
    agent_mod.ToolStarted: "tool_start",
    agent_mod.ToolFinished: "tool_end",
    agent_mod.AwaitingUserInput: "question",
    agent_mod.PlanUpdated: "plan",
    agent_mod.NotesUpdated: "notes",
    agent_mod.HistoryCompacted: "compacted",
    agent_mod.PreviewUpdated: "preview",
    agent_mod.TurnFinished: "done",
    agent_mod.AgentError: "error",
}


def sse(event_type: str, payload: dict[str, Any]) -> str:
    return f"data: {json.dumps({'type': event_type, **payload}, ensure_ascii=False)}\n\n"


def event_to_sse(event: Any) -> str:
    name = _EVENT_NAMES.get(type(event), "unknown")
    payload = asdict(event) if is_dataclass(event) else {"value": str(event)}
    return sse(name, payload)


def anchored_names(messages: list[dict[str, Any]]) -> set[str]:
    """Nomi degli allegati gia' agganciati a un messaggio dell'utente."""
    out: set[str] = set()
    for msg in messages:
        for entry in msg.get("attachments") or []:
            name = str((entry or {}).get("name") or "")
            if name:
                out.add(name)
    return out


def anchor_attachments(session_id: str, names: list[str]) -> list[dict[str, Any]]:
    """I metadati degli allegati richiesti, se sono davvero di questa chat.

    Si copia il dizionario invece di tenerne il riferimento: il messaggio e'
    cronologia, e la cronologia non deve cambiare sotto i piedi se un giorno
    l'elenco della sessione viene ritoccato.
    """
    if not names:
        return []
    voluti = [n for n in names if isinstance(n, str)]
    per_nome = {str(a.get("name") or ""): a for a in STATE.attachments(session_id)}
    gia = anchored_names(STATE.messages(session_id))
    return [
        {**per_nome[n], "image": is_image(n)}
        for n in voluti
        if n in per_nome and n not in gia
    ]


def pending_attachments(session_id: str) -> list[dict[str, Any]]:
    """Allegati caricati ma non ancora partiti con un messaggio.

    Sono quelli che la UI mostra nella barra sopra la casella di testo. Uno
    gia' agganciato a un messaggio si vede li' e non deve comparire due volte:
    la barra dice "parte con il prossimo invio", non "esiste".
    """
    gia = anchored_names(STATE.messages(session_id))
    return [
        {**a, "image": is_image(str(a.get("name") or ""))}
        for a in STATE.attachments(session_id)
        if str(a.get("name") or "") not in gia
    ]


def session_stats(session_id: str) -> dict[str, Any]:
    messages = STATE.messages(session_id)
    api_messages = agent_mod.build_api_messages(
        messages,
        system_prompt=STATE.system_prompt(),
        env_header=STATE.context_header(session_id),
        strip_thinking=bool(STATE.settings["strip_think_from_context"]),
        compact_old_tools=bool(STATE.settings["compact_old_tool_results"]),
    )
    return {
        "context_used": estimate_messages_tokens(api_messages) + STATE.tool_schema_tokens(),
        "context_window": int(STATE.settings["num_ctx"]),
        "touched_files": sorted(STATE.touched(session_id)),
        # Solo quelli ancora in attesa di partire: gli altri stanno gia'
        # disegnati sotto il messaggio con cui sono stati inviati, e ripeterli
        # nella barra del composer li farebbe sembrare in coda una seconda
        # volta. 'image' dice alla UI quali il modello puo' davvero guardare:
        # con un modello senza vision restano file come gli altri.
        "attachments": pending_attachments(session_id),
        "vision": STATE.vision_enabled(),
        "memories": len(STATE.memories),
        "sessions": session_list(),
        "session_id": session_id,
    }


def session_list() -> list[dict[str, Any]]:
    running = RUNNERS.running_ids()
    sessions = session_mod.list_sessions()
    for item in sessions:
        item["running"] = item["id"] in running
    return sessions


# ---------------------------------------------------------------------------
# Esecuzione di un turno
# ---------------------------------------------------------------------------


def ultima_richiesta(messages: list[dict[str, Any]]) -> str:
    """Il testo dell'ultimo messaggio vero dell'utente. I solleciti non contano."""
    for msg in reversed(messages):
        if msg.get("role") == "user" and not msg.get("hidden"):
            return str(msg.get("content") or "")
    return ""


def start_turn(session_id: str) -> TurnRunner:
    """Avvia il turno in background per la conversazione indicata."""
    messages = STATE.messages(session_id)
    snapshot = [dict(m) for m in messages]

    images, skipped = STATE.turn_images(session_id)

    def work(runner: TurnRunner) -> None:
        runner.emit(sse("start", {"session_id": session_id}))
        try:
            events = agent_mod.run_turn(
                backend=STATE.backend(),
                params=STATE.gen_params(),
                tools_schema=STATE.tools_schema(),
                tool_ctx=STATE.tool_ctx(session_id),
                ui_messages=messages,
                system_prompt=STATE.system_prompt(),
                # Il turno rilegge il disco: e' qui che l'agente deve vedere
                # il workspace com'e' adesso, non com'era all'ultimo click.
                env_header=STATE.context_header(session_id, fresh=True),
                images=images,
                should_stop=runner.cancelled.is_set,
                max_steps=int(STATE.settings["max_agent_loops"]),
                strip_thinking=bool(STATE.settings["strip_think_from_context"]),
                compact_old_tools=bool(STATE.settings["compact_old_tool_results"]),
                require_plan=bool(STATE.settings["require_plan"]),
                plan_gate=bool(STATE.settings["plan_gate"]),
                # Le skill si scelgono sull'ultima richiesta vera dell'utente,
                # non su tutta la cronologia: quello che ha chiesto tre turni
                # fa non deve continuare a tirarsi dietro le sue istruzioni.
                skills_block=STATE.skills_block(ultima_richiesta(messages)),
                compact_history=bool(STATE.settings["compact_history"]),
                soglia=float(STATE.settings["compact_threshold"]),
                auto_preview=bool(STATE.settings["preview_enabled"]),
                think_watchdog=bool(STATE.settings["think_watchdog"]),
            )
            for event in events:
                if isinstance(event, agent_mod.PreviewUpdated):
                    STATE.store_preview(session_id, event.payload)
                runner.emit(event_to_sse(event))
                if isinstance(
                    event,
                    (
                        agent_mod.ToolFinished,
                        agent_mod.AwaitingUserInput,
                        agent_mod.PlanUpdated,
                        agent_mod.NotesUpdated,
                        agent_mod.HistoryCompacted,
                        agent_mod.PreviewUpdated,
                    ),
                ):
                    STATE.save(session_id)
        except Exception as exc:  # noqa: BLE001 - l'errore va mostrato, non nascosto
            runner.emit(sse("error", {"message": f"{type(exc).__name__}: {exc}"}))
        finally:
            STATE.save(session_id)
            runner.emit(sse("state", session_stats(session_id)))

    return RUNNERS.start(session_id, snapshot, work)


def sse_response(generator: Iterator[str]) -> StreamingResponse:
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Disattiva il buffering dei proxy: senza, i frame arrivano a
            # blocchi e lo streaming perde tutto il senso.
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Modelli di richiesta
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    session_id: str
    prompt: str
    # Nomi degli allegati che l'utente ha messo nella barra del composer prima
    # di premere invio. Vengono agganciati al messaggio: un file allegato e'
    # parte della richiesta, non un accessorio della conversazione, e in una
    # chat lunga "quale messaggio portava quel CSV?" e' una domanda che si
    # risponde da sola solo se il file sta nella riga giusta.
    attachments: list[str] = []


class AnswerRequest(BaseModel):
    session_id: str
    answer: str | list[str]


class SettingsRequest(BaseModel):
    values: dict[str, Any]


class MemoryRequest(BaseModel):
    text: str


class WorkspaceRequest(BaseModel):
    path: str


# ---------------------------------------------------------------------------
# Rotte: pagina e stato
# ---------------------------------------------------------------------------

class StaticRivalidati(StaticFiles):
    """I file di ``web/`` serviti con ``Cache-Control: no-cache``.

    Non e' micro-ottimizzazione al contrario: e' un bug che costa ore. Ne'
    ``StaticFiles`` ne' ``FileResponse`` mandano un ``Cache-Control``, e in
    quel caso il browser applica la **freschezza euristica** -- tiene il file
    per circa il 10% del tempo passato dall'ultima modifica, senza chiedere
    niente al server. Su un file toccato la mattina, il pomeriggio vale
    mezz'ora di cache muta: si modifica ``app.js``, si riavvia ``run.py``, si
    ricarica la pagina, e gira ancora il codice di prima. Il sintomo e' il
    peggiore possibile -- la correzione "non funziona" -- e la diagnosi non e'
    nel codice che si sta guardando.

    ``no-cache`` non vuol dire "non mettere in cache": vuol dire "rivalida
    sempre". L'ETag e il ``Last-Modified`` continuano a fare il loro lavoro e
    la risposta normale resta un 304 da qualche decina di byte. Su un server
    che gira su 127.0.0.1 il prezzo e' zero.
    """

    def file_response(self, *args: Any, **kwargs: Any) -> Any:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", StaticRivalidati(directory=str(WEB_DIR)), name="static")


def _impronta(nome: str) -> str:
    """Impronta breve di un file di ``web/``: cambia quando il file cambia."""
    try:
        st = (WEB_DIR / nome).stat()
        return hashlib.sha1(f"{st.st_mtime_ns}:{st.st_size}".encode()).hexdigest()[:10]
    except OSError:
        return APP_VERSION


@app.get("/")
def index() -> Response:
    """La pagina, con l'impronta dei suoi asset dentro gli URL.

    Perche' non basta ``Cache-Control``. Senza intestazione il browser applica
    la **freschezza euristica**: tiene un file per circa il 10% del tempo
    passato dall'ultima modifica e in quell'intervallo **non contatta il
    server**. Il guaio e' che un'intestazione la puo' leggere solo su una
    risposta, e una risposta arriva solo se la richiesta parte: finche' la
    copia vecchia e' considerata fresca, `no-cache` non viene mai letto. La
    correzione non puo' arrivare da sola -- e' il caso in cui si modifica
    ``app.js``, si riavvia il server, si ricarica la pagina e gira ancora il
    codice di prima, senza niente che lo faccia sospettare.

    L'unica cosa che scavalca una cache e' **cambiare l'URL**: un file con un
    indirizzo mai visto non puo' avere una copia vecchia. Qui l'indirizzo porta
    l'impronta di dimensione e data di modifica, quindi cambia da se' ad ogni
    salvataggio e non c'e' nessun numero da ricordarsi di alzare a mano.

    La riscrittura e' su una stringa e non su ``index.html`` sul disco: il file
    resta apribile a mano durante lo sviluppo.
    """
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    for nome in ("app.js", "style.css"):
        html = html.replace(f"/static/{nome}", f"/static/{nome}?v={_impronta(nome)}")
    return Response(
        html,
        media_type="text/html; charset=utf-8",
        # La pagina invece non puo' portare un'impronta -- e' lei il punto di
        # ingresso -- quindi deve rivalidarsi ad ogni caricamento. E' una
        # richiesta sola, su 127.0.0.1, che di norma si chiude con un 304.
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/api/bootstrap")
def bootstrap() -> dict[str, Any]:
    backend = STATE.backend()
    if hasattr(backend, "status"):
        online, detail, models = backend.status()
    else:
        online, detail = backend.ping()
        models = backend.list_models()

    # Il modello configurato puo' non esserci piu': o e' il default di fabbrica
    # invecchiato, o l'utente ha disinstallato quello che usava. In entrambi i
    # casi la prima richiesta fallirebbe con un 404 poco parlante. Con la lista
    # dei modelli in mano, questo e' il punto giusto per accorgersene.
    sostituto = settings_mod.pick_available_model(STATE.settings, models)
    if sostituto:
        STATE.settings["model_name"] = sostituto
        STATE.forget_backend()
        STATE.persist()

    session_id = STATE.last_opened
    return {
        "app": {"name": APP_NAME, "version": APP_VERSION},
        "settings": STATE.settings,
        "session": open_payload(session_id),
        "memories": STATE.memories,
        "backend": {
            "name": backend.name,
            "online": online,
            "detail": detail,
            "url": normalise_base_url(STATE.settings["api_base"]),
            "version": backend.version() if hasattr(backend, "version") else "",
            "models": models,
            "streams_tools": (
                backend.streams_tool_calls()
                if hasattr(backend, "streams_tool_calls")
                else None
            ),
        },
    }


def open_payload(session_id: str) -> dict[str, Any]:
    """Tutto il necessario per disegnare una conversazione.

    Se un turno e' in corso, ``messages`` e' la cronologia com'era *prima*
    dell'inizio del turno: il client la disegna e poi si attacca allo stream,
    che gli riapplica gli eventi dall'inizio. Niente doppioni.
    """
    messages = STATE.messages(session_id)
    runner = RUNNERS.get(session_id)
    live = runner is not None and not runner.finished.is_set()
    return {
        "session_id": session_id,
        "messages": runner.snapshot if live else messages,
        "running": live,
        "pending": agent_mod.pending_question(messages),
        # Il piano viene dal disco, non dagli eventi: riaprendo una
        # conversazione il pannello si ridisegna anche se il turno che lo ha
        # scritto e' finito ieri.
        "plan": STATE.plan(session_id).to_list(),
        "notes": STATE.notes(session_id).to_list(),
        "preview": STATE.preview(session_id),
        "stats": session_stats(session_id),
    }


# ---------------------------------------------------------------------------
# Rotte: conversazioni
# ---------------------------------------------------------------------------


@app.get("/api/sessions")
def list_sessions() -> dict[str, Any]:
    return {"sessions": session_list(), "current": STATE.last_opened}


@app.get("/api/sessions/search")
def search_sessions(q: str = "", limit: int = 40) -> dict[str, Any]:
    """Conversazioni che nominano quello che si sta cercando.

    Sta sul server e non nel client per una ragione sola: il contenuto delle
    conversazioni non e' mai stato mandato al browser -- la sidebar riceve
    titolo, data e numero di messaggi -- e mandarne quaranta per intero ad ogni
    tasto premuto sarebbe megabyte per una riga di elenco.
    """
    risultati = session_mod.search_sessions(q, limit=limit)
    correnti = RUNNERS.running_ids()
    for riga in risultati:
        riga["running"] = riga["id"] in correnti
    return {"sessions": risultati, "query": q}


@app.post("/api/sessions")
def create_session() -> dict[str, Any]:
    return open_payload(STATE.new_session())


@app.post("/api/sessions/{session_id}/open")
def open_session(session_id: str) -> dict[str, Any]:
    payload = open_payload(session_id)
    STATE.last_opened = session_id
    return payload


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str) -> dict[str, Any]:
    if RUNNERS.is_running(session_id):
        raise HTTPException(409, "Un turno e' in corso in questa conversazione.")
    session_mod.delete_session(session_id)
    STATE.drop(session_id)
    if STATE.last_opened == session_id:
        STATE.new_session()
    return open_payload(STATE.last_opened)


# ---------------------------------------------------------------------------
# Rotte: turno agentico
# ---------------------------------------------------------------------------


@app.post("/api/chat")
def chat(request: ChatRequest) -> dict[str, Any]:
    prompt = (request.prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "Prompt vuoto.")
    session_id = request.session_id
    messages = STATE.messages(session_id)
    if agent_mod.pending_question(messages):
        raise HTTPException(409, "C'e' una domanda dell'agente in attesa di risposta.")
    if RUNNERS.is_running(session_id):
        raise HTTPException(409, "Un turno e' gia' in corso in questa conversazione.")

    entry: dict[str, Any] = {"role": "user", "content": prompt}
    agganciati = anchor_attachments(session_id, request.attachments)
    if agganciati:
        entry["attachments"] = agganciati
    messages.append(entry)
    STATE.save(session_id)
    start_turn(session_id)
    # Le statistiche tornano **subito**, non a fine turno. Prima l'unico
    # ``state`` lo emetteva il ``finally`` del worker: fino ad allora il
    # misuratore del contesto restava al valore precedente al messaggio appena
    # inviato, e una conversazione nuova non compariva nell'elenco a sinistra
    # (ne' prendeva il suo titolo) finche' l'agente non aveva finito di
    # lavorare -- che su un turno lungo vuol dire minuti. Il messaggio e' gia'
    # in cronologia qui sopra: il conto e' esatto.
    return {"ok": True, "session_id": session_id, "stats": session_stats(session_id)}


@app.post("/api/answer")
def answer(request: AnswerRequest) -> dict[str, Any]:
    session_id = request.session_id
    messages = STATE.messages(session_id)
    if RUNNERS.is_running(session_id):
        raise HTTPException(409, "Un turno e' gia' in corso in questa conversazione.")
    if not agent_mod.resume_with_answer(messages, request.answer):
        raise HTTPException(409, "Nessuna domanda in attesa.")
    STATE.save(session_id)
    start_turn(session_id)
    # Stesso motivo di ``/api/chat``: la risposta dell'utente e' gia' in
    # cronologia, e il consumo che si legge nel pannello deve tenerne conto
    # adesso, non quando il ragionamento sara' finito.
    return {"ok": True, "session_id": session_id, "stats": session_stats(session_id)}


@app.post("/api/preload")
def preload() -> dict[str, Any]:
    """Scalda il modello in VRAM. Chiamata dalla UI subito dopo l'avvio.

    Non blocca nulla: se fallisce, il primo turno paghera' il caricamento come
    prima. Per questo il client la lancia e non ne aspetta l'esito.
    """
    backend = STATE.backend()
    if not hasattr(backend, "preload"):
        return {"ok": False, "detail": "Disponibile solo sul transport Ollama."}
    ok = backend.preload(
        str(STATE.settings["model_name"]), str(STATE.settings["keep_alive"])
    )
    return {"ok": ok, "model": STATE.settings["model_name"]}


@app.get("/api/models")
def models() -> dict[str, Any]:
    """Catalogo del backend **adesso**, per ricaricare la tendina dei modelli.

    Esiste perche' l'elenco arrivava solo da ``/api/bootstrap``, cioe' una
    volta sola al caricamento della pagina. Cambiando endpoint il server
    ricostruiva il backend ma la tendina restava quella di prima: elencava i
    modelli della macchina precedente, e il modello nuovo sembrava non esserci.
    """
    backend = STATE.backend()
    if hasattr(backend, "status"):
        online, detail, models_list = backend.status()
    else:
        online, detail = backend.ping()
        models_list = backend.list_models()

    sostituto = settings_mod.pick_available_model(STATE.settings, models_list)
    if sostituto:
        STATE.settings["model_name"] = sostituto
        STATE.forget_backend()
        STATE.persist()

    return {
        "online": online,
        "detail": detail,
        "models": models_list,
        "model_name": STATE.settings["model_name"],
        "url": normalise_base_url(STATE.settings["api_base"]),
    }


@app.get("/api/profile")
def recommended_profile() -> dict[str, Any]:
    """Parametri consigliati per il modello selezionato.

    Il contesto non viene dalla tabella ma dalla VRAM libera adesso: e' l'unico
    modo onesto di proporre un numero, perche' dipende dalla scheda e da cosa
    ci sta gia' sopra.
    """
    model = str(STATE.settings["model_name"])
    thinking = STATE.thinking_enabled()
    profile = profiles.profile_for(model, thinking=thinking)

    vram = _vram_snapshot()
    free_mb = vram.get("free")
    # Il costo del KV cache per token si ricava dai metadati GGUF del modello,
    # non da una costante: fra un 7B e un 27B c'e' un fattore quattro, e
    # sbagliarlo per difetto significa proporre un contesto che non ci sta.
    info = STATE.backend().model_info(model) if hasattr(STATE.backend(), "model_info") else {}
    mb_per_token = profiles.kv_mb_per_token(info.get("model_info") if info else None)

    if free_mb:
        suggested_ctx = profiles.context_for_vram(free_mb, mb_per_token)
    else:
        # VRAM sconosciuta: e' il caso normale con Ollama su un'altra macchina
        # e nessuna VRAM dichiarata. Proporre il minimo sarebbe una *modifica*
        # travestita da consiglio -- l'utente clicca "applica i consigliati" e
        # si ritrova il contesto dimezzato senza che nessuno abbia misurato
        # niente. Meglio confermare quello che c'e' gia'.
        suggested_ctx = int(STATE.settings["num_ctx"])

    values = profiles.as_settings(profile, num_ctx=suggested_ctx)
    return {
        "model": model,
        "profile": profile.name,
        "note": profile.note,
        "values": values,
        "free_vram_mb": free_mb,
        "vram": vram,
        "kv_mb_per_token": round(mb_per_token, 4) if mb_per_token else None,
        "vision": STATE.vision_enabled(),
        "thinking": thinking,
    }


@app.post("/api/profile/apply")
def apply_profile() -> dict[str, Any]:
    """Scrive i parametri consigliati nelle impostazioni."""
    recommended = recommended_profile()
    STATE.settings.update(recommended["values"])
    STATE.persist()
    return {"settings": STATE.settings, "applied": recommended}


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", ""}


def endpoint_is_local(api_base: str) -> bool:
    """L'endpoint Ollama gira sulla stessa macchina dell'harness?

    E' la domanda che decide se ``nvidia-smi`` misura qualcosa di pertinente.
    Con il modello su un'altra macchina della rete, la scheda locale -- se c'e'
    -- e' semplicemente la scheda sbagliata: leggerla non da' un errore, da'
    un numero plausibile e falso, che e' molto peggio.
    """
    host = urlparse(normalise_base_url(api_base)).hostname or ""
    return host.lower() in _LOCAL_HOSTS


def _nvidia_smi(fields: str) -> list[str] | None:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
            text=True, timeout=4,
        )
        return [p.strip() for p in out.strip().splitlines()[0].split(",")]
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def _vram_snapshot() -> dict[str, Any]:
    """VRAM della scheda che esegue **il modello**, non di quella locale.

    Due sorgenti, scelte in base a dove sta Ollama:

    * **endpoint locale** -> ``nvidia-smi``, come prima: da tutto (totale,
      usato, libero) e tiene conto anche di cosa occupa la scheda oltre al
      modello.
    * **endpoint remoto** -> ``GET /api/ps`` di Ollama, che dichiara quanta
      VRAM occupano i modelli caricati. Non dice quanto e' grande la scheda,
      perche' l'API non lo espone: quel numero lo si dichiara una volta in
      ``gpu_total_vram_mb``. Senza, il libero resta ``None`` e chi lo usa deve
      comportarsi di conseguenza invece di inventare un valore prudente.
    """
    api_base = str(STATE.settings["api_base"])
    declared = int(STATE.settings.get("gpu_total_vram_mb") or 0)

    if endpoint_is_local(api_base):
        parts = _nvidia_smi("name,memory.total,memory.used,memory.free,utilization.gpu")
        if parts and len(parts) >= 5:
            return {
                "source": "nvidia-smi",
                "remote": False,
                "name": parts[0],
                "total": int(parts[1]),
                "used": int(parts[2]),
                "free": int(parts[3]),
                "util": int(parts[4]),
            }
        return {"source": None, "remote": False, "free": None}

    backend = STATE.backend()
    loaded = backend.loaded_models() if hasattr(backend, "loaded_models") else []
    used_mb = sum(int(m.get("size_vram") or 0) for m in loaded) // (1024 * 1024)
    snapshot: dict[str, Any] = {
        "source": "ollama /api/ps",
        "remote": True,
        "host": urlparse(normalise_base_url(api_base)).hostname,
        "used": used_mb if loaded else None,
        "total": declared or None,
        "free": (declared - used_mb) if declared else None,
        "models": [str(m.get("name") or m.get("model") or "") for m in loaded],
    }
    return snapshot


# ---------------------------------------------------------------------------
# Prontezza dell'ambiente
# ---------------------------------------------------------------------------


def _check(id_: str, label: str, state: str, detail: str, action: str = "") -> dict[str, Any]:
    return {"id": id_, "label": label, "state": state, "detail": detail, "action": action}


@app.get("/api/readiness")
def readiness() -> dict[str, Any]:
    """Tutto quello che serve perche' il primo messaggio funzioni davvero.

    Prima la schermata iniziale diceva "Workspace pronto" appena la pagina si
    apriva: era una speranza, non una verifica. Ollama poteva essere spento,
    Docker non avviato, l'immagine mai costruita -- e a scoprirlo era il primo
    turno dell'utente, sotto forma di errore dentro una tendina.

    Tre stati e non due: ``error`` e' cio' che impedisce di lavorare, ``warn``
    e' cio' che funziona ma peggio (l'immagine di serie esegue i comandi, solo
    senza pytest ne' git). Schiacciarli insieme renderebbe la spia rossa troppo
    spesso per essere presa sul serio.
    """
    checks: list[dict[str, Any]] = []

    # 1. Il workspace esiste? Banale, e la prima cosa che rompe tutto il resto.
    workspace = Path(str(STATE.settings["workspace_dir"]))
    if workspace.is_dir():
        checks.append(_check("workspace", "Cartella di lavoro", "ok", str(workspace)))
    else:
        checks.append(_check(
            "workspace", "Cartella di lavoro", "error",
            f"{workspace} non esiste piu'.", "workspace",
        ))

    # 2. Il modello.
    backend = STATE.backend()
    online, dettaglio, modelli = (
        backend.status() if hasattr(backend, "status")
        else (*backend.ping(), backend.list_models())
    )
    modello = str(STATE.settings["model_name"])
    if not online:
        checks.append(_check(
            "ollama", "Server dei modelli", "error",
            f"{normalise_base_url(STATE.settings['api_base'])} non risponde: {dettaglio}",
            "settings",
        ))
    elif modelli and modello not in modelli:
        checks.append(_check(
            "ollama", "Modello selezionato", "error",
            f"'{modello}' non e' installato su questo endpoint.", "settings",
        ))
    else:
        checks.append(_check("ollama", "Server dei modelli", "ok", f"{modello} · {dettaglio}"))

    # 3. Docker e immagine: hanno senso solo se i comandi girano in sandbox.
    if str(STATE.settings["sandbox"]) != "docker":
        checks.append(_check(
            "docker", "Sandbox", "warn",
            "I comandi girano direttamente sulla macchina: l'agente vede tutto il disco.",
            "settings",
        ))
    else:
        disponibile, dettaglio_docker = sandbox_mod.docker_available()
        lavoro = PREP.docker.snapshot()
        if disponibile:
            checks.append(_check("docker", "Docker", "ok", dettaglio_docker))
            tag = sandbox_mod.image_tag(STATE.settings["workspace_dir"])
            costruita = sandbox_mod.image_exists(tag)
            lavoro_img = PREP.image.snapshot()
            if lavoro_img["state"] == "running":
                checks.append(_check(
                    "image", "Immagine del progetto", "warn",
                    "Costruzione in corso: la prima volta puo' richiedere qualche minuto.",
                ))
            elif costruita and str(STATE.settings["docker_image"]) == tag:
                checks.append(_check("image", "Immagine del progetto", "ok", tag))
            elif costruita:
                checks.append(_check(
                    "image", "Immagine del progetto", "warn",
                    f"L'immagine {tag} esiste ma non e' quella in uso "
                    f"({STATE.settings['docker_image']}).", "image",
                ))
            else:
                checks.append(_check(
                    "image", "Immagine del progetto", "warn",
                    "Non ancora costruita: dentro la sandbox mancano pytest, ruff e git.",
                    "image",
                ))
        elif lavoro["state"] == "running":
            checks.append(_check(
                "docker", "Docker", "warn",
                "Avvio in corso: Docker Desktop ci mette qualche decina di secondi.",
            ))
        else:
            checks.append(_check(
                "docker", "Docker", "error",
                lavoro["detail"] or dettaglio_docker, "docker",
            ))

    return {
        "checks": checks,
        "ready": not any(c["state"] == "error" for c in checks),
        "jobs": PREP.snapshot(),
    }


@app.post("/api/prep/docker")
def prep_docker() -> dict[str, Any]:
    PREP.start_docker()
    return {"job": PREP.docker.snapshot()}


@app.post("/api/prep/image")
def prep_image() -> dict[str, Any]:
    def seleziona(tag: str) -> None:
        # Costruirla e non usarla sarebbe il peggio dei due mondi: si paga la
        # build e i comandi continuano a girare sull'immagine di serie.
        STATE.settings["docker_image"] = tag
        STATE.persist()

    PREP.build_image(str(STATE.settings["workspace_dir"]), on_done=seleziona)
    return {"job": PREP.image.snapshot()}


def maybe_prepare_workspace() -> None:
    """Costruisce l'immagine del workspace corrente, se serve e se e' voluto.

    Chiamata quando il workspace cambia. ``image_needed`` e' quello che evita
    di far partire una build da minuti ad ogni cambio di cartella: se
    l'immagine c'e' gia', o l'utente ne ha scelta una sua, non si tocca niente.
    """
    if not STATE.settings.get("image_autobuild"):
        return
    if str(STATE.settings["sandbox"]) != "docker":
        return
    workspace = str(STATE.settings["workspace_dir"])
    if image_needed(workspace, str(STATE.settings["docker_image"])):
        prep_image()


RECENT_WORKSPACES_MAX = 6


def remember_workspace(path: str = "") -> list[str]:
    """Mette la cartella in testa all'elenco dei recenti, senza doppioni.

    Il confronto e' sul percorso normalizzato in minuscolo: su Windows
    ``C:\\Progetti`` e ``c:\\progetti`` sono la stessa cartella, e vederla due
    volte nella tendina farebbe dubitare di quale sia quella buona. La
    corrente sta in testa: e' l'unica che non ha bisogno di essere ritrovata,
    ma toglierla dall'elenco farebbe sparire una riga quando ci si sposta.
    """
    corrente = str(path or STATE.settings["workspace_dir"])
    # Le cartelle che non esistono piu' escono dall'elenco: un progetto
    # cancellato, una chiavetta staccata, o -- meno esotico -- le cartelle
    # temporanee che la suite di test si porta dietro passando da /api/workspace.
    # Offrire una scorciatoia che porta a un errore e' peggio che non offrirla.
    recenti = [
        str(p)
        for p in (STATE.settings.get("recent_workspaces") or [])
        if str(p).strip() and os.path.isdir(str(p))
    ]
    chiave = os.path.normcase(os.path.normpath(corrente))
    tenuti = [
        p for p in recenti if os.path.normcase(os.path.normpath(p)) != chiave
    ]
    aggiornati = [corrente, *tenuti][:RECENT_WORKSPACES_MAX]
    STATE.settings["recent_workspaces"] = aggiornati
    return aggiornati


def autostart_docker() -> None:
    """Accende Docker appena l'harness parte, se l'utente lo vuole."""
    if not STATE.settings.get("docker_autostart"):
        return
    if str(STATE.settings["sandbox"]) != "docker":
        return
    disponibile, _ = sandbox_mod.docker_available()
    if not disponibile:
        PREP.start_docker()


@app.get("/api/preview/file")
def preview_file(path: str) -> FileResponse:
    """Serve un file del workspace al pannello di anteprima.

    La guardia e' ``resolve_path``, la stessa che usano i tool: e' l'unico
    punto del progetto che sa dire "questo percorso e' dentro il workspace", e
    duplicarne la logica qui vorrebbe dire avere due risposte diverse alla
    stessa domanda -- con la seconda che nessuno ricorda di aggiornare.

    Il contenuto viene mostrato in un iframe **senza** ``allow-same-origin``
    (vedi web/app.js): un HTML scritto dal modello ottiene cosi' un'origine
    opaca e non puo' parlare con le API dell'harness, che stanno sulla stessa
    porta.
    """
    try:
        target = resolve_path(STATE.settings["workspace_dir"], path)
    except WorkspaceError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not target.is_file():
        raise HTTPException(404, "File non trovato.")

    media, _ = mimetypes.guess_type(target.name)
    if target.suffix.lower() in {".md", ".markdown"}:
        # Il markdown lo rende il client, con lo stesso renderer della chat:
        # servirlo come text/markdown farebbe scaricare il file al browser.
        media = "text/plain; charset=utf-8"
    return FileResponse(
        target,
        media_type=media or "text/plain; charset=utf-8",
        # Il tipo dichiarato e' quello che decide come viene interpretato:
        # lasciare che il browser tiri a indovinare su file scritti dal modello
        # e' esattamente il caso in cui non si vuole.
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"},
    )


@app.get("/api/sandbox")
def sandbox_status() -> dict[str, Any]:
    """Stato della sandbox, per il pannello impostazioni."""
    info = sandbox_mod.status(STATE.settings["workspace_dir"])
    info["mode"] = STATE.settings["sandbox"]
    info["image"] = STATE.settings["docker_image"]
    info["network"] = bool(STATE.settings["sandbox_network"])
    porte = STATE.preview_ports()
    info["preview_ports"] = list(porte) if porte else None
    return info


@app.post("/api/sandbox/dockerfile")
def sandbox_dockerfile() -> dict[str, Any]:
    """Crea il Dockerfile della sandbox nel workspace, se non c'e'."""
    path = sandbox_mod.write_dockerfile(STATE.settings["workspace_dir"])
    STATE.env_header(fresh=True)          # il file nuovo compare nell'albero
    return {"path": path.name, "content": path.read_text(encoding="utf-8")}


@app.post("/api/sandbox/build")
def sandbox_build() -> dict[str, Any]:
    """Costruisce l'immagine del progetto e la seleziona.

    E' l'operazione che rende la sandbox davvero usabile: l'immagine di serie
    ha solo Python, quindi 'pytest -q' o 'git status' non esistono la' dentro.
    """
    workspace = STATE.settings["workspace_dir"]
    try:
        tag, log = sandbox_mod.build_image(workspace)
    except sandbox_mod.SandboxError as exc:
        raise HTTPException(400, str(exc)) from exc
    STATE.settings["docker_image"] = tag
    STATE.persist()
    return {"image": tag, "log": log, "settings": STATE.settings}


@app.post("/api/sandbox/restart")
def sandbox_restart() -> dict[str, Any]:
    """Butta il container e lo ricrea: serve dopo aver cambiato immagine."""
    workspace = STATE.settings["workspace_dir"]
    try:
        sandbox_mod.stop(workspace)
        if STATE.settings["sandbox"] == "docker":
            sandbox_mod.ensure_container(
                workspace,
                image=str(STATE.settings["docker_image"]),
                network=bool(STATE.settings["sandbox_network"]),
            )
    except sandbox_mod.SandboxError as exc:
        raise HTTPException(400, str(exc)) from exc
    return sandbox_status()


@app.post("/api/attachments/{session_id}")
async def add_attachments(
    session_id: str, files: list[UploadFile] = File(...)  # noqa: B008
) -> dict[str, Any]:
    """Salva i file allegati in ``allegati/`` dentro il workspace.

    Restano sul disco: l'agente li apre con ``read_file`` come qualsiasi altro
    file del progetto, e l'utente se li ritrova in Esplora risorse anche
    quando la conversazione e' chiusa.
    """
    entries = STATE.attachments(session_id)
    added: list[dict[str, Any]] = []
    for upload in files:
        data = await upload.read()
        if not data:
            continue
        try:
            entry = store_attachment(
                STATE.settings["workspace_dir"], upload.filename or "allegato", data
            )
        except (WorkspaceError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc
        entries.append(entry)
        added.append(entry)
    STATE.save(session_id)
    return {"added": added, "attachments": entries, "stats": session_stats(session_id)}


@app.delete("/api/attachments/{session_id}")
def remove_attachment(session_id: str, name: str) -> dict[str, Any]:
    """Toglie l'allegato dalla conversazione e cancella il file.

    Non mentre l'agente lavora: il file e' montato nel workspace e un turno in
    corso potrebbe averlo aperto un istante fa. Cancellarlo sotto ai piedi
    trasformerebbe un gesto di pulizia in un ``read_file`` fallito a meta'
    ragionamento, con il modello che si mette a indagare su un errore che ha
    causato l'utente. Si aspetta la fine del ciclo che l'ha coinvolto.
    """
    if RUNNERS.is_running(session_id):
        raise HTTPException(
            409, "L'agente sta lavorando su questa chat: riprova a turno finito."
        )
    entries = STATE.attachments(session_id)
    keep = [e for e in entries if e.get("name") != name]
    if len(keep) == len(entries):
        raise HTTPException(404, "Allegato non trovato in questa conversazione.")
    # Anche dai messaggi a cui era agganciato, o la goccia resterebbe disegnata
    # in chat a puntare a un file che non c'e' piu'.
    for msg in STATE.messages(session_id):
        agganci = msg.get("attachments")
        if not agganci:
            continue
        rimasti = [a for a in agganci if a.get("name") != name]
        if rimasti:
            msg["attachments"] = rimasti
        else:
            msg.pop("attachments", None)
    try:
        path = Path(STATE.settings["workspace_dir"]) / ATTACHMENTS_DIR / name
        if path.is_file():
            path.unlink()
    except OSError:
        pass          # il file puo' essere stato rimosso a mano: non e' un errore
    entries[:] = keep
    STATE.save(session_id)
    return {"attachments": entries, "stats": session_stats(session_id)}


@app.post("/api/stop/{session_id}")
def stop(session_id: str) -> dict[str, Any]:
    """Chiede al turno di fermarsi al primo punto sicuro.

    Non interrompe un tool gia' partito: l'agente finisce quello che ha in
    mano, scrive il risultato in cronologia e chiude. In pratica il ritorno e'
    immediato, perche' il grosso del tempo se ne va nella generazione, ed e'
    li' che il controllo scatta ad ogni token.
    """
    runner = RUNNERS.get(session_id)
    if runner is None or runner.finished.is_set():
        return {"ok": False, "detail": "Nessun turno in corso."}
    runner.cancel()
    return {"ok": True, "session_id": session_id}


@app.get("/api/stream/{session_id}")
def stream(session_id: str) -> StreamingResponse:
    """Si attacca al turno: prima l'arretrato, poi il flusso dal vivo.

    Chiamabile in qualunque momento e piu' volte: e' cosi' che tornare su una
    conversazione mostra i passi di pensiero e i tool gia' eseguiti mentre si
    era altrove.
    """
    runner = RUNNERS.get(session_id)
    if runner is None:
        return sse_response(iter([sse("idle", {"session_id": session_id})]))
    return sse_response(runner.stream())


# ---------------------------------------------------------------------------
# Rotte: impostazioni, memoria, workspace
# ---------------------------------------------------------------------------


@app.post("/api/settings")
def update_settings(request: SettingsRequest) -> dict[str, Any]:
    touched = set()
    for key, value in request.values.items():
        if key in STATE.settings or key == "system_prompt":
            if STATE.settings.get(key) != value:
                touched.add(key)
            STATE.settings[key] = value
    # Le cache dei derivati valgono finche' non si cambia a cosa puntano.
    if touched & {"transport", "api_base", "api_key", "timeout_seconds",
                  "stream_tools", "model_name"}:
        STATE.forget_backend()
    # L'immagine e la rete si applicano alla creazione del container: se
    # cambiano, quello in piedi va buttato o resterebbe con i vecchi parametri.
    if touched & {"docker_image", "sandbox_network"}:
        try:
            sandbox_mod.stop(STATE.settings["workspace_dir"])
        except sandbox_mod.SandboxError:
            pass
    if touched:
        STATE.persist()
    # Il workspace si puo' cambiare anche da qui, non solo dal selettore.
    if "workspace_dir" in touched:
        remember_workspace()
        STATE.persist()
        maybe_prepare_workspace()
    return {"settings": STATE.settings, "stats": session_stats(STATE.last_opened)}


@app.get("/api/memories")
def get_memories() -> dict[str, Any]:
    return {"memories": STATE.memories}


@app.post("/api/memories")
def add_memory(request: MemoryRequest) -> dict[str, Any]:
    ok, message = memory_mod.add_memory(STATE.memories, request.text)
    if ok:
        memory_mod.save_memories(STATE.memories)
    return {"ok": ok, "message": message, "memories": STATE.memories}


@app.delete("/api/memories/{memory_id}")
def remove_memory(memory_id: str) -> dict[str, Any]:
    memory_mod.remove_memory(STATE.memories, memory_id)
    memory_mod.save_memories(STATE.memories)
    return {"memories": STATE.memories}


@app.post("/api/workspace")
def set_workspace(request: WorkspaceRequest) -> dict[str, Any]:
    path = Path(request.path).expanduser()
    if not path.is_dir():
        raise HTTPException(400, f"'{request.path}' non e' una cartella esistente.")
    STATE.settings["workspace_dir"] = str(path.resolve())
    remember_workspace()
    STATE.persist()
    maybe_prepare_workspace()
    return {
        "workspace_dir": STATE.settings["workspace_dir"],
        "recent_workspaces": STATE.settings["recent_workspaces"],
        "stats": session_stats(STATE.last_opened),
        "jobs": PREP.snapshot(),
    }


class OpenRequest(BaseModel):
    # Vuoto: apre la radice del workspace. Valorizzato: apre la cartella che
    # contiene quel file, con il file gia' selezionato dove il sistema lo sa
    # fare. E' la scorciatoia dietro l'iconcina della cartella nelle gocce.
    path: str = ""


@app.post("/api/workspace/open")
def open_workspace(request: OpenRequest | None = None) -> dict[str, Any]:
    """Apre la cartella nel file manager del sistema (l'app gira in locale)."""
    base = Path(STATE.settings["workspace_dir"])
    if not base.is_dir():
        raise HTTPException(400, "La cartella non esiste.")

    relativo = (request.path if request else "") or ""
    target: Path | None = None
    if relativo:
        try:
            # Stessa guardia dei tool: la scorciatoia non deve diventare un
            # modo per far aprire al sistema un file fuori dal workspace.
            target = resolve_path(base, relativo)
        except WorkspaceError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not target.exists():
            # Il file puo' essere stato spostato o cancellato dopo il turno:
            # meglio aprire la cartella che rispondere con un errore.
            target = None

    try:
        system = platform.system()
        if target is not None and system == "Windows":
            # /select vuole il path come argomento unico e senza spazio dopo
            # la virgola: explorer e' notoriamente schizzinoso su questo.
            subprocess.run(["explorer", f"/select,{target}"], check=False)
        elif target is not None and system == "Darwin":
            subprocess.run(["open", "-R", str(target)], check=False)
        else:
            # Linux non ha un "rivela il file" universale: si apre la cartella
            # che lo contiene, che e' il 90% del valore della scorciatoia.
            cartella = target.parent if target is not None else base
            if system == "Windows":
                os.startfile(str(cartella))  # noqa: S606
            elif system == "Darwin":
                subprocess.run(["open", str(cartella)], check=False)
            else:
                subprocess.run(["xdg-open", str(cartella)], check=False)
    except OSError as exc:
        raise HTTPException(500, str(exc)) from exc
    return {"ok": True, "revealed": str(target) if target is not None else ""}


@app.post("/api/workspace/pick")
def pick_workspace() -> dict[str, Any]:
    """Apre il selettore di cartelle del sistema operativo.

    Su Windows e' il dialogo di Esplora risorse, con barra dei percorsi,
    ricerca e preferiti: un elenco disegnato da noi non potrebbe competere,
    ne' come velocita' ne' come familiarita'.

    La chiamata resta appesa finche' l'utente non sceglie o annulla: e' una
    richiesta sincrona servita dal threadpool, quindi non blocca il resto.
    """
    try:
        chosen = pick_folder(STATE.settings["workspace_dir"])
    except DialogUnavailable as exc:
        raise HTTPException(
            501,
            f"Selettore di sistema non disponibile: {exc}. "
            "Imposta il percorso da Impostazioni.",
        ) from exc

    if not chosen:
        return {"cancelled": True}

    path = Path(chosen).expanduser()
    if not path.is_dir():
        raise HTTPException(400, f"'{chosen}' non e' una cartella.")
    STATE.settings["workspace_dir"] = str(path.resolve())
    remember_workspace()
    STATE.persist()
    # La cartella appena scelta e' il momento giusto per costruire l'immagine:
    # l'utente ha appena dichiarato su cosa vuole lavorare, e la build parte
    # mentre scrive il primo messaggio invece che dopo.
    maybe_prepare_workspace()
    return {
        "cancelled": False,
        "workspace_dir": STATE.settings["workspace_dir"],
        "recent_workspaces": STATE.settings["recent_workspaces"],
        "stats": session_stats(STATE.last_opened),
        "jobs": PREP.snapshot(),
    }


# ---------------------------------------------------------------------------
# Rotte: diagnostica
# ---------------------------------------------------------------------------


@app.get("/api/gpu")
def gpu() -> JSONResponse:
    """Occupazione della scheda che esegue il modello.

    Passa da ``_vram_snapshot``, quindi segue l'endpoint: con Ollama su
    un'altra macchina non riporta piu' la GPU locale. Il pannello mostrava
    l'utilizzo della scheda sbagliata -- o "non disponibile" su una macchina
    senza GPU -- proprio mentre il modello girava altrove.
    """
    try:
        snap = _vram_snapshot()
    except Exception:  # noqa: BLE001
        return JSONResponse({"available": False})

    if snap.get("used") is None and snap.get("total") is None:
        return JSONResponse({
            "available": False,
            "remote": snap.get("remote", False),
            "host": snap.get("host"),
            # Con l'endpoint remoto la mancanza di dati ha una causa precisa e
            # una soluzione precisa: e' un'informazione, non un guasto.
            "detail": (
                "Ollama gira su un'altra macchina: dichiara la VRAM totale "
                "della scheda in Impostazioni per vedere l'occupazione."
                if snap.get("remote") else "nvidia-smi non disponibile."
            ),
        })

    return JSONResponse({
        "available": True,
        "remote": snap.get("remote", False),
        "name": snap.get("name") or (
            f"GPU remota ({snap.get('host')})" if snap.get("remote") else "GPU"
        ),
        "used": snap.get("used"),
        "total": snap.get("total"),
        "util": snap.get("util"),
        "models": snap.get("models") or [],
        "source": snap.get("source"),
    })


__all__ = ["app", "STATE", "RUNNERS"]
