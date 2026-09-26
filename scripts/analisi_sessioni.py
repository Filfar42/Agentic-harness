"""Scompone le sessioni salvate in turni, e misura come e perche' finiscono.

Fase 1 del lavoro del 25/09/2026 (riprogettazione del ciclo). Non chiama il
modello e non tocca la rete: legge ``chat_sessions/`` e basta.

    python scripts/analisi_sessioni.py chat_sessions [--json uscita.json]

Cosa conta, turno per turno (un "turno" e' un'esecuzione di ``run_turn``:
comincia con un messaggio visibile dell'utente, o con la risposta a una
``ask_user_question``):

* **passi**: generazioni del modello. Dalla traccia ``think.passo`` quando
  c'e' (sessioni dal 23/08), altrimenti messaggi dell'assistente piu' i
  passi buttati dal watchdog e dal troncamento, che un messaggio non lo
  lasciano;
* **esito**: come si e' chiuso il turno -- risposta, riepilogo forzato,
  silenzio dopo i tool, errore, domanda all'utente, interruzione;
* **chiamate**: per tool, fallite per classe d'errore, chiamate identiche
  ripetute, fallimenti identici consecutivi, alternanze A-B-A-B;
* **solleciti**: ogni messaggio nascosto dell'harness, classificato;
* **pensiero**: caratteri di ``<think>`` per passo, e per tipo di mossa;
* **segnali di chiusura prematura**: il messaggio successivo dell'utente e'
  un "continua" / "vai avanti", oppure il turno si chiude con un punto di
  piano ancora aperto o con l'ultima verifica rossa.

I numeri servono a confrontare, non a certificare: le sessioni vengono da
modelli diversi (qwen2.5-coder 7B, qwen3.5 9B, Qwen3.8 27B, due endpoint di
OpenRouter) e da versioni diverse dell'harness.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Caricamento (due formati: .json con i messaggi dentro, o .json + .jsonl)
# ---------------------------------------------------------------------------


def carica(cartella: Path) -> list[dict[str, Any]]:
    sessioni = []
    for f in sorted(cartella.glob("*.json")):
        if f.name.endswith(".telemetry.json"):
            continue
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(meta, dict):
            continue
        messaggi = meta.get("messages")
        if messaggi is None:
            jl = f.with_suffix(".jsonl")
            messaggi = []
            if jl.exists():
                for riga in jl.read_text(encoding="utf-8").splitlines():
                    if riga.strip():
                        try:
                            messaggi.append(json.loads(riga))
                        except json.JSONDecodeError:
                            continue
        telemetria = None
        tf = cartella / f"{f.stem}.telemetry.json"
        if tf.exists():
            try:
                telemetria = json.loads(tf.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                telemetria = None
        sessioni.append({"id": f.stem, "meta": meta, "messaggi": messaggi,
                         "telemetria": telemetria})
    return sessioni


# ---------------------------------------------------------------------------
# Classificazione dei solleciti (per prefisso del testo, in tutte le versioni)
# ---------------------------------------------------------------------------

SOLLECITI = (
    ("watchdog", ("Ti ho interrotto: eri a",)),
    ("json_leak", ("Hai stampato una chiamata a tool come testo JSON",)),
    ("summary", ("Hai finito di usare i tool ma non hai scritto nulla",
                 "Hai usato dei tool ma questo passo non ha prodotto niente")),
    ("plan_summary", ("Chiudi adesso il turno con un messaggio breve, costruito",)),
    ("failed_summary", ("Chiudi adesso con un messaggio breve, e sii esplicito",)),
    ("tool", ("Hai risposto a parole, ma la richiesta riguarda il workspace",)),
    ("ask", ("Hai fatto bene a fermarti",)),
    ("plan", ("Questa richiesta contiene piu' azioni distinte",)),
    ("truncated", ("Il tuo ragionamento e' stato **troncato**",)),
    ("verify", ("Il comando `",)),
    ("loop", ("Hai eseguito `",)),
    ("coverage", ("Fermo: hai aggiunto",)),
    ("delega", ("Sono ",)),
    ("ripetizione", ("Hai gia' chiamato `",)),
    ("stallo", ("Hai chiamato `",)),
    ("oscillazione", ("Il file `",)),
    ("aggiornamento_workspace", ("<aggiornamento_workspace>",)),
    ("nota_harness", ("[Nota dell'harness]",)),
)


def tipo_sollecito(testo: str) -> str:
    for nome, prefissi in SOLLECITI:
        if any(testo.startswith(p) for p in prefissi):
            if nome == "delega" and "letture di fila" not in testo[:80]:
                continue
            return nome
    return "altro"


# ---------------------------------------------------------------------------
# Utilita' sui messaggi
# ---------------------------------------------------------------------------

_THINK = re.compile(r"<think>(.*?)</think>", re.S)


def pensiero_e_risposta(contenuto: str) -> tuple[str, str]:
    testo = contenuto or ""
    pensieri = _THINK.findall(testo)
    risposta = _THINK.sub("", testo)
    # Un <think> aperto e mai chiuso (passo interrotto): tutto pensiero.
    if "<think>" in risposta:
        prima, _, dopo = risposta.partition("<think>")
        pensieri.append(dopo)
        risposta = prima
    return "".join(pensieri), risposta.strip()


def busta(contenuto: Any) -> dict[str, Any] | None:
    try:
        p = json.loads(contenuto or "{}")
    except (TypeError, ValueError, RecursionError):
        return None
    return p if isinstance(p, dict) else None


def classe_errore(nome: str, p: dict[str, Any] | None, ok: Any) -> str | None:
    """None se il risultato e' riuscito, altrimenti una classe leggibile."""
    if p is None:
        return None if ok is not False else "illeggibile"
    if nome == "run_command" and p.get("esito") == "FALLITO":
        rc = p.get("returncode")
        if rc in (126, 127):
            return "comando_inesistente"
        if rc in (4, 5) and "pytest" in str(p.get("command", "")):
            return "pytest_uso_sbagliato"
        return "comando_rosso"
    if "error" not in p:
        return None if ok is not False else "fallito_senza_codice"
    codice = p.get("error_code")
    if codice:
        return str(codice)
    e = str(p.get("error") or "")
    regole = (
        ("old_string' non trovato", "edit_non_trovato"),
        ("volte in", "edit_ambiguo"),
        ("nessun punto ''", "piano_id_vuoto"),
        ("Non puoi dichiarare fatto", "piano_bloccato_da_rosso"),
        ("asserzione", "guardia_test"),
        ("Sandbox Docker", "sandbox"),
        ("non ha restituito risultati", "web_vuoto"),
        ("non e' disponibile in questo turno", "tool_non_disponibile"),
        ("non esiste", "percorso_inesistente"),
        ("La richiesta era di analizzare", "sola_lettura"),
        ("occupata", "porta_occupata"),
        ("non l'hai ancora letto", "scrittura_non_letta"),
        ("Argomenti JSON malformati", "invalid_json"),
        ("Chiamata troncata", "invalid_json_troncato"),
        ("Argomenti non validi", "invalid_arguments"),
        ("Errore interno", "errore_interno"),
    )
    for chiave, classe in regole:
        if chiave in e:
            return classe
    return "altro"


