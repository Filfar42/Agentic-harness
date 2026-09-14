"""Il pensiero si accorcia davvero mentre il turno avanza?

Legge le tracce che ``run_turn`` scrive sui messaggi dell'assistente (chiave
``think``) e risponde alla domanda che dai soli ``<think>`` non si poteva
rispondere: il livello *chiesto* al modello e' sceso, o no?

Uso, dalla radice del progetto:  python "claude output/verifica_pensiero.py" [filtro-modello]
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import Counter
from pathlib import Path

filtro = (sys.argv[1] if len(sys.argv) > 1 else "").lower()

tracce = []
for path in sorted(Path("chat_sessions").glob("*.json")):
    try:
        dati = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        continue
    modello = str(dati.get("model_name") or "")
    if filtro and filtro not in modello.lower():
        continue
    for m in dati.get("messages") or []:
        t = m.get("think")
        if isinstance(t, dict):
            tracce.append({**t, "modello": modello, "file": path.name})

if not tracce:
    print("Nessuna traccia trovata. Le scrive `run_turn` dai turni nuovi: "
          "le conversazioni precedenti non ne hanno.")
    raise SystemExit(0)

print(f"{len(tracce)} passi tracciati"
      + (f" (modelli con «{filtro}»)" if filtro else "") + "\n")

modulabili = [t for t in tracce if t["configurato"] not in ("on", "off", "?")]
print(f"Passi in cui il livello e' modulabile (native_think su un livello, non 'auto'): "
      f"{len(modulabili)} su {len(tracce)}")
if not modulabili:
    print("  -> con `native_think: auto` il valore e' un booleano: non c'e' nessuna")
    print("     scala su cui scendere, e `think_for_step` non puo' fare niente.")

if modulabili:
    scesi = [t for t in modulabili if t["usato"] != t["configurato"]]
    print(f"Passi in cui e' sceso davvero: {len(scesi)} "
          f"({100 * len(scesi) / len(modulabili):.0f}%)")
    print(f"  livelli usati: {dict(Counter(t['usato'] for t in modulabili))}")
    aperto = sum(1 for t in modulabili if t["punto_aperto"])
    print(f"  passi con un punto di piano aperto: {aperto} su {len(modulabili)} "
          "(senza, non si scende mai: e' la condizione)")

print("\nPensiero prodotto, per passo del turno:")
per_passo: dict[int, list[int]] = {}
for t in tracce:
    per_passo.setdefault(min(int(t.get("passo", 1)), 8), []).append(int(t.get("pensato", 0)))
for passo in sorted(per_passo):
    v = per_passo[passo]
    etichetta = f"passo {passo}" + ("+" if passo == 8 else " ")
    print(f"  {etichetta:<9} n={len(v):>4}  mediana {int(statistics.median(v)):>7,} char")

print("\nPensiero prodotto, per livello davvero chiesto:")
per_liv: dict[str, list[int]] = {}
for t in tracce:
    per_liv.setdefault(str(t.get("usato")), []).append(int(t.get("pensato", 0)))
for liv, v in sorted(per_liv.items(), key=lambda x: -len(x[1])):
    print(f"  {liv:<8} n={len(v):>4}  mediana {int(statistics.median(v)):>7,} char")

wd = sum(1 for t in tracce if t.get("watchdog"))
muti = sum(1 for t in tracce if not t.get("risposto") and not t.get("chiamate"))
print(f"\nInterruzioni del watchdog: {wd}  |  passi che non producono nulla: {muti}")
