"""Test della verifica d'ambiente e dei lavori di preparazione.

Il problema di partenza: la schermata iniziale diceva "Workspace pronto" appena
la pagina si apriva. Era una speranza, non una verifica -- Ollama poteva essere
spento, Docker non avviato, l'immagine mai costruita -- e a scoprirlo era il
primo turno dell'utente, sotto forma di errore dentro una tendina a meta'
lavoro.

Qui si prova che la scritta dipende da misure vere, e che le due attese lunghe
(accendere Docker, costruire l'immagine) non bloccano mai una richiesta HTTP.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tests.test_agent_loop as fake  # noqa: E402
from core import config as config_mod  # noqa: E402
from core import sandbox as sandbox_mod  # noqa: E402
from server import prep as prep_mod  # noqa: E402

fake_ollama = fake.fake_ollama


@pytest.fixture()
def client(fake_ollama, tmp_path, monkeypatch):
    url, _ = fake_ollama
    monkeypatch.setattr(config_mod, "DATA_DIR", tmp_path / "sessions")
    monkeypatch.setattr(config_mod, "MEMORY_FILE", tmp_path / "memories.json")

    from core import memory as memory_mod
    from core import session as session_mod

    monkeypatch.setattr(session_mod, "DATA_DIR", tmp_path / "sessions")
    monkeypatch.setattr(memory_mod, "MEMORY_FILE", tmp_path / "memories.json")

    from server import main as server_main

    ws = tmp_path / "ws"
    ws.mkdir()

    server_main.STATE = server_main.AppState()
    server_main.PREP = prep_mod.Prep()
    server_main.STATE.settings.update(
        {
            "api_base": url,
            "transport": "ollama",
            "model_name": "fake:latest",
            "workspace_dir": str(ws),
            "timeout_seconds": 20,
            "docker_autostart": False,
            "image_autobuild": False,
        }
    )
    with TestClient(server_main.app) as c:
        c.server = server_main
        c.ws = ws
        yield c


def check(data, id_):
    return next(c for c in data["checks"] if c["id"] == id_)


# ---------------------------------------------------------------------------
# Le verifiche
# ---------------------------------------------------------------------------


def test_con_ollama_su_e_il_modello_giusto_il_controllo_passa(client):
    dati = client.get("/api/readiness").json()
    assert check(dati, "ollama")["state"] == "ok"


def test_un_endpoint_spento_e_un_errore_non_un_avviso(client):
    """Senza modello non si lavora: e' esattamente cio' che 'error' significa."""
    client.server.STATE.settings["api_base"] = "http://127.0.0.1:1"
    client.server.STATE.forget_backend()
    dati = client.get("/api/readiness").json()
    assert check(dati, "ollama")["state"] == "error"
    assert dati["ready"] is False
    assert check(dati, "ollama")["action"] == "settings"


def test_un_modello_non_installato_viene_detto_per_nome(client):
    """Il caso vero: endpoint cambiato, modello vecchio ancora selezionato."""
    client.server.STATE.settings["model_name"] = "inesistente:70b"
    dati = client.get("/api/readiness").json()
    controllo = check(dati, "ollama")
    assert controllo["state"] == "error"
    assert "inesistente:70b" in controllo["detail"]


def test_una_cartella_di_lavoro_sparita_e_un_errore(client, tmp_path):
    client.server.STATE.settings["workspace_dir"] = str(tmp_path / "mai-esistita")
    dati = client.get("/api/readiness").json()
    assert check(dati, "workspace")["state"] == "error"
    assert dati["ready"] is False


def test_senza_docker_l_ambiente_non_e_pronto(client, monkeypatch):
    monkeypatch.setattr(
        sandbox_mod, "docker_available", lambda: (False, "Il demone non risponde.")
    )
    dati = client.get("/api/readiness").json()
    assert check(dati, "docker")["state"] == "error"
    assert check(dati, "docker")["action"] == "docker"
    assert dati["ready"] is False


def test_mentre_docker_si_avvia_e_un_avviso_non_un_errore(client, monkeypatch):
    """Un avvio in corso non e' un guasto: dirlo rosso farebbe premere di
    nuovo il pulsante a chi sta gia' aspettando."""
    monkeypatch.setattr(sandbox_mod, "docker_available", lambda: (False, "non ancora"))
    client.server.PREP.docker._set(prep_mod.RUNNING)
    dati = client.get("/api/readiness").json()
    assert check(dati, "docker")["state"] == "warn"
    assert dati["ready"] is True


