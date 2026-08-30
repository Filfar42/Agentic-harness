"""Test del pannello di anteprima: file del workspace e applicazioni avviate.

Due meta' con vincoli diversi:

* **i file** non toccano la sandbox. L'unica cosa che puo' andare storta e' che
  la rotta serva qualcosa fuori dal workspace, quindi e' quello che si prova
  per primo.
* **le applicazioni** richiedono porte pubblicate dal container e processi che
  sopravvivono al comando che li avvia. Qui i modi di fallire in silenzio sono
  tre, e sono tutti provati sotto: un container riusato con le porte vecchie,
  un server legato a 127.0.0.1 dentro il container (invisibile da fuori), e un
  run_command che uccide il server con il suo timeout.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tests.test_agent_loop as fake  # noqa: E402
import tests.test_sandbox as sbox  # noqa: E402
from core import agent as agent_mod  # noqa: E402
from core import config as config_mod  # noqa: E402
from core import sandbox  # noqa: E402
from core.prompts import build_env_header  # noqa: E402
from core.tools import (  # noqa: E402
    PREVIEW_TOOL,
    ToolContext,
    dispatch,
    looks_like_server,
    preview_backend,
    preview_kind,
    preview_root,
)
from server import previewhost  # noqa: E402

fake_ollama = fake.fake_ollama
fake_docker = sbox.fake_docker
workspace = sbox.workspace


# ---------------------------------------------------------------------------
# Che cosa e' mostrabile
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,atteso",
    [
        ("report.md", "render"),
        ("index.html", "render"),
        ("diagramma.svg", "render"),
        ("grafico.png", "render"),
        ("manuale.pdf", "render"),
        ("motore.py", "text"),
        ("dati.csv", "text"),
        ("archivio.zip", "none"),
        ("binario", "none"),
    ],
)
def test_riconoscimento_dei_tipi(path, atteso):
    assert preview_kind(path) == atteso


# ---------------------------------------------------------------------------
# Anteprima dedotta invece che dichiarata
# ---------------------------------------------------------------------------


def test_un_file_visuale_scritto_apre_l_anteprima_da_solo():
    """Chiedere al modello di annunciare ogni file costerebbe un round-trip a
    testa e verrebbe dimenticato: se l'informazione ce l'abbiamo, e' gratis."""
    payload = agent_mod.preview_from_result(
        "write_file", {"filepath": "report.md"}, '{"status": "ok"}', True
    )
    assert payload["kind"] == "render"
    assert payload["path"] == "report.md"
    assert payload["auto"] is True


def test_un_file_di_codice_scritto_non_apre_niente():
    """Dieci write_file su .py in un refactoring = dieci anteprime di rumore."""
    assert agent_mod.preview_from_result(
        "write_file", {"filepath": "motore.py"}, '{"status": "ok"}', True
    ) is agent_mod._NO_PREVIEW


def test_una_scrittura_fallita_non_apre_niente():
    assert agent_mod.preview_from_result(
        "write_file", {"filepath": "report.md"}, '{"error": "boom"}', False
    ) is agent_mod._NO_PREVIEW


def test_l_automatismo_si_puo_spegnere():
    assert agent_mod.preview_from_result(
        "write_file", {"filepath": "report.md"}, '{"status": "ok"}', True, auto=False
    ) is agent_mod._NO_PREVIEW


def test_il_tool_di_anteprima_puo_anche_chiudere_il_pannello():
    """None e "non c'entra niente" sono due risposte diverse.

    Con una sola sentinella, un action='stop' -- che deve *chiudere* il
    pannello -- verrebbe scambiato per una chiamata irrilevante e l'anteprima
    resterebbe li' a mostrare un'applicazione che non esiste piu'.
    """
    payload = agent_mod.preview_from_result(
        PREVIEW_TOOL, {"action": "stop"}, '{"status": "ok", "preview": null}', True
    )
    assert payload is None
    assert payload is not agent_mod._NO_PREVIEW


