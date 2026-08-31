"""Persistenza delle sessioni di chat su disco.

## Due file per conversazione, e il motivo

``<id>.json`` tiene i **metadati** -- titolo, piano, note, allegati, anteprima,
quanti messaggi ci sono -- ed e' piccolo: si riscrive per intero ad ogni
salvataggio senza che costi niente.

``<id>.jsonl`` tiene i **messaggi**, uno per riga, e si scrive **in coda**.

Prima era un file solo, e la cronologia stava dentro il JSON dei metadati:
salvare voleva dire riserializzare e riscrivere tutto. Su una conversazione da
2,5 MB sono cinquanta millisecondi di GIL -- cioe' cinquanta millisecondi in
cui il server non risponde a nessuno -- pagati ad **ogni tool finito**. Una
conversazione lunga e' proprio quella in cui l'agente lavora di piu', quindi il
costo cresceva insieme al motivo di pagarlo.

Con la coda, un messaggio nuovo costa la sua riga e basta.

### Quando la coda non basta

I messaggi vengono anche **modificati sul posto**: la traccia del pensiero si
attacca al messaggio dell'assistente del passo appena chiuso, la cancellazione
di un allegato ripulisce i messaggi che lo nominavano, la compattazione
sostituisce un tratto di cronologia con un riassunto. Accodare, li', scriverebbe
la riga nuova lasciando indietro quella vecchia.

Due difese, e insieme coprono tutto:

1. **il conto e l'impronta**. Si accoda solo se i messaggi sono cresciuti *e*
   l'ultima riga gia' scritta e' ancora identica. Un accorciamento, una
   compattazione o una modifica all'ultimo messaggio scritto fanno cadere il
   controllo, e si riscrive tutto;
2. **la riscrittura a fine turno** (``riscrivi=True``). E' il momento in cui la
   cronologia e' ferma e completa: qualunque modifica a un messaggio piu'
   vecchio -- che la prima difesa non puo' vedere senza rileggere tutto --
   viene riportata su disco li'. Una volta per turno invece di una per tool.

Nel mezzo, il file puo' essere indietro di una annotazione su un messaggio gia'
scritto. Non dei messaggi: quelli ci sono tutti, in ordine.

Il resto era gia' cosi' e resta:
  * scrittura **atomica** (tmp + replace) per non corrompere niente se il
    processo muore a meta';
  * salvataggio **debounced**;
  * indice delle sessioni in cache -- e ora l'indice non apre nemmeno i
    messaggi, perche' ``n_messages`` sta nei metadati.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterable
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


# ---------------------------------------------------------------------------
# I messaggi: un file a parte, scritto in coda
# ---------------------------------------------------------------------------


def messages_path(session_id: str) -> Path:
    return DATA_DIR / f"{session_id}.jsonl"


def _riga(msg: dict[str, Any]) -> str:
    """Un messaggio su una riga sola. Deve essere **deterministico**: l'impronta
    che decide "posso accodare?" e' il confronto fra due di queste stringhe."""
    return json.dumps(msg, ensure_ascii=False, separators=(",", ":"))


# Cosa c'e' gia' su disco, per conversazione: (quante righe, l'ultima riga).
# L'ultima riga e' l'impronta: se il messaggio in quella posizione non
# serializza piu' uguale, qualcosa e' stato modificato e accodare mentirebbe.
_scritti: dict[str, tuple[int, str]] = {}


def dimentica_coda(session_id: str = "") -> None:
    """Scorda cosa risulta scritto: la prossima scrittura sara' completa.

    Serve ai test e a chi tocca i file da fuori. Senza argomenti, per tutte.
    """
    if session_id:
        _scritti.pop(session_id, None)
    else:
        _scritti.clear()


def _leggi_messaggi(session_id: str) -> list[dict[str, Any]] | None:
    """I messaggi dalla coda, o None se la coda non c'e' (formato vecchio)."""
    path = messages_path(session_id)
    if not path.exists():
        return None
    messaggi: list[dict[str, Any]] = []
    ultima = ""
    try:
        with open(path, encoding="utf-8") as fh:
            for grezza in fh:
                riga = grezza.strip()
                if not riga:
                    continue
                try:
                    messaggi.append(json.loads(riga))
                except json.JSONDecodeError:
                    # Una riga tronca puo' esistere solo in coda, se il
                    # processo e' morto durante l'append: si scarta quella e
                    # si tiene tutto il resto. Con un file unico, la stessa
                    # morte rendeva illeggibile **tutta** la conversazione.
                    continue
                ultima = riga
    except OSError:
        return None
    _scritti[session_id] = (len(messaggi), ultima)
    return messaggi


