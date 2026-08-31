"""Il banco di prova `.analisi/` e cosa il file di sessione sa della run.

Il caso reale (esercizio `cantiere/`, qwen3.8:27b, 18/08/2026). Il prompt dice
al modello che `.analisi/` "viene svuotata" e non fa parte del progetto, ma
``reset_scratch`` girava **solo** sulle richieste di sola lettura: su una
richiesta di costruzione quella frase era falsa. Il modello ci ha creduto --
"which gets cleared on every analysis anyway", parole sue -- e ha lasciato
`.analisi/caso_negativo/` nella radice del workspace, violando l'unico vincolo
che l'utente aveva scritto sui confini del lavoro. Nessuno poteva accorgersene:
le cartelle col punto non compaiono in nessun elenco.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tests.test_agent_loop as fake
from tests.test_server import (  # noqa: F401
    client,
    current_session,
    fake_ollama,
    run_and_collect,
)


def semina(client) -> Path:
    """Uno scarto di un turno precedente nel banco di prova."""
    banco = client.workspace / ".analisi" / "vecchia_prova"
    banco.mkdir(parents=True)
    (banco / "bozza.py").write_text("print('scarto')\n", encoding="utf-8")
    return banco


def test_il_banco_si_svuota_anche_quando_si_costruisce(client):
    """Non solo davanti a una richiesta di analisi: e' la promessa che il
    prompt fa al modello, e vale in tutti e due i casi o non vale."""
    session_id = current_session(client)
    scarto = semina(client)

    run_and_collect(client, session_id, "scrivi il modulo di conteggio")

    assert not scarto.exists()
    assert not (client.workspace / ".analisi").exists()
    # e non ha portato via nient'altro
    assert (client.workspace / "hello.py").is_file()


def test_rispondendo_a_una_domanda_il_banco_non_sparisce(client):
    """Quel turno e' la continuazione del precedente: la prova che il modello
    aveva appena impostato e' esattamente cio' su cui deve tornare."""
    original = fake.SCRIPT
    fake.SCRIPT = [
        [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "ask_user_question",
                                "arguments": {
                                    "question": "Procedo?",
                                    "options": ["si", "no"],
                                },
                            }
                        }
                    ],
                }
            }
        ],
        [{"message": {"content": "Fatto: proseguito.\nPoi: niente."}}],
    ]
    try:
        session_id = current_session(client)
        run_and_collect(client, session_id, "sistema il modulo")
        scarto = semina(client)          # il modello lo prepara e poi chiede

        client.post("/api/answer", json={"session_id": session_id, "answer": "si"})
        client.get(f"/api/stream/{session_id}")

        assert scarto.exists(), "la ripresa ha buttato via la prova in corso"
    finally:
        fake.SCRIPT = original


def test_la_promessa_del_prompt_dice_quello_che_il_codice_fa():
    """Se qualcuno riporta reset_scratch al solo ramo di sola lettura, questa
    frase torna a essere una bugia detta al modello ad ogni richiesta."""
    from core.prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_LEAN

    for prompt in (SYSTEM_PROMPT, SYSTEM_PROMPT_LEAN):
        assert "ogni turno" in prompt
        assert "svuotata ad ogni analisi" not in prompt


def test_la_conversazione_salvata_dice_con_cosa_e_stata_fatta(client, tmp_path):
    """Erano due campi scritti da sempre e riempiti da nessuno: il modello
    restava "" e il workspace era la cwd del processo. Su una chat riletta
    domani -- o mandata a qualcuno per confrontare due modelli -- mancava
    proprio quello."""
    import json

    from core import config as config_mod

    session_id = current_session(client)
    client.server.STATE.settings["model_name"] = "qwen3.8:27b"
    run_and_collect(client, session_id, "conta i file")

    salvata = json.loads(
        (config_mod.DATA_DIR / f"{session_id}.json").read_text(encoding="utf-8")
    )
    assert salvata["model_name"] == "qwen3.8:27b"
    assert salvata["workspace_dir"] == str(client.workspace)
