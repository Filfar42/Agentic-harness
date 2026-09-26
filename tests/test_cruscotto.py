"""Il cruscotto della colonna di destra: misure dal vivo, righe, storico.

Tre livelli, dal basso: il ``Tachimetro`` (funzione pura dei pezzi dello
stream, con un orologio finto), il ``Registro`` e ``storico`` (righe della
timeline e cio' che si ricava dalla telemetria salvata), e le cuciture --
il transport che emette i battiti, il ciclo che emette ``Metriche``, il
runner che tiene le metriche dal vivo come frame volatili.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import agent
from core import backend as transport
from core import cruscotto
from core import session as session_mod
from core.backend import LlamaCppBackend, OpenAICompatBackend, StreamEvent
from core.config import GenParams
from core.cruscotto import Metriche, Registro, Tachimetro, storico
from core.tools import TOOLS_SCHEMA, ToolContext
from server.runner import TurnRunner


class Orologio:
    """Un orologio che va avanti solo quando lo dice il test."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def avanti(self, secondi: float) -> None:
        self.t += secondi


def pezzo(kind: str, testo: str = "x") -> StreamEvent:
    return StreamEvent(kind, text=testo)


def battito(**dati: Any) -> StreamEvent:
    return StreamEvent("battito", usage=dati)


# ---------------------------------------------------------------------------
# Tachimetro
# ---------------------------------------------------------------------------


def test_prima_del_primo_token_e_attesa_e_non_c_e_velocita():
    orologio = Orologio()
    t = Tachimetro(3, prompt_stimato=12_000, finestra=65_536, orologio=orologio)
    m = t.aggiorna(forza=True)
    assert m.passo == 3 and m.fase == "attesa"
    assert m.tok_s is None and m.attesa_ms is None and m.generati == 0
    assert m.prompt_stimato == 12_000 and m.finestra == 65_536
    json.dumps(cruscotto.riga_da_metriche(m))  # serializzabile


def test_un_token_per_pezzo_sull_orologio_dell_harness():
    """Ollama: un pezzo e' un token, e la velocita' e' pezzi su secondi."""
    orologio = Orologio()
    t = Tachimetro(1, un_token_per_chunk=True, orologio=orologio)
    orologio.avanti(2.0)                     # due secondi di prefill
    for _ in range(40):                      # 40 token in 2 s -> 20 tok/s
        t.evento(pezzo("reasoning"))
        orologio.avanti(0.05)
    m = t.aggiorna(forza=True)
    assert m.fase == "pensiero" and m.fonte == "stima"
    assert m.attesa_ms == 2000
    assert m.generati == 40 and m.pensiero == 40
    assert m.tok_s == pytest.approx(20.0, rel=0.08)
    assert m.tok_s_passo == pytest.approx(20.0, rel=0.08)


def test_senza_un_token_per_pezzo_si_stima_dai_caratteri():
    orologio = Orologio()
    t = Tachimetro(1, un_token_per_chunk=False, orologio=orologio)
    t.evento(pezzo("content", "a" * 36))     # ~10 token (3,6 caratteri l'uno)
    m = t.aggiorna(forza=True)
    assert m.fase == "risposta"
    assert m.risposta == 10 and m.generati == 10


def test_un_modello_fermo_scende_a_zero_sull_orologio_dell_harness():
    """Senza token nell'ultima finestra la velocita' e' zero, non l'ultima vista."""
    orologio = Orologio()
    t = Tachimetro(1, un_token_per_chunk=True, orologio=orologio)
    for _ in range(20):
        t.evento(pezzo("content"))
        orologio.avanti(0.05)
    assert t.aggiorna(forza=True).tok_s > 0
    orologio.avanti(cruscotto.FINESTRA_MS / 1000 + 0.5)
    assert t.aggiorna(forza=True).tok_s == 0.0


