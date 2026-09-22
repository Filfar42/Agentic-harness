"""Guardia di sintassi, finestra dopo la modifica, ritorni dei file, slot di servizio.

Le innovazioni del 22/09/2026 per i modelli piccoli (vedi
``docs/audit/AUDIT_2026-09-22.md``, sezione 6).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import agent as agent_mod
from core import sintassi
from core.backend import LlamaCppBackend, StreamEvent
from core.config import GenParams
from core.telemetry import TrackedBackend
from core.tools import TOOLS_SCHEMA, ToolContext, dispatch, finestra_modifica


# ---------------------------------------------------------------------------
# Guardia di sintassi
# ---------------------------------------------------------------------------


def test_una_modifica_che_rompe_python_viene_rifiutata():
    insistite: set[str] = set()
    v = sintassi.valuta("m.py", "def f():\n    return 1\n", "def f(:\n    return 1\n", insistite)
    assert v.rifiuta and v.errore.riga == 1


def test_la_stessa_scrittura_ripetuta_passa_con_avviso():
    """L'uscita per una sintassi piu' nuova del parser dell'harness."""
    insistite: set[str] = set()
    prima, dopo = "x = 1\n", "x = (\n"
    assert sintassi.valuta("m.py", prima, dopo, insistite).rifiuta
    v = sintassi.valuta("m.py", prima, dopo, insistite)
    assert not v.rifiuta and v.errore is not None


def test_file_nuovo_o_gia_rotto_si_scrive_con_avviso():
    assert not sintassi.valuta("n.py", None, "x = (\n", set()).rifiuta
    v = sintassi.valuta("r.py", "x = (\n", "y = (\n", set())
    assert not v.rifiuta and v.errore is not None


@pytest.mark.parametrize(("percorso", "prima", "dopo", "rifiuta"), [
    ("a.json", '{"a": 1}', '{"a": 1,}', True),
    ("tsconfig.json", '{"a": 1}', '{"a": 1, // commento\n}', False),
    (".vscode/settings.json", '{"a": 1}', '{"a": 1,}', False),
    ("pyproject.toml", 'a = 1\n', 'a = \n', True),
    ("note.md", "# titolo", "# titolo (", False),
    ("m.py", "x = 1\n", "x = '\\d'\n", False),     # SyntaxWarning, non errore
])
def test_formati(percorso, prima, dopo, rifiuta):
    assert sintassi.valuta(percorso, prima, dopo, set()).rifiuta is rifiuta


def test_edit_file_rifiutato_lascia_il_disco_com_era(tmp_path):
    f = tmp_path / "m.py"
    f.write_text("def g():\n    return 2\n", encoding="utf-8")
    ctx = ToolContext(workspace=str(tmp_path), sandbox="host")
    dispatch(ctx, "read_file", {"filepath": "m.py"})
    esito = json.loads(dispatch(ctx, "edit_file", {
        "filepath": "m.py", "old_string": "    return 2", "new_string": "  return 2\n    x"}))
    assert esito["error_code"] == "syntax_guard"
    assert esito["sintassi"]["contesto"]
    assert f.read_text(encoding="utf-8") == "def g():\n    return 2\n"


def test_write_file_su_un_file_sano_rifiutato(tmp_path):
    (tmp_path / "c.json").write_text('{"a": 1}', encoding="utf-8")
    ctx = ToolContext(workspace=str(tmp_path), sandbox="host")
    dispatch(ctx, "read_file", {"filepath": "c.json"})
    esito = json.loads(dispatch(ctx, "write_file", {"filepath": "c.json", "content": '{"a": }'}))
    assert esito["error_code"] == "syntax_guard"


def test_write_file_nuovo_rotto_avvisa(tmp_path):
    ctx = ToolContext(workspace=str(tmp_path), sandbox="host")
    esito = json.loads(dispatch(ctx, "write_file", {"filepath": "n.py", "content": "def f(:\n"}))
    assert esito["status"] == "ok" and esito["avviso_sintassi"]["riga"] == 1


# ---------------------------------------------------------------------------
# Finestra dopo la modifica
# ---------------------------------------------------------------------------


def test_edit_file_mostra_le_righe_modificate(tmp_path):
    (tmp_path / "m.py").write_text("a = 1\nb = 2\nc = 3\nd = 4\ne = 5\n", encoding="utf-8")
    ctx = ToolContext(workspace=str(tmp_path), sandbox="host")
    dispatch(ctx, "read_file", {"filepath": "m.py"})
    esito = json.loads(dispatch(ctx, "edit_file", {
        "filepath": "m.py", "old_string": "c = 3", "new_string": "c = 30\nc2 = 31"}))
    finestra = esito["dopo_la_modifica"]
    assert finestra["righe"] == "1-6"      # file di 6 righe: la finestra non va oltre

    assert "3 | c = 30" in finestra["testo"] and "4 | c2 = 31" in finestra["testo"]


