"""A/B con il modello vero: stessa prova, due versioni dell'harness.

Quello che il banco simulato non puo' dire. Il banco (``banco_simulato.py``)
usa un modello finto che obbedisce ai solleciti con una probabilita' fissa: e'
onesto sul *meccanismo* (quale rete scatta, quanti passi costa, se il turno si
chiude) ma non sa niente di come un modello vero reagisce al prompt a moduli
(A8) o alla frase di chiusura del pensiero di Qwen3 (A9). Questo script rifa'
gli stessi compiti -- stessi file, stessa richiesta, stesso criterio di
riuscita sul disco -- con il tuo llama-server (o Ollama), su due versioni.

Uso, dal ramo nuovo, con il server del modello acceso::

    git worktree add ../astra-master master
    python scripts/ab_reale.py --radice ../astra-master --json ab_prima.json \\
        --api-base http://127.0.0.1:8080 --transport llamacpp --modello qwen3.8-27b
    python scripts/ab_reale.py --radice . --json ab_dopo.json \\
        --api-base http://127.0.0.1:8080 --transport llamacpp --modello qwen3.8-27b
    python scripts/banco_simulato.py --confronta ab_prima.json ab_dopo.json

Ogni corsa e' un turno vero (minuti su un 27B): con ``--ripetizioni 3`` e gli
otto compiti sono 24 turni per versione. I compiti S3 e S7 sono costruiti per
essere difficili o impossibili: li' conta *come* il turno si chiude, non se
riesce. Il pensiero segue ``--pensiero`` (low/medium/high/no).
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))


def esegui(h: dict[str, Any], sc: Any, backend: Any, params: Any, pensiero: bool) -> dict[str, Any]:
    agent, tools, prompts = h["agent"], h["tools"], h["prompts"]
    ws = Path(tempfile.mkdtemp(prefix="ab_"))
    inizio = time.monotonic()
    try:
        for nome, corpo in sc.file.items():
            (ws / nome).write_text(corpo, encoding="utf-8")
        schema = list(getattr(tools, "TOOLS_SCHEMA_LEAN", tools.TOOLS_SCHEMA))
        system = prompts.pick_system_prompt(thinking=pensiero)
        if hasattr(prompts, "build_system_prompt"):
            system = prompts.build_system_prompt(system, native_think=pensiero)
        env = prompts.build_env_header(str(ws), tool_names=[t["function"]["name"] for t in schema],
                                       sandbox="host")
        ui: list[dict[str, Any]] = [{"role": "user", "content": sc.richiesta}]
        ctx = tools.ToolContext(workspace=str(ws), sandbox="host", timeout_s=120)
        motivi: list[str] = []
        passi = 0
        token = 0
        nudges: Counter = Counter()
        checkpoint = None
        from banco_simulato import _kwargs_accettati

        for t, messaggio in enumerate([sc.richiesta, *sc.turni]):
            if t > 0:
                ui.append({"role": "user", "content": messaggio})
                ctx = tools.ToolContext(workspace=str(ws), sandbox="host", timeout_s=120,
                                        plan=ctx.plan, notes=ctx.notes,
                                        known_files=set(ctx.known_files))
            max_passi = [sc.max_passi, *sc.max_passi_turni][t]
            kw = _kwargs_accettati(agent.run_turn, {
                "backend": backend, "params": params, "tools_schema": schema, "tool_ctx": ctx,
                "ui_messages": ui, "system_prompt": system, "env_header": env,
                "max_steps": max_passi, "libreria_attiva": False, "estratto_pensiero": False,
                "spec_delega": False, "plan_gate": False, "abilita_delega": False,
                "auto_preview": False, "checkpoint_precedente": checkpoint,
            })
            for ev in agent.run_turn(**kw):
                nome = type(ev).__name__
                if nome == "StepStarted":
                    passi += 1
                if nome == "TurnFinished":
                    motivi.append(ev.reason)
                    uso = ev.usage or {}
                    token += int(uso.get("prompt_tokens") or uso.get("prompt_eval_count") or 0)
                    nudges.update(uso.get("nudges") or {})
                    checkpoint = getattr(ev, "checkpoint", None)
        fallite = sum(1 for m in ui if m.get("role") == "tool" and m.get("ok") is False)
        chiamate = sum(len(m.get("tool_calls") or []) for m in ui if m.get("role") == "assistant")
        try:
            ok = bool(sc.riuscito(ws))
        except OSError:
            ok = False
        finale = next((m for m in reversed(ui) if m.get("role") == "assistant"
                       and not m.get("tool_calls")), None)
        json_finale = bool(finale and '"arguments"' in str(finale.get("content")))
        return {
            "riuscito": ok and not json_finale, "passi": passi, "chiamate_modello": passi,
            "chiamate_servizio": 0, "token_prompt": token, "chiamate_tool": chiamate,
            "tool_falliti": fallite, "solleciti": sum(nudges.values()),
            "solleciti_per_tipo": dict(nudges), "motivo": motivi[-1] if motivi else "?",
            "motivi": motivi, "json_come_risposta": json_finale,
            "secondi": round(time.monotonic() - inizio, 1),
        }
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--radice", type=Path, required=True)
    ap.add_argument("--api-base", default="http://127.0.0.1:8080")
    ap.add_argument("--transport", default="auto")
    ap.add_argument("--modello", default="")
    ap.add_argument("--num-ctx", type=int, default=32768)
    ap.add_argument("--max-tokens", type=int, default=8192)
    ap.add_argument("--pensiero", default="medium", choices=("no", "low", "medium", "high"))
    ap.add_argument("--ripetizioni", type=int, default=3)
    ap.add_argument("--solo", default="", help="nomi di compito separati da virgola")
    ap.add_argument("--json", type=Path, required=True)
    a = ap.parse_args(argv)

    from banco_simulato import carica_harness, prepara_ambiente, riassumi, scenari

    prepara_ambiente()
    h = carica_harness(a.radice.resolve())
    backend_mod, config = h["backend"], h["config"]
    backend = backend_mod.build_backend(transport=a.transport, base_url=a.api_base, api_key="",
                                        timeout_s=600.0, stream_tools="auto")
    think: Any = False if a.pensiero == "no" else a.pensiero
    params = config.GenParams(model=a.modello, num_ctx=a.num_ctx, max_tokens=a.max_tokens,
                              think=think, temperature=0.6, top_p=0.95, top_k=20)
    limite = getattr(backend, "clamp_num_ctx", None)
    if callable(limite):
        params.num_ctx = int(limite(params.num_ctx))
    solo = {s for s in a.solo.split(",") if s}
    rapporto: dict[str, Any] = {"radice": str(a.radice.resolve()), "modello": a.modello,
                                "pensiero": a.pensiero, "ripetizioni": a.ripetizioni,
                                "scenari": {}}
    for sc in scenari(0.75, 0.8):
        if solo and sc.nome not in solo:
            continue
        corse = [esegui(h, sc, backend, params, a.pensiero != "no") for _ in range(a.ripetizioni)]
        rapporto["scenari"][sc.nome] = {"guasto": sc.guasto, **riassumi(corse),
                                        "secondi_media": round(statistics.mean(
                                            c["secondi"] for c in corse), 1),
                                        "corse_dettaglio": corse}
        r = rapporto["scenari"][sc.nome]
        print(f"{sc.nome:26s} riusciti {r['riusciti_pct']:5.1f}%  passi {r['passi_media']:5.2f}  "
              f"falliti {r['tool_falliti_pct']:4.1f}%  motivi {r['motivi']}", flush=True)
    a.json.write_text(json.dumps(rapporto, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
