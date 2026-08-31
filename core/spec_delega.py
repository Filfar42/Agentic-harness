"""Spec riutilizzabili dell'esploratore: fatti append-only, derivati rigenerabili.

Perche' esiste
--------------
Misurato il 23/08/2026: ``esplora`` **viene usato** (17 esplorazioni in 4
sessioni) ma falliva -- 11 referti utili contro 6 fallimenti, e **8 su 17**
toccavano il tetto dei sei passi. Un tool che fallisce una volta su tre insegna
a non usarlo. ``referto_di_chiusura`` ha tolto il fallimento peggiore (passi
bruciati senza niente in mano); qui si attacca la causa a monte, cioe' che il
figlio non sa quanto tempo gli resta -- lo stesso difetto che il blocco del
piano aveva gia' risolto per il padre ("il modello pianificava come se avesse
tempo infinito perche' non sapeva di averne poco").

La regola che decide cosa sta qui
---------------------------------
Dal Continual Harness si prende il pezzo che si puo' verificare, e si lascia
quello che non si puo':

    Il diritto di essere raffinato spetta solo a cio' che e' ricostruibile
    dai fatti.

* **Fatti** (``registra``): cos'e' stato chiesto a un'esplorazione e com'e'
  andata. Append-only, mai riscritti, mai interpretati -- gli stessi campi che
  ``delega.esegui`` gia' calcola per il referto.
* **Derivati** (``blocco_figlio``, ``esempio_riuscito``): frasi ricavate dai
  fatti al momento in cui servono. Non si conservano, si ricalcolano; se
  l'evidenza cambia, la frase cambia da sola, e se l'evidenza sparisce la frase
  sparisce. E' questo che rende *evidence-backed* una parola verificabile
  invece che una promessa.

Quello che **non** si fa, ed e' la parte del paper che qui non regge: nessun
prompt supplementare scritto dall'agente su se stesso. Il system prompt di base
resta intoccato. Su un modello che delibera cinquemila caratteri prima di una
``edit_file``, lasciargli scrivere le proprie regole e' una fabbrica di frasi
false -- e questo progetto ha gia' pagato due volte per scoprire che una frase
falsa dell'harness diventa una convinzione su cui il modello agisce.

Dove stanno i fatti
-------------------
``<workspace>/.memoria/.deleghe.json``: dentro la cartella che esiste gia', e
con un nome che comincia per punto, cosi' ``libreria.voci()`` -- che raccoglie
i ``*.md`` -- non lo vede nemmeno. Una cartella nascosta sola invece di due.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

NOME = ".deleghe.json"

# Quante esplorazioni si conservano. Non e' una potatura di spazio (sono
# poche centinaia di byte l'una): e' la finestra su cui i derivati guardano.
# Un tasso di fallimento calcolato su tutta la storia del workspace
# continuerebbe a raccontare com'era sei mesi fa.
MAX_FATTI = 60

# Da quante esplorazioni in poi si osa dire qualcosa. Sotto, il tasso e' rumore.
MIN_PER_UN_TASSO = 4

# Sopra questa frazione di esplorazioni finite senza risposta, il figlio se lo
# sente dire. Meta' e' alto di proposito: il messaggio deve comparire quando il
# problema c'e' davvero, o diventa l'ennesima riga che si impara a saltare.
SOGLIA_ESAURIMENTO = 0.5


def _percorso(base: Path) -> Path:
    return Path(base) / ".memoria" / NOME


def leggi(base: Path) -> list[dict[str, Any]]:
    """I fatti su disco. Un file illeggibile vale come nessun fatto."""
    try:
        dati = json.loads(_percorso(base).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [f for f in dati if isinstance(f, dict)] if isinstance(dati, list) else []


def registra(
    base: Path,
    *,
    domanda: str,
    passi: int,
    riuscito: bool,
    esaurito: bool = False,
    chiuso_a_forza: bool = False,
) -> None:
    """Imbuca com'e' andata un'esplorazione. Non solleva mai.

    Solo cose osservate, mai dedotte: sono gli stessi quattro campi che
    ``delega.esegui`` mette nel referto per il padre.
    """
    domanda = " ".join(str(domanda or "").split())[:200]
    if not domanda:
        return
    fatti = leggi(base)
    fatti.append(
        {
            "domanda": domanda,
            "passi": int(passi),
            "riuscito": bool(riuscito),
            "esaurito": bool(esaurito),
            "chiuso_a_forza": bool(chiuso_a_forza),
        }
    )
    try:
        percorso = _percorso(base)
        percorso.parent.mkdir(parents=True, exist_ok=True)
        marker = percorso.parent / ".gitignore"
        if not marker.exists():
            marker.write_text("*\n", encoding="utf-8")
        # tmp + replace, come le preferenze: ``write_text`` tronca il file e
        # poi scrive, quindi un'interruzione a meta' -- o due turni che
        # archiviano insieme, e ``RUNNERS`` li fa girare in thread di sfondo --
        # lascia un JSON monco. Al giro dopo ``_leggi`` non lo parsa e i fatti
        # accumulati spariscono tutti insieme, in silenzio.
        tmp = percorso.with_suffix(percorso.suffix + ".tmp")
        tmp.write_text(
            json.dumps(fatti[-MAX_FATTI:], ensure_ascii=False, indent=1), encoding="utf-8"
        )
        os.replace(tmp, percorso)
    except OSError:
        return


# ---------------------------------------------------------------------------
# I derivati. Si ricalcolano, non si conservano.
# ---------------------------------------------------------------------------


def _pulito(fatto: dict[str, Any]) -> bool:
    """Un'esplorazione riuscita **dentro il budget**: la forma da imitare."""
    return (
        bool(fatto.get("riuscito"))
        and not fatto.get("esaurito")
        and not fatto.get("chiuso_a_forza")
    )


