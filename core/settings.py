"""Persistenza delle impostazioni fra un avvio e l'altro.

Prima di questo modulo ``AppState`` ripartiva da ``DEFAULTS`` ad ogni riavvio:
modello, num_ctx, livello di pensiero, workspace, sandbox -- tutto da rifare a
mano, ogni volta. Le memorie e le conversazioni erano gia' su disco; le
impostazioni no, ed era l'unico pezzo di stato che l'utente doveva ridigitare.

Due scelte che meritano una riga di spiegazione, perche' non sono ovvie:

**Si fa merge sui default, non si sostituiscono.** Il file salvato vince solo
sulle chiavi che contiene davvero. Cosi' una chiave aggiunta in una versione
nuova dell'harness arriva con il suo default nuovo, invece di restare assente o
peggio di essere riempita con un valore vecchio; e una chiave rimossa dal codice
sparisce invece di sopravvivere per sempre nel file.

**Il system prompt si salva solo se l'utente l'ha riscritto.** Salvare anche
quello stock significherebbe congelare per sempre la versione del prompt in
vigore il giorno del primo salvataggio: ogni miglioramento successivo al
prompt non arriverebbe mai all'utente, che si ritroverebbe con un default
silenziosamente vecchio senza capire perche'. Se invece l'ha modificato lui,
quel testo e' suo e va conservato.
"""

from __future__ import annotations

import json
import os
import math
import tempfile
from pathlib import Path
from typing import Any

from .config import DEFAULTS, SETTINGS_FILE
from .prompts import is_stock_prompt
from .jsonsafe import JsonBoundaryError, loads_object

# Stato del turno in corso, non preferenze: salvarlo significherebbe riaprire
# l'app convinta che un agente stia girando.
VOLATILE_KEYS = frozenset({"agent_running", "pending_prompt", "last_usage"})


def _defaults() -> dict[str, Any]:
    return {
        key: (value.copy() if isinstance(value, (dict, list)) else value)
        for key, value in DEFAULTS.items()
    }


def persistable(settings: dict[str, Any]) -> dict[str, Any]:
    """Il sottoinsieme che ha senso ritrovare al prossimo avvio."""
    out = {
        key: value
        for key, value in settings.items()
        if key in DEFAULTS and key not in VOLATILE_KEYS
    }
    prompt = settings.get("system_prompt")
    if isinstance(prompt, str) and prompt.strip() and not is_stock_prompt(prompt):
        out["system_prompt"] = prompt
    return out


def load_settings(path: Path | None = None) -> dict[str, Any]:
    """Default, con sopra quello che l'utente aveva scelto l'ultima volta.

    Il percorso si risolve **alla chiamata**, non nella firma: un default
    valutato all'import congela il file di allora, e non c'e' piu' modo di
    spostarlo altrove. La suite di test lo fa: senza, girerebbe sulle
    preferenze vere della macchina -- le legge (quindi l'esito dipende da come
    l'utente ha lasciato l'interfaccia) e le riscrive (quindi cambiargliele e'
    un effetto collaterale di ``pytest``). Osservato davvero: con la sandbox su
    'host' cinque test di prontezza fallivano senza che nulla fosse rotto.
    """
    path = path or SETTINGS_FILE
    settings = _defaults()
    if not path.exists():
        return settings
    try:
        with open(path, encoding="utf-8") as fh:
            saved = loads_object(fh.read(1_048_577))
    except (OSError, UnicodeError, JsonBoundaryError):
        # Un file corrotto non deve impedire l'avvio: si riparte dai default.
        return settings
    if not isinstance(saved, dict):
        return settings

    for key, value in saved.items():
        if key == "system_prompt":
            if isinstance(value, str) and value.strip():
                settings["system_prompt"] = value
            continue
        if key in DEFAULTS and key not in VOLATILE_KEYS:
            # Il tipo del default e' la specifica: un valore salvato di tipo
            # incompatibile (file modificato a mano) viene ignorato invece di
            # far esplodere int(...) o bool(...) da qualche parte in seguito.
            if tipo_compatibile(value, DEFAULTS[key]):
                settings[key] = value
    return settings


