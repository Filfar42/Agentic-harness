"""Persistenza delle sessioni di chat su disco.

Migliorie rispetto alla versione precedente:
  * scrittura **atomica** (tmp + replace) per non corrompere il JSON se il
    processo muore a meta' salvataggio;
  * salvataggio **debounced**: il vecchio codice riscriveva l'intero file ad
    ogni singola tool call, con costo O(n^2) sulla lunghezza della chat;
  * indice delle sessioni in cache, cosi' la sidebar non rilegge N file JSON
    ad ogni richiesta: con decine di conversazioni lunghe sarebbero megabyte
    riletti ad ogni click.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import DATA_DIR

_SAVE_DEBOUNCE_S = 1.0
_last_save: dict[str, float] = {}


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def generate_session_id() -> str:
    return f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:4]}"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def derive_title(messages: list[dict]) -> str:
    for msg in messages:
        if msg.get("role") == "user" and msg.get("content"):
            title = " ".join(str(msg["content"]).split())
            return (title[:44] + "...") if len(title) > 44 else title
    return "Nuova conversazione"


def save_session(state: Any, *, force: bool = False) -> None:
    """Salva la sessione corrente. Debounced salvo ``force=True``."""
    session_id = state.get("current_session_id")
    if not session_id:
        return

    now = time.monotonic()
    if not force and (now - _last_save.get(session_id, 0.0)) < _SAVE_DEBOUNCE_S:
        return
    _last_save[session_id] = now

    ensure_dirs()
    payload = {
        "id": session_id,
        "title": derive_title(state.get("messages", [])),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "messages": state.get("messages", []),
        "touched_files": sorted(state.get("touched_files", set())),
        # File di cui l'agente conosce il contenuto: sopravvive fra i turni,
        # cosi' non e' costretto a rileggere lo stesso file ad ogni richiesta.
        "known_files": sorted(state.get("known_files", set())),
        # Piano di lavoro: sopravvive ai turni e ai riavvii. Un compito in
        # cinque punti non si esaurisce in un turno, e un piano che riparte da
        # zero ad ogni messaggio dell'utente non sarebbe un piano.
        "plan": list(state.get("plan", [])),
        # Foglio di note del compito: come il piano sopravvive ai turni, e a
        # differenza della cronologia sopravvive anche alla compattazione --
        # e' esattamente il motivo per cui esiste.
        "notes": list(state.get("notes", [])),
        # Ultima anteprima mostrata: riaprendo la conversazione il pannello
        # torna com'era. Le anteprime di applicazioni si ricontrollano al
        # momento -- un processo del turno di ieri non e' piu' vivo.
        "preview": state.get("preview") or None,
        # Allegati della conversazione: solo i metadati, i file veri stanno
        # nella cartella 'allegati/' del workspace.
        "attachments": list(state.get("attachments", [])),
        "workspace_dir": state.get("workspace_dir", os.getcwd()),
        "model_name": state.get("model_name", ""),
    }
    try:
        _atomic_write_json(DATA_DIR / f"{session_id}.json", payload)
    except OSError:
        pass  # il salvataggio non deve mai far crashare la UI


_index_cache: dict[str, tuple[float, int, dict[str, Any]]] = {}


def _session_summary(path: Path) -> dict[str, Any] | None:
    """Riga di indice per un file di sessione, con cache su (mtime, size).

    L'indice richiede il numero di messaggi, e per averlo bisogna aprire il
    JSON per intero: su una cartella con decine di conversazioni lunghe
    significa rileggere megabyte ad ogni click sulla sidebar. Il contenuto di
    un file cambia solo quando lo riscriviamo noi, quindi mtime+size sono una
    chiave di validita' sufficiente e costano una sola stat.
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    cached = _index_cache.get(path.name)
    if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    messages = data.get("messages", [])
    summary = {
        "id": data.get("id", path.stem),
        "title": data.get("title", "Conversazione"),
        "updated_at": data.get("updated_at", ""),
        "n_messages": sum(1 for m in messages if not m.get("hidden")),
        # La conversazione si e' fermata su una domanda dell'agente: la sidebar
        # lo segnala, cosi' non resta appesa in silenzio quando l'utente passa
        # a un'altra chat e poi torna.
        "pending": bool(messages) and messages[-1].get("role") == "pending_question",
    }
    _index_cache[path.name] = (stat.st_mtime, stat.st_size, summary)
    return summary


