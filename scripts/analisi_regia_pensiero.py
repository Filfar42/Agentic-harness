"""Misura della regia del pensiero sulle sessioni salvate.

Uso (da Windows, nella cartella del progetto):

    .venv\\Scripts\\python scripts\\analisi_regia_pensiero.py chat_sessions [altre cartelle...]

Legge i ``.jsonl`` delle sessioni e, per ogni passo che ha la traccia ``think``
sul messaggio dell'assistente, raccoglie quanto ha pensato e come sono andati i
tool chiamati in quel passo. Stampa quattro tabelle:

1. **per tipo** -- quello scritto dalla regia se c'e', altrimenti ricavato *a
   posteriori* dai tool del passo (sessioni vecchie). Se pensiero e riuscita
   non seguono il tipo, la regia sta tipizzando la cosa sbagliata;
2. **per livello chiesto** -- se ``low``/``medium``/``high`` danno lo stesso
   pensiero, su quel modello i livelli non sono una manopola;
3. **per chiusura** -- budget, oscillazione, decisione: quante, e se dopo la
   continuazione il passo ha agito e i tool sono riusciti;
4. **edit_file per fascia di pensiero** -- la misura che andava fatta prima di
   abbassare il pensiero sull'esecuzione: se le edit con poco pensiero
   falliscono di piu', quel pensiero non era spreco.

Il tipo a posteriori usa i tool del passo stesso: e' lecito qui perche' e' una
misura fatta dopo, e **non** lo sarebbe nella regia, che decide prima.
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

LETTURA = {"read_file", "search_files", "list_files", "esplora", "web_search", "web_fetch"}
SCRITTURA = {"write_file", "edit_file"}


def passi(percorso: Path):
    righe = []
    for riga in percorso.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            m = json.loads(riga)
        except json.JSONDecodeError:
            continue
        if isinstance(m, dict):
            righe.append(m)
    fallito_prima = False
    for i, m in enumerate(righe):
        if m.get("role") != "assistant" or not isinstance(m.get("think"), dict):
            continue
        tools = []
        for dopo in righe[i + 1:]:
            if dopo.get("role") == "assistant" or (dopo.get("role") == "user" and not dopo.get("hidden")):
                break
            if dopo.get("role") == "tool":
                tools.append((str(dopo.get("name") or ""), bool(dopo.get("ok", True)), dopo.get("args") or {}))
        t = m["think"]
        yield t, tools, fallito_prima
        fallito_prima = any(not ok for _, ok, _ in tools)


def tipo_posteriori(t, tools, fallito_prima):
    if t.get("tipo_effettivo"):
        return t["tipo_effettivo"]
    nomi = [n for n, _, _ in tools]
    if fallito_prima:
        return "diagnosi*"
    if any(n == "manage_plan" and str(a.get("action")) in ("set", "add") for n, _, a in tools):
        return "progetta*"
    lavoro = [n for n in nomi if n not in ("manage_plan", "manage_notes")]
    if lavoro and all(n in LETTURA for n in lavoro):
        return "indaga*"
    if any(n in SCRITTURA or n == "run_command" for n in lavoro):
        return "esegui*"
    return "(nessun tool)"


def riga(nome, valori, riusciti, extra=""):
    if not valori:
        return
    v = sorted(valori)
    p90 = v[min(len(v) - 1, int(len(v) * 0.9))]
    ok = f"{100 * sum(riusciti) / len(riusciti):5.1f}%" if riusciti else "   -  "
    print(f"  {nome:<22} n={len(v):<5} mediana={statistics.median(v):>8.0f}  p90={p90:>8.0f}  tool ok={ok} {extra}")


def main(cartelle: list[str]) -> None:
    per_tipo = defaultdict(lambda: ([], []))
    per_livello = defaultdict(lambda: ([], []))
    per_chiusura = defaultdict(lambda: ([], []))
    per_fascia = defaultdict(lambda: ([], []))
    n = 0
    for cartella in cartelle or ["chat_sessions"]:
        for f in sorted(Path(cartella).glob("*.jsonl")):
            for t, tools, fallito_prima in passi(f):
                n += 1
                pensato = int(t.get("pensato") or 0)
                riuscito = [all(ok for _, ok, _ in tools)] if tools else []
                tipo = tipo_posteriori(t, tools, fallito_prima)
                per_tipo[tipo][0].append(pensato)
                per_tipo[tipo][1].extend(riuscito)
                per_livello[str(t.get("usato"))][0].append(pensato)
                per_livello[str(t.get("usato"))][1].extend(riuscito)
                chiusura = t.get("chiusura") or ("watchdog" if t.get("watchdog") else "nessuna")
                per_chiusura[chiusura][0].append(pensato)
                per_chiusura[chiusura][1].extend([bool(tools)] if chiusura != "nessuna" else riuscito)
                for nome, ok, _ in tools:
                    if nome == "edit_file":
                        fascia = "<2k" if pensato < 2000 else "2k-5k" if pensato < 5000 else ">5k"
                        per_fascia[fascia][0].append(pensato)
                        per_fascia[fascia][1].append(ok)
    print(f"Passi con traccia: {n}\n")
    print("1. Per tipo (* = ricavato a posteriori). Caratteri pensati.")
    for k in sorted(per_tipo):
        riga(k, *per_tipo[k])
    print("\n2. Per livello chiesto. Stesse mediane = livelli inerti su questo modello.")
    for k in sorted(per_livello):
        riga(k, *per_livello[k])
    print("\n3. Per chiusura. 'tool ok' qui = il passo ha poi chiamato almeno un tool.")
    for k in sorted(per_chiusura):
        riga(k, *per_chiusura[k])
    print("\n4. edit_file per fascia di pensiero. 'tool ok' = edit riuscita.")
    for k in ("<2k", "2k-5k", ">5k"):
        riga(k, *per_fascia[k])


if __name__ == "__main__":
    main(sys.argv[1:])
