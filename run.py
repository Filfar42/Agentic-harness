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

import uvicorn

# Una sola implementazione per l'indirizzo LAN e per la chiave: erano nate due
# volte, qui e in run_mobile.py, e due copie della stessa funzione divergono
# sempre -- di solito il giorno in cui una delle due viene corretta.
from run_mobile import chiave, ip_lan


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
        threading.Thread(
            target=lambda: uvicorn.run(
                "server.mobile:app",
                host="0.0.0.0",
                port=args.mobile_port,
                log_level="warning",
            ),
            daemon=True,
            name="interfaccia-mobile",
        ).start()
        print(
            f"  Interfaccia mobile   ->  http://{ip_lan()}:{args.mobile_port}/?k={token}"
        )
        print("                           (dal telefono; la chiave resta nel browser)")

    print()
    if not args.no_browser and not args.reload:
        Timer(1.2, lambda: webbrowser.open(url)).start()

    uvicorn.run(
        "server.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