def test_la_finestra_ha_un_tetto():
    nuovo = "\n".join(f"r{i}" for i in range(200))
    sostituto = "\n".join(f"r{i}" for i in range(10, 150))
    finestra = finestra_modifica(nuovo, nuovo.index("r10"), sostituto, 1)
    assert len(finestra["testo"].splitlines()) == 30 and "nota" in finestra


# ---------------------------------------------------------------------------
# Output vuoto esplicito
# ---------------------------------------------------------------------------


def test_un_comando_muto_lo_dice(tmp_path):
    ctx = ToolContext(workspace=str(tmp_path), sandbox="host")
    esito = json.loads(dispatch(ctx, "run_command", {"command": f'"{sys.executable}" -c "pass"'}))
    assert esito["esito"] == "ok" and esito["output"].startswith("nessuno")


# ---------------------------------------------------------------------------
# Ritorni dei file: la modifica fatta e disfatta
# ---------------------------------------------------------------------------


def _scrittura(path: str, sha: str) -> str:
    return json.dumps({"status": "ok", "filepath": path, "sha256": sha})


def test_ritorni_contati():
    r = agent_mod.RitorniDeiFile()
    assert r.registra(_scrittura("m.py", "B")) == ("m.py", 0)
    assert r.registra(_scrittura("m.py", "A")) == ("m.py", 0)
    assert r.registra(_scrittura("m.py", "B")) == ("m.py", 1)
    assert r.registra(_scrittura("m.py", "A")) == ("m.py", 2)
    assert r.registra(_scrittura("x.py", "A")) == ("x.py", 0)
    assert r.registra('{"error": "no"}') is None


class _PingPong:
    """Il modello che alterna due versioni della stessa riga."""

    def __init__(self) -> None:
        self.passo = 0

    def stream(self, messages, tools, params):
        self.passo += 1
        if self.passo == 1:
            chiamata = ("read_file", {"filepath": "m.py"})
        elif self.passo <= 6:
            vecchio, nuovo = ("x = 1", "x = 2") if self.passo % 2 == 0 else ("x = 2", "x = 1")
            chiamata = ("edit_file", {"filepath": "m.py", "old_string": vecchio, "new_string": nuovo})
        else:
            yield StreamEvent("content", text="fine")
            return
        yield StreamEvent("tool_call", tool_call={
            "id": f"c{self.passo}", "name": chiamata[0], "arguments": json.dumps(chiamata[1])})
        yield StreamEvent("usage", usage={"done_reason": "stop"})


def test_il_ping_pong_sulle_modifiche_riceve_un_sollecito(tmp_path):
    (tmp_path / "m.py").write_text("x = 1\n", encoding="utf-8")
    schema = [t for t in TOOLS_SCHEMA if t["function"]["name"] in ("read_file", "edit_file")]
    ui = [{"role": "user", "content": "sistema m.py"}]
    list(agent_mod.run_turn(
        backend=_PingPong(), params=GenParams(model="f", num_ctx=32_768),
        tools_schema=schema, tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
        ui_messages=ui, system_prompt="S", env_header="E", max_steps=8,
        require_summary=False, require_plan=False,
    ))
    solleciti = [m for m in ui if m.get("hidden") and "facendo e disfacendo" in m["content"]]
    assert len(solleciti) == 1


# ---------------------------------------------------------------------------
# Slot di servizio su llama-server
# ---------------------------------------------------------------------------


def _llama(totali: int | None, servizio: int) -> LlamaCppBackend:
    be = LlamaCppBackend("http://127.0.0.1:9")
    be._props = {"total_slots": totali} if totali is not None else {"n_ctx": 1}
    be._props_at = time.monotonic()
    be.slot_servizio = servizio
    return be


def test_slot_per_scopo():
    be = _llama(2, 1)
    assert be.slot_per("main") == 0
    assert be.slot_per("compaction") == 1
    assert _llama(2, 0).slot_per("main") == 1


@pytest.mark.parametrize(("totali", "servizio"), [(1, 1), (None, 1), (2, -1), (2, 5)])
def test_slot_spento_se_il_server_non_li_ha(totali, servizio):
    assert _llama(totali, servizio).slot_per("compaction") is None


def test_il_payload_porta_id_slot():
    be = _llama(2, 1)
    assert be._payload([{"role": "user", "content": "x"}], None, GenParams(model="m"), 1)["id_slot"] == 1
    assert "id_slot" not in be._payload([{"role": "user", "content": "x"}], None, GenParams(model="m"))


class _RegistraKwargs:
    supports_id_slot = True

    def __init__(self) -> None:
        self.visti: list[dict] = []

    def slot_per(self, scopo: str) -> int | None:
        return 0 if scopo == "main" else 1

    def stream(self, messages, tools, params, **kwargs):
        self.visti.append(kwargs)
        yield StreamEvent("content", text="ok")


def test_la_telemetria_passa_lo_slot_secondo_lo_scopo():
    grezzo = _RegistraKwargs()
    be = TrackedBackend(grezzo)
    list(be.stream([], None, GenParams(model="m")))
    list(be.scope("compaction").stream([], None, GenParams(model="m")))
    assert [k["id_slot"] for k in grezzo.visti] == [0, 1]