def firma(nome: str, argomenti: Any) -> str:
    try:
        a = json.loads(argomenti) if isinstance(argomenti, str) else argomenti
        return nome + ":" + json.dumps(a, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return nome + ":" + str(argomenti)


_CONTINUA = re.compile(
    r"^\s*(continua|prosegui|vai avanti|avanti|finisci|completa|termina|"
    r"riprendi|go on|continue|ok continua|procedi)\b", re.I)


# ---------------------------------------------------------------------------
# Turni
# ---------------------------------------------------------------------------


@dataclass
class Turno:
    sessione: str
    modello: str
    indice: int
    richiesta: str
    passi: int = 0
    passi_da_traccia: bool = False
    chiamate: Counter = field(default_factory=Counter)
    fallite: Counter = field(default_factory=Counter)
    solleciti: Counter = field(default_factory=Counter)
    ripetute_identiche: int = 0          # chiamate uguali a una gia' fatta nel turno
    fallimenti_identici_max: int = 0     # fallimenti identici di fila, massimo
    alternanze_ab: int = 0               # finestre A,B,A,B nelle firme
    pensiero_per_passo: list[int] = field(default_factory=list)
    risposta_chars: int = 0
    esito: str = ""
    piano_aperto_a_fine: int | None = None
    ultima_verifica_rossa: bool = False
    seguito_da_continua: bool = False
    json_nel_testo: int = 0
    tool_response_inventati: int = 0
    riepilogo_forzato: bool = False
    compattazioni: int = 0
    prompt_tokens_telemetria: int | None = None

    @property
    def n_chiamate(self) -> int:
        return sum(self.chiamate.values())

    @property
    def n_fallite(self) -> int:
        return sum(self.fallite.values())


def _sembra_json_di_tool(testo: str) -> bool:
    t = testo.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]
    return bool(re.search(r'\{\s*"(name|tool|function)"\s*:', t))


