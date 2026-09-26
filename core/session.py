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
import threading
import time
import uuid
import re
import logging
from functools import wraps
from collections import OrderedDict
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import DATA_DIR
from .atomic import write_text as atomic_write_text
from .jsonsafe import JsonBoundaryError, loads_object
from .textutils import strip_think

logger = logging.getLogger(__name__)
# Fixed striped locks avoid unbounded lock allocation from supplied ids.
_storage_locks = tuple(threading.RLock() for _ in range(64))


def _session_locked(function: Any) -> Any:
    """Serialize per-session storage transactions within this process."""
    @wraps(function)
    def wrapped(state: Any, *args: Any, **kwargs: Any) -> Any:
        sid = state.get("current_session_id", "") if hasattr(state, "get") else state
        with _storage_locks[hash(str(sid)) % len(_storage_locks)]:
            return function(state, *args, **kwargs)
    return wrapped


def _valid_id(session_id: str) -> bool:
    return isinstance(session_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id) is not None


def _read_metadata(path: Path) -> dict[str, Any] | None:
    """Reject corrupted stored shapes before indexing or restoring state."""
    try:
        with path.open(encoding="utf-8") as stream:
            data = loads_object(stream.read(64_000_001), max_chars=64_000_000, max_nodes=1_000_000)
        for key in ("touched_files", "known_files"):
            if key in data and (not isinstance(data[key], list) or any(not isinstance(x, str) for x in data[key])):
                raise JsonBoundaryError(f"Invalid storage field: {key}")
        for key in ("messages", "attachments", "plan", "notes"):
            if key in data and (not isinstance(data[key], list) or any(not isinstance(x, dict) for x in data[key])):
                raise JsonBoundaryError(f"Invalid storage field: {key}")
        if data.get("preview") is not None and not isinstance(data["preview"], dict):
            raise JsonBoundaryError("Invalid preview")
        if data.get("checkpoint") is not None and not isinstance(data["checkpoint"], dict):
            raise JsonBoundaryError("Invalid checkpoint")
        if "n_messages" in data and (type(data["n_messages"]) is not int or data["n_messages"] < 0):
            raise JsonBoundaryError("Invalid message count")
        return data
    except (OSError, UnicodeError, JsonBoundaryError):
        logger.warning("Cannot load session metadata: %s", path, exc_info=True)
        return None

_SAVE_DEBOUNCE_S = 1.0
_last_save: dict[str, float] = {}
MAX_TURN_TELEMETRY = 50
MAX_TELEMETRY_BYTES = 2 * 1024 * 1024


def bounded_turn_telemetry(value: Any) -> list[dict[str, Any]]:
    """Keep recent diagnostics within the on-disk budget, independently of chat.

    Invalid optional diagnostics cannot prevent a conversation from loading.
    An oversized latest turn retains its totals when dropping call details is
    sufficient. The byte count includes indentation used by session metadata.
    """
    if not isinstance(value, list):
        return []
    kept: list[dict[str, Any]] = []
    size = 32  # enclosing object, field name, array and newlines
    for raw_entry in reversed(value[-MAX_TURN_TELEMETRY:]):
        entry = raw_entry
        if not isinstance(entry, dict):
            continue
        try:
            encoded = json.dumps(entry, ensure_ascii=False, indent=2, allow_nan=False)
            entry_size = len(encoded.encode("utf-8")) + 4 * (encoded.count("\n") + 1) + 2
            if entry_size + 32 > MAX_TELEMETRY_BYTES:
                calls = entry.get("calls")
                previous = entry.get("calls_truncated", 0)
                entry = {
                    key: entry[key] for key in (
                        "version", "totals", "since_offset", "end_offset", "turn_reason",
                        "steps", "recorded_at", "turn_wall_ms", "cruscotto",
                    ) if key in entry
                }
                entry.update(calls=[], calls_truncated=(len(calls) if isinstance(calls, list) else 0)
                             + (previous if type(previous) is int and previous >= 0 else 0))
                encoded = json.dumps(entry, ensure_ascii=False, indent=2, allow_nan=False)
                entry_size = len(encoded.encode("utf-8")) + 4 * (encoded.count("\n") + 1) + 2
        except (TypeError, ValueError, OverflowError, RecursionError):
            continue
        if entry_size + 32 > MAX_TELEMETRY_BYTES:
            continue
        if entry_size + size > MAX_TELEMETRY_BYTES:
            break
        kept.append(json.loads(encoded))
        size += entry_size
    return list(reversed(kept))