def _scrivi_messaggi(
    session_id: str, messages: list[dict[str, Any]], *, riscrivi: bool = False
) -> bool:
    """Manda su disco i messaggi: in coda se si puo', tutti se serve.

    Ritorna False se non c'e' riuscita. Chi chiama **deve** guardarlo: con i
    messaggi fuori dai metadati, scrivere i metadati dopo una coda fallita
    vorrebbe dire un file che dichiara una conversazione i cui messaggi non
    esistono da nessuna parte.
    """
    path = messages_path(session_id)
    gia = _scritti.get(session_id)

    accoda = (
        not riscrivi
        and gia is not None
        and path.exists()
        and gia[0] <= len(messages)
        # L'impronta: l'ultimo messaggio gia' scritto deve essere ancora
        # identico. Costa una serializzazione di **un** messaggio.
        and (gia[0] == 0 or _riga(messages[gia[0] - 1]) == gia[1])
    )

    righe = [_riga(m) for m in (messages[gia[0]:] if accoda else messages)]
    try:
        if accoda:
            if not righe:
                return True
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("".join(r + "\n" for r in righe))
        else:
            tmp = path.with_suffix(".jsonl.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write("".join(r + "\n" for r in righe))
            os.replace(tmp, path)
    except OSError:
        # Il salvataggio non deve mai far crashare la UI. Ma cio' che risulta
        # scritto non si sa piu': la prossima volta si riscrive tutto.
        _scritti.pop(session_id, None)
        return False
    _scritti[session_id] = (len(messages), righe[-1] if righe else (gia[1] if gia else ""))
    return True


def derive_title(messages: list[dict]) -> str:
    for msg in messages:
        if msg.get("role") == "user" and msg.get("content"):
            title = " ".join(str(msg["content"]).split())
            return (title[:44] + "...") if len(title) > 44 else title
    return "Nuova conversazione"


def save_session(state: Any, *, force: bool = False, riscrivi: bool = False) -> None:
    """Salva la sessione corrente. Debounced salvo ``force=True``.

    ``riscrivi=True`` rifa' la coda dei messaggi da capo invece di accodare.
    Va chiesta quando la cronologia puo' essere cambiata **dentro** invece che
    in fondo -- a fine turno, dopo una compattazione, dopo la cancellazione di
    un allegato -- perche' l'impronta da sola vede solo l'ultima riga scritta.
    """
    session_id = state.get("current_session_id")
    if not session_id:
        return

    now = time.monotonic()
    if not force and (now - _last_save.get(session_id, 0.0)) < _SAVE_DEBOUNCE_S:
        return
    _last_save[session_id] = now

    ensure_dirs()
    messages = state.get("messages", [])
    coda_ok = _scrivi_messaggi(session_id, messages, riscrivi=riscrivi)
    payload = {
        "id": session_id,
        "title": derive_title(messages),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        # I messaggi stanno nella coda accanto. Qui restano i due numeri che
        # servono alla sidebar: senza, l'indice dovrebbe aprire la cronologia
        # di ogni conversazione per contarla -- che e' esattamente quello che
        # faceva, e costava megabyte riletti ad ogni avvio.
        "n_messages": sum(1 for m in messages if not m.get("hidden")),
        "pending": bool(messages) and messages[-1].get("role") == "pending_question",
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
    if not coda_ok:
        # La coda non si e' scritta: i messaggi tornano qui dentro, dove
        # stavano prima che esistesse. Meglio un file grosso di una
        # conversazione perduta -- ed e' anche cio' che rende la conversione
        # al formato nuovo senza rischi: finche' la coda non c'e' davvero, i
        # metadati continuano a portarsi dietro tutto.
        payload["messages"] = messages
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
    # Il conto e la domanda in sospeso stanno nei metadati: l'indice non apre
    # piu' la cronologia di nessuno. Il ramo con ``messages`` e' il formato
    # vecchio, dove i messaggi erano dentro questo stesso file.
    messages = data.get("messages")
    if messages is None:
        n_messaggi = int(data.get("n_messages") or 0)
        pending = bool(data.get("pending"))
    else:
        n_messaggi = sum(1 for m in messages if not m.get("hidden"))
        pending = bool(messages) and messages[-1].get("role") == "pending_question"
    summary = {
        "id": data.get("id", path.stem),
        "title": data.get("title", "Conversazione"),
        "updated_at": data.get("updated_at", ""),
        "n_messages": n_messaggi,
        # La conversazione si e' fermata su una domanda dell'agente: la sidebar
        # lo segnala, cosi' non resta appesa in silenzio quando l'utente passa
        # a un'altra chat e poi torna.
        "pending": pending,
        # La cartella su cui e' stata fatta. E' cio' che lega una chat al suo
        # vault: nessun campo nuovo da scrivere e nessuna migrazione, il legame
        # era gia' salvato e non veniva letto da nessuno.
        "workspace_dir": str(data.get("workspace_dir") or ""),
    }
    _index_cache[path.name] = (stat.st_mtime, stat.st_size, summary)
    return summary


def chiave_cartella(path: str) -> str:
    """Forma confrontabile di un percorso: stessa cartella, stessa chiave.

    Su Windows lo stesso posto si scrive in almeno tre modi (maiuscole della
    lettera di unita', separatori misti, barra finale). Confrontare le stringhe
    grezze farebbe sparire dal vault le chat fatte proprio li'.
    """
    testo = str(path or "").strip()
    if not testo:
        return ""
    return os.path.normcase(os.path.normpath(testo))


def list_sessions(
    limit: int = 60,
    *,
    cartella: str = "",
    escludi: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Indice leggero delle sessioni: metadati, senza rileggere i messaggi.

    ``cartella`` tiene solo le conversazioni fatte li' dentro -- e' l'elenco
    di un vault. ``escludi`` toglie quelle di certe cartelle: e' l'elenco
    generale, che non deve mostrare le chat che vivono dentro un vault (li'
    si arriva dal vault, non da qui).
    """
    ensure_dirs()
    voluta = chiave_cartella(cartella)
    fuori = {chiave_cartella(p) for p in escludi} - {""}
    live = set()
    out: list[dict[str, Any]] = []
    for path in DATA_DIR.glob("*.json"):
        live.add(path.name)
        summary = _session_summary(path)
        if summary is None:
            continue
        casa = chiave_cartella(summary.get("workspace_dir", ""))
        if voluta and casa != voluta:
            continue
        if fuori and casa in fuori:
            continue
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


def _pezzi_cercabili(
    data: dict[str, Any], messaggi: list[dict[str, Any]] | None = None
) -> dict[str, str]:
    """Il testo di una conversazione diviso per dove si e' trovato.

    Tre insiemi e non uno solo perche' l'esito deve poter dire *perche'* una
    riga e' in elenco: "l'hai chiamata cosi'", "l'hai detto", "ci avevi
    allegato quel file" sono tre modi diversi di ricordarsi una chat.
    """
    if messaggi is None:
        messaggi = data.get("messages") or []
    testi: list[str] = []
    comandi: list[str] = []
    for msg in messaggi:
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
        for m in messaggi
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
    # La chiave guarda **anche** la coda dei messaggi: i metadati si riscrivono
    # ad ogni salvataggio, quindi in pratica basterebbero, ma legarsi a quel
    # "in pratica" vorrebbe dire un indice di ricerca che resta indietro il
    # giorno in cui qualcuno salva i due file separatamente.
    coda = messages_path(path.stem)
    try:
        s2 = coda.stat()
        chiave = (stat.st_mtime + s2.st_mtime, stat.st_size + s2.st_size)
    except OSError:
        chiave = (stat.st_mtime, stat.st_size)
    cached = _search_cache.get(path.name)
    if cached and cached[0] == chiave[0] and cached[1] == chiave[1]:
        return cached[2]
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    messaggi = data.get("messages")
    if messaggi is None:
        # Leggere la coda senza toccare ``_scritti``: qui si sta indicizzando,
        # non aprendo la conversazione, e dire "questo e' cio' che risulta
        # scritto" per una chat che magari e' aperta altrove sarebbe una
        # bugia con conseguenze.
        messaggi = []
        try:
            with open(coda, encoding="utf-8") as fh:
                for grezza in fh:
                    riga = grezza.strip()
                    if not riga:
                        continue
                    try:
                        messaggi.append(json.loads(riga))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            messaggi = []
    pezzi = _pezzi_cercabili(data, messaggi)
    _search_cache[path.name] = (chiave[0], chiave[1], pezzi)
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
    # La coda, se c'e'. Se non c'e' siamo su una conversazione del formato
    # vecchio, che teneva i messaggi dentro i metadati: si leggono da li' e la
    # coda nasce al primo salvataggio -- nessuna migrazione da lanciare, e
    # nessun file che si rompe se questa versione non viene mai aperta.
    dalla_coda = _leggi_messaggi(session_id)
    dai_metadati = data.get("messages")
    if dalla_coda is None:
        state["messages"] = list(dai_metadati or [])
        _scritti.pop(session_id, None)      # scrittura completa la prossima volta
    elif dai_metadati is not None and len(dai_metadati) > len(dalla_coda):
        # I due posti non concordano: e' successo solo se una scrittura della
        # coda era fallita e i messaggi erano rimasti nei metadati. Vince chi
        # ne ha di piu', e la coda si rifa' al prossimo salvataggio.
        state["messages"] = list(dai_metadati)
        _scritti.pop(session_id, None)
    else:
        state["messages"] = dalla_coda
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
        messages_path(session_id).unlink(missing_ok=True)
    except OSError:
        pass
    _scritti.pop(session_id, None)
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
