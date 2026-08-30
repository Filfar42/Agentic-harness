"""Preparazione dell'ambiente: accendere Docker, costruire l'immagine.

Perche' esiste
--------------
L'harness dichiarava "Workspace pronto" appena la pagina si apriva, e quella
scritta era una speranza, non una verifica: Ollama poteva essere spento, Docker
poteva non essere avviato, l'immagine del progetto poteva non essere mai stata
costruita. Il primo messaggio dell'utente scopriva il problema al posto suo --
e lo scopriva nel modo peggiore, con un errore dentro una tendina a meta' turno.

Le due operazioni che servono a sistemare la cosa hanno lo stesso problema:
**durano troppo per una richiesta HTTP**. Docker Desktop ci mette dai venti ai
sessanta secondi ad accendersi, e la prima build dell'immagine puo' arrivare a
qualche minuto perche' scarica un'immagine di base e compila. Farle in modo
sincrono significherebbe una pagina bloccata su uno spinner senza sapere se sta
succedendo qualcosa.

Quindi: girano in un thread, e lo stato lo si legge quando si vuole. Non si usa
il meccanismo dei turni (``server/runner.py``) perche' quello e' legato a una
conversazione, mentre questi lavori sono dell'**applicazione**: valgono per
tutte le chat e devono sopravvivere al passaggio da una all'altra.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from core import sandbox as sandbox_mod

# Stati di un lavoro. 'idle' vuol dire "mai partito", ed e' diverso da 'ok':
# non aver mai provato ad accendere Docker non e' un successo.
IDLE = "idle"
RUNNING = "running"
OK = "ok"
ERROR = "error"


class Job:
    """Un lavoro lungo con stato leggibile da fuori.

    Volutamente minimale: niente coda, niente priorita', niente cancellazione.
    Sono due operazioni che l'utente lancia al massimo una volta per avvio, e
    un'infrastruttura piu' ricca sarebbe codice da mantenere per un problema
    che non c'e'.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._lock = threading.Lock()
        self.state = IDLE
        self.detail = ""
        self.log = ""
        self.updated_at = 0.0
        self._thread: threading.Thread | None = None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "name": self.name,
                "state": self.state,
                "detail": self.detail,
                "log": self.log[-4000:],
                "updated_at": self.updated_at,
            }

    @property
    def running(self) -> bool:
        with self._lock:
            return self.state == RUNNING

    def _set(self, state: str, detail: str = "", log: str = "") -> None:
        with self._lock:
            self.state = state
            self.detail = detail
            if log:
                self.log = log
            self.updated_at = time.time()

    def begin(self) -> None:
        """Marca il lavoro come in corso senza aprire un thread suo.

        Serve quando a farlo e' il thread di un altro lavoro: la preparazione
        automatica costruisce l'immagine dentro il thread del container,
        perche' il secondo deve aspettare il primo. Senza questo, chi guarda
        lo stato dell'immagine la vedrebbe 'idle' proprio mentre si costruisce
        -- ed e' la scritta piu' sbagliata possibile in quel momento.
        """
        self._set(RUNNING)

    def end(self, riuscito: bool, detail: str = "", log: str = "") -> None:
        """Chiude un lavoro aperto con ``begin``."""
        self._set(OK if riuscito else ERROR, detail, log)

    def start(self, work: Callable[[], tuple[bool, str, str]]) -> bool:
        """Lancia ``work`` in un thread. False se ne stava gia' girando uno.

        ``work`` ritorna (riuscito, dettaglio, log). Le eccezioni non sfuggono:
        un lavoro di preparazione che esplode deve raccontarlo nel suo stato,
        non far cadere il thread lasciando l'interfaccia su "in corso" per
        sempre.
        """
        with self._lock:
            if self.state == RUNNING:
                return False
            self.state = RUNNING
            self.detail = ""
            self.log = ""
            self.updated_at = time.time()

        def runner() -> None:
            try:
                riuscito, detail, log = work()
            except Exception as exc:  # noqa: BLE001
                self._set(ERROR, f"{type(exc).__name__}: {exc}")
                return
            self._set(OK if riuscito else ERROR, detail, log)

        self._thread = threading.Thread(target=runner, daemon=True)
        self._thread.start()
        return True


