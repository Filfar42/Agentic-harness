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

import json
import queue
import threading
from collections.abc import Callable, Iterator
from typing import Any

# Un lettore lento non deve bloccare il turno: oltre questa soglia i frame
# vengono scartati per quel singolo abbonato, che comunque ha gia' l'arretrato.
_SUBSCRIBER_QUEUE_MAX = 2000
_KEEPALIVE_S = 15.0


# ---------------------------------------------------------------------------
# Spegnimento
# ---------------------------------------------------------------------------
#
# Una risposta SSE non finisce: e' il suo mestiere. Ma uvicorn, quando riceve
# Ctrl+C, smette di accettare connessioni e poi **aspetta che le risposte in
# corso finiscano** -- e quelle non finiscono mai. Il risultato visto
# dall'utente e' che Ctrl+C non fa niente e bisogna chiudere la finestra del
# terminale: basta una scheda del browser aperta sulla UI, che tiene sempre
# aperto ``/api/events``.
#
# Qui c'e' il "sta chiudendo" che quelle risposte devono poter guardare. Chi
# tiene code di abbonati registra una sveglia: al momento dello spegnimento le
# code ricevono il colpetto che le fa uscire dal ``get()`` bloccante, invece di
# aspettare i quindici secondi del keepalive.
SPEGNIMENTO = threading.Event()
_SVEGLIE: list[Callable[[], None]] = []
_SVEGLIE_LOCK = threading.Lock()


def al_spegnimento(sveglia: Callable[[], None]) -> None:
    """Registra chi va svegliato quando il processo si ferma."""
    with _SVEGLIE_LOCK:
        _SVEGLIE.append(sveglia)


def annuncia_spegnimento() -> None:
    """Il processo si sta fermando: chi ha risposte aperte le chiuda.

    La chiama ``run.py`` dal gestore del segnale, **prima** che uvicorn si
    metta ad aspettare le connessioni. Deve essere veloce e non alzare mai
    eccezioni: gira dentro un handler di segnale.
    """
    SPEGNIMENTO.set()
    with _SVEGLIE_LOCK:
        sveglie = list(_SVEGLIE)
    for sveglia in sveglie:
        try:
            sveglia()
        except Exception:  # noqa: BLE001 - in un handler di segnale non si alza niente
            pass


def dimentica_spegnimento() -> None:
    """Riporta tutto a "non stiamo chiudendo". Serve ai test, che nello stesso
    processo alzano e riabbassano la bandiera piu' volte."""
    SPEGNIMENTO.clear()
    with _SVEGLIE_LOCK:
        _SVEGLIE.clear()


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
        self.stacca_gli_abbonati()

    def stacca_gli_abbonati(self) -> None:
        """Chiude le risposte SSE aperte su questo turno, senza fermarlo.

        Il turno vive nel suo thread e non c'entra con le connessioni: allo
        spegnimento si chiudono le risposte -- che altrimenti terrebbero in
        ostaggio uvicorn -- e il worker se ne accorge da solo.
        """
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

    def _residuo(self, sub: queue.Queue[str | None]) -> Iterator[str]:
        """Quello che e' rimasto in coda, senza aspettare un istante.

        Serve nei due punti in cui si esce perche' il turno risulta finito. Il
        turno puo' finire **fra** lo scatto dell'arretrato e quel controllo: i
        frame emessi in quella finestra sono gia' in questa coda -- si e' gia'
        abbonati -- e uscire senza guardarla li butta via. Fra loro c'e'
        ``done``, cioe' l'unico frame che dice al client che il turno e'
        chiuso: il telefono restava con la risposta a meta' e la rotella che
        gira. Si vedeva come un test intermittente, che e' il modo in cui una
        corsa si presenta prima di presentarsi come un difetto.
        """
        while True:
            try:
                frame = sub.get_nowait()
            except queue.Empty:
                return
            if frame is None:
                return
            yield frame

    def stream(self) -> Iterator[str]:
        """Arretrato completo, poi il flusso dal vivo fino alla fine."""
        backlog, sub = self.subscribe()
        try:
            yield from backlog
            if self.finished.is_set():
                yield from self._residuo(sub)
                return
            while not SPEGNIMENTO.is_set():
                try:
                    frame = sub.get(timeout=_KEEPALIVE_S)
                except queue.Empty:
                    if self.finished.is_set() or SPEGNIMENTO.is_set():
                        yield from self._residuo(sub)
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

    # Quanti turni **finiti** restano ricordati. Vedi ``_pota``.
    MAX_FINITI = 24

    def __init__(self) -> None:
        self._runners: dict[str, TurnRunner] = {}
        self._lock = threading.Lock()
        # Allo spegnimento gli stream dei turni vanno chiusi come tutti gli
        # altri: un turno lungo terrebbe aperta la sua risposta, e uvicorn
        # aspetterebbe lui invece del Ctrl+C.
        al_spegnimento(self._sveglia_gli_stream)

    def _sveglia_gli_stream(self) -> None:
        with self._lock:
            runners = list(self._runners.values())
        for runner in runners:
            runner.stacca_gli_abbonati()

    def _pota(self) -> None:
        """Dimentica i turni finiti piu' vecchi. Va chiamata col lock preso.

        Il registro non veniva mai potato: ogni conversazione toccata nel
        processo lasciava per sempre il suo ``TurnRunner``, e ognuno tiene lo
        snapshot della cronologia **piu' tutti i frame del turno** -- che su un
        turno lungo sono megabyte. Un'app tenuta aperta per giorni li
        accumulava tutti.

        Si tiene una coda generosa: riaprire una chat finita e rivederne lo
        svolgimento e' il motivo per cui i frame esistono, e ``MAX_FINITI`` e'
        molto piu' di quante conversazioni si guardino in una sessione.
        """
        finiti = [
            sid for sid, r in self._runners.items() if r.finished.is_set()
        ]
        if len(finiti) <= self.MAX_FINITI:
            return
        # I dizionari conservano l'ordine di inserimento: i primi sono i piu'
        # vecchi, e sono quelli che si lasciano andare.
        for sid in finiti[: len(finiti) - self.MAX_FINITI]:
            self._runners.pop(sid, None)

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
            self._pota()

        def target() -> None:
            try:
                work(runner)
            except Exception as exc:  # noqa: BLE001 - l'errore va mostrato
                runner.error = f"{type(exc).__name__}: {exc}"
                # ...e va mostrato **davvero**. Prima l'attributo veniva
                # valorizzato e nessuno lo emetteva: un'eccezione nel worker
                # chiudeva lo stream in silenzio, e l'utente vedeva il turno
                # smettere senza una riga che dicesse perche'. Il frame passa
                # dalla stessa strada di tutti gli altri, quindi finisce
                # nell'arretrato e chi si riaggancia lo rivede.
                runner.emit(
                    "data: "
                    + json.dumps(
                        {
                            "type": "error",
                            "message": (
                                "Il turno si e' interrotto per un guasto "
                                f"dell'harness: {runner.error}"
                            ),
                        },
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )
            finally:
                runner.close()

        threading.Thread(target=target, daemon=True, name=f"turn-{session_id}").start()
        return runner
