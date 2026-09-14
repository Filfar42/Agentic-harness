"""Secondo passaggio: dove finiscono davvero i caratteri pensati."""
from __future__ import annotations

import json
import re
import statistics
from collections import Counter
from pathlib import Path

MODELLO = "qwen3.8"


def frasi(testo: str) -> list[str]:
    pezzi = re.split(r"(?<=[.!?;:])\s+|\n+", testo or "")
    return [" ".join(p.split()) for p in pezzi if p.strip()]


META = {
    "harness (piano, prompt, solleciti, passi rimasti)": re.compile(
        r"manage_plan|system prompt|\bthe prompt\b|nudge|interruption message|"
        r"agent steps? (left|remaining)|steps? left|\bthe user (wants|asked|is asking)|"
        r"manage_notes|esplora\(|ask_user_question|tool call", re.I),
    "ripensamenti (wait / actually / hmm / but)": re.compile(
        r"\b(wait|actually|hmm|hold on|on second thought|let me reconsider)\b", re.I),
    "auto-istruzioni (I should / I need to / let me)": re.compile(
        r"\b(i should|i need to|i must|let me|i'll need)\b", re.I),
}

righe = []
for path in sorted(Path("chat_sessions").glob("*.json")):
    dati = json.loads(path.read_text(encoding="utf-8"))
    if MODELLO not in str(dati.get("model_name") or "").lower():
        continue
    msgs = dati.get("messages") or []
    passo = 0
    blocchi = []
    for m in msgs:
        if m.get("role") == "user":
            passo = 0            # nuovo turno dell'utente (anche i solleciti)
        if m.get("role") != "assistant":
            continue
        passo += 1
        raw = str(m.get("content") or "")
        pensiero = ""
        if "<think>" in raw:
            resto = raw.split("<think>", 1)[1]
            pensiero = resto.split("</think>", 1)[0] if "</think>" in resto else resto
        blocchi.append({
            "passo": passo,
            "pensiero": pensiero.strip(),
            "risposta": (raw.split("</think>", 1)[-1] if "</think>" in raw else raw).strip(),
            "n_call": len(m.get("tool_calls") or []),
            "nomi": [(c.get("function") or {}).get("name") for c in (m.get("tool_calls") or [])],
        })
    righe.append({"file": path.name, "b": blocchi, "msgs": msgs})

tutti = [b for r in righe for b in r["b"] if b["pensiero"]]

print("=" * 78)
print("A. DOVE FINISCONO I CARATTERI PENSATI")
print("=" * 78)
tot = sum(len(b["pensiero"]) for b in tutti)
for nome, rx in META.items():
    quanti = 0
    for b in tutti:
        quanti += sum(len(f) for f in frasi(b["pensiero"]) if rx.search(f))
    print(f"  {100 * quanti / tot:>5.1f}%  ({quanti:>9,} char)  frasi che parlano di: {nome}")

print()
print("=" * 78)
print("B. QUANTE VOLTE CAMBIA IDEA DENTRO UN BLOCCO")
print("=" * 78)
rx = META["ripensamenti (wait / actually / hmm / but)"]
conteggi = [(len(rx.findall(b["pensiero"])), b) for b in tutti]
solo = [c for c, _ in conteggi]
print(f"'wait/actually/hmm' per blocco: mediana {statistics.median(solo):.0f}, "
      f"media {statistics.mean(solo):.1f}, massimo {max(solo)}")
lunghi = [(c, len(b['pensiero'])) for c, b in conteggi if len(b["pensiero"]) > 15_000]
if lunghi:
    print(f"Nei {len(lunghi)} blocchi sopra i 15.000 caratteri: "
          f"mediana {statistics.median([c for c, _ in lunghi]):.0f} ripensamenti")
corti = [c for c, b in conteggi if len(b["pensiero"]) < 2_000]
print(f"Nei {len(corti)} blocchi sotto i 2.000 caratteri: "
      f"mediana {statistics.median(corti):.0f} ripensamenti")

print()
print("=" * 78)
print("C. IL PENSIERO SI ACCORCIA MENTRE IL TURNO AVANZA?")
print("=" * 78)
print("(``think_for_step`` abbassa il livello di un solo scalino dal passo 2 in poi)")
per_passo: dict[int, list[int]] = {}
for b in tutti:
    per_passo.setdefault(min(b["passo"], 8), []).append(len(b["pensiero"]))
for passo in sorted(per_passo):
    v = per_passo[passo]
    etichetta = f"passo {passo}" + ("+" if passo == 8 else "")
    print(f"  {etichetta:<9} n={len(v):>3}  mediana {int(statistics.median(v)):>7,} char  "
          f"media {int(statistics.mean(v)):>7,}")

print()
print("=" * 78)
print("D. IL PENSIERO SEGUE IL TIPO DI MOSSA?")
print("=" * 78)
per_tool: dict[str, list[int]] = {}
for b in tutti:
    chiave = ",".join(sorted(set(b["nomi"]))) or "(nessuna chiamata)"
    per_tool.setdefault(chiave, []).append(len(b["pensiero"]))
for chiave, v in sorted(per_tool.items(), key=lambda x: -len(x[1]))[:9]:
    print(f"  {chiave[:44]:<45} n={len(v):>3}  mediana {int(statistics.median(v)):>7,} char")

print()
print("=" * 78)
print("E. LA SESSIONE CHE NON HA MAI RISPOSTO")
print("=" * 78)
for r in righe:
    risposte = [b for b in r["b"] if b["risposta"]]
    if risposte:
        continue
    print(f"{r['file']}: {len(r['b'])} passi, "
          f"{sum(len(b['pensiero']) for b in r['b']):,} caratteri pensati, 0 di risposta")
    ruoli = Counter(m.get("role") for m in r["msgs"])
    print(f"  ruoli nella cronologia: {dict(ruoli)}")
    nascosti = [m for m in r["msgs"] if m.get("role") == "user" and m.get("hidden")]
    print(f"  solleciti dell'harness: {len(nascosti)}")
    for m in nascosti[:3]:
        print(f"    «{' '.join(str(m.get('content') or '').split())[:130]}…»")
    ultimo = r["b"][-1]
    print(f"  ultimo passo: {ultimo['n_call']} chiamate, "
          f"{len(ultimo['pensiero']):,} char di pensiero")
    print(f"    coda del pensiero: «…{' '.join(ultimo['pensiero'].split())[-260:]}»")

print()
print("=" * 78)
print("F. I COMANDI RIPETUTI: FALLIVANO?")
print("=" * 78)
for r in righe:
    esiti: dict[str, list[str]] = {}
    per_id = {}
    for m in r["msgs"]:
        if m.get("role") == "assistant":
            for c in m.get("tool_calls") or []:
                fn = c.get("function") or {}
                per_id[c.get("id")] = f"{fn.get('name')}:{str(fn.get('arguments'))[:150]}"
        elif m.get("role") == "tool":
            chiave = per_id.get(m.get("tool_call_id"))
            if not chiave:
                continue
            corpo = str(m.get("content") or "")
            ok = '"error"' not in corpo[:200] and '"returncode": 0' in corpo or (
                '"error"' not in corpo[:200])
            esiti.setdefault(chiave, []).append("ok" if ok else "KO")
    ripetuti = {k: v for k, v in esiti.items() if len(v) > 2}
    if not ripetuti:
        continue
    print(f"\n{r['file']}")
    for k, v in sorted(ripetuti.items(), key=lambda x: -len(x[1]))[:4]:
        print(f"  {len(v)}x [{' '.join(v)}]  {k[:100]}")