def record_turn_telemetry(state: Any, telemetry: Any, *, reason: str, steps: int,
                          qualita: Any = None, cruscotto: Any = None) -> None:
    """Attach a completed turn snapshot before the final session save.

    ``qualita`` e' lo stato delle verifiche alla fine del turno. Sta qui e non
    accanto ai messaggi perche' e' un fatto del **turno**: riaprendo la sessione
    si deve poter vedere che quel turno si e' chiuso con due rossi aperti, e
    non solo che si e' chiuso.
    """
    if not isinstance(telemetry, dict) or not telemetry:
        return
    entry = {
        **telemetry,
        "turn_reason": str(reason)[:64],
        "steps": max(0, int(steps)),
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
    }
    if isinstance(qualita, dict) and qualita:
        entry["qualita"] = qualita
    # Le righe della timeline del cruscotto (``core/cruscotto.Registro``):
    # come il turno e' stato visto dal vivo, per chi riapre la conversazione.
    if isinstance(cruscotto, list) and cruscotto:
        entry["cruscotto"] = cruscotto
    previous = state.get("turn_telemetry")
    state["turn_telemetry"] = bounded_turn_telemetry(
        [*(previous if isinstance(previous, list) else []), entry]
    )


def telemetry_path(session_id: str) -> Path:
    if not _valid_id(session_id):
        raise ValueError("Invalid session id")
    return DATA_DIR / f"{session_id}.telemetry.json"


def _telemetry_marker(session_id: str, value: Any) -> tuple:
    # I record pubblicati sono snapshot: record_turn_telemetry sostituisce la
    # lista. Identita', lunghezza e ultimo record rilevano anche append e clear
    # senza scandire megabyte a ogni tool. Il riferimento impedisce riuso di id.
    return (session_id, value, len(value) if isinstance(value, list) else -1,
            value[-1] if isinstance(value, list) and value else None)


def _save_telemetry(state: Any, session_id: str) -> None:
    value = state.get("turn_telemetry")
    previous = state.get("_telemetry_saved")
    marker = _telemetry_marker(session_id, value)
    if (isinstance(previous, tuple) and len(previous) == 4
            and previous[0] == session_id and previous[1] is value
            and previous[2] == marker[2] and previous[3] is marker[3]):
        return
    turns = bounded_turn_telemetry(value)
    # Anche la lista vuota va scritta: puo' azzerare un sidecar precedente.
    _atomic_write_json(telemetry_path(session_id), {"turn_telemetry": turns})
    state["turn_telemetry"] = turns
    state["_telemetry_saved"] = _telemetry_marker(session_id, turns)


def _load_telemetry(session_id: str) -> list[dict[str, Any]] | None:
    path = telemetry_path(session_id)
    try:
        if not path.exists():
            return None
        if path.stat().st_size > MAX_TELEMETRY_BYTES:
            raise ValueError("Telemetry sidecar exceeds its byte budget")
        with path.open(encoding="utf-8") as stream:
            payload = loads_object(stream.read(MAX_TELEMETRY_BYTES + 1),
                                   max_chars=MAX_TELEMETRY_BYTES, max_nodes=1_000_000)
        return bounded_turn_telemetry(payload.get("turn_telemetry"))
    except (OSError, UnicodeError, ValueError):
        logger.warning("Cannot load optional telemetry: %s", path, exc_info=True)
        return None


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def generate_session_id() -> str:
    return f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex}"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False))


# ---------------------------------------------------------------------------
# I messaggi: un file a parte, scritto in coda
# ---------------------------------------------------------------------------


