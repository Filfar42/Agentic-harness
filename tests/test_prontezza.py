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

import tests.test_agent_loop as fake
from core import config as config_mod
from core import sandbox as sandbox_mod
from server import prep as prep_mod

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


def _finito(job, secondi: float = 5.0):
    """Aspetta che un lavoro di preparazione smetta di girare."""
    import time as _t

    scadenza = _t.time() + secondi
    while job.running and _t.time() < scadenza:
        _t.sleep(0.01)
    return job.snapshot()


def _docker_finto(monkeypatch, *, immagine_c_e: bool) -> dict:
    """Docker acceso, senza toccare la macchina."""
    fatti: dict = {"build": 0, "container": []}
    monkeypatch.setattr(sandbox_mod, "docker_available", lambda: (True, "Engine 27"))
    monkeypatch.setattr(sandbox_mod, "image_exists", lambda _tag: immagine_c_e)
    monkeypatch.setattr(sandbox_mod, "write_dockerfile", lambda _w: None)

    def _build(w):
        fatti["build"] += 1
        return sandbox_mod.image_tag(w), "log"

    def _container(w, **kw):
        fatti["container"].append((str(w), kw.get("image")))
        return "harness-fake"

    monkeypatch.setattr(sandbox_mod, "build_image", _build)
    monkeypatch.setattr(sandbox_mod, "ensure_container", _container)
    return fatti


def test_scegliere_una_cartella_prepara_l_ambiente(client, monkeypatch, tmp_path):
    """È il momento giusto: l'utente ha appena dichiarato su cosa lavorare, e
    immagine e container si preparano mentre scrive il primo messaggio.

    L'ordine non è quello in cui si nominano le due cose: il container si crea
    *dall'*immagine, quindi prima quella."""
    nuova = tmp_path / "progetto"
    nuova.mkdir()
    client.server.STATE.settings["image_autobuild"] = True
    fatti = _docker_finto(monkeypatch, immagine_c_e=False)

    risposta = client.post("/api/workspace", json={"path": str(nuova)}).json()
    # Il lavoro è già partito quando la risposta esce: `Job.start` marca lo
    # stato sul filo della richiesta e poi apre il thread. Coi finti qui sopra
    # può anche aver già finito -- quello che non deve mai essere è 'idle',
    # cioè "non ci ha nemmeno provato".
    assert risposta["jobs"]["container"]["state"] in {"running", "ok"}

    esito = _finito(client.server.PREP.container)
    assert esito["state"] == "ok", esito["detail"]
    assert fatti["build"] == 1
    # ...e il container nasce dall'immagine appena costruita, non da quella di
    # serie: costruirla e non usarla sarebbe il peggio dei due mondi.
    atteso = sandbox_mod.image_tag(str(nuova.resolve()))
    assert fatti["container"] == [(str(nuova.resolve()), atteso)]
    # Lo stato dell'immagine si legge dov'è sempre stato, anche se a costruirla
    # è stato il thread del container.
    assert client.server.PREP.image.snapshot()["state"] == "ok"


def test_l_immagine_che_c_e_gia_non_si_ricostruisce(client, monkeypatch, tmp_path):
    """Una build dura minuti: rifarla ad ogni cambio di cartella sarebbe il
    modo più caro di non fare niente."""
    nuova = tmp_path / "progetto3"
    nuova.mkdir()
    client.server.STATE.settings["image_autobuild"] = True
    fatti = _docker_finto(monkeypatch, immagine_c_e=True)

    client.post("/api/workspace", json={"path": str(nuova)})
    _finito(client.server.PREP.container)
    assert fatti["build"] == 0
    assert len(fatti["container"]) == 1, "il container si prepara lo stesso"


def test_senza_l_automatismo_scegliere_una_cartella_non_costruisce(
    client, monkeypatch, tmp_path
):
    """L'interruttore vale per l'immagine, non per il container: senza
    container l'agente non ha dove eseguire, e crearlo quando manca non
    scavalca nessuna scelta dell'utente."""
    nuova = tmp_path / "progetto2"
    nuova.mkdir()
    client.server.STATE.settings["image_autobuild"] = False
    fatti = _docker_finto(monkeypatch, immagine_c_e=False)

    client.post("/api/workspace", json={"path": str(nuova)})
    _finito(client.server.PREP.container)
    assert fatti["build"] == 0
    assert client.server.PREP.image.snapshot()["state"] == "idle"
    assert len(fatti["container"]) == 1


def test_cambiare_cartella_non_chiama_docker_sul_filo_della_richiesta(
    client, monkeypatch, tmp_path
):
    """Il costo che si voleva togliere: due sottoprocessi (`docker version` e
    `docker images`) dentro l'apertura di una conversazione, per scoprire
    quasi sempre che non c'era niente da fare.

    Qui si prova che la richiesta HTTP non li fa più: la decisione la prende il
    thread di preparazione."""
    import threading

    nuova = tmp_path / "progetto4"
    nuova.mkdir()
    client.server.STATE.settings["image_autobuild"] = True
    filo_http = threading.get_ident()
    visto: list[str] = []

    def _spia(nome, ritorno):
        def _f(*_a, **_kw):
            if threading.get_ident() == filo_http:
                visto.append(nome)
            return ritorno

        return _f

    monkeypatch.setattr(
        sandbox_mod, "docker_available", _spia("docker_available", (True, "x"))
    )
    monkeypatch.setattr(sandbox_mod, "image_exists", _spia("image_exists", True))
    monkeypatch.setattr(sandbox_mod, "ensure_container", lambda _w, **_kw: "c")

    client.post("/api/workspace", json={"path": str(nuova)})
    assert visto == [], f"chiamate a docker sul filo della richiesta: {visto}"
