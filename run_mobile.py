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
import secrets
import socket

import uvicorn


def ip_lan() -> str:
    """L'indirizzo con cui il telefono ci vede, senza generare traffico."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # non invia nulla: serve al routing
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def chiave() -> str:
    """La chiave d'accesso del ponte: quella dell'ambiente, o una nuova."""
    return os.environ.get("HARNESS_MOBILE_TOKEN") or secrets.token_urlsafe(9)


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
    token = chiave()
    os.environ["HARNESS_MOBILE_TOKEN"] = token
    # L'indirizzo si stampa **con la chiave dentro**: e' l'unico gesto che si
    # chiede all'utente, e va fatto una volta sola per telefono (poi resta un
    # cookie). Senza, il ponte risponde 401 a chiunque -- compreso lui.
    print(f"\n  Harness Mobile  ->  http://{ip_lan()}:{args.port}/?k={token}")
    print("  (aprilo dal telefono: la chiave resta nel browser per 30 giorni)")
    print(f"  upstream: {args.upstream}\n")

    uvicorn.run(
        "server.mobile:app",
        host=args.host,
        port=args.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
