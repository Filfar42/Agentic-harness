"""Un llama-server finto, per vedere il cruscotto muoversi senza una GPU.

Parla quel tanto del dialetto di llama-server che serve all'harness:
``/props``, ``/health``, ``/v1/models`` e ``/v1/chat/completions`` in stream,
con ``timings`` a ogni token (come ``timings_per_token``), il progresso del
prefill (come ``return_progress``) e i contatori di un draft MTP. La velocita'
cala man mano che il prompt cresce, come su un modello vero: e' quello che il
grafico "velocita' e contesto" deve far vedere.

Il copione di un turno: pensa e guarda i file, ne legge due in parallelo,
pensa a lungo e scrive un file (gli argomenti arrivano a pezzi), risponde.
Nessun modello, nessuna rete: solo tempi credibili.

    python scripts/finto_llama_server.py --port 8090 [--veloce 3]

Poi, nelle impostazioni dell'harness: transport ``llamacpp``, indirizzo
``http://127.0.0.1:8090``, modello ``finto-27b``, sandbox ``host``.
"""

from __future__ import annotations

import argparse
import json
import random
import time
from collections.abc import Iterator
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

FINESTRA = 65_536
MODELLO = "finto-27b"
VELOCE = 1.0

app = FastAPI(title="llama-server finto")

PENSIERI = (
    "Devo capire com'e' fatto il progetto prima di toccare qualcosa. ",
    "Il modulo del parser legge i token uno alla volta; ",
    "la funzione che gestisce le graffe annidate sembra il punto debole. ",
    "Controllo come vengono propagati gli errori, poi decido. ",
    "Se cambio la firma devo aggiornare anche i test. ",
)


