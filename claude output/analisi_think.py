"""Anatomia del ragionamento di qwen3.8: quanto pensa, e cosa fa mentre pensa.

Quattro domande, una per sezione: quanto costa il pensiero, quando gira a
vuoto (loop), quando si ripete dentro sé stesso, e quando parla d'altro.
"""
from __future__ import annotations

import json
import re
import statistics
import sys
from collections import Counter
from pathlib import Path

MODELLO = "qwen3.8"
SESSIONI = sorted(Path("chat_sessions").glob("*.json"))

# Parole troppo comuni per dire qualcosa su "di cosa si sta parlando".
STOP = set("""
the a an and or but if then that this these those is are was were be been being
to of in on at for with from by as it its it's i he she they we you your my our
not no do does did done can could should would will shall may might must have
has had there here what which who whom when where why how all any both each few
more most other some such only own same so than too very just now also then
il lo la i gli le un uno una di a da in con su per tra fra e o ma se che non
sono e' del della dei delle al alla nel nella come piu' meno anche gia' quindi
poi ora qui questo questa questi queste quello quella cosa fare c'e' ci si
""".split())

ITA = set("""
che non per una del della sono anche quindi quando perche' come questo questa
gli le dei delle nella nel alla allo dal dalla piu' meno cosa fare devo posso
serve deve solo ancora adesso allora invece pero' senza dopo prima
""".split())
ENG = set("""
the and for that with this from they have will not are was were been being
should would could need let's user file files check need to i'll i should
let me first then now but so if because when while what which
""".split())


def parole(testo: str) -> list[str]:
    return [p for p in re.findall(r"[a-zà-ÿ']{3,}", (testo or "").lower()) if p not in STOP]


def lingua(testo: str) -> str:
    """Italiano o inglese, per conteggio di parole-spia. Grezzo ma robusto."""
    grezze = re.findall(r"[a-zà-ÿ']{2,}", (testo or "").lower())
    it = sum(1 for p in grezze if p in ITA)
    en = sum(1 for p in grezze if p in ENG)
    if it == en:
        return "?"
    return "it" if it > en else "en"


def frasi(testo: str) -> list[str]:
    pezzi = re.split(r"(?<=[.!?;:\n])\s+", testo or "")
    return [" ".join(p.split()) for p in pezzi if len(p.split()) >= 4]


def ripetizione_ngrammi(testo: str, n: int = 8) -> float:
    """Quota di n-grammi che il blocco ripete al proprio interno.

    E' la misura piu' onesta di "sta girando a vuoto dentro una frase": un
    testo che avanza ha pochissimi 8-grammi ripetuti.
    """
    ps = parole(testo)
    if len(ps) < n * 3:
        return 0.0
    grammi = [tuple(ps[i:i + n]) for i in range(len(ps) - n + 1)]
    quante = Counter(grammi)
    ripetuti = sum(c - 1 for c in quante.values() if c > 1)
    return ripetuti / len(grammi)


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def firma_call(call: dict) -> str:
    fn = call.get("function") or {}
    args = fn.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args or "{}")
        except json.JSONDecodeError:
            args = {"_": args}
    if not isinstance(args, dict):
        args = {"_": str(args)}
    corpo = " ".join(f"{k}={str(v)[:120]}" for k, v in sorted(args.items()))
    return f"{fn.get('name')}({corpo})"


righe = []
for path in SESSIONI:
    try:
        dati = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        continue
    if MODELLO not in str(dati.get("model_name") or "").lower():
        continue

    msgs = dati.get("messages") or []
    richiesta = ""
    blocchi = []
    firme: list[str] = []
    for i, m in enumerate(msgs):
        if m.get("role") == "user" and not m.get("hidden"):
            richiesta = str(m.get("content") or "")
        if m.get("role") != "assistant":
            continue
        raw = str(m.get("content") or "")
        pensiero = ""
        chiuso = True
        if "<think>" in raw:
            resto = raw.split("<think>", 1)[1]
            if "</think>" in resto:
                pensiero, risposta = resto.split("</think>", 1)
            else:
                pensiero, risposta, chiuso = resto, "", False
        else:
            risposta = raw
        calls = m.get("tool_calls") or []
        for c in calls:
            firme.append(firma_call(c))
        blocchi.append({
            "i": i,
            "pensiero": pensiero.strip(),
            "risposta": risposta.strip(),
            "chiuso": chiuso,
            "n_call": len(calls),
            "firme": [firma_call(c) for c in calls],
            "richiesta": richiesta,
        })
    if blocchi:
        righe.append({"file": path.name, "titolo": dati.get("title", ""), "blocchi": blocchi,
                      "firme": firme, "msgs": msgs})

