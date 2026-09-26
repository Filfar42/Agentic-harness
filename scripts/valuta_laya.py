"""Misura Laya (o Jev) come valutatore della compattazione selettiva, sui tuoi dati.

Perche' esiste
--------------
Il 25/09 la compattazione selettiva (``core/selezione.py``) e' stata misurata
senza un modello di decisione vero: con le sole regole, con un "limite" che
toglie tutto e con un "oracolo" che conosce il futuro della sessione (tiene
una chiamata solo se il suo file, comando o pattern ricompare piu' avanti).
Quello che manca e' **quanto Laya si avvicina all'oracolo** sulle tue
conversazioni, in italiano, a zero-shot -- l'unico numero che dice se vale
la pena accenderla. Questo script lo misura.

Come
----
Rigioca le sessioni salvate come ``replay_contesto.py --selezione oracolo``
(stessa traiettoria, stessi punti di compattazione). A ogni candidato chiede a
Laya le due domande di ``core/selezione.py`` (stesso stato, stesse domande che
riceverebbe nel turno vero) e registra la risposta accanto all'etichetta
dell'oracolo. Le decisioni che guidano il replay restano quelle dell'oracolo,
cosi' Laya viene interrogata su tutti i punti, non solo su quelli che le sue
risposte avrebbero prodotto.

Prerequisiti (sulla macchina con le sessioni)::

    pip install "laya[serve]"
    LAYA_MODELS=multilingual laya-serve        # http://127.0.0.1:8000/v1/systemone
    python scripts/valuta_laya.py chat_sessions --json laya.json

Il primo avvio di ``laya-serve`` scarica il checkpoint da Hugging Face (circa
650 MB per il multilingue). Niente esce dalla macchina: ``laya-serve`` e'
locale. Per Jev: ``--url https://api.typesafe.ai/v1/systemone --modello
jev-latest`` con ``TYPESAFE_API_KEY`` nell'ambiente -- in quel caso gli stati
(obiettivo, chiamata, inizio dell'esito) vanno a TypeSafe.

Cosa stampa
-----------
* accordo con l'oracolo sulla decisione "togli / tieni" del risultato
  (``keep_result`` sotto soglia = togli), precisione e richiamo di "togli":
  un "togli" sbagliato e' informazione persa, un "tieni" sbagliato solo
  risparmio mancato;
* AUC di ``keep_result`` rispetto all'etichetta (0,5 = a caso);
* latenza per domanda e per compattazione: e' il tempo che il turno aspetta.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from analisi_sessioni import carica
from replay_contesto import ValutatoreOracolo, rigioca

from core import selezione


class Registratore(ValutatoreOracolo):
    """Decide come l'oracolo, e intanto chiede e registra il valutatore vero."""

    nome = "registratore"

    def __init__(self, vero: Any) -> None:
        super().__init__()
        self.vero = vero
        self.righe: list[dict[str, Any]] = []
        self.errori = 0

    def valuta(self, stato: Any, domande: dict[str, Any]) -> dict[str, float]:
        oracolo = super().valuta(stato, domande)
        inizio = time.monotonic()
        try:
            risposta = self.vero.valuta(stato, domande)
        except Exception as exc:  # noqa: BLE001 - si conta e si prosegue
            self.errori += 1
            risposta = {"errore": str(exc)[:200]}
        self.righe.append({
            "tool": (stato.get("call") or {}).get("tool"),
            "superata": stato.get("later") != ["nothing related happened later"],
            "etichetta_tieni": oracolo["keep_result"] >= 0.5,
            "p_call": risposta.get("keep_call"),
            "p_result": risposta.get("keep_result"),
            "errore": risposta.get("errore"),
            "ms": round((time.monotonic() - inizio) * 1000, 1),
            "domande": len(domande),
        })
        return oracolo


def auc(positivi: list[float], negativi: list[float]) -> float | None:
    """Probabilita' che un positivo abbia punteggio piu' alto di un negativo."""
    if not positivi or not negativi:
        return None
    vinte = 0.0
    for p in positivi:
        for n in negativi:
            vinte += 1.0 if p > n else 0.5 if p == n else 0.0
    return vinte / (len(positivi) * len(negativi))