# ---------------------------------------------------------------------------
# Il tool, lato file
# ---------------------------------------------------------------------------


def test_mostrare_un_file_del_workspace(tmp_path):
    (tmp_path / "report.md").write_text("# titolo\n", encoding="utf-8")
    ctx = ToolContext(workspace=str(tmp_path), sandbox="host")
    payload = json.loads(dispatch(ctx, PREVIEW_TOOL, {"action": "file", "path": "report.md"}))
    assert payload["preview"] == {
        "kind": "render",
        "path": "report.md",
        # La cartella servita. Vuota = la radice del workspace: qui non c'e'
        # nessun marcatore di progetto e il file sta in cima.
        "root": "",
        "title": "report.md",
    }


def test_un_file_fuori_dal_workspace_viene_rifiutato(tmp_path):
    ctx = ToolContext(workspace=str(tmp_path), sandbox="host")
    risposta = dispatch(ctx, PREVIEW_TOOL, {"action": "file", "path": "../../etc/passwd"})
    assert "error" in json.loads(risposta)


def test_un_tipo_che_non_si_sa_mostrare_lo_dice(tmp_path):
    (tmp_path / "roba.zip").write_bytes(b"PK")
    ctx = ToolContext(workspace=str(tmp_path), sandbox="host")
    payload = json.loads(dispatch(ctx, PREVIEW_TOOL, {"action": "file", "path": "roba.zip"}))
    assert "error" in payload and "hint" in payload


# ---------------------------------------------------------------------------
# La rotta HTTP
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "DATA_DIR", tmp_path / "sessions")
    monkeypatch.setattr(config_mod, "MEMORY_FILE", tmp_path / "memories.json")
    from core import memory as memory_mod
    from core import session as session_mod

    monkeypatch.setattr(session_mod, "DATA_DIR", tmp_path / "sessions")
    monkeypatch.setattr(memory_mod, "MEMORY_FILE", tmp_path / "memories.json")

    from server import main as server_main

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "report.md").write_text("# ciao\n", encoding="utf-8")
    (ws / "pagina.html").write_text("<h1>ciao</h1>", encoding="utf-8")
    (tmp_path / "segreto.txt").write_text("non toccare", encoding="utf-8")

    server_main.STATE = server_main.AppState()
    server_main.STATE.settings.update({"workspace_dir": str(ws), "docker_autostart": False})
    with TestClient(server_main.app) as c:
        c.server = server_main
        c.ws = ws
        yield c


def test_la_rotta_serve_un_file_del_workspace(client):
    risposta = client.get("/api/preview/file", params={"path": "report.md"})
    assert risposta.status_code == 200
    assert "# ciao" in risposta.text


def test_l_html_arriva_come_html_ma_senza_indovinare(client):
    risposta = client.get("/api/preview/file", params={"path": "pagina.html"})
    assert risposta.headers["content-type"].startswith("text/html")
    # Il tipo dichiarato deve essere quello che vale: su file scritti dal
    # modello, lasciare che il browser tiri a indovinare e' proprio il caso in
    # cui non si vuole.
    assert risposta.headers["x-content-type-options"] == "nosniff"


def test_il_markdown_non_viene_scaricato_ma_letto(client):
    """Servito come text/markdown il browser lo scaricherebbe invece di
    mostrarlo: lo rende il client, con lo stesso markdown della chat."""
    risposta = client.get("/api/preview/file", params={"path": "report.md"})
    assert risposta.headers["content-type"].startswith("text/plain")


@pytest.mark.parametrize("path", ["../segreto.txt", "/etc/passwd", "../../etc/hosts"])
def test_la_rotta_non_esce_dal_workspace(client, path):
    """Stessa guardia dei tool (resolve_path), non una seconda copia della
    logica: due risposte diverse alla stessa domanda sono un buco in attesa."""
    assert client.get("/api/preview/file", params={"path": path}).status_code in (400, 404)


