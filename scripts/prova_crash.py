"""Prova di crash: il processo muore a meta' di un effetto, poi si riparte.

Misura di A5 (referto del 25/09/2026). Un turno lancia un comando che scrive
un file e poi resta appeso; il processo viene ucciso con SIGKILL mentre il
comando e' in corso -- l'effetto e' gia' avvenuto, il risultato no. Poi si
ricarica la sessione dal disco, come farebbe il server al riavvio, e si
guarda cosa sa la conversazione di quello che e' successo.

Il salvataggio imita la politica del server della versione sotto prova:
prima del 25/09 il server salvava su ToolFinished (e pochi altri eventi);
dopo, salva anche su ToolStarted dei tool con effetti, quando l'``intento``
e' gia' in coda.

    python scripts/prova_crash.py --radice /percorso/versione [--json esito.json]
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

VITTIMA = r'''
import json, sys
from pathlib import Path
radice, dati, ws = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, radice)
from core import agent, session as session_mod
from core.backend import StreamEvent
from core.config import GenParams
from core.tools import TOOLS_SCHEMA, ToolContext
session_mod.DATA_DIR = Path(dati)
try:
    from core.ciclo import ripresa
    EFFETTI = ripresa.EFFETTI
except ImportError:
    EFFETTI = frozenset()
comando = "python3 -c \"import time; open('segno.txt','w').write('fatto'); time.sleep(120)\""
class Backend:
    def __init__(self):
        self.passi = iter([[StreamEvent("tool_call", tool_call={
            "id": "c1", "name": "run_command", "arguments": json.dumps({"command": comando})})]])
    def stream(self, messages, tools, params, **_):
        yield from next(self.passi, [StreamEvent("content", text="Fatto.")])
state = {"current_session_id": "crash1", "messages": [
    {"role": "user", "content": "crea segno.txt con un comando"}]}
session_mod.save_session(state, force=True)
for ev in agent.run_turn(backend=Backend(), params=GenParams(model="finto"),
        tools_schema=TOOLS_SCHEMA, tool_ctx=ToolContext(workspace=ws, sandbox="host"),
        ui_messages=state["messages"], system_prompt="S", env_header=None, max_steps=3,
        require_plan=False, require_summary=False, compact_history=False,
        auto_preview=False, estratto_pensiero=False, libreria_attiva=False):
    salva = isinstance(ev, agent.ToolFinished) or (
        isinstance(ev, agent.ToolStarted) and ev.name in EFFETTI)
    if salva:
        session_mod.save_session(state, force=True)
'''


def prova(radice: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as cartella:
        dati = Path(cartella) / "sessioni"
        ws = Path(cartella) / "ws"
        dati.mkdir()
        ws.mkdir()
        script = Path(cartella) / "vittima.py"
        script.write_text(VITTIMA, encoding="utf-8")
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        proc = subprocess.Popen([sys.executable, str(script), str(radice), str(dati), str(ws)],
                                env=env, start_new_session=True)
        scadenza = time.monotonic() + 60
        while not (ws / "segno.txt").exists() and time.monotonic() < scadenza:
            time.sleep(0.05)
        effetto = (ws / "segno.txt").exists()
        time.sleep(0.3)
        os.killpg(proc.pid, signal.SIGKILL)   # il processo e il comando figlio
        proc.wait(timeout=10)

        sys.path.insert(0, str(radice))
        from core import agent
        from core import session as session_mod

        session_mod.DATA_DIR = dati
        state: dict[str, Any] = {}
        caricata = session_mod.load_session(state, "crash1")
        messaggi = list(state.get("messages") or [])
        ruoli = [m.get("role") for m in messaggi]
        chiamata_su_disco = any(m.get("tool_calls") for m in messaggi)
        intento_su_disco = "intento" in ruoli
        riparate = 0
        esito: dict[str, Any] = {}
        try:
            from core.ciclo import ripresa

            riparate = ripresa.ripara_orfani(messaggi, str(ws))
            esiti = [m for m in messaggi if m.get("role") == "tool"]
            if esiti:
                esito = json.loads(esiti[-1]["content"])
        except ImportError:
            pass
        api = agent.build_api_messages(messaggi, system_prompt="S", env_header=None)
        chiamate = {c["id"] for m in api for c in (m.get("tool_calls") or [])}
        risultati = {m.get("tool_call_id") for m in api if m.get("role") == "tool"}
        return {
            "radice": str(radice),
            "effetto_avvenuto_su_disco": effetto,
            "sessione_ricaricata": caricata,
            "ruoli_dopo_il_crash": ruoli,
            "la_chiamata_e_in_cronologia": chiamata_su_disco,
            "intento_in_cronologia": intento_su_disco,
            "orfane_riparate": riparate,
            "esito_sintetico": esito,
            "cronologia_valida_per_il_template": chiamate <= risultati,
            "il_modello_sa_che_il_comando_e_partito": bool(esito.get("error_code")
                                                          == "esito_sconosciuto"),
        }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--radice", type=Path, default=Path(__file__).resolve().parents[1])
    ap.add_argument("--json", type=Path, default=None)
    a = ap.parse_args(argv)
    esito = prova(a.radice.resolve())
    print(json.dumps(esito, ensure_ascii=False, indent=1))
    if a.json:
        a.json.write_text(json.dumps(esito, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
