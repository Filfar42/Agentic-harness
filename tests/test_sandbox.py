"""Test della sandbox Docker.

Il demone Docker non c'e' in CI (ne' dentro un container), quindi qui si mette
sul PATH un **finto ``docker``**: uno script che registra gli argomenti
ricevuti e simula le risposte. Non verifica che Linux isoli davvero -- quello
lo fa il kernel -- ma verifica le cose che possono rompersi nel nostro codice:
che il mount sia solo il workspace, che il container venga riusato invece di
ricrearlo ad ogni comando, e soprattutto che quando Docker manca l'harness
**non ripieghi silenziosamente sull'host**.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import sandbox
from core.tools import ToolContext, dispatch

STUB = '''#!/usr/bin/env python3
import json, os, sys
log = os.environ["DOCKER_LOG"]
state = os.environ["DOCKER_STATE"]
args = sys.argv[1:]
with open(log, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(args) + "\\n")

if os.environ.get("DOCKER_BROKEN"):
    sys.stderr.write("Cannot connect to the Docker daemon\\n")
    sys.exit(1)

cmd = args[0] if args else ""
if cmd == "version":
    print("27.1.0"); sys.exit(0)
if cmd == "ps":
    if "--quiet" in args:
        # _is_running: lo stato del nostro container.
        print("abc123" if os.path.exists(state) else "", end="")
    else:
        # ps --format con --filter label=... (il filtro e' di docker; qui si
        # simulano i contenitori etichettati altrui da una variabile d'ambiente).
        for riga in os.environ.get("DOCKER_FOREIGN_PS", "").splitlines():
            if riga.strip(): print(riga)
    sys.exit(0)
if cmd == "run":
    # Registra l'impronta con cui il container e' stato creato: e' quello che
    # 'docker inspect' rilegge per capire se gli argomenti sono cambiati.
    impronta = ""
    for i, a in enumerate(args):
        if a == "--label" and i + 1 < len(args) and args[i + 1].startswith("harness-fingerprint="):
            impronta = args[i + 1].split("=", 1)[1]
    open(state, "w").write(impronta); print("abc123"); sys.exit(0)
if cmd == "image" and len(args) > 1 and args[1] == "inspect":
    # Etichetta delle capacita' dell'immagine: e' cosi' che l'harness
    # distingue un'immagine costruita col Dockerfile nuovo da una vecchia.
    print(os.environ.get("DOCKER_IMAGE_FEATURES", "<no value>")); sys.exit(0)
if cmd == "inspect":
    if not os.path.exists(state):
        sys.stderr.write("No such object" + chr(10)); sys.exit(1)
    salvata = open(state).read().strip()
    print(salvata if salvata else "<no value>"); sys.exit(0)
if cmd == "rm":
    if os.path.exists(state): os.remove(state)
    sys.exit(0)
if cmd == "exec":
    shell_cmd = args[-1]
    if "FALLISCI" in shell_cmd:
        sys.stderr.write("boom\\n"); sys.exit(1)
    if "SCADI" in shell_cmd:
        sys.exit(124)                    # convenzione di coreutils timeout
    print("eseguito nel container: " + shell_cmd)
    sys.exit(0)
sys.exit(0)
'''


@pytest.fixture()
def fake_docker(tmp_path, monkeypatch):
    """Mette un finto ``docker`` in testa al PATH e restituisce il log.

    Su Windows non basta scrivere un file di nome ``docker`` con lo shebang:
    ``CreateProcess`` non legge la prima riga di uno script, cerca un
    eseguibile fra quelli elencati in ``PATHEXT``. Senza un ``docker.cmd``
    ogni ``subprocess.run(["docker", ...])`` moriva con ``WinError 2``, e i
    ventotto test della sandbox e delle anteprime risultavano rossi per un
    motivo che con il codice del progetto non c'entra niente.

    Anche il PATH va costruito con ``os.pathsep`` e conservando quello vero:
    su Windows servono ``python.exe`` e le DLL di sistema, e sostituirlo con
    ``/usr/bin:/bin`` significava lasciare il finto docker da solo in un PATH
    che non contiene neppure l'interprete che deve eseguirlo.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"

    if os.name == "nt":
        script = bin_dir / "docker_stub.py"
        script.write_text(STUB, encoding="utf-8")
        # %* passa gli argomenti cosi' come sono; le virgolette attorno ai due
        # percorsi reggono gli spazi di "C:\Users\...\AppData\Local\Temp".
        (bin_dir / "docker.cmd").write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8"
        )
        percorso = os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")])
        # A .cmd shim interprets Docker's Go-template pipes as shell syntax.
        # Keep the real executable lookup/error handling, but execute this
        # Python fixture directly so argv is identical to docker.exe's argv.
        original_run = sandbox.run_bounded

        def run_stub(args, **kwargs):
            if str(args[0]).lower() == str(bin_dir / "docker.cmd").lower():
                args = [sys.executable, str(script), *args[1:]]
            return original_run(args, **kwargs)

        monkeypatch.setattr(sandbox, "run_bounded", run_stub)
    else:
        stub = bin_dir / "docker"
        stub.write_text(STUB, encoding="utf-8")
        stub.chmod(0o755)
        percorso = os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"])

    monkeypatch.setenv("PATH", percorso)
    monkeypatch.setenv("DOCKER_LOG", str(log))
    monkeypatch.setenv("DOCKER_STATE", str(tmp_path / "container.up"))

    def calls() -> list[list[str]]:
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    return calls


