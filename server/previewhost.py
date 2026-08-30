"""Il server che da' un'origine vera alle anteprime.

Perche' esiste un secondo server per mostrare un file
--------------------------------------------------------------------------
Fino alla v2.34 l'anteprima di un file era **un file solo**: la rotta
``/api/preview/file?path=...`` serviva quel byte-stream dentro un iframe.
Per un ``.md`` o un ``.png`` va benissimo. Per una pagina no, e il motivo e'
nell'indirizzo: un ``<link href="style.css">`` dentro una pagina servita da
``/api/preview/file`` si risolve in ``/api/preview/style.css``, che non esiste.
Quindi ogni sito scritto dall'agente si vedeva **senza foglio di stile e senza
script**: non "un po' diverso", proprio un'altra cosa.

La cura e' servire la **cartella**, non il file. Ma appena la si serve dalla
stessa porta dell'harness nasce il secondo problema, che e' di sicurezza:
quella pagina l'ha scritta il modello, e sulla nostra origine potrebbe
chiamare le nostre rotte e leggerne le risposte. Per questo l'iframe delle
anteprime di file era ``sandbox`` **senza** ``allow-same-origin``, cioe' con
origine opaca -- ed e' l'altra meta' del problema, perche' con origine opaca:

* ``localStorage`` e ``IndexedDB`` **sollevano un'eccezione** (non tornano
  vuoti: proprio buttano). Una pagina che salva lo stato si rompe alla prima
  riga;
* i moduli ES (``<script type="module">``) vengono presi con CORS e
  ``Origin: null``, e rifiutati;
* ogni ``fetch()`` verso il proprio backend e' cross-origin senza credenziali.

Le due cose insieme -- servire la cartella e avere un'origine vera -- si
ottengono in un modo solo che non apra le API dell'harness: **un'altra
origine**. Qui e' un altro server, su un'altra porta di 127.0.0.1, che sa fare
una cosa sola (leggere file sotto una radice) e non conosce nessuna rotta
dell'harness. La pagina ottiene ``allow-same-origin``, quindi ha storage,
moduli e fetch; e cio' che puo' raggiungere resta la sua cartella, perche' qui
dentro non c'e' nient'altro.

Una radice per volta
--------------------------------------------------------------------------
Il pannello mostra una cosa sola, quindi questo server ha una sola radice
attiva, montata su ``/``. Non e' pigrizia: e' l'unico modo perche' funzionino
**anche** i percorsi assoluti (``/css/app.css``), che sono la meta' dei casi
reali e che un prefisso tipo ``/r/<id>/`` romperebbe di nuovo. Il cambio di
radice lo fa l'harness prima di puntarci l'iframe.
"""

from __future__ import annotations

import mimetypes
import os
import socket
import threading
from dataclasses import dataclass, field
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, Response
from starlette.routing import Route

from core.tools import WorkspaceError, resolve_path

# Quante porte provare dopo quella chiesta prima di arrendersi. Serve al caso
# banale ma frequente: due harness aperti sulla stessa macchina.
TENTATIVI_PORTA = 10


@dataclass
class _Stato:
    radice: Path | None = None
    porta: int = 0
    server: uvicorn.Server | None = None
    lucchetto: threading.Lock = field(default_factory=threading.Lock)


_STATO = _Stato()


def radice_attuale() -> Path | None:
    return _STATO.radice


def set_root(path: str | Path) -> None:
    """Cambia la cartella servita su ``/``.

    Chi chiama ha gia' verificato che stia dentro il workspace: qui non si
    ricontrolla il **perimetro esterno**, si controlla solo -- ad ogni
    richiesta -- che non si esca da questa radice.
    """
    _STATO.radice = Path(path).resolve()


def origin() -> str | None:
    """L'origine da cui servire le anteprime, o None se il server non e' su."""
    return f"http://127.0.0.1:{_STATO.porta}" if _STATO.porta else None


def _apri_socket(base: int) -> socket.socket | None:
    """Un socket in ascolto su 127.0.0.1, dalla porta chiesta in avanti.

    Il socket lo apriamo noi e non uvicorn per un motivo pratico: cosi' la
    porta effettiva si conosce **prima** che il thread parta, e nessuno deve
    aspettare o indovinare quale sia.

    Niente ``SO_REUSEADDR`` su Windows, dove non significa quello che sembra:
    li' permette di rubare una porta a chi la sta gia' usando, e il sintomo
    sarebbe un'anteprima che scippa la porta a un altro programma.
    """
    for porta in range(base, base + TENTATIVI_PORTA):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name != "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", porta))
        except OSError:
            sock.close()
            continue
        sock.listen(64)
        return sock
    return None


def ensure_running(base_port: int = 8124) -> str | None:
    """Solleva il server se non c'e' gia' e ritorna la sua origine.

    Si accende **alla prima anteprima** e non all'avvio dell'harness: chi non
    apre mai una pagina non si ritrova una porta in ascolto che non ha chiesto,
    e la suite dei test non ne apre nessuna.
    """
    if base_port <= 0:
        return None
    with _STATO.lucchetto:
        if _STATO.server is not None and not _STATO.server.should_exit:
            return origin()
        sock = _apri_socket(int(base_port))
        if sock is None:
            return None
        _STATO.porta = sock.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(app, log_level="warning", timeout_graceful_shutdown=2)
        )
        _STATO.server = server

        def gira() -> None:
            # Un'eccezione in un thread demone stampa un traceback e basta:
            # allo spegnimento e' solo rumore.
            try:
                server.run(sockets=[sock])
            except (KeyboardInterrupt, SystemExit):
                pass

        threading.Thread(
            target=gira, daemon=True, name="anteprima-statica"
        ).start()
        return origin()


def shutdown() -> None:
    """Chiude il server, se e' su. Non deve mai alzare eccezione."""
    server, _STATO.server = _STATO.server, None
    _STATO.porta = 0
    if server is not None:
        server.should_exit = True


# ---------------------------------------------------------------------------
# L'applicazione: una rotta sola
# ---------------------------------------------------------------------------


async def servi(request: Request) -> Response:
    radice = _STATO.radice
    if radice is None:
        return PlainTextResponse("Nessuna anteprima attiva.", status_code=404)

    percorso = request.path_params.get("percorso") or "."
    try:
        # La stessa guardia dei tool, con la radice al posto del workspace:
        # ``..`` e percorsi assoluti vengono rifiutati qui, non piu' in la'.
        target = resolve_path(radice, percorso)
    except WorkspaceError:
        return PlainTextResponse("Fuori dalla radice dell'anteprima.", status_code=403)

    if target.is_dir():
        target = target / "index.html"
    if not target.is_file():
        return PlainTextResponse("File non trovato.", status_code=404)

    media, _ = mimetypes.guess_type(target.name)
    return FileResponse(
        target,
        media_type=media or "text/plain; charset=utf-8",
        headers={
            # Il tipo dichiarato e' quello che decide come viene interpretato:
            # su file scritti dal modello non si lascia indovinare al browser.
            "X-Content-Type-Options": "nosniff",
            # L'agente riscrive gli stessi file di continuo: una risposta in
            # cache qui vorrebbe dire guardare la versione di prima e non
            # capire perche' la correzione "non ha funzionato".
            "Cache-Control": "no-store",
        },
    )


# Nessun CORS, di proposito: da qualunque altra origine (l'harness compreso)
# questa roba si puo' chiedere ma non leggere.
app = Starlette(
    routes=[
        Route("/", servi, methods=["GET", "HEAD"]),
        Route("/{percorso:path}", servi, methods=["GET", "HEAD"]),
    ]
)