def list_sessions(limit: int = 60) -> list[dict[str, Any]]:
    """Indice leggero delle sessioni: metadati, senza rileggere i messaggi."""
    ensure_dirs()
    live = set()
    out: list[dict[str, Any]] = []
    for path in DATA_DIR.glob("*.json"):
        live.add(path.name)
        summary = _session_summary(path)
        if summary is not None:
            out.append(summary)
    for stale in set(_index_cache) - live:      # conversazioni cancellate
        _index_cache.pop(stale, None)
        _search_cache.pop(stale, None)
    out.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
    return out[:limit]


# ---------------------------------------------------------------------------
# Ricerca
# ---------------------------------------------------------------------------
#
# Cercare nel titolo soltanto non basta: i titoli sono i primi 44 caratteri del
# primo messaggio, e su una raccolta di prove ripetute cominciano tutti uguali
# ("Nel workspace deve esistere una ca..."). Quello che distingue una
# conversazione dall'altra sta dentro: un comando lanciato, un errore trovato,
# il nome di un file allegato.

# Quanto testo di una conversazione entra nell'indice. Serve un tetto: una
# sessione da 120 messaggi con dentro i corpi dei file supera il megabyte, e
# l'indice sta in memoria per tutte.
MAX_INDEX_CHARS = 120_000
# Quanto testo attorno alla parola trovata. Asimmetrico di proposito: la
# colonna e' stretta e ci stanno una quarantina di caratteri, quindi mettendone
# quarantasei *prima* la parola cercata finisce oltre il bordo destro e
# l'estratto mostra tutto tranne il motivo per cui e' li'.
SNIPPET_PRIMA = 14
SNIPPET_DOPO = 90


def _pezzi_cercabili(data: dict[str, Any]) -> dict[str, str]:
    """Il testo di una conversazione diviso per dove si e' trovato.

    Tre insiemi e non uno solo perche' l'esito deve poter dire *perche'* una
    riga e' in elenco: "l'hai chiamata cosi'", "l'hai detto", "ci avevi
    allegato quel file" sono tre modi diversi di ricordarsi una chat.
    """
    testi: list[str] = []
    comandi: list[str] = []
    for msg in data.get("messages", []):
        ruolo = msg.get("role")
        if ruolo in ("user", "assistant", "summary"):
            testi.append(str(msg.get("content") or ""))
        elif ruolo == "tool":
            # Degli argomenti si tiene la forma dell'azione -- quale comando, su
            # quale file -- non il corpo: cercare "def main" e trovare ogni
            # conversazione in cui e' passato un write_file sarebbe rumore.
            args = msg.get("args") or {}
            for chiave in ("command", "filepath", "pattern", "subfolder"):
                valore = args.get(chiave)
                if valore:
                    comandi.append(str(valore))
    allegati = [
        str(a.get("name") or "")
        for m in data.get("messages", [])
        for a in (m.get("attachments") or [])
    ] + [str(a.get("name") or "") for a in data.get("attachments", [])]
    return {
        "titolo": str(data.get("title") or ""),
        "allegato": " ".join(dict.fromkeys(allegati)),
        "comando": "\n".join(comandi)[:MAX_INDEX_CHARS],
        "testo": "\n".join(testi)[:MAX_INDEX_CHARS],
    }


# Indice di ricerca, con la stessa chiave di validita' dell'indice della
# sidebar: il contenuto di un file cambia solo quando lo riscriviamo noi.
_search_cache: dict[str, tuple[float, int, dict[str, str]]] = {}


def _cercabile(path: Path) -> dict[str, str] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    cached = _search_cache.get(path.name)
    if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    pezzi = _pezzi_cercabili(data)
    _search_cache[path.name] = (stat.st_mtime, stat.st_size, pezzi)
    return pezzi


