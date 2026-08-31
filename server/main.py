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
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager
from dataclasses import asdict, is_dataclass, replace
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlparse

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.datastructures import MutableHeaders

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import agent as agent_mod
from core import memory as memory_mod
from core import notes as notes_mod
from core import skills as skills_mod
from core import plan as plan_mod
from core import profiles
from core import sandbox as sandbox_mod
from core import session as session_mod
from core import settings as settings_mod
from core import vault as vault_mod
from server import previewhost
from core.backend import (
    build_backend,
    forget_model_info,
    normalise_base_url,
)
from core.config import (
    DEFAULTS,
    SKILLS_DIR,
    SKILLS_SUBDIR_WORKSPACE,
    APP_NAME,
    APP_VERSION,
    THINK_AUTO_LEVEL,
    GenParams,
    resolve_think,
    resolve_tristate,
)
from core.prompts import (
    SYSTEM_PROMPT,
    build_attachments_block,
    build_env_header,
    build_system_prompt,
    append_web_search_clause,
    is_stock_prompt,
    pick_system_prompt,
)
from core.textutils import estimate_messages_tokens, estimate_tokens
from core.tools import (
    ATTACHMENTS_DIR,
    MAX_IMAGE_BYTES,
    MAX_IMAGES_IN_CONTEXT,
    TOOLS_SCHEMA,
    TOOLS_SCHEMA_LEAN,
    VAULT_SEARCH_TOOL,
    ToolContext,
    WEB_SEARCH_TOOLS,
    WEB_SEARCH_TOOLS_LEAN,
    WorkspaceError,
    is_image,
    load_images_b64,
    preview_root,
    resolve_path,
    store_attachment,
)
from server.nativedialog import DialogUnavailable, pick_folder
from server.prep import Prep
from server import runner as runner_mod
from server.runner import RunnerRegistry, TurnRunner

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
    # I marchi "c'e' un'app viva nella sandbox" sopravvivono al processo che li
    # ha scritti; i container no. Quelli rimasti da un riavvio precedente sono
    # bugie che costano un giro di Docker al primo cambio di conversazione, per
    # andare a fermare un processo che non esiste piu'.
    sandbox_mod.dimentica_marchi_vivi()
    autostart_docker()
    yield
    # Il server delle anteprime si e' acceso alla prima pagina mostrata, in un
    # thread demone: un demone morirebbe comunque all'uscita, ma chiedere e'
    # piu' pulito che tagliare -- e con --reload il processo non esce affatto.
    previewhost.shutdown()


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
        # Cartelle gia' spazzate dai container rimasti in piedi da prima di un
        # riavvio: una volta per cartella e per avvio. Vedi ``smonta_se_serve``.
        self._spazzate: set[str] = set()
        # Bootstrap pigro: la sessione piu' recente si carica alla prima
        # richiesta, non qui. Il file puo' pesare megabyte e veniva letto e
        # parsato durante l'import del modulo, prima ancora che il server si
        # mettese in ascolto; cosi' l'avvio non paga piu' il JSON piu' grande.
        self._booted = False
        self._last_opened: str = ""

    def _ensure_bootstrap(self) -> None:
        """Alla prima lettura di ``last_opened``: ripristina l'ultima sessione.

        Doppio check sul flag: fuori dal lock per la via veloce, sotto il lock
        per chi arriva mentre un altro sta ancora facendo il bootstrap.
        """
        if self._booted:
            return
        with self._lock:
            if self._booted:
                return
            bootstrap: dict[str, Any] = {}
            session_mod.bootstrap_session(bootstrap)
            self._sessions[bootstrap["current_session_id"]] = bootstrap
            self._last_opened = bootstrap["current_session_id"]
            self._booted = True

    @property
    def last_opened(self) -> str:
        self._ensure_bootstrap()
        return self._last_opened

    @last_opened.setter
    def last_opened(self, value: str) -> None:
        # Chi apre una conversazione la sta per usare subito: se il bootstrap
        # non e' ancora avvenuto meglio farlo adesso, non rimandarlo.
        self._ensure_bootstrap()
        self._last_opened = value

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

    def ricorda_web_search(self, session_id: str, acceso: bool) -> None:
        """Stato della goccia con cui e' partito l'ultimo turno.

        Vive in memoria e non sul disco di proposito: serve solo a riprendere
        un turno sospeso da una domanda all'utente, che e' una cosa che comincia
        e finisce dentro lo stesso processo. Se il server viene riavviato con
        una domanda in sospeso, si riparte da spenta -- il verso giusto in cui
        sbagliare per un permesso.
        """
        self.session(session_id)["web_search"] = bool(acceso)

    def ricorda_think_level(self, session_id: str, livello: str | None) -> None:
        """Livello di pensiero della goccia con cui e' partito il turno.

        Stessa vita effimera di ``web_search``: serve solo alla ripresa dopo
        ``ask_user_question``, che resta nello stesso processo.
        """
        self.session(session_id)["think_level"] = livello

    def think_level_di(self, session_id: str) -> str | None:
        """Livello di pensiero scelto dal composer per il turno in corso."""
        return self.session(session_id).get("think_level")

    def web_search_di(self, session_id: str) -> bool:
        return bool(self.session(session_id).get("web_search"))

    def ferma_anteprime(self) -> bool:
        """Ferma i processi in background e poi smonta il container della sandbox.

        La parte che conta e' la seconda: ``ensure_container`` pubblica
        l'intero intervallo con un solo ``-p``, e Docker tiene i proxy sulle
        porte dell'host per tutta la vita del container -- dentro ci sia una
        preview o no. Un cambio chat mentre le app di un'altra conversazione
        stanno girando non libera dunque niente, finche' il container vive;
        e' anche per questo che ogni nuovo turno riprova ``-p`` sulle stesse
        porte e puo' fallire al bind se un altro harness usa gia' la base.

        Il container e' senza stato (il lavoro e' nel volume del workspace),
        quindi non si perde niente, a parte i log dei processi in background.
        Non deve mai alzare eccezione: un guasto qui non puo' bloccare l'apertura
        di una conversazione.
        """
        fermato = False
        try:
            s = self.settings
            if (
                str(s.get("sandbox")) == "docker"
                and s.get("sandbox_network")
                and Path(self.settings["workspace_dir"]).is_dir()
            ):
                kwargs = {
                    "image": str(self.settings["docker_image"]),
                    "network": True,
                    "ports": self.preview_ports(),
                }
                ws = str(self.settings["workspace_dir"])
                if sandbox_mod.background_live(ws):
                    # L'harness ha davvero avviato un'app in background: si
                    # ferma per slot, come prima.
                    fermato = (
                        sandbox_mod.stop_background(ws, **kwargs)
                        or bool(sandbox_mod.stop(ws))
                    )
                else:
                    # Nessun marchio su disco: non esiste un processo da
                    # uccidere e non si ricrea il container per verificarlo --
                    # era la causa del cambio-chat lento. Resta una spazzata
                    # leggera per eventuali container rimasti in piedi (es. da
                    # prima di un riavvio): una sola verifica se c'e' gia' tutto pulito.
                    fermato = bool(sandbox_mod.stop(ws))
                if fermato:
                    # I pannelli di anteprima delle altre conversazioni puntano
                    # a porte che non esistono piu': senza questo passo restereb-
                    # bero link morti fino al prossimo serve.
                    for sessione in list(self._sessions.values()):
                        sessione.pop("preview", None)
        except Exception:  # noqa: BLE001 - mai bloccare il cambio chat
            # Nessun canale per l'errore: chi chiama questa funzione sta
            # aprendo una conversazione, e non c'e' niente che possa fare con
            # un guasto di Docker. Il fork aveva un parametro ``eccezione`` per
            # raccoglierlo, che nessuno dei tre chiamanti passava mai.
            return fermato
        return fermato

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

    def save(self, session_id: str, *, riscrivi: bool = False) -> None:
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
            session_mod.save_session(session, force=True, riscrivi=riscrivi)

    def workspace_di(self, session_id: str) -> str:
        """La cartella su cui questa conversazione ha lavorato.

        E' registrata ad ogni salvataggio. Riaprendo una chat di ieri e' il
        dato che permette di rimettere l'harness dov'era: una conversazione
        parla di *quel* progetto, e ritrovarsela puntata su un altro workspace
        significa che il primo comando lavora sui file sbagliati.
        """
        return str(self.session(session_id).get("workspace_dir") or "")

    def new_session(self) -> str:
        fresh: dict[str, Any] = {}
        session_id = session_mod.new_session(fresh)
        # La chat nuova nasce sulla cartella aperta adesso: cosi' il legame
        # esiste dal primo istante, non dal primo salvataggio.
        fresh["workspace_dir"] = self.settings["workspace_dir"]
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
        backend = self._backend_se_gia_pronto()
        if backend is None:
            # Nessun backend costruito: qui non si costruisce. Vedi
            # ``_backend_se_gia_pronto``.
            return resolve_tristate("auto", None)
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
        params = GenParams(
            model=s["model_name"],
            temperature=float(s["temperature"]),
            top_p=float(s["top_p"]),
            max_tokens=int(s["max_tokens"]),
            num_ctx=int(s["num_ctx"]),
            num_gpu=int(s["num_gpu"]),
            keep_alive=str(s["keep_alive"]),
            top_k=int(s["top_k"]),
            presence_penalty=float(s["presence_penalty"]),
            repetition_penalty=float(s["repetition_penalty"]),
            # Il livello ('high', 'max', ...) sopravvive fino alla richiesta:
            # thinking_enabled() lo schiaccia a booleano solo per scegliere il
            # system prompt, dove un livello non cambierebbe nulla.
            think=self.think_setting(),
        )
        # Il server vince sull'impostazione. Su llama-server la finestra la
        # fissa -c al lancio: chiederne una piu' grande non la allarga, fa solo
        # credere all'harness uno spazio che non c'e' -- e i budget di
        # troncamento (budgets_for) si tarerebbero su un numero falso, mentre
        # il server taglia o fa context shift senza dire niente.
        limite = getattr(self.backend(), "clamp_num_ctx", None)
        if callable(limite):
            params.num_ctx = int(limite(params.num_ctx))
        return params

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

    def _backend_se_gia_pronto(self):
        """Il backend, ma solo se esiste gia'. Non lo costruisce.

        Costruirlo con ``transport: auto`` -- il valore di serie -- costa due
        sonde di rete da quattro secondi, e le due capability (pensiero,
        visione) venivano interrogate anche da ``/api/bootstrap``, cioe' dalla
        richiesta che serve a **disegnare la pagina**. Erano fino a otto
        secondi di pagina bianca dentro la funzione il cui docstring promette
        "niente di remoto".

        Una capability non vale il prezzo di costruire il backend: chi la
        chiede prima che il backend esista riceve la risposta prudente, e la
        sonda vera la fa ``/api/backend`` a pagina gia' viva -- dopo di che
        l'istanza c'e' e la capability si rileva normalmente.
        """
        with self._lock:
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
        return skills_mod.render_blocco(skills_mod.scegli(tutte, richiesta))

    def system_prompt(
        self,
        web_search: bool = False,
        *,
        pensiero: bool | None = None,
    ) -> str:
        """Prompt di sistema effettivo per il modello in uso.

        Finche' il testo e' uno dei nostri, lo sceglie l'harness in base alle
        capability: snello per i modelli che ragionano, esteso per quelli che
        hanno bisogno delle stampelle. Appena l'utente lo riscrive, la scelta
        automatica si fa da parte -- il suo testo vince sempre.

        ``pensiero`` e' l'override della goccia del composer: True/False
        forza la descrizione del canale di pensiero per questo turno, None
        lascia la decisione a ``thinking_enabled()`` (impostazioni).
        """
        thinking = self.thinking_enabled() if pensiero is None else pensiero
        base = self.settings["system_prompt"]
        if is_stock_prompt(base):
            if vault_mod.is_modalita_vault(self.settings["workspace_dir"]):
                # Il workspace e' un vault LLM Wiki: qui l'agente non e' un
                # ingegnere generico, e' il manutentore della wiki. Il testo
                # personalizzato dell'utente, se c'e', vince sempre.
                base = vault_mod.VAULT_SYSTEM_PROMPT
            else:
                base = pick_system_prompt(thinking=thinking)
        prompt = append_web_search_clause(
            build_system_prompt(base, native_think=thinking),
            enabled=web_search,
        )
        if vault_mod.is_modalita_vault(self.settings["workspace_dir"]):
            # Solo la **struttura**: percorsi e nomi di cartella, che due
            # ingest di fila producono identici. Lo stato della wiki -- indice
            # e coda del log -- va in coda con ruolo 'user', come il piano e
            # le note, e per la stessa ragione: qui invalidava il prefisso a
            # ogni ingest, cioe' proprio nell'operazione per cui la modalita'
            # wiki esiste. Vedi ``core/vault.blocco_stato``.
            prompt += vault_mod.blocco_struttura(self.settings["workspace_dir"])
        # Le istruzioni del vault valgono anche fuori dalla modalita' wiki: e'
        # il senso di averle separate dalla descrizione. Vanno dopo il blocco
        # del manutentore perche' sono dell'utente, e l'ultima parola su come
        # si lavora in una cartella e' di chi ci lavora.
        if vault_mod.is_registrato(self.settings["workspace_dir"]):
            prompt += vault_mod.blocco_istruzioni(
                vault_mod.leggi_config(self.settings["workspace_dir"])
            )
        return prompt + memory_mod.format_for_prompt(self.memories)

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
        """Valore di ``think`` da mandare al modello: un livello, o False.

        Con ``auto`` si ritorna il **livello** ``THINK_AUTO_LEVEL`` invece di
        ``True``, e non e' un dettaglio di forma: ``agent.think_for_step``
        abbassa il pensiero di uno scalino col punto di piano aperto e di due
        dal terzo passo, ma sa farlo solo su una scala -- davanti a un booleano
        restituisce il valore intatto. Con ``auto`` (che e' il default) tutta
        quella modulazione era quindi **inerte**, e non si vedeva da nessuna
        parte: misurato il 23/08/2026 sulle quattro sessioni qwen3.8, il
        pensiero non si accorciava mai col passo (mediana 3.327 caratteri al
        passo 1, 3.452 al passo 8+).

        Il costo: i livelli li accettano solo le versioni recenti di Ollama.
        Non e' un rischio nuovo -- la tendina delle impostazioni offre gia'
        ``low|medium|high|max`` e li manda sul filo allo stesso modo -- e il
        backend impara dal rifiuto e ripiega sul booleano (vedi
        ``OllamaBackend._livello_rifiutato``).
        """
        setting = self.settings["native_think"]
        if isinstance(setting, str) and setting.strip().lower() == "auto":
            return THINK_AUTO_LEVEL if self.thinking_enabled() else False
        return resolve_think(setting, None)

    def vision_enabled(self) -> bool:
        """Il modello in uso legge le immagini? Rilevato, non configurato."""
        key = (self.settings["api_base"], self.settings["model_name"])
        if self._vision is not None and self._vision[0] == key:
            return self._vision[1]
        backend = self._backend_se_gia_pronto()
        if backend is None:
            return False
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

    def tools_schema(self, web_search: bool = False) -> list[dict[str, Any]]:
        """Schemi completi o snelli, con la stessa regola del system prompt.

        Gli schemi dei tool sono il blocco fisso piu' grande della richiesta,
        piu' del prompt di sistema in entrambi i percorsi, e si pagano ad ogni
        passo agentico. Le descrizioni lunghe servono a un modello che fatica a
        collegare la richiesta al tool; a uno che ragiona sono peso morto.
        Quanto pesino di preciso lo dice ``prompts.costi_del_prefisso()``: i
        numeri che stavano scritti qui erano sbagliati di 2,4x e 4,9x, perche'
        erano veri il giorno in cui qualcuno li aveva misurati.
        """
        snello = self.thinking_enabled()
        base = TOOLS_SCHEMA_LEAN if snello else TOOLS_SCHEMA
        if not self.settings.get("vaults"):
            # Stessa regola della ricerca online, applicata al vault: senza
            # nessun vault registrato, ``vault_search`` e' un tool che puo'
            # solo fallire. Descriverlo ad ogni passo costa token per far
            # sapere al modello che esiste una porta chiusa a chiave.
            base = [
                t for t in base if t["function"]["name"] != VAULT_SEARCH_TOOL
            ]
        if not web_search:
            # Goccia "Ricerca online" spenta -> web_search non esiste per il
            # modello: nessuno schema, nessun token pagato per un tool che non
            # potrebbe usare.
            return base
        return base + (WEB_SEARCH_TOOLS_LEAN if snello else WEB_SEARCH_TOOLS)

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

    def tool_ctx(self, session_id: str, web_search: bool = False) -> ToolContext:
        def persist(memories: list[dict[str, str]]) -> None:
            self.memories = memories
            memory_mod.save_memories(memories)

        def persist_plan(plan: plan_mod.Plan) -> None:
            self.store_plan(session_id, plan)

        def persist_notes(notes: notes_mod.Notes) -> None:
            self.store_notes(session_id, notes)

        # Il vault in cui si sta lavorando, se il workspace ne e' uno. Vuoto
        # altrove: e' cio' che rende ``manage_notes ambito='vault'`` possibile
        # qui e un errore pulito da tutte le altre parti.
        workspace = str(self.settings["workspace_dir"])
        vault_corrente = workspace if vault_mod.is_registrato(workspace) else ""

        return ToolContext(
            workspace=self.settings["workspace_dir"],
            timeout_s=int(self.settings["timeout_seconds"]),
            # La goccia era accesa quando il turno e' partito: senza questo
            # campo la guardia del tool rifiutava ogni chiamata anche col
            # flag vero (bug della sessione 20260822_211656_cdda).
            web_search_enabled=bool(web_search),
            memories=self.memories,
            touched_files=self.touched(session_id),
            known_files=self.known(session_id),
            plan=self.plan(session_id),
            notes=self.notes(session_id),
            preview=self.preview(session_id),
            preview_ports=self.preview_ports(),
            preview_autostart_backend=bool(
                self.settings.get("preview_autostart_backend")
            ),
            on_memories_changed=persist,
            on_plan_changed=persist_plan,
            on_notes_changed=persist_notes,
            allow_dangerous_commands=bool(self.settings["confirm_commands"]),
            registri_vault=list(self.settings.get("vaults") or []),
            # La memoria del vault si carica una volta per turno e vive nel
            # contesto: cambia solo quando la cambia il modello, e in quel caso
            # e' il tool a riscrivere sia il file sia questa lista.
            vault_dir=vault_corrente,
            vault_notes=(
                list(vault_mod.leggi_config(vault_corrente).note) if vault_corrente else []
            ),
            sandbox=str(self.settings["sandbox"]),
            docker_image=str(self.settings["docker_image"]),
            sandbox_network=bool(self.settings["sandbox_network"]),
            deposito_attivo=bool(self.settings["deposito_risultati"]),
            deposito_max_mb=int(self.settings["deposito_max_mb"]),
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


def _e_un_guasto_del_backend(event: Any) -> bool:
    """Questo errore giustifica di buttare via l'istanza del backend?

    Lo dice il flag che chi emette l'evento ha impostato, non una parola
    cercata nel testo: il ciclo sa se la connessione e' caduta, il server non
    lo sa. Un elenco di parole avrebbe preso "Il modello ha pensato troppo a
    lungo" e buttato via un backend perfettamente sano.

    Prima li giustificava **tutti**, e ``AgentError`` non e' un errore fatale:
    e' il canale con cui il ciclo racconta all'utente cosa sta succedendo. Lo
    emettono il watchdog del pensiero, la risposta troncata, il finto
    ``<tool_response>``, la finestra quasi piena -- tutte cose in cui il
    backend sta benissimo. E ``forget_backend`` non e' gratis: azzera
    l'istanza, le capability di pensiero e visione, e chiama
    ``forget_model_info()``, cioe' svuota proprio la memoria dei fallimenti
    introdotta per non pagare sei secondi di timeout ad ogni turno.
    """
    return bool(getattr(event, "guasto_backend", False))


def event_to_sse(event: Any) -> str:
    name = _EVENT_NAMES.get(type(event), "unknown")
    payload = asdict(event) if is_dataclass(event) else {"value": str(event)}
    return sse(name, payload)


class EventBus:
    """Eventi di *processo*: "qualcosa e' cambiato", per chi ascolta.

    ``/api/stream/{id}`` racconta un solo turno a chi lo sta guardando; qui
    passa la notizia larga: e' partito un turno, e' finito, l'agente ha fatto
    una domanda, l'elenco delle chat e' cambiato. Le due interfacce (desktop
    e telefono) se lo sottoscrivono e si ridisegnano da sole, senza polling
    ne' refresh manuale.
    """

    _QUEUE_MAX = 500
    _KEEPALIVE_S = 15.0

    def __init__(self) -> None:
        self._subscribers: list[queue.Queue[str | None]] = []
        self._lock = threading.Lock()
        # Questa e' la risposta che non finisce mai per definizione: ogni
        # scheda aperta sulla UI ne tiene una. Senza questa riga, Ctrl+C non
        # spegne il server finche' c'e' un browser aperto -- uvicorn aspetta
        # educatamente che ``/api/events`` finisca, e non finisce.
        runner_mod.al_spegnimento(self.stacca_tutti)

    def subscribe(self) -> queue.Queue[str | None]:
        sub: queue.Queue[str | None] = queue.Queue()
        with self._lock:
            self._subscribers.append(sub)
        return sub

    def stacca_tutti(self) -> None:
        """Chiude tutte le connessioni al bus: un colpetto per ognuna."""
        with self._lock:
            subscribers = list(self._subscribers)
        for sub in subscribers:
            sub.put(None)

    def unsubscribe(self, sub: queue.Queue[str | None]) -> None:
        with self._lock:
            if sub in self._subscribers:
                self._subscribers.remove(sub)

    def publish(self, event_type: str, **payload: Any) -> None:
        frame = sse(event_type, payload)
        with self._lock:
            subscribers = list(self._subscribers)
        for sub in subscribers:
            if sub.qsize() < self._QUEUE_MAX:
                sub.put(frame)

    def stream(self) -> Iterator[str]:
        """SSE: saluto, poi ogni evento finche' il client resta collegato.

        Finisce in due casi soli: il client se ne va (il generatore viene
        chiuso), oppure il processo si sta spegnendo -- ed e' il secondo che
        va detto, perche' e' l'unico modo che ha Ctrl+C di funzionare.
        """
        sub = self.subscribe()
        try:
            yield sse("hello", {"ts": time.time()})
            while not runner_mod.SPEGNIMENTO.is_set():
                try:
                    frame = sub.get(timeout=self._KEEPALIVE_S)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                if frame is None:      # il colpetto dello spegnimento
                    return
                yield frame
        finally:
            self.unsubscribe(sub)


EVENTS = EventBus()


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


def contesto_usato(session_id: str) -> int:
    """Quanti token occupa la conversazione, ricalcolati solo se serve.

    Ricostruire i messaggi per l'API e stimarli costa quanto **tutta** la
    cronologia: su una chat da cinquecento messaggi sono decine di
    millisecondi, e ``session_stats`` viene chiamata a ogni apertura, a ogni
    fine turno, a ogni invio e a ogni riallineamento. Il numero pero' cambia
    solo quando cambia uno dei suoi ingredienti, e sono tutti a portata di
    mano: quanti messaggi ci sono, e le due manopole che decidono cosa entra
    in contesto. L'intestazione dell'ambiente e il system prompt entrano per
    lunghezza -- basta a distinguerli, e non costa una copia.
    """
    messages = STATE.messages(session_id)
    prompt = STATE.system_prompt()
    header = STATE.context_header(session_id)
    strip = bool(STATE.settings["strip_think_from_context"])
    compatta = bool(STATE.settings["compact_old_tool_results"])
    # ``header or ""``: con ``auto_env_header`` spento ``context_header``
    # ritorna None, e ``len(None)`` sollevava un TypeError ad **ogni**
    # ``session_stats`` -- cioe' ad ogni apertura di chat, fine turno, invio e
    # riallineamento. La barra del contesto spariva e nessuno sapeva perche'.
    chiave = (len(messages), len(prompt), len(header or ""), strip, compatta)

    sessione = STATE.session(session_id)
    memo = sessione.get("_contesto_memo")
    if isinstance(memo, tuple) and memo[0] == chiave:
        return memo[1]

    api_messages = agent_mod.build_api_messages(
        messages,
        system_prompt=prompt,
        env_header=header,
        strip_thinking=strip,
        compact_old_tools=compatta,
    )
    usato = estimate_messages_tokens(api_messages) + STATE.tool_schema_tokens()
    sessione["_contesto_memo"] = (chiave, usato)
    return usato


def session_stats(session_id: str) -> dict[str, Any]:
    return {
        "context_used": contesto_usato(session_id),
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
        "sessions": elenco_corrente(),
        "session_id": session_id,
    }


def session_list(*, cartella: str = "", solo_libere: bool = False) -> list[dict[str, Any]]:
    """Elenco di conversazioni, con lo stato "sta girando" gia' dentro.

    ``cartella`` -> le chat di quel vault. ``solo_libere`` -> l'elenco
    generale, da cui le chat dei vault sono escluse: si arriva a quelle
    aprendo il vault, e vederle anche qui vorrebbe dire lo stesso posto in due
    elenchi diversi.
    """
    running = RUNNERS.running_ids()
    sessions = session_mod.list_sessions(
        cartella=cartella,
        escludi=[v["path"] for v in _registro_vault()] if solo_libere else (),
    )
    for item in sessions:
        item["running"] = item["id"] in running
    return sessions


def elenco_corrente() -> list[dict[str, Any]]:
    """L'elenco che va nella colonna di sinistra: le conversazioni **libere**.

    Prima cambiava sotto i piedi -- dentro un vault diventava l'elenco di quel
    vault -- e le conversazioni recenti sparivano finche' non si usciva. Ma
    aprire un vault non e' andarsene: si apre un posto di lavoro, e le chat di
    quel posto ora stanno **sotto di lui**, annidate nella sezione Vault (vedi
    ``/api/vaults/home``). Cosi' i due elenchi sono visibili insieme e nessuno
    dei due copre l'altro; l'unico posto in cui una chat compare due volte non
    esiste, perche' qui i vault restano esclusi come sempre.
    """
    return session_list(solo_libere=True)


# ---------------------------------------------------------------------------
# Esecuzione di un turno
# ---------------------------------------------------------------------------


def ultima_richiesta(messages: list[dict[str, Any]]) -> str:
    """Il testo dell'ultimo messaggio vero dell'utente. I solleciti non contano."""
    for msg in reversed(messages):
        if msg.get("role") == "user" and not msg.get("hidden"):
            return str(msg.get("content") or "")
    return ""


def start_turn(
    session_id: str,
    web_search: bool = False,
    think_level: str | None = None,
) -> TurnRunner:
    """Avvia il turno in background per la conversazione indicata.

    ``web_search`` e' lo stato della goccia al momento dell'invio: viene
    letto qui una volta sola, perche' il worker gira dopo che la richiesta
    HTTP e' finita e l'oggetto request non esiste piu'. Viene anche ricordato
    sulla sessione, cosi' una ripresa dopo ``ask_user_question`` riparte con
    lo stesso permesso invece di perderlo per strada.

    ``think_level`` e' il livello di pensiero scelto dalla goccia accanto
    all'invio ("low" | "medium" | "high"), o None/"auto" per non avere
    override: vale cio' che le impostazioni hanno deciso per ``native_think``.
    Come ``web_search`` viene ricordato sulla sessione per la ripresa.
    """
    STATE.ricorda_web_search(session_id, web_search)
    STATE.ricorda_think_level(session_id, think_level)
    messages = STATE.messages(session_id)
    snapshot = [dict(m) for m in messages]

    images, esclusi = STATE.turn_images(session_id)

    def work(runner: TurnRunner) -> None:
        runner.emit(sse("start", {"session_id": session_id}))
        # Gli allegati che non sono entrati in contesto si dicono, e si dicono
        # PRIMA che il modello parli.
        #
        # ``load_images_b64`` calcolava gia' questa lista e il suo docstring
        # dice perche' ("l'utente deve sapere che non sono in contesto"),
        # ``turn_images`` la propagava, e la UI ha da sempre il commento che
        # promette l'avviso ("immagini entrate in contesto, allegati esclusi").
        # Mancava solo chi lo mandasse: la variabile veniva spacchettata e
        # buttata. Si allegavano quattro immagini, una pesava troppo, il
        # modello ne riceveva tre e rispondeva come se fossero tutte.
        if esclusi:
            runner.emit(sse("note", {"message":
                f"Non sono entrati in contesto ({len(esclusi)}): "
                + ", ".join(esclusi)
                + f". Il tetto e' {MAX_IMAGES_IN_CONTEXT} immagini per "
                f"turno e {MAX_IMAGE_BYTES // (1024 * 1024)} MB "
                "ciascuna; anche un file illeggibile finisce qui."
            }))
        try:
            # Il livello della goccia e' un override puntuale: sostituisce
            # ``native_think`` per questo turno soltanto, senza toccare le
            # impostazioni, che restano quelle salvate.
            params_turno = STATE.gen_params()
            pensiero_forzato = None
            if think_level in ("low", "medium", "high"):
                params_turno = replace(params_turno, think=think_level)
                # Con un livello esplicito il canale di pensiero c'e': anche
                # il system prompt deve descriverlo, altrimenti dice al
                # modello di non usarlo mentre il backend lo chiede.
                pensiero_forzato = True
            events = agent_mod.run_turn(
                backend=STATE.backend(),
                params=params_turno,
                tools_schema=STATE.tools_schema(web_search),
                tool_ctx=STATE.tool_ctx(session_id, web_search),
                ui_messages=messages,
                system_prompt=STATE.system_prompt(web_search, pensiero=pensiero_forzato),
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
                compact_max_tokens=int(STATE.settings["compact_max_tokens"]),
                libreria_attiva=bool(STATE.settings["libreria_concetti"]),
                estratto_pensiero=bool(STATE.settings["estratto_pensiero"]),
                spec_delega=bool(STATE.settings["spec_delega"]),
                auto_preview=bool(STATE.settings["preview_enabled"]),
                think_watchdog=bool(STATE.settings["think_watchdog"]),
            )
            for event in events:
                if isinstance(event, agent_mod.AgentError) and _e_un_guasto_del_backend(
                    event
                ):
                    STATE.forget_backend()
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
                # La domanda dell'agente vale anche per chi non sta guardando
                # questo turno: sul telefono deve spuntare il riquadro di
                # risposta senza refresh, come sul desktop.
                if isinstance(event, agent_mod.AwaitingUserInput):
                    EVENTS.publish("question", session_id=session_id)
        except Exception as exc:  # noqa: BLE001 - l'errore va mostrato, non nascosto
            runner.emit(sse("error", {"message": f"{type(exc).__name__}: {exc}"}))
        finally:
            # Riscrittura completa della coda, una volta per turno: durante il
            # turno si accoda, e l'accodamento non vede le modifiche fatte a
            # messaggi gia' scritti (la traccia del pensiero che si attacca
            # all'assistente del passo prima, una compattazione). Qui la
            # cronologia e' ferma, ed e' il posto giusto per rimetterla in pari.
            STATE.save(session_id, riscrivi=True)
            runner.emit(sse("state", session_stats(session_id)))
            # Fine turno su TUTTE le interfacce: chi ha la chat aperta la
            # ricarica dal disco (ora c'e' il messaggio finale), gli altri
            # aggiornano solo l'elenco.
            EVENTS.publish("turn", session_id=session_id, running=False)

    EVENTS.publish("turn", session_id=session_id, running=True)
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
    # Modalita' "Ricerca online" della goccia nel composer: vale per questo
    # messaggio soltanto. Vero -> il turno vede lo schema di web_search e la
    # riga di prompt che lo descrive; falso -> il modello non sa nemmeno che
    # esiste (nessun token pagato per un tool spento).
    web_search: bool = False
    # Livello di pensiero scelto dalla goccia del composer per QUESTO turno.
    # Solo tre valori ammessi: qualunque altra stringa e' un errore del
    # client (422 di pydantic), non qualcosa da ignorare in silenzio.
    # "auto" NON viaggia sulla rete: l'assenza del campo vale come auto.
    think_level: Literal["low", "medium", "high"] | None = None


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


_NOMI_TRANSPORT = {
    "ollama": "Ollama",
    "llamacpp": "llama.cpp",
    "openai": "OpenAI-compatibile",
}


def _tipo_compatibile(valore: Any, atteso: Any) -> bool:
    """Il valore ha un tipo che ``load_settings`` accettera' al prossimo avvio?

    Stessa regola di ``settings.load_settings``, applicata all'ingresso invece
    che alla rilettura: un booleano non passa per un intero (``isinstance(True,
    int)`` e' vero, ed e' il caso che ci si scorda), e un intero passa per un
    float perche' JSON non distingue ``1`` da ``1.0``.
    """
    if isinstance(atteso, bool):
        return isinstance(valore, bool)
    if isinstance(atteso, int):
        return isinstance(valore, int) and not isinstance(valore, bool)
    if isinstance(atteso, float):
        return isinstance(valore, (int, float)) and not isinstance(valore, bool)
    return isinstance(valore, type(atteso))


def _nome_transport(transport: str) -> str:
    """Il nome del transport senza toccare la rete.

    Con ``auto`` non si sa ancora, e dirlo e' piu' onesto che scoprirlo con due
    sonde da quattro secondi mentre l'utente guarda una pagina bianca: la sonda
    vera la fa ``/api/backend``, che gira a pagina gia' disegnata.
    """
    return _NOMI_TRANSPORT.get((transport or "auto").lower(), "in rilevamento")


@app.get("/api/bootstrap")
def bootstrap() -> dict[str, Any]:
    """Tutto quello che serve a disegnare la pagina, e **niente di remoto**.

    Qui dentro si facevano tre viaggi di rete verso il server del modello --
    ``status()``, ``version()``, ``streams_tool_calls()`` -- prima di
    rispondere. Con il modello su un'altra macchina spenta erano fino a una
    decina di secondi di **pagina bianca**: non un'interfaccia lenta, proprio
    nessuna interfaccia, mentre tutto cio' che serviva a disegnarla (le
    impostazioni, la conversazione, le memorie) era gia' su questo disco.

    Adesso la pagina si disegna con ``online: null`` -- la goccia dice
    "controllo..." -- e la sonda va per conto suo su ``/api/backend``. Chi ha
    il modello acceso non se ne accorge: e' un giro di rete in piu' su una
    pagina gia' viva.
    """
    session_id = STATE.last_opened
    # **Non** ``STATE.backend()``: con ``transport: auto`` -- che e' il valore
    # di serie -- costruire il backend fa due sonde di rete, ``ping()`` e
    # ``parla_llamacpp()``, quattro secondi di timeout ciascuna. Erano fino a
    # otto secondi di pagina bianca, dentro la funzione il cui docstring
    # promette "niente di remoto".
    #
    # Il nome del transport si sa senza chiedere a nessuno: e' l'impostazione,
    # e con ``auto`` la risposta onesta e' "lo sto ancora decidendo" -- che e'
    # esattamente lo stato che la goccia sa gia' disegnare.
    nome_backend = _nome_transport(STATE.settings["transport"])
    return {
        "app": {"name": APP_NAME, "version": APP_VERSION},
        "settings": STATE.settings,
        "session": open_payload(session_id),
        "memories": STATE.memories,
        "backend": {
            "name": nome_backend,
            # None = non ancora chiesto. La goccia sa gia' disegnare i tre
            # stati (in attesa, online, offline).
            "online": None,
            "detail": "",
            "url": normalise_base_url(STATE.settings["api_base"]),
            "version": "",
            "models": [],
            "streams_tools": None,
        },
    }


@app.get("/api/backend")
def backend_info() -> dict[str, Any]:
    """La sonda che il bootstrap non fa piu': stato, versione, modelli.

    Tre domande al server del modello in una richiesta sola, perche' sono tre
    round-trip e farne tre richieste separate dal client vorrebbe dire tre
    attese in fila invece di una.
    """
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

    return {
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
        # Se e' stato sostituito, il client deve saperlo: la tendina in alto
        # mostrerebbe ancora quello che non c'e' piu'.
        "model_name": STATE.settings["model_name"],
    }


# Quanti messaggi viaggiano all'apertura di una conversazione.
#
# Una chat agentica lunga sono migliaia di messaggi, per la gran parte
# risultati di tool: mandarli tutti voleva dire megabyte di JSON e qualche
# secondo di disegno prima che si vedesse qualcosa -- per arrivare, nove volte
# su dieci, in fondo, dove sta l'unica cosa che si stava andando a leggere.
# Quindici coprono l'ultimo scambio con i suoi tool; il resto arriva
# risalendo, un blocco per volta. Erano quaranta, ed e' stato un errore di
# taratura misurato: sulla chat piu' pesante di questa postazione la coda
# passa da 288 kB a 116, ma il numero che conta non e' quello -- sono le
# **tendine**, perche' ogni risultato di tool diventa nodi nel DOM e quaranta
# messaggi di una chat agentica sono in maggioranza risultati di tool.
MESSAGGI_PER_PAGINA = 15


def open_payload(session_id: str) -> dict[str, Any]:
    """Tutto il necessario per disegnare una conversazione.

    ``messages`` e' la **coda** della cronologia, non tutta: gli ultimi
    ``MESSAGGI_PER_PAGINA``, con ``messages_offset`` a dire da che punto
    comincia e ``messages_total`` quanti ce ne sono. I precedenti si chiedono
    a ``/api/sessions/{id}/messages`` risalendo.

    Se un turno e' in corso, ``messages`` e' la cronologia com'era *prima*
    dell'inizio del turno: il client la disegna e poi si attacca allo stream,
    che gli riapplica gli eventi dall'inizio. Niente doppioni.
    """
    messages = STATE.messages(session_id)
    runner = RUNNERS.get(session_id)
    live = runner is not None and not runner.finished.is_set()
    completi = runner.snapshot if live else messages
    coda = completi[-MESSAGGI_PER_PAGINA:] if MESSAGGI_PER_PAGINA else completi
    return {
        "session_id": session_id,
        "messages": coda,
        # Dove comincia quello che si sta mandando, e quanti ce ne sono in
        # tutto: sono i due numeri che permettono al client di sapere che c'e'
        # dell'altro sopra e da che punto chiederlo. Senza il totale, "sono
        # arrivato in cima" sarebbe indistinguibile da "il server non ne ha
        # mandati abbastanza".
        "messages_offset": len(completi) - len(coda),
        "messages_total": len(completi),
        "running": live,
        "pending": agent_mod.pending_question(messages),
        # Il piano viene dal disco, non dagli eventi: riaprendo una
        # conversazione il pannello si ridisegna anche se il turno che lo ha
        # scritto e' finito ieri.
        "plan": STATE.plan(session_id).to_list(),
        "notes": STATE.notes(session_id).to_list(),
        "preview": STATE.preview(session_id),
        "stats": session_stats(session_id),
        # La cartella di **questa** conversazione. Viaggia nel payload perche'
        # aprire una chat puo' spostare l'harness, e il client deve
        # accorgersene senza una seconda chiamata: il percorso in testa, la
        # scheda della sandbox e i recenti si aggiornano da qui.
        "workspace_dir": STATE.settings["workspace_dir"],
        "recent_workspaces": STATE.settings["recent_workspaces"],
        # Barra superiore mobile: modello in uso e cartella di QUESTA
        # conversazione, entrambi di sola lettura. Campo nuovo e separato da
        # ``workspace_dir`` perche' quello resta lo stato globale dell'harness
        # (il desktop lo usa per la testa del pannello): qui invece vale la
        # chat che si sta guardando, anche se l'apertura di un'altra sessione
        # ha spostato altrove il container.
        "model": STATE.settings["model_name"],
        "session_workspace": STATE.workspace_di(session_id),
    }


# ---------------------------------------------------------------------------
# Rotte: conversazioni
# ---------------------------------------------------------------------------


@app.get("/api/sessions")
def list_sessions(tutte: bool = False) -> dict[str, Any]:
    """Conversazioni per la colonna di sinistra.

    ``tutte=1`` le da' invece **tutte**, chat dei vault comprese. Lo usa il
    telefono, che di vault non sa niente: li' un elenco solo e' l'unico
    elenco che c'e', e togliergli le chat dei vault vorrebbe dire renderle
    irraggiungibili da fuori casa.
    """
    sessions = session_list() if tutte else elenco_corrente()
    return {"sessions": sessions, "current": STATE.last_opened}


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
    # La chat nuova nasce sulla cartella corrente: non si cambia posto, quindi
    # l'unico motivo per smontare e' un'app rimasta viva nella conversazione
    # da cui veniamo. Vedi ``smonta_se_serve``.
    smonta_se_serve()
    nuovo = STATE.new_session()
    # Una chat creata dal telefono (o da un'altra finestra) deve comparire
    # nell'elenco delle altre senza refresh manuale.
    EVENTS.publish("sessions", reason="created", session_id=nuovo)
    return open_payload(nuovo)


@app.post("/api/sessions/{session_id}/open")
def open_session(session_id: str) -> dict[str, Any]:
    # Prima di tutto: la conversazione esiste? Senza questa riga una richiesta
    # con un id sbagliato fermerebbe le anteprime e sposterebbe la cartella
    # *prima* di rispondere 404.
    STATE.session(session_id)
    # E da qui in poi la corrente e' questa. L'ordine conta: cambiare cartella
    # la lega alla chat aperta, e se ``last_opened`` puntasse ancora a quella
    # di prima le scriverebbe addosso il workspace di questa -- cioe' aprire
    # una chat cancellerebbe il ricordo della chat da cui vieni.
    STATE.last_opened = session_id
    if not RUNNERS.running_ids():
        # Si liberano le porte delle app che restavano in piedi nella
        # conversazione da cui venivamo -- ma solo quando serve davvero: vedi
        # ``smonta_se_serve``, che e' la differenza fra due sottoprocessi
        # docker ad ogni apertura di chat e zero.
        smonta_se_serve(session_id)
        ripristina_workspace(session_id)
    return open_payload(session_id)


def cambiera_cartella(session_id: str) -> bool:
    """La conversazione che si sta per aprire porta altrove?

    Domanda a costo zero -- due stringhe normalizzate -- che serve a non
    pagarne una cara.
    """
    registrato = STATE.workspace_di(session_id)
    if not registrato or not Path(registrato).expanduser().is_dir():
        return False
    return session_mod.chiave_cartella(registrato) != session_mod.chiave_cartella(
        str(STATE.settings["workspace_dir"])
    )


def smonta_se_serve(session_id: str = "") -> None:
    """Smonta il container solo quando c'e' davvero un motivo.

    ``ferma_anteprime`` esiste per liberare le porte pubblicate, e le porte
    danno fastidio in due casi soltanto: quando si sta per cambiare cartella
    (il container nuovo proverebbe a pubblicare le stesse) e quando in questa
    c'e' un'app viva da fermare. Fuori da quei due casi smontare e' lavoro
    contro se' stessi -- il container e' senza stato e si ricrea uguale, e nel
    frattempo si sono pagati un ``docker inspect`` e un ``docker rm`` **ad
    ogni apertura di conversazione**.

    Da quando immagine e container si preparano da soli al cambio di cartella
    (``maybe_prepare_workspace``) non e' piu' solo uno spreco: e' distruggere
    esattamente la cosa che si era appena finito di preparare.

    ``background_live`` guarda un marchio su disco, non Docker: costa una stat.
    """
    if RUNNERS.running_ids():
        # Un turno vivo tiene la sandbox occupata per buono: non si tocca.
        return
    ws = str(STATE.settings["workspace_dir"])
    chiave = session_mod.chiave_cartella(ws)
    subito = (
        # 1. Si va altrove: il container della cartella nuova proverebbe a
        #    pubblicare le stesse porte e fallirebbe al bind.
        bool(session_id and cambiera_cartella(session_id))
        # 2. C'e' un'app viva da fermare davvero (marchio su disco, non Docker).
        or sandbox_mod.background_live(ws)
        # 3. Una conversazione ha l'anteprima di un'app: quella tiene le porte
        #    anche quando il marchio manca. E' la chat che ha fatto partire un
        #    server ed e' stata lasciata li'.
        or any(
            (s.get("preview") or {}).get("kind") == "serve"
            for s in STATE._sessions.values()
        )
    )
    # 4. Prima volta in questa cartella, in questo processo: la spazzata dei
    #    container rimasti in piedi da prima di un riavvio. Una volta sola --
    #    rifarla ad ogni apertura di chat era un ``docker inspect`` per
    #    scoprire, quasi sempre, che era gia' tutto pulito.
    spazzata = chiave not in STATE._spazzate
    STATE._spazzate.add(chiave)

    if subito:
        # Qui si aspetta, e si deve: il container nuovo nasce subito dopo
        # (``ripristina_workspace`` -> ``maybe_prepare_workspace``) e
        # troverebbe le porte ancora prese.
        STATE.ferma_anteprime()
    elif spazzata:
        # Qui no: non c'e' niente che dipenda dall'esito, e il risultato quasi
        # sempre e' "era gia' tutto pulito". Su Windows ogni ``docker`` e' un
        # processo nuovo da mezzo secondo buono, e pagarlo **dentro** la
        # richiesta significa una prima apertura di chat che sembra impiantata.
        threading.Thread(
            target=STATE.ferma_anteprime, daemon=True, name="spazzata-sandbox"
        ).start()



def ripristina_workspace(session_id: str) -> bool:
    """Rimette l'harness sulla cartella di questa conversazione.

    Aprire una chat vecchia e trovarsi puntati sul progetto di un'altra e' il
    modo piu' rapido di far scrivere l'agente nel posto sbagliato: la
    cronologia parla di *quei* file, e il primo comando andrebbe altrove.

    Si sposta solo a turni fermi -- il workspace e' uno per processo (una
    sandbox, un albero nell'environment header, un intervallo di porte) e
    cambiarlo sotto un turno vivo vorrebbe dire cambiargli il pavimento sotto
    i piedi. Una cartella sparita nel frattempo si ignora: meglio restare dove
    si e' che rifiutare di aprire una conversazione.
    """
    registrato = STATE.workspace_di(session_id)
    if not registrato:
        return False
    corrente = str(STATE.settings["workspace_dir"])
    if os.path.normcase(os.path.normpath(registrato)) == os.path.normcase(
        os.path.normpath(corrente)
    ):
        return False
    percorso = Path(registrato).expanduser()
    if not percorso.is_dir():
        return False
    applica_workspace(percorso)
    return True


@app.get("/api/sessions/{session_id}")
def read_session(session_id: str) -> dict[str, Any]:
    """Lo stesso payload di ``/open``, ma senza toccare niente.

    Serve a chi si sta solo **riallineando**: il bus globale annuncia la fine
    di un turno e la pagina rilegge la conversazione dal disco, dove ora c'e'
    il messaggio finale. Farlo con la POST ``/open`` -- come faceva il fork --
    significa passare da ``ferma_anteprime()`` ad ogni fine turno: il
    container viene smontato e l'anteprima che l'utente stava guardando
    sparisce da sola, qualche millisecondo dopo che il turno e' finito.
    Aprire e' un gesto, rileggere e' un'altra cosa: due verbi, due rotte.

    Dichiarata dopo ``/api/sessions/search`` di proposito: FastAPI prova le
    rotte in ordine e ``{session_id}`` catturerebbe anche "search".
    """
    return open_payload(session_id)


@app.get("/api/sessions/{session_id}/messages")
def read_messages(session_id: str, before: int, limit: int = MESSAGGI_PER_PAGINA) -> dict[str, Any]:
    """Il blocco di messaggi che sta **prima** di ``before``.

    ``before`` e' un indice nella cronologia, non un id: e' l'``offset`` che
    il client ha ricevuto con il blocco che ha gia' in mano, e chiedere
    "quello che viene prima di questo" e' l'unica domanda che la risalita
    pone. Un cursore per id costringerebbe a scandire la lista per ritrovarlo,
    e i messaggi di una conversazione un id non ce l'hanno nemmeno.

    Durante un turno vivo si legge dallo snapshot del runner e non dal disco,
    o risalendo in una chat in corso si vedrebbe una cronologia piu' corta di
    quella disegnata.
    """
    runner = RUNNERS.get(session_id)
    live = runner is not None and not runner.finished.is_set()
    completi = runner.snapshot if live else STATE.messages(session_id)
    fine = max(0, min(int(before), len(completi)))
    inizio = max(0, fine - max(1, int(limit)))
    return {
        "session_id": session_id,
        "messages": completi[inizio:fine],
        "messages_offset": inizio,
        "messages_total": len(completi),
    }


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str) -> dict[str, Any]:
    if RUNNERS.is_running(session_id):
        raise HTTPException(409, "Un turno e' in corso in questa conversazione.")
    session_mod.delete_session(session_id)
    STATE.drop(session_id)
    if STATE.last_opened == session_id:
        STATE.new_session()
    EVENTS.publish("sessions", reason="deleted", session_id=session_id)
    # Una chat cancellata non deve lasciare le sue anteprime a tenere le porte
    # fino al riavvio dell'app.
    smonta_se_serve()
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
    start_turn(
        session_id,
        web_search=bool(request.web_search),
        think_level=request.think_level,
    )
    # Il messaggio e' gia' in cronologia e il turno e' partito (start_turn ha
    # gia' avvertito tutti con ``turn``): qui avviso solo che l'elenco chat e'
    # cambiato, per titolo e numero di messaggi.
    EVENTS.publish("sessions", reason="message", session_id=session_id)
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
    # La ricerca online era accesa quando il turno si e' fermato per fare la
    # domanda: riprendendo deve restare accesa. Senza questo, un compito che
    # passava da ``ask_user_question`` perdeva il permesso a meta' strada, e
    # il modello si prendeva un "modalita' non attiva" dalla guardia del tool
    # subito dopo che l'utente gli aveva risposto -- cioe' nel punto in cui
    # sembra di piu' un bug.
    start_turn(
        session_id,
        web_search=STATE.web_search_di(session_id),
        think_level=STATE.think_level_di(session_id),
    )
    # La risposta e' partita: l'altro schermo chiude il riquadro della
    # domanda e vede il nuovo turno senza refresh.
    EVENTS.publish("sessions", reason="answer", session_id=session_id)
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


