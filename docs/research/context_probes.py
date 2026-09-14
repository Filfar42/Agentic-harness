"""Diagnostica del contesto; nessuna inferenza o modifica del runtime.

Eseguire dalla radice: .venv/Scripts/python.exe docs/research/context_probes.py
Le scritture della libreria avvengono solo in una directory temporanea.
I risultati descrivono il comportamento corrente, non il successo di un LLM.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core import libreria
from core.agent import _compact_tool_result, build_api_messages, compatta_cronologia
from core.backend import StreamEvent
from core.config import GenParams, budgets_for
from core.prompts import costi_del_prefisso
from core.textutils import estimate_tokens
from core.tools import TOOLS_SCHEMA_LEAN, VAULT_SEARCH_TOOL


def scrittura(i: int, path: str, body: str) -> list[dict]:
    return [
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": f"c{i}", "type": "function", "function": {
                "name": "write_file", "arguments": json.dumps({"filepath": path, "content": body})
            }
        }]},
        {"role": "tool", "tool_call_id": f"c{i}", "name": "write_file", "ok": True,
         "content": json.dumps({"status": "ok", "filepath": path, "action": "creato"})},
    ]


def cronologia(n: int) -> list[dict]:
    result = [{"role": "user", "content": "scrivi i moduli"}]
    for i in range(n):
        result += scrittura(i, f"mod_{i}.py", "x" * 4000)
    return result


class Recorder:
    def __init__(self):
        self.calls = []

    def stream(self, messages, tools, params):
        self.calls.append(messages)
        yield StreamEvent("content", text="FATTO:\n- moduli scritti")


def main() -> None:
    prefisso = costi_del_prefisso()
    schema = [t for t in TOOLS_SCHEMA_LEAN if t["function"]["name"] != VAULT_SEARCH_TOOL]
    out = {
        "nota": "Prove sintetiche senza LLM. I token sono stime, non conteggi del tokenizer.",
        "source_sha256": {
            p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
            for p in ("core/agent.py", "core/compaction.py", "core/libreria.py", "core/config.py")
        },
        "prefisso_stima_token": prefisso,
        "prompt_piu_schema_snello_senza_vault": prefisso["prompt_snello"] + estimate_tokens(
            json.dumps(schema, ensure_ascii=False)
        ),
    }
    raw = json.dumps({
        "command": "pytest -q", "returncode": 1, "stdout": "A" * 2000, "stderr": "B" * 2000,
        "stdout_deposito": ".deposito/001-log.txt", "stderr_deposito": ".deposito/002-err.txt",
        "deposito": ".deposito/003-search.txt",
    })
    compact = json.loads(_compact_tool_result(raw, full=False))
    out["handle_nel_risultato_vecchio"] = {
        k: k in compact for k in ("stdout_deposito", "stderr_deposito", "deposito")
    }
    raw = json.dumps({"filepath": "example.py", "content": "X" * 11000})
    full = _compact_tool_result(raw, full=True, budgets=budgets_for(16384))
    try:
        json.loads(full)
        valid = True
    except json.JSONDecodeError:
        valid = False
    out["read_recente"] = {
        "caratteri_input": len(raw), "caratteri_api": len(full), "json_valido": valid,
    }
    msgs = cronologia(10)
    msgs[1]["tool_calls"][0]["function"]["arguments"] = json.dumps({
        "filepath": "not_written.py", "content": "NEVER_SAVED" * 100,
    })
    msgs[2].update(ok=False, content=json.dumps({"error": "write denied"}))
    api = build_api_messages(msgs, system_prompt="", env_header=None, budgets=budgets_for(8192))
    args = next(m for m in api if m.get("tool_calls"))["tool_calls"][0]["function"]["arguments"]
    out["scrittura_fallita_potata_come_salvata"] = "omesso" in args and "disco" in args

    with tempfile.TemporaryDirectory(prefix="astra-context-audit-") as temp:
        base = Path(temp)
        msgs = cronologia(14)
        msgs[1]["tool_calls"][0]["function"]["arguments"] = json.dumps({
            "filepath": "OLD_UNIQUE_MARKER.py", "content": "x" * 4000,
        })
        be = Recorder()
        kwargs = dict(backend=be, params=GenParams(num_ctx=8192), budgets=budgets_for(8192),
                      strip_thinking=True, schedario=base)
        first = compatta_cronologia(msgs, **kwargs)
        for i in range(14, 28):
            msgs += scrittura(i, f"new_{i}.py", "x" * 4000)
        second = compatta_cronologia(msgs, **kwargs)
        out["compattazione_successiva"] = {
            "compattazioni_riuscite": sum(x is not None for x in (first, second)),
            "marcatore_vecchio_primo_input": "OLD_UNIQUE_MARKER" in be.calls[0][1]["content"],
            "marcatore_vecchio_secondo_input": "OLD_UNIQUE_MARKER" in be.calls[1][1]["content"],
            "caratteri_input": [len(c[1]["content"]) for c in be.calls],
        }
        voce = libreria.archivia(base,
            riassunto="SCOPERTO: il limite zephyr_unique_timeout va impostato a 240.",
            richieste=["Correggere il sistema"])
        out["recupero_su_parola_solo_nel_corpo"] = bool(
            libreria.precarico(base, [voce], "zephyr_unique_timeout")
        )
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
