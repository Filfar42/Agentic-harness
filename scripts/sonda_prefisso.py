"""Quanto prefisso il server riusa davvero, passo dopo passo.

Due misure, la seconda facoltativa.

1. **A secco, sulle sessioni salvate** (nessuna rete). Ricostruisce con
   ``build_api_messages`` la richiesta che l'harness manderebbe a ogni passo
   e la confronta con quella del passo successivo. Dove le due divergono, il
   server deve ricalcolare tutto da li' in avanti, anche i messaggi che aveva
   gia' in cache: sono i token *sprecati*. Il minimo inevitabile (i messaggi
   nuovi piu' il blocco di coda, che cambia sempre) non si conta.

       python scripts/sonda_prefisso.py chat_sessions/<id>.jsonl [--num-ctx 131072]
       python scripts/sonda_prefisso.py --sintetica 40      # sessione finta

2. **Contro llama-server** (``--server http://host:8080``). Chiede a
   ``/apply-template`` il prompt che il template produce davvero e a
   ``/tokenize`` quanti token ci sono prima del primo carattere diverso, per
   due domande che a secco non hanno risposta:

   * cambiare ``reasoning_effort`` fra un passo e l'altro (lo fa la regia
     del pensiero) sposta testo **in testa** al prompt? Se si', ogni cambio di
     livello costa un ricalcolo completo;
   * due passi consecutivi di una sessione vera divergono dove dice la
     misura a secco, o prima (per esempio perche' il template riscrive il
     pensiero dei turni vecchi)?

I numeri a secco usano la stima dell'harness (3,6 caratteri per token), non il
tokenizer: servono a confrontare due politiche, non a prevedere millisecondi.
Il costo vero di un passo si legge nella telemetria: ``cached_tokens`` e
``prompt_processed_tokens`` sul transport llamacpp.
"""

from __future__ import annotations

import argparse
import itertools
import json
import statistics
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.agent import build_api_messages  # noqa: E402
from core.config import COMPACT_MAX_TOKENS, budgets_for  # noqa: E402
from core.textutils import estimate_messages_tokens  # noqa: E402

CODA = "<comunicazione_turno>"


def _canonico(msg: dict[str, Any]) -> str:
    return json.dumps(msg, ensure_ascii=False, sort_keys=True)