def _estratto(testo: str, termine: str) -> str:
    """Il giro di parole attorno alla prima occorrenza, con la parola marcata."""
    dove = testo.lower().find(termine)
    if dove < 0:
        return ""
    inizio = max(0, dove - SNIPPET_PRIMA)
    fine = min(len(testo), dove + len(termine) + SNIPPET_DOPO)
    frammento = " ".join(testo[inizio:fine].split())
    return ("…" if inizio > 0 else "") + frammento + ("…" if fine < len(testo) else "")


# L'ordine e' quello con cui si spiega un risultato: prima come si chiama, poi
# cosa ci hai messo dentro, poi cosa e' stato detto, e infine cosa e' stato
# fatto -- il piu' voluminoso e il meno riconoscibile.
CAMPI_RICERCA = ("titolo", "allegato", "testo", "comando")


def search_sessions(query: str, limit: int = 40) -> list[dict[str, Any]]:
    """Conversazioni che contengono **tutti** i termini cercati.

    Tutti e non almeno uno: due parole scritte insieme sono un modo di
    restringere, e un OR restituirebbe piu' righe man mano che si aggiunge
    precisione -- l'esatto contrario di quello che si sta cercando di fare.
    """
    termini = [t for t in (query or "").lower().split() if t]
    if not termini:
        return []
    ensure_dirs()
    trovate: list[dict[str, Any]] = []
    for path in DATA_DIR.glob("*.json"):
        pezzi = _cercabile(path)
        riga = _session_summary(path)
        if pezzi is None or riga is None:
            continue
        minuscoli = {campo: pezzi[campo].lower() for campo in CAMPI_RICERCA}
        if not all(any(t in minuscoli[c] for c in CAMPI_RICERCA) for t in termini):
            continue
        # Dove mostrarlo: il primo campo che contiene il primo termine, cosi'
        # l'estratto parla della parola che l'utente ha scritto per prima.
        dove, estratto = "testo", ""
        for campo in CAMPI_RICERCA:
            for termine in termini:
                if termine in minuscoli[campo]:
                    dove, estratto = campo, _estratto(pezzi[campo], termine)
                    break
            if estratto:
                break
        trovate.append({**riga, "match_in": dove, "snippet": estratto})
    trovate.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
    return trovate[:limit]


def load_session(state: Any, session_id: str) -> bool:
    path = DATA_DIR / f"{session_id}.json"
    if not path.exists():
        return False
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return False

    state["current_session_id"] = data.get("id", session_id)
    state["messages"] = data.get("messages", [])
    state["touched_files"] = set(data.get("touched_files", []))
    state["known_files"] = set(data.get("known_files", []))
    state["plan"] = list(data.get("plan", []))
    state["notes"] = list(data.get("notes", []))
    state["preview"] = data.get("preview") or None
    state["attachments"] = list(data.get("attachments", []))
    if data.get("workspace_dir"):
        state["workspace_dir"] = data["workspace_dir"]
    return True


def delete_session(session_id: str) -> None:
    try:
        (DATA_DIR / f"{session_id}.json").unlink(missing_ok=True)
    except OSError:
        pass
    # Le due cache sono indicizzate per nome file: una conversazione cancellata
    # e poi ricreata con lo stesso id troverebbe altrimenti il testo di prima.
    _index_cache.pop(f"{session_id}.json", None)
    _search_cache.pop(f"{session_id}.json", None)


def new_session(state: Any) -> str:
    session_id = generate_session_id()
    state["current_session_id"] = session_id
    state["messages"] = []
    state["touched_files"] = set()
    state["known_files"] = set()
    state["plan"] = []
    state["notes"] = []
    state["preview"] = None
    state["attachments"] = []
    state["last_usage"] = {}
    return session_id


def bootstrap_session(state: Any) -> None:
    """Ripristina l'ultima sessione all'avvio, o ne crea una nuova."""
    if state.get("current_session_id"):
        return
    sessions = list_sessions(limit=1)
    if sessions and load_session(state, sessions[0]["id"]):
        return
    new_session(state)
