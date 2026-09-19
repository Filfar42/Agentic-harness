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

from core.backend import _LIVELLI_APPRESI, LlamaCppBackend, OpenAICompatBackend, build_backend
from core.config import GenParams

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
    {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
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
    # Il chat template che /props dichiara (None = campo assente) e se il
    # server lo applica con la severita' del template ufficiale di Qwen3.8:
    # 500 su un livello non ammesso e su un system che non sia il primo.
    template: str | None = None
    severo = False
    # Un 500 del template forzato su ogni richiesta, per l'errore in chat.
    rompi = ""
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
                    **({"chat_template": type(self).template} if type(self).template else {}),
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
        motivo = type(self).rompi or (type(self).severo and _eccezione_jinja(type(self).corpi[-1]))
        if motivo:
            corpo = json.dumps(
                {"error": {"code": 500, "message": motivo, "type": "server_error"}}
            ).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)
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


ERRORE_LIVELLO = (
    "Jinja Exception: Unexpected reasoning effort {}. "
    "Supported types are xhigh (default), medium, and low."
)
ERRORE_SYSTEM = "Jinja Exception: System message must be at the beginning."

# Il pezzo del template ufficiale di Qwen3.8 che conta qui (QwenLM/Qwen3.8#217).
TEMPLATE_UFFICIALE = (
    "{%- if enable_thinking is defined and enable_thinking is false %}{%- else %}"
    "{%- set resolved_reasoning_effort = reasoning_effort|default('xhigh') %}"
    "{%- if resolved_reasoning_effort not in ('xhigh', 'medium', 'low') %}"
    "{{- raise_exception('Unexpected reasoning effort ' ~ reasoning_effort ~ "
    "'. Supported types are xhigh (default), medium, and low.') }}"
    "{%- endif %}{%- endif %}"
    "{%- for message in messages %}{%- if message.role == 'system' and not loop.first %}"
    "{{- raise_exception('System message must be at the beginning.') }}"
    "{%- endif %}{%- endfor %}{%- if tools %}<tools>{%- endif %}"
)
# Lo stesso controllo scritto in modo che l'elenco non si possa leggere: il
# caso in cui resta solo il ripiego sul messaggio del 500.
TEMPLATE_OPACO = (
    "{%- set ammessi = livelli_del_modello %}"
    "{%- if enable_thinking and reasoning_effort is defined "
    "and reasoning_effort not in ammessi %}{{- raise_exception(errore) }}{%- endif %}"
    "{%- if tools %}<tools>{%- endif %}"
)


def _eccezione_jinja(corpo: dict) -> str:
    """Quello che il template ufficiale solleverebbe su questa richiesta."""
    kwargs = corpo.get("chat_template_kwargs") or {}
    if kwargs.get("enable_thinking") is not False:
        livello = kwargs.get("reasoning_effort", corpo.get("reasoning_effort"))
        if livello is not None and livello not in ("xhigh", "medium", "low"):
            return ERRORE_LIVELLO.format(livello)
    if any(m.get("role") == "system" for m in corpo.get("messages", [])[1:]):
        return ERRORE_SYSTEM
    return ""


@pytest.fixture()
def finto_llama():
    _Handler.corpi = []
    _Handler.template = None
    _Handler.severo = False
    _Handler.rompi = ""
    # I livelli imparati da un 500 sono di modulo: un test non li eredita.
    _LIVELLI_APPRESI.clear()
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


def test_il_neutro_sovrascrive_il_default_del_server(finto_llama):
    url, handler = finto_llama
    backend = LlamaCppBackend(url)
    params = GenParams(model="qwen", repetition_penalty=1.0)
    list(backend.stream([{"role": "user", "content": "ciao"}], None, params))
    assert handler.corpi[-1]["repeat_penalty"] == 1.0


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
    url, _handler = finto_llama

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
    assert usage[-1].usage["done_reason"] == "tool_calls"


def test_lo_stop_per_length_viaggia_nell_usage(finto_llama, monkeypatch):
    """Il caso che conta: il server dice 'length' e il numero arriva a chi
    deve decidere se la tool call e' monca o scritta male."""
    monkeypatch.setattr(sys.modules[__name__], "CHUNKS", [
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]},
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
    ])

    url, _ = finto_llama
    eventi = list(
        LlamaCppBackend(url).stream(
            [{"role": "user", "content": "ciao"}], None, GenParams(model="q")
        )
    )
    usage = [e for e in eventi if e.kind == "usage"]
    assert usage and usage[-1].usage["done_reason"] == "length"


