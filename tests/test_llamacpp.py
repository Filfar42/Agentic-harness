"""Transport ``llamacpp``: quello che llama-server dice, e quello che non dice.

Il finto server qui dentro e' il gemello del finto Ollama di
``test_agent_loop``: stessa forma (un ``HTTPServer`` vero su una porta a caso),
altre rotte. Serve perche' le tre differenze fra llama-server e un endpoint
OpenAI qualunque sono tutte **silenziose**, e una regressione su una di esse
non darebbe nessun errore:

  * il contesto lo comanda ``-c`` al lancio, non la richiesta;
  * i sampler hanno altri nomi (``top_k``, ``repeat_penalty``);
  * i tempi e i contatori del draft model arrivano in ``timings``, che nello
    standard OpenAI non esiste.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.backend import LlamaCppBackend, OpenAICompatBackend, build_backend  # noqa: E402
from core.config import GenParams  # noqa: E402

CHUNKS = [
    {"choices": [{"index": 0, "delta": {"reasoning_content": "ci penso"}}]},
    {"choices": [{"index": 0, "delta": {"content": "ecco"}}]},
    {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "list_files", "arguments": '{"path":"."}'},
                        }
                    ]
                },
            }
        ]
    },
    {
        "choices": [],
        "usage": {"prompt_tokens": 120, "completion_tokens": 45},
        "timings": {
            "prompt_ms": 300.0,
            "predicted_ms": 1500.0,
            "predicted_per_second": 30.0,
            "draft_n": 40,
            "draft_n_accepted": 33,
        },
    },
]


class _Handler(BaseHTTPRequestHandler):
    corpi: list[dict] = []
    hits: list[str] = []
    n_ctx = 65536
    caps: dict = {"supports_tools": True, "supports_reasoning": True}
    props_ok = True
    # La chiave che il server pretende. Vuota = server aperto, come quello
    # di casa; valorizzata = llama-server lanciato con --api-key.
    chiave = ""

    def log_message(self, *args):
        pass

    def _json(self, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        type(self).hits.append(self.path)
        if type(self).chiave and self.headers.get("Authorization") != f"Bearer {type(self).chiave}":
            # llama-server lanciato con --api-key: 401 su tutto, /props
            # compreso. Senza header la UI lo leggeva come "server spento".
            self.send_error(401)
            return
        if self.path == "/props":
            if not type(self).props_ok:
                self.send_error(404)
                return
            self._json(
                {
                    "default_generation_settings": {"n_ctx": type(self).n_ctx},
                    "chat_template_caps": type(self).caps,
                    "model_path": "/m/Qwen3.8-27B-UD-Q4_K_XL.gguf",
                }
            )
        elif self.path == "/slots":
            self._json([{"id": 0, "n_ctx": 4096}])
        elif self.path == "/v1/models":
            self._json({"data": [{"id": "qwen"}]})
        else:
            self.send_error(404)

    def do_POST(self):
        type(self).hits.append(self.path)
        length = int(self.headers.get("Content-Length", 0))
        type(self).corpi.append(json.loads(self.rfile.read(length) or b"{}"))
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in CHUNKS:
            intero = {
                "id": "c1",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "qwen",
                **chunk,
            }
            self.wfile.write(b"data: " + json.dumps(intero).encode() + b"\n\n")
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


@pytest.fixture()
def finto_llama():
    _Handler.corpi = []
    _Handler.hits = []
    _Handler.n_ctx = 65536
    _Handler.caps = {"supports_tools": True, "supports_reasoning": True}
    _Handler.props_ok = True
    _Handler.chiave = ""
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", _Handler
    server.shutdown()
    server.server_close()


# ---------------------------------------------------------------------------
# Il contesto e' del server, non dell'impostazione
# ---------------------------------------------------------------------------


def test_il_contesto_si_legge_dal_server_e_non_si_supera(finto_llama):
    url, _ = finto_llama
    backend = LlamaCppBackend(url)

    assert backend.server_num_ctx() == 65536
    # chiedere di piu' non allarga niente: si scende al vero
    assert backend.clamp_num_ctx(131072) == 65536
    # chiedere di meno e' legittimo: si resta dove si e'
    assert backend.clamp_num_ctx(32768) == 32768


def test_senza_n_ctx_nei_props_ripiega_sugli_slot(finto_llama):
    url, handler = finto_llama
    handler.n_ctx = 0                     # il server non lo dichiara in /props
    backend = LlamaCppBackend(url)
    assert backend.server_num_ctx() == 4096


def test_le_impostazioni_non_vincono_sul_server(finto_llama, tmp_path):
    """La regressione da temere: 131k dichiarati, 64k reali, nessun errore."""
    url, _ = finto_llama
    from server import main as server_main

    server_main.STATE = server_main.AppState()
    server_main.STATE.settings.update(
        {
            "api_base": url,
            "transport": "llamacpp",
            "model_name": "qwen",
            "num_ctx": 131072,
            "native_think": "no",
            "workspace_dir": str(tmp_path),
        }
    )
    params = server_main.STATE.gen_params()
    assert params.num_ctx == 65536


# ---------------------------------------------------------------------------
# Il dialetto
# ---------------------------------------------------------------------------


def test_i_sampler_viaggiano_coi_nomi_di_llama_cpp(finto_llama):
    """``top_k`` e ``repeat_penalty``: sbagliarli non darebbe errore."""
    url, handler = finto_llama
    backend = LlamaCppBackend(url)
    params = GenParams(model="qwen", top_k=20, repetition_penalty=1.15, num_ctx=131072)

    list(backend.stream([{"role": "user", "content": "ciao"}], None, params))

    corpo = handler.corpi[-1]
    assert corpo["top_k"] == 20
    assert corpo["repeat_penalty"] == 1.15
    assert corpo["timings_per_token"] is True
    # e soprattutto: NON si manda un contesto che il server ignorerebbe
    assert "max_model_len" not in corpo


def test_il_neutro_non_si_manda(finto_llama):
    url, handler = finto_llama
    backend = LlamaCppBackend(url)
    params = GenParams(model="qwen", repetition_penalty=1.0)
    list(backend.stream([{"role": "user", "content": "ciao"}], None, params))
    assert "repeat_penalty" not in handler.corpi[-1]


def test_lo_stream_porta_pensiero_testo_e_tool_call(finto_llama):
    url, _ = finto_llama
    backend = LlamaCppBackend(url)
    eventi = list(
        backend.stream([{"role": "user", "content": "ciao"}], None, GenParams(model="qwen"))
    )
    generi = [e.kind for e in eventi]
    assert "reasoning" in generi and "content" in generi and "tool_call" in generi
    chiamata = next(e.tool_call for e in eventi if e.kind == "tool_call")
    assert chiamata["name"] == "list_files"


def test_i_contatori_del_draft_arrivano_nel_consumo(finto_llama):
    """Sono l'unica misura di quanto rende lo speculative decoding."""
    url, _ = finto_llama
    backend = LlamaCppBackend(url)
    eventi = list(
        backend.stream([{"role": "user", "content": "ciao"}], None, GenParams(model="qwen"))
    )
    usage = next(e.usage for e in eventi if e.kind == "usage")

    assert usage["draft_n"] == 40
    assert usage["draft_accepted"] == 33
    # e i tempi, che sul transport openai generico non arrivavano mai: senza
    # eval_ms la riga "Velocita'" mostrava 0,0 tok/s su qualunque turno
    assert usage["eval_ms"] == 1500
    assert usage["prompt_eval_ms"] == 300
    assert usage["total_ms"] == 1800
    # un evento usage solo: sommarli due volte falserebbe il conto dei token
    assert sum(1 for e in eventi if e.kind == "usage") == 1


