"""Regia del pensiero: tipi di punto, budget, osservatore e continuazione.

Il perche' sta in ``core/regia_pensiero.py``. Qui si prova che:

* il tipo lo scrive il modello nel testo del punto e sopravvive a disco e ai
  ``set`` successivi;
* l'harness corregge il tipo con i fatti gia' accaduti, mai con quelli futuri;
* l'osservatore chiude per budget, per oscillazione e per decisione scritta;
* sul canale nativo di Ollama la chiusura **continua** la generazione invece
  di buttarla, e ricade sul watchdog se il server non la rispetta.
"""

from __future__ import annotations

import json

import pytest

import tests.test_agent_loop as fake
from core import agent as agent_mod
from core import regia_pensiero as regia
from core.backend import OllamaBackend, to_ollama_messages
from core.config import GenParams
from core.plan import Plan, PlanError, render_block
from core.tools import PLAN_TOOL, TOOLS_SCHEMA, ToolContext, dispatch

fake_ollama = fake.fake_ollama

# ---------------------------------------------------------------------------
# Tipi nel testo dei punti
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("grezzo", "tipo", "testo"),
    [
        ("diagnosi: capire perche' fallisce", "diagnosi", "capire perche' fallisce"),
        ("[esegui] scrivere il test", "esegui", "scrivere il test"),
        ("(Indaga) leggere il parser", "indaga", "leggere il parser"),
        ("design - scegliere lo schema", "progetta", "scegliere lo schema"),
        ("Nota: non e' un tipo", None, "Nota: non e' un tipo"),
        ("README - sezione install", None, "README - sezione install"),
        ("scrivere il test", None, "scrivere il test"),
    ],
)
def test_il_tipo_si_legge_dal_prefisso_e_solo_se_e_un_tipo(grezzo, tipo, testo):
    assert regia.separa_tipo(grezzo) == (tipo, testo)


def test_il_piano_conserva_il_tipo_e_un_set_senza_prefisso_non_lo_cancella():
    p = Plan()
    p.set_steps(["indaga: leggere il parser", "esegui: correggere _has_cycle"])
    assert [s.tipo for s in p.steps] == ["indaga", "esegui"]
    assert [s.text for s in p.steps] == ["leggere il parser", "correggere _has_cycle"]

    p.start("1")
    p.set_steps(["leggere il parser", "diagnosi: correggere _has_cycle"])
    assert [s.tipo for s in p.steps] == ["indaga", "diagnosi"]

    rilett = Plan.from_list(p.to_list())
    assert [s.tipo for s in rilett.steps] == ["indaga", "diagnosi"]


def test_il_blocco_mostra_il_tipo_e_le_istruzioni_del_punto_in_corso():
    p = Plan()
    p.set_steps(["diagnosi: capire il test rosso", "esegui: correggere"])
    p.avanza()
    p.annota_ipotesi("cache vecchia -> svuotarla -> ancora rosso")
    blocco = render_block(p)
    assert "(diagnosi) capire il test rosso" in blocco
    assert "ipotesi: cache vecchia -> svuotarla -> ancora rosso" in blocco
    assert regia.ISTRUZIONI_TIPO["diagnosi"] in blocco


def test_una_ipotesi_riscritta_aggiorna_la_riga_invece_di_duplicarla():
    p = Plan()
    p.set_steps(["diagnosi: capire"])
    p.avanza()
    p.annota_ipotesi("path relativo -> stampare cwd")
    p.annota_ipotesi("Path relativo -> stampare cwd -> era /tmp, confermata")
    assert p.current.ipotesi == ["Path relativo -> stampare cwd -> era /tmp, confermata"]
    for i in range(10):
        p.annota_ipotesi(f"ipotesi {i} -> prova")
    assert len(p.current.ipotesi) == 6


def test_senza_punto_in_corso_l_ipotesi_viene_rifiutata():
    with pytest.raises(PlanError):
        Plan().annota_ipotesi("x -> y")


def test_manage_plan_ipotesi_via_dispatch(tmp_path):
    ctx = ToolContext(workspace=str(tmp_path), sandbox="host")
    dispatch(ctx, PLAN_TOOL, {"action": "set", "steps": ["diagnosi: capire"]})
    esito = json.loads(dispatch(ctx, PLAN_TOOL, {"action": "ipotesi", "note": "import ciclico -> pytest -x"}))
    assert esito["status"] == "ok"
    assert ctx.plan.current.ipotesi == ["import ciclico -> pytest -x"]


def test_to_ollama_messages_porta_il_pensiero_solo_se_c_e():
    out = to_ollama_messages([
        {"role": "user", "content": "ciao"},
        {"role": "assistant", "content": "ok"},
        {"role": "assistant", "content": "", "thinking": "parziale"},
    ])
    assert "thinking" not in out[1]
    assert out[2]["thinking"] == "parziale"