print(f"Sessioni con {MODELLO}: {len(righe)}\n")
print("=" * 78)
print("1. QUANTO COSTA IL PENSIERO")
print("=" * 78)

tutti = [b for r in righe for b in r["blocchi"]]
con_pensiero = [b for b in tutti if b["pensiero"]]
tot_pensiero = sum(len(b["pensiero"]) for b in tutti)
tot_risposta = sum(len(b["risposta"]) for b in tutti)
print(f"{'sessione':<30} {'passi':>6} {'pensiero':>10} {'risposte':>9} {'rapporto':>9} "
      f"{'mediana':>8} {'max':>7} {'tronchi':>8}")
for r in righe:
    b = r["blocchi"]
    pen = [len(x["pensiero"]) for x in b if x["pensiero"]]
    tp = sum(len(x["pensiero"]) for x in b)
    tr = sum(len(x["risposta"]) for x in b)
    tronchi = sum(1 for x in b if not x["chiuso"])
    print(f"{r['file']:<30} {len(b):>6} {tp:>10,} {tr:>9,} "
          f"{(tp / max(tr, 1)):>8.1f}x {int(statistics.median(pen or [0])):>8,} "
          f"{max(pen or [0]):>7,} {tronchi:>8}")
print(f"{'TOTALE':<30} {len(tutti):>6} {tot_pensiero:>10,} {tot_risposta:>9,} "
      f"{(tot_pensiero / max(tot_risposta, 1)):>8.1f}x")
print(f"\nIn token stimati: {tot_pensiero // 4:,} pensati contro {tot_risposta // 4:,} scritti.")

pen_tutti = [len(b["pensiero"]) for b in con_pensiero]
if pen_tutti:
    pen_tutti.sort()
    def pct(q):
        return pen_tutti[min(len(pen_tutti) - 1, int(len(pen_tutti) * q))]
    print(f"Distribuzione dei blocchi di pensiero (caratteri): "
          f"p50={pct(0.5):,}  p75={pct(0.75):,}  p90={pct(0.9):,}  p99={pct(0.99):,}")

# Pensiero speso senza produrre niente: ne' tool call ne' risposta.
sterili = [b for b in con_pensiero if b["n_call"] == 0 and len(b["risposta"]) < 40]
print(f"\nPassi che pensano e non producono nulla (0 tool call, risposta < 40 char): "
      f"{len(sterili)} su {len(con_pensiero)} "
      f"({100 * len(sterili) / max(len(con_pensiero), 1):.0f}%), "
      f"{sum(len(b['pensiero']) for b in sterili):,} caratteri buttati")

# Costo del pensiero per tool call emessa.
call_tot = sum(b["n_call"] for b in tutti)
print(f"Pensiero per tool call emessa: {tot_pensiero // max(call_tot, 1):,} caratteri "
      f"(~{tot_pensiero // 4 // max(call_tot, 1):,} token) per ognuna delle {call_tot} chiamate")

print()
print("=" * 78)
print("2. LOOP: LA STESSA MOSSA, DI NUOVO")
print("=" * 78)
for r in righe:
    ripetute = Counter(r["firme"])
    doppie = {k: v for k, v in ripetute.items() if v > 1}
    consecutive = sum(
        1 for a, b in zip(r["firme"], r["firme"][1:]) if a == b
    )
    sprecate = sum(v - 1 for v in doppie.values())
    print(f"\n{r['file']}  ({len(r['firme'])} chiamate)")
    print(f"  chiamate identiche ripetute: {sprecate} "
          f"({100 * sprecate / max(len(r['firme']), 1):.0f}% del totale), "
          f"di cui {consecutive} immediatamente di fila")
    for k, v in sorted(doppie.items(), key=lambda x: -x[1])[:4]:
        print(f"    {v}x  {k[:110]}")

