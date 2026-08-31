"""Esecuzione dei comandi dentro un container Docker.

Il problema
-----------
``run_command`` girava con ``shell=True`` e ``cwd=workspace``, protetto solo da
un elenco di espressioni regolari "distruttive". Ma la working directory non e'
un recinto: ``cd ..`` e sei fuori, ``type C:\\Users\\...`` legge quello che
vuole, ``curl`` manda dove vuole. Contro un modello che vaga -- e contro un
prompt injection dentro un file che l'agente legge -- non c'e' regex che tenga.

La soluzione
------------
I comandi girano in un container che vede **solo la cartella di lavoro**,
montata su ``/work``. Non e' un filtro sul comando: e' il kernel che non
espone il resto del disco. Il denylist resta come prima linea, ma non e' piu'
lui a reggere la sicurezza.

Scelte di progetto
------------------
* **Un container per workspace, tenuto vivo.** Avviarne uno per comando
  costerebbe mezzo secondo a botta su un ciclo agentico che di comandi ne fa
  molti. Il container dorme (``sleep infinity``) e ogni comando e' un
  ``docker exec``, che costa poche decine di millisecondi.
* **Fallimento chiuso.** Se Docker non risponde, i comandi *non* ripiegano
  sull'host: sarebbe esattamente il buco che si sta chiudendo. L'errore lo
  dice, e chi vuole l'esecuzione diretta la sceglie a mano.
* **Timeout dentro il container.** ``timeout`` di coreutils ferma il processo
  la' dentro; uccidere il solo ``docker exec`` dall'esterno lascerebbe il
  comando vivo nel container a bruciare CPU.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Immagine di default: piccola, con Python e i coreutils. Chi ha bisogno di
# altro (nodejs, compilatori) punta l'impostazione a un'immagine propria.
#
# Un tag e non un digest, di proposito -- ed e' la scelta opposta a quella
# fatta per il binario di ttyd venti righe piu' giu', quindi vale la pena dire
# perche'. Il digest darebbe build riproducibili: due ricostruzioni a un mese
# di distanza danno la stessa immagine. Ma questa immagine e' il recinto in cui
# gira l'agente, si ricostruisce di rado, e un digest fissato oggi vuol dire
# restare su una Debian senza le patch di sicurezza dei mesi successivi finche'
# qualcuno non si ricorda di aggiornarlo. Per ttyd non c'e' il dilemma: quello
# e' un binario preso da internet e la verifica non costa niente in aggiornabilita'.
DEFAULT_IMAGE = "python:3.12-slim"
WORKDIR = "/work"
LABEL = "local-agent-harness"

# Tetti di risorse: un ciclo agentico che sbaglia un comando non deve poter
# mettere in ginocchio la macchina mentre l'utente guarda.
MEM_LIMIT = "4g"
PIDS_LIMIT = "512"

_DOCKER_TIMEOUT = 90.0        # per i comandi di gestione (run, ps, stop)

# Etichetta con l'impronta degli argomenti di ``docker run``. Serve a un
# problema che altrimenti non da' nessun sintomo: il container viene riusato
# per nome, quindi cambiare le porte pubblicate nelle impostazioni non avrebbe
# alcun effetto finche' quel container resta in piedi -- l'utente vedrebbe la
# porta configurata e un'anteprima che non si connette, senza un errore da
# nessuna parte. Con l'impronta, gli argomenti diversi si vedono e il container
# si rifa'.
FINGERPRINT_LABEL = "harness-fingerprint"

# Le porte pubblicate si legano a **127.0.0.1**, mai a 0.0.0.0. La sandbox
# esiste per contenere l'agente: aprire una porta e' gia' una concessione, e
# quella concessione deve fermarsi a questa macchina invece di affacciarsi
# sulla rete locale.
PORT_BIND_HOST = "127.0.0.1"


class SandboxError(RuntimeError):
    """Docker non utilizzabile: il chiamante deve dirlo all'utente, non aggirarlo."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


def docker_available() -> tuple[bool, str]:
    """(disponibile, dettaglio leggibile). Non solleva: serve anche alla UI."""
    if shutil.which("docker") is None:
        return False, "Il comando 'docker' non e' nel PATH."
    try:
        proc = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"Docker non risponde: {exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        return False, detail[-1] if detail else "Il demone Docker non e' raggiungibile."
    return True, f"Docker Engine {proc.stdout.strip()}"


# Comandi per accendere Docker Desktop, provati in ordine. Il primo e' la CLI
# ufficiale di Docker Desktop (`docker desktop start`), che c'e' dalle versioni
# recenti; gli altri sono il modo di lanciare l'applicazione su ciascun sistema,
# per chi ha una versione precedente. Non e' una cosa che si indovina: se
# nessuno funziona lo si dice, invece di restare in attesa di un demone che non
# arrivera' mai.
_DOCKER_LAUNCHERS: dict[str, tuple[list[str], ...]] = {
    "Windows": (
        ["docker", "desktop", "start"],
        ["cmd", "/c", "start", "", r"C:\Program Files\Docker\Docker\Docker Desktop.exe"],
    ),
    "Darwin": (
        ["docker", "desktop", "start"],
        ["open", "-a", "Docker"],
    ),
    "Linux": (
        ["docker", "desktop", "start"],
        ["systemctl", "--user", "start", "docker-desktop"],
    ),
}


