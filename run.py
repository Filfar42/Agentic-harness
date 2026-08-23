"""Avvio dell'harness.

    python run.py                        # http://127.0.0.1:8123
    python run.py --port 9000 --reload
    python run.py --mobile               # + interfaccia mobile su 0.0.0.0:8200
    python run.py --mobile --mobile-port 8210

Con ``--mobile`` lo stesso comando solleva anche il ponte del telefono
(``server/mobile.py``, bind 0.0.0.0 per la LAN): un solo processo da lanciare,
due interfacce sincronizzate.
"""

from __future__ import annotations

import argparse
import os
import threading
import webbrowser
from threading import Timer
from types import FrameType

import uvicorn
from uvicorn.main import STARTUP_FAILURE

# Una sola implementazione per l'indirizzo LAN e per la chiave: erano nate due
# volte, qui e in run_mobile.py, e due copie della stessa funzione divergono
# sempre -- di solito il giorno in cui una delle due viene corretta.
from run_mobile import TIMEOUT_SPEGNIMENTO, chiave, ip_lan


class ServerCheSiFermaDavvero(uvicorn.Server):
    """Un uvicorn che a Ctrl+C chiude anche le risposte che non finiscono mai.

    Il problema, in ordine di causa. Le rotte SSE (``/api/events`` per il bus
    globale, ``/api/stream/{id}`` per il turno) sono risposte HTTP che per
    mestiere non terminano. Uvicorn, ricevuto il segnale, smette di accettare
    connessioni e poi **aspetta che le risposte in corso finiscano**: quelle
    non finiscono, e il server resta li'. Dall'esterno sembra che Ctrl+C non
    faccia niente e che serva chiudere la finestra del terminale -- e basta
    una scheda del browser aperta sulla UI, perche' quella tiene sempre aperto
    il bus.

    Il gestore del segnale e' l'unico posto in cui si arriva **prima** di
    quell'attesa: lo spegnimento della lifespan viene dopo, quando ormai si e'
    gia' bloccati. Qui si avvisano gli stream, che chiudono da soli, e poi si
    lascia fare a uvicorn quello che avrebbe fatto comunque.
    """

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        from server.runner import annuncia_spegnimento

        annuncia_spegnimento()
        for altro in getattr(self, "_compagni", ()):
            altro.should_exit = True
        super().handle_exit(sig, frame)


def upstream_mobile(porta_principale: int) -> str:
    """Dove il ponte deve trovare il principale.

    Di norma la stessa porta del comando (``--port``); un ``HARNESS_UPSTREAM``
    esplicito nell'ambiente vince sempre.
    """
    return os.environ.get("HARNESS_UPSTREAM") or f"http://127.0.0.1:{porta_principale}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Agent Harness")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument("--reload", action="store_true", help="ricarica a ogni salvataggio")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--mobile",
        action="store_true",
        help="avvia anche l'interfaccia mobile, raggiungibile dal telefono in LAN",
    )
    parser.add_argument(
        "--mobile-port",
        type=int,
        default=8200,
        help="porta dell'interfaccia mobile (default 8200)",
    )
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}"
    print(f"\n  Local Agent Harness  ->  {url}")

    ponte = None
    if args.mobile:
        # Il ponte vive in un thread: resta il processo principale a fare da
        # riferimento (Ctrl+C chiude tutto). L'upstream segue la porta di
        # questo comando, senno' con --port 8201 il telefono guarderebbe la
        # 8123 e troverebbe il principale spento.
        os.environ.setdefault("HARNESS_UPSTREAM", upstream_mobile(args.port))
        # La chiave si decide **qui**, prima che il thread parta: cosi'
        # l'indirizzo stampato la contiene gia' e non c'e' un momento in cui
        # il ponte e' su e l'utente non sa come entrarci.
        token = chiave()
        os.environ["HARNESS_MOBILE_TOKEN"] = token
        ponte = avvia_ponte_mobile(args.mobile_port)
        print(
            f"  Interfaccia mobile   ->  http://{ip_lan()}:{args.mobile_port}/?k={token}"
        )
        print("                           (dal telefono; la chiave resta nel browser)")

    print()
    if not args.no_browser and not args.reload:
        Timer(1.2, lambda: webbrowser.open(url)).start()

    if args.reload:
        # Con il ricaricamento automatico il server vero gira in un processo
        # figlio, e il gestore del segnale non e' nostro: resta il tetto
        # all'attesa, che e' la rete di sicurezza per gli stream aperti.
        uvicorn.run(
            "server.main:app",
            host=args.host,
            port=args.port,
            reload=True,
            log_level="warning",
            timeout_graceful_shutdown=TIMEOUT_SPEGNIMENTO,
        )
        return

    server = ServerCheSiFermaDavvero(
        uvicorn.Config(
            "server.main:app",
            host=args.host,
            port=args.port,
            log_level="warning",
            # Cintura, oltre alle bretelle di ``handle_exit``: se un domani
            # qualcuno aggiunge un'altra risposta lunga e si scorda di
            # ascoltare lo spegnimento, si aspetta questo e poi si chiude.
            timeout_graceful_shutdown=TIMEOUT_SPEGNIMENTO,
        )
    )
    server._compagni = [ponte] if ponte is not None else []
    corri(server)


def corri(server: uvicorn.Server) -> None:
    """Fa girare il server e ne gestisce l'uscita come farebbe ``uvicorn.run``.

    Costruire il ``Server`` a mano -- serve per agganciare ``handle_exit`` --
    fa perdere due cose che ``uvicorn.run()`` faceva per noi, e si sono viste
    tutte e due:

    1. **Il KeyboardInterrupt finale.** A spegnimento avvenuto uvicorn
       ri-solleva il segnale che aveva intercettato, per lasciar succedere
       quello che sarebbe successo senza il suo gestore. ``uvicorn.run()`` lo
       assorbe; senza, un Ctrl+C andato **a buon fine** stampava sette righe
       di traceback e sembrava un crash.
    2. **Il codice di uscita quando l'avvio fallisce.** Porta occupata o app
       che non importa: il server non parte, ``run()`` torna lo stesso, e il
       processo uscirebbe con zero -- cioe' dicendo "tutto bene" a chiunque lo
       stia lanciando da uno script.
    """
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    if not server.started:
        raise SystemExit(STARTUP_FAILURE)


def avvia_ponte_mobile(porta: int) -> uvicorn.Server:
    """Solleva il ponte del telefono in un thread e ne ritorna il server.

    Torna l'oggetto e non solo il thread perche' allo spegnimento gli si dice
    ``should_exit``: un thread demone verrebbe ammazzato comunque all'uscita
    dell'interprete, ma chiedere e' piu' pulito che tagliare.
    """
    server = uvicorn.Server(
        uvicorn.Config(
            "server.mobile:app",
            host="0.0.0.0",
            port=porta,
            log_level="warning",
            timeout_graceful_shutdown=TIMEOUT_SPEGNIMENTO,
        )
    )
    # I gestori di segnale se li installa solo il thread principale (lo
    # controlla uvicorn stesso): qui non ce ne sono, ed e' giusto cosi' --
    # Ctrl+C lo raccoglie il server principale, che poi avvisa questo.
    def in_silenzio() -> None:
        # Un'eccezione in un thread demone stampa un traceback e basta: qui
        # non c'e' niente da salvare, e a spegnimento in corso e' rumore.
        try:
            server.run()
        except (KeyboardInterrupt, SystemExit):
            pass

    threading.Thread(target=in_silenzio, daemon=True, name="interfaccia-mobile").start()
    return server


if __name__ == "__main__":
    main()