def test_con_i_timings_del_server_contano_i_suoi_numeri():
    """llama.cpp: token e millisecondi del server, non i pezzi ne' la rete."""
    orologio = Orologio()
    t = Tachimetro(2, un_token_per_chunk=True, orologio=orologio)
    for i in range(1, 61):
        t.evento(pezzo("reasoning"))
        # 25 tok/s sul server, qualunque cosa faccia la rete
        t.evento(battito(generati=i, generazione_ms=i * 40.0, prompt_calcolati=800,
                         prefill_ms=400.0, cache=15_200, draft_n=i, draft_accepted=i // 2))
        orologio.avanti(0.001 if i % 10 else 0.5)   # pezzi a grappoli
    m = t.aggiorna(forza=True)
    assert m.fonte == "server"
    assert m.generati == 60
    assert m.tok_s == pytest.approx(25.0, rel=0.02)
    assert m.tok_s_passo == pytest.approx(25.0, rel=0.02)
    assert m.prompt == 16_000                # calcolati + dalla cache
    assert m.cache == 15_200 and m.prefill_token == 800 and m.prefill_ms == 400
    assert (m.draft_n, m.draft_accettati) == (60, 30)


def test_il_progresso_del_prefill_accende_la_fase_prefill():
    orologio = Orologio()
    t = Tachimetro(1, orologio=orologio)
    t.evento(battito(prefill_totale=40_000, prefill_cache=30_000, prefill_fatti=4_096,
                     prefill_trascorsi_ms=900))
    m = t.aggiorna(forza=True)
    assert m.fase == "prefill"
    assert (m.prefill_fatti, m.prefill_totale) == (4_096, 40_000)


def test_gli_argomenti_di_una_chiamata_si_contano_mentre_arrivano():
    orologio = Orologio()
    t = Tachimetro(1, un_token_per_chunk=True, orologio=orologio)
    t.evento(pezzo("content", "Scrivo il file."))
    for _ in range(30):
        t.evento(battito(chiamata=12))
    m = t.aggiorna(forza=True)
    assert m.fase == "chiamata"
    assert m.chiamate == 30 and m.risposta == 1


def test_al_piu_quattro_aggiornamenti_al_secondo():
    orologio = Orologio()
    t = Tachimetro(1, un_token_per_chunk=True, orologio=orologio)
    assert t.aggiorna(forza=True) is not None
    t.evento(pezzo("content"))
    assert t.aggiorna() is None                  # troppo presto
    orologio.avanti(cruscotto.INTERVALLO_S)
    assert t.aggiorna() is not None
    orologio.avanti(cruscotto.INTERVALLO_S)
    assert t.aggiorna() is None                  # niente di nuovo da dire


def test_la_continuazione_somma_le_due_richieste_del_passo():
    """I contatori del server ripartono a ogni richiesta; il passo no."""
    orologio = Orologio()
    t = Tachimetro(1, un_token_per_chunk=True, orologio=orologio)
    t.evento(pezzo("reasoning"))
    t.evento(battito(generati=100, generazione_ms=4000.0, draft_n=80, draft_accepted=40))
    t.evento(StreamEvent("usage", usage={"completion_tokens": 100, "eval_ms": 4000,
                                         "draft_n": 80, "draft_accepted": 40}))
    t.nuova_richiesta()
    t.evento(pezzo("content"))
    t.evento(battito(generati=50, generazione_ms=2000.0, draft_n=20, draft_accepted=10))
    fine = t.chiudi()
    assert fine.definitivo and fine.fase == "fine"
    assert fine.generati == 150
    assert fine.generazione_ms == 6000
    assert fine.tok_s_passo == pytest.approx(25.0)
    assert (fine.draft_n, fine.draft_accettati) == (100, 50)


def test_ollama_a_fine_passo_i_numeri_esatti():
    """``eval_count``/``eval_duration`` vincono sulla stima dei pezzi."""
    orologio = Orologio()
    t = Tachimetro(1, un_token_per_chunk=True, prompt_intero=False, orologio=orologio)
    for _ in range(10):
        t.evento(pezzo("content"))
        orologio.avanti(0.1)
    t.evento(StreamEvent("usage", usage={"completion_tokens": 12, "eval_ms": 400,
                                         "prompt_tokens": 350, "prompt_eval_ms": 70}))
    fine = t.chiudi()
    assert fine.fonte == "server"
    assert fine.generati == 12 and fine.tok_s_passo == pytest.approx(30.0)
    # Su Ollama ``prompt_eval_count`` sono i token calcolati, non il prompt
    # intero: il prompt resta "non dichiarato" e il prefill e' misurato.
    assert fine.prompt is None
    assert fine.prefill_token == 350 and fine.prefill_ms == 70


# ---------------------------------------------------------------------------
# Registro e storico
# ---------------------------------------------------------------------------


def _definitiva(passo: int, **campi: Any) -> Metriche:
    base = {"fase": "fine", "definitivo": True, "generati": 100, "generazione_ms": 4000,
            "tok_s_passo": 25.0, "attesa_ms": 800, "durata_ms": 4800, "prompt": 10_000,
            "finestra": 65_536}
    base.update(campi)
    return Metriche(passo=passo, **base)


def test_il_registro_fa_una_riga_per_passo_con_tool_e_compattazioni():
    orologio = Orologio()
    registro = Registro(orologio=orologio)
    registro.osserva(agent.StepStarted(step=1, total=10))
    registro.osserva(Metriche(passo=1, fase="pensiero"))           # dal vivo: ignorata
    registro.osserva(_definitiva(1))
    for nome, durata in (("read_file", 0.2), ("read_file", 0.1), ("run_command", 3.0)):
        registro.osserva(agent.ToolFinished(call_id="c", name=nome, args={}, result="",
                                            duration_s=durata, ok=True))
    orologio.avanti(9.0)
    registro.osserva(agent.HistoryCompacted(messages=10, tokens_before=30_000, tokens_after=8_000))
    registro.osserva(agent.StepStarted(step=2, total=10))
    registro.osserva(_definitiva(2, prompt=8_500))
    righe = registro.chiudi()
    assert [r["passo"] for r in righe] == [1, 2]
    assert righe[0]["tool"] == {"read_file": 2, "run_command": 1}
    assert righe[0]["tool_ms"] == 3300
    assert righe[0]["durata_ms"] == 9000         # dall'inizio del passo al successivo
    assert not righe[0]["compattato"] and righe[1]["compattato"]
    json.dumps(righe)


def test_lo_storico_usa_le_righe_salvate_e_i_punti_di_tutti_i_turni():
    turni = [
        {"cruscotto": [cruscotto.riga_da_metriche(_definitiva(1, prompt=5_000, tok_s_passo=31.0))],
         "turn_wall_ms": 6000, "steps": 1, "turn_reason": "completed"},
        {"cruscotto": [cruscotto.riga_da_metriche(_definitiva(1, prompt=20_000, tok_s_passo=24.0)),
                       cruscotto.riga_da_metriche(_definitiva(2, prompt=22_000, tok_s_passo=23.0))],
         "turn_wall_ms": 12_000, "steps": 2, "turn_reason": "completed"},
    ]
    s = storico(turni)
    assert s["punti"] == [[5000, 31.0, -1], [20000, 24.0, 0], [22000, 23.0, 0]]
    assert [t["passi"] for t in s["turni"]] == [1, 2]
    assert s["turni"][1]["tok_s"] == pytest.approx(25.0)
    assert len(s["ultimo"]["righe"]) == 2 and s["ultimo"]["durata_ms"] == 12_000


def test_i_turni_di_prima_si_ricostruiscono_dalle_chiamate():
    """Le chat registrate prima del Registro hanno comunque un cruscotto."""
    chiamata = {
        "purpose": "main", "backend": "LlamaCppBackend", "wall_time_ms": 5000.0,
        "first_output_ms": 1000.0, "started_at": "2026-09-20T10:00:00.000+00:00",
        "input_tokens_estimated": 9000, "config": {"num_ctx": 32768},
        "usage": {"prompt_tokens": 10_000, "completion_tokens": 100, "eval_ms": 4000,
                  "cached_tokens": 9_500, "draft_n": 60, "draft_accepted": 45},
    }
    seconda = {**chiamata, "started_at": "2026-09-20T10:00:07.000+00:00",
               "usage": {**chiamata["usage"], "prompt_tokens": 10_400}}
    servizio = {**chiamata, "purpose": "compaction"}
    s = storico([{"calls": [chiamata, servizio, seconda], "turn_wall_ms": 13_000, "steps": 2}])
    righe = s["ultimo"]["righe"]
    assert len(righe) == 2 and righe[0]["fonte"] == "telemetria"
    assert righe[0]["attesa_ms"] == 1000 and righe[0]["generazione_ms"] == 4000
    assert righe[0]["tool_ms"] == 2000           # 7 s fra le partenze - 5 s di chiamata
    assert righe[0]["tok_s"] == 25.0 and righe[0]["cache"] == 9_500
    assert s["punti"][0] == [10_000, 25.0, 0]


def test_su_ollama_il_prompt_dello_storico_e_la_stima():
    chiamata = {"purpose": "main", "backend": "OllamaBackend", "wall_time_ms": 3000.0,
                "first_output_ms": 500.0, "input_tokens_estimated": 7777,
                "usage": {"prompt_tokens": 120, "completion_tokens": 50, "eval_ms": 2000}}
    s = storico([{"calls": [chiamata]}])
    assert s["punti"] == [[7777, 25.0, 0]]


def test_storico_regge_telemetria_rovinata():
    assert storico(None) == {"punti": [], "turni": [], "ultimo": None}
    s = storico([17, "x", {"calls": "rotto"}, {"cruscotto": [None, {"prompt": "?"}]}])
    assert s["punti"] == []


def test_lo_storico_della_sessione_si_rifa_solo_se_la_telemetria_cambia():
    sessione: dict[str, Any] = {"turn_telemetry": [{"cruscotto": [
        cruscotto.riga_da_metriche(_definitiva(1))]}]}
    primo = cruscotto.storico_della_sessione(sessione)
    assert cruscotto.storico_della_sessione(sessione) is primo
    session_mod.record_turn_telemetry(sessione, {"calls": []}, reason="completed", steps=1,
                                      cruscotto=[cruscotto.riga_da_metriche(_definitiva(1))])
    assert cruscotto.storico_della_sessione(sessione) is not primo
    assert len(sessione["turn_telemetry"]) == 2
    assert "cruscotto" in sessione["turn_telemetry"][-1]


# ---------------------------------------------------------------------------
# Transport: i battiti
# ---------------------------------------------------------------------------


@pytest.fixture()
def wire_client(monkeypatch):
    clients: list[httpx.Client] = []

    def install(handler):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        clients.append(client)
        monkeypatch.setattr(transport, "get_client", lambda: client)
        return client

    yield install
    for client in clients:
        client.close()


def _sse(*chunks: Any) -> bytes:
    righe = b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks)
    return righe + b"data: [DONE]\n\n"


def _choice(delta: dict[str, Any] | None = None, finish: str | None = None, **extra: Any):
    return {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}], **extra}


