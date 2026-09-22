"""Audit del 22/09/2026: prefisso stabile, scadenze del transport, finestra.

Ogni test qui fallisce sul codice di prima e dice perche' in una riga: sono i
difetti misurati nel rapporto ``docs/audit/AUDIT_2026-09-22.md``.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import agent as agent_mod
from core import backend as backend_mod
from core.agent import (
    FATTORE_STIMA_MAX,
    build_api_messages,
    calibra_stima,
    drop_oldest_turns,
    risultati_integrali,
    tetto_per_la_finestra,
)
from core.backend import (
    PREFISSO_NOTA_HARNESS,
    LlamaCppBackend,
    StreamEvent,
    _request_timeout,
    _stream_with_retries,
)
from core.config import Budgets, GenParams, budgets_for
from core.prompts import (
    MAX_RIGHE_AGGIORNAMENTO,
    TAG_AGGIORNAMENTO,
    aggiornamento_albero,
    build_env_header,
)
from core.textutils import estimate_messages_tokens
from core.tools import TOOLS_SCHEMA, ToolContext, workspace_snapshot


# ---------------------------------------------------------------------------
# Finestra dei risultati integrali: a scatti, non a ogni passo
# ---------------------------------------------------------------------------


def _lettura(i: int, chars: int) -> list[dict]:
    cid = f"c{i}"
    return [
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": cid, "type": "function",
            "function": {"name": "read_file", "arguments": json.dumps({"filepath": f"f{i}.py"})},
        }]},
        {"role": "tool", "tool_call_id": cid, "name": "read_file", "ok": True,
         "content": json.dumps({"filepath": f"f{i}.py", "content": "x" * chars})},
    ]


def _storia(passi: int, chars: int = 2_000) -> list[dict]:
    ui = [{"role": "user", "content": "leggi tutto"}]
    for i in range(passi):
        ui += _lettura(i, chars)
    return ui


def _richiesta(ui: list[dict], budgets: Budgets) -> list[dict]:
    return build_api_messages(ui, system_prompt="S", env_header="E", budgets=budgets)


def _divergenza(prima: list[dict], dopo: list[dict]) -> int:
    i = 0
    while i < min(len(prima), len(dopo)) and prima[i] == dopo[i]:
        i += 1
    return i


def test_la_finestra_non_riscrive_la_cronologia_a_ogni_passo():
    """Prima: ogni passo compattava il risultato uscito dalla finestra, e il
    server ricalcolava da li' in poi anche cio' che aveva in cache."""
    budgets = Budgets(tool_result_full_window=3, tool_result_full_tokens=6_000)
    riscritture = 0
    for passi in range(4, 30):
        prima = _richiesta(_storia(passi), budgets)
        dopo = _richiesta(_storia(passi + 1), budgets)
        if _divergenza(prima, dopo) < len(prima):
            riscritture += 1
    # 26 passi: con la finestra che scorre sarebbero 26 riscritture.
    assert riscritture <= 9, riscritture


def test_senza_quota_resta_il_comportamento_di_prima():
    budgets = Budgets(tool_result_full_window=3)
    ui = _storia(10)
    posizioni = [i for i, m in enumerate(ui) if m["role"] == "tool"]
    assert risultati_integrali(ui, posizioni, budgets) == set(posizioni[-3:])


@pytest.mark.parametrize("passi", range(1, 40))
def test_la_zona_integrale_rispetta_la_quota_o_la_finestra(passi):
    """L'invariante di ``budgets_for``: al momento dell'invio la zona integrale
    o sta nella quota, o contiene esattamente N risultati."""
    budgets = Budgets(tool_result_full_window=3, tool_result_full_tokens=5_000)
    ui = _storia(passi, chars=3_000)
    posizioni = [i for i, m in enumerate(ui) if m["role"] == "tool"]
    zona = risultati_integrali(ui, posizioni, budgets)
    costo = estimate_messages_tokens([ui[p] for p in zona])
    assert len(zona) <= 3 or costo <= 5_000 + 200


def test_le_scritture_pesanti_contano_nella_zona():
    """Un write_file costa nel messaggio assistant, non nel risultato."""
    ui = [{"role": "user", "content": "scrivi"}]
    for i in range(8):
        cid = f"w{i}"
        ui += [
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": cid, "type": "function",
                "function": {"name": "write_file", "arguments": json.dumps(
                    {"filepath": f"m{i}.py", "content": "y" * 9_000})},
            }]},
            {"role": "tool", "tool_call_id": cid, "name": "write_file", "ok": True,
             "content": json.dumps({"status": "ok", "filepath": f"m{i}.py"})},
        ]
    budgets = Budgets(tool_result_full_window=2, tool_result_full_tokens=6_000)
    posizioni = [i for i, m in enumerate(ui) if m["role"] == "tool"]
    assert len(risultati_integrali(ui, posizioni, budgets)) <= 3


def test_budgets_for_valorizza_la_quota():
    assert budgets_for(131_072).tool_result_full_tokens > 0
    assert Budgets().tool_result_full_tokens == 0


# ---------------------------------------------------------------------------
# Stima dei token corretta sulla misura del server
# ---------------------------------------------------------------------------


def test_calibra_stima_e_limitata():
    assert calibra_stima(1_300, 1_000) == pytest.approx(1.3)
    assert calibra_stima(800, 1_000) == 1.0            # mai sotto la stima
    assert calibra_stima(10_000, 1_000) == FATTORE_STIMA_MAX
    assert calibra_stima(None, 1_000) is None
    assert calibra_stima(True, 1_000) is None


def test_il_tetto_scende_quando_il_server_conta_piu_token():
    msgs = [{"role": "user", "content": "x" * 72_000}]    # ~20.000 stimati
    tetto_1, spazio_1 = tetto_per_la_finestra(msgs, 32_768, 16_000)
    tetto_2, spazio_2 = tetto_per_la_finestra(msgs, 32_768, 16_000, fattore=1.3)
    assert spazio_2 < spazio_1 - 5_000
    assert tetto_2 < tetto_1


class _BackendConConteggio:
    """Il server conta il 40% di token in piu' della stima."""

    prompt_tokens_is_total = True

    def __init__(self) -> None:
        self.tetti: list[int] = []
        self.passo = 0

    def stream(self, messages, tools, params):
        self.tetti.append(params.max_tokens)
        self.passo += 1
        stimati = estimate_messages_tokens(messages) + (
            agent_mod.estimate_tokens(json.dumps(tools, ensure_ascii=False)) if tools else 0)
        if self.passo < 3:
            yield StreamEvent("tool_call", tool_call={
                "id": f"c{self.passo}", "name": "list_files", "arguments": "{}"})
        else:
            yield StreamEvent("content", text="finito")
        yield StreamEvent("usage", usage={"prompt_tokens": int(stimati * 1.4),
                                          "done_reason": "stop"})