def esempio_riuscito(base: Path) -> str:
    """L'ultima domanda che in questo workspace ha funzionato al primo colpo.

    Serve a chi la delega la deve *scrivere*, cioe' al padre, e va detta dove
    il padre sta gia' leggendo un sollecito -- non in un elenco a parte che
    costerebbe contesto a ogni passo per essere letto una volta ogni tanto.
    """
    for fatto in reversed(leggi(base)):
        if _pulito(fatto):
            return str(fatto.get("domanda") or "")
    return ""


def tasso_esaurimento(base: Path) -> tuple[int, int]:
    """Quante esplorazioni su quante hanno finito i passi senza rispondere."""
    fatti = leggi(base)
    if len(fatti) < MIN_PER_UN_TASSO:
        return (0, 0)
    return (sum(1 for f in fatti if f.get("esaurito")), len(fatti))


def blocco_figlio(base: Path | None, *, passi_rimasti: int, totale: int) -> str:
    """Il blocco in coda al contesto dell'esploratore.

    Costa zero al padre -- vive nella finestra del figlio, che e' usa e getta.
    La prima riga e' informazione, non esortazione: e' la leva che sul padre ha
    gia' funzionato. La seconda compare **solo** se i fatti la reggono, e
    sparisce da sola quando smettono di reggerla.
    """
    if passi_rimasti < 0:
        return ""
    righe = [
        f"[esplorazione] Passi rimasti dopo questo: {passi_rimasti} su {totale}.",
    ]
    if passi_rimasti <= 1:
        righe.append(
            "E' l'ultimo utile: scrivi ORA il referto con quello che hai gia' "
            "letto, anche incompleto. Un passo speso a leggere adesso e' un "
            "referto perso."
        )
    if base is not None:
        esauriti, totali = tasso_esaurimento(base)
        if totali and esauriti / totali >= SOGLIA_ESAURIMENTO:
            righe.append(
                f"In questo workspace {esauriti} delle ultime {totali} "
                "esplorazioni hanno finito i passi senza rispondere: punta "
                "dritto con search_files, non allargare."
            )
    return "\n".join(righe)