def test_llamacpp_manda_i_timings_e_il_progresso_come_battiti(wire_client):
    corpi: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        corpi.append(json.loads(request.content))
        timings = {"prompt_n": 700, "prompt_ms": 350.5, "cache_n": 9300,
                   "predicted_n": 1, "predicted_ms": 38.2,
                   "draft_n": 3, "draft_n_accepted": 2}
        return httpx.Response(200, content=_sse(
            _choice({"role": "assistant", "content": None},
                    prompt_progress={"total": 10_000, "cache": 9300, "processed": 700,
                                     "time_ms": 350}),
            _choice({"content": "Ciao"}, timings=timings),
            _choice(finish="stop", timings=timings),
        ))

    wire_client(handler)
    eventi = list(LlamaCppBackend("http://llama", "", timeout_s=2).stream(
        [{"role": "user", "content": "ciao"}], None, GenParams(model="m")))
    assert corpi[0]["return_progress"] is True and corpi[0]["timings_per_token"] is True
    battiti = [e.usage for e in eventi if e.kind == "battito"]
    assert battiti[0] == {"prefill_totale": 10_000, "prefill_cache": 9300,
                          "prefill_fatti": 700, "prefill_trascorsi_ms": 350}
    assert battiti[1]["generati"] == 1 and battiti[1]["cache"] == 9300
    assert battiti[1]["draft_n"] == 3 and battiti[1]["draft_accepted"] == 2
    assert [e.kind for e in eventi if e.kind != "battito"] == ["content", "usage"]