def turni_di(sessione: dict[str, Any]) -> list[Turno]:
    msgs = sessione["messaggi"]
    modello = str(sessione["meta"].get("model_name") or "?")
    inizi = []
    for i, m in enumerate(msgs):
        visibile_utente = m.get("role") == "user" and not m.get("hidden")
        risposta_domanda = m.get("role") == "tool" and m.get("answered")
        if visibile_utente or risposta_domanda:
            inizi.append(i)
    turni: list[Turno] = []
    for k, inizio in enumerate(inizi):
        fine = inizi[k + 1] if k + 1 < len(inizi) else len(msgs)
        tratto = msgs[inizio:fine]
        richiesta = str(msgs[inizio].get("content") or "")[:300]
        t = Turno(sessione["id"], modello, k, richiesta)
        passi_traccia = [
            int((m.get("think") or {}).get("passo") or 0)
            for m in tratto if m.get("role") == "assistant" and isinstance(m.get("think"), dict)
        ]
        n_assistant = sum(1 for m in tratto if m.get("role") == "assistant")
        viste: set[str] = set()
        storia: list[str] = []
        fall_di_fila = 0
        ultima_fallita = None
        ultima_verifica = None
        for m in tratto:
            ruolo = m.get("role")
            if ruolo == "user" and m.get("hidden"):
                tipo = tipo_sollecito(str(m.get("content") or ""))
                t.solleciti[tipo] += 1
            elif ruolo == "assistant":
                pensiero, risposta = pensiero_e_risposta(str(m.get("content") or ""))
                t.pensiero_per_passo.append(len(pensiero))
                if risposta and not m.get("tool_calls"):
                    t.risposta_chars += len(risposta)
                if not m.get("tool_calls") and _sembra_json_di_tool(risposta):
                    t.json_nel_testo += 1
                if "<tool_response>" in (m.get("content") or ""):
                    t.tool_response_inventati += 1
                if m.get("forzato"):
                    t.riepilogo_forzato = True
                for c in m.get("tool_calls") or []:
                    fn = c.get("function") or {}
                    nome = str(fn.get("name") or "?")
                    t.chiamate[nome] += 1
                    f = firma(nome, fn.get("arguments"))
                    if f in viste:
                        t.ripetute_identiche += 1
                    viste.add(f)
                    storia.append(f)
            elif ruolo == "tool":
                if m.get("answered"):
                    continue
                nome = str(m.get("name") or "?")
                p = busta(m.get("content"))
                classe = classe_errore(nome, p, m.get("ok"))
                if classe:
                    t.fallite[classe] += 1
                    chiave = firma(nome, m.get("args"))
                    fall_di_fila = fall_di_fila + 1 if chiave == ultima_fallita else 1
                    ultima_fallita = chiave
                    t.fallimenti_identici_max = max(t.fallimenti_identici_max, fall_di_fila)
                else:
                    fall_di_fila = 0
                    ultima_fallita = None
                if nome == "run_command" and p is not None:
                    ultima_verifica = p.get("esito") == "FALLITO"
                if nome == "manage_plan" and p is not None and "error" not in p:
                    punti = p.get("punti") or p.get("plan") or p.get("steps")
                    aperti = None
                    if isinstance(punti, list):
                        aperti = sum(1 for s in punti if isinstance(s, dict)
                                     and s.get("status") in ("todo", "doing", "in_corso", "da_fare"))
                    elif isinstance(punti, dict):
                        aperti = sum(v for k2, v in punti.items()
                                     if k2 in ("todo", "doing", "da_fare", "in_corso", "aperti")
                                     and isinstance(v, int))
                    if aperti is not None:
                        t.piano_aperto_a_fine = aperti
            elif ruolo == "summary":
                t.compattazioni += 1
        t.ultima_verifica_rossa = bool(ultima_verifica)
        # alternanze A,B,A,B (firme diverse che si alternano)
        for j in range(len(storia) - 3):
            a, b, c, d = storia[j:j + 4]
            if a != b and a == c and b == d:
                t.alternanze_ab += 1
        watch = t.solleciti.get("watchdog", 0) + t.solleciti.get("truncated", 0)
        if passi_traccia:
            t.passi = max(passi_traccia)
            t.passi_da_traccia = True
        else:
            t.passi = n_assistant + watch
        # esito
        ultimo_assistant = next((m for m in reversed(tratto) if m.get("role") == "assistant"), None)
        if any(m.get("role") == "pending_question" for m in tratto):
            t.esito = "domanda_aperta"
        elif fine < len(msgs) and msgs[fine].get("role") == "tool" and msgs[fine].get("answered"):
            t.esito = "domanda"
        elif fine < len(msgs) and msgs[fine].get("kind") == "gate_piano":
            # Il cancello sul piano: il turno si e' fermato per farlo leggere.
            t.esito = "domanda"
        elif any(m.get("role") == "error" for m in tratto):
            t.esito = "errore"
        elif ultimo_assistant is not None and ultimo_assistant.get("stopped"):
            t.esito = "interrotto"
        elif t.riepilogo_forzato:
            t.esito = "passi_finiti_riepilogo_forzato"
        else:
            _, risposta = pensiero_e_risposta(str((ultimo_assistant or {}).get("content") or ""))
            ha_tool = t.n_chiamate > 0
            if ultimo_assistant is not None and not ultimo_assistant.get("tool_calls") and risposta:
                t.esito = "risposta"
            elif ha_tool:
                t.esito = "silenzio_dopo_tool"
            elif ultimo_assistant is None:
                # Nessuna generazione registrata: nelle versioni di agosto un
                # errore di stream non lasciava traccia in cronologia.
                t.esito = "nessuna_risposta"
            else:
                t.esito = "vuoto"
        # il turno dopo e' un "continua"?
        if fine < len(msgs):
            succ = msgs[fine]
            if succ.get("role") == "user" and not succ.get("hidden"):
                t.seguito_da_continua = bool(_CONTINUA.match(str(succ.get("content") or "")))
        turni.append(t)
    return turni


