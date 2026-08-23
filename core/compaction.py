"""Compattazione della cronologia quando il contesto si riempie.

## Perche' esiste, e perche' e' l'ultima difesa e non la prima

Su una sessione reale di questo progetto -- 82 messaggi, un compito di
refactoring -- il contesto inviato al modello era di 42.126 token. La
composizione, misurata e non stimata:

    argomenti delle tool call   36.335   86,3%
    risultati dei tool           4.814   11,4%
    messaggi dell'utente            877    2,1%
    testo dell'assistente           100    0,2%

Di quei 36.335, **33.312 erano il contenuto dei file dentro gli argomenti di
``write_file``**: testo gia' scritto su disco, tenuto in cronologia per sempre
in una seconda copia che nessuno rileggeva. La potatura in ``agent.py``
(``_prune_tool_call``) toglie quella duplicazione e riporta la stessa sessione
a 12.096 token, senza perdere una virgola di informazione e senza chiamare il
modello.

Questo modulo interviene *dopo*, quando anche il contesto onesto e' troppo. Ed
e' un'operazione costosa in due modi diversi, che vale la pena tenere a mente:

1. **Costa una generazione.** Riassumere vuol dire un'altra chiamata al
   modello, con la sua latenza.
2. **Costa il KV cache.** Riscrivere la cronologia a meta' fa divergere il
   prefisso: Ollama deve rivalutare il prompt da li' in avanti. Su una
   finestra da 64k e' un prezzo reale.

Per questo la soglia e' alta e la compattazione tiene una coda generosa: farla
tardi e bene batte farla presto e spesso.

## Cosa sopravvive, e chi lo decide

Sopravvivono, senza passare dal riassunto:

* le **richieste testuali dell'utente**, copiate parola per parola. Sono la
  specifica: un riassunto che le parafrasa e' un riassunto che puo' cambiare
  il compito. Le copia l'harness, non il modello, quindi non c'e' modo che si
  perdano;
* le **note di lavoro** (``core/notes.py``) e il **piano**, che vivono nel
  blocco di coda e non nella cronologia;
* la **coda recente** dei blocchi, intatta.

Del resto -- il ragionamento, i comandi lanciati, gli errori incontrati --
resta quello che il modello riesce a mettere in un riassunto. Che e' anche il
motivo per cui le note esistono: sono la parte che l'agente ha deciso di
salvare *mentre* capiva, non quella che ricostruisce alla fine.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from .textutils import chars_for_tokens, smart_truncate, strip_think

# Quanta parte della finestra puo' occupare la cronologia prima di intervenire.
# Non e' "quanto contesto e' pieno": e' quanto ne resta libero per il passo
# successivo, che deve contenere il prossimo risultato di tool *e* la
# generazione. A 0,75 su 64k restano 16k, che bastano.
SOGLIA_DEFAULT = 0.75

# Quanto contesto deve restare occupato *dopo* la compattazione. La distanza
# fra questo e la soglia e' cio' che impedisce di ricompattare al passo dopo:
# con 0,75 e 0,35 ogni compattazione libera circa il 40% della finestra, cioe'
# abbastanza lavoro da non rifarla per un pezzo.
CODA_DEFAULT = 0.35

# Sotto questo numero di blocchi non si compatta: una generazione e un
# ricalcolo di KV cache per accorpare tre messaggi sono un cattivo affare.
MIN_BLOCCHI = 4

# Tetto al riassunto. Deve stare comodamente sotto quello che sostituisce,
# altrimenti l'operazione non ha senso.
MAX_TOKEN_RIASSUNTO = 700

# Quanta finestra puo' occupare la trascrizione da riassumere. Il caso normale
# ci sta comodo (2.837 token per 72 messaggi di lavoro vero), ma il caso da
# temere e' proprio quello estremo: e' quando la cronologia e' enorme che si
# compatta, e una trascrizione piu' lunga di num_ctx farebbe fallire la
# chiamata esattamente nel momento in cui serve. Si taglia al centro, dove le
# cose stanno gia' nel riassunto precedente o sono ancora nella coda.
QUOTA_TRASCRIZIONE = 0.5


@dataclass(slots=True)
class Compattazione:
    """Esito di una compattazione, per l'evento e per i test."""

    messaggi_prima: int
    messaggi_dopo: int
    token_prima: int
    token_dopo: int
    riassunto: str
    richieste: list[str]

    @property
    def risparmio(self) -> int:
        return max(0, self.token_prima - self.token_dopo)