def test_un_file_inesistente_e_un_404(client):
    assert client.get("/api/preview/file", params={"path": "manca.md"}).status_code == 404


def test_l_anteprima_torna_riaprendo_la_conversazione(client):
    sid = client.get("/api/bootstrap").json()["session"]["session_id"]
    client.server.STATE.store_preview(sid, {"kind": "render", "path": "report.md"})
    client.server.STATE.save(sid)
    assert client.post(f"/api/sessions/{sid}/open").json()["preview"]["path"] == "report.md"


# ---------------------------------------------------------------------------
# Porte pubblicate
# ---------------------------------------------------------------------------


def test_le_porte_finiscono_nel_comando_docker(fake_docker, workspace):
    sandbox.ensure_container(workspace, ports=(8200, 8203))
    run = next(c for c in fake_docker() if c[0] == "run")
    assert "--publish" in run
    pubblicazione = run[run.index("--publish") + 1]
    # Su 127.0.0.1 e non 0.0.0.0: aprire una porta e' gia' una concessione,
    # e deve fermarsi a questa macchina invece di affacciarsi sulla rete.
    assert pubblicazione == "127.0.0.1:8200-8203:8200-8203"


def test_senza_porte_non_si_pubblica_niente(fake_docker, workspace):
    sandbox.ensure_container(workspace)
    run = next(c for c in fake_docker() if c[0] == "run")
    assert "--publish" not in run


def test_cambiare_intervallo_ricrea_il_container(fake_docker, workspace):
    """Il fallimento silenzioso che l'impronta chiude.

    Gli argomenti di ``docker run`` valgono solo alla creazione: senza
    controllo, cambiare le porte nelle impostazioni non avrebbe **nessun**
    effetto finche' quel container resta in piedi. L'utente vedrebbe la porta
    configurata e un'anteprima che non si connette, senza un errore da nessuna
    parte.
    """
    sandbox.ensure_container(workspace, ports=(8200, 8203))
    sandbox.ensure_container(workspace, ports=(9000, 9003))
    corse = [c for c in fake_docker() if c[0] == "run"]
    assert len(corse) == 2
    assert "127.0.0.1:9000-9003:9000-9003" in corse[1]


def test_gli_stessi_argomenti_riusano_il_container(fake_docker, workspace):
    """L'impronta non deve trasformare il riuso in un ricreo continuo."""
    for _ in range(3):
        sandbox.ensure_container(workspace, ports=(8200, 8203))
    assert sum(1 for c in fake_docker() if c[0] == "run") == 1


def test_run_propaga_le_porte(fake_docker, workspace):
    """Se un comando qualsiasi creasse il container senza porte, la prima
    anteprima lo troverebbe con l'impronta sbagliata e lo ricreerebbe,
    ammazzando quello che ci stava girando dentro."""
    sandbox.run("echo ciao", workspace, timeout_s=5, ports=(8200, 8203))
    run = next(c for c in fake_docker() if c[0] == "run")
    assert "127.0.0.1:8200-8203:8200-8203" in run


def test_le_porte_compaiono_nell_environment_header(tmp_path):
    header = build_env_header(str(tmp_path), sandbox="docker", preview_ports=(8200, 8203))
    assert "8200-8203" in header
    # Il modello sbaglia sempre questo, la prima volta.
    assert "0.0.0.0" in header


def test_senza_anteprime_l_header_non_ne_parla(tmp_path):
    assert "porte_per_anteprime" not in build_env_header(str(tmp_path), sandbox="docker")


# ---------------------------------------------------------------------------
# Applicazioni: i rifiuti utili
# ---------------------------------------------------------------------------


def test_una_porta_fuori_intervallo_viene_rifiutata_con_l_intervallo_giusto(tmp_path):
    ctx = ToolContext(
        workspace=str(tmp_path), sandbox="docker", preview_ports=(8200, 8203)
    )
    payload = json.loads(
        dispatch(ctx, PREVIEW_TOOL, {"action": "serve", "command": "x", "port": 9999})
    )
    assert "error" in payload
    assert "8200" in payload["hint"] and "8203" in payload["hint"]