def tipo_compatibile(valore: Any, atteso: Any) -> bool:
    """Il valore ha un tipo che queste impostazioni accettano?

    Sta qui perche' qui c'e' la specifica -- ``DEFAULTS`` -- e la regola veniva
    applicata in **due** posti con due testi diversi: la rilettura all'avvio
    qui sopra e il controllo all'ingresso di ``/api/settings``, che ne aveva
    una copia piu' stretta. Il commento della copia diceva "stessa regola di
    ``load_settings``" e non lo era piu': un ``true`` mandato su un campo
    intero veniva rifiutato dalla rotta e accettato dalla rilettura, cioe' le
    due porte della stessa casa avevano due serrature diverse.

    ``bool`` prima di ``int`` perche' ``isinstance(True, int)`` e' vero, ed e'
    il caso che ci si scorda; ``int`` passa per ``float`` perche' JSON non
    distingue ``1`` da ``1.0``.
    """
    if atteso is None:
        return True
    if isinstance(atteso, bool):
        return isinstance(valore, bool)
    if isinstance(atteso, int):
        return isinstance(valore, int) and not isinstance(valore, bool)
    if isinstance(atteso, float):
        return isinstance(valore, (int, float)) and not isinstance(valore, bool) and math.isfinite(valore)
    return isinstance(valore, type(atteso))


def save_settings(settings: dict[str, Any], path: Path | None = None) -> bool:
    """Scrittura atomica: un crash a meta' non lascia un file monco.

    Percorso risolto alla chiamata, per la ragione detta in ``load_settings``.
    """
    path = path or SETTINGS_FILE
    payload = persistable(settings)
    tmp: Path | None = None
    try:
        fd, filename = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        tmp = Path(filename)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, allow_nan=False)
            fh.flush()
            os.fsync(fh.fileno())
        # I permessi si stringono sul **temporaneo**, prima del replace: cosi'
        # il file definitivo nasce gia' chiuso e non c'e' un istante in cui
        # esiste leggibile da tutti. Qui dentro ci sono ``api_key`` e
        # ``mobile_token`` accanto a ``theme_mode``, e il file nasceva con i
        # permessi di default -- su Linux 0644, su Windows l'ACL della cartella.
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            # Su alcuni filesystem (FAT, certe condivisioni di rete) chmod non
            # fa niente e non e' un motivo per non salvare le preferenze.
            pass
        os.replace(tmp, path)
    except (OSError, UnicodeError, ValueError):
        return False
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    return True


def pick_available_model(settings: dict[str, Any], models: list[str]) -> str | None:
    """Modello da usare quando quello configurato non c'e' piu'.

    Il default di fabbrica invecchia: resta scritto un modello che l'utente non
    ha piu' installato, e alla prima richiesta il backend risponde 404 senza
    che sia chiaro il perche'. Se il modello configurato non e' fra quelli che
    il server dichiara, si ripiega sul primo disponibile.

    Restituisce None quando non c'e' niente da cambiare -- lista vuota (server
    spento: non e' il momento di riscrivere le preferenze) o modello gia' buono.
    """
    if not models:
        return None
    current = str(settings.get("model_name") or "").strip()
    if current in models:
        return None
    # Match indulgente sul tag: "qwen3.5" deve riconoscersi in "qwen3.5:latest".
    # Una sola scansione: ``any`` seguito da ``next`` percorreva l'elenco due
    # volte per rispondere alla stessa domanda.
    stem = current.split(":")[0]
    if stem:
        simile = next((m for m in models if m.split(":")[0] == stem), None)
        if simile:
            return simile
    return models[0]


# ---------------------------------------------------------------------------
# Valori di serie e scambio fra macchine
# ---------------------------------------------------------------------------
#
# Il menu delle impostazioni mostra cosa hai cambiato rispetto ai valori di
# fabbrica e sa rimetterli: per farlo li deve conoscere, e la fonte e' una sola,
# ``DEFAULTS``. Esportazione e importazione servono a portare le preferenze da
# una macchina all'altra: quello che non ha senso portare -- segreti e percorsi
# di questa macchina -- non esce e non entra, e il motivo viaggia con la chiave
# perche' la UI lo possa dire invece di far sparire una voce in silenzio.