# ---------------------------------------------------------------------------
# Decisione del budget: solo da cio' che e' gia' successo
# ---------------------------------------------------------------------------


def _decidi(tipo, fase="execution", stato=None, **kw):
    kw.setdefault("ha_piano", True)
    kw.setdefault("passo_nel_turno", 3)
    kw.setdefault("verifica_rossa", False)
    return regia.decidi(tipo, fase, stato or regia.StatoPunto(), **kw)


def test_il_budget_segue_tipo_e_fase():
    assert _decidi("esegui").budget < _decidi("indaga").budget < _decidi("diagnosi").budget
    assert _decidi("diagnosi", "decision").budget > _decidi("diagnosi", "execution").budget
    # Senza tipo restano i tetti storici del watchdog.
    assert _decidi(None, "decision").budget == agent_mod.THINK_CEILING_FIRST
    assert _decidi(None, "execution").budget == agent_mod.THINK_CEILING_STEP


def test_i_fallimenti_nel_punto_lo_trasformano_in_diagnosi():
    stato = regia.StatoPunto()
    stato.nuovo_punto("1")
    stato.registra_passo([("run_command", False)])
    assert _decidi("esegui", stato=stato).tipo_effettivo == "esegui"
    stato.registra_passo([("edit_file", False)])
    d = _decidi("esegui", stato=stato)
    assert d.tipo == "esegui" and d.tipo_effettivo == "diagnosi"
    # Punto nuovo: si riparte da zero.
    stato.nuovo_punto("2")
    assert _decidi("esegui", stato=stato).tipo_effettivo == "esegui"


def test_una_verifica_rossa_in_recupero_e_una_diagnosi():
    d = _decidi("esegui", "recovery", verifica_rossa=True)
    assert d.tipo_effettivo == "diagnosi"


def test_senza_piano_il_tipo_viene_dal_passo_precedente():
    kw = {"ha_piano": False, "passo_nel_turno": 2}
    assert _decidi(None, ultimo_passo=[("read_file", True)], **kw).tipo_effettivo == "indaga"
    assert _decidi(None, ultimo_passo=[("edit_file", True)], **kw).tipo_effettivo == "esegui"
    assert _decidi(None, ultimo_passo=[("run_command", False)], **kw).tipo_effettivo == "diagnosi"
    primo = {"ha_piano": False, "passo_nel_turno": 1}
    assert _decidi(None, ultimo_passo=[("read_file", True)], **primo).tipo_effettivo is None


def test_dopo_molte_letture_un_indagine_riceve_il_budget_di_sintesi():
    stato = regia.StatoPunto()
    stato.nuovo_punto("1")
    for _ in range(regia.LETTURE_PER_SINTESI):
        stato.registra_passo([("read_file", True), ("manage_plan", True)])
    d = _decidi("indaga", stato=stato)
    assert d.sintesi and d.budget >= regia.BUDGET_SINTESI
    stato.registra_passo([("edit_file", True)])
    assert not _decidi("indaga", stato=stato).sintesi


def test_il_livello_scende_secondo_il_tipo():
    assert agent_mod.think_for_step("high", None, 1, "esegui") == "medium"
    assert agent_mod.think_for_step("high", None, 1, "diagnosi") == "high"
    assert agent_mod.think_for_step("high", None, 5, "diagnosi") == "medium"
    assert agent_mod.think_for_step("high", None, 5, "progetta") == "medium"
    # Senza tipo, come prima.
    assert agent_mod.think_for_step("high", None, 1) == "high"
    assert agent_mod.think_for_step("high", None, 4) == "low"
    assert agent_mod.think_for_step(True, None, 4, "esegui") is True


# ---------------------------------------------------------------------------
# Osservatore
# ---------------------------------------------------------------------------


def _nutri(oss, testo, passo=200):
    for fine in range(passo, len(testo) + passo, passo):
        ragione = oss.aggiorna(testo[:fine])
        if ragione:
            return ragione, fine
    return None, len(testo)


def test_l_osservatore_chiude_al_budget():
    oss = regia.OsservatorePensiero(3000)
    ragione, dove = _nutri(oss, "ragiono con calma. " * 400)
    assert ragione == "budget" and 3000 < dove <= 3200


def test_un_pensiero_che_oscilla_ha_meta_budget():
    frase = "maybe the cache. Wait, actually it could be the path. Hmm. "
    oss = regia.OsservatorePensiero(10000)
    ragione, dove = _nutri(oss, frase * 400)
    assert ragione == "oscillazione" and dove <= 5200
    assert oss.oscillazione and oss.ripensamenti >= regia.OSCILLAZIONE_MIN_RIPENSAMENTI