def test_l_immagine_mancante_e_un_avviso_perche_si_lavora_lo_stesso(client, monkeypatch):
    """Tre stati e non due: senza immagine i comandi girano, solo senza pytest
    ne' git. Schiacciarlo su 'errore' renderebbe la spia rossa cosi' spesso da
    non essere piu' presa sul serio."""
    monkeypatch.setattr(sandbox_mod, "docker_available", lambda: (True, "Engine 27"))
    monkeypatch.setattr(sandbox_mod, "image_exists", lambda _tag: False)
    dati = client.get("/api/readiness").json()
    assert check(dati, "image")["state"] == "warn"
    assert check(dati, "image")["action"] == "image"
    assert dati["ready"] is True


def test_l_immagine_costruita_ma_non_selezionata_viene_segnalata(client, monkeypatch):
    """Il peggio dei due mondi: si paga la build e i comandi continuano a
    girare sull'immagine di serie."""
    monkeypatch.setattr(sandbox_mod, "docker_available", lambda: (True, "Engine 27"))
    monkeypatch.setattr(sandbox_mod, "image_exists", lambda _tag: True)
    client.server.STATE.settings["docker_image"] = "python:3.12-slim"
    controllo = check(client.get("/api/readiness").json(), "image")
    assert controllo["state"] == "warn"
    assert "non e' quella in uso" in controllo["detail"]


def test_tutto_a_posto_significa_pronto(client, monkeypatch):
    monkeypatch.setattr(sandbox_mod, "docker_available", lambda: (True, "Engine 27"))
    monkeypatch.setattr(sandbox_mod, "image_exists", lambda _tag: True)
    tag = sandbox_mod.image_tag(client.ws)
    client.server.STATE.settings["docker_image"] = tag
    dati = client.get("/api/readiness").json()
    assert dati["ready"] is True
    assert all(c["state"] == "ok" for c in dati["checks"])


def test_con_la_sandbox_su_host_docker_non_viene_preteso(client):
    """Non e' un guasto, e' una scelta: ma va detto, perche' l'agente vede
    tutto il disco."""
    client.server.STATE.settings["sandbox"] = "host"
    dati = client.get("/api/readiness").json()
    assert check(dati, "docker")["state"] == "warn"
    assert not any(c["id"] == "image" for c in dati["checks"])
    assert dati["ready"] is True


# ---------------------------------------------------------------------------
# I lavori non bloccano
# ---------------------------------------------------------------------------


def test_avviare_docker_ritorna_subito(client, monkeypatch):
    """Docker Desktop ci mette 20-60 secondi: una POST che li aspettasse
    sarebbe una pagina bloccata su uno spinner."""
    def lento(**_kwargs):
        time.sleep(0.4)
        return True, "Engine 27"

    monkeypatch.setattr(sandbox_mod, "start_engine", lento)
    inizio = time.monotonic()
    risposta = client.post("/api/prep/docker").json()
    assert time.monotonic() - inizio < 0.3
    assert risposta["job"]["state"] == "running"


def test_il_lavoro_arriva_a_conclusione(client, monkeypatch):
    monkeypatch.setattr(sandbox_mod, "start_engine", lambda **_kw: (True, "Engine 27"))
    client.post("/api/prep/docker")
    for _ in range(50):
        stato = client.get("/api/readiness").json()["jobs"]["docker"]
        if stato["state"] != "running":
            break
        time.sleep(0.05)
    assert stato["state"] == "ok"