# ---------------------------------------------------------------------------
# Aggregati
# ---------------------------------------------------------------------------


def _q(valori: Sequence[float], q: float) -> float:
    if not valori:
        return 0.0
    s = sorted(valori)
    k = min(len(s) - 1, max(0, round(q * (len(s) - 1))))
    return float(s[k])


def aggrega(turni: Iterable[Turno]) -> dict[str, Any]:
    turni = [t for t in turni if t.passi > 0]
    n = len(turni)
    if not n:
        return {"turni": 0}
    esiti = Counter(t.esito for t in turni)
    chiamate = sum(t.n_chiamate for t in turni)
    fallite = Counter()
    for t in turni:
        fallite.update(t.fallite)
    solleciti = Counter()
    for t in turni:
        solleciti.update(t.solleciti)
    pensiero = [p for t in turni for p in t.pensiero_per_passo]
    con_tool = [t for t in turni if t.n_chiamate]
    riusciti = [t for t in turni if t.esito in ("risposta", "domanda")]
    prematuri = [
        t for t in turni
        if t.esito == "risposta" and (
            t.seguito_da_continua or (t.piano_aperto_a_fine or 0) > 0 or t.ultima_verifica_rossa
        )
    ]
    return {
        "turni": n,
        "passi_totali": sum(t.passi for t in turni),
        "passi_mediana": statistics.median(t.passi for t in turni),
        "passi_p90": _q([t.passi for t in turni], 0.9),
        "esiti": dict(esiti),
        "chiusi_con_risposta_pct": round(100 * len(riusciti) / n, 1),
        "silenzio_dopo_tool_pct": round(100 * esiti.get("silenzio_dopo_tool", 0) / n, 1),
        "passi_finiti_pct": round(100 * esiti.get("passi_finiti_riepilogo_forzato", 0) / n, 1),
        "chiusure_sospette_pct": round(100 * len(prematuri) / n, 1),
        "seguiti_da_continua": sum(t.seguito_da_continua for t in turni),
        "chiamate": chiamate,
        "chiamate_per_passo": round(chiamate / max(1, sum(t.passi for t in turni)), 2),
        "fallite": dict(fallite.most_common()),
        "fallite_pct": round(100 * sum(fallite.values()) / max(1, chiamate), 1),
        "errori_di_protocollo": sum(fallite.get(k, 0) for k in (
            "invalid_json", "invalid_json_troncato", "invalid_arguments",
            "tool_not_allowed", "unknown_tool", "piano_id_vuoto")),
        "edit_non_trovato": fallite.get("edit_non_trovato", 0) + fallite.get("edit_ambiguo", 0),
        "json_nel_testo": sum(t.json_nel_testo for t in turni),
        "tool_response_inventati": sum(t.tool_response_inventati for t in turni),
        "ripetute_identiche": sum(t.ripetute_identiche for t in turni),
        "turni_con_ripetizioni": sum(1 for t in con_tool if t.ripetute_identiche),
        "fallimenti_identici_max": max((t.fallimenti_identici_max for t in turni), default=0),
        "alternanze_ab": sum(t.alternanze_ab for t in turni),
        "solleciti": dict(solleciti.most_common()),
        "solleciti_per_turno": round(sum(v for k, v in solleciti.items()
                                         if k not in ("aggiornamento_workspace",)) / n, 2),
        "pensiero_mediana_chars": _q(pensiero, 0.5),
        "pensiero_p90_chars": _q(pensiero, 0.9),
        "pensiero_totale_chars": sum(pensiero),
        "risposta_totale_chars": sum(t.risposta_chars for t in turni),
    }


