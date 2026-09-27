"""Avvio dell'interfaccia mobile, accanto a quella principale.

    python run_mobile.py                 # ascolta su tutte le interfacce, :8200
    python run_mobile.py --port 8201     # se la 8200 e' occupata

Stampa gli URL da aprire dal telefono: quello LAN e' quello giusto quando
il PC e il telefono sono sulla stessa rete Wi-Fi. Il server principale deve
essere gia' in piedi ('python run.py'): il mobile e' un suo ponte, non un
secondo cervello.
"""

from __future__ import annotations

import argparse
import os
import socket

import uvicorn

# Quanto uvicorn aspetta le risposte in corso prima di chiudere comunque. Le
# rotte SSE non finiscono mai per mestiere: senza un tetto, Ctrl+C resta
# appeso finche' c'e' un browser collegato. Cinque secondi bastano a lasciar
# finire una richiesta normale e non fanno sembrare il server bloccato.
TIMEOUT_SPEGNIMENTO = 5


def ip_lan() -> str:
    """L'indirizzo con cui il telefono ci vede, senza generare traffico.

    Il trucco: si "connette" un socket UDP a un indirizzo irraggiungibile e si
    chiede al kernel quale interfaccia avrebbe usato. Non parte nessun
    pacchetto -- UDP non fa handshake -- e non serve nessuna libreria.

    Su una macchina senza default route (nessuna rete, o solo il loopback) la
    connect fallisce e si torna ``127.0.0.1``: e' il ripiego giusto, perche' e'
    letteralmente l'unico indirizzo su cui si e' raggiungibili. Ma allora il
    QR e la riga stampata dicono al telefono di connettersi a se stesso, e chi
    guarda non capisce perche' non funziona: e' l'unico caso in cui questo
    valore va letto come "non lo so", non come un indirizzo.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # non invia nulla: serve al routing
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Interfaccia mobile dell'harness")
    parser.add_argument("--host", default="0.0.0.0", help="0.0.0.0 per la LAN")
    parser.add_argument("--port", type=int, default=int(os.environ.get("MOBILE_PORT", "8200")))
    parser.add_argument(
        "--upstream",
        default=os.environ.get("HARNESS_UPSTREAM", "http://127.0.0.1:8123"),
        help="dove gira il server principale",
    )
    args = parser.parse_args()

    os.environ["MOBILE_PORT"] = str(args.port)
    os.environ["HARNESS_UPSTREAM"] = args.upstream
    os.environ["HARNESS_MOBILE_PORT"] = str(args.port)
    print(f"\n  Harness Mobile  ->  http://{ip_lan()}:{args.port}/")
    print("  Associa il telefono da Impostazioni → Mobile nel desktop.")
    print(f"  upstream: {args.upstream}\n")

    uvicorn.run(
        "server.mobile:app",
        host=args.host,
        port=args.port,
        log_level="warning",
        timeout_graceful_shutdown=TIMEOUT_SPEGNIMENTO,
    )


if __name__ == "__main__":
    main()
