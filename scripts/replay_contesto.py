"""Rigioca le sessioni salvate attraverso la politica di contesto del codice corrente.

Fase 1 e Fase 5 del lavoro del 25/09/2026. Non chiama nessun modello: prende le
traiettorie **vere** (cosa il modello ha chiamato, cosa i tool hanno risposto)
e ricostruisce, passo per passo, la richiesta che *questa* versione dell'harness
manderebbe: prefisso, cronologia potata e compattata, riassunti a soglia,
scarto dei turni vecchi. Poi misura.

    python scripts/replay_contesto.py chat_sessions [--num-ctx 65536] [--json out.json]

Si lancia identico su due versioni del codice (per esempio ``master`` in un
worktree e il ramo di lavoro) e i numeri si confrontano: stessa traiettoria,
politica diversa. Cosa **non** misura: come il modello avrebbe reagito a un
contesto diverso. Quello lo dice solo una prova con il modello vero.

Il riassunto della compattazione lo scrive un finto riassuntore deterministico
(``RiassuntoreFinto``): stessa taglia di un riassunto vero (circa 700 token,
il tetto ``MAX_TOKEN_RIASSUNTO``), cosi' il conto dei token resta onesto senza
una chiamata al modello.

Metriche, per sessione e in totale:

* ``token_inviati``: somma dei token di prompt stimati su tutti i passi;
* ``picco``: la richiesta piu' grande;
* ``ricalcolati``: token che il server aveva gia' in cache e deve rifare
  perche' la richiesta diverge dalla precedente prima della fine (prefisso
  instabile). E' la misura del costo di prefill su llama.cpp/Ollama;
* ``compattazioni`` / ``scarti``: quante volte interviene il riassunto e
  quante l'ultima spiaggia;
* ``copie_superate``: token di risultati di ``read_file`` ancora in contesto
  mentre una lettura o una scrittura piu' recente dello stesso file li ha resi
  vecchi (contenuto che il modello non dovrebbe piu' usare).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from analisi_sessioni import carica  # noqa: E402

from core import agent as agent_mod  # noqa: E402
from core.backend import StreamEvent  # noqa: E402
from core.config import GenParams, budgets_for, finestra_efficace  # noqa: E402
from core.textutils import estimate_messages_tokens, estimate_tokens  # noqa: E402

try:  # il prompt snello e' quello dei modelli che ragionano
    from core.prompts import pick_system_prompt

    SYSTEM = pick_system_prompt(thinking=True)
except ImportError:  # pragma: no cover - versioni molto vecchie
    from core.prompts import SYSTEM_PROMPT as SYSTEM

try:
    from core.tools import TOOLS_SCHEMA_LEAN as SCHEMA
except ImportError:  # pragma: no cover
    from core.tools import TOOLS_SCHEMA as SCHEMA

# Un environment di taglia tipica (albero di un progetto medio): uguale per
# tutte le versioni, perche' qui si confrontano le politiche, non l'albero.
ENV = "<environment>\n" + ("  modulo/file_di_esempio.py (1,234 B)\n" * 90) + "</environment>"
SCHEMA_TOKENS = estimate_tokens(json.dumps(SCHEMA, ensure_ascii=False))

RIASSUNTO_FINTO = (
    "FATTO:\n" + "- punto completato con i file toccati e l'esito verificato\n" * 12
    + "SCOPERTO:\n" + "- fatto tecnico verificato con percorso e versione\n" * 12
    + "APERTO:\n" + "- lavoro ancora da fare con la prima mossa\n" * 8
)


class RiassuntoreFinto:
    """Risponde a ogni chiamata di servizio con un testo di taglia realistica."""

    def stream(self, _messages: Any, _tools: Any = None, _params: Any = None, **_: Any):
        yield StreamEvent("content", text=RIASSUNTO_FINTO)
        yield StreamEvent("usage", usage={"done_reason": "stop"})


def _canonico(msg: dict[str, Any]) -> str:
    return json.dumps(msg, ensure_ascii=False, sort_keys=True)


def _divergenza(prima: list[dict[str, Any]], dopo: list[dict[str, Any]]) -> int:
    """Token della richiesta precedente da ricalcolare (dal primo messaggio diverso)."""
    n = min(len(prima), len(dopo))
    i = 0
    while i < n and _canonico(prima[i]) == _canonico(dopo[i]):
        i += 1
    return estimate_messages_tokens(prima[i:])


def _copie_superate(api: list[dict[str, Any]]) -> int:
    """Token di read_file integrali resi vecchi da una lettura/scrittura successiva."""
    ultimo: dict[str, int] = {}
    letture: list[tuple[int, str, int]] = []
    for i, m in enumerate(api):
        if m.get("role") == "assistant":
            for c in m.get("tool_calls") or []:
                fn = c.get("function") or {}
                if fn.get("name") in ("write_file", "edit_file"):
                    try:
                        fp = json.loads(fn.get("arguments") or "{}").get("filepath")
                    except (ValueError, AttributeError):
                        fp = None
                    if fp:
                        ultimo[str(fp)] = i
        if m.get("role") == "tool" and m.get("name") == "read_file":
            try:
                p = json.loads(m.get("content") or "{}")
            except ValueError:
                continue
            if isinstance(p, dict) and p.get("filepath") and "content" in p and not p.get("_compacted"):
                fp = str(p["filepath"])
                letture.append((i, fp, estimate_tokens(m.get("content") or "")))
                ultimo[fp] = max(ultimo.get(fp, -1), i)
    return sum(tok for i, fp, tok in letture if ultimo.get(fp, i) > i)


def _costruisci(storia: list[dict[str, Any]], budgets: Any) -> list[dict[str, Any]]:
    api = agent_mod.build_api_messages(
        storia, system_prompt=SYSTEM, env_header=ENV, budgets=budgets,
        strip_thinking=True, compact_old_tools=True,
    )
    # Il blocco di coda cambia a ogni passo per costruzione: fuori dal confronto.
    if api and api[-1].get("role") == "user" and str(api[-1].get("content", "")).startswith(
        ("<comunicazione_turno>", "<piano", "<skill", "<anteprima", "<libreria", "<note")
    ):
        api = api[:-1]
    return api


def rigioca(messaggi: list[dict[str, Any]], *, num_ctx: int, tetto: int,
            soglia: float = 0.75) -> dict[str, Any]:
    budgets = budgets_for(num_ctx, tetto)
    params = GenParams(num_ctx=num_ctx, max_tokens=8192)
    finestra = finestra_efficace(num_ctx, tetto)
    storia: list[dict[str, Any]] = []
    precedente: list[dict[str, Any]] | None = None
    inviati: list[int] = []
    ricalcolati = 0
    superate: list[int] = []
    compattazioni = scarti = fallite = 0
    ferma_fino_a = 0
    passo = 0
    for msg in messaggi:
        ruolo = msg.get("role")
        if ruolo not in ("user", "assistant", "tool", "summary"):
            continue
        if ruolo == "summary":
            # I riassunti registrati li ha prodotti la politica di allora: qui
            # decide quella corrente.
            continue
        if ruolo == "assistant":
            passo += 1
            api = _costruisci(storia, budgets)
            if passo >= ferma_fino_a and agent_mod.context_pressure(
                api, finestra, reserved_tokens=SCHEMA_TOKENS
            ) > soglia:
                esito = agent_mod.compatta_cronologia(
                    storia, backend=RiassuntoreFinto(), params=params, budgets=budgets,
                    strip_thinking=True, finestra=finestra, schedario=None,
                )
                if esito is None:
                    fallite += 1
                    ferma_fino_a = passo + 3
                else:
                    compattazioni += 1
                    api = _costruisci(storia, budgets)
            if agent_mod.context_pressure(api, num_ctx, reserved_tokens=SCHEMA_TOKENS) > max(0.75, soglia):
                api = agent_mod.drop_oldest_turns(api, num_ctx, soglia=max(0.75, soglia),
                                                  reserved_tokens=SCHEMA_TOKENS)
                scarti += 1
            tok = estimate_messages_tokens(api) + SCHEMA_TOKENS
            inviati.append(tok)
            if precedente is not None:
                ricalcolati += _divergenza(precedente, api)
            superate.append(_copie_superate(api))
            precedente = api
        storia.append(dict(msg))
    return {
        "passi": passo,
        "token_inviati": sum(inviati),
        "picco": max(inviati, default=0),
        "mediana_per_passo": int(statistics.median(inviati)) if inviati else 0,
        "ricalcolati": ricalcolati,
        "copie_superate_picco": max(superate, default=0),
        "copie_superate_somma": sum(superate),
        "compattazioni": compattazioni,
        "compattazioni_fallite": fallite,
        "scarti": scarti,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("cartella", type=Path, nargs="?", default=Path("chat_sessions"))
    ap.add_argument("--num-ctx", type=int, default=65_536)
    ap.add_argument("--tetto", type=int, default=32_768)
    ap.add_argument("--min-passi", type=int, default=3)
    ap.add_argument("--json", type=Path, default=None)
    a = ap.parse_args(argv)
    per_sessione = {}
    for s in carica(a.cartella):
        if sum(1 for m in s["messaggi"] if m.get("role") == "assistant") < a.min_passi:
            continue
        per_sessione[s["id"]] = rigioca(s["messaggi"], num_ctx=a.num_ctx, tetto=a.tetto)
    chiavi = ("passi", "token_inviati", "ricalcolati", "compattazioni", "compattazioni_fallite",
              "scarti", "copie_superate_somma")
    totale = {k: sum(v[k] for v in per_sessione.values()) for k in chiavi}
    totale["sessioni"] = len(per_sessione)
    totale["picco_massimo"] = max((v["picco"] for v in per_sessione.values()), default=0)
    totale["picco_mediano"] = int(statistics.median(v["picco"] for v in per_sessione.values())) if per_sessione else 0
    totale["token_per_passo"] = round(totale["token_inviati"] / max(1, totale["passi"]), 1)
    totale["ricalcolati_pct"] = round(100 * totale["ricalcolati"] / max(1, totale["token_inviati"]), 1)
    totale["system_tokens"] = estimate_tokens(SYSTEM)
    totale["schema_tokens"] = SCHEMA_TOKENS
    rapporto = {"parametri": {"num_ctx": a.num_ctx, "tetto": a.tetto}, "totale": totale}
    print(json.dumps(rapporto, ensure_ascii=False, indent=1))
    if a.json:
        a.json.write_text(json.dumps({**rapporto, "per_sessione": per_sessione},
                                     ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