print()
print("=" * 78)
print("3. RIPETIZIONI DENTRO IL PENSIERO")
print("=" * 78)
peggiori = sorted(con_pensiero, key=lambda b: -ripetizione_ngrammi(b["pensiero"]))[:6]
medie = [ripetizione_ngrammi(b["pensiero"]) for b in con_pensiero]
print(f"Quota media di 8-grammi ripetuti dentro un blocco: {100 * statistics.mean(medie):.1f}%")
print(f"Blocchi sopra il 10% (il modello si sta riscrivendo addosso): "
      f"{sum(1 for m in medie if m > 0.10)} su {len(medie)}")
print("\nI peggiori:")
for b in peggiori:
    r = ripetizione_ngrammi(b["pensiero"])
    if r < 0.05:
        continue
    fs = frasi(b["pensiero"])
    doppioni = [f for f, c in Counter(fs).items() if c > 1]
    print(f"  {100 * r:.0f}% ripetuto, {len(b['pensiero']):,} char, "
          f"{len(doppioni)} frasi identiche ripetute")
    if doppioni:
        print(f"     es. «{doppioni[0][:150]}»")

# Ripetizione fra un passo e il successivo: sta ri-derivando la stessa cosa?
coppie = []
for r in righe:
    b = [x for x in r["blocchi"] if x["pensiero"]]
    for x, y in zip(b, b[1:]):
        coppie.append(jaccard(set(parole(x["pensiero"])), set(parole(y["pensiero"]))))
if coppie:
    print(f"\nSomiglianza fra un blocco di pensiero e il successivo (Jaccard): "
          f"mediana {statistics.median(coppie):.2f}, "
          f"sopra 0.5 (quasi lo stesso ragionamento) {sum(1 for c in coppie if c > 0.5)}/{len(coppie)}")

print()
print("=" * 78)
print("4. FUORI TEMA")
print("=" * 78)
lingue = Counter(lingua(b["pensiero"]) for b in con_pensiero)
print(f"Lingua del pensiero: {dict(lingue)}")
lingue_utente = Counter(lingua(b["richiesta"]) for b in con_pensiero if b["richiesta"])
print(f"Lingua della richiesta dell'utente: {dict(lingue_utente)}")

sovr = []
for b in con_pensiero:
    if not b["richiesta"]:
        continue
    sovr.append((jaccard(set(parole(b["pensiero"])), set(parole(b["richiesta"]))), b))
if sovr:
    valori = [s for s, _ in sovr]
    print(f"\nSovrapposizione lessicale pensiero/richiesta: mediana {statistics.median(valori):.3f}")
    lontani = sorted(sovr)[:5]
    print("I blocchi piu' lontani da quello che era stato chiesto:")
    for s, b in lontani:
        print(f"  {s:.3f} — «{b['pensiero'][:170].strip()}…»")

META = [
    (r"\b(the )?(system )?prompt\b", "parla del proprio prompt"),
    (r"\bthe user (wants|asked|is asking)\b", "riformula la richiesta"),
    (r"\b(nudge|sollecito|interruption message|reminder)\b", "commenta i solleciti dell'harness"),
    (r"\bmanage_plan\b", "ragiona sul tool del piano"),
    (r"\b(i should|i need to|let me|i'll) (be careful|make sure|remember)\b", "auto-istruzioni"),
    (r"\bwait\b[,.]", "si corregge in corsa ('wait,')"),
    (r"\b(actually|hmm|but wait)\b", "ripensamenti"),
]
print("\nMarcatori nel pensiero (quanti blocchi su "
      f"{len(con_pensiero)} li contengono):")
for pattern, nome in META:
    n = sum(1 for b in con_pensiero if re.search(pattern, b["pensiero"], re.I))
    print(f"  {n:>4}  ({100 * n / max(len(con_pensiero), 1):>3.0f}%)  {nome}")