@app.get("/api/ping")
def ping_backend() -> dict[str, Any]:
    """Solo lo stato dell'endpoint, senza effetti collaterali.

    La goccia in alto la interroga a intervalli. ``/api/models`` avrebbe
    detto la stessa cosa, ma sceglie anche un modello sostitutivo e riscrive
    le impostazioni su disco: legare quell'effetto a un timer vorrebbe dire
    che il modello configurato puo' cambiare mentre nessuno guarda.

    Serve perche' la goccia si calcolava **una volta sola**, al caricamento
    della pagina. Chi accende llama-server dopo aver aperto la UI vedeva
    "offline" per sempre, senza nessun modo di farle cambiare idea che non
    fosse un F5.
    """
    backend = STATE.backend()
    online, detail = backend.ping()
    return {
        "online": online,
        "detail": detail,
        "name": backend.name,
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

    # Se il server dichiara la sua finestra (llama-server: -c al lancio), non
    # c'e' niente da consigliare: quello **e'** il contesto. Proporne un altro
    # sarebbe un consiglio che il server ignorera' comunque.
    dal_server = getattr(STATE.backend(), "server_num_ctx", None)
    fissato = dal_server() if callable(dal_server) else None

    if fissato:
        suggested_ctx = int(fissato)
    elif free_mb:
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
        # Non None = il contesto non e' un consiglio ma un dato del server.
        "server_num_ctx": int(fissato) if fissato else None,
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
    if not hasattr(backend, "loaded_models"):
        # llama-server non espone la VRAM: nessuna rotta dice cosa c'e' sulla
        # scheda. Rispondere "occupato 0, quindi libera tutta" sarebbe la
        # bugia peggiore possibile -- il modello e' li' che occupa 18 GB, e
        # il contesto consigliato verrebbe calcolato su una scheda vuota.
        # Meglio dichiarare di non sapere: con llama.cpp il numero che conta
        # e' comunque il num_ctx del server, che si legge da /props.
        return {
            "source": None,
            "remote": True,
            "host": urlparse(normalise_base_url(api_base)).hostname,
            "used": None,
            "total": declared or None,
            "free": None,
        }
    loaded = backend.loaded_models()
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
    """Costruisce l'immagine adesso, che l'automatismo l'abbia gia' fatto o no.

    E' la voce "Crea l'immagine" della tendina della cartella: un gesto
    esplicito, quindi non passa da ``image_needed`` -- chi la chiede a mano la
    vuole rifare, ed e' il rimedio quando il Dockerfile e' cambiato.
    """
    PREP.build_image(
        str(STATE.settings["workspace_dir"]), on_done=seleziona_immagine
    )
    return {"job": PREP.image.snapshot()}


@app.post("/api/prep/container")
def prep_container() -> dict[str, Any]:
    """Crea il container adesso. E' la voce "Crea il container" della tendina."""
    PREP.ensure_container(
        str(STATE.settings["workspace_dir"]),
        image=str(STATE.settings["docker_image"]),
        network=bool(STATE.settings["sandbox_network"]),
        ports=STATE.preview_ports(),
    )
    return {"job": PREP.container.snapshot()}


def maybe_prepare_workspace() -> None:
    """Prepara l'ambiente della cartella appena aperta: immagine, poi container.

    Chiamata quando il workspace **cambia** davvero (``ripristina_workspace``
    esce prima se la cartella e' la stessa), e non ad ogni apertura di chat.

    ## Perche' non fa piu' niente qui dentro

    Prima questa funzione chiamava ``image_needed``, che fa un ``docker
    version`` e un ``docker images``: due sottoprocessi **sul filo della
    richiesta HTTP**, cioe' dentro l'apertura di una conversazione. Su Windows
    sono decimi di secondo buoni ciascuno, e si pagavano ad ogni cambio di
    cartella per scoprire, quasi sempre, che non c'era niente da fare.

    Adesso la decisione la prende il thread di preparazione: qui resta un
    controllo di sole impostazioni, che costa un accesso a un dizionario.
    """
    if str(STATE.settings["sandbox"]) != "docker":
        return
    PREP.prepara(
        str(STATE.settings["workspace_dir"]),
        image=str(STATE.settings["docker_image"]),
        network=bool(STATE.settings["sandbox_network"]),
        ports=STATE.preview_ports(),
        # L'interruttore vale per l'immagine, non per il container: senza
        # container l'agente non ha dove eseguire, e crearlo quando manca non
        # scavalca nessuna scelta dell'utente.
        costruisci_immagine=bool(STATE.settings.get("image_autobuild")),
        on_image=seleziona_immagine,
    )


def seleziona_immagine(tag: str) -> None:
    """L'immagine appena costruita diventa quella in uso.

    Costruirla e non usarla sarebbe il peggio dei due mondi: si paga la build
    e i comandi continuano a girare sull'immagine di serie, senza pytest ne'
    git.
    """
    STATE.settings["docker_image"] = tag
    STATE.persist()


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


class PreviewHostRequest(BaseModel):
    path: str


@app.post("/api/preview/host")
def preview_host(req: PreviewHostRequest) -> dict[str, Any]:
    """Apparecchia la cartella e ritorna l'indirizzo vivo di una pagina.

    Perche' e' una POST e non una GET: ha due effetti collaterali veri --
    sceglie la radice servita e, la prima volta, accende il server delle
    anteprime. Una GET che cambia lo stato del server e' il genere di cosa che
    un prefetch del browser esegue per conto suo.

    La radice la calcola il server e non il client, per la ragione di sempre:
    ``preview_root`` e' gia' scritta una volta in ``core/tools.py`` e la usa
    anche il tool. Due implementazioni della stessa domanda divergono, e la
    seconda e' quella che nessuno ricorda di aggiornare.
    """
    workspace = str(STATE.settings["workspace_dir"])
    try:
        target = resolve_path(workspace, req.path)
    except WorkspaceError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not target.is_file():
        raise HTTPException(404, "File non trovato.")

    radice_rel = preview_root(workspace, req.path)
    radice = resolve_path(workspace, radice_rel or ".")
    # La radice si mette prima di accendere: cosi' non esiste un istante in
    # cui il server e' su e servirebbe ancora la cartella di prima.
    previewhost.set_root(radice)
    origine = previewhost.ensure_running(
        int(STATE.settings.get("preview_host_port") or 0)
    )
    if not origine:
        # Porta spenta dall'utente, o dieci porte occupate di fila. Il client
        # sa ripiegare sull'iframe con origine opaca: si vede la pagina senza
        # storage ne' moduli, che e' meno di prima ma non e' niente.
        return {"origin": None, "url": None, "root": radice_rel}

    rel = target.resolve().relative_to(radice.resolve()).as_posix()
    return {
        "origin": origine,
        "url": f"{origine}/{quote(rel)}",
        "root": radice_rel,
    }


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
                # ...e le porte. Senza, il container rinasceva senza
                # l'intervallo pubblicato: le anteprime delle applicazioni
                # smettevano di funzionare dopo un riavvio della sandbox, e
                # l'unico modo di riaverle era cambiare qualcosa che facesse
                # ricreare il container una seconda volta.
                ports=STATE.preview_ports(),
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
    # Il ciclo qui sopra ha tolto l'allegato da messaggi che possono essere
    # ovunque nella cronologia: la coda va rifatta, non accodata.
    STATE.save(session_id, riscrivi=True)
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


@app.get("/api/events")
def global_events() -> StreamingResponse:
    """Bus globale di processo: notizie larghe per tutte le interfacce.

    Desktop e telefono si collegano qui all'avvio: quando l'altro schermo
    manda un messaggio, fa partire un turno o l'agente fa una domanda,
    l'evento arriva a entrambi e le pagine si ridisegnano da sole, senza
    polling ne' refresh manuale.
    """
    return sse_response(EVENTS.stream())


# ---------------------------------------------------------------------------
# Rotte: impostazioni, memoria, workspace
# ---------------------------------------------------------------------------


@app.post("/api/settings")
def update_settings(request: SettingsRequest) -> dict[str, Any]:
    touched = set()
    rifiutate: list[str] = []
    for key, value in request.values.items():
        if key not in STATE.settings and key != "system_prompt":
            continue
        # Il tipo si controlla **all'ingresso**, non al prossimo avvio.
        #
        # Prima passava qualunque cosa: ``{"num_ctx": "grande"}`` finiva nelle
        # impostazioni in memoria e funzionava fino al riavvio, quando
        # ``load_settings`` scartava il valore per tipo sbagliato e rimetteva
        # il default in silenzio. E' lo stesso modo di guasto di
        # ``deposito_max_mb``, ma per la porta HTTP invece che per un campo
        # della UI -- e qui vale per tutte e cinquanta le chiavi.
        atteso = DEFAULTS.get(key)
        if atteso is not None and not _tipo_compatibile(value, atteso):
            rifiutate.append(
                f"{key}: atteso {type(atteso).__name__}, ricevuto "
                f"{type(value).__name__}"
            )
            continue
        if STATE.settings.get(key) != value:
            touched.add(key)
        STATE.settings[key] = value
    if rifiutate:
        raise HTTPException(400, "Valori non validi -- " + "; ".join(rifiutate))
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
    # Il workspace si puo' cambiare anche da qui, non solo dal selettore: e
    # allora passa dalla stessa porta, senno' questa strada si dimenticherebbe
    # di legare la cartella alla conversazione aperta.
    if "workspace_dir" in touched:
        applica_workspace(Path(str(STATE.settings["workspace_dir"])).expanduser())
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


def applica_workspace(path: Path) -> str:
    """Sposta l'harness su una cartella e prepara quello che va preparato.

    Un solo posto per il cambio: lo usano la scelta a mano, l'apertura di un
    vault e il ripristino della cartella di una conversazione riaperta. Tre
    gesti diversi che devono avere lo stesso effetto, senno' uno dei tre si
    dimentica di ricostruire l'immagine o di aggiornare i recenti.
    """
    STATE.settings["workspace_dir"] = str(path.resolve())
    remember_workspace()
    STATE.persist()
    maybe_prepare_workspace()
    lega_workspace_alla_chat_aperta()
    return STATE.settings["workspace_dir"]


def lega_workspace_alla_chat_aperta() -> None:
    """Segna sulla conversazione aperta la cartella su cui si lavora adesso.

    Il legame nascerebbe comunque al primo salvataggio, cioe' al primo
    messaggio: farlo qui vuol dire che vale anche per una chat aperta e
    lasciata li' -- ed e' proprio quella che, riaperta domani, deve ritrovarsi
    dove l'avevi messa.

    Non puo' far fallire il cambio di cartella: la conversazione corrente
    potrebbe non essere caricabile (appena cancellata dall'altro schermo, file
    sparito). In quel caso il legame salta e si riformera' al primo
    salvataggio.
    """
    try:
        STATE.session(STATE.last_opened)["workspace_dir"] = STATE.settings["workspace_dir"]
    except HTTPException:
        pass


@app.post("/api/workspace")
def set_workspace(request: WorkspaceRequest) -> dict[str, Any]:
    path = Path(request.path).expanduser()
    if not path.is_dir():
        raise HTTPException(400, f"'{request.path}' non e' una cartella esistente.")
    applica_workspace(path)
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
                os.startfile(str(cartella))
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
    # La cartella appena scelta e' anche il momento giusto per costruire
    # l'immagine: l'utente ha appena dichiarato su cosa vuole lavorare, e la
    # build parte mentre scrive il primo messaggio invece che dopo. Lo fa
    # ``applica_workspace``, che e' l'unica strada per cambiare cartella.
    applica_workspace(path)
    return {
        "cancelled": False,
        "workspace_dir": STATE.settings["workspace_dir"],
        "recent_workspaces": STATE.settings["recent_workspaces"],
        "stats": session_stats(STATE.last_opened),
        "jobs": PREP.snapshot(),
    }


# ---------------------------------------------------------------------------
# Rotte: vault LLM Wiki
# ---------------------------------------------------------------------------


class VaultRequest(BaseModel):
    # ``path`` e' la cartella del vault. Registrarla scrive ``.vault.json``;
    # la struttura LLM Wiki si crea **solo** se il vault e' una wiki.
    path: str
    # Nome breve mostrato nella colonna di sinistra. Se il vault ha gia' un
    # ``.vault.json``, quello vince: l'identita' sta nella cartella.
    nome: str = ""


class VaultOpenRequest(BaseModel):
    # Si apre per nome (quello del registro) o, in mancanza, per percorso.
    nome: str = ""
    path: str = ""


class VaultPatchRequest(BaseModel):
    """Modifica dell'identita' di un vault. Solo i campi passati cambiano."""

    path: str
    nome: str | None = None
    descrizione: str | None = None
    # Le sole che arrivano al modello: vedi ``core/vault.py``.
    istruzioni: str | None = None
    wiki: bool | None = None
    # La memoria del vault. Dall'interfaccia si puo' solo **togliere**: la
    # scrive il modello mentre lavora, e un campo libero in cui riscriverla a
    # mano sarebbe un secondo posto da cui puo' divergere.
    note: list[str] | None = None


def _registro_vault() -> list[dict[str, Any]]:
    return [v for v in (STATE.settings.get("vaults") or []) if str(v.get("path", "")).strip()]


def _voce_vault(path: str, nome: str = "") -> dict[str, Any]:
    """Una riga d'elenco completa: identita', conteggi, chat, se e' aperto."""
    corrente = session_mod.chiave_cartella(str(STATE.settings["workspace_dir"]))
    info = vault_mod.info_vault(
        path, nome, chat=len(session_mod.list_sessions(limit=999, cartella=path))
    )
    voce = info.as_dict()
    voce["attivo"] = session_mod.chiave_cartella(path) == corrente
    return voce


@app.get("/api/vaults")
def lista_vault() -> dict[str, Any]:
    """Elenco dei vault registrati, con identita' e conteggi."""
    voci: list[dict[str, Any]] = []
    for v in _registro_vault():
        path = str(v.get("path"))
        if not os.path.isdir(path):
            # Come per i recent_workspaces: un vault cancellato non deve
            # restare nella tendina a suggerire errori.
            continue
        voci.append(_voce_vault(path, str(v.get("nome") or "")))
    return {"vaults": voci}


@app.get("/api/vaults/home")
def home_vault(path: str) -> dict[str, Any]:
    """Tutto quello che serve alla schermata iniziale di un vault.

    Una chiamata sola e non tre: identita', conteggi ed elenco delle chat si
    guardano insieme, e chiederli separatamente vorrebbe dire disegnare la
    schermata a pezzi mentre le risposte arrivano.
    """
    if not os.path.isdir(path):
        raise HTTPException(404, f"'{path}' non e' una cartella esistente.")
    return {
        "vault": _voce_vault(path),
        "sessions": session_list(cartella=path),
    }


@app.patch("/api/vaults")
def modifica_vault(request: VaultPatchRequest) -> dict[str, Any]:
    """Cambia nome, descrizione, istruzioni o modalita' wiki di un vault.

    Scrive in ``.vault.json``, cioe' **dentro la cartella**: il vault resta
    quello che dice di essere anche se lo si sposta o lo si apre da un'altra
    macchina, e il registro nelle preferenze torna a essere solo un elenco di
    percorsi conosciuti.
    """
    base = Path(request.path).expanduser()
    if not base.is_dir():
        raise HTTPException(400, f"'{request.path}' non e' una cartella esistente.")
    if request.wiki:
        # Accendere la wiki senza la sua struttura darebbe un manutentore che
        # fallisce alla prima ingest: le due cose si fanno insieme.
        vault_mod.abilita_wiki(base)
    vault_mod.aggiorna_config(
        base,
        nome=request.nome,
        descrizione=request.descrizione,
        istruzioni=request.istruzioni,
        wiki=request.wiki,
        note=tuple(request.note) if request.note is not None else None,
    )
    # Il nome nel registro e' solo un ripiego per quando il file non c'e':
    # tenerlo allineato evita che l'elenco mostri il vecchio nome finche' la
    # cartella non e' raggiungibile.
    registro = _registro_vault()
    chiave = session_mod.chiave_cartella(str(base))
    for v in registro:
        if session_mod.chiave_cartella(v["path"]) == chiave:
            v["nome"] = vault_mod.leggi_config(base).nome
    STATE.settings["vaults"] = registro
    STATE.persist()
    return {"vault": _voce_vault(str(base))}


@app.post("/api/vaults")
def registra_vault(request: VaultRequest) -> dict[str, Any]:
    """Registra un vault: scrive la sua identita' e lo mette nel registro."""
    path = Path(request.path).expanduser()
    if not path.is_dir():
        raise HTTPException(400, f"'{request.path}' non e' una cartella esistente.")
    path = path.resolve()
    # Qui la cartella diventa un vault: ``.vault.json`` con il nome, e la
    # struttura wiki solo se e' una wiki (una cartella qualsiasi non deve
    # ritrovarsi raw/ e wiki/ che non ha chiesto).
    vault_mod.ensure_vault(path, nome=request.nome.strip())
    nome = vault_mod.leggi_config(path).nome
    registro = _registro_vault()
    chiave = session_mod.chiave_cartella(str(path))
    if not any(session_mod.chiave_cartella(v["path"]) == chiave for v in registro):
        registro.append({"path": str(path), "nome": nome})
        STATE.settings["vaults"] = registro
        STATE.persist()
    return {"vault": _voce_vault(str(path), nome)}


@app.post("/api/vaults/pick")
def scegli_vault_con_dialogo() -> dict[str, Any]:
    """Apre il selettore nativo di cartelle e registra il vault scelto.

    Stampa di ``/api/workspace/pick``: su Windows e' Esplora risorse, la
    chiamata resta appesa finche' l'utente non sceglie o annulla (richiesta
    sincrona servita dal threadpool, quindi senza bloccare il resto).
    La registrazione riusa ``registra_vault``, cosi' validazione, dedupe e
    creazione della struttura LLM Wiki hanno una sola implementazione.
    """
    try:
        scelto = pick_folder(STATE.settings["workspace_dir"])
    except DialogUnavailable as exc:
        raise HTTPException(
            501,
            f"Selettore di sistema non disponibile: {exc}. "
            "Registra il percorso da Impostazioni.",
        ) from exc

    if not scelto:
        return {"cancelled": True}

    voce = registra_vault(VaultRequest(path=scelto))
    return {"cancelled": False, "vault": voce["vault"]}


@app.post("/api/vaults/remove")
def rimuovi_vault(request: VaultOpenRequest) -> dict[str, Any]:
    """Toglie un vault dal registro: la cartella su disco non si tocca."""
    registro = _registro_vault()
    chiave = ""
    if request.path:
        chiave = os.path.normcase(os.path.normpath(request.path))
    elif request.nome:
        match = [v for v in registro if v.get("nome") == request.nome]
        if match:
            chiave = os.path.normcase(os.path.normpath(match[0]["path"]))
    tenuti = [
        v for v in registro
        if os.path.normcase(os.path.normpath(v["path"])) != chiave
    ] if chiave else registro
    STATE.settings["vaults"] = tenuti
    STATE.persist()
    return {"rimossi": len(registro) - len(tenuti), "vaults": tenuti}


@app.post("/api/vaults/open")
def apri_vault(request: VaultOpenRequest) -> dict[str, Any]:
    """Apre un vault come workspace corrente, creandolo se non esiste ancora."""
    bersaglio = ""
    if request.nome:
        match = [
            v for v in _registro_vault()
            if v.get("nome") == request.nome and os.path.isdir(v["path"])
        ]
        if match:
            bersaglio = match[0]["path"]
    if not bersaglio and request.path:
        bersaglio = request.path
    if not bersaglio or not Path(bersaglio).is_dir():
        raise HTTPException(400, f"Vault '{request.nome or request.path}' non trovato.")

    radice = vault_mod.ensure_vault(Path(bersaglio).expanduser())
    applica_workspace(radice)
    return {
        "workspace_dir": STATE.settings["workspace_dir"],
        "recent_workspaces": STATE.settings["recent_workspaces"],
        "stats": session_stats(STATE.last_opened),
        "jobs": PREP.snapshot(),
        # La schermata iniziale del vault si disegna con questi due, che
        # arrivano nella stessa risposta dell'apertura: aprire un vault e'
        # un gesto solo, e non deve costare tre richieste in fila.
        "vault": _voce_vault(str(radice)),
        "sessions": session_list(cartella=str(radice)),
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


__all__ = ["RUNNERS", "STATE", "app"]