def messages_path(session_id: str) -> Path:
    if not _valid_id(session_id):
        raise ValueError("Invalid session id")
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
    sporca = False
    try:
        with open(path, encoding="utf-8") as fh:
            for grezza in fh:
                riga = grezza.strip()
                if not riga:
                    continue
                try:
                    messaggi.append(loads_object(riga, max_chars=16_777_216))
                except JsonBoundaryError:
                    # Una riga tronca puo' esistere solo in coda, se il
                    # processo e' morto durante l'append: si scarta quella e
                    # si tiene tutto il resto. Con un file unico, la stessa
                    # morte rendeva illeggibile **tutta** la conversazione.
                    sporca = True
                    continue
                ultima = riga
    except (OSError, UnicodeError):
        return None
    if sporca:
        # Scartata alla lettura, ma **sul disco c'e' ancora**: senza questa
        # riga il file si portava dietro la riga rotta per sempre, perche' la
        # scrittura successiva accodava dopo di lei. Dimenticare cosa risulta
        # scritto forza una riscrittura completa al prossimo salvataggio, che
        # e' l'unico gesto che la toglie.
        _scritti.pop(session_id, None)
    else:
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

    try:
        righe = [_riga(m) for m in (messages[gia[0]:] if accoda else messages)]
        if accoda:
            if not righe:
                return True
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("".join(r + "\n" for r in righe))
                fh.flush()
                os.fsync(fh.fileno())
        else:
            atomic_write_text(path, "".join(r + "\n" for r in righe))
    except (OSError, UnicodeError, ValueError, TypeError):
        # Il salvataggio non deve mai far crashare la UI. Ma cio' che risulta
        # scritto non si sa piu': la prossima volta si riscrive tutto.
        _scritti.pop(session_id, None)
        return False
    _scritti[session_id] = (len(messages), righe[-1] if righe else (gia[1] if gia else ""))
    return True


# Ruoli che stanno nella cronologia salvata ma non sono messaggi per nessuno:
# l'``intento`` e' il diario degli effetti (``core/ciclo/ripresa.py``), scritto
# prima di un'azione perche' un crash lasci traccia che l'azione era partita;
# la ``selezione`` e' la compattazione selettiva (``core/selezione.py``).
# ``memoria`` e' la goccia della memoria del progetto a fine turno: si vede
# nel thread, ma non e' un messaggio di nessuno.
RUOLI_DI_SERVIZIO = frozenset({"intento", "selezione", "memoria"})


def conta_visibili(messages: list[dict]) -> int:
    """I messaggi che la sidebar conta: niente solleciti, niente diario."""
    return sum(
        1 for m in messages
        if not m.get("hidden") and m.get("role") not in RUOLI_DI_SERVIZIO
    )


MAX_TITOLO_CHARS = 120


def _pulisci_titolo(valore: object) -> str:
    return " ".join(str(valore or "").split())[:MAX_TITOLO_CHARS]


@_session_locked
def aggiorna_metadati(session_id: str, **campi: Any) -> bool:
    """Cambia campi dei metadati sul disco **senza** toccare ``updated_at``.

    Rinominare o archiviare una conversazione non e' averci lavorato: se la
    data cambiasse, la chat rinominata salirebbe in cima all'elenco come se
    fosse appena successo qualcosa. Torna False se il file non c'e' (una chat
    nuova mai salvata) o non si scrive: chi chiama salva per la strada normale.
    """
    if not _valid_id(session_id):
        return False
    path = DATA_DIR / f"{session_id}.json"
    data = _read_metadata(path) if path.exists() else None
    if data is None:
        return False
    if "titolo_utente" in campi:
        titolo = _pulisci_titolo(campi["titolo_utente"])
        data["titolo_utente"] = titolo or None
        if titolo:
            data["title"] = titolo
        elif campi.get("titolo_ricavato"):
            data["title"] = campi["titolo_ricavato"]
    if "archiviata" in campi:
        data["archiviata"] = bool(campi["archiviata"])
    if "workspace_dir" in campi:
        data["workspace_dir"] = str(campi["workspace_dir"] or "")
    try:
        _atomic_write_json(path, data)
    except (OSError, UnicodeError, ValueError):
        logger.exception("Cannot update metadata of session %s", session_id)
        return False
    return True


def leggi_metadati(session_id: str) -> dict[str, Any] | None:
    """I metadati di una conversazione (piano, checkpoint, ...), senza messaggi."""
    if not _valid_id(session_id):
        return None
    path = DATA_DIR / f"{session_id}.json"
    return _read_metadata(path) if path.exists() else None