def test_una_decisione_scritta_chiude_dopo_la_grazia():
    testo = ("the user wants the parser fixed. " * 30
             + "So I'll call read_file on core/parser.py to see it. "
             + "then maybe check other things too. " * 60)
    oss = regia.OsservatorePensiero(50000, nomi_tool=["read_file", "edit_file"])
    ragione, dove = _nutri(oss, testo, passo=50)
    assert ragione == "decisione"
    assert dove - oss.decisione_a >= regia.GRAZIA_DECISIONE


def test_in_diagnosi_una_decisione_scritta_non_chiude():
    testo = "So I'll call run_command to run pytest. " + "then reason more about it. " * 200
    oss = regia.OsservatorePensiero(50000, tipo="diagnosi", nomi_tool=["run_command"])
    assert _nutri(oss, testo)[0] is None


def test_soglia_zero_non_chiude_mai():
    assert _nutri(regia.OsservatorePensiero(0), "wait " * 5000)[0] is None


# ---------------------------------------------------------------------------
# Continuazione contro il finto Ollama
# ---------------------------------------------------------------------------


def _pensiero_nativo(caratteri: int, pezzo: int = 400) -> list[dict]:
    testo = ("pianifico tutto con molta cura. " * (caratteri // 30 + 1))[:caratteri]
    return [{"message": {"thinking": testo[i:i + pezzo]}} for i in range(0, len(testo), pezzo)]


def _esegui(url, tmp_path, **kwargs):
    ui = [{"role": "user", "content": "ciao"}]
    backend = kwargs.pop("backend", None) or OllamaBackend(url, timeout_s=20)
    eventi = list(
        agent_mod.run_turn(
            backend=backend,
            params=GenParams(model="fake:latest", max_tokens=4096, num_ctx=32768),
            tools_schema=TOOLS_SCHEMA,
            tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
            ui_messages=ui,
            system_prompt="SYS",
            env_header=None,
            max_steps=kwargs.pop("max_steps", 2),
            **kwargs,
        )
    )
    return ui, eventi, backend


def test_il_pensiero_troppo_lungo_continua_invece_di_essere_buttato(fake_ollama, tmp_path, monkeypatch):
    url, handler = fake_ollama
    monkeypatch.setattr(fake, "SCRIPT", [
        _pensiero_nativo(20000),
        [{"message": {"content": "Ecco la risposta."}}],
    ])
    ui, _eventi, backend = _esegui(url, tmp_path)

    assert len(handler.calls) == 2
    continua = handler.calls[1]
    ultimo = continua["messages"][-1]
    assert ultimo["role"] == "assistant" and ultimo["content"] == ""
    assert ultimo["thinking"].endswith(regia.chiusura("budget"))
    assert continua["think"] is False
    # Il pensiero precompilato non porta il pensiero che ricomincia: e' quello
    # che il modello aveva gia' prodotto, fino alla soglia.
    assert ultimo["thinking"].startswith("pianifico tutto")

    assert not [m for m in ui if m.get("hidden") and "Ti ho interrotto" in str(m.get("content"))]
    assistente = [m for m in ui if m.get("role") == "assistant"][-1]
    assert "Ecco la risposta." in assistente["content"]
    traccia = assistente["think"]
    assert traccia["chiusura"] == "budget" and traccia["continuata"] is True
    assert backend.modo_continuazione() is False


def test_se_il_modello_ricomincia_a_pensare_si_torna_al_watchdog(fake_ollama, tmp_path, monkeypatch):
    url, _handler = fake_ollama
    monkeypatch.setattr(fake, "SCRIPT", [
        _pensiero_nativo(20000),
        _pensiero_nativo(5000),            # la continuazione non rispettata
        [{"message": {"content": "fine"}}],
    ])
    ui, eventi, backend = _esegui(url, tmp_path, max_steps=3)
    assert any(m.get("hidden") and "Ti ho interrotto" in str(m.get("content")) for m in ui)
    finito = [e for e in eventi if isinstance(e, agent_mod.TurnFinished)][-1]
    assert finito.usage["nudges"]["continuazione_rifiutata"] == 1
    # Il primo modo e' stato scartato, resta l'altro.
    assert backend.modo_continuazione() is True


def test_il_pensiero_nei_tag_di_testo_non_si_continua(fake_ollama, tmp_path, monkeypatch):
    """Un <think> aperto nel contenuto non si chiude precompilando ``thinking``."""
    url, handler = fake_ollama
    monkeypatch.setattr(fake, "SCRIPT", [
        [{"message": {"content": "<think>" + ("pianifico tutto. " * 1500)}}],
        [{"message": {"content": "fine"}}],
    ])
    ui, _, _ = _esegui(url, tmp_path)
    assert all(not m["messages"][-1].get("thinking") for m in handler.calls)
    assert any(m.get("hidden") and "Ti ho interrotto" in str(m.get("content")) for m in ui)