#: Chiavi che non hanno un valore "di serie" da proporre: dipendono dalla
#: macchina (``workspace_dir`` vale ``os.getcwd()`` al momento dell'import) o
#: sono stato che l'utente non sceglie da un menu.
SENZA_VALORE_DI_SERIE = VOLATILE_KEYS | frozenset(
    {"workspace_dir", "recent_workspaces", "vaults", "mobile_token"}
)

#: Chiavi che un'esportazione non scrive e un'importazione non applica.
FUORI_DALLO_SCAMBIO: dict[str, str] = {
    "api_key": "la chiave del server è un segreto e resta su questa macchina",
    "mobile_token": "la chiave del telefono vale solo per questa macchina",
    "workspace_dir": "è un percorso di questa macchina",
    "recent_workspaces": "sono percorsi di questa macchina",
    "vaults": "sono percorsi di questa macchina",
    "docker_image": "il nome dell'immagine dipende dal percorso del progetto",
}

FORMATO_SCAMBIO = "astra-impostazioni"
VERSIONE_SCAMBIO = 1


def valori_di_serie() -> dict[str, Any]:
    """I valori di fabbrica delle chiavi che si possono cambiare dal menu."""
    return {
        key: value for key, value in _defaults().items()
        if key not in SENZA_VALORE_DI_SERIE
    }


def esporta(settings: dict[str, Any], *, versione_app: str, adesso: str) -> dict[str, Any]:
    """Le preferenze in un involucro che si riconosce al ritorno.

    Parte da ``persistable``: e' cio' che finisce nel file delle preferenze,
    quindi il system prompt c'e' solo se e' stato riscritto -- esportare quello
    di serie vorrebbe dire congelarlo sull'altra macchina, la trappola
    descritta in testa al modulo.
    """
    valori = {
        key: value for key, value in persistable(settings).items()
        if key not in FUORI_DALLO_SCAMBIO
    }
    return {
        "formato": FORMATO_SCAMBIO,
        "versione": VERSIONE_SCAMBIO,
        "app": versione_app,
        "esportate_il": adesso,
        "impostazioni": valori,
    }


def da_importare(dati: Any) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Cosa di un file importato si puo' applicare, e cosa no con il motivo.

    Accetta l'involucro di ``esporta`` e anche un ``agent_settings.json``
    copiato cosi' com'e': e' lo stesso dizionario senza la busta. La regola sui
    tipi e' ``tipo_compatibile``, la stessa della rilettura all'avvio e di
    ``/api/settings``: tre porte, una serratura.
    """
    if isinstance(dati, dict) and dati.get("formato") == FORMATO_SCAMBIO:
        valori = dati.get("impostazioni")
    else:
        valori = dati
    if not isinstance(valori, dict):
        raise ValueError("Il file non contiene impostazioni: serve un oggetto JSON.")

    applicabili: dict[str, Any] = {}
    ignorate: list[dict[str, str]] = []
    for key, value in valori.items():
        if key in FUORI_DALLO_SCAMBIO:
            ignorate.append({"chiave": key, "motivo": FUORI_DALLO_SCAMBIO[key]})
        elif key == "system_prompt":
            if isinstance(value, str):
                applicabili[key] = value
            else:
                ignorate.append({"chiave": key, "motivo": "non è un testo"})
        elif key not in DEFAULTS or key in VOLATILE_KEYS:
            ignorate.append({"chiave": key, "motivo": "questa versione non la conosce"})
        elif not tipo_compatibile(value, DEFAULTS[key]):
            ignorate.append({
                "chiave": key,
                "motivo": f"atteso {type(DEFAULTS[key]).__name__}, trovato {type(value).__name__}",
            })
        else:
            applicabili[key] = value
    return applicabili, ignorate