def test_gli_argomenti_di_una_chiamata_battono_senza_uscire_prima_della_fine(wire_client):
    wire_client(lambda request: httpx.Response(200, content=_sse(
        _choice({"tool_calls": [{"index": 0, "id": "c1", "function": {
            "name": "write_file", "arguments": '{"filepath": "a.py", '}}]}),
        _choice({"tool_calls": [{"index": 0, "function": {"arguments": '"content": "x"}'}}]}),
        _choice(finish="tool_calls"),
    )))
    eventi = list(OpenAICompatBackend("http://local", "k", timeout_s=2).stream(
        [], None, GenParams(model="m")))
    tipi = [e.kind for e in eventi]
    assert tipi == ["battito", "battito", "usage", "tool_call"]
    assert [e.usage["chiamata"] for e in eventi[:2]] == [len('{"filepath": "a.py", '),
                                                           len('"content": "x"}')]


def test_un_battito_non_impedisce_di_riprovare(monkeypatch, wire_client):
    """Il retry vale finche' non e' uscito niente di osservabile: i battiti no.

    La prima risposta manda un pezzo di argomenti (un battito) e poi la
    connessione cade. Contare il battito come output impedirebbe di riprovare.
    """
    monkeypatch.setattr(transport, "_retry_delay", lambda *a, **k: 0.0)

    class Cade(httpx.SyncByteStream):
        def __iter__(self):
            yield b"data: " + json.dumps(_choice({"tool_calls": [
                {"index": 0, "id": "c1", "function": {"name": "read_file", "arguments": "{"}}]}
            )).encode() + b"\n\n"
            raise httpx.RemoteProtocolError("peer closed")

    risposte = iter([
        httpx.Response(200, stream=Cade()),
        httpx.Response(200, content=_sse(_choice({"content": "ok"}), _choice(finish="stop"))),
    ])
    wire_client(lambda request: next(risposte))
    eventi = list(OpenAICompatBackend("http://local", "k", timeout_s=2).stream(
        [], None, GenParams(model="m")))
    assert eventi[0].kind == "battito"          # il primo tentativo aveva battuto
    assert [e.kind for e in eventi if e.kind != "battito"] == ["content", "usage"]