# ---------------------------------------------------------------------------
# Il template severo (Qwen3.8 ufficiale): livelli e system
# ---------------------------------------------------------------------------
#
# Il finto server risponde 500 con i due corpi d'errore di llama-server quando
# riceve un livello che il template non ammette o un system non in testa. Prima
# della correzione ogni messaggio moriva cosi'; qui si prova che le richieste
# passano, e passano al primo colpo quando il template si puo' leggere.


def _conversazione():
    return [
        {"role": "system", "content": "SYS"},
        {"role": "system", "content": "ENV"},
        {"role": "user", "content": "ciao"},
    ]


def _eventi(backend, params, messaggi=None):
    return list(backend.stream(messaggi or _conversazione(), None, params))


def test_il_finto_server_e_severo_come_il_template(finto_llama):
    """Il banco di prova: senza correzione, i due 500 della console."""
    import httpx

    url, handler = finto_llama
    handler.severo = True
    r = httpx.post(f"{url}/v1/chat/completions", json={
        "messages": _conversazione(), "chat_template_kwargs": {"reasoning_effort": "high"},
    })
    assert r.status_code == 500
    assert "Unexpected reasoning effort high" in r.json()["error"]["message"]
    r = httpx.post(f"{url}/v1/chat/completions", json={"messages": _conversazione()})
    assert r.status_code == 500
    assert r.json()["error"]["message"] == ERRORE_SYSTEM


def test_il_livello_si_legge_dal_template_e_passa_al_primo_colpo(finto_llama):
    url, handler = finto_llama
    handler.severo = True
    handler.template = TEMPLATE_UFFICIALE
    backend = LlamaCppBackend(url)
    backend.props()                     # come fa gen_params() a ogni turno
    params = GenParams(model="qwen", think="high")

    eventi = _eventi(backend, params)

    assert not [e for e in eventi if e.kind == "error"]
    assert [e.text for e in eventi if e.kind == "content"] == ["ecco"]
    assert len(handler.corpi) == 1                       # nessun 500 da pagare
    corpo = handler.corpi[0]
    assert corpo["chat_template_kwargs"]["reasoning_effort"] == "xhigh"
    assert [m["role"] for m in corpo["messages"]] == ["system", "user"]
    assert corpo["messages"][0]["content"] == "SYS\n\nENV"
    assert backend.livello_inviato(params) == "xhigh"
    report = backend.reasoning_control(params)
    assert report["translated"] == {"from": "high", "to": "xhigh"}


def test_un_livello_gia_ammesso_non_si_tocca(finto_llama):
    url, handler = finto_llama
    handler.severo = True
    handler.template = TEMPLATE_UFFICIALE
    backend = LlamaCppBackend(url)
    backend.props()
    params = GenParams(model="qwen", think="medium")

    assert not [e for e in _eventi(backend, params) if e.kind == "error"]
    assert handler.corpi[0]["chat_template_kwargs"]["reasoning_effort"] == "medium"
    assert "translated" not in backend.reasoning_control(params)


def test_senza_elenco_nel_template_ripiega_sul_messaggio_d_errore(finto_llama):
    url, handler = finto_llama
    handler.severo = True
    handler.template = TEMPLATE_OPACO
    backend = LlamaCppBackend(url)
    backend.props()
    params = GenParams(model="qwen", think="high")

    eventi = _eventi(backend, params)

    assert not [e for e in eventi if e.kind == "error"]
    livelli = [c["chat_template_kwargs"]["reasoning_effort"] for c in handler.corpi]
    assert livelli == ["high", "xhigh"]          # un 500, poi un solo nuovo giro
    # L'elenco resta in cache: il turno dopo non ripaga il 500, anche con
    # un'istanza nuova (il server ricostruisce il backend).
    handler.corpi.clear()
    altro = LlamaCppBackend(url)
    altro.props()
    assert not [e for e in _eventi(altro, params) if e.kind == "error"]
    assert len(handler.corpi) == 1
    assert handler.corpi[0]["chat_template_kwargs"]["reasoning_effort"] == "xhigh"


