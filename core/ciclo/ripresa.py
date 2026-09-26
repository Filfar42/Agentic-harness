"""Checkpoint fra i turni, diario degli effetti, riparazione delle chiamate orfane.

Tre pezzi, un'idea sola: **cio' che il turno sa non deve morire con il turno**.

1. **Checkpoint.** A fine turno ``StatoTurno.istantanea`` fotografa i fatti che
   contano per il turno dopo: perche' si e' fermato, le verifiche ancora rosse,
   le chiamate fallite piu' volte, i file scritti. Il server la salva nella
   sessione. Se il turno si era fermato senza finire (passi esauriti, stallo,
   stop) e l'utente scrive "continua", il turno nuovo riparte con quei rossi
   nel registro e con una nota ``<ripresa>`` in coda alla cronologia.

   Perche': il registro delle verifiche nasceva vuoto a ogni turno. Dopo un
   "continua" un test rosso del turno prima non esisteva piu', e il modello
   poteva chiudere con "i test passano" senza rilanciarli -- nessuna rete lo
   vedeva. Nei log di agosto i "continua" sono 54.

2. **Diario degli effetti.** Prima di ``write_file``, ``edit_file`` e
   ``run_command`` il ciclo accoda un record ``intento`` (con lo sha del file,
   se e' un file) e il server salva. Se il processo muore durante l'effetto,
   al turno dopo si sa che la chiamata era partita.

3. **Riparazione delle orfane.** Una chiamata senza risultato in cronologia
   rompe i template severi (il 500 di Qwen3.8 del 19/09) e lascia il modello
   senza sapere se l'effetto c'e' stato. ``ripara_orfani`` scrive un risultato
   onesto -- ``esito_sconosciuto``, con lo sha di prima e quello di adesso --
   invece di lasciare che il modello ripeta l'effetto alla cieca.

Riferimenti: i runtime a esecuzione durevole (LangGraph, Temporal, Restate)
scrivono l'intento prima dell'effetto e la ricevuta dopo; il passaggio di
consegne fra sessioni lunghe funziona con stato verificabile a macchina, non
con prosa (Anthropic, *Effective harnesses for long-running agents*).
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from ..verifiche import ROSSA, RegistroVerifiche, Verifica
from .segnali import e_una_prosecuzione

# Motivi di chiusura dopo i quali un "continua" riprende il lavoro.
RIPRENDIBILI = frozenset({
    "max_steps", "stallo", "stopped", "finestra_piena", "reasoning_budget", "error",
})
# I tool con un effetto sul mondo: prima di eseguirli si scrive l'intento.
EFFETTI = frozenset({"write_file", "edit_file", "run_command"})
RUOLO_INTENTO = "intento"


# ---------------------------------------------------------------------------
# Checkpoint e ripresa
# ---------------------------------------------------------------------------


def va_ripreso(checkpoint: Any, richiesta: str) -> bool:
    """Il turno che parte e' la prosecuzione di uno rimasto a meta'?"""
    if not isinstance(checkpoint, dict):
        return False
    return str(checkpoint.get("motivo") or "") in RIPRENDIBILI and e_una_prosecuzione(richiesta)


def ripristina_verifiche(registro: RegistroVerifiche, checkpoint: dict[str, Any]) -> int:
    """Rimette nel registro le verifiche rimaste rosse. Torna quante."""
    rimesse = 0
    for dati in checkpoint.get("verifiche_rosse") or []:
        if not isinstance(dati, dict) or not dati.get("identita"):
            continue
        identita = str(dati["identita"])
        if identita in registro.verifiche:
            continue
        codice = dati.get("returncode")
        verifica = Verifica(
            identita=identita,
            comando=str(dati.get("comando") or identita),
            ambito=str(dati.get("ambito") or "ignoto"),
            stato=ROSSA,
            returncode=codice if isinstance(codice, int) else None,
            tentativi=int(dati.get("tentativi") or 1),
        )
        verifica.vedi(verifica.comando)
        registro.verifiche[identita] = verifica
        rimesse += 1
    return rimesse


# ---------------------------------------------------------------------------
# Diario degli effetti
# ---------------------------------------------------------------------------


def _impronta(workspace: str, filepath: str) -> str | None:
    """Lo sha256 del file com'e' adesso, None se non esiste o non si legge."""
    if not workspace or not filepath:
        return None
    try:
        from ..tools import resolve_path

        percorso = resolve_path(workspace, filepath)
        return hashlib.sha256(Path(percorso).read_bytes()).hexdigest()
    except Exception:  # noqa: BLE001 - un'impronta mancata non deve fermare il turno
        return None


def intento(call_id: str, nome: str, args: dict[str, Any], workspace: str) -> dict[str, Any]:
    """Il record da accodare prima di un effetto. Non arriva mai al modello."""
    record: dict[str, Any] = {
        "role": RUOLO_INTENTO,
        "tool_call_id": call_id,
        "name": nome,
        # Vuoto ma presente: chi scorre la cronologia legge m["content"] su
        # ogni messaggio (UI, test, esportazioni) e non deve inciampare qui.
        "content": "",
        "ts": time.time(),
    }
    if nome in ("write_file", "edit_file"):
        filepath = str(args.get("filepath") or "")
        record["filepath"] = filepath
        record["sha_prima"] = _impronta(workspace, filepath)
    elif nome == "run_command":
        record["command"] = str(args.get("command") or "")[:300]
    return record


def ripara_orfani(ui_messages: list[dict[str, Any]], workspace: str = "") -> int:
    """Scrive un risultato per ogni chiamata rimasta senza. Torna quante.

    Il risultato va subito dopo i risultati gia' presenti della stessa
    chiamata dell'assistente: l'abbinamento chiamata-esito deve restare
    contiguo, o i template lo rifiutano.
    """
    con_esito = {
        str(m.get("tool_call_id") or "")
        for m in ui_messages
        if m.get("role") in ("tool", "pending_question")
    }
    intenti = {
        str(m.get("tool_call_id") or ""): m
        for m in ui_messages
        if m.get("role") == RUOLO_INTENTO
    }
    riparate = 0
    i = 0
    while i < len(ui_messages):
        msg = ui_messages[i]
        if msg.get("role") != "assistant" or not msg.get("tool_calls"):
            i += 1
            continue
        mancanti = [
            c for c in msg["tool_calls"]
            if isinstance(c, dict) and str(c.get("id") or "") not in con_esito
        ]
        if not mancanti:
            i += 1
            continue
        j = i + 1
        while j < len(ui_messages) and ui_messages[j].get("role") in ("tool", RUOLO_INTENTO):
            j += 1
        nuovi = [_esito_orfano(c, intenti.get(str(c.get("id") or "")), workspace) for c in mancanti]
        ui_messages[j:j] = nuovi
        riparate += len(nuovi)
        con_esito.update(str(c.get("id") or "") for c in mancanti)
        i = j + len(nuovi)
    return riparate


def _esito_orfano(call: dict[str, Any], record: dict[str, Any] | None, workspace: str) -> dict[str, Any]:
    fn = call.get("function") or {}
    nome = str(fn.get("name") or call.get("name") or "")
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except (TypeError, ValueError):
        args = {}
    if not isinstance(args, dict):
        args = {}
    if record is None:
        corpo: dict[str, Any] = {
            "error": "Chiamata senza esito: il turno si e' interrotto prima che finisse.",
            "error_code": "esito_sconosciuto",
            "retryable": False,
            "hint": (
                "Non si sa se e' stata eseguita. Se aveva un effetto, controlla lo "
                "stato (read_file, git status) prima di ripeterla."
            ),
        }
    else:
        corpo = {
            "error": (
                "Esito sconosciuto: il processo si e' interrotto mentre la chiamata "
                "era in corso."
            ),
            "error_code": "esito_sconosciuto",
            "retryable": False,
        }
        if nome in ("write_file", "edit_file"):
            prima = record.get("sha_prima")
            adesso = _impronta(workspace, str(record.get("filepath") or args.get("filepath") or ""))
            corpo["filepath"] = record.get("filepath")
            corpo["sha_prima"] = prima
            corpo["sha_adesso"] = adesso
            if prima == adesso:
                corpo["effetto"] = "non avvenuto: il file e' identico a prima della chiamata"
                corpo["hint"] = "Il file non e' cambiato: puoi ripetere la chiamata."
            else:
                corpo["effetto"] = "avvenuto: il file e' cambiato dopo la chiamata"
                corpo["hint"] = (
                    "Rileggi il file con read_file prima di rifare la modifica: "
                    "potrebbe essere gia' applicata."
                )
        else:
            corpo["hint"] = (
                "Il comando era partito: controlla il suo effetto prima di rilanciarlo."
            )
    return {
        "role": "tool",
        "tool_call_id": str(call.get("id") or ""),
        "name": nome,
        "content": json.dumps(corpo, ensure_ascii=False),
        "args": args,
        "duration_s": 0.0,
        "ok": False,
        "riparato": True,
        "ts": time.time(),
    }