@pytest.fixture()
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "hello.py").write_text("print('ciao')\n", encoding="utf-8")
    return ws


def ctx_for(workspace, **kwargs) -> ToolContext:
    return ToolContext(workspace=str(workspace), timeout_s=10, **kwargs)


# --- il recinto c'e' e monta solo il workspace ------------------------------


def test_the_container_mounts_the_workspace_and_nothing_else(fake_docker, workspace):
    sandbox.ensure_container(workspace, image="python:3.12-slim")
    run = next(c for c in fake_docker() if c[0] == "run")
    mounts = [run[i + 1] for i, a in enumerate(run) if a == "--volume"]
    assert mounts == [f"{workspace.resolve()}:/work"]
    assert run[run.index("--workdir") + 1] == "/work"
    assert "--cap-drop" in run and run[run.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges" in run


def test_network_can_be_switched_off(fake_docker, workspace):
    sandbox.ensure_container(workspace, network=False)
    run = next(c for c in fake_docker() if c[0] == "run")
    assert run[run.index("--network") + 1] == "none"


def test_network_is_on_by_default(fake_docker, workspace):
    sandbox.ensure_container(workspace)
    run = next(c for c in fake_docker() if c[0] == "run")
    assert "--network" not in run


def test_two_workspaces_get_two_containers(workspace, tmp_path):
    other = tmp_path / "altro"
    other.mkdir()
    assert sandbox.container_name(workspace) != sandbox.container_name(other)
    assert sandbox.container_name(workspace) == sandbox.container_name(workspace)


# --- il container si riusa --------------------------------------------------


def test_the_container_is_started_once_and_then_reused(fake_docker, workspace):
    for _ in range(3):
        sandbox.run("echo ciao", workspace, timeout_s=5)
    calls = fake_docker()
    assert sum(1 for c in calls if c[0] == "run") == 1
    assert sum(1 for c in calls if c[0] == "exec") == 3


def test_the_timeout_runs_inside_the_container(fake_docker, workspace):
    sandbox.run("pytest -q", workspace, timeout_s=42)
    exec_call = next(c for c in fake_docker() if c[0] == "exec")
    assert exec_call[-1] == (
        "timeout --signal=TERM --kill-after=5 42 /bin/sh -c 'pytest -q'"
    )


def test_a_timeout_is_reported_as_such(fake_docker, workspace):
    result = sandbox.run("SCADI", workspace, timeout_s=1)
    assert result.timed_out is True


# --- fallimento chiuso: mai un ripiego sull'host ----------------------------


def test_without_docker_the_command_does_not_run_on_the_host(
    fake_docker, workspace, monkeypatch
):
    """Il punto di tutta la funzione: se il recinto manca, non si esegue."""
    monkeypatch.setenv("DOCKER_BROKEN", "1")
    sentinel = workspace.parent / "evasione.txt"

    result = json.loads(
        dispatch(
            ctx_for(workspace, sandbox="docker"),
            "run_command",
            {"command": f"touch {sentinel}"},
        )
    )
    assert "error" in result
    assert "Docker" in result["error"]
    assert not sentinel.exists(), "il comando e' stato eseguito sull'host!"


def test_the_error_explains_how_to_get_out_of_it(fake_docker, workspace, monkeypatch):
    monkeypatch.setenv("DOCKER_BROKEN", "1")
    result = json.loads(
        dispatch(ctx_for(workspace, sandbox="docker"), "run_command", {"command": "ls"})
    )
    assert "Docker Desktop" in result["hint"]
    assert "host" in result["hint"]


# --- integrazione con run_command -------------------------------------------


def test_run_command_goes_through_docker_when_the_sandbox_is_on(fake_docker, workspace):
    result = json.loads(
        dispatch(ctx_for(workspace, sandbox="docker"), "run_command", {"command": "pytest -q"})
    )
    assert result["esito"] == "ok"
    assert "eseguito nel container" in result["stdout"]
    assert any(c[0] == "exec" for c in fake_docker())


def test_a_failure_in_the_container_still_drives_self_correction(fake_docker, workspace):
    """Il ciclo di riparazione deve funzionare identico dentro la sandbox."""
    result = json.loads(
        dispatch(ctx_for(workspace, sandbox="docker"), "run_command", {"command": "FALLISCI"})
    )
    assert result["esito"] == "FALLITO"
    assert result["returncode"] == 1
    assert "next_step" in result


def test_host_mode_still_runs_locally(workspace):
    result = json.loads(
        dispatch(ctx_for(workspace, sandbox="host"), "run_command", {"command": "echo locale"})
    )
    assert result["esito"] == "ok"
    assert "locale" in result["stdout"]


def test_dentro_la_sandbox_si_blocca_il_bersaglio_non_il_verbo(fake_docker, workspace):
    """Il recinto e' la cartella montata: sopra si rifiuta, dentro si esegue.

    Prima bastava la stringa ``rm -r`` per un rifiuto, e sulla sessione del
    19/08/2026 l'agente si e' preso tre no di fila su un ``rm -r __pycache__``
    che l'utente aveva chiesto a parole. Un guard-rail che vieta il lavoro
    richiesto non protegge nessuno: insegna solo ad aggirarlo.
    """
    ctx = ctx_for(workspace, sandbox="docker")
    fuori = json.loads(dispatch(ctx, "run_command", {"command": "rm -rf /"}))
    assert "fuori dalla cartella di lavoro" in fuori["error"]
    assert not any(c[0] == "exec" for c in fake_docker())

    # La radice montata **e'** il progetto dell'utente: resta fuori portata.
    radice = json.loads(dispatch(ctx, "run_command", {"command": "rm -rf /work"}))
    assert "error" in radice
    svuota = json.loads(dispatch(ctx, "run_command", {"command": "rm -rf /work/*"}))
    assert "error" in svuota

    # Dentro, invece, si esegue: e' esattamente cio' per cui la sandbox esiste.
    dentro = json.loads(
        dispatch(ctx, "run_command", {"command": "rm -rf todo_list/__pycache__"})
    )
    assert "error" not in dentro
    assert any(c[0] == "exec" for c in fake_docker())


def test_senza_sandbox_il_guard_rail_resta_severo(workspace):
    """In modalita' host un percorso relativo puo' puntare ovunque: ``cd ..`` e
    si e' nel disco dell'utente. Li' il verbo torna a essere l'unico segnale."""
    ctx = ctx_for(workspace, sandbox="host")
    result = json.loads(dispatch(ctx, "run_command", {"command": "rm -rf build"}))
    assert "guard-rail" in result["error"]


def test_quello_che_non_e_un_file_resta_vietato_ovunque(fake_docker, workspace):
    """Il container condivide il kernel: spegnere la macchina o riscrivere un
    disco a blocchi non diventa innocuo perche' si e' dentro un recinto."""
    ctx = ctx_for(workspace, sandbox="docker")
    for comando in ("mkfs.ext4 /dev/sda1", "shutdown -h now", "dd if=/dev/zero of=/dev/sda"):
        result = json.loads(dispatch(ctx, "run_command", {"command": comando}))
        assert "tocca la macchina" in result["error"], comando
    assert not any(c[0] == "exec" for c in fake_docker())


# --- immagine del progetto --------------------------------------------------
# L'immagine di serie ha Python e basta: senza pytest, ruff e git, il primo
# comando di verifica dell'agente risponde "not found" e sembra un bug nostro.


def test_the_default_image_is_declared_incomplete_to_the_ui(fake_docker, workspace):
    info = sandbox.status(workspace)
    assert info["dockerfile"] is None            # non c'e' ancora
    assert info["project_image"].endswith("-img")


def test_the_dockerfile_installs_what_the_agent_needs(workspace):
    path = sandbox.write_dockerfile(workspace)
    testo = path.read_text(encoding="utf-8")
    assert path.name == "Dockerfile.sandbox"
    for strumento in ("git", "pytest", "ruff"):
        assert strumento in testo
    assert "WORKDIR /work" in testo


def test_an_existing_dockerfile_is_never_overwritten(workspace):
    path = sandbox.write_dockerfile(workspace)
    path.write_text("FROM mia-immagine\n", encoding="utf-8")
    sandbox.write_dockerfile(workspace)
    assert path.read_text(encoding="utf-8") == "FROM mia-immagine\n"


def test_il_contesto_di_build_esclude_le_cartelle_pesanti(fake_docker, workspace):
    """Il Dockerfile copia tre file; il contesto e' tutto il workspace.

    Su un progetto con ``node_modules`` o ``.venv`` sono gigabyte impacchettati
    e spediti al demone per essere buttati, e la barra resta ferma su "invio del
    contesto" per minuti prima che la build cominci.

    Il file si chiama ``Dockerfile.sandbox.dockerignore`` e non ``.dockerignore``
    di proposito: docker cerca prima il nome legato al Dockerfile, cosi' le
    esclusioni valgono per questa build e non per i ``docker build`` che
    l'utente fa per conto suo nella stessa cartella.
    """
    sandbox.write_dockerfile(workspace)
    esclusioni = sandbox.dockerignore_path(workspace)
    assert esclusioni.name == "Dockerfile.sandbox.dockerignore"
    testo = esclusioni.read_text(encoding="utf-8")
    for pesante in ("node_modules/", ".venv/", ".git/", "__pycache__/"):
        assert pesante in testo, f"{pesante} finisce ancora nel contesto di build"


def test_un_workspace_gia_preparato_ottiene_comunque_le_esclusioni(fake_docker, workspace):
    """``write_dockerfile`` si ferma appena trova il Dockerfile.

    Un workspace preparato da una versione precedente ha il Dockerfile e non le
    esclusioni, e non passerebbe mai di la': sarebbe la build lenta per sempre.
    """
    sandbox.dockerfile_path(workspace).write_text("FROM python:3.12-slim\n", encoding="utf-8")
    assert not sandbox.dockerignore_path(workspace).exists()
    sandbox.build_image(workspace)
    assert sandbox.dockerignore_path(workspace).exists()


def test_le_esclusioni_esistenti_non_si_sovrascrivono(fake_docker, workspace):
    sandbox.write_dockerfile(workspace)
    esclusioni = sandbox.dockerignore_path(workspace)
    esclusioni.write_text("mia-roba/\n", encoding="utf-8")
    sandbox.write_dockerfile(workspace)
    sandbox.build_image(workspace)
    assert esclusioni.read_text(encoding="utf-8") == "mia-roba/\n"


def test_building_without_a_dockerfile_says_so(fake_docker, workspace):
    with pytest.raises(sandbox.SandboxError, match=r"Dockerfile.sandbox"):
        sandbox.build_image(workspace)


def test_the_build_tags_the_image_and_drops_the_stale_container(fake_docker, workspace):
    sandbox.write_dockerfile(workspace)
    sandbox.ensure_container(workspace)          # container con la vecchia immagine
    tag, _ = sandbox.build_image(workspace)

    calls = fake_docker()
    build = next(c for c in calls if c[0] == "build")
    assert build[build.index("-t") + 1] == tag
    # il container vecchio gira ancora sull'immagine precedente: va buttato
    assert any(c[0] == "rm" for c in calls[calls.index(build):])


def test_the_dockerfile_builds_even_without_a_requirements_file():
    """Una COPY con soli glob fallisce se nessuno combacia.

    In un workspace che non e' un progetto Python (o che usa solo
    pyproject.toml) l'immagine non si sarebbe costruita affatto: serve almeno
    una sorgente che esista sempre.
    """
    copia = next(
        r for r in sandbox.DOCKERFILE_TEMPLATE.splitlines() if r.startswith("COPY ")
    )
    sorgenti = copia.split()[1:-1]
    assert any("*" not in s for s in sorgenti), copia
    assert sandbox.SANDBOX_DOCKERFILE in copia


# --- le porte non si litigano tra istanze -----------------------------------


def test_other_workspace_port_conflict_never_removes_its_container(fake_docker, workspace, monkeypatch):
    """A live container in another workspace is not an automatic eviction target."""
    monkeypatch.setenv("DOCKER_FOREIGN_PS", "fed456|agentic-harness-9c1d|127.0.0.1:8204-8207->8204-8207/tcp")
    with pytest.raises(sandbox.SandboxError, match="altro workspace"):
        sandbox.ensure_container(workspace, ports=(8204, 8207))
    calls = fake_docker()
    assert not any(c[0] == "rm" and c[-1] == "agentic-harness-9c1d" for c in calls)
    assert not any(c[0] == "run" for c in calls)


def test_no_overlap_means_no_removals(fake_docker, workspace, monkeypatch):
    """Nessun sovrapporsi di porte: non si rimuove nulla, neppure il contenitore altrui."""
    nome_nostro = sandbox.container_name(workspace)
    monkeypatch.setenv(
        "DOCKER_FOREIGN_PS",
        "fed456|agentic-harness-9c1d|0.0.0.0:9001->3000/tcp",
    )
    sandbox.ensure_container(workspace, ports=(8204, 8207))

    calls = fake_docker()
    estranee = [c for c in calls if c[0] == "rm" and c[-1] != nome_nostro]
    assert not estranee, f"rimozione senza motivo: {estranee}"


def test_a_command_starting_with_cd_is_not_broken_by_the_timeout(fake_docker, workspace):
    """`timeout N cd /work && pytest` fallisce con 127: cd non e' un binario.

    Osservato con qwen3.5, che aveva scritto il comando piu' naturale del
    mondo e si era preso "failed to run command 'cd'". Il comando va passato
    a una shell, non a timeout come argomenti nudi.
    """
    sandbox.run("cd /work && python -m pytest -v", workspace, timeout_s=60)
    exec_call = next(c for c in fake_docker() if c[0] == "exec")
    wrapped = exec_call[-1]
    assert wrapped.startswith("timeout --signal=TERM --kill-after=5 60 /bin/sh -c ")
    # il comando dell'utente e' un singolo argomento quotato, non parole sparse
    assert "'cd /work && python -m pytest -v'" in wrapped


def test_quotes_in_a_command_survive_the_wrapping(fake_docker, workspace):
    comando = """python -c "print('ciao')" """.strip()
    sandbox.run(comando, workspace, timeout_s=10)
    wrapped = next(c for c in fake_docker() if c[0] == "exec")[-1]
    import shlex
    # la shell interna deve ricevere esattamente il comando originale
    assert shlex.split(wrapped)[-1] == comando


def test_il_binario_scaricato_si_verifica_prima_di_installarlo():
    """Il ripiego di ttyd scarica un eseguibile e lo mette in /usr/local/bin.

    È il punto più delicato del Dockerfile: quel binario poi gira dentro
    l'immagine dell'agente. Senza un digest ci si fida di chiunque stia in
    mezzo alla connessione nel momento della build -- e una build si rifà
    raramente, quindi un binario sbagliato ci resta per mesi.
    """
    tpl = sandbox.DOCKERFILE_TEMPLATE
    blocco = tpl[tpl.index("ttyd: se la distribuzione") :]
    blocco = blocco[: blocco.index("\n\n#")]
    assert "sha256sum -c" in blocco, "il binario si installa senza verifica"
    # Un digest per architettura, non uno solo copiato due volte.
    import re

    digest = set(re.findall(r"sha=([0-9a-f]{64})", blocco))
    assert len(digest) == 2, f"{len(digest)} digest distinti: {digest}"