def _senza_coda(api: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if api and api[-1].get("role") == "user" and str(api[-1].get("content", "")).startswith(CODA):
        return api[:-1]
    return api


def richieste(ui: list[dict[str, Any]], *, num_ctx: int, tetto: int,
              system: str, env: str) -> list[list[dict[str, Any]]]:
    """Una richiesta per ogni passo: la cronologia prima di ogni assistant."""
    budgets = budgets_for(num_ctx, tetto)
    fuori = []
    for i, msg in enumerate(ui):
        if msg.get("role") == "assistant" and i > 0:
            fuori.append(_senza_coda(build_api_messages(
                ui[:i], system_prompt=system, env_header=env, budgets=budgets,
            )))
    return fuori


def spreco(prima: list[dict[str, Any]], dopo: list[dict[str, Any]]) -> tuple[int, int, bool]:
    """(token da ricalcolare che il server aveva gia', token di prima, compattazione?)"""
    n = min(len(prima), len(dopo))
    i = 0
    while i < n and _canonico(prima[i]) == _canonico(dopo[i]):
        i += 1
    riassunto = any(m.get("role") == "user" and "riassunt" in str(m.get("content", ""))[:200]
                    for m in dopo[:3])
    return estimate_messages_tokens(prima[i:]), estimate_messages_tokens(prima), riassunto


def misura(ui: list[dict[str, Any]], *, num_ctx: int, tetto: int) -> dict[str, Any]:
    reqs = richieste(ui, num_ctx=num_ctx, tetto=tetto, system="S" * 9000, env="E" * 4000)
    per_passo = []
    for a, b in itertools.pairwise(reqs):
        sprecati, totale, _ = spreco(a, b)
        per_passo.append((sprecati, totale))
    sprecati = [s for s, _ in per_passo]
    return {
        "passi": len(reqs),
        "token_sprecati_totali": sum(sprecati),
        "token_inviati_totali": sum(t for _, t in per_passo),
        "passi_con_spreco": sum(1 for s in sprecati if s),
        "spreco_mediano": int(statistics.median(sprecati)) if sprecati else 0,
        "spreco_massimo": max(sprecati, default=0),
    }


def sessione_sintetica(passi: int, *, seme: int = 7) -> list[dict[str, Any]]:
    """Un lavoro di codice plausibile: letture grosse, comandi, modifiche, piano.

    Le proporzioni vengono dalla misura del 23/08/2026 (read_file 36%,
    run_command 33%, manage_plan 13%, search_files 10%, edit_file 5% dei token
    di risultato): la sonda deve confrontare le politiche su un carico che
    somiglia a quello vero, non su file da tre righe.
    """
    import random

    rnd = random.Random(seme)
    ui: list[dict[str, Any]] = [{"role": "user", "content": "Sistema il modulo e fai passare i test."}]
    riga = "    valore = calcola(parametro, altro_parametro)  # commento di codice\n"
    for n in range(passi):
        tipo = rnd.choices(
            ["read_file", "run_command", "manage_plan", "search_files", "edit_file"],
            weights=[36, 33, 13, 10, 8],
        )[0]
        if tipo == "read_file":
            args = {"filepath": f"core/modulo_{n}.py"}
            corpo = riga * rnd.randint(60, 420)
            esito = {"filepath": args["filepath"], "content": corpo, "total_lines": corpo.count("\n")}
        elif tipo == "run_command":
            args = {"command": "pytest -q"}
            esito = {"returncode": rnd.choice([0, 1]), "stdout": "." * 80 + "\n" + riga * rnd.randint(10, 90),
                     "stderr": ""}
        elif tipo == "manage_plan":
            args = {"action": "complete", "note": "fatto"}
            esito = {"ok": True}
        elif tipo == "search_files":
            args = {"pattern": "calcola"}
            esito = {"matches": [f"core/m{k}.py:{k}: calcola(x)" for k in range(rnd.randint(5, 60))]}
        else:
            args = {"filepath": "core/modulo.py", "old_string": riga * 3, "new_string": riga * 4}
            esito = {"status": "ok", "filepath": "core/modulo.py"}
        cid = f"c{n}"
        ui.append({"role": "assistant", "content": "", "tool_calls": [{
            "id": cid, "type": "function",
            "function": {"name": tipo, "arguments": json.dumps(args, ensure_ascii=False)},
        }]})
        ui.append({"role": "tool", "tool_call_id": cid, "name": tipo, "ok": True,
                   "content": json.dumps(esito, ensure_ascii=False)})
    ui.append({"role": "assistant", "content": "Fatto."})
    return ui


def carica(percorso: Path) -> list[dict[str, Any]]:
    righe = percorso.read_text(encoding="utf-8").splitlines()
    return [json.loads(r) for r in righe if r.strip()]


# -- contro llama-server ------------------------------------------------------


def _post(client: Any, url: str, corpo: dict[str, Any]) -> dict[str, Any]:
    risposta = client.post(url, json=corpo, timeout=60.0)
    risposta.raise_for_status()
    return risposta.json()


def token_prima_della_divergenza(client: Any, server: str, a: str, b: str) -> tuple[int, int]:
    i = 0
    n = min(len(a), len(b))
    while i < n and a[i] == b[i]:
        i += 1
    comune = _post(client, f"{server}/tokenize", {"content": a[:i]}).get("tokens", [])
    totale = _post(client, f"{server}/tokenize", {"content": a}).get("tokens", [])
    return len(comune), len(totale)


def sonda_server(server: str, ui: list[dict[str, Any]], livelli: list[str]) -> None:
    import httpx

    from core.backend import messaggi_per_il_filo

    server = server.rstrip("/")
    storia = [m for m in ui if m.get("role") in ("user", "assistant", "tool")][:12] or [
        {"role": "user", "content": "ciao"}]
    messaggi = messaggi_per_il_filo(build_api_messages(
        storia, system_prompt="Sei un assistente.", env_header="<environment/>"))
    with httpx.Client() as client:
        print("\n== reasoning_effort: dove cade nel prompt ==")
        resi = {}
        for livello in livelli:
            try:
                resi[livello] = _post(client, f"{server}/apply-template", {
                    "messages": messaggi,
                    "chat_template_kwargs": {"reasoning_effort": livello, "enable_thinking": True},
                })["prompt"]
            except Exception as exc:  # noqa: BLE001 - diagnostica
                print(f"  {livello}: /apply-template ha rifiutato ({exc})")
        base = livelli[0]
        for livello in livelli[1:]:
            if base in resi and livello in resi:
                comune, totale = token_prima_della_divergenza(client, server, resi[base], resi[livello])
                esito = ("identici" if resi[base] == resi[livello]
                         else f"divergono al token {comune} di {totale}")
                print(f"  {base} -> {livello}: {esito}")
        print("  Se la divergenza e' vicina a 0, cambiare livello a meta' turno ricalcola tutto il prompt.")

        print("\n== due passi consecutivi della sessione ==")
        reqs = richieste(ui, num_ctx=131_072, tetto=COMPACT_MAX_TOKENS,
                         system="Sei un assistente.", env="<environment/>")
        for k in range(min(3, max(0, len(reqs) - 1))):
            a = _post(client, f"{server}/apply-template",
                      {"messages": messaggi_per_il_filo(reqs[k])})["prompt"]
            b = _post(client, f"{server}/apply-template",
                      {"messages": messaggi_per_il_filo(reqs[k + 1])})["prompt"]
            comune, totale = token_prima_della_divergenza(client, server, a, b)
            print(f"  passo {k + 1} -> {k + 2}: {comune} token riusabili su {totale}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("sessioni", nargs="*", type=Path)
    p.add_argument("--sintetica", type=int, default=0, help="passi di una sessione finta")
    p.add_argument("--num-ctx", type=int, default=131_072)
    p.add_argument("--tetto", type=int, default=COMPACT_MAX_TOKENS)
    p.add_argument("--server", default="", help="llama-server, per la seconda misura")
    p.add_argument("--livelli", default="low,medium,high,xhigh")
    a = p.parse_args()

    casi: list[tuple[str, list[dict[str, Any]]]] = []
    if a.sintetica:
        casi.append((f"sintetica ({a.sintetica} passi)", sessione_sintetica(a.sintetica)))
    for percorso in a.sessioni:
        casi.append((percorso.name, carica(percorso)))
    if not casi:
        p.error("serve almeno una sessione o --sintetica N")
    for nome, ui in casi:
        print(f"\n# {nome}  (num_ctx {a.num_ctx}, tetto compattazione {a.tetto})")
        for chiave, valore in misura(ui, num_ctx=a.num_ctx, tetto=a.tetto).items():
            print(f"  {chiave:24} {valore}")
    if a.server:
        sonda_server(a.server, casi[-1][1], [x.strip() for x in a.livelli.split(",") if x.strip()])


if __name__ == "__main__":
    main()