def ultima_risposta_salvata(session_id: str, max_chars: int = 400) -> str:
    """Il testo dell'ultima risposta dell'agente, letto dalla coda del file.

    Serve alla scheda "Riprendi da qui" del progetto, che mostra dove si era
    arrivati. Si legge solo la **fine** della cronologia (gli ultimi 256 KB):
    una conversazione lunga sono megabyte, e la risposta che interessa e'
    sempre fra gli ultimi messaggi.
    """
    if not _valid_id(session_id):
        return ""
    path = messages_path(session_id)
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 262_144))
            righe = stream.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for riga in reversed(righe):
        try:
            msg = json.loads(riga)
        except ValueError:
            continue
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        testo = " ".join(strip_think(str(msg.get("content") or "")).split())
        if testo:
            return testo if len(testo) <= max_chars else testo[: max_chars - 1].rstrip() + "…"
    return ""


def derive_title(messages: list[dict]) -> str:
    """Il titolo e' il primo messaggio **visibile** dell'utente.

    ``hidden`` va saltato: i solleciti che l'harness accoda hanno ruolo
    ``user``, e una conversazione ripresa da un sollecito prendeva per titolo
    il testo del sollecito -- che parla all'agente, non descrive il lavoro.
    """
    for msg in messages:
        if msg.get("hidden"):
            continue
        if msg.get("role") == "user" and msg.get("content"):
            title = " ".join(str(msg["content"]).split())
            return (title[:44] + "...") if len(title) > 44 else title
    return "Nuova conversazione"


@_session_locked
def save_session(state: Any, *, force: bool = False, riscrivi: bool = False) -> bool:
    """Salva la sessione corrente. Debounced salvo ``force=True``.

    ``riscrivi=True`` rifa' la coda dei messaggi da capo invece di accodare.
    Va chiesta quando la cronologia puo' essere cambiata **dentro** invece che
    in fondo -- a fine turno, dopo una compattazione, dopo la cancellazione di
    un allegato -- perche' l'impronta da sola vede solo l'ultima riga scritta.
    """
    session_id = state.get("current_session_id")
    if not session_id:
        return True
    if not _valid_id(session_id):
        state["save_error"] = "Invalid session id"
        return False

    now = time.monotonic()
    if not force and (now - _last_save.get(session_id, 0.0)) < _SAVE_DEBOUNCE_S:
        return True

    try:
        ensure_dirs()
    except OSError as exc:
        logger.exception("Cannot create session storage")
        state["save_error"] = str(exc)
        return False
    messages = state.get("messages", [])
    coda_ok = _scrivi_messaggi(session_id, messages, riscrivi=riscrivi)
    telemetry_error = None
    try:
        _save_telemetry(state, session_id)
    except (OSError, UnicodeError, ValueError) as exc:
        # I messaggi si salvano comunque. Lo snapshot rimane in memoria e il
        # marker non avanza, quindi il salvataggio successivo ritenta.
        logger.exception("Cannot persist telemetry for session %s", session_id)
        telemetry_error = str(exc)
    titolo_utente = _pulisci_titolo(state.get("titolo_utente"))
    payload = {
        "id": session_id,
        # Il titolo scelto dall'utente vince su quello ricavato dalla prima
        # riga; ``titolo_utente`` resta a parte per sapere che e' stato scelto.
        "title": titolo_utente or derive_title(messages),
        "titolo_utente": titolo_utente or None,
        # Archiviata: fuori dagli elenchi, non cancellata. Si ritrova fra le
        # archiviate e con la ricerca.
        "archiviata": bool(state.get("archiviata")),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        # I messaggi stanno nella coda accanto. Qui restano i due numeri che
        # servono alla sidebar: senza, l'indice dovrebbe aprire la cronologia
        # di ogni conversazione per contarla -- che e' esattamente quello che
        # faceva, e costava megabyte riletti ad ogni avvio.
        "n_messages": conta_visibili(messages),
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
        # L'istantanea dell'ultimo turno (``TurnFinished.checkpoint``): perche'
        # si e' fermato, le verifiche rimaste rosse, i file scritti. Un
        # "continua" dopo un turno rimasto a meta' riparte da qui
        # (``core/ciclo/ripresa.py``), anche dopo un riavvio.
        "checkpoint": state.get("checkpoint") or None,
        # Allegati della conversazione: solo i metadati, i file veri stanno
        # nella cartella 'allegati/' del workspace.
        "attachments": list(state.get("attachments", [])),
        "workspace_dir": state.get("workspace_dir", os.getcwd()),
        # Scritto e mai riletto, **di proposito**: dice con che modello e' stata
        # fatta questa conversazione, e serve a chi apre il file per capire
        # perche' un turno di due mesi fa e' andato come e' andato. Rimetterlo
        # in ``state`` al caricamento sarebbe un'altra cosa -- cambierebbe il
        # modello selezionato sotto le mani dell'utente ogni volta che apre una
        # chat vecchia -- e non e' quello che questo campo vuole essere.
        "model_name": state.get("model_name", ""),
    }
    if not coda_ok:
        # La coda non si e' scritta: i messaggi tornano qui dentro, dove
        # stavano prima che esistesse. Meglio un file grosso di una
        # conversazione perduta -- ed e' anche cio' che rende la conversione
        # al formato nuovo senza rischi: finche' la coda non c'e' davvero, i
        # metadati continuano a portarsi dietro tutto.
        payload["messages"] = messages
    if telemetry_error is not None:
        # Fallback compatibile col formato precedente: anche la prima
        # migrazione conserva una copia durevole se il sidecar non si scrive.
        payload["turn_telemetry"] = bounded_turn_telemetry(state.get("turn_telemetry"))
    try:
        _atomic_write_json(DATA_DIR / f"{session_id}.json", payload)
    except (OSError, UnicodeError, ValueError) as exc:
        logger.exception("Cannot persist session %s", session_id)
        state["save_error"] = str(exc)
        return False
    if telemetry_error is not None:
        state["save_error"] = f"Telemetria non salvata: {telemetry_error}"
        return False
    _last_save[session_id] = now
    state.pop("save_error", None)
    return True


