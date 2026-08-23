"""Esecuzione dei turni agentici in background, indipendente dalla connessione.

Il problema che risolve
-----------------------
Nella prima versione il turno girava *dentro* la richiesta HTTP che il browser
aveva aperto. Conseguenze:

  * cambiando conversazione il client smetteva di leggere lo stream e il turno
    moriva a meta';
  * lo stato del server (``STATE.session``) veniva cambiato sotto i piedi al
    turno in corso, che continuava a scrivere nella lista sbagliata;
  * niente sopravviveva a un reload della pagina.

Qui il turno e' un **lavoro con un'identita' propria**, legato al suo
``session_id`` e non alla connessione. Il browser si attacca e si stacca
liberamente: gli eventi finiscono in un buffer, e chi si (ri)collega riceve
prima tutto l'arretrato e poi il flusso dal vivo.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable, Iterator
from typing import Any

# Un lettore lento non deve bloccare il turno: oltre questa soglia i frame
# vengono scartati per quel singolo abbonato, che comunque ha gia' l'arretrato.
_SUBSCRIBER_QUEUE_MAX = 2000
_KEEPALIVE_S = 15.0


class TurnRunner:
    """Un turno agentico in esecuzione per una conversazione.

    Thread-safe: il worker produce, un numero qualunque di connessioni SSE
    consuma. Il buffer ``frames`` e' la memoria completa del turno, quindi
    riattaccarsi non perde nulla.
    """

    def __init__(self, session_id: str, snapshot: list[dict[str, Any]]) -> None:
        self.session_id = session_id
        # Cronologia com'era *prima* dell'inizio del turno: chi si ricollega
        # ridisegna questa e poi riapplica i frame, senza duplicati.
        self.snapshot = snapshot
        self.frames: list[str] = []
        self.finished = threading.Event()
        # Alzata dal tasto stop. Il worker la controlla nei punti sicuri: qui
        # non si uccide il thread, gli si chiede di fermarsi. Ammazzarlo di
        # forza lascerebbe file scritti a meta' e cronologia incoerente.
        self.cancelled = threading.Event()
        self.error: str | None = None
        self._subscribers: list[queue.Queue[str | None]] = []
        self._lock = threading.Lock()

    def cancel(self) -> None:
        self.cancelled.set()

    # -- produzione --------------------------------------------------------

    def emit(self, frame: str) -> None:
        with self._lock:
            self.frames.append(frame)
            subscribers = list(self._subscribers)
        for sub in subscribers:
            if sub.qsize() < _SUBSCRIBER_QUEUE_MAX:
                sub.put(frame)

    def close(self) -> None:
        self.finished.set()
        with self._lock:
            subscribers = list(self._subscribers)
        for sub in subscribers:
            sub.put(None)

    # -- consumo -----------------------------------------------------------

    def subscribe(self) -> tuple[list[str], queue.Queue[str | None]]:
        with self._lock:
            backlog = list(self.frames)
            sub: queue.Queue[str | None] = queue.Queue()
            self._subscribers.append(sub)
        return backlog, sub

    def unsubscribe(self, sub: queue.Queue[str | None]) -> None:
        with self._lock:
            if sub in self._subscribers:
                self._subscribers.remove(sub)

    def stream(self) -> Iterator[str]:
        """Arretrato completo, poi il flusso dal vivo fino alla fine."""
        backlog, sub = self.subscribe()
        try:
            yield from backlog
            if self.finished.is_set():
                return
            while True:
                try:
                    frame = sub.get(timeout=_KEEPALIVE_S)
                except queue.Empty:
                    if self.finished.is_set():
                        return
                    # Commento SSE: tiene viva la connessione senza toccare la UI.
                    yield ": keepalive\n\n"
                    continue
                if frame is None:
                    return
                yield frame
        finally:
            self.unsubscribe(sub)


class RunnerRegistry:
    """I turni in corso, uno per conversazione al massimo."""

    def __init__(self) -> None:
        self._runners: dict[str, TurnRunner] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str) -> TurnRunner | None:
        with self._lock:
            return self._runners.get(session_id)

    def is_running(self, session_id: str) -> bool:
        runner = self.get(session_id)
        return runner is not None and not runner.finished.is_set()

    def running_ids(self) -> set[str]:
        with self._lock:
            return {
                sid for sid, r in self._runners.items() if not r.finished.is_set()
            }

    def start(
        self,
        session_id: str,
        snapshot: list[dict[str, Any]],
        work: Callable[[TurnRunner], None],
    ) -> TurnRunner:
        """Avvia il worker. Solleva se un turno e' gia' in corso per la sessione."""
        with self._lock:
            existing = self._runners.get(session_id)
            if existing is not None and not existing.finished.is_set():
                raise RuntimeError("Un turno e' gia' in corso per questa conversazione.")
            runner = TurnRunner(session_id, snapshot)
            self._runners[session_id] = runner

        def target() -> None:
            try:
                work(runner)
            except Exception as exc:  # noqa: BLE001 - l'errore va mostrato
                runner.error = f"{type(exc).__name__}: {exc}"
            finally:
                runner.close()

        threading.Thread(target=target, daemon=True, name=f"turn-{session_id}").start()
        return runner