def blocchi(ui_messages: Sequence[dict[str, Any]]) -> list[tuple[int, int]]:
    """Confini dei blocchi indivisibili, come coppie ``(inizio, fine esclusa)``.

    Un blocco e' un messaggio dell'assistente con tutti i risultati dei tool
    che ha chiamato, oppure un singolo messaggio di altro ruolo. Tagliare
    *dentro* un blocco e' l'unico errore veramente grave possibile qui: una
    ``tool_call`` senza il suo risultato produce una cronologia che diversi
    chat template rifiutano, e l'errore arriva dal server come un 400 opaco
    molte richieste dopo.
    """
    confini: list[tuple[int, int]] = []
    inizio: int | None = None
    for i, msg in enumerate(ui_messages):
        if msg.get("role") == "tool":
            if inizio is None:  # risultato orfano: blocco a se'
                inizio = i
            continue
        if inizio is not None:
            confini.append((inizio, i))
        inizio = i
    if inizio is not None:
        confini.append((inizio, len(ui_messages)))
    return confini


def richieste_utente(messaggi: Iterable[dict[str, Any]]) -> list[str]:
    """Le richieste vere dell'utente, escludendo i solleciti dell'harness."""
    fuori = []
    for msg in messaggi:
        if msg.get("role") == "user" and not msg.get("hidden"):
            testo = str(msg.get("content") or "").strip()
            if testo:
                fuori.append(testo)
    return fuori


def trascrizione(messaggi: Sequence[dict[str, Any]]) -> str:
    """Il tratto da riassumere, in testo piano e gia' asciugato.

    Non si passa al riassuntore la cronologia in formato API: contiene i
    contenuti integrali dei file e costerebbe quanto il problema che stiamo
    risolvendo. Qui si tiene la forma dell'azione (quale tool, su cosa, con
    che esito) e si buttano i corpi.
    """
    righe: list[str] = []
    for msg in messaggi:
        ruolo = msg.get("role")
        if ruolo == "user":
            etichetta = "SOLLECITO" if msg.get("hidden") else "UTENTE"
            righe.append(f"[{etichetta}] {str(msg.get('content') or '').strip()}")
        elif ruolo == "assistant":
            testo = strip_think(str(msg.get("content") or "")).strip()
            if testo:
                righe.append(f"[AGENTE] {testo}")
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") or {}
                righe.append(f"[CHIAMATA] {fn.get('name')} {_argomenti_brevi(fn)}")
        elif ruolo == "tool":
            righe.append(
                f"[ESITO {msg.get('name')}] "
                f"{'ok' if msg.get('ok', True) else 'FALLITO'} "
                f"{_esito_breve(str(msg.get('content') or ''))}"
            )
        elif ruolo == "summary":
            # Un riassunto precedente entra nel nuovo: altrimenti si
            # accumulerebbero, e la cronologia diventerebbe una pila di
            # riassunti di riassunti.
            righe.append(f"[RIASSUNTO PRECEDENTE] {str(msg.get('content') or '').strip()}")
    return "\n".join(righe)


def _argomenti_brevi(fn: dict[str, Any]) -> str:
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except json.JSONDecodeError:
        return str(fn.get("arguments") or "")[:120]
    if not isinstance(args, dict):
        return str(args)[:120]
    pezzi = []
    for chiave, valore in args.items():
        testo = valore if isinstance(valore, str) else json.dumps(valore, ensure_ascii=False)
        if len(testo) > 120:
            testo = f"<{len(testo)} caratteri>"
        pezzi.append(f"{chiave}={testo}")
    return " ".join(pezzi)[:300]


def _esito_breve(raw: str) -> str:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:200]
    if not isinstance(payload, dict):
        return raw[:200]
    if "error" in payload:
        return f"errore: {str(payload['error'])[:200]}"
    tenuti = {
        k: v
        for k, v in payload.items()
        if k in ("status", "action", "filepath", "returncode", "match_count")
    }
    # stderr conta piu' di stdout in un riassunto: e' dove sta il motivo.
    for chiave in ("stderr", "stdout"):
        corpo = str(payload.get(chiave) or "").strip()
        if corpo:
            tenuti[chiave] = corpo[-200:]
            break
    return json.dumps(tenuti, ensure_ascii=False)[:300]