def test_senza_porte_pubblicate_lo_dice_invece_di_provarci(tmp_path):
    ctx = ToolContext(workspace=str(tmp_path), sandbox="docker", preview_ports=None)
    payload = json.loads(
        dispatch(ctx, PREVIEW_TOOL, {"action": "serve", "command": "x", "port": 8200})
    )
    assert "disattivate" in payload["error"]


def test_sull_host_le_anteprime_di_applicazioni_non_esistono(tmp_path):
    ctx = ToolContext(workspace=str(tmp_path), sandbox="host", preview_ports=(8200, 8203))
    payload = json.loads(
        dispatch(ctx, PREVIEW_TOOL, {"action": "serve", "command": "x", "port": 8200})
    )
    assert "Docker" in payload["error"]


# ---------------------------------------------------------------------------
# run_command e i server
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "comando",
    [
        "python -m http.server 8200",
        "uvicorn app:app --host 0.0.0.0",
        "npm run dev",
        "streamlit run demo.py",
    ],
)
def test_i_comandi_che_non_finiscono_mai_si_riconoscono(comando):
    assert looks_like_server(comando) is True


@pytest.mark.parametrize("comando", ["pytest -q", "ruff check .", "git status", "ls"])
def test_un_comando_normale_non_e_un_server(comando):
    assert looks_like_server(comando) is False


@pytest.mark.usefixtures("fake_docker")
def test_un_server_ucciso_dal_timeout_manda_al_tool_giusto(workspace):
    """Il timeout su un processo che non termina per natura non e' un errore
    da riparare: e' il tool sbagliato. Senza questo suggerimento il modello
    spende tre passi ad "aggiustare" un server che funzionava."""
    ctx = ToolContext(workspace=str(workspace), sandbox="docker")
    payload = json.loads(
        dispatch(ctx, "run_command", {"command": "uvicorn app:app SCADI"})
    )
    assert "error" in payload
    assert "preview" in payload["hint"] and "serve" in payload["hint"]


@pytest.mark.usefixtures("fake_docker")
def test_un_comando_normale_scaduto_resta_un_timeout_normale(workspace):
    ctx = ToolContext(workspace=str(workspace), sandbox="docker")
    payload = json.loads(dispatch(ctx, "run_command", {"command": "pytest SCADI"}))
    assert "timeout" in payload["error"].lower()
    assert "preview" not in payload.get("hint", "")


# ---------------------------------------------------------------------------
# Regressioni della prima anteprima vera (sessione del 18/08/2026)
# ---------------------------------------------------------------------------
#
# Tre bug in catena, osservati dal vivo su qwen3.8. Nessuno dei tre era una
# svista del modello: il modello si e' comportato ragionevolmente in un
# ambiente che gli mentiva.


def test_il_mkdir_precede_l_avvio_in_background(fake_docker, workspace):
    """Il bug numero uno, e la ragione per cui i `;` non sono uno stile.

    Con `mkdir && cd && nohup ... & echo $! > pid` la `&` mette in background
    **l'intera catena**, quindi `echo $! > pid` girava prima che il mkdir
    avesse creato la cartella: "Directory nonexistent". E intanto il server, in
    background, partiva davvero -- lasciando un processo vivo, in ascolto sulla
    porta, senza pid registrato e quindi impossibile da fermare. Il tentativo
    dopo trovava la porta occupata da un orfano che nessuno poteva vedere.
    """
    sandbox.start_background("sleep 30", workspace, ports=(8200, 8203))
    avvio = next(c[-1] for c in fake_docker() if c[0] == "exec" and "nohup" in c[-1])

    prima_della_e_commerciale = avvio.split("&")[0]
    assert "mkdir -p" in prima_della_e_commerciale, avvio
    assert "&&" not in prima_della_e_commerciale, (
        "il mkdir non deve finire nella catena messa in background: " + avvio
    )
    # E il pid deve essere registrato dopo la &, cioe' quello del nohup.
    assert avvio.rstrip().endswith(".pid"), avvio


