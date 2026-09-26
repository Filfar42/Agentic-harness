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


class ValutatoreLimite:
    """Toglie tutto cio' che e' candidato: il massimo che *qualunque* valutatore
    (Laya, Jev, regole) potrebbe liberare con la selezione. Serve a dire quante
    compattazioni una selezione potrebbe evitare nel migliore dei casi."""

    nome = "limite"

    def valuta(self, _stato: Any, domande: dict[str, Any]) -> dict[str, float]:
        return dict.fromkeys(domande, 0.0)


class ValutatoreOracolo:
    """Un valutatore che conosce il futuro della sessione vera.

    Tiene una chiamata (e il suo risultato) solo se il suo oggetto -- il file,
    il comando, il pattern -- ricompare piu' avanti: in una chiamata o nel
    testo dell'assistente. E' il meglio che un valutatore di *pertinenza*
    potrebbe fare senza perdere cose che poi servono: Laya o Jev possono al
    massimo avvicinarlo, perche' il futuro non lo vedono. ``futuro`` lo
    aggiorna il rigiocatore a ogni compattazione.
    """

    nome = "oracolo"

    def __init__(self) -> None:
        self.futuro: list[dict[str, Any]] = []

    def valuta(self, stato: Any, domande: dict[str, Any]) -> dict[str, float]:
        try:
            args = json.loads(str(stato["call"]["input"]).rstrip("…"))
        except (TypeError, ValueError, KeyError):
            args = {}
        oggetto = ""
        if isinstance(args, dict):
            oggetto = str(args.get("filepath") or args.get("command") or args.get("pattern")
                          or args.get("path") or args.get("query") or "")
        oggetto = " ".join(oggetto.split())[:120]
        serve = bool(oggetto) and _ricompare(self.futuro, oggetto)
        return dict.fromkeys(domande, 1.0 if serve else 0.0)


def _ricompare(futuro: list[dict[str, Any]], oggetto: str) -> bool:
    for m in futuro:
        if m.get("role") == "assistant":
            if oggetto in " ".join(str(m.get("content") or "").split()):
                return True
            for c in m.get("tool_calls") or []:
                argomenti = " ".join(str((c.get("function") or {}).get("arguments") or "").split())
                if oggetto in argomenti:
                    return True
        elif m.get("role") == "user" and not m.get("hidden"):
            if oggetto in " ".join(str(m.get("content") or "").split()):
                return True
    return False