def test_senza_timings_il_consumo_resta_quello_standard(finto_llama):
    """Un server senza draft non deve far comparire una riga vuota."""
    url, _ = finto_llama
    backend = OpenAICompatBackend(url, "")
    eventi = list(
        backend.stream([{"role": "user", "content": "ciao"}], None, GenParams(model="qwen"))
    )
    usage = next(e.usage for e in eventi if e.kind == "usage")
    assert "draft_n" not in usage
    assert usage["prompt_tokens"] == 120


# ---------------------------------------------------------------------------
# Capability
# ---------------------------------------------------------------------------


def test_i_tool_spenti_sono_il_sintomo_di_jinja_mancante(finto_llama):
    url, handler = finto_llama
    backend = LlamaCppBackend(url)
    assert backend.supports_tools("qwen") is True
    assert backend.supports_thinking("qwen") is True

    # llama-server avviato senza --jinja: nessuna capability sui tool
    handler.caps = {"supports_tools": False}
    backend_2 = LlamaCppBackend(url)
    assert backend_2.supports_tools("qwen") is False


def test_il_modello_si_ricava_dai_props_se_l_elenco_e_vuoto(finto_llama):
    """Un elenco vuoto lascia l'utente a indovinare cosa scrivere."""
    url, handler = finto_llama

    class SenzaModelli(_Handler):
        pass

    backend = LlamaCppBackend(url)
    assert backend.list_models() == ["qwen"]        # /v1/models risponde

    # server che non espone /v1/models: si ripiega sul percorso del file
    import core.backend as backend_mod

    monkey = backend_mod.OpenAICompatBackend.list_models
    try:
        backend_mod.OpenAICompatBackend.list_models = lambda self: []
        backend_2 = LlamaCppBackend(url)
        assert backend_2.list_models() == ["Qwen3.8-27B-UD-Q4_K_XL"]
    finally:
        backend_mod.OpenAICompatBackend.list_models = monkey