def test_un_server_ucciso_non_diventa_una_verifica_rossa():
    """Il ciclo osservato: l'harness ordinava di "riseguire ESATTAMENTE lo
    stesso comando" su un `python -m http.server` ucciso dal timeout. Il
    comando ripartiva, veniva ucciso di nuovo, e il turno girava a vuoto
    finche' l'utente non premeva stop.
    """
    tracker = agent_mod.VerificationTracker()
    tracker.record(
        "run_command",
        json.dumps({"command": "python -m http.server 8200 --bind 0.0.0.0",
                    "returncode": 1}),
    )
    assert tracker.unresolved is None


def test_una_verifica_vera_resta_rossa():
    """La stessa guardia non deve smontare il ciclo di self-correction."""
    tracker = agent_mod.VerificationTracker()
    tracker.record("run_command", json.dumps({"command": "pytest -q", "returncode": 1}))
    assert tracker.unresolved is not None
    assert tracker.unresolved[0] == "pytest -q"


def test_il_raccoglitore_di_orfani_resta_dentro_l_intervallo(tmp_path):
    """Uccidere per numero di porta e' grezzo: l'ambito stretto e' cio' che lo
    rende accettabile. Fuori dalle porte pubblicate non si tocca niente."""
    assert sandbox.kill_port_listener(tmp_path, 22, ports=(8200, 8203)) is False
    assert sandbox.kill_port_listener(tmp_path, 8200, ports=None) is False


# ---------------------------------------------------------------------------
# Una pagina e' una cartella, non un file (v2.35)
# ---------------------------------------------------------------------------
#
# Il difetto osservato dall'utente: "un html senza il css e senza il backend
# avviato non funziona come dovrebbe". Erano due cose diverse sotto la stessa
# frase, e qui si provano separate.


def test_una_pagina_porta_con_se_la_sua_cartella(tmp_path):
    """Il primo guasto: `<link href="style.css">` dentro una pagina servita da
    ``/api/preview/file`` si risolveva in ``/api/preview/style.css``. Nessun
    errore visibile: solo un sito senza foglio di stile."""
    sito = tmp_path / "sito"
    sito.mkdir()
    (sito / "index.html").write_text("<link href='style.css'>", encoding="utf-8")
    (sito / "style.css").write_text("body{}", encoding="utf-8")
    assert preview_root(tmp_path, "sito/index.html") == "sito"


def test_una_sottopagina_risale_alla_radice_del_sito(tmp_path):
    """`sito/pagine/chi-siamo.html` deve essere servita da `sito/`, senno' il
    suo `../css/` e' fuori dalla radice e non esiste."""
    (tmp_path / "sito" / "pagine").mkdir(parents=True)
    (tmp_path / "sito" / "index.html").write_text("<h1>casa</h1>", encoding="utf-8")
    (tmp_path / "sito" / "pagine" / "chi.html").write_text("<h1>chi</h1>", encoding="utf-8")
    assert preview_root(tmp_path, "sito/pagine/chi.html") == "sito"


def test_il_confine_piu_vicino_vince_sul_piu_alto(tmp_path):
    """In un monorepo con `.git` in cima e `package.json` nel sito, la radice
    giusta e' il sito: e' li' che si risolvono i percorsi assoluti."""
    (tmp_path / ".git").mkdir()
    app = tmp_path / "frontend"
    (app / "public").mkdir(parents=True)
    (app / "package.json").write_text("{}", encoding="utf-8")
    (app / "public" / "index.html").write_text("<h1>x</h1>", encoding="utf-8")
    assert preview_root(tmp_path, "frontend/public/index.html") == "frontend"


