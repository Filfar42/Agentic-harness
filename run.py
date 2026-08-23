"""Avvio dell'harness.

    python run.py            # http://127.0.0.1:8123
    python run.py --port 9000 --reload
"""

from __future__ import annotations

import argparse
import webbrowser
from threading import Timer

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Agent Harness")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument("--reload", action="store_true", help="ricarica a ogni salvataggio")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}"
    print(f"\n  Local Agent Harness  ->  {url}\n")
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
