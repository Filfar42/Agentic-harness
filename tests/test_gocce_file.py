"""Le gocce dei file a fine ciclo: rotta di sistema e regola di derivazione.

La lista dei file toccati la ricava la UI dai risultati dei tool (vedi
``fileTocca`` in web/app.js): qui si controlla il lato server della
scorciatoia -- l'apertura del file manager -- e che la regola in JavaScript
esista in un solo posto, dato che deve valere sia in diretta sia alla
riapertura di una sessione salvata.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import config as config_mod  # noqa: E402

RADICE = Path(__file__).resolve().parents[1]
WEB = RADICE / "web"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Server con un workspace usa e getta. Niente finto Ollama: queste rotte
    non parlano con il modello."""
    monkeypatch.setattr(config_mod, "DATA_DIR", tmp_path / "sessions")
    monkeypatch.setattr(config_mod, "MEMORY_FILE", tmp_path / "memories.json")

    from core import memory as memory_mod
    from core import session as session_mod

    monkeypatch.setattr(session_mod, "DATA_DIR", tmp_path / "sessions")
    monkeypatch.setattr(memory_mod, "MEMORY_FILE", tmp_path / "memories.json")

    from server import main as server_main

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "hello.py").write_text("print('ciao')\n", encoding="utf-8")

    server_main.STATE = server_main.AppState()
    server_main.STATE.settings.update(
        {"workspace_dir": str(workspace), "docker_autostart": False}
    )
    with TestClient(server_main.app) as test_client:
        test_client.workspace = workspace
        test_client.server = server_main
        yield test_client


def test_apri_cartella_senza_corpo(client, monkeypatch):
    """Il pulsante storico del pannello chiama la rotta senza corpo JSON.

    Aggiungere un parametro non deve rompere chi non lo passa.
    """
    visti: list[list[str]] = []
    monkeypatch.setattr(client.server.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        client.server.subprocess, "run", lambda cmd, **_: visti.append(list(cmd))
    )

    risposta = client.post("/api/workspace/open")
    assert risposta.status_code == 200
    assert risposta.json()["revealed"] == ""
    assert visti and visti[0][-1] == str(client.workspace)


def test_apri_cartella_su_un_file(client, monkeypatch):
    """Con un percorso si apre la cartella che contiene il file."""
    visti: list[list[str]] = []
    monkeypatch.setattr(client.server.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        client.server.subprocess, "run", lambda cmd, **_: visti.append(list(cmd))
    )

    risposta = client.post("/api/workspace/open", json={"path": "hello.py"})
    assert risposta.status_code == 200
    assert risposta.json()["revealed"].endswith("hello.py")
    # Su Linux non esiste un "rivela il file" universale: si apre la cartella.
    assert visti[0][-1] == str(client.workspace)


def test_apri_cartella_su_windows_seleziona_il_file(client, monkeypatch):
    """Su Windows explorer vuole /select,PERCORSO come argomento unico."""
    visti: list[list[str]] = []
    monkeypatch.setattr(client.server.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        client.server.subprocess, "run", lambda cmd, **_: visti.append(list(cmd))
    )

    client.post("/api/workspace/open", json={"path": "hello.py"})
    assert visti[0][0] == "explorer"
    assert visti[0][1].startswith("/select,")
    assert visti[0][1].endswith("hello.py")


def test_file_sparito_ripiega_sulla_radice(client, monkeypatch):
    """Un file cancellato dopo il turno non deve dare errore all'utente."""
    monkeypatch.setattr(client.server.platform, "system", lambda: "Linux")
    monkeypatch.setattr(client.server.subprocess, "run", lambda *_a, **_k: None)

    risposta = client.post("/api/workspace/open", json={"path": "mai-esistito.py"})
    assert risposta.status_code == 200
    assert risposta.json()["revealed"] == ""


@pytest.mark.parametrize("fuga", ["../../etc/passwd", "/etc/passwd"])
def test_la_scorciatoia_non_esce_dal_workspace(client, fuga):
    """La stessa guardia dei tool, o la goccia diventa un modo per aprire
    qualsiasi cosa sul sistema."""
    risposta = client.post("/api/workspace/open", json={"path": fuga})
    assert risposta.status_code == 400


def test_regola_dei_file_in_un_solo_posto():
    """La derivazione dei file toccati vive solo nella UI.

    Se tornasse anche nel backend, le due copie divergerebbero al primo
    cambio di formato dei risultati di write_file.
    """
    app_js = (WEB / "app.js").read_text(encoding="utf-8")
    assert app_js.count("function fileTocca(") == 1
    agent_py = (RADICE / "core" / "agent.py").read_text(encoding="utf-8")
    assert "file_from_result" not in agent_py


def test_le_gocce_si_disegnano_su_entrambi_i_percorsi():
    """In diretta e alla riapertura: sono due percorsi di rendering diversi e
    la funzione e' vera solo se la chiamano entrambi."""
    app_js = (WEB / "app.js").read_text(encoding="utf-8")
    # streaming: tool_end alimenta, done chiude
    assert "turn.noteFile(event.name" in app_js
    assert re.search(r"reason !== 'awaiting_user'\) turn\.showFiles\(\)", app_js)
    # cronologia: il tool alimenta, la chiusura del turno disegna
    assert "t.noteFile(msg.name" in app_js
    assert "const closeTurn = () =>" in app_js


def test_le_gocce_hanno_nome_e_scorciatoia():
    """Icona + nome che apre l'anteprima, iconcina cartella che apre la
    cartella. Niente altro: la goccia dice *cosa*, non *dove*."""
    app_js = (WEB / "app.js").read_text(encoding="utf-8")
    blocco = app_js[app_js.index("function fileChips("):]
    blocco = blocco[: blocco.index("\nfunction ")]
    assert "chip-icon" in blocco and "chip-label" in blocco
    assert "openPreviewFile(file.path)" in blocco
    assert "'/api/workspace/open'" in blocco
    assert "FOLDER_ICON" in blocco
    # Il percorso NON e' scritto sulla goccia: l'iconcina della cartella e' una
    # scorciatoia, non un'etichetta. Era stampato per distinguere due file
    # omonimi in src/ e tests/ -- un caso raro pagato ad ogni riga, che su un
    # refactoring da quindici file faceva dell'elenco una colonna di percorsi.
    assert "chip-dir-name" not in blocco
    # ma dove sta il file resta a portata di puntatore
    assert "chip.title = `${file.path}" in blocco

    css = (WEB / "style.css").read_text(encoding="utf-8")
    for regola in (".file-chips", ".file-chips .chip", ".file-chips .chip-dir"):
        assert regola in css
    assert ".chip-dir-name" not in css, "regola rimasta senza il suo elemento"