def test_senza_nessun_indizio_si_serve_solo_la_cartella_del_file(tmp_path):
    """Quello che non serve non si espone: mai il workspace intero per un file
    che sta in una cartella sua."""
    (tmp_path / "bozze").mkdir()
    (tmp_path / "bozze" / "volantino.svg").write_text("<svg/>", encoding="utf-8")
    assert preview_root(tmp_path, "bozze/volantino.svg") == "bozze"


def test_la_radice_non_esce_mai_dal_workspace(tmp_path):
    assert preview_root(tmp_path, "../../etc/passwd") == ""


# --- il server che da' un'origine vera --------------------------------------


@pytest.fixture()
def statico(tmp_path):
    """Il server delle anteprime, con una radice sotto controllo."""
    radice = tmp_path / "sito"
    (radice / "css").mkdir(parents=True)
    (radice / "index.html").write_text("<link href='/css/app.css'>", encoding="utf-8")
    (radice / "css" / "app.css").write_text("body{color:red}", encoding="utf-8")
    (tmp_path / "segreto.txt").write_text("non toccare", encoding="utf-8")
    previewhost.set_root(radice)
    with TestClient(previewhost.app) as c:
        yield c


def test_il_foglio_di_stile_si_trova_dalla_radice(statico):
    """Il motivo per cui la radice e' montata su `/` e non sotto un prefisso:
    i percorsi assoluti sono meta' dei casi veri, e un prefisso li romperebbe
    di nuovo."""
    risposta = statico.get("/css/app.css")
    assert risposta.status_code == 200
    assert "color:red" in risposta.text
    assert risposta.headers["content-type"].startswith("text/css")


def test_la_cartella_serve_il_suo_index(statico):
    assert "<link" in statico.get("/").text


@pytest.mark.parametrize("path", ["/../segreto.txt", "/css/../../segreto.txt"])
def test_il_server_delle_anteprime_non_esce_dalla_radice(statico, path):
    """Stessa guardia dei tool (`resolve_path`) con la radice al posto del
    workspace: non una seconda copia della logica."""
    assert statico.get(path).status_code in (403, 404)


def test_niente_cache_sulle_anteprime(statico):
    """L'agente riscrive gli stessi file di continuo: una risposta in cache
    significa guardare la versione di prima e non capire perche' la correzione
    'non ha funzionato'."""
    intestazioni = statico.get("/index.html").headers
    assert intestazioni["cache-control"] == "no-store"
    assert intestazioni["x-content-type-options"] == "nosniff"


# --- la rotta che apparecchia -----------------------------------------------


def test_la_rotta_host_dice_dove_guardare(client, monkeypatch):
    (client.ws / "sito").mkdir()
    (client.ws / "sito" / "index.html").write_text("<h1>x</h1>", encoding="utf-8")
    client.server.STATE.settings["preview_host_port"] = 0  # server spento
    risposta = client.post("/api/preview/host", json={"path": "sito/index.html"})
    assert risposta.status_code == 200
    corpo = risposta.json()
    assert corpo["root"] == "sito"
    # Porta spenta: il client sa ripiegare sull'iframe a origine opaca di
    # prima. Peggio del nuovo, ma non e' un pannello vuoto.
    assert corpo["url"] is None


def test_la_rotta_host_non_apparecchia_fuori_dal_workspace(client):
    assert client.post(
        "/api/preview/host", json={"path": "../segreto.txt"}
    ).status_code in (400, 404)


# --- il backend acceso da solo ----------------------------------------------


@pytest.fixture()
def backend_finto(monkeypatch):
    """Sostituisce il container: la porta risponde, e si registra il comando."""
    avviati: list[str] = []

    def start(command, workspace, **kwargs):
        avviati.append(command)
        return "4242"

    monkeypatch.setattr(sandbox, "start_background", start)
    monkeypatch.setattr(sandbox, "stop_background", lambda *a, **k: False)
    monkeypatch.setattr(sandbox, "kill_port_listener", lambda *a, **k: False)
    monkeypatch.setattr(sandbox, "background_log", lambda *a, **k: "")
    monkeypatch.setattr(sandbox, "background_alive", lambda *a, **k: True)
    # La porta si apre **dopo** l'avvio, come nella realta': prima e' libera
    # (senno' il tool crederebbe di doverla liberare e si arrenderebbe), poi
    # risponde. Un finto che risponde sempre nasconderebbe proprio il passo che
    # qui interessa.
    monkeypatch.setattr(
        sandbox, "port_state",
        lambda *a, **k: sandbox.PORT_OPEN if avviati else sandbox.PORT_FREE,
    )
    return avviati


