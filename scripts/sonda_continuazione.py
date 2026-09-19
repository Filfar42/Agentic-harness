"""Sonda: su questo Ollama e questo modello, la regia del pensiero funziona?

Uso (da Windows, con Ollama acceso):

    .venv\\Scripts\\python scripts\\sonda_continuazione.py --model qwen3.8:27b
    .venv\\Scripts\\python scripts\\sonda_continuazione.py --url http://192.168.1.50:11434 --model qwen3.8:27b

Due domande, a cui dal codice non si puo' rispondere:

1. **La continuazione legge il pensiero precompilato?** L'harness chiude un
   pensiero troppo lungo mandando un assistant col solo ``thinking`` (il
   pensiero parziale piu' una frase che chiude) e lascia che il modello
   riprenda. Qui nel pensiero precompilato c'e' una parola in codice che non
   compare da nessun'altra parte: se la risposta la contiene, il modello l'ha
   letto. Si prova con ``think: false`` e ``think: true``, nello stesso ordine
   in cui li prova l'harness.

2. **I livelli di pensiero cambiano qualcosa?** Stessa domanda con ``low``,
   ``medium`` e ``high``: se i caratteri pensati sono simili, su questo modello
   i livelli sono etichette e l'unica manopola vera e' il budget dell'harness.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request

PAROLA = "ZAFFIRO-41"
DOMANDA_LIVELLI = (
    "Un treno parte alle 9:40 e viaggia a 84 km/h; un secondo parte dalla stessa "
    "stazione alle 10:05 a 120 km/h sullo stesso binario parallelo. A che ora lo "
    "raggiunge? Rispondi con l'orario."
)


def chat(url: str, payload: dict, timeout: float) -> tuple[int, dict, float]:
    corpo = json.dumps(payload).encode()
    req = urllib.request.Request(f"{url}/api/chat", data=corpo, headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}"), time.monotonic() - t0
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode(errors="replace")[:300]}, time.monotonic() - t0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:11434")
    ap.add_argument("--model", required=True)
    ap.add_argument("--timeout", type=float, default=600)
    a = ap.parse_args()
    url = a.url.rstrip("/")

    print(f"== 1. Continuazione del pensiero ({a.model})")
    pensiero = (
        "The user asks for the code word. From the setup I remember it clearly: "
        f"the code word is {PAROLA}. Nothing else is needed."
        "\n\nThe next move is decided. No more re-checking: I act on it now.\n"
    )
    esiti = {}
    for think in (False, True):
        stato, r, dt = chat(url, {
            "model": a.model, "stream": False, "think": think,
            "options": {"num_predict": 200, "temperature": 0},
            "messages": [
                {"role": "system", "content": "Rispondi in italiano, brevissimo."},
                {"role": "user", "content": "Qual e' la parola in codice? Scrivi solo la parola."},
                {"role": "assistant", "content": "", "thinking": pensiero},
            ],
        }, a.timeout)
        msg = r.get("message") or {}
        contenuto = str(msg.get("content") or "")
        pensato = str(msg.get("thinking") or "")
        letta = PAROLA in contenuto
        esiti[think] = stato == 200 and letta and len(pensato) < 1200
        print(f"  think={think!s:<5} HTTP {stato}  {dt:5.1f}s  pensiero nuovo={len(pensato):>5} car."
              f"  risposta={contenuto.strip()[:60]!r}")
        if stato != 200:
            print(f"    errore: {r.get('error')}")
        elif not letta:
            print("    la parola in codice NON e' nella risposta: il pensiero precompilato non e' stato letto")
        elif len(pensato) >= 1200:
            print("    ha ricominciato a pensare: l'harness lo rileva e ricade sul watchdog")
    if esiti.get(False):
        print("  -> OK: l'harness usera' think=false, e funziona.")
    elif esiti.get(True):
        print("  -> OK con think=true: l'harness ci arrivera' dopo un tentativo fallito con false.")
    else:
        print("  -> NON funziona su questo server: l'harness ricade sul watchdog che interrompe.\n"
              "     Conviene aggiornare Ollama e rilanciare la sonda.")

    print("\n== 2. I livelli di pensiero cambiano qualcosa?")
    for livello in ("low", "medium", "high"):
        stato, r, dt = chat(url, {
            "model": a.model, "stream": False, "think": livello,
            "options": {"num_predict": 6000, "temperature": 0},
            "messages": [{"role": "user", "content": DOMANDA_LIVELLI}],
        }, a.timeout)
        msg = r.get("message") or {}
        print(f"  {livello:<6} HTTP {stato}  {dt:6.1f}s  pensato={len(str(msg.get('thinking') or '')):>6} car."
              f"  eval={r.get('eval_count')}")
    print("  Mediane simili (entro ~20%) = livelli inerti: conta solo il budget dell'harness.")


if __name__ == "__main__":
    main()