def start_engine(timeout_s: float = 120.0, poll_s: float = 2.0) -> tuple[bool, str]:
    """Prova ad accendere Docker e aspetta che il demone risponda.

    Bloccante di proposito: il chiamante la mette in un thread. Docker Desktop
    ci mette dai venti ai sessanta secondi a partire, e un endpoint HTTP che
    aspettasse quel tempo sarebbe una pessima idea -- ma neanche un "avviato!"
    immediato serve a qualcosa, perche' la domanda vera e' "adesso posso
    eseguire comandi?", e la risposta arriva solo interrogando il demone.
    """
    gia_su, dettaglio = docker_available()
    if gia_su:
        return True, dettaglio

    lanciati: list[str] = []
    for argv in _DOCKER_LAUNCHERS.get(platform.system(), ()):
        try:
            subprocess.Popen(
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
            lanciati.append(argv[0])
            break
        except (OSError, ValueError):
            continue

    if not lanciati:
        return False, (
            "Non ho trovato come avviare Docker su questo sistema: "
            "avvialo a mano e riprova."
        )

    scadenza = time.monotonic() + timeout_s
    while time.monotonic() < scadenza:
        time.sleep(poll_s)
        pronto, dettaglio = docker_available()
        if pronto:
            return True, dettaglio
    return False, (
        f"Docker e' stato avviato ma il demone non ha risposto entro "
        f"{int(timeout_s)}s. Di solito significa che sta ancora caricando: "
        "riprova fra poco."
    )


def image_exists(tag: str) -> bool:
    """L'immagine e' gia' costruita in locale?

    Serve a non rifare una build da minuti ad ogni cambio di workspace: il
    Dockerfile sta nel workspace, ma l'immagine costruita vive nel demone e
    sopravvive benissimo a una chiusura dell'applicazione.
    """
    if not tag:
        return False
    try:
        proc = _run_docker(["image", "inspect", tag, "--format", "{{.Id}}"], timeout=20)
    except SandboxError:
        return False
    return proc.returncode == 0 and bool(proc.stdout.strip())


# Container gia' verificati: nome -> (impronta, quando). Vedi ``ensure_container``.
_CONTAINER_OK: dict[str, tuple[str, float]] = {}
_CONTAINER_OK_TTL_S = 3.0


def dimentica_container(name: str | None = None) -> None:
    """Invalida la memo: dopo un remove, un restart o un cambio di immagine."""
    if name is None:
        _CONTAINER_OK.clear()
    else:
        _CONTAINER_OK.pop(name, None)


def container_name(workspace: str | Path) -> str:
    """Nome stabile per workspace: due cartelle diverse non si mescolano."""
    digest = hashlib.sha256(str(Path(workspace).resolve()).encode()).hexdigest()[:10]
    return f"agentic-harness-{digest}"


def _run_docker(args: list[str], timeout: float = _DOCKER_TIMEOUT) -> subprocess.CompletedProcess:
    """Un comando docker. Il binario si lascia risolvere a ``subprocess``.

    Memorizzare il percorso assoluto con ``shutil.which`` sembra un
    risparmio -- le chiamate sono tante -- ma **il PATH non e' costante per
    tutta la vita del processo**, e qui cambia davvero: la suite mette un finto
    ``docker`` in testa al PATH per ogni test, e un percorso assoluto messo in
    cache lo scavalcherebbe. Quello che costa non e' trovare il binario: e'
    avviare il processo, e a quello risponde la memo di ``ensure_container``.
    """
    try:
        return subprocess.run(
            ["docker", *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise SandboxError("Il comando 'docker' non e' nel PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise SandboxError(f"Docker non ha risposto entro {timeout:.0f}s.") from exc
    except OSError as exc:
        raise SandboxError(f"Docker non utilizzabile: {exc}") from exc


def _is_running(name: str) -> bool:
    proc = _run_docker(["ps", "--quiet", "--filter", f"name=^{name}$"], timeout=20)
    return bool(proc.stdout.strip())


def _remove(name: str) -> None:
    dimentica_container(name)
    _run_docker(["rm", "--force", name], timeout=30)


def _porta_host(spec: str) -> tuple[int, int] | None:
    """Estremi (inclusi) delle porte host in una voce di ``{{.Ports}}`` di docker ps."""
    lato = spec.split("->", 1)[0].strip()
    if not lato:
        return None
    # Con IP esplicito ("127.0.0.1:8204-8207") l'ultima parte dopo ':' e' la/o
    # le porte host; senza (raro) tutto il lato e' gia' la specifica di porta.
    cand = lato.rsplit(":", 1)[-1] if ":" in lato else lato
    try:
        lo, hi = (int(x) for x in cand.split("-", 1)) if "-" in cand else (int(cand), int(cand))
    except ValueError:
        return None
    return lo, hi


def _port_bind_conflittuali(nostra: str, lo: int, hi: int) -> list[str]:
    """Nomi dei contenitori dell'harness di un ALTRO workspace che pubblicano una nostra porta.

    Solo quelli con la nostra etichetta (il resto non ci riguarda) e solo in
    esecuzione: un container fermo non tiene proxy sull'host, quindi non puo'
    occupare nessuna porta. Il nostro stesso contenitore e' escluso per nome:
    se va ricreato per impronta diversa, ``ensure_container`` lo fa da se'.
    """
    proc = _run_docker(
        ["ps", "--format", "{{.ID}}|{{.Names}}|{{.Ports}}", f"--filter=label={LABEL}=1"],
        timeout=20,
    )
    if proc.returncode != 0:
        return []
    trovati = []
    for riga in proc.stdout.splitlines():
        parti = riga.split("|")
        if len(parti) < 3:
            continue
        nome = parti[1].strip()
        if not nome or nome == nostra:
            continue
        for voce in (v.strip() for v in parti[2].split(",")):
            estremi = _porta_host(voce)
            if estremi and estremi[0] <= hi and estremi[1] >= lo:
                trovati.append(nome)
                break
    return trovati


def _fingerprint(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


def _running_fingerprint(name: str) -> str | None:
    """Impronta con cui il container in piedi era stato avviato, se c'e'."""
    proc = _run_docker(
        [
            "inspect",
            "--format", "{{ index .Config.Labels " + f'"{FINGERPRINT_LABEL}"' + " }}",
            name,
        ],
        timeout=20,
    )
    if proc.returncode != 0:
        return None
    valore = proc.stdout.strip()
    # Docker stampa '<no value>' per un'etichetta assente: e' un container di
    # una versione precedente, senza impronta. Trattarlo come "diversa" lo fa
    # ricreare una volta sola, e da li' in poi ce l'ha.
    return valore if valore and valore != "<no value>" else None


def port_range(base: int, count: int) -> tuple[int, int] | None:
    """Estremi (inclusi) delle porte da pubblicare, o None se disattivate."""
    base = int(base or 0)
    count = int(count or 0)
    if base <= 0 or count <= 0:
        return None
    return base, base + count - 1


def ensure_container(
    workspace: str | Path,
    *,
    image: str = DEFAULT_IMAGE,
    network: bool = True,
    ports: tuple[int, int] | None = None,
) -> str:
    """Restituisce il nome del container pronto all'uso, avviandolo se serve.

    ``ports`` pubblica un intervallo su 127.0.0.1: serve a far vedere nel
    browser quello che l'agente avvia dentro la sandbox. Cambiare intervallo
    **ricrea** il container: gli argomenti di ``docker run`` si applicano solo
    alla creazione, e riusare quello vecchio darebbe una porta configurata che
    non risponde e nessun errore da nessuna parte.
    """
    workspace_path = Path(workspace).resolve()
    if not workspace_path.is_dir():
        raise SandboxError(f"La cartella di lavoro non esiste: {workspace_path}")

    name = container_name(workspace_path)
    impronta = _fingerprint(image, network, ports, MEM_LIMIT, PIDS_LIMIT)

    # Memo cortissima: "questo container, con questa impronta, girava un
    # istante fa".
    #
    # Ogni operazione della sandbox chiama ``ensure_container``, e ognuna
    # costava due processi ``docker`` (``ps`` e ``inspect``) prima ancora di
    # fare qualcosa. Il caso peggiore misurato e' ``_attendi_stato``, che
    # ripete fino a quaranta giri a 0,3 s: un solo ``preview action='serve'``
    # avviava circa centoventi processi docker per aspettare che una porta
    # rispondesse. Tre secondi bastano: piu' corti di qualunque operazione
    # utile, piu' lunghi di un giro di polling.
    adesso = time.monotonic()
    fresco = _CONTAINER_OK.get(name)
    if fresco is not None and fresco[0] == impronta and adesso - fresco[1] < _CONTAINER_OK_TTL_S:
        return name

    if _is_running(name):
        if _running_fingerprint(name) == impronta:
            _CONTAINER_OK[name] = (impronta, adesso)
            return name
        # Gli argomenti sono cambiati: il container va rifatto. E' senza stato
        # -- il lavoro vive nel volume del workspace -- quindi non si perde
        # niente, a parte i processi in background, che vanno riavviati.
        _remove(name)
    else:
        # Un container fermo con lo stesso nome (riavvio della macchina, crash)
        # bloccherebbe la creazione: si butta e si rifa'.
        _remove(name)

    args = [
        "run", "--detach", "--name", name,
        "--label", f"{LABEL}=1",
        "--label", f"{FINGERPRINT_LABEL}={impronta}",
        "--workdir", WORKDIR,
        "--volume", f"{workspace_path}:{WORKDIR}",
        "--memory", MEM_LIMIT,
        "--pids-limit", PIDS_LIMIT,
        # Nessun privilegio in piu' di quelli necessari a compilare ed eseguire.
        "--security-opt", "no-new-privileges",
        "--cap-drop", "ALL",
        # Manca il ``--user``, e non e' una dimenticanza.
        #
        # Il container gira come root. Su Linux questo lascia di root i file
        # che l'agente crea nel workspace montato -- fastidioso, e il motivo
        # per cui la voce e' segnata nell'audit. Ma passare l'uid dell'host
        # (``--user 1000:1000``) cambia il container in un modo che non si
        # ripaga: quell'uid dentro l'immagine non ha una voce in /etc/passwd
        # ne' una home scrivibile, e soprattutto ``pip install`` -- che
        # l'agente usa davvero, dentro un turno -- fallisce con un errore di
        # permessi su /usr/lib/python3. Si scambierebbe un fastidio sui
        # permessi dei file con un tool che smette di funzionare a meta'
        # lavoro. Farlo bene vuol dire home scrivibile, PIP_USER e un venv
        # nell'immagine, e va provato su un host Linux vero: finche' non lo
        # e' stato, root con ``--cap-drop ALL`` e' la scelta piu' onesta.
    ]
    if ports and network:
        lo, hi = ports
        args += ["--publish", f"{PORT_BIND_HOST}:{lo}-{hi}:{lo}-{hi}"]
    if not network:
        # Senza rete non ci sono porte da pubblicare: chiederle sarebbe un
        # errore di docker, e silenziarlo lascerebbe l'utente a chiedersi
        # perche' l'anteprima non parte.
        args += ["--network", "none"]
    args += [image, "sleep", "infinity"]

    if ports and network:
        lo, hi = ports
        for ostacolo in _port_bind_conflittuali(name, lo, hi):
            # Un container dell'harness di un ALTRO workspace tiene le nostre
            # porte (il suo server non e' mai stato fermato). Senza questo
            # passaggio ``docker run`` fallisce con "port is already allocated"
            # e l'agente resta appeso a un errore che non capisce: da qui non
            # si puo' neppure vedere chi ce l'ha, perche' il proprio shell gira
            # dentro un container che non parte. Si rimuovono solo quelli con
            # la nostra etichetta: mai i contenitori di qualcun altro.
            _remove(ostacolo)

    proc = _run_docker(args, timeout=300)     # il primo avvio puo' scaricare l'immagine
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise SandboxError(f"Avvio del container fallito: {detail}")
    return name


def run(
    command: str,
    workspace: str | Path,
    *,
    timeout_s: int = 120,
    image: str = DEFAULT_IMAGE,
    network: bool = True,
    ports: tuple[int, int] | None = None,
) -> CommandResult:
    """Esegue ``command`` nel container del workspace.

    ``ports`` viene propagato a ``ensure_container`` anche qui, e non solo
    dall'anteprima: se un comando qualsiasi creasse il container senza porte,
    il primo tentativo di anteprima lo troverebbe gia' in piedi con l'impronta
    sbagliata e lo ricreerebbe, uccidendo quello che ci stava girando dentro.
    """
    name = ensure_container(workspace, image=image, network=network, ports=ports)

    # ``timeout`` ferma il processo dentro il container; il margine esterno
    # serve solo a non restare appesi se e' docker stesso a non rispondere.
    #
    # Il comando va dato a ``timeout`` dentro una shell, non come argomenti
    # nudi. Scritto ``timeout N <comando>``, timeout cerca un **eseguibile**:
    #   * `cd /work && pytest` diventava `timeout N cd ...` e falliva con
    #     "failed to run command 'cd'", exit 127 -- perche' `cd` e' una
    #     builtin, non un binario. Un `cd` all'inizio del comando e' la cosa
    #     piu' normale del mondo, e l'agente si prendeva un errore
    #     incomprensibile;
    #   * in `timeout N a && b` il timeout copriva solo `a`, quindi la seconda
    #     meta' di un comando composto girava senza limite.
    # Con ``sh -c`` la shell interna gestisce builtin, pipe e `&&`, e il
    # timeout vale per tutto l'insieme.
    wrapped = (
        f"timeout --signal=TERM --kill-after=5 {int(timeout_s)} "
        f"/bin/sh -c {shlex.quote(command)}"
    )
    # La radice del workspace sul path di import. Senza, uno script eseguito da
    # una sottocartella -- il caso tipico e' una prova in `.analisi/` -- non
    # riesce a importare i moduli del progetto, perche' Python mette in testa a
    # sys.path la cartella *dello script*, non quella da cui l'hai lanciato.
    # E' anche cio' che `python -m pytest` fa gia' per conto suo: qui si rende
    # la stessa cosa vera per qualunque comando.
    proc = _run_docker(
        [
            "exec",
            "--workdir", WORKDIR,
            "--env", f"PYTHONPATH={WORKDIR}",
            name, "/bin/sh", "-lc", wrapped,
        ],
        timeout=timeout_s + 30,
    )
    # 124 e' la convenzione di coreutils per "scaduto il tempo".
    return CommandResult(
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        timed_out=proc.returncode == 124,
    )


# --- processi in background --------------------------------------------------
#
# ``run`` avvolge tutto in ``timeout``: e' quello che serve a un comando che
# deve finire, ed e' esattamente sbagliato per un server, che non finisce mai.
# Un `uvicorn` lanciato con run_command veniva ucciso allo scadere e il modello
# leggeva "esito: FALLITO, scaduto il tempo" su un processo che stava
# funzionando benissimo.
#
# Qui il processo viene messo in background **dentro il container** e il suo
# log finisce in /tmp del container, non nel workspace: gli scarti di un server
# di prova non devono comparire fra i file del progetto, ne' finire in un
# commit.
_BG_DIR = "/tmp/harness-preview"


def _bg_paths(slot: str) -> tuple[str, str]:
    return f"{_BG_DIR}/{slot}.log", f"{_BG_DIR}/{slot}.pid"


# Marchi di "processo in background vivo", sul lato host e fuori dal
# workspace. Servono a rispondere alla domanda "c'e' qualcosa da fermare?" con
# uno sguardo a un file invece che con un exec dentro il container: era quella
# la voce che costava secondi a ogni cambio di conversazione, e quando non
# c'era niente da fermare si ricreava per intero la sandbox solo per verificare.
# Scrive ``start_background``; cancella chi scopre che lo slot e' davvero vuoto.
def _live_dir() -> Path:
    """Dove vivono i marchi. ``HARNESS_BG_LIVE`` vince: e' cosi' che la suite
    li tiene nella propria ``tmp_path`` invece che nella home dell'utente.

    Il posto giusto dipende dal sistema: su Windows e' ``%LOCALAPPDATA%``,
    non un ``~/.local/share`` preso in prestito da POSIX -- che li' e' solo
    una cartella nascosta che nessuno pulisce mai.
    """
    personalizzato = os.environ.get("HARNESS_BG_LIVE")
    if personalizzato:
        return Path(personalizzato)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "harness-sandbox-bg"
    return Path.home() / ".local" / "share" / "harness-sandbox-bg"


def dimentica_marchi_vivi() -> int:
    """Cancella tutti i marchi. Ritorna quanti ne ha tolti.

    La chiama il server all'avvio: un marchio sopravvive al processo che l'ha
    scritto, ma i container no. Dopo un riavvio -- o dopo un crash -- ogni
    marchio rimasto e' una bugia che costa un giro di Docker al primo cambio
    di conversazione, per andare a fermare un processo che non esiste piu'.
    """
    tolti = 0
    try:
        for marchio in _live_dir().glob("*.live"):
            try:
                marchio.unlink()
                tolti += 1
            except OSError:
                pass
    except OSError:
        return tolti
    return tolti


def _ws_digest(workspace: str | Path) -> str:
    return hashlib.sha256(str(Path(workspace).resolve()).encode("utf-8")).hexdigest()[:16]


def _mark_file(workspace: str | Path, slot: str) -> Path:
    return _live_dir() / f"{_ws_digest(workspace)}-{slot}.live"


def _set_live_mark(workspace: str | Path, slot: str) -> None:
    try:
        percorso = _mark_file(workspace, slot)
        percorso.parent.mkdir(parents=True, exist_ok=True)
        percorso.write_text("1", encoding="utf-8")
    except OSError:
        pass  # il marchio e' solo un'ottimizzazione: perderlo non deve rompere l'avvio


def _clear_live_mark(workspace: str | Path, slot: str) -> None:
    try:
        _mark_file(workspace, slot).unlink(missing_ok=True)
    except OSError:
        pass


def background_live(workspace: str | Path) -> bool:
    """Almeno un'app lanciata dall'harness e' marcata viva nella sandbox.

    Zero costo, nessun docker: serve a decidere se un cambio di chat ha davvero
    qualcosa da fermare. Un container residuo (riavvio del server) non lo vede
    qui, ma il chiamante puo' ancora spazzarlo con ``stop``, che costa una sola
    verifica quando c'e' gia' tutto pulito.
    """
    try:
        return bool(list(_live_dir().glob(f"{_ws_digest(workspace)}-*.live")))
    except OSError:
        return False


# --- schermo e terminale ------------------------------------------------------
#
# Il pannello di anteprima sa mostrare una cosa sola: un iframe su una porta
# pubblicata. Quindi un'applicazione con una finestra e una shell diventano
# entrambe "un server HTTP su una porta": Xvfb + x11vnc + noVNC per la prima,
# ttyd per la seconda. Il resto dell'harness -- attesa della porta, log,
# spegnimento, orfani -- non deve sapere che esistono.
GUI_DISPLAY = ":99"
GUI_VNC_PORT = 5900            # solo dentro il container, mai pubblicata
GUI_GEOMETRY = "1280x800x24"
NOVNC_DIR = "/usr/share/novnc"
# La pagina ridotta di noVNC, non `vnc.html`: quella piena tiene le proprie
# impostazioni in localStorage, e in un iframe con origine opaca localStorage
# solleva un'eccezione.
NOVNC_PAGE = "/vnc_lite.html?autoconnect=true&resize=scale"
FEATURES_LABEL = "harness-sandbox-features"


def terminal_command(port: int, *, shell: str = "bash") -> str:
    """Una shell nel browser, sulla porta indicata.

    ``-W`` la rende scrivibile: senza, si guarda e basta. Non serve dirgli di
    ascoltare su 0.0.0.0 -- e' il suo default, ed e' cio' che evita la trappola
    del processo legato a 127.0.0.1 dentro il container.
    """
    return f"ttyd -W -p {int(port)} {shell}"


def gui_command(command: str, port: int, *, geometry: str = GUI_GEOMETRY) -> str:
    """Un'applicazione con finestra, vista dal browser.

    Quattro processi in catena: lo schermo finto, il server VNC che lo legge,
    il ponte websocket che serve noVNC sulla porta pubblicata, e finalmente il
    comando dell'utente con DISPLAY puntato li'. Separatori ``;`` e non ``&&``
    per la stessa ragione di ``start_background``, e attese brevi perche' ogni
    anello ha bisogno del precedente: x11vnc su un display che non c'e' ancora
    esce subito.

    Non c'e' nessun window manager: la finestra compare in alto a sinistra e
    non si sposta ne' si ridimensiona. E' una scelta -- un WM sarebbero altri
    pacchetti e un'altra cosa che puo' rompersi -- e il costo e' un bordo di
    scrivania vuota attorno alle finestre piccole.
    """
    display = GUI_DISPLAY
    return (
        f"Xvfb {display} -screen 0 {geometry} -nolisten tcp & "
        f"sleep 1 ; "
        f"x11vnc -display {display} -forever -shared -nopw -quiet "
        f"-rfbport {GUI_VNC_PORT} -localhost & "
        f"sleep 1 ; "
        f"websockify --web={NOVNC_DIR} {int(port)} localhost:{GUI_VNC_PORT} & "
        f"sleep 0.5 ; "
        f"DISPLAY={display} {command}"
    )


def image_features(tag: str) -> set[str]:
    """Cosa dichiara di saper fare l'immagine (etichetta del Dockerfile).

    Serve a distinguere "non funziona" da "questa immagine e' di prima": senza,
    chiedere un terminale a un'immagine costruita la settimana scorsa darebbe
    un 'ttyd: command not found' dentro un log, cioe' il modo peggiore di
    scoprire che basta ricostruirla.
    """
    proc = _run_docker(
        [
            "image", "inspect",
            "--format", "{{ index .Config.Labels " + f'"{FEATURES_LABEL}"' + " }}",
            tag,
        ],
        timeout=20,
    )
    if proc.returncode != 0:
        return set()
    valore = proc.stdout.strip()
    if not valore or valore == "<no value>":
        return set()
    return {pezzo.strip() for pezzo in valore.split(",") if pezzo.strip()}


def start_background(
    command: str,
    workspace: str | Path,
    *,
    slot: str = "app",
    image: str = DEFAULT_IMAGE,
    network: bool = True,
    ports: tuple[int, int] | None = None,
) -> str:
    """Avvia ``command`` nel container e ritorna subito. Restituisce il PID.

    Il comando non viene passato a ``docker exec --detach`` cosi' com'e':
    ``--detach`` non restituisce nessun PID, e senza PID non si puo' ne'
    fermare il processo ne' sapere se e' ancora vivo. Si usa invece una shell
    che mette in background, scrive il proprio PID e esce -- ``docker exec``
    torna comunque subito, ma con un identificatore in mano.
    """
    name = ensure_container(workspace, image=image, network=network, ports=ports)
    log_path, pid_path = _bg_paths(slot)

    stop_background(workspace, slot=slot, image=image, network=network, ports=ports)

    # I separatori sono `;` e non `&&`, e non e' un dettaglio di stile: e' il
    # bug che ha rotto la prima anteprima vera.
    #
    # In `A && B && C & D` la `&` mette in background **l'intera catena**
    # `A && B && C`, e `D` parte subito in primo piano. Quindi `echo $! >
    # pidfile` girava *prima* che il `mkdir -p` in background avesse creato la
    # cartella, e falliva con "Directory nonexistent" -- mentre il server, in
    # background, partiva davvero. Risultato peggiore del semplice errore: un
    # processo vivo, in ascolto sulla porta, e senza pid registrato, quindi
    # impossibile da fermare. Ogni tentativo successivo trovava la porta
    # occupata da un orfano che nessuno poteva vedere.
    #
    # Con `;` il mkdir e il cd finiscono in primo piano, la `&` si applica al
    # solo `nohup`, e `$!` e' davvero il pid del server.
    wrapped = (
        f"mkdir -p {_BG_DIR}; "
        f"cd {WORKDIR}; "
        f"nohup /bin/sh -c {shlex.quote(command)} > {log_path} 2>&1 & "
        f"echo $! > {pid_path}"
    )
    proc = _run_docker(
        [
            "exec",
            "--workdir", WORKDIR,
            "--env", f"PYTHONPATH={WORKDIR}",
            name, "/bin/sh", "-lc", wrapped,
        ],
        timeout=30,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise SandboxError(f"Avvio in background fallito: {detail}")

    letto = _run_docker(["exec", name, "/bin/sh", "-lc", f"cat {pid_path}"], timeout=15)
    pid = letto.stdout.strip()
    if pid:
        # Da questo momento un cambio di chat sa che c'e' qualcosa da fermare
        # senza doverlo andarsi a cercare dentro il container.
        _set_live_mark(workspace, slot)
    return pid


def background_log(
    workspace: str | Path,
    *,
    slot: str = "app",
    lines: int = 40,
    image: str = DEFAULT_IMAGE,
    network: bool = True,
    ports: tuple[int, int] | None = None,
) -> str:
    """Ultime righe del log del processo. Vuoto se non c'e' niente.

    E' la meta' importante della funzione: quando l'anteprima non risponde, la
    domanda vera non e' "e' partito?" ma "cosa ha detto mentre moriva". Senza
    il log, il modello vede un iframe bianco e comincia a indovinare.
    """
    try:
        name = ensure_container(workspace, image=image, network=network, ports=ports)
    except SandboxError:
        return ""
    log_path, _ = _bg_paths(slot)
    proc = _run_docker(
        ["exec", name, "/bin/sh", "-lc", f"tail -n {int(lines)} {log_path} 2>/dev/null"],
        timeout=15,
    )
    return proc.stdout or ""


def background_alive(
    workspace: str | Path,
    *,
    slot: str = "app",
    image: str = DEFAULT_IMAGE,
    network: bool = True,
    ports: tuple[int, int] | None = None,
) -> bool:
    try:
        name = ensure_container(workspace, image=image, network=network, ports=ports)
    except SandboxError:
        return False
    _, pid_path = _bg_paths(slot)
    proc = _run_docker(
        [
            "exec", name, "/bin/sh", "-lc",
            f"kill -0 $(cat {pid_path} 2>/dev/null) 2>/dev/null && echo vivo",
        ],
        timeout=15,
    )
    return "vivo" in (proc.stdout or "")


# Stati possibili di una porta **dentro** il container.
PORT_FREE = "libera"
PORT_OPEN = "aperta"          # in ascolto su 0.0.0.0: raggiungibile da fuori
PORT_LOCAL_ONLY = "solo_locale"  # in ascolto su 127.0.0.1: invisibile da fuori


def port_state(
    workspace: str | Path,
    port: int,
    *,
    image: str = DEFAULT_IMAGE,
    network: bool = True,
    ports: tuple[int, int] | None = None,
) -> str:
    """Chi ascolta sulla porta, visto **da dentro** il container.

    Perche' non si guarda da fuori
    ------------------------------
    Questa funzione ha sostituito un controllo TCP fatto dall'host, che era
    sbagliato in un modo particolarmente cattivo: **sempre positivo**. Quando
    una porta e' pubblicata con ``-p``, Docker mette in ascolto un proprio
    proxy sull'host per tutta la vita del container, che dentro ci sia
    qualcosa o no. Una connessione da 127.0.0.1 riesce comunque, quindi:

    * il controllo "e' libera?" rispondeva *occupata* per ogni porta
      dell'intervallo, sempre. Osservato dal vivo: l'agente ha provato 8200,
      8201 e 8202 e se l'e' sentite dire occupate tutte e tre;
    * il controllo di salute dopo l'avvio rispondeva *funziona* all'istante,
      anche con il server morto. Il peggiore dei due errori, perche' silenzioso.

    ``/proc/net/tcp`` invece dice la verita' e in piu' dice **su quale
    indirizzo**: ``00000000`` e' 0.0.0.0 (raggiungibile dal browser),
    ``0100007F`` e' 127.0.0.1 (il classico errore da dentro un container). Si
    passa da /proc e non da ``ss``/``netstat``/``lsof`` perche' in
    ``python:*-slim`` nessuno dei tre esiste.
    """
    try:
        name = ensure_container(workspace, image=image, network=network, ports=ports)
    except SandboxError:
        return PORT_FREE
    esadecimale = f"{int(port):04X}"
    script = (
        # I socket in ascolto hanno stato 0A e indirizzo remoto tutto zeri.
        f'if grep -qiE "^[[:space:]]*[0-9]+: 00000000:{esadecimale} '
        '[0-9A-F]+:0000 0A " /proc/net/tcp 2>/dev/null; then echo APERTA; '
        f'elif grep -qiE ":{esadecimale} [0-9A-F]+:0000 0A " '
        "/proc/net/tcp /proc/net/tcp6 2>/dev/null; then echo LOCALE; "
        "else echo LIBERA; fi"
    )
    proc = _run_docker(["exec", name, "/bin/sh", "-lc", script], timeout=15)
    uscita = (proc.stdout or "").strip()
    if "APERTA" in uscita:
        return PORT_OPEN
    if "LOCALE" in uscita:
        return PORT_LOCAL_ONLY
    return PORT_FREE


def kill_port_listener(
    workspace: str | Path,
    port: int,
    *,
    image: str = DEFAULT_IMAGE,
    network: bool = True,
    ports: tuple[int, int] | None = None,
) -> bool:
    """Uccide, dentro il container, i processi che nominano ``port``.

    E' il raccoglitore di orfani. Serve quando un'anteprima e' rimasta in piedi
    senza pid registrato -- succedeva per un bug di precedenza della shell, ma
    puo' capitare anche se l'harness viene chiuso di brutto: il container
    sopravvive, e con lui il server.

    Si passa da ``/proc`` e non da ``fuser``/``lsof``, che in ``python:*-slim``
    non ci sono. Il criterio e' grezzo -- la riga di comando contiene il numero
    di porta -- e va bene perche' l'ambito e' strettissimo: questo container
    serve un solo workspace e le porte pubblicate esistono solo per le
    anteprime. Fuori da quell'intervallo non si tocca niente.

    Il perimetro e' il **container**, non la macchina: lo script gira dentro,
    quindi "ogni processo la cui riga di comando contiene il numero" vuol dire
    ogni processo di questo workspace. Il ``websockify`` di un'altra anteprima
    che ci finisse dentro sarebbe di questo stesso workspace, ed e' proprio
    quello che va raccolto.
    """
    if not ports or not (ports[0] <= int(port) <= ports[1]):
        return False
    try:
        name = ensure_container(workspace, image=image, network=network, ports=ports)
    except SandboxError:
        return False
    script = (
        "ucciso=0; "
        "for p in /proc/[0-9]*; do "
        '  pid=${p#/proc/}; '
        '  [ "$pid" = 1 ] && continue; '
        # ``\|`` e' l'alternanza di grep BRE, ma per Python era una sequenza di
        # escape sconosciuta: il SyntaxWarning usciva ad ogni avvio del server,
        # in cima al log, e sembrava un errore dell'applicazione. La barra va
        # raddoppiata come le altre.
        f'  if tr "\\0" " " < $p/cmdline 2>/dev/null | grep -q " {int(port)}\\b\\|:{int(port)}\\b"; then '
        "    kill -9 $pid 2>/dev/null && ucciso=1; "
        "  fi; "
        "done; "
        "echo $ucciso"
    )
    proc = _run_docker(["exec", name, "/bin/sh", "-lc", script], timeout=20)
    return "1" in (proc.stdout or "")


def stop_background(
    workspace: str | Path,
    *,
    slot: str = "app",
    image: str = DEFAULT_IMAGE,
    network: bool = True,
    ports: tuple[int, int] | None = None,
) -> bool:
    """Ferma il processo dello slot. True se ce n'era uno da fermare.

    Si uccide il **gruppo di processi**, non il solo PID: un `npm run dev` o un
    `uvicorn --reload` avviano figli, e ammazzare il padre lascerebbe la porta
    occupata da un orfano -- con l'avvio successivo che fallisce con "address
    already in use" per un motivo invisibile.

    Si agisce solo se ``start_background`` ha lasciato un marchio sul lato host
    (o se il container risulta gia' vivo, come rete di sicurezza): altrimenti si
    tornerebbe a ricreare la sandbox da zero solo per verificare l'assenza, che
    era proprio la voce che rallentava ogni cambio di conversazione.
    """
    if not _mark_file(workspace, slot).exists():
        if not _is_running(container_name(workspace)):
            # Nessun marchio e nessun container: niente da fermare, zero docker.
            return False
        # Container senza marchio (versione precedente che non lo lasciava): si
        # uccide l'app con il solo exec e si spazzera' il contenitore a parte.
        name = container_name(workspace)
    else:
        try:
            name = ensure_container(
                workspace, image=image, network=network, ports=ports
            )
        except SandboxError:
            return False
    _, pid_path = _bg_paths(slot)
    proc = _run_docker(
        [
            "exec", name, "/bin/sh", "-lc",
            f"PID=$(cat {pid_path} 2>/dev/null); "
            f'[ -n "$PID" ] || exit 3; '
            'kill -TERM -"$PID" 2>/dev/null || kill -TERM "$PID" 2>/dev/null; '
            "sleep 0.3; "
            'kill -KILL -"$PID" 2>/dev/null || kill -KILL "$PID" 2>/dev/null; '
            f"rm -f {pid_path}; exit 0",
        ],
        timeout=20,
    )
    if proc.returncode in (0, 3):
        # Slot vuoto: kill eseguito oppure pidfile assente. Si cancella il
        # marchio affinche' i cambi di chat successivi non rifacciano questa
        # verifica a vuoto; con altri codi si conserva -- meglio ritentare una
        # volta in piu' che perdere un'app.
        _clear_live_mark(workspace, slot)
    return proc.returncode == 0


# --- immagine del progetto ---------------------------------------------------
# ``python:3.12-slim`` contiene Python e pip, e nient'altro: niente git, niente
# pytest, niente ruff, nessuna dipendenza del progetto. Il primo `pytest -q`
# dell'agente dentro la sandbox risponderebbe "not found", e sembrerebbe un
# bug dell'harness invece che un'immagine incompleta.
#
# La soluzione e' un Dockerfile nel workspace: si costruisce una volta, e da
# li' in poi il container ha gli stessi strumenti che ha l'utente.
SANDBOX_DOCKERFILE = "Dockerfile.sandbox"

DOCKERFILE_TEMPLATE = """\
# Immagine della sandbox in cui l'agente esegue i comandi.
# Costruiscila da Impostazioni -> Connessione -> "Costruisci l'immagine".
FROM python:3.12-slim

# git serve all'agente per diff, log e status; build-essential per i pacchetti
# Python che compilano codice nativo.
RUN apt-get update \\
 && apt-get install -y --no-install-recommends git build-essential \\
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir pytest ruff

# Le dipendenze del progetto, se ci sono.
#
# Il Dockerfile stesso e' il primo argomento di COPY di proposito: una COPY con
# soli glob **fallisce** se nessuno combacia ("no source files were
# specified"), e in un workspace senza requirements.txt l'immagine non si
# costruirebbe affatto. Con una sorgente che esiste sempre, i glob diventano
# facoltativi. Il '|| true' copre il caso in cui il file non c'e'.
COPY Dockerfile.sandbox requirements.txt* pyproject.toml* /tmp/deps/
RUN cd /tmp/deps \\
 && (pip install --no-cache-dir -r requirements.txt || true) \\
 && rm -rf /tmp/deps

# --- schermo e terminale nel browser ----------------------------------------
#
# Servono a far vedere nel pannello di anteprima due cose che non parlano HTTP:
# un'applicazione con una finestra (tkinter, pygame, Qt) e una shell. La strada
# e' la stessa per tutte e due -- farle parlare HTTP su una porta pubblicata --
# perche' e' l'unica cosa che il pannello sa mostrare.
#
# libtk8.6/libtcl8.6 e non python3-tk: l'immagine slim **compila** _tkinter
# (tk-dev e' fra le dipendenze di build del Dockerfile ufficiale), ma poi la
# riga che rimarca le librerie da tenere esclude apposta tkinter
# (`-not -name '*tkinter*'`), quindi le librerie a run time vengono rimosse.
# Qui si rimettono solo quelle. python3-tk installerebbe invece tkinter per il
# python di Debian, che non e' quello dell'immagine.
RUN apt-get update \\
 && apt-get install -y --no-install-recommends \\
      xvfb x11vnc novnc websockify libtk8.6 libtcl8.6 \\
 && rm -rf /var/lib/apt/lists/*

# ttyd: se la distribuzione lo ha, si prende quello; altrimenti il binario
# statico ufficiale. Non si da' per scontato ne' l'uno ne' l'altro.
#
# Il ripiego scarica un eseguibile da internet e lo rende eseguibile **dentro
# l'immagine in cui gira l'agente**: e' il punto piu' delicato di tutto il
# Dockerfile, e senza verifica ci si fida di chiunque stia in mezzo alla
# connessione al momento della build. Gli SHA-256 qui sotto sono quelli dei
# due artefatti della release 1.7.7, calcolati sui file veri il 31/08/2026.
# Se cambia la versione vanno ricalcolati: la build fallisce rumorosamente,
# che e' esattamente cio' che deve fare.
RUN (apt-get update && apt-get install -y --no-install-recommends ttyd \\
     && rm -rf /var/lib/apt/lists/*) \\
 || ( arch="$(dpkg --print-architecture)" \\
      && case "$arch" in \\
           amd64) f=ttyd.x86_64; \\
                  sha=8a217c968aba172e0dbf3f34447218dc015bc4d5e59bf51db2f2cd12b7be4f55 ;; \\
           arm64) f=ttyd.aarch64; \\
                  sha=b38acadd89d1d396a0f5649aa52c539edbad07f4bc7348b27b4f4b7219dd4165 ;; \\
           *) echo "architettura $arch senza binario ttyd" >&2; exit 1 ;; esac \\
      && apt-get update && apt-get install -y --no-install-recommends wget \\
      && wget -qO /tmp/ttyd \\
         "https://github.com/tsl0922/ttyd/releases/download/1.7.7/$f" \\
      && echo "$sha  /tmp/ttyd" | sha256sum -c - \\
      && install -m 0755 /tmp/ttyd /usr/local/bin/ttyd \\
      && rm -f /tmp/ttyd \\
      && rm -rf /var/lib/apt/lists/* )

# Verifica, non dichiarazione: se una di queste tre cose non c'e', a fallire e'
# la build -- adesso, con un messaggio -- e non l'agente fra due giorni con un
# "command not found" in mezzo a un turno.
RUN python -c "import tkinter" \\
 && ttyd --version \\
 && test -f /usr/share/novnc/vnc_lite.html \\
 && which Xvfb x11vnc websockify

LABEL harness-sandbox-features="gui,terminal"

WORKDIR /work
"""


# Il contesto di build e' **tutto il workspace** (``docker build ... <ws>``), e
# il Dockerfile ne copia tre file. Tutto il resto viene impacchettato e spedito
# al demone per essere buttato: su un progetto con ``node_modules`` o ``.venv``
# sono gigabyte, e la barra resta ferma su "invio del contesto" per minuti prima
# che la build cominci davvero.
#
# Il nome non e' ``.dockerignore`` di proposito. Docker cerca prima
# ``<nome-del-dockerfile>.dockerignore`` e solo dopo ``.dockerignore`` del
# contesto: cosi' questo file vale per **questa** build e non tocca i
# ``docker build`` che l'utente fa per conto suo nella stessa cartella --
# scriverne uno chiamato ``.dockerignore`` sarebbe entrare nel suo progetto.
# Sulle versioni che non conoscono quel nome il file viene semplicemente
# ignorato: si torna al comportamento di prima, non a una build rotta.
#
# Si escludono cartelle, non ``*``: chi personalizza il Dockerfile -- ed e'
# previsto, ``write_dockerfile`` non lo sovrascrive mai -- aggiunge le sue COPY
# e deve trovarcele. Queste cartelle in un contesto di build non servono a
# nessuno, neanche a lui.
SANDBOX_DOCKERIGNORE = f"{SANDBOX_DOCKERFILE}.dockerignore"

DOCKERIGNORE_TEMPLATE = """\
# Cosa NON spedire al demone Docker quando si costruisce l'immagine della
# sandbox. Vale solo per Dockerfile.sandbox: i tuoi build non lo leggono.
.git/
.hg/
.svn/
node_modules/
.venv/
venv/
env/
__pycache__/
*.pyc
.mypy_cache/
.pytest_cache/
.ruff_cache/
.tox/
.cache/
dist/
build/
target/
.next/
.nuxt/
.gradle/
.idea/
.vscode/
"""


def dockerfile_path(workspace: str | Path) -> Path:
    return Path(workspace).resolve() / SANDBOX_DOCKERFILE


def dockerignore_path(workspace: str | Path) -> Path:
    return Path(workspace).resolve() / SANDBOX_DOCKERIGNORE


def has_dockerfile(workspace: str | Path) -> bool:
    return dockerfile_path(workspace).is_file()


def scrivi_dockerignore(workspace: str | Path, *, overwrite: bool = False) -> Path:
    """Mette la lista delle esclusioni accanto al Dockerfile, se non c'e' gia'."""
    path = dockerignore_path(workspace)
    if overwrite or not path.exists():
        path.write_text(DOCKERIGNORE_TEMPLATE, encoding="utf-8")
    return path


def write_dockerfile(workspace: str | Path, *, overwrite: bool = False) -> Path:
    """Scrive il Dockerfile di partenza nel workspace, se non c'e' gia'.

    Insieme al suo ``.dockerignore``: sono una cosa sola, e un Dockerfile senza
    la sua lista di esclusioni e' proprio la build che impiega minuti a partire.
    """
    scrivi_dockerignore(workspace, overwrite=overwrite)
    path = dockerfile_path(workspace)
    if path.exists() and not overwrite:
        return path
    path.write_text(DOCKERFILE_TEMPLATE, encoding="utf-8")
    return path


IMAGE_SUFFIX = "-img"


def image_tag(workspace: str | Path) -> str:
    """Tag stabile per workspace, come per il nome del container."""
    return f"{container_name(workspace)}{IMAGE_SUFFIX}"


def is_harness_image(tag: str) -> bool:
    """Il tag e' un'immagine costruita da noi, per un workspace qualsiasi?

    Il confronto va fatto sulla **forma** e non con il tag del workspace
    corrente: cambiando cartella, l'immagine ancora selezionata e' quella del
    progetto precedente -- nostra a tutti gli effetti, ma diversa da questa.
    Confrontandola solo con il tag corrente sembrerebbe un'immagine scelta a
    mano dall'utente, e l'harness si tirerebbe indietro dal costruire proprio
    nel caso in cui serve di piu'.
    """
    tag = (tag or "").strip()
    return tag.startswith("agentic-harness-") and tag.endswith(IMAGE_SUFFIX)


def build_image(workspace: str | Path) -> tuple[str, str]:
    """Costruisce l'immagine dal Dockerfile del workspace. Ritorna (tag, log)."""
    workspace_path = Path(workspace).resolve()
    if not has_dockerfile(workspace_path):
        raise SandboxError(
            f"Nel workspace non c'e' un {SANDBOX_DOCKERFILE}: crealo prima di "
            "costruire l'immagine."
        )
    tag = image_tag(workspace_path)
    # Anche qui, e non solo in ``write_dockerfile``: un workspace preparato da
    # una versione precedente dell'harness ha il Dockerfile ma non le
    # esclusioni, e ``build_image`` non passa da li' -- si ferma prima, perche'
    # il Dockerfile c'e' gia'. E' il caso in cui la build lenta rimarrebbe
    # lenta per sempre.
    scrivi_dockerignore(workspace_path)
    proc = _run_docker(
        ["build", "-f", str(dockerfile_path(workspace_path)), "-t", tag, str(workspace_path)],
        timeout=900,        # la prima build scarica e compila: puo' essere lunga
    )
    log = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise SandboxError(f"Build dell'immagine fallita:\n{log[-2000:]}")
    # L'immagine e' cambiata: il container in piedi usa ancora quella vecchia.
    _remove(container_name(workspace_path))
    return tag, log[-2000:]


def stop(workspace: str | Path) -> bool:
    """Ferma e rimuove il container del workspace. True se ce n'era uno."""
    name = container_name(workspace)
    if not _is_running(name):
        return False
    _remove(name)
    return True


def status(workspace: str | Path) -> dict:
    """Stato leggibile per il pannello impostazioni."""
    available, detail = docker_available()
    name = container_name(workspace)
    return {
        "available": available,
        "detail": detail,
        "container": name,
        "running": available and _is_running(name),
        "workdir": WORKDIR,
        # Con l'immagine di serie mancano pytest, ruff e git: la UI lo dice
        # prima che sia l'agente a scoprirlo con un "command not found".
        "dockerfile": SANDBOX_DOCKERFILE if has_dockerfile(workspace) else None,
        "project_image": image_tag(workspace),
        # Costruita davvero, non solo "il Dockerfile c'e'". Sono due domande
        # diverse e finora la UI rispondeva alla prima credendo di rispondere
        # alla seconda.
        "image_built": available and image_exists(image_tag(workspace)),
    }