def riassumi(righe: list[dict[str, Any]], soglia: float = selezione.SOGLIA_TENUTA) -> dict[str, Any]:
    valide = [r for r in righe if isinstance(r.get("p_result"), (int, float))]
    fuori: dict[str, Any] = {"domande_totali": sum(r["domande"] for r in righe),
                             "candidati": len(righe), "risposte_valide": len(valide),
                             "errori": sum(1 for r in righe if r.get("errore"))}
    if not valide:
        return fuori
    togli_pred = [r["p_result"] < soglia for r in valide]
    togli_vero = [not r["etichetta_tieni"] for r in valide]
    vp = sum(1 for p, v in zip(togli_pred, togli_vero, strict=True) if p and v)
    fp = sum(1 for p, v in zip(togli_pred, togli_vero, strict=True) if p and not v)
    fn = sum(1 for p, v in zip(togli_pred, togli_vero, strict=True) if not p and v)
    giusti = sum(1 for p, v in zip(togli_pred, togli_vero, strict=True) if p == v)
    fuori.update({
        "accordo_con_oracolo": round(giusti / len(valide), 3),
        "precisione_togli": round(vp / (vp + fp), 3) if vp + fp else None,
        "richiamo_togli": round(vp / (vp + fn), 3) if vp + fn else None,
        "tolti_ma_servivano": fp,
        "auc_keep_result": auc([r["p_result"] for r in valide if r["etichetta_tieni"]],
                               [r["p_result"] for r in valide if not r["etichetta_tieni"]]),
        "ms_per_domanda_mediana": round(statistics.median(
            r["ms"] / max(1, r["domande"]) for r in valide), 1),
        "base_togli_oracolo": round(sum(togli_vero) / len(valide), 3),
    })
    return fuori


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("cartella", type=Path, nargs="?", default=Path("chat_sessions"))
    ap.add_argument("--url", default="http://127.0.0.1:8000/v1/systemone")
    ap.add_argument("--modello", default="multilingual")
    ap.add_argument("--num-ctx", type=int, default=65_536)
    ap.add_argument("--tetto", type=int, default=32_768)
    ap.add_argument("--min-passi", type=int, default=3)
    ap.add_argument("--max-sessioni", type=int, default=0, help="0 = tutte")
    ap.add_argument("--json", type=Path, default=None)
    a = ap.parse_args(argv)
    vero = selezione.ValutatoreSystemOne(a.url, a.modello, timeout_s=30.0)
    registratore = Registratore(vero)
    per_sessione: dict[str, Any] = {}
    sessioni = [s for s in carica(a.cartella)
                if sum(1 for m in s["messaggi"] if m.get("role") == "assistant") >= a.min_passi]
    if a.max_sessioni:
        sessioni = sessioni[: a.max_sessioni]
    for s in sessioni:
        prima = len(registratore.righe)
        esito = rigioca(s["messaggi"], num_ctx=a.num_ctx, tetto=a.tetto, selezione="oracolo",
                        valutatore_esterno=registratore)
        per_sessione[s["id"]] = {"compattazioni": esito["selezioni_riuscite"]
                                 + esito["selezioni_insufficienti"],
                                 "candidati": len(registratore.righe) - prima,
                                 "candidati_per_evento": esito["candidati_per_evento"]}
    totale = riassumi(registratore.righe)
    eventi = [n for v in per_sessione.values() for n in v["candidati_per_evento"]]
    if eventi and "ms_per_domanda_mediana" in totale:
        totale["secondi_per_compattazione_stimati"] = round(
            statistics.median(eventi) * 2 * totale["ms_per_domanda_mediana"] / 1000, 1)
    rapporto = {"parametri": {"url": a.url, "modello": a.modello, "num_ctx": a.num_ctx,
                              "tetto": a.tetto, "valutatore": vero.nome},
                "totale": totale}
    print(json.dumps(rapporto, ensure_ascii=False, indent=1))
    if a.json:
        a.json.write_text(json.dumps({**rapporto, "per_sessione": per_sessione,
                                      "righe": registratore.righe},
                                     ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