# Un lucchetto per tutte e due le cache di questo modulo.
#
# I turni girano in thread di sfondo (``RUNNERS``) e le rotte di FastAPI
# sincrone girano nel threadpool: due ricerche insieme, o una ricerca e un
# ``elenca`` che pota le conversazioni cancellate, sono normali. Le singole
# operazioni su un dict sono atomiche sotto il GIL, ma le **sequenze** no, e
# qui ce ne sono due che rompono davvero:
#
#   * ``get`` seguito da ``move_to_end``: se nel frattempo un altro thread ha
#     sfrattato quella voce, ``move_to_end`` alza ``KeyError`` -- e la ricerca
#     dell'utente muore con un 500 su una cache, cioe' su un'ottimizzazione;
#   * ``elenca`` che pota mentre una ricerca inserisce: la voce appena messa
#     puo' sparire subito, e la volta dopo si rilegge il file. Innocuo, ma
#     senza lucchetto e' indistinguibile dal caso sopra.
#
# Il lavoro pesante -- aprire e parsare i file -- resta **fuori** dal lucchetto:
# dentro ci stanno solo le poche righe di contabilita' della cache.
_cache_lock = threading.Lock()

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
    with _cache_lock:
        cached = _index_cache.get(path.name)
    if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]
    data = _read_metadata(path)
    if data is None:
        return None
    # Il conto e la domanda in sospeso stanno nei metadati: l'indice non apre
    # piu' la cronologia di nessuno. Il ramo con ``messages`` e' il formato
    # vecchio, dove i messaggi erano dentro questo stesso file.
    messages = data.get("messages")
    if messages is None:
        n_messaggi = int(data.get("n_messages") or 0)
        pending = bool(data.get("pending"))
    else:
        n_messaggi = conta_visibili(messages)
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
        "archiviata": bool(data.get("archiviata")),
    }
    with _cache_lock:
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
    archiviate: bool | None = False,
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
        if not _valid_id(path.stem):
            continue
        live.add(path.name)
        summary = _session_summary(path)
        if summary is None:
            continue
        casa = chiave_cartella(summary.get("workspace_dir", ""))
        if voluta and casa != voluta:
            continue
        if fuori and casa in fuori:
            continue
        # ``archiviate``: False = solo quelle in uso (l'elenco normale), True =
        # solo le archiviate, None = tutte (il telefono, la ricerca).
        if archiviate is not None and bool(summary.get("archiviata")) != archiviate:
            continue
        out.append(summary)
    with _cache_lock:
        for stale in set(_index_cache) - live:  # conversazioni cancellate
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
#
# Con un tetto, e non e' pignoleria: ogni voce tiene fino a 2 x MAX_INDEX_CHARS
# (240 kB) di testo estratto, e la potatura in ``elenca`` toglie solo le
# conversazioni **cancellate**. Su un archivio di cinquecento chat una ricerca
# che le tocca tutte lasciava in memoria piu' di cento megabyte per il resto
# della vita del processo. Il tetto e' a uso recente: chi cerca due volte di
# fila sulla stessa chat la ritrova in cache, che e' il caso che conta.
MAX_SEARCH_CACHE = 120
_search_cache: OrderedDict[str, tuple[float, int, dict[str, str]]] = OrderedDict()


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
    with _cache_lock:
        cached = _search_cache.get(path.name)
        if cached and cached[0] == chiave[0] and cached[1] == chiave[1]:
            _search_cache.move_to_end(path.name)   # e' appena servita
            return cached[2]
    data = _read_metadata(path)
    if data is None:
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
                        messaggi.append(loads_object(riga, max_chars=16_777_216))
                    except JsonBoundaryError:
                        continue
        except (OSError, UnicodeError):
            messaggi = []
    pezzi = _pezzi_cercabili(data, messaggi)
    with _cache_lock:
        _search_cache[path.name] = (chiave[0], chiave[1], pezzi)
        _search_cache.move_to_end(path.name)
        while len(_search_cache) > MAX_SEARCH_CACHE:
            _search_cache.popitem(last=False)  # la meno usata di recente
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
        if not _valid_id(path.stem):
            continue
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
    if not _valid_id(session_id):
        return False
    path = DATA_DIR / f"{session_id}.json"
    if not path.exists():
        return False
    data = _read_metadata(path)
    if data is None:
        return False

    state["current_session_id"] = session_id
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
    sidecar = _load_telemetry(session_id)
    inline = data.get("turn_telemetry")
    # Il campo inline esiste solo nelle sessioni legacy o nel fallback di un
    # salvataggio fallito: in quel caso e' piu' recente del vecchio sidecar.
    state["turn_telemetry"] = bounded_turn_telemetry(inline) if isinstance(inline, list) else (
        sidecar if sidecar is not None else [])
    state.pop("_telemetry_saved", None)
    if sidecar is not None and not isinstance(inline, list):
        state["_telemetry_saved"] = _telemetry_marker(session_id, sidecar)
    state["preview"] = data.get("preview") or None
    state["checkpoint"] = data.get("checkpoint") or None
    state["attachments"] = list(data.get("attachments", []))
    state["titolo_utente"] = _pulisci_titolo(data.get("titolo_utente"))
    state["archiviata"] = bool(data.get("archiviata"))
    if data.get("workspace_dir"):
        state["workspace_dir"] = data["workspace_dir"]
    return True


@_session_locked
def delete_session(session_id: str) -> None:
    if not _valid_id(session_id):
        raise ValueError("Invalid session id")
    try:
        (DATA_DIR / f"{session_id}.json").unlink(missing_ok=True)
        messages_path(session_id).unlink(missing_ok=True)
        telemetry_path(session_id).unlink(missing_ok=True)
    except OSError:
        pass
    _scritti.pop(session_id, None)
    # Le due cache sono indicizzate per nome file: una conversazione cancellata
    # e poi ricreata con lo stesso id troverebbe altrimenti il testo di prima.
    with _cache_lock:
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
    state["checkpoint"] = None
    state["attachments"] = []
    state["titolo_utente"] = ""
    state["archiviata"] = False
    state["last_usage"] = {}
    state["turn_telemetry"] = []
    state.pop("_telemetry_saved", None)
    return session_id


def bootstrap_session(state: Any) -> None:
    """Ripristina l'ultima sessione all'avvio, o ne crea una nuova."""
    if state.get("current_session_id"):
        return
    sessions = list_sessions(limit=1)
    if sessions and load_session(state, sessions[0]["id"]):
        return
    new_session(state)