def test_il_turno_usa_la_misura_del_server_per_il_tetto(tmp_path):
    be = _BackendConConteggio()
    schema = [t for t in TOOLS_SCHEMA if t["function"]["name"] == "list_files"]
    ui = [{"role": "user", "content": "guarda " + "z" * 40_000}]
    list(agent_mod.run_turn(
        backend=be, params=GenParams(model="f", num_ctx=32_768, max_tokens=30_000),

        tools_schema=schema, tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
        ui_messages=ui, system_prompt="S", env_header="E", max_steps=3,
        require_summary=False, require_plan=False, compact_history=False,
    ))
    # Dal secondo passo il tetto tiene conto dei token veri, non della stima.
    assert be.tetti[1] < be.tetti[0] - 2_000
    traccia = [m["think"] for m in ui if m.get("role") == "assistant" and m.get("think")]
    assert traccia[0]["prompt_reale"] and traccia[-1]["fattore_stima"] == pytest.approx(1.4, 0.05)


# ---------------------------------------------------------------------------
# Compattazione che fallisce: non ogni passo
# ---------------------------------------------------------------------------


class _RiassuntoRotto:
    def __init__(self) -> None:
        self.riassunti = 0
        self.passo = 0

    def stream(self, messages, tools, params):
        if tools is None:
            self.riassunti += 1
            yield StreamEvent("error", text="riassunto rifiutato")
            return
        self.passo += 1
        yield StreamEvent("tool_call", tool_call={
            "id": f"c{self.passo}", "name": "list_files", "arguments": "{}"})
        yield StreamEvent("usage", usage={"done_reason": "stop"})