def _ctx_docker(tmp_path) -> ToolContext:
    return ToolContext(
        workspace=str(tmp_path), sandbox="docker", preview_ports=(8200, 8203)
    )


def test_una_pagina_con_un_backend_lo_accende_da_sola(tmp_path, monkeypatch, backend_finto):
    """Il secondo guasto: la pagina si vedeva, ma ogni fetch verso la sua API
    tornava un errore e sembrava rotta. L'informazione per capirlo era gia' sul
    disco -- e quello che si puo' sapere gratis non si chiede al modello."""
    (tmp_path / "app.py").write_text(
        "from flask import Flask\napp = Flask(__name__)\n", encoding="utf-8"
    )
    (tmp_path / "index.html").write_text("<h1>ciao</h1>", encoding="utf-8")

    payload = json.loads(
        dispatch(_ctx_docker(tmp_path), PREVIEW_TOOL, {"action": "file", "path": "index.html"})
    )
    assert payload["preview"]["kind"] == "app"
    assert payload["preview"]["port"] == 8200
    # Un backend decide da se' i propri indirizzi: il percorso del file sul
    # disco non e' l'URL della pagina.
    assert payload["preview"]["url_path"] == "/"
    assert "--host 0.0.0.0" in backend_finto[0] and "flask" in backend_finto[0]
    assert "non riavviarlo" in payload["note"]


def test_un_backend_che_non_risponde_ripiega_sullo_statico(tmp_path, monkeypatch, backend_finto):
    """Meglio una pagina senza dati che un pannello vuoto -- ma il modello deve
    leggere perche', o non ha modo di rimediare."""
    (tmp_path / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8"
    )
    (tmp_path / "index.html").write_text("<h1>ciao</h1>", encoding="utf-8")
    # Il processo parte e muore subito: la porta resta libera per sempre.
    monkeypatch.setattr(sandbox, "port_state", lambda *a, **k: sandbox.PORT_FREE)

    payload = json.loads(
        dispatch(_ctx_docker(tmp_path), PREVIEW_TOOL, {"action": "file", "path": "index.html"})
    )
    assert payload["preview"]["kind"] == "render"
    assert "statico" in payload["note"]
    assert "uvicorn" in backend_finto[0]


def test_l_avvio_automatico_si_puo_spegnere(tmp_path, backend_finto):
    (tmp_path / "app.py").write_text(
        "from flask import Flask\napp = Flask(__name__)\n", encoding="utf-8"
    )
    (tmp_path / "index.html").write_text("<h1>ciao</h1>", encoding="utf-8")
    ctx = _ctx_docker(tmp_path)
    ctx.preview_autostart_backend = False

    payload = json.loads(
        dispatch(ctx, PREVIEW_TOOL, {"action": "file", "path": "index.html"})
    )
    assert payload["preview"]["kind"] == "render"
    assert backend_finto == []


def test_sull_host_una_pagina_resta_statica_senza_provarci(tmp_path, backend_finto):
    """Senza container non c'e' nessuna porta da pubblicare: si mostra il file
    e basta, senza far partire niente sulla macchina dell'utente."""
    (tmp_path / "app.py").write_text(
        "from flask import Flask\napp = Flask(__name__)\n", encoding="utf-8"
    )
    (tmp_path / "index.html").write_text("<h1>ciao</h1>", encoding="utf-8")
    ctx = ToolContext(workspace=str(tmp_path), sandbox="host")
    payload = json.loads(
        dispatch(ctx, PREVIEW_TOOL, {"action": "file", "path": "index.html"})
    )
    assert payload["preview"]["kind"] == "render"
    assert backend_finto == []


