"""Checkpoint, diario degli effetti e orfani attraverso il server (25/09).

Il ciclo produce l'istantanea e ripara le orfane; qui si controlla che il
server faccia la sua parte: salvare l'istantanea nella sessione (anche su
disco), ripararle prima del turno e riscrivere la coda, passare le impostazioni
nuove a ``run_turn``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_server import client, current_session, fake_ollama, run_and_collect  # noqa: F401


def test_il_checkpoint_del_turno_finisce_nella_sessione_e_su_disco(client):
    session_id = current_session(client)
    run_and_collect(client, session_id, "cosa c'e' nel progetto?")
    sessione = client.server.STATE.session(session_id)
    cp = sessione.get("checkpoint")
    assert isinstance(cp, dict) and cp.get("versione") == 1
    assert cp.get("motivo") == "completed"
    from core import session as session_mod

    su_disco = json.loads((session_mod.DATA_DIR / f"{session_id}.json").read_text(encoding="utf-8"))
    assert su_disco.get("checkpoint", {}).get("motivo") == "completed"


def test_una_chiamata_orfana_viene_riparata_prima_del_turno(client):
    session_id = current_session(client)
    run_and_collect(client, session_id, "cosa c'e' nel progetto?")
    messaggi = client.server.STATE.messages(session_id)
    # Il processo e' morto durante un write_file: c'e' la chiamata e il suo
    # intento, non c'e' il risultato.
    messaggi.append({"role": "assistant", "content": "", "tool_calls": [{
        "id": "orfana1", "type": "function",
        "function": {"name": "write_file",
                     "arguments": json.dumps({"filepath": "hello.py", "content": "x\n"})},
    }]})
    messaggi.append({"role": "intento", "tool_call_id": "orfana1", "name": "write_file",
                     "content": "", "filepath": "hello.py", "sha_prima": None})
    run_and_collect(client, session_id, "continua")
    esiti = [m for m in messaggi if m.get("role") == "tool" and m.get("tool_call_id") == "orfana1"]
    assert len(esiti) == 1
    corpo = json.loads(esiti[0]["content"])
    assert corpo["error_code"] == "esito_sconosciuto"
    # hello.py esisteva e sha_prima era None: il file "e' cambiato" rispetto
    # all'impronta registrata, quindi l'harness dice di rileggerlo.
    assert "read_file" in corpo["hint"]
    i_chiamata = next(i for i, m in enumerate(messaggi) if m.get("tool_calls")
                      and m["tool_calls"][0]["id"] == "orfana1")
    ruoli_dopo = [messaggi[i_chiamata + 1]["role"], messaggi[i_chiamata + 2]["role"]]
    assert ruoli_dopo == ["intento", "tool"], "l'esito sta accanto alla sua chiamata"


def test_le_impostazioni_nuove_hanno_i_loro_default(client):
    s = client.get("/api/bootstrap").json()["settings"]
    assert s["monitor_avanzamento"] is True
    assert s["compattazione_selettiva"] == "spenta"
    assert s["laya_url"].endswith("/v1/systemone")
    assert s["laya_modello"] == "multilingual"


def test_selezione_da_impostazioni():
    from server.main import _selezione_da_impostazioni

    assert _selezione_da_impostazioni({}) == ("spenta", None)
    assert _selezione_da_impostazioni({"compattazione_selettiva": "regole"}) == ("regole", None)
    modo, v = _selezione_da_impostazioni({"compattazione_selettiva": "laya",
                                          "laya_url": "http://127.0.0.1:8000/v1/systemone"})
    assert modo == "valutatore" and v.nome == "laya" and v.chiave == ""
    assert _selezione_da_impostazioni({"compattazione_selettiva": "boh"}) == ("spenta", None)
