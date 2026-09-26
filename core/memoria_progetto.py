"""La memoria del progetto scritta dall'harness, a fine turno.

## Perche' non la scrive il modello

Fino al 26/09/2026 la memoria (allora "del vault") la scriveva solo il
modello, con ``manage_notes ambito='vault'``. Nelle 81 sessioni salvate: 2.890
chiamate a tool, 25 a ``manage_notes``, **nessuna** con quell'ambito. Le chat
di un progetto non avevano niente da passarsi, e una chat nuova ripartiva da
zero come fuori da un progetto.

Non e' un difetto del modello, e' la forma della richiesta. Lo stesso progetto
l'aveva gia' misurato sul piano: ``manage_plan complete`` **pretende** una nota
e l'ha avuta 23 volte su 24; ``manage_notes``, che la propone e basta, e' stato
usato in 0 conversazioni su 42. Il rito batte l'invito. Qui il rito lo fa
l'harness: alla fine di un turno che ha lavorato chiede al modello, in una
richiesta sola e senza strumenti, cosa di questo turno deve restare.

## Perche' in coda alla conversazione, e non come chiamata di servizio

Una chiamata di servizio (un prompt corto con un riassunto del turno) su
llama-server con uno slot solo **sostituisce** la cache della chat: il messaggio
successivo dell'utente ricalcola tutta la conversazione, decine di secondi su
un 27B. Qui la richiesta e' la cronologia della chat -- identica, byte per
byte, a quella del passo prima -- piu' un messaggio in fondo: il server riusa
il prefisso e calcola solo la coda. Per la stessa ragione passano anche gli
schemi dei tool (``service_text(tools=...)``): sono parte del prefisso, e
toglierli lo invaliderebbe dal primo token. E con due slot va nello slot della
conversazione (``backend.SCOPI_DELLA_CONVERSAZIONE``), non in quello di
servizio, che quella cronologia non ce l'ha.

C'e' anche un guadagno di qualita': il modello che scrive la memoria e' quello
che ha appena fatto il lavoro, con tutto il turno davanti -- non un riassunto
del turno.

## Quando

Solo dopo i turni che hanno **lavorato**: scritto o modificato file, lanciato
comandi, chiuso punti del piano. Una domanda e una risposta costerebbero un
passo in piu' per quasi mai niente da tenere (scelta dell'utente del
26/09/2026). E solo se il turno si e' chiuso lui: non dopo uno stop, un errore o
una domanda all'utente -- li' il turno non e' finito.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from .progetto import (
    AUTORE_PROTETTO,
    ETICHETTE_TIPO,
    MAX_OPERAZIONI_PER_GIRO,
    MAX_VOCE_CHARS,
    MAX_VOCI_MEMORIA,
    ProgettoConfig,
    _attributo,
)

# Tetto alla risposta: sei operazioni da una riga stanno larghe in 700 token.
# Stessa taglia del riepilogo forzato.
MAX_TOKEN_MEMORIA = 700

# I motivi di chiusura dopo cui si scrive. ``stallo`` e ``max_steps`` compresi:
# un turno fermato a meta' e' proprio quello di cui la prossima chat deve
# sapere dove si era arrivati ("lavori aperti").
MOTIVI_DI_CHIUSURA = frozenset({"completed", "max_steps", "stallo"})

# Che cosa conta come "ha lavorato".
TOOL_DI_SCRITTURA = frozenset({"write_file", "edit_file"})
AZIONI_CHE_CHIUDONO = frozenset({"complete", "skip"})

TAG_RICHIESTA = "aggiorna_memoria_del_progetto"


def _risultato_ok(msg: dict[str, Any]) -> bool:
    """Il tool e' andato bene? La busta dei tool ha l'errore come **chiave**."""
    if msg.get("ok") is False:
        return False
    try:
        busta = json.loads(str(msg.get("content") or ""))
    except ValueError:
        return True
    return not (isinstance(busta, dict) and "error" in busta)


def turno_ha_lavorato(ui_messages: Sequence[dict[str, Any]]) -> bool:
    """Il turno in corso ha scritto file, lanciato comandi o chiuso punti?

    Si guarda solo dopo l'ultimo messaggio **visibile** dell'utente: i solleciti
    dell'harness hanno ruolo ``user`` ma non aprono un turno.
    """
    inizio = 0
    for i in range(len(ui_messages) - 1, -1, -1):
        msg = ui_messages[i]
        if msg.get("role") == "user" and not msg.get("hidden"):
            inizio = i + 1
            break
    argomenti: dict[str, dict[str, Any]] = {}
    for msg in ui_messages[inizio:]:
        if msg.get("role") == "assistant":
            for call in msg.get("tool_calls") or []:
                funzione = call.get("function") or {}
                grezzi = funzione.get("arguments")
                if isinstance(grezzi, str):
                    try:
                        grezzi = json.loads(grezzi)
                    except ValueError:
                        grezzi = {}
                argomenti[str(call.get("id") or "")] = grezzi if isinstance(grezzi, dict) else {}
            continue
        if msg.get("role") != "tool":
            continue
        nome = str(msg.get("name") or "")
        if nome in TOOL_DI_SCRITTURA and _risultato_ok(msg):
            return True
        if nome == "run_command":
            # Anche un comando fallito ha lavorato: un rosso vero e' spesso
            # proprio la "strada scartata" che la prossima chat deve sapere.
            # Non conta un comando rifiutato prima di partire (argomenti
            # invalidi, permesso negato): quello non ha toccato niente.
            if '"returncode"' in str(msg.get("content") or "") or _risultato_ok(msg):
                return True
        if nome == "manage_plan" and _risultato_ok(msg):
            args = msg.get("args") if isinstance(msg.get("args"), dict) else None
            if args is None:
                args = argomenti.get(str(msg.get("tool_call_id") or ""), {})
            if str(args.get("action") or "").lower() in AZIONI_CHE_CHIUDONO:
                return True
    return False


def _righe_memoria(config: ProgettoConfig) -> list[str]:
    if not config.memoria:
        return ["(vuota)"]
    righe = []
    for voce in config.memoria:
        segno = " (utente: non si tocca)" if voce.autore == AUTORE_PROTETTO else ""
        righe.append(f"[{voce.id}] {voce.tipo}{segno}: {voce.testo}")
    return righe


def richiesta(config: ProgettoConfig, *, piano_aperto: Sequence[str] = ()) -> str:
    """Il messaggio in coda alla conversazione che chiede l'aggiornamento.

    Porta la memoria **con gli id**, che il blocco di coda normale non mostra:
    servono solo qui, per modificare e togliere, e a ogni passo sarebbero
    rumore.
    """
    tipi = "\n".join(
        f"- {tipo}: {spiega}"
        for tipo, spiega in (
            ("decisione", "una scelta presa (con l'utente, o verificata) e il suo perche'"),
            ("convenzione", "come si lavora qui: comandi, percorsi, stile, strumenti"),
            ("fatto", "com'e' fatto il progetto, o a che punto e' arrivato il lavoro"),
            ("scartato", "una strada provata che non funziona, e perche'"),
            ("aperto", "un lavoro rimasto da fare alla fine di questo turno"),
        )
    )
    righe = [
        f'<{TAG_RICHIESTA} progetto="{_attributo(config.nome)}">',
        "Il turno e' finito. Prima di chiudere aggiorna la memoria del progetto: "
        "e' l'unica cosa di questa conversazione che le prossime chat del "
        "progetto vedranno.",
        "",
        f"Memoria attuale ({len(config.memoria)} voci su {MAX_VOCI_MEMORIA}):",
        *_righe_memoria(config),
    ]
    if piano_aperto:
        righe += ["", "Punti del piano rimasti aperti:", *[f"- {p}" for p in piano_aperto]]
    righe += [
        "",
        "Tieni solo cio' che varra' ancora in un'altra chat, fra una settimana. Tipi:",
        tipi,
        "",
        "Non scrivere i passi fatti uno per uno (il codice e' sul disco), cose gia' "
        "in memoria, ipotesi non verificate, dettagli che valgono solo in questa "
        "chat. Aggiorna invece di accumulare: una voce superata si modifica, un "
        "lavoro aperto che e' stato fatto si toglie. Le voci dell'utente non si "
        "toccano.",
        f"Ogni testo: una frase sola, autosufficiente, in italiano, al massimo "
        f"{MAX_VOCE_CHARS} caratteri. Al massimo {MAX_OPERAZIONI_PER_GIRO} operazioni.",
        "",
        "Rispondi SOLO con un oggetto JSON, senza altro testo e senza chiamare strumenti:",
        '{"operazioni": [',
        '  {"azione": "aggiungi", "tipo": "decisione", "testo": "..."},',
        '  {"azione": "modifica", "id": "a1b2c3", "testo": "..."},',
        '  {"azione": "togli", "id": "d4e5f6"}',
        "]}",
        'Se non c\'e\' niente da cambiare: {"operazioni": []}',
        f"</{TAG_RICHIESTA}>",
    ]
    return "\n".join(righe)


_RECINTO = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def interpreta(testo: str) -> list[dict[str, Any]] | None:
    """Le operazioni dalla risposta del modello, o None se non si leggono.

    Tollerante sulla busta -- un recinto ```json, una frase prima, l'elenco
    nudo senza la chiave -- perche' sbagliare la busta non vuol dire sbagliare
    il contenuto. Rigido sul contenuto: quello lo valida ``applica_operazioni``.
    """
    testo = str(testo or "").strip()
    if not testo:
        return None
    candidati = [m.group(1) for m in _RECINTO.finditer(testo)] + [testo]
    decoder = json.JSONDecoder()
    for candidato in candidati:
        for i, carattere in enumerate(candidato):
            if carattere not in "{[":
                continue
            try:
                valore, _fine = decoder.raw_decode(candidato[i:])
            except ValueError:
                continue
            if isinstance(valore, dict) and isinstance(valore.get("operazioni"), list):
                return [op for op in valore["operazioni"] if isinstance(op, dict)]
            if isinstance(valore, list) and all(isinstance(op, dict) for op in valore):
                return list(valore)
    return None


def riassunto_esito(esito: dict[str, Any]) -> str:
    """Una riga per la goccia in chat: "+2 · 1 modificata · 1 tolta"."""
    pezzi = []
    if esito.get("aggiunte"):
        pezzi.append(f"+{len(esito['aggiunte'])}")
    if esito.get("modificate"):
        n = len(esito["modificate"])
        pezzi.append(f"{n} modificat{'a' if n == 1 else 'e'}")
    if esito.get("tolte"):
        n = len(esito["tolte"])
        pezzi.append(f"{n} tolt{'a' if n == 1 else 'e'}")
    return " · ".join(pezzi) or "nessun cambiamento"


__all__ = [
    "ETICHETTE_TIPO",
    "MAX_TOKEN_MEMORIA",
    "MOTIVI_DI_CHIUSURA",
    "TAG_RICHIESTA",
    "interpreta",
    "riassunto_esito",
    "richiesta",
    "turno_ha_lavorato",
]