def test_auto_riconosce_llama_cpp_da_props(finto_llama):
    """``/v1/models`` risponde a tutti; ``/props`` solo a llama-server."""
    url, _ = finto_llama
    backend = build_backend(
        transport="auto", base_url=url, api_key="", timeout_s=5.0
    )
    assert isinstance(backend, LlamaCppBackend)


def test_auto_senza_props_resta_sul_generico(finto_llama):
    url, handler = finto_llama
    handler.props_ok = False
    backend = build_backend(
        transport="auto", base_url=url, api_key="", timeout_s=5.0
    )
    assert isinstance(backend, OpenAICompatBackend)
    assert not isinstance(backend, LlamaCppBackend)


# ---------------------------------------------------------------------------
# VRAM
# ---------------------------------------------------------------------------


def test_la_vram_non_si_inventa(finto_llama, monkeypatch, tmp_path):
    """llama-server non dice cosa c'e' sulla scheda: e non lo si deduce.

    Il rischio concreto: nessun modello "caricato" visibile -> libera tutta la
    VRAM dichiarata -> contesto consigliato calcolato su una scheda vuota,
    mentre sopra ci sono 18 GB di pesi.
    """
    url, _ = finto_llama
    from server import main as server_main

    server_main.STATE = server_main.AppState()
    server_main.STATE.settings.update(
        {
            "api_base": url,
            "transport": "llamacpp",
            "model_name": "qwen",
            "gpu_total_vram_mb": 32768,
            "workspace_dir": str(tmp_path),
        }
    )
    monkeypatch.setattr(server_main, "endpoint_is_local", lambda _u: False)

    snapshot = server_main._vram_snapshot()
    assert snapshot["free"] is None
    assert snapshot["used"] is None
    assert snapshot["total"] == 32768


# ---------------------------------------------------------------------------
# La goccia di stato: "e' acceso?" e "chi sei?" sono due domande diverse
# ---------------------------------------------------------------------------


def test_la_chiave_viaggia_anche_sulle_rotte_native(finto_llama):
    """``--api-key`` protegge tutto, ``/props`` compreso.

    Senza l'header quel 401 arrivava alla UI come "endpoint non
    raggiungibile": goccia rossa su un server che stava benissimo, cioe' il
    caso peggiore, perche' non sembra un errore.
    """
    url, handler = finto_llama
    handler.chiave = "segreto"

    muto = LlamaCppBackend(url, api_key="")
    assert muto.ping()[0] is False

    backend = LlamaCppBackend(url, api_key="segreto")
    online, _ = backend.ping()
    assert online is True
    assert backend.props().get("model_path")