PROMPT_RIASSUNTO = (
    "Sei la memoria di lavoro di un agente di programmazione. Ricevi la "
    "trascrizione asciutta di un tratto di lavoro che sta per essere "
    "cancellato dal contesto per far spazio.\n\n"
    "Scrivi cosa deve sapere l'agente per continuare come se lo avesse "
    "ancora davanti. In italiano, senza preamboli, in punti elenco brevi, "
    "sotto queste quattro voci (salta quelle vuote):\n"
    "FATTO: cosa e' stato completato, con i file toccati.\n"
    "SCOPERTO: fatti tecnici verificati -- errori veri, versioni, percorsi, "
    "comandi che funzionano.\n"
    "SCARTATO: strade provate che non hanno funzionato, e perche'. Servono a "
    "non rifarle.\n"
    "APERTO: cosa resta da fare o da verificare.\n\n"
    "Regole: solo cose presenti nella trascrizione, mai dedotte. Se un "
    "comando e' fallito dillo con l'errore esatto. Niente frasi di cortesia, "
    "niente 'l'agente ha proceduto a': scrivi il fatto."
)


def costruisci_riassunto(
    trascritto: str,
    *,
    backend: Any,
    params: Any,
) -> str:
    """Una chiamata sola, senza tool e senza pensiero, per riassumere il tratto."""
    if not trascritto.strip():
        return ""
    finestra = int(getattr(params, "num_ctx", 0) or 0)
    if finestra > 0:
        trascritto = smart_truncate(
            trascritto,
            chars_for_tokens(finestra * QUOTA_TRASCRIZIONE),
            label="cronologia da riassumere",
        )
    # think spento e max_tokens stretto: qui si vuole un referto, non un
    # ragionamento. Un modello che pensa ottomila token per riassumere
    # vanificherebbe la compattazione nel momento stesso in cui la fa.
    p = replace(
        params,
        max_tokens=min(int(getattr(params, "max_tokens", 2048) or 2048), MAX_TOKEN_RIASSUNTO),
        think=False,
        temperature=0.1,
    )
    messaggi = [
        {"role": "system", "content": PROMPT_RIASSUNTO},
        {"role": "user", "content": trascritto},
    ]
    pezzi: list[str] = []
    for evento in backend.stream(messaggi, None, p):
        if evento.kind == "content":
            pezzi.append(evento.text)
        elif evento.kind == "error":
            # Un riassunto mancato non deve far fallire il turno: si torna
            # vuoto e il chiamante rinuncia alla compattazione, che e' sempre
            # meglio che buttare la cronologia senza averla riassunta.
            return ""
    return strip_think("".join(pezzi)).strip()


def render_messaggio(riassunto: str, richieste: Sequence[str]) -> str:
    """Il messaggio che sostituisce il tratto compattato."""
    pezzi = [
        "<cronologia_compattata>",
        "Questa parte della conversazione e' stata riassunta per far spazio "
        "nel contesto. I dettagli non ci sono piu': quello che segue e' tutto "
        "cio' che ne resta.",
    ]
    if richieste:
        pezzi += [
            "",
            "Richieste dell'utente in questo tratto, testuali:",
            *[f"- «{r}»" for r in richieste],
        ]
    if riassunto:
        pezzi += ["", riassunto]
    pezzi.append("</cronologia_compattata>")
    return "\n".join(pezzi)


def taglio(
    ui_messages: Sequence[dict[str, Any]],
    *,
    costo: Callable[[Sequence[dict[str, Any]]], int],
    budget_coda: int,
) -> int:
    """Fin dove compattare: indice del primo messaggio da tenere.

    Si parte dalla fine e si tiene tutto quello che sta nel budget della coda,
    a blocchi interi. Ritorna 0 se non c'e' niente da guadagnare.
    """
    gruppi = blocchi(ui_messages)
    if len(gruppi) < MIN_BLOCCHI:
        return 0

    tenuti = 0
    inizio_coda = len(ui_messages)
    for inizio, fine in reversed(gruppi):
        costo_blocco = costo(ui_messages[inizio:fine])
        if tenuti + costo_blocco > budget_coda and inizio_coda < len(ui_messages):
            break
        tenuti += costo_blocco
        inizio_coda = inizio

    # Serve che restino abbastanza blocchi da compattare da giustificare la
    # spesa: comprimere l'ultimo blocco prima della coda non ripaga il
    # ricalcolo del KV cache.
    davanti = [g for g in gruppi if g[0] < inizio_coda]
    if len(davanti) < MIN_BLOCCHI - 1:
        return 0
    return inizio_coda
