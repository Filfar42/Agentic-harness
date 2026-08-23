"""Applicazioni con una finestra, e una shell, dentro il pannello di anteprima.

Il pannello sa mostrare una cosa sola: un iframe su una porta pubblicata.
Quindi il lavoro non e' stato insegnargli tkinter -- e' stato far parlare HTTP
a una finestra (Xvfb + x11vnc + noVNC) e a una shell (ttyd). Tutto il resto --
liberare la porta, aspettare che risponda, leggere il log, spegnere, raccogliere
gli orfani -- e' la strada che c'era gia'.

Provato per davvero in Chromium prima di essere scritto: tkinter cliccato
attraverso l'iframe, `echo` digitato nel terminale.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tests.test_sandbox as sbox  # noqa: E402
from core import sandbox  # noqa: E402
from core.tools import PREVIEW_TOOL, ToolContext, dispatch  # noqa: E402

fake_docker = sbox.fake_docker
workspace = sbox.workspace

WEB = Path(__file__).resolve().parents[1] / "web"


def ctx_docker(tmp_path, **kwargs) -> ToolContext:
    base = {"sandbox": "docker", "preview_ports": (8200, 8203)}
    base.update(kwargs)
    return ToolContext(workspace=str(tmp_path), timeout_s=5, **base)


def esito(raw: str) -> dict:
    return json.loads(raw)


# ---------------------------------------------------------------------------
# La catena dei processi
# ---------------------------------------------------------------------------


def test_la_catena_grafica_e_in_ordine():
    """Ogni anello ha bisogno del precedente: x11vnc su un display che non
    esiste ancora esce subito, e websockify senza x11vnc proxa nel vuoto."""
    cmd = sandbox.gui_command("python gioco.py", 8200)
    ordine = [cmd.index(p) for p in ("Xvfb", "x11vnc", "websockify", "DISPLAY=")]
    assert ordine == sorted(ordine)
    assert "python gioco.py" in cmd


def test_i_separatori_sono_punto_e_virgola():
    """La stessa trappola gia' pagata da start_background: in `A && B & C` la
    & mette in background l'intera catena, e quello che segue parte prima."""
    assert "&&" not in sandbox.gui_command("python app.py", 8200)


def test_la_porta_vnc_non_esce_dal_container():
    """Pubblicata sarebbe un desktop esposto senza password: fuori ci va solo
    noVNC, sulla porta che l'utente ha gia' concesso."""
    cmd = sandbox.gui_command("python app.py", 8200)
    assert f"-rfbport {sandbox.GUI_VNC_PORT}" in cmd and "-localhost" in cmd
    assert f"websockify --web={sandbox.NOVNC_DIR} 8200" in cmd
    assert str(sandbox.GUI_VNC_PORT) not in "8200 8203"


def test_il_terminale_e_scrivibile():
    """Senza -W si guarda e basta, che e' meta' del motivo per cui esiste."""
    assert "-W" in sandbox.terminal_command(8201)
    assert "-p 8201" in sandbox.terminal_command(8201)


def test_si_usa_la_pagina_ridotta_di_novnc():
    """`vnc.html` tiene le sue impostazioni in localStorage, e in un iframe con
    origine opaca localStorage solleva un'eccezione. `vnc_lite.html` no."""
    assert sandbox.NOVNC_PAGE.startswith("/vnc_lite.html")
    assert "autoconnect" in sandbox.NOVNC_PAGE


# ---------------------------------------------------------------------------
# Immagini vecchie
# ---------------------------------------------------------------------------


def test_l_immagine_dichiara_cosa_sa_fare(fake_docker, monkeypatch):
    monkeypatch.setenv("DOCKER_IMAGE_FEATURES", "gui,terminal")
    assert sandbox.image_features("qualsiasi-img") == {"gui", "terminal"}


def test_un_immagine_senza_etichetta_non_dichiara_niente(fake_docker):
    assert sandbox.image_features("qualsiasi-img") == set()


@pytest.mark.parametrize("azione", ["terminal", "gui"])
def test_su_un_immagine_vecchia_lo_dice_invece_di_provarci(
    fake_docker, tmp_path, azione
):
    """Il sintomo altrimenti sarebbe un 'command not found' dentro un log, cioe'
    il modo peggiore di scoprire che basta ricostruire l'immagine."""
    payload = esito(dispatch(
        ctx_docker(tmp_path),
        PREVIEW_TOOL,
        {"action": azione, "command": "python app.py"},
    ))
    assert "error" in payload
    assert "Costruisci l'immagine" in payload["hint"]
    assert "Dockerfile.sandbox" in payload["hint"]


# ---------------------------------------------------------------------------
# Le due azioni nuove
# ---------------------------------------------------------------------------


@pytest.fixture()
def porta_che_risponde(monkeypatch):
    """Il finto docker non sa simulare /proc/net/tcp: qui si finge la porta,
    libera prima dell'avvio e aperta dopo. Cosi' il test guarda quello che
    riguarda questa funzione -- quale comando parte e su quale porta -- e non
    la lettura degli stati, che ha i suoi test."""
    stati = iter([sandbox.PORT_FREE])

    def finto(*_a, **_k):
        return next(stati, sandbox.PORT_OPEN)

    monkeypatch.setattr(sandbox, "port_state", finto)