def test_una_compattazione_fallita_non_si_riprova_a_ogni_passo(tmp_path):
    be = _RiassuntoRotto()
    schema = [t for t in TOOLS_SCHEMA if t["function"]["name"] == "list_files"]
    ui = [{"role": "user", "content": "scrivi"}]
    for i in range(14):
        cid = f"w{i}"
        ui += [
            {"role": "assistant", "content": "a" * 600, "tool_calls": [{
                "id": cid, "type": "function",
                "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": cid, "name": "read_file", "ok": True,
             "content": json.dumps({"content": "q" * 1_500})},
        ]
    list(agent_mod.run_turn(
        backend=be, params=GenParams(model="f", num_ctx=4_096, max_tokens=512),
        tools_schema=schema, tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
        ui_messages=ui, system_prompt="S", env_header="E", max_steps=6,
        require_summary=False, require_plan=False,
    ))
    # Sei passi sopra soglia: prima sei riassunti falliti, adesso uno ogni
    # PAUSA_COMPATTAZIONE_FALLITA passi.
    assert 1 <= be.riassunti <= 2


# ---------------------------------------------------------------------------
# Ultima spiaggia: il compito resta, il prefisso non si riscrive a ogni passo
# ---------------------------------------------------------------------------


def _gonfia(coppie: int) -> list[dict]:
    msgs = [{"role": "system", "content": "S"},
            {"role": "user", "content": "IL COMPITO: sistemare il parser"}]
    for i in range(coppie):
        msgs += [
            {"role": "assistant", "content": "a", "tool_calls": [{
                "id": f"c{i}", "type": "function",
                "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"c{i}", "name": "read_file",
             "content": "x" * 3_000},
        ]
    msgs.append({"role": "user", "content": "<piano>coda</piano>"})
    return msgs


def test_lo_scarto_tiene_la_richiesta_dell_utente():
    out = drop_oldest_turns(_gonfia(40), 8_192)
    assert out[1]["content"].startswith("IL COMPITO")
    assert out[-1]["content"] == "<piano>coda</piano>"


def test_la_nota_dello_scarto_non_tocca_il_prompt_di_sistema():
    out = drop_oldest_turns(_gonfia(40), 8_192)
    assert [m["role"] for m in out[:1]] == ["system"] and out[0]["content"] == "S"
    assert out[2]["role"] == "user" and out[2]["content"].startswith(PREFISSO_NOTA_HARNESS)


def test_lo_scarto_scende_sotto_la_soglia():
    """Tornare esattamente alla parete voleva dire scartare di nuovo al passo
    dopo, e ricalcolare tutto il prompt ogni volta."""
    out = drop_oldest_turns(_gonfia(40), 8_192, soglia=0.75)
    assert estimate_messages_tokens(out) <= 8_192 * (0.75 - agent_mod.MARGINE_SCARTO) + 900


def test_sotto_la_soglia_non_si_scarta_niente():
    msgs = _gonfia(2)
    assert drop_oldest_turns(list(msgs), 131_072) == msgs


# ---------------------------------------------------------------------------
# Transport: timeout di inattivita', non scadenza della generazione
# ---------------------------------------------------------------------------


def test_una_generazione_lunga_non_viene_tagliata_dal_timeout():
    """Prima ``timeout_seconds`` era la durata massima dell'intera generazione:
    un passo di pensiero lungo moriva a meta', con output gia' emesso."""

    def lento(_deadline):
        for i in range(6):
            time.sleep(0.1)
            yield StreamEvent("content", text=str(i))

    eventi = list(_stream_with_retries(lento, 0.25, None))
    assert [e.kind for e in eventi] == ["content"] * 6


def test_il_tetto_assoluto_resta(monkeypatch):
    monkeypatch.setattr(backend_mod, "DURATA_MAX_GENERAZIONE_S", 0.2)

    def lento(_deadline):
        for i in range(10):
            time.sleep(0.05)
            yield StreamEvent("content", text=str(i))

    eventi = list(_stream_with_retries(lento, 0.1, None))
    assert eventi[-1].kind == "error"


def test_la_lettura_usa_l_inattivita():
    t = _request_timeout(time.monotonic() + 3_600, 30.0)
    assert t.read == pytest.approx(30.0)
    assert _request_timeout(time.monotonic() + 5, 30.0).read <= 5


# ---------------------------------------------------------------------------
# llama-server: chiamate multiple e misura della cache
# ---------------------------------------------------------------------------


def test_llamacpp_chiede_le_chiamate_multiple():
    be = LlamaCppBackend("http://127.0.0.1:9")
    assert be._extra_body(GenParams(model="m"))["parallel_tool_calls"] is True


def test_llamacpp_legge_cache_e_ricalcolati():
    be = LlamaCppBackend("http://127.0.0.1:9")
    fuori = be._extra_usage({"timings": {"cache_n": 30_000, "prompt_n": 412,
                                         "prompt_ms": 350.0, "predicted_ms": 900.0}})
    assert fuori["cached_tokens"] == 30_000
    assert fuori["prompt_processed_tokens"] == 412


def test_i_trasporti_compatibili_dichiarano_il_prompt_intero():
    assert backend_mod.OpenAICompatBackend.prompt_tokens_is_total is True
    assert backend_mod.OllamaBackend.prompt_tokens_is_total is False


# ---------------------------------------------------------------------------
# Environment fermo fra i turni, differenze accodate
# ---------------------------------------------------------------------------


def test_aggiornamento_albero_elenca_le_differenze(tmp_path):
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "a.py").write_text("x")
    base = workspace_snapshot(str(tmp_path))
    (tmp_path / "core" / "a.py").write_text("xyz")
    (tmp_path / "core" / "b.py").write_text("nuovo")
    nota, rifai = aggiornamento_albero(base, workspace_snapshot(str(tmp_path)))
    assert not rifai
    assert "+ core/b.py" in nota and "~ core/a.py" in nota
    assert nota.startswith(f"<{TAG_AGGIORNAMENTO}>")


def test_albero_uguale_niente_nota(tmp_path):
    (tmp_path / "a.py").write_text("x")
    albero = workspace_snapshot(str(tmp_path))
    assert aggiornamento_albero(albero, albero) == ("", False)


def test_troppe_differenze_si_rifa_la_base(tmp_path):
    base = workspace_snapshot(str(tmp_path))
    for i in range(MAX_RIGHE_AGGIORNAMENTO + 5):
        (tmp_path / f"f{i}.txt").write_text("x")
    assert aggiornamento_albero(base, workspace_snapshot(str(tmp_path))) == ("", True)


def test_l_environment_con_albero_fermo_non_cambia_col_disco(tmp_path):
    (tmp_path / "a.py").write_text("x")
    albero = workspace_snapshot(str(tmp_path))
    prima = build_env_header(str(tmp_path), albero=albero)
    (tmp_path / "a.py").write_text("molto piu' lungo")
    assert build_env_header(str(tmp_path), albero=albero) == prima
    assert build_env_header(str(tmp_path)) != prima


def test_il_server_accoda_la_nota_una_volta_e_tiene_l_albero(tmp_path, monkeypatch):
    import copy

    from server import main as main_mod

    stato = copy.copy(main_mod.STATE)
    stato.settings = dict(main_mod.STATE.settings, workspace_dir=str(tmp_path))
    sessioni: dict[str, dict] = {}
    monkeypatch.setattr(stato, "session", lambda sid: sessioni.setdefault(sid, {}), raising=False)
    (tmp_path / "a.py").write_text("x")
    msgs: list[dict] = [{"role": "user", "content": "primo"}]
    base = stato.albero_del_turno("s1", msgs)
    assert len(msgs) == 1                         # primo turno: niente nota

    (tmp_path / "b.py").write_text("y")
    msgs.append({"role": "user", "content": "secondo"})
    assert stato.albero_del_turno("s1", msgs) == base      # environment fermo
    note = [m for m in msgs if m.get("kind") == TAG_AGGIORNAMENTO]
    assert len(note) == 1 and note[0]["hidden"] and "+ b.py" in note[0]["content"]

    msgs.append({"role": "user", "content": "terzo"})
    stato.albero_del_turno("s1", msgs)
    assert len([m for m in msgs if m.get("kind") == TAG_AGGIORNAMENTO]) == 1


# ---------------------------------------------------------------------------
# Richiesta con una parola lunghissima: l'avvio del turno non si blocca
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("testo", [
    "guarda " + "z" * 50_000,
    "questo token: " + "aGVsbG8gd29ybGQ" * 4_000,
    "percorso " + "a/" * 20_000,
])
def test_una_parola_lunghissima_non_blocca_il_turno(testo):
    """Prima: costo cubico nella lunghezza di una sequenza senza spazi, 31 s a
    4.000 caratteri. Una riga di base64 incollata fermava tutto il server."""
    inizio = time.perf_counter()
    agent_mod.looks_multi_step(testo)
    assert time.perf_counter() - inizio < 1.0


def test_gli_artefatti_si_riconoscono_ancora():
    testo = ("dentro `cantiere/grezzi/` quattro file. `cantiere/misura.py` ne ricava "
             "`cantiere/misure.csv`, poi README.md e lunghi/")
    assert agent_mod.artefatti_nominati(testo) == {
        "cantiere/grezzi/", "cantiere/misura.py", "cantiere/misure.csv", "readme.md", "lunghi/",
    }