def test_il_generico_impara_dal_500_e_riprova_una_volta(finto_llama):
    """vLLM/OpenRouter col template ufficiale: stesso 500, stesso ripiego."""
    url, handler = finto_llama
    handler.severo = True
    backend = OpenAICompatBackend(url, "")
    params = GenParams(model="qwen3.8-27b", think="high")

    assert not [e for e in _eventi(backend, params) if e.kind == "error"]
    assert [c["reasoning_effort"] for c in handler.corpi] == ["high", "xhigh"]


def test_il_generico_senza_errori_resta_com_era(finto_llama):
    """OpenRouter & co.: un livello accettato viaggia identico, una richiesta."""
    url, handler = finto_llama
    backend = OpenAICompatBackend(url, "")
    params = GenParams(model="qwen3.8-27b", think="high")

    assert not [e for e in _eventi(backend, params) if e.kind == "error"]
    assert len(handler.corpi) == 1
    assert handler.corpi[0]["reasoning_effort"] == "high"


def test_un_500_del_template_arriva_leggibile_e_senza_tre_tentativi(finto_llama):
    url, handler = finto_llama
    handler.rompi = ERRORE_SYSTEM
    backend = LlamaCppBackend(url)

    eventi = _eventi(backend, GenParams(model="qwen"))

    errori = [e.text for e in eventi if e.kind == "error"]
    assert len(errori) == 1
    assert "System message must be at the beginning." in errori[0]
    assert '{"error"' not in errori[0]           # il motivo, non l'involucro
    assert len(handler.corpi) == 1               # deterministico: niente backoff


def test_il_turno_passa_e_la_traccia_dice_richiesto_e_inviato(finto_llama, tmp_path):
    """End-to-end sul ciclo: due system in testa, livello high, template severo.

    E la cronologia salvata non si tocca: niente conversioni scritte su disco.
    """
    import copy

    from core import agent as agent_mod
    from core.tools import ToolContext

    url, handler = finto_llama
    handler.severo = True
    handler.template = TEMPLATE_UFFICIALE
    backend = LlamaCppBackend(url)
    backend.props()
    storia = [
        {"role": "user", "content": "prima domanda"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "type": "function",
             "function": {"name": "list_files", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "name": "list_files", "content": "a.py"},
        {"role": "assistant", "content": "fatto"},
        {"role": "user", "content": "e adesso?"},
    ]
    prima = copy.deepcopy(storia)
    eventi = list(agent_mod.run_turn(
        backend=backend,
        params=GenParams(model="qwen", num_ctx=8192, max_tokens=2048, think="high"),
        tools_schema=[{"type": "function", "function": {"name": "list_files", "parameters": {}}}],
        tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
        ui_messages=storia,
        system_prompt="SYS",
        env_header="ENV",
        max_steps=1,
        require_plan=False,
        enable_nudge=False,
    ))

    assert not [e for e in eventi if type(e).__name__ == "AgentError"]
    assert storia[: len(prima)] == prima
    assert not any(m.get("role") == "system" for m in storia)
    assert not any("[Nota dell'harness]" in str(m.get("content")) for m in storia)
    corpo = handler.corpi[0]
    assert sum(m["role"] == "system" for m in corpo["messages"]) == 1
    traccia = next(m["think"] for m in storia if m.get("think"))
    assert traccia["usato"] == "high"
    assert traccia["inviato"] == "xhigh"
    assert traccia["traduzione_livello"] == "high -> xhigh"


def test_l_errore_che_chiude_il_turno_resta_nella_cronologia(finto_llama, tmp_path):
    from core import agent as agent_mod
    from core.tools import ToolContext

    url, handler = finto_llama
    handler.rompi = ERRORE_LIVELLO.format("high")
    msgs = [{"role": "user", "content": "ciao"}]
    eventi = list(agent_mod.run_turn(
        backend=LlamaCppBackend(url),
        params=GenParams(model="qwen", num_ctx=8192, max_tokens=2048),
        tools_schema=[],
        tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
        ui_messages=msgs,
        system_prompt="SYS",
        env_header="ENV",
        max_steps=2,
        require_plan=False,
        enable_nudge=False,
    ))

    errori = [e for e in eventi if type(e).__name__ == "AgentError"]
    assert errori and "Unexpected reasoning effort high" in errori[-1].message
    assert msgs[-1]["role"] == "error"
    assert "Supported types are xhigh" in msgs[-1]["content"]
    # Il record non raggiunge mai il modello.
    api = agent_mod.build_api_messages(msgs, system_prompt="S", env_header=None)
    assert all(m["role"] != "error" for m in api)