def _parole(frasi: tuple[str, ...], n: int) -> Iterator[str]:
    """Pezzi da uno-tre caratteri di parola, come i token di un modello."""
    testo = "".join(random.choice(frasi) for _ in range(n // 8 + 2))
    parole = testo.split(" ")
    for i in range(n):
        yield parole[i % len(parole)] + " "


def _passo(messaggi: list[dict[str, Any]]) -> int:
    """A che punto del copione siamo: quanti risultati di tool ci sono gia'.

    Si conta dall'ultimo messaggio dell'utente che non e' un sollecito
    dell'harness: i solleciti arrivano come ``user`` anche loro, e contare da
    li' farebbe ricominciare il copione a meta'. Un messaggio vero dell'utente
    viene dopo una risposta finale (o in testa); un sollecito dopo un tool o
    dopo una risposta che l'harness ha rifiutato -- qui basta la prima regola:
    il copione chiude sempre con una risposta.
    """
    inizio = 0
    for i, m in enumerate(messaggi):
        if m.get("role") != "user":
            continue
        prima = messaggi[i - 1] if i else None
        if prima is None or prima.get("role") == "system" or (
            prima.get("role") == "assistant" and not prima.get("tool_calls")
            and "Ho sistemato" in str(prima.get("content") or "")
        ):
            inizio = i
    return sum(1 for m in messaggi[inizio:] if m.get("role") == "tool")


def _prompt_token(messaggi: list[dict[str, Any]], tools: Any) -> int:
    caratteri = len(json.dumps(messaggi, ensure_ascii=False)) + len(json.dumps(tools or []))
    return max(200, int(caratteri / 3.6))


@app.get("/props")
def props() -> dict[str, Any]:
    return {
        "default_generation_settings": {"n_ctx": FINESTRA},
        "model_path": f"/modelli/{MODELLO}.gguf",
        "chat_template": "{{ messages }}",
        "modalities": {"vision": False},
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models")
def models() -> dict[str, Any]:
    return {"object": "list", "data": [{"id": MODELLO, "object": "model"}]}


@app.post("/v1/chat/completions")
async def chat(request: Request) -> Any:
    corpo = await request.json()
    messaggi = corpo.get("messages") or []
    if not corpo.get("stream"):
        return JSONResponse({"choices": [{"index": 0, "message": {
            "role": "assistant", "content": "Riassunto finto."}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 3}})
    tools = corpo.get("tools")
    passo = _passo(messaggi) if tools else 99
    prompt = _prompt_token(messaggi, tools)
    return StreamingResponse(_stream(passo, prompt, bool(corpo.get("return_progress"))),
                             media_type="text/event-stream")


def _chunk(delta: dict[str, Any] | None = None, finish: str | None = None,
           **extra: Any) -> str:
    dati = {"object": "chat.completion.chunk", "model": MODELLO,
            "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}], **extra}
    return f"data: {json.dumps(dati, ensure_ascii=False)}\n\n"


def _stream(passo: int, prompt: int, progresso: bool) -> Iterator[str]:
    # La cache regge quasi tutto il prompt: si ricalcola la coda.
    cache = max(0, prompt - random.randint(600, 1800))
    nuovi = prompt - cache
    prefill_tok_s = 900.0
    prefill_ms = nuovi / prefill_tok_s * 1000.0
    if progresso:
        fatti = 0
        while fatti < nuovi:
            fatti = min(nuovi, fatti + 512)
            time.sleep(512 / prefill_tok_s / VELOCE)
            yield _chunk({"role": "assistant", "content": None}, prompt_progress={
                "total": prompt, "cache": cache, "processed": fatti,
                "time_ms": round(fatti / prefill_tok_s * 1000.0)})
    else:
        time.sleep(prefill_ms / 1000.0 / VELOCE)

    # Velocita' che cala col contesto, con un po' di rumore: ~31 tok/s a
    # prompt vuoto, ~22 a 40k.
    base = max(8.0, 31.0 - prompt / 4500.0)
    stato = {"n": 0, "ms": 0.0, "draft_n": 0, "draft_ok": 0}

    def timings() -> dict[str, Any]:
        return {"prompt_n": nuovi, "prompt_ms": round(prefill_ms, 1), "cache_n": cache,
                "predicted_n": stato["n"], "predicted_ms": round(stato["ms"], 1),
                "predicted_per_second": stato["n"] / max(stato["ms"], 1) * 1000,
                "draft_n": stato["draft_n"], "draft_n_accepted": stato["draft_ok"]}

    def token() -> None:
        # MTP: ogni ciclo propone 3 token e ne accetta 0-3; i token escono a
        # grappoli, ma sul totale la velocita' e' ``base``.
        stato["n"] += 1
        passo_ms = 1000.0 / (base * random.uniform(0.85, 1.15))
        stato["ms"] += passo_ms
        if stato["n"] % 3 == 1:
            stato["draft_n"] += 3
            stato["draft_ok"] += random.choice((1, 2, 2, 3, 3))
        time.sleep(passo_ms / 1000.0 / VELOCE)

    def pensa(n: int) -> Iterator[str]:
        for pezzo in _parole(PENSIERI, n):
            token()
            yield _chunk({"reasoning_content": pezzo}, timings=timings())

    def rispondi(testo: str) -> Iterator[str]:
        for parola in testo.split(" "):
            token()
            yield _chunk({"content": parola + " "}, timings=timings())

    def chiama(indice: int, nome: str, argomenti: dict[str, Any], a_pezzi: int = 8) -> Iterator[str]:
        testo = json.dumps(argomenti, ensure_ascii=False)
        token()
        yield _chunk({"tool_calls": [{"index": indice, "id": f"c{passo}_{indice}", "type": "function",
                                      "function": {"name": nome, "arguments": ""}}]},
                     timings=timings())
        for i in range(0, len(testo), a_pezzi):
            token()
            yield _chunk({"tool_calls": [{"index": indice, "function": {
                "arguments": testo[i:i + a_pezzi]}}]}, timings=timings())

    if passo == 0:
        yield from pensa(160)
        yield from chiama(0, "list_files", {"subfolder": "."})
        fine = "tool_calls"
    elif passo == 1:
        yield from pensa(90)
        yield from chiama(0, "read_file", {"filepath": "parser.py"})
        yield from chiama(1, "read_file", {"filepath": "test_parser.py"})
        fine = "tool_calls"
    elif passo == 3:
        yield from pensa(320)
        righe = "".join(f"    # passo {i}: chiude la graffa aperta al livello {i}\n" for i in range(24))
        corpo = f"def parse(s):\n{righe}    return s\n"
        yield from chiama(0, "write_file", {"filepath": "parser.py", "content": corpo}, a_pezzi=4)
        fine = "tool_calls"
    elif passo == 4:
        yield from pensa(70)
        yield from chiama(0, "run_command", {
            "command": "python3 -c \"import time; time.sleep(2.5); print('3 passed')\""})
        fine = "tool_calls"
    elif passo >= 5 and passo < 99:
        yield from pensa(60)
        yield from rispondi(
            "Ho sistemato il parser: le graffe annidate ora si chiudono nell'ordine giusto "
            "e gli errori risalgono con la riga esatta. Ho riscritto parser.py e lasciato "
            "i test come erano, perche' coprivano gia' il caso che si rompeva.")
        fine = "stop"
    else:
        yield from rispondi("Riassunto finto della conversazione.")
        fine = "stop"
    yield _chunk(None, finish=fine, timings=timings())
    yield ("data: " + json.dumps({"choices": [], "usage": {
        "prompt_tokens": prompt, "completion_tokens": stato["n"],
        "prompt_tokens_details": {"cached_tokens": cache}}, "timings": timings()}) + "\n\n")
    yield "data: [DONE]\n\n"


def main() -> None:
    global VELOCE  # noqa: PLW0603 - un'opzione della riga di comando
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--veloce", type=float, default=1.0,
                        help="moltiplica il tempo: 3 = tre volte piu' in fretta (i tok/s dichiarati restano)")
    args = parser.parse_args()
    VELOCE = max(0.1, args.veloce)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