def per_famiglia(modello: str) -> str:
    m = modello.lower()
    if "3.8" in m or "qwen3.8" in m:
        return "qwen3.8-27b"
    if "3.5" in m:
        return "qwen3.5-9b"
    if "2.5-coder" in m:
        return "qwen2.5-coder-7b"
    if "qwen3:8b" in m:
        return "qwen3-8b"
    if "stealth" in m or "/" in m:
        return "openrouter"
    return "sconosciuto"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("cartella", type=Path, nargs="?", default=Path("chat_sessions"))
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--turni", action="store_true", help="stampa anche i turni peggiori")
    a = ap.parse_args(argv)
    sessioni = carica(a.cartella)
    tutti: list[Turno] = []
    for s in sessioni:
        tutti.extend(turni_di(s))
    rapporto = {"sessioni": len(sessioni), "totale": aggrega(tutti), "per_modello": {}}
    famiglie = defaultdict(list)
    for t in tutti:
        famiglie[per_famiglia(t.modello)].append(t)
    for fam, ts in sorted(famiglie.items()):
        rapporto["per_modello"][fam] = aggrega(ts)
    print(json.dumps(rapporto, ensure_ascii=False, indent=1))
    if a.turni:
        peggiori = sorted(
            (t for t in tutti if t.passi),
            key=lambda t: (t.esito not in ("risposta", "domanda"), t.passi, t.n_fallite),
            reverse=True,
        )[:25]
        for t in peggiori:
            print(f"{t.sessione} #{t.indice} [{per_famiglia(t.modello)}] passi={t.passi} "
                  f"esito={t.esito} chiamate={t.n_chiamate} fallite={dict(t.fallite)} "
                  f"solleciti={dict(t.solleciti)} rip={t.ripetute_identiche} "
                  f"ab={t.alternanze_ab} :: {t.richiesta[:90]!r}")
    if a.json:
        a.json.write_text(json.dumps({
            "rapporto": rapporto,
            "turni": [{**asdict(t), "chiamate": dict(t.chiamate), "fallite": dict(t.fallite),
                       "solleciti": dict(t.solleciti)} for t in tutti],
        }, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