def test_il_terminale_parte_sulla_prima_porta_pubblicata(
    fake_docker, tmp_path, monkeypatch, porta_che_risponde
):
    """Senza 'port' non si chiede al modello di sceglierne una: la prima
    dell'intervallo va sempre bene, e una domanda in meno e' un errore in meno."""
    monkeypatch.setenv("DOCKER_IMAGE_FEATURES", "gui,terminal")
    payload = esito(dispatch(ctx_docker(tmp_path), PREVIEW_TOOL, {"action": "terminal"}))
    assert payload.get("status") == "ok", payload
    assert payload["preview"]["port"] == 8200
    assert payload["preview"]["mode"] == "terminal"
    avviati = [" ".join(c) for c in fake_docker() if c[0] == "exec"]
    assert any("ttyd -W -p 8200" in c for c in avviati), avviati


def test_lo_schermo_manda_in_pagina_novnc(
    fake_docker, tmp_path, monkeypatch, porta_che_risponde
):
    """L'utente deve ricevere l'indirizzo della pagina che disegna lo schermo,
    non la radice del server: websockify serve anche altro."""
    monkeypatch.setenv("DOCKER_IMAGE_FEATURES", "gui,terminal")
    payload = esito(dispatch(
        ctx_docker(tmp_path), PREVIEW_TOOL,
        {"action": "gui", "command": "python gioco.py"},
    ))
    assert payload.get("status") == "ok", payload
    assert payload["preview"]["url_path"] == sandbox.NOVNC_PAGE
    assert payload["preview"]["mode"] == "gui"
    assert "gioco.py" in payload["preview"]["title"]
    avviati = [" ".join(c) for c in fake_docker() if c[0] == "exec"]
    assert any("Xvfb" in c and "python gioco.py" in c for c in avviati)


def test_lo_schermo_vuole_sapere_cosa_mostrare(fake_docker, tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKER_IMAGE_FEATURES", "gui,terminal")
    payload = esito(dispatch(ctx_docker(tmp_path), PREVIEW_TOOL, {"action": "gui"}))
    assert "error" in payload and "command" in payload["error"]
    # e non chiede all'agente di sapere niente di X
    assert "DISPLAY" not in payload["hint"]


def test_le_azioni_ammesse_sono_dichiarate():
    payload = esito(dispatch(
        ToolContext(workspace="/tmp"), PREVIEW_TOOL, {"action": "inventata"}
    ))
    for azione in ("file", "serve", "terminal", "gui", "stop", "logs"):
        assert azione in payload["hint"]


@pytest.mark.parametrize("azione", ["terminal", "gui"])
def test_senza_docker_e_senza_porte_valgono_le_stesse_regole(tmp_path, azione):
    args = {"action": azione, "command": "python app.py"}
    host = esito(dispatch(
        ToolContext(workspace=str(tmp_path), sandbox="host", preview_ports=(8200, 8203)),
        PREVIEW_TOOL, args,
    ))
    assert "Docker" in host["error"]
    spente = esito(dispatch(
        ToolContext(workspace=str(tmp_path), sandbox="docker", preview_ports=None),
        PREVIEW_TOOL, args,
    ))
    assert "disattivate" in spente["error"]


# ---------------------------------------------------------------------------
# Immagine della sandbox
# ---------------------------------------------------------------------------


def test_il_dockerfile_verifica_invece_di_dichiarare():
    """Se una delle tre cose non c'e', a fallire deve essere la build -- adesso,
    con un messaggio -- non l'agente fra due giorni in mezzo a un turno."""
    t = sandbox.DOCKERFILE_TEMPLATE
    assert 'python -c "import tkinter"' in t
    assert "ttyd --version" in t
    assert "test -f /usr/share/novnc/vnc_lite.html" in t
    assert f'LABEL {sandbox.FEATURES_LABEL}="gui,terminal"' in t


def test_tkinter_si_ripara_con_le_librerie_non_col_pacchetto_python():
    """L'immagine slim compila _tkinter ma butta via libtk/libtcl (la riga che
    rimarca le librerie da tenere esclude apposta tkinter). python3-tk
    installerebbe tkinter per il python di Debian, che non e' quello
    dell'immagine."""
    t = sandbox.DOCKERFILE_TEMPLATE
    assert "libtk8.6" in t and "libtcl8.6" in t
    # nei commenti si puo' nominare: e' proprio li' che si spiega perche' no
    istruzioni = "\n".join(r for r in t.splitlines() if not r.lstrip().startswith("#"))
    assert "python3-tk" not in istruzioni


def test_ttyd_non_e_dato_per_scontato():
    """Se la distribuzione non lo pacchettizza si prende il binario ufficiale:
    la funzione non deve dipendere da cosa c'e' in un repository."""
    t = sandbox.DOCKERFILE_TEMPLATE
    assert "apt-get install -y --no-install-recommends ttyd" in t
    assert "releases/download" in t and "ttyd.x86_64" in t and "ttyd.aarch64" in t


# ---------------------------------------------------------------------------
# L'iframe
# ---------------------------------------------------------------------------


def test_allow_same_origin_solo_alle_applicazioni():
    """Misurato in Chromium: con l'origine opaca il CORS rifiuta i moduli ES di
    noVNC e la fetch di ttyd da 'origin: null'. Un'applicazione sta su
    127.0.0.1:82xx, un'origine diversa da quella dell'harness, quindi il
    permesso non le da' accesso ne' al nostro DOM ne' alle nostre risposte.
    Un'anteprima di FILE arriva invece da /api/preview/file, cioe' dalla nostra
    origine: li' quella combinazione annullerebbe il sandbox."""
    js = (WEB / "app.js").read_text(encoding="utf-8")
    blocco = js[js.index("if (payload.kind === 'app' || FRAME_EXT.includes(ext))"):]
    blocco = blocco[: blocco.index("return;")]
    assert "if (payload.kind === 'app') permessi.push('allow-same-origin')" in blocco
    # e nessun altro punto del client lo concede
    assert js.count("allow-same-origin'") == 1