# ---------------------------------------------------------------------------
# Il ciclo: le Metriche di ogni passo
# ---------------------------------------------------------------------------


class ScriptBackend:
    un_token_per_chunk = True

    def __init__(self, passi: list[list[StreamEvent]]) -> None:
        self.passi = iter(passi)

    def stream(self, messages: Any, tools: Any, params: Any, **_: Any):
        yield from next(self.passi, [StreamEvent("content", text="Fine.")])


def test_ogni_passo_apre_con_attesa_e_chiude_con_una_metrica_definitiva(tmp_path):
    chiamata = StreamEvent("tool_call", tool_call={"id": "c1", "name": "list_files",
                                                   "arguments": json.dumps({"subfolder": "."})})
    passi = [
        [StreamEvent("reasoning", text="guardo"), StreamEvent("battito", usage={"chiamata": 5}),
         chiamata, StreamEvent("usage", usage={"completion_tokens": 7, "eval_ms": 350})],
        [StreamEvent("content", text="Ecco."),
         StreamEvent("usage", usage={"completion_tokens": 3, "eval_ms": 100})],
    ]
    eventi = list(agent.run_turn(
        backend=ScriptBackend(passi), params=GenParams(model="fake", num_ctx=16_384),
        tools_schema=TOOLS_SCHEMA, tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
        ui_messages=[{"role": "user", "content": "cosa c'e'?"}], system_prompt="SYS",
        env_header=None, max_steps=5, require_plan=False, compact_history=False,
        auto_preview=False, estratto_pensiero=False, libreria_attiva=False,
    ))
    metriche = [e for e in eventi if isinstance(e, Metriche)]
    definitive = [m for m in metriche if m.definitivo]
    assert [m.passo for m in definitive] == [1, 2]
    assert definitive[0].generati == 7 and definitive[0].tok_s_passo == pytest.approx(20.0)
    assert definitive[0].finestra == 16_384 and definitive[0].prompt_stimato > 0
    prime = [m for m in metriche if not m.definitivo and m.fase == "attesa"]
    assert len(prime) >= 2                      # una all'inizio di ogni passo
    # La metrica definitiva precede le tool call del suo passo: il registro
    # attacca i tool alla riga giusta.
    i_def = eventi.index(definitive[0])
    i_tool = next(i for i, e in enumerate(eventi) if isinstance(e, agent.ToolFinished))
    assert i_def < i_tool
    assert isinstance(eventi[-1], agent.TurnFinished)


# ---------------------------------------------------------------------------
# Il runner: frame volatili
# ---------------------------------------------------------------------------


def _frame(tipo: str, **campi: Any) -> str:
    return f"data: {json.dumps({'type': tipo, **campi})}\n\n"


def test_dei_frame_volatili_l_arretrato_tiene_solo_l_ultimo_al_suo_posto():
    runner = TurnRunner("s", [])
    runner.emit(_frame("start"))
    for i in range(50):
        runner.emit(_frame("metriche", n=i), volatile="metriche")
    runner.emit(_frame("reasoning", append="a"))
    runner.emit(_frame("metriche", n=99, definitivo=True))
    assert len(runner.frames) == 3                 # i volatili non entrano nell'arretrato
    arretrato, _ = runner.subscribe()
    tipi = [json.loads(f[6:]) for f in arretrato]
    assert [t["type"] for t in tipi] == ["start", "metriche", "reasoning", "metriche"]
    assert tipi[1]["n"] == 49 and tipi[3]["n"] == 99


def test_un_volatile_non_stacca_un_abbonato_lento():
    runner = TurnRunner("s", [])
    _arretrato, coda = runner.subscribe()
    for i in range(3000):                          # oltre la capienza della coda
        runner.emit(_frame("metriche", n=i), volatile="metriche")
    assert coda in runner._subscribers