def test_un_lavoro_che_esplode_lo_racconta_invece_di_restare_appeso(client, monkeypatch):
    """Senza la cattura, il thread muore e l'interfaccia resta su 'in corso'
    per sempre."""
    def esplode(**_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(sandbox_mod, "start_engine", esplode)
    client.post("/api/prep/docker")
    for _ in range(50):
        stato = client.get("/api/readiness").json()["jobs"]["docker"]
        if stato["state"] != "running":
            break
        time.sleep(0.05)
    assert stato["state"] == "error"
    assert "boom" in stato["detail"]


def test_due_avvii_insieme_non_lanciano_due_thread(client, monkeypatch):
    partenze: list[int] = []

    def lento(**_kwargs):
        partenze.append(1)
        time.sleep(0.3)
        return True, ""

    monkeypatch.setattr(sandbox_mod, "start_engine", lento)
    client.post("/api/prep/docker")
    client.post("/api/prep/docker")
    time.sleep(0.5)
    assert len(partenze) == 1


def test_la_build_seleziona_l_immagine_che_ha_costruito(client, monkeypatch):
    """Costruirla e non usarla sarebbe il peggio dei due mondi."""
    tag = sandbox_mod.image_tag(client.ws)
    monkeypatch.setattr(sandbox_mod, "docker_available", lambda: (True, "Engine 27"))
    monkeypatch.setattr(sandbox_mod, "build_image", lambda _ws: (tag, "log della build"))
    client.post("/api/prep/image")
    for _ in range(50):
        stato = client.server.PREP.image.snapshot()
        if stato["state"] != "running":
            break
        time.sleep(0.05)
    assert stato["state"] == "ok"
    assert client.server.STATE.settings["docker_image"] == tag


def test_senza_docker_la_build_non_ci_prova_nemmeno(client, monkeypatch):
    monkeypatch.setattr(sandbox_mod, "docker_available", lambda: (False, "spento"))
    client.post("/api/prep/image")
    for _ in range(50):
        stato = client.server.PREP.image.snapshot()
        if stato["state"] != "running":
            break
        time.sleep(0.05)
    assert stato["state"] == "error"
    assert "Docker non e' pronto" in stato["detail"]


# ---------------------------------------------------------------------------
# Quando costruire da soli
# ---------------------------------------------------------------------------


def test_serve_una_build_se_l_immagine_non_c_e(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_mod, "docker_available", lambda: (True, ""))
    monkeypatch.setattr(sandbox_mod, "image_exists", lambda _tag: False)
    assert prep_mod.image_needed(str(tmp_path), sandbox_mod.DEFAULT_IMAGE) is True


def test_non_si_rifa_una_build_gia_fatta(tmp_path, monkeypatch):
    """Il motivo per cui esiste image_exists: senza, ogni cambio di cartella
    farebbe ripartire una build da minuti."""
    monkeypatch.setattr(sandbox_mod, "docker_available", lambda: (True, ""))
    monkeypatch.setattr(sandbox_mod, "image_exists", lambda _tag: True)
    assert prep_mod.image_needed(str(tmp_path), sandbox_mod.DEFAULT_IMAGE) is False


def test_un_immagine_scelta_a_mano_non_viene_scavalcata(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_mod, "docker_available", lambda: (True, ""))
    monkeypatch.setattr(sandbox_mod, "image_exists", lambda _tag: False)
    assert prep_mod.image_needed(str(tmp_path), "mia-immagine:custom") is False


def test_senza_docker_non_si_costruisce_niente(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_mod, "docker_available", lambda: (False, "spento"))
    assert prep_mod.image_needed(str(tmp_path), sandbox_mod.DEFAULT_IMAGE) is False


def test_scegliere_una_cartella_avvia_la_build(client, monkeypatch, tmp_path):
    """È il momento giusto: l'utente ha appena dichiarato su cosa lavorare, e
    la build parte mentre scrive il primo messaggio invece che dopo."""
    nuova = tmp_path / "progetto"
    nuova.mkdir()
    client.server.STATE.settings["image_autobuild"] = True
    monkeypatch.setattr(sandbox_mod, "docker_available", lambda: (True, "Engine 27"))
    monkeypatch.setattr(sandbox_mod, "image_exists", lambda _tag: False)
    monkeypatch.setattr(
        sandbox_mod, "build_image",
        lambda w: (sandbox_mod.image_tag(w), "log"),
    )
    risposta = client.post("/api/workspace", json={"path": str(nuova)}).json()
    assert risposta["jobs"]["image"]["state"] in {"running", "ok"}


def test_senza_l_automatismo_scegliere_una_cartella_non_costruisce(client, tmp_path):
    nuova = tmp_path / "progetto2"
    nuova.mkdir()
    client.server.STATE.settings["image_autobuild"] = False
    risposta = client.post("/api/workspace", json={"path": str(nuova)}).json()
    assert risposta["jobs"]["image"]["state"] == "idle"