@pytest.mark.parametrize(
    "nome,contenuto,atteso",
    [
        ("app.py", "from flask import Flask\napp = Flask(__name__)", "flask"),
        ("main.py", "from fastapi import FastAPI\napi = FastAPI()", "uvicorn main:api"),
        ("manage.py", "import django\n", "runserver"),
    ],
)
def test_il_riconoscimento_del_backend(tmp_path, nome, contenuto, atteso):
    (tmp_path / nome).write_text(contenuto, encoding="utf-8")
    spia = preview_backend(tmp_path, 8200)
    assert spia and atteso in spia["command"]
    assert "8200" in spia["command"]


def test_vite_riceve_la_porta_giusta(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"dev": "vite"}, "devDependencies": {"vite": "^5"}}),
        encoding="utf-8",
    )
    spia = preview_backend(tmp_path, 8201)
    assert spia["kind"] == "vite"
    assert "--port 8201" in spia["command"] and "--host 0.0.0.0" in spia["command"]


def test_un_sito_statico_non_ha_nessun_backend_da_avviare(tmp_path):
    """Nel dubbio None: un riconoscimento sbagliato costa un avvio fallito e
    del rumore, uno mancato costa solo quello che si aveva prima."""
    (tmp_path / "index.html").write_text("<h1>x</h1>", encoding="utf-8")
    (tmp_path / "style.css").write_text("body{}", encoding="utf-8")
    assert preview_backend(tmp_path, 8200) is None


# --- quello che il modello deve sapere --------------------------------------


def test_al_modello_si_dice_che_il_pannello_si_ricarica_da_solo():
    """Senza, richiama preview dopo ogni correzione: un passo intero per non
    fare niente."""
    nota = agent_mod.render_preview_note(
        {"kind": "render", "path": "sito/index.html", "root": "sito"}
    )
    assert "sito" in nota
    assert "ricarica" in nota.lower()


def test_l_anteprima_aperta_da_sola_sa_da_che_cartella_e_servita(fake_ollama, tmp_path):
    """``preview_from_result`` guarda una tool call e basta: il workspace non ce
    l'ha. Senza l'aggiunta nel ciclo, la nota al modello direbbe "la cartella
    del workspace" anche per una pagina che sta in sito/ -- una frase falsa su
    cui poi ragiona."""
    from core.backend import OllamaBackend
    from core.config import GenParams
    from core.tools import TOOLS_SCHEMA

    (tmp_path / "sito").mkdir()
    # C'e' gia' un index.html: e' l'indizio che rende `sito/` la radice. Il
    # file scritto e' un altro, perche' riscrivere alla cieca un file mai letto
    # e' proprio cio' che l'harness rifiuta.
    (tmp_path / "sito" / "index.html").write_text("<h1>casa</h1>", encoding="utf-8")

    url, _ = fake_ollama
    originale = fake.SCRIPT
    fake.SCRIPT = [[
        {
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "write_file",
                            "arguments": {
                                "filepath": "sito/contatti.html",
                                "content": "<h1>contatti</h1>",
                            },
                        }
                    }
                ],
            }
        }
    ]]
    try:
        eventi = list(
            agent_mod.run_turn(
                backend=OllamaBackend(url, timeout_s=20),
                params=GenParams(model="fake:latest"),
                tools_schema=TOOLS_SCHEMA,
                tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
                ui_messages=[{"role": "user", "content": "aggiungi la pagina contatti"}],
                system_prompt="SYS",
                env_header=None,
                max_steps=2,
            )
        )
    finally:
        fake.SCRIPT = originale

    anteprime = [e for e in eventi if isinstance(e, agent_mod.PreviewUpdated)]
    assert anteprime, "un file visuale scritto apre il pannello da solo"
    assert anteprime[-1].payload["root"] == "sito"