def test_senza_props_la_goccia_resta_accesa(finto_llama):
    """Un reverse proxy che non inoltra ``/props`` non spegne il server.

    ``/health`` e ``/v1/models`` sono le altre due risposte alla domanda
    "sei acceso?", e la prima che risponde basta.
    """
    url, handler = finto_llama
    handler.props_ok = False
    backend = LlamaCppBackend(url)
    online, detail = backend.ping()
    assert online is True
    assert "/v1/models" in detail          # ha dovuto scendere fino all'ultima


def test_un_server_spento_dice_quali_rotte_ha_provato():
    """Il dettaglio e' l'unica riga che l'utente ha per capire *cosa* manca."""
    backend = LlamaCppBackend("http://127.0.0.1:1")   # porta chiusa
    online, detail = backend.ping()
    assert online is False
    assert "/props" in detail and "/health" in detail and "/v1/models" in detail


def test_il_riconoscimento_del_dialetto_guarda_solo_props(finto_llama):
    """``ping`` puo' essere generoso: accende una goccia. Questa no: sceglie
    come si formano le richieste per tutta la sessione."""
    url, handler = finto_llama
    assert LlamaCppBackend(url).parla_llamacpp() is True
    handler.props_ok = False
    sonda = LlamaCppBackend(url)
    assert sonda.ping()[0] is True          # acceso...
    assert sonda.parla_llamacpp() is False  # ...ma non e' llama-server


def test_status_del_generico_e_una_richiesta_sola(finto_llama):
    """``ping`` e ``list_models`` chiedevano lo stesso ``/v1/models``."""
    url, handler = finto_llama
    handler.hits = []
    online, _, modelli = OpenAICompatBackend(url, "").status()
    assert online is True and modelli == ["qwen"]
    assert handler.hits.count("/v1/models") == 1


def test_il_motivo_dello_stop_arriva_fino_al_ciclo_agentico(finto_llama):
    """``finish_reason`` non lo leggeva nessuno.

    Su Ollama il ``done_reason`` arrivava dal primo giorno, e il ciclo
    agentico sa gia' cosa farsene. Sul transport OpenAI-compatibile -- cioe'
    su llama.cpp, vLLM, LM Studio -- il campo equivalente veniva scartato: una
    generazione tagliata dal tetto o dalla finestra era indistinguibile da una
    finita bene, e se il taglio cadeva dentro una tool call il modello si
    prendeva la colpa di un JSON che non aveva scritto lui.
    """
    url, _ = finto_llama
    backend = LlamaCppBackend(url)
    eventi = list(
        backend.stream([{"role": "user", "content": "ciao"}], None, GenParams(model="q"))
    )
    usage = [e for e in eventi if e.kind == "usage"]
    assert usage, "nessun evento usage"
    # Il finto server chiude senza finish_reason sui chunk: il campo c'e'
    # comunque, vuoto, perche' il ciclo agentico lo legge sempre.
    assert "done_reason" in usage[-1].usage


def test_lo_stop_per_length_viaggia_nell_usage(finto_llama, monkeypatch):
    """Il caso che conta: il server dice 'length' e il numero arriva a chi
    deve decidere se la tool call e' monca o scritta male."""
    import core.backend as backend_mod

    class FintoChunk:
        def __init__(self, finish):
            self.choices = [type("C", (), {"finish_reason": finish, "delta": type(
                "D", (), {"content": None, "tool_calls": None})()})()]
            self.usage = None

    class FintoUsage:
        prompt_tokens = 10
        completion_tokens = 5

    class FintoFinale:
        choices = []
        usage = FintoUsage()

    class FintoClient:
        def __init__(self, **_):
            self.chat = type("Chat", (), {"completions": self})()

        def create(self, **_):
            return iter([FintoChunk(None), FintoChunk("length"), FintoFinale()])

    monkeypatch.setattr(backend_mod, "OpenAI", FintoClient, raising=False)
    import sys
    import types
    finto_modulo = types.ModuleType("openai")
    finto_modulo.OpenAI = FintoClient
    monkeypatch.setitem(sys.modules, "openai", finto_modulo)

    url, _ = finto_llama
    eventi = list(
        LlamaCppBackend(url).stream(
            [{"role": "user", "content": "ciao"}], None, GenParams(model="q")
        )
    )
    usage = [e for e in eventi if e.kind == "usage"]
    assert usage and usage[-1].usage["done_reason"] == "length"