def _richiesto_dopo(futuro: list[dict[str, Any]], nome: str, args: dict[str, Any]) -> bool:
    """La sessione vera richiede di nuovo la stessa cosa piu' avanti?

    Indizio debole che il risultato tolto serviva: il modello di allora vedeva
    un altro contesto, e una rilettura puo' essere anche solo abitudine.
    """
    chiave = ""
    if nome == "read_file":
        chiave = str(args.get("filepath") or "")
    elif nome == "run_command":
        chiave = " ".join(str(args.get("command") or "").split())
    if not chiave:
        return False
    for m in futuro:
        if m.get("role") != "assistant":
            continue
        for c in m.get("tool_calls") or []:
            fn = c.get("function") or {}
            if fn.get("name") != nome:
                continue
            try:
                a = json.loads(fn.get("arguments") or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(a, dict):
                continue
            altra = (str(a.get("filepath") or "") if nome == "read_file"
                     else " ".join(str(a.get("command") or "").split()))
            if altra == chiave:
                return True
    return False


def rigioca(messaggi: list[dict[str, Any]], *, num_ctx: int, tetto: int,
            soglia: float = 0.75, selezione: str = "spenta",
            valutatore_esterno: Any = None) -> dict[str, Any]:
    budgets = budgets_for(num_ctx, tetto)
    params = GenParams(num_ctx=num_ctx, max_tokens=8192)
    finestra = finestra_efficace(num_ctx, tetto)
    storia: list[dict[str, Any]] = []
    precedente: list[dict[str, Any]] | None = None
    inviati: list[int] = []
    ricalcolati = 0
    superate: list[int] = []
    compattazioni = scarti = fallite = 0
    sel_riuscite = sel_insufficienti = sel_domande = sel_candidati = 0
    sel_tolti = sel_accorciati = sel_richiesti_dopo = sel_tolti_rr = 0
    riass_tolti = riass_richiesti_dopo = 0
    candidati_per_evento: list[int] = []
    ferma_fino_a = 0
    passo = 0
    valutatore: Any = valutatore_esterno or {
        "limite": ValutatoreLimite, "oracolo": ValutatoreOracolo}.get(selezione, lambda: None)()
    for k, msg in enumerate(messaggi):
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
                selezionata = False
                if selezione != "spenta" and hasattr(agent_mod, "proponi_selezione"):
                    if isinstance(valutatore, ValutatoreOracolo):
                        # Il futuro "json" dell'argomento: dal messaggio corrente in poi.
                        valutatore.futuro = [
                            {**m, "tool_calls": [
                                {**c, "function": {**(c.get("function") or {}), "arguments": str(
                                    (c.get("function") or {}).get("arguments") or "")}}
                                for c in (m.get("tool_calls") or [])]}
                            for m in messaggi[k:]
                        ]
                    def pressione(msgs: list[dict[str, Any]]) -> float:
                        return agent_mod.context_pressure(
                            _costruisci(msgs, budgets), finestra, reserved_tokens=SCHEMA_TOKENS)

                    prop = agent_mod.proponi_selezione(
                        storia, budgets=budgets, strip_thinking=True, finestra=finestra,
                        valutatore=valutatore,
                        **({"pressione": pressione} if selezione != "limite" else {}),
                    )
                    if prop is not None:
                        fine, sel = prop
                        candidati_per_evento.append(sel.candidati)
                        sel_candidati += sel.candidati
                        # Le domande che un valutatore vero riceverebbe: due a chiamata.
                        sel_domande += 2 * sel.candidati
                        storia.insert(fine, sel.record)
                        prova = _costruisci(storia, budgets)
                        if agent_mod.context_pressure(
                            prova, finestra, reserved_tokens=SCHEMA_TOKENS
                        ) <= agent_mod.selezione_mod.QUOTA_OBIETTIVO:
                            selezionata = True
                            sel_riuscite += 1
                            api = prova
                            tolte = set(sel.record["decisioni"])
                            sel_tolti += sel.eliminati
                            sel_accorciati += sel.troncati
                            chiamate = {
                                str(c.get("id") or ""): c for m in storia
                                if m.get("role") == "assistant" for c in (m.get("tool_calls") or [])
                            }
                            futuro = messaggi[k:]
                            for cid in tolte:
                                fn = (chiamate.get(cid) or {}).get("function") or {}
                                if fn.get("name") not in ("read_file", "run_command"):
                                    continue
                                try:
                                    a = json.loads(fn.get("arguments") or "{}")
                                except (TypeError, ValueError):
                                    a = {}
                                if not isinstance(a, dict):
                                    continue
                                sel_tolti_rr += 1
                                if _richiesto_dopo(futuro, str(fn.get("name") or ""), a):
                                    sel_richiesti_dopo += 1
                        else:
                            del storia[fine]
                            sel_insufficienti += 1
                esito = None if selezionata else agent_mod.compatta_cronologia(
                    storia, backend=RiassuntoreFinto(), params=params, budgets=budgets,
                    strip_thinking=True, finestra=finestra, schedario=None,
                )
                if selezionata:
                    pass
                elif esito is None:
                    fallite += 1
                    ferma_fino_a = passo + 3
                else:
                    compattazioni += 1
                    api = _costruisci(storia, budgets)
                    # Il termine di paragone del "richiesto di nuovo": il
                    # riassunto toglie *tutte* le chiamate del tratto. Quante
                    # di quelle la sessione vera richiede poi di nuovo?
                    ultimo = max(i for i, m in enumerate(storia) if m.get("role") == "summary")
                    rec = storia[ultimo]
                    futuro = messaggi[k:]
                    for m in storia[int(rec.get("source_start") or 0):int(rec.get("source_end") or 0)]:
                        if m.get("role") != "assistant":
                            continue
                        for c in m.get("tool_calls") or []:
                            fn = c.get("function") or {}
                            nome_c = str(fn.get("name") or "")
                            if nome_c not in ("read_file", "run_command"):
                                continue
                            try:
                                a = json.loads(fn.get("arguments") or "{}")
                            except (TypeError, ValueError):
                                continue
                            if not isinstance(a, dict):
                                continue
                            riass_tolti += 1
                            if _richiesto_dopo(futuro, nome_c, a):
                                riass_richiesti_dopo += 1
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
        "selezioni_riuscite": sel_riuscite,
        "selezioni_insufficienti": sel_insufficienti,
        "selezione_candidati": sel_candidati,
        "selezione_domande": sel_domande,
        "selezione_tolti": sel_tolti,
        "selezione_accorciati": sel_accorciati,
        "selezione_richiesti_dopo": sel_richiesti_dopo,
        # Denominatore del "richiesto di nuovo": solo read_file e run_command,
        # gli unici per cui "la stessa cosa richiesta" si riconosce bene.
        "selezione_tolti_letture_comandi": sel_tolti_rr,
        "riassunto_tolti": riass_tolti,
        "riassunto_richiesti_dopo": riass_richiesti_dopo,
        "candidati_per_evento": candidati_per_evento,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("cartella", type=Path, nargs="?", default=Path("chat_sessions"))
    ap.add_argument("--num-ctx", type=int, default=65_536)
    ap.add_argument("--tetto", type=int, default=32_768)
    ap.add_argument("--min-passi", type=int, default=3)
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--selezione", choices=("spenta", "regole", "limite", "oracolo"),
                    default="spenta",
                    help="compattazione selettiva prima del riassunto (core/selezione.py)")
    a = ap.parse_args(argv)
    per_sessione = {}
    for s in carica(a.cartella):
        if sum(1 for m in s["messaggi"] if m.get("role") == "assistant") < a.min_passi:
            continue
        per_sessione[s["id"]] = rigioca(s["messaggi"], num_ctx=a.num_ctx, tetto=a.tetto,
                                        selezione=a.selezione)
    chiavi = ("passi", "token_inviati", "ricalcolati", "compattazioni", "compattazioni_fallite",
              "scarti", "copie_superate_somma", "selezioni_riuscite", "selezioni_insufficienti",
              "selezione_candidati", "selezione_domande", "selezione_tolti",
              "selezione_accorciati", "selezione_richiesti_dopo",
              "selezione_tolti_letture_comandi", "riassunto_tolti", "riassunto_richiesti_dopo")
    totale = {k: sum(v[k] for v in per_sessione.values()) for k in chiavi}
    totale["sessioni"] = len(per_sessione)
    totale["picco_massimo"] = max((v["picco"] for v in per_sessione.values()), default=0)
    totale["picco_mediano"] = int(statistics.median(v["picco"] for v in per_sessione.values())) if per_sessione else 0
    totale["token_per_passo"] = round(totale["token_inviati"] / max(1, totale["passi"]), 1)
    totale["ricalcolati_pct"] = round(100 * totale["ricalcolati"] / max(1, totale["token_inviati"]), 1)
    totale["system_tokens"] = estimate_tokens(SYSTEM)
    totale["schema_tokens"] = SCHEMA_TOKENS
    eventi = [n for v in per_sessione.values() for n in v["candidati_per_evento"]]
    if eventi:
        totale["candidati_per_evento_mediana"] = int(statistics.median(eventi))
        totale["candidati_per_evento_max"] = max(eventi)
    rapporto = {"parametri": {"num_ctx": a.num_ctx, "tetto": a.tetto, "selezione": a.selezione},
                "totale": totale}
    print(json.dumps(rapporto, ensure_ascii=False, indent=1))
    if a.json:
        a.json.write_text(json.dumps({**rapporto, "per_sessione": per_sessione},
                                     ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