class Prep:
    """I lavori di preparazione dell'applicazione."""

    def __init__(self) -> None:
        self.docker = Job("docker")
        self.image = Job("image")
        self.container = Job("container")

    def snapshot(self) -> dict[str, Any]:
        return {
            "docker": self.docker.snapshot(),
            "image": self.image.snapshot(),
            "container": self.container.snapshot(),
        }

    # -- Docker ------------------------------------------------------------

    def start_docker(self, timeout_s: float = 120.0) -> bool:
        def work() -> tuple[bool, str, str]:
            riuscito, dettaglio = sandbox_mod.start_engine(timeout_s=timeout_s)
            return riuscito, dettaglio, ""

        return self.docker.start(work)

    # -- immagine ----------------------------------------------------------

    def build_image(self, workspace: str, *, on_done: Callable[[str], None] | None = None) -> bool:
        """Costruisce l'immagine del workspace, creando il Dockerfile se manca.

        ``on_done`` riceve il tag: serve al chiamante per **selezionare**
        l'immagine appena costruita nelle impostazioni. Costruirla e non usarla
        sarebbe il peggio dei due mondi -- si paga la build e i comandi
        continuano a girare sull'immagine di serie, senza pytest ne' git.
        """

        def work() -> tuple[bool, str, str]:
            pronto, dettaglio = sandbox_mod.docker_available()
            if not pronto:
                return False, f"Docker non e' pronto: {dettaglio}", ""
            sandbox_mod.write_dockerfile(workspace)      # non sovrascrive
            try:
                tag, log = sandbox_mod.build_image(workspace)
            except sandbox_mod.SandboxError as exc:
                return False, str(exc), ""
            if on_done:
                on_done(tag)
            return True, f"Immagine {tag} pronta.", log

        return self.image.start(work)


    # -- container ---------------------------------------------------------

    def ensure_container(
        self,
        workspace: str,
        *,
        image: str,
        network: bool,
        ports: tuple[int, int] | None = None,
    ) -> bool:
        """Crea il container del workspace se non c'e' gia'.

        Sta fra i lavori di preparazione e non fra i tool per una ragione di
        tempi: la prima ``docker run`` su un'immagine appena costruita puo'
        durare parecchi secondi, e finora quel tempo lo pagava il **primo
        comando** dell'agente -- cioe' l'utente lo vedeva come "il primo turno
        e' lento" senza sapere perche'.
        """

        def work() -> tuple[bool, str, str]:
            pronto, dettaglio = sandbox_mod.docker_available()
            if not pronto:
                return False, f"Docker non e' pronto: {dettaglio}", ""
            try:
                nome = sandbox_mod.ensure_container(
                    workspace, image=image, network=network, ports=ports
                )
            except sandbox_mod.SandboxError as exc:
                return False, str(exc), ""
            return True, f"Container {nome} pronto.", ""

        return self.container.start(work)

    # -- la catena intera --------------------------------------------------

    def prepara(
        self,
        workspace: str,
        *,
        image: str,
        network: bool,
        ports: tuple[int, int] | None = None,
        costruisci_immagine: bool = True,
        on_image: Callable[[str], None] | None = None,
    ) -> bool:
        """Immagine se manca, **poi** container. Tutto dentro un thread solo.

        Due cose, in quest'ordine e non nell'altro: il container si crea
        *dall'*immagine, quindi farlo prima vorrebbe dire crearlo da quella di
        serie e ritrovarselo vecchio appena la build finisce.

        E **la decisione sta qui dentro**, non nel chiamante. ``image_needed``
        fa un ``docker version`` e un ``docker images``: due sottoprocessi che
        prima si pagavano sul filo della richiesta HTTP -- cioe' dentro
        l'apertura di una conversazione -- per scoprire quasi sempre che non
        c'era niente da fare.
        """

        def work() -> tuple[bool, str, str]:
            pronto, dettaglio = sandbox_mod.docker_available()
            if not pronto:
                return False, f"Docker non e' pronto: {dettaglio}", ""

            tag = image
            log = ""
            if costruisci_immagine and image_needed(workspace, image):
                # Il lavoro dell'immagine si apre e si chiude da qui: gira in
                # questo thread, ma il suo stato deve leggersi dove tutti lo
                # cercano.
                self.image.begin()
                sandbox_mod.write_dockerfile(workspace)      # non sovrascrive
                try:
                    tag, log = sandbox_mod.build_image(workspace)
                except sandbox_mod.SandboxError as exc:
                    # Senza l'immagine il container non puo' nascere: si dice
                    # cos'e' andato storto invece di crearne uno da quella
                    # sbagliata.
                    self.image.end(False, str(exc))
                    return False, f"Immagine non costruita: {exc}", ""
                self.image.end(True, f"Immagine {tag} pronta.", log)
                if on_image:
                    on_image(tag)

            try:
                nome = sandbox_mod.ensure_container(
                    workspace, image=tag, network=network, ports=ports
                )
            except sandbox_mod.SandboxError as exc:
                return False, str(exc), log
            costruita = " (immagine ricostruita)" if log else ""
            return True, f"Container {nome} pronto{costruita}.", log

        return self.container.start(work)


def image_needed(workspace: str, current_image: str) -> bool:
    """C'e' un motivo per (ri)costruire l'immagine di questo workspace?

    Tre no che valgono piu' del si': se Docker non risponde non si costruisce
    niente (e lo dira' il check apposta), se l'immagine del progetto esiste gia'
    non si rifa' una build da minuti ad ogni cambio di cartella, e se l'utente
    ha scelto a mano un'immagine sua non gliela si scavalca.
    """
    pronto, _ = sandbox_mod.docker_available()
    if not pronto:
        return False
    tag = sandbox_mod.image_tag(workspace)
    if sandbox_mod.image_exists(tag):
        return False
    # Immagine personalizzata dall'utente: non e' quella di serie e non l'ha
    # costruita l'harness. Sono affari suoi.
    return (
        current_image in {sandbox_mod.DEFAULT_IMAGE, ""}
        or sandbox_mod.is_harness_image(current_image)
    )
