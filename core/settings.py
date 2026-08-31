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
from pathlib import Path
from typing import Any

from .config import DEFAULTS, SETTINGS_FILE
from .prompts import is_stock_prompt

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
            saved = json.load(fh)
    except (OSError, json.JSONDecodeError):
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
        return isinstance(valore, (int, float)) and not isinstance(valore, bool)
    return isinstance(valore, type(atteso))


def save_settings(settings: dict[str, Any], path: Path | None = None) -> bool:
    """Scrittura atomica: un crash a meta' non lascia un file monco.

    Percorso risolto alla chiamata, per la ragione detta in ``load_settings``.
    """
    path = path or SETTINGS_FILE
    payload = persistable(settings)
    tmp = path.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
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
    except OSError:
        return False
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
