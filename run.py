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
import asyncio
import os
import socket
import webbrowser
from threading import Timer
from types import FrameType

import uvicorn
from uvicorn.main import STARTUP_FAILURE

# Il limite di attesa allo spegnimento è condiviso con il launcher standalone.
from run_mobile import TIMEOUT_SPEGNIMENTO


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

    # Altri server da fermare insieme a questo (il ponte del telefono).
    # Dichiarato qui e non appiccicato all'istanza dal chiamante: era un
    # attributo che compariva dal nulla su un oggetto di libreria, e chi
    # leggeva ``getattr(self, "_compagni", ())`` non aveva modo di sapere chi
    # ce lo mettesse ne' quando.
    compagni: list[uvicorn.Server]

    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(config)
        self.compagni = []

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        from server.runner import annuncia_spegnimento

        if not self.should_exit:
            # Con log_level=warning uvicorn non dice niente mentre aspetta, e
            # un terminale muto dopo Ctrl+C invita a premerlo di nuovo -- che
            # e' esattamente l'uscita forzata qui sotto.
            print("  Spegnimento in corso...", flush=True)
        annuncia_spegnimento()
        for altro in self.compagni:
            altro.should_exit = True
        super().handle_exit(sig, frame)

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        """Lo spegnimento di uvicorn, piu' la lifespan anche quando si forza.

        Il secondo Ctrl+C mette ``force_exit``, e con quello uvicorn **salta**
        ``lifespan.shutdown()``: il task della lifespan resta fermo su
        ``receive()``, ``asyncio.run`` lo cancella uscendo, e Starlette
        registra la ``CancelledError`` come ``ERROR:`` con due traceback
        concatenati (KeyboardInterrupt -> CancelledError). Sembra un crash ed
        e' solo un'uscita forzata; in piu' la nostra chiusura (anteprime,
        client verso il backend) non girava. Qui la si fa comunque, con un
        tetto breve: forzare deve restare veloce.
        """
        await super().shutdown(sockets=sockets)
        if self.force_exit:
            try:
                await asyncio.wait_for(self.lifespan.shutdown(), timeout=2.0)
            except Exception:  # noqa: BLE001 - TimeoutError compreso
                pass


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

    os.environ["HARNESS_DESKTOP_URL"] = f"http://127.0.0.1:{args.port}"
    os.environ["HARNESS_MOBILE_PORT"] = str(args.mobile_port)
    os.environ["HARNESS_MOBILE_AUTOSTART"] = "1" if args.mobile else "0"
    if args.mobile:
        print("  Mobile: associa il telefono da Impostazioni → Mobile.")

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


if __name__ == "__main__":
    main()
