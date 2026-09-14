"""Quanto contesto paga davvero il modello, passo per passo, sulle sessioni salvate.

Ricostruisce per ogni generazione dell'assistente il contesto che le stava
davanti, usando le funzioni vere dell'harness (build_api_messages +
estimate_messages_tokens), e ne misura composizione e freschezza.
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))  # si lancia dalla radice del progetto

from core.agent import build_api_messages
from core.config import budgets_for
from core.prompts import SYSTEM_PROMPT
from core.textutils import estimate_messages_tokens, estimate_tokens

NUM_CTX = int(sys.argv[1]) if len(sys.argv) > 1 else 98_304
BUDGETS = budgets_for(NUM_CTX)
SESSIONI = sorted(Path("chat_sessions").glob("*.json"))

# Il prefisso vero: system prompt + intestazione d'ambiente. L'albero del
# workspace non e' ricostruibile a posteriori, quindi l'env_header si stima
# con una taglia tipica e si riporta a parte, non mescolato al resto.
COSTO_SYSTEM = estimate_tokens(SYSTEM_PROMPT)
ENV_HEADER_TIPICO = 600


def _api_corpo(ui_prefix):
    """Il contesto vero senza prefisso fisso e senza blocco di coda."""
    api = build_api_messages(
        ui_prefix,
        system_prompt=SYSTEM_PROMPT,
        env_header="x" * (ENV_HEADER_TIPICO * 4),
        strip_thinking=True,
        compact_old_tools=True,
        budgets=BUDGETS,
    )
    return [m for m in api if m.get("role") != "system"], api


def freschezza(ui_prefix):
    """Token del contesto **reale** nati piu' di 5 passi fa, contro i recenti.

    Si misura sugli api_messages, non sui messaggi grezzi: e' li' che la
    potatura degli argomenti e la compattazione dei risultati hanno gia'
    agito, e quindi e' quello che il modello paga davvero. La corrispondenza
    e' posizionale -- build_api_messages scorre in ordine -- quindi bastano
    i primi k messaggi, con k = quanti ne produce il tratto vecchio.
    """
    passi = [i for i, m in enumerate(ui_prefix) if m.get("role") == "assistant"]
    if len(passi) <= 5:
        return 0, 0
    taglio = passi[-5]
    corpo, _ = _api_corpo(ui_prefix)
    vecchio_corpo, _ = _api_corpo(ui_prefix[:taglio])
    k = len(vecchio_corpo)
    return estimate_messages_tokens(corpo[:k]), estimate_messages_tokens(corpo[k:])


ESPLORAZIONE = {"read_file", "search_files", "list_files", "grep_files"}


def per_tool(api):
    """Quanto pesa ogni tool nei risultati, e quanto pesa la sola esplorazione."""
    quote = Counter()
    for m in api:
        if m.get("role") != "tool":
            continue
        quote[str(m.get("name") or "?")] += estimate_tokens(str(m.get("content") or ""))
    return quote


def composizione(api):
    quote = Counter()
    for m in api:
        ruolo = m.get("role", "")
        costo = estimate_tokens(str(m.get("content") or ""))
        for call in m.get("tool_calls") or []:
            fn = call.get("function") or {}
            costo += estimate_tokens(str(fn.get("arguments") or "")) + 10
            quote["argomenti tool call"] += estimate_tokens(str(fn.get("arguments") or ""))
        if ruolo == "tool":
            quote["risultati dei tool"] += costo
        elif ruolo == "assistant":
            quote["testo assistente"] += estimate_tokens(str(m.get("content") or ""))
        elif ruolo == "user":
            quote["utente + coda"] += costo
        elif ruolo == "system":
            quote["prefisso"] += costo
    return quote


righe = []
quote_totali = Counter()
quote_tool = Counter()
picco_globale = (0, "", 0)

for path in SESSIONI:
    try:
        dati = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        continue
    msgs = dati.get("messages") or []
    if len(msgs) < 6:
        continue

    picchi = []
    ultimo_api = None
    ultimo_prefix = None
    for i, m in enumerate(msgs):
        if m.get("role") != "assistant":
            continue
        prefix = msgs[:i]
        if not prefix:
            continue
        api = build_api_messages(
            prefix,
            system_prompt=SYSTEM_PROMPT,
            env_header="x" * (ENV_HEADER_TIPICO * 4),
            strip_thinking=True,
            compact_old_tools=True,
            budgets=BUDGETS,
        )
        tok = estimate_messages_tokens(api)
        picchi.append(tok)
        if tok > picco_globale[0]:
            picco_globale = (tok, path.name, len(picchi))
        ultimo_api, ultimo_prefix = api, prefix

    if not picchi:
        continue
    vecchi, nuovi = freschezza(ultimo_prefix)
    quote_totali.update(composizione(ultimo_api))
    quote_tool.update(per_tool(ultimo_api))
    righe.append(
        {
            "file": path.name,
            "msg": len(msgs),
            "passi": len(picchi),
            "picco": max(picchi),
            "mediana": int(statistics.median(picchi)),
            # Il numero che conta su un endpoint remoto: il contesto si
            # rispedisce **intero a ogni passo**, e li' si paga a token.
            "somma": sum(picchi),
            "vecchi": vecchi,
            "nuovi": nuovi,
        }
    )

righe.sort(key=lambda r: -r["picco"])

print(f"num_ctx simulato: {NUM_CTX:,}   sessioni utili: {len(righe)}/{len(SESSIONI)}")
print(f"prefisso fisso (system {COSTO_SYSTEM} + env ~{ENV_HEADER_TIPICO}) = "
      f"~{COSTO_SYSTEM + ENV_HEADER_TIPICO:,} token, pagati a ogni passo\n")

print(f"{'sessione':<28} {'msg':>5} {'passi':>6} {'picco':>8} {'mediana':>8} {'%ctx':>6} {'>5 passi fa':>12}")
for r in righe[:20]:
    quota = 100.0 * r["picco"] / NUM_CTX
    stale = f"{r['vecchi']:,}" if r["vecchi"] else "-"
    print(f"{r['file']:<28} {r['msg']:>5} {r['passi']:>6} {r['picco']:>8,} "
          f"{r['mediana']:>8,} {quota:>5.1f}% {stale:>12}")

picchi = [r["picco"] for r in righe]
print("\ntoken di prompt spediti in tutto (contesto x passi), le 8 sessioni piu' care:")
for r in sorted(righe, key=lambda r: -r["somma"])[:8]:
    print(f"  {r['file']:<28} {r['passi']:>4} passi x {r['mediana']:>7,} mediani "
          f"= {r['somma']:>12,} token")
print(f"  {'TOTALE su tutte le sessioni':<28} {'':>4}          "
      f"    {sum(r['somma'] for r in righe):>12,} token")

print(f"\npicco mediano su tutte le sessioni: {int(statistics.median(picchi)):,}")
print(f"picco massimo: {picco_globale[0]:,} ({picco_globale[1]}, al passo {picco_globale[2]})")
print(f"sessioni sopra il 25% di {NUM_CTX:,}: "
      f"{sum(1 for p in picchi if p > NUM_CTX * 0.25)}")
print(f"sessioni sopra la soglia di compattazione (75%): "
      f"{sum(1 for p in picchi if p > NUM_CTX * 0.75)}")

tot = sum(quote_totali.values()) or 1
print("\ncomposizione del contesto all'ultimo passo, sommata su tutte le sessioni:")
for voce, val in quote_totali.most_common():
    print(f"  {voce:<24} {val:>10,}  {100.0 * val / tot:>5.1f}%")

tot_tool = sum(quote_tool.values()) or 1
esplor = sum(v for k, v in quote_tool.items() if k in ESPLORAZIONE)
print("\nrisultati dei tool, per tool (somma su tutte le sessioni):")
for nome, val in quote_tool.most_common(10):
    marchio = "  <- esplorazione" if nome in ESPLORAZIONE else ""
    print(f"  {nome:<24} {val:>10,}  {100.0 * val / tot_tool:>5.1f}%{marchio}")
print(f"  {'TOTALE esplorazione':<24} {esplor:>10,}  {100.0 * esplor / tot_tool:>5.1f}%")

vecchi_tot = sum(r["vecchi"] for r in righe)
nuovi_tot = sum(r["nuovi"] for r in righe)
if vecchi_tot + nuovi_tot:
    print(f"\nfreschezza all'ultimo passo (somma): {vecchi_tot:,} token nati "
          f"oltre 5 passi fa contro {nuovi_tot:,} recenti "
          f"({100.0 * vecchi_tot / (vecchi_tot + nuovi_tot):.1f}% vecchio)")
