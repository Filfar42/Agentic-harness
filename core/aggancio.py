"""Aggancio delle modifiche: quando ``old_string`` non si trova alla lettera.

Perche' esiste
--------------
Nei log di agosto-settembre il 10,7% delle ``edit_file`` falliva (39 volte
"old_string non trovato", 6 "ambiguo"). L'errore diceva solo "rileggi e copia
esatto": il modello rileggeva il file intero -- un passo e qualche migliaio di
token -- e ritentava, spesso con lo stesso scarto. Un harness con lo stesso
problema (Crow, issue #276: 16 fallimenti su 147, l'11%) ha misurato che in 12
casi su 15 il file conteneva un tratto simile almeno all'85% al testo cercato,
e in 4 la differenza erano solo spazi. Aider riporta lo stesso: togliere
l'aggancio tollerante moltiplica gli errori di modifica.

Cosa fa, e dove si ferma
------------------------
Due gradi di tolleranza, e **solo sugli spazi**:

1. ``spazi_finali`` -- righe intere uguali a meno degli spazi a fine riga
   (e degli a capo CRLF/LF);
2. ``rientro`` -- righe intere uguali a meno di uno spostamento di rientro
   **identico** su tutte le righe non vuote, senza tab mescolati.

Si applica solo se il tratto e' **unico**. Sul rientro c'e' una scelta in piu':
``new_string`` si sposta dello stesso rientro solo se era rientrato come
``old_string`` (i due blocchi sbagliati insieme, il caso di Aider); se era gia'
rientrato come il file si applica com'e'; altrimenti non si indovina.

Il contenuto non si tocca mai: una parola diversa non e' "quasi uguale", e
applicare una modifica a un tratto *simile* vorrebbe dire modificare codice che
il modello non ha visto. Li' l'aggancio si ferma e l'errore porta invece il
tratto piu' simile -- righe, somiglianza, differenze -- cosi' la correzione
costa una chiamata e non una rilettura.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any

# Tetto alle righe mostrate nella diagnosi: serve a correggere la chiamata, non
# a sostituire read_file.
MAX_RIGHE_DIAGNOSI = 12
# Sotto questa somiglianza il tratto "piu' vicino" non aiuta: e' un altro pezzo
# di file, e mostrarlo manderebbe il modello a modificare la cosa sbagliata.
SOMIGLIANZA_MINIMA = 0.5
# Quanti candidati si valutano per davvero dopo la stima veloce.
CANDIDATI = 8
# File oltre queste righe: niente ricerca del piu' vicino (costo quadratico).
MAX_RIGHE_FILE = 20_000


@dataclass(frozen=True, slots=True)
class Aggancio:
    """Un tratto trovato con tolleranza: dove sta e cosa ci va al suo posto."""

    inizio: int          # offset nel testo del file
    fine: int            # offset esclusivo (a capo dell'ultima riga escluso)
    sostituto: str       # new_string gia' adattato (a capo, rientro)
    modo: str            # "a_capo" | "spazi_finali" | "rientro"
    riga: int            # prima riga del tratto, da 1


def righe_occorrenze(testo: str, frammento: str, massimo: int = 10) -> list[int]:
    """Le righe (da 1) in cui comincia ogni occorrenza esatta del frammento."""
    righe: list[int] = []
    da = 0
    while len(righe) < massimo:
        i = testo.find(frammento, da)
        if i < 0:
            break
        righe.append(testo.count("\n", 0, i) + 1)
        da = i + max(1, len(frammento))
    return righe


def dentro_il_rientro(testo: str, frammento: str, massimo: int = 10) -> bool:
    """Ogni occorrenza esatta comincia *dentro* il rientro di una riga?

    E' la firma della deriva di rientro su una riga sola: ``old_string`` con
    quattro spazi in meno si trova lo stesso, come sottostringa a meta' degli
    spazi iniziali, e la sostituzione esatta lascia davanti gli spazi non
    coperti -- con un ``new_string`` rientrato giusto, la riga finisce con
    quattro spazi di troppo. Meglio trattarla come aggancio sul rientro.
    """
    if not frammento[:1].isspace() or frammento[:1] in "\r\n":
        return False
    da = 0
    trovate = 0
    while trovate < massimo:
        i = testo.find(frammento, da)
        if i < 0:
            break
        trovate += 1
        inizio_riga = testo.rfind("\n", 0, i) + 1
        davanti = testo[inizio_riga:i]
        if not davanti or davanti.strip():
            return False
        da = i + 1
    return trovate > 0


def _righe_con_offset(testo: str) -> list[tuple[int, str, str]]:
    """(offset d'inizio, contenuto senza a capo, a capo) per ogni riga."""
    fuori: list[tuple[int, str, str]] = []
    pos = 0
    for grezza in testo.splitlines(keepends=True):
        contenuto = grezza.rstrip("\r\n")
        fuori.append((pos, contenuto, grezza[len(contenuto):]))
        pos += len(grezza)
    return fuori


def _rientro(riga: str) -> str:
    return riga[: len(riga) - len(riga.lstrip(" \t"))]


def _a_capo_del_file(testo: str) -> str:
    return "\r\n" if "\r\n" in testo else "\n"


def cerca(testo: str, vecchio: str, nuovo: str) -> tuple[list[Aggancio], str]:
    """Cerca ``vecchio`` con tolleranza sugli spazi.

    Torna gli agganci trovati al grado **meno** tollerante che ne trova almeno
    uno, e quel grado. Lista vuota: nessun aggancio possibile.
    """
    cercate = vecchio.replace("\r\n", "\n").split("\n")
    # Un old_string che finisce con "\n" ha un'ultima riga vuota fittizia.
    if len(cercate) > 1 and cercate[-1] == "":
        cercate = cercate[:-1]
    if not any(r.strip() for r in cercate):
        return [], ""
    righe = _righe_con_offset(testo)
    n = len(cercate)
    if n > len(righe):
        return [], ""
    a_capo = _a_capo_del_file(testo)
    # Stessa semantica della sostituzione esatta: se old_string finiva con un
    # a capo, anche l'a capo dell'ultima riga fa parte del tratto sostituito.
    consuma_a_capo = vecchio.endswith("\n")
    for modo in ("a_capo", "spazi_finali", "rientro"):
        trovati: list[Aggancio] = []
        for i in range(len(righe) - n + 1):
            finestra = [c for _, c, _ in righe[i:i + n]]
            sostituto = _confronta(finestra, cercate, nuovo, modo, a_capo)
            if sostituto is None:
                continue
            inizio = righe[i][0]
            ultima_off, ultima, terminatore = righe[i + n - 1]
            fine = ultima_off + len(ultima) + (len(terminatore) if consuma_a_capo else 0)
            trovati.append(Aggancio(inizio=inizio, fine=fine,
                                    sostituto=sostituto, modo=modo, riga=i + 1))
        if trovati:
            return trovati, modo
    return [], ""


def _confronta(finestra: list[str], cercate: list[str], nuovo: str, modo: str,
               a_capo: str) -> str | None:
    """Il sostituto adattato se la finestra aggancia al grado ``modo``, se no None."""
    righe_nuove = nuovo.replace("\r\n", "\n").split("\n")
    if modo == "a_capo":
        if finestra != cercate:
            return None
        return a_capo.join(righe_nuove)
    if modo == "spazi_finali":
        if [r.rstrip() for r in finestra] != [r.rstrip() for r in cercate]:
            return None
        return a_capo.join(righe_nuove)
    # modo == "rientro"
    delta: int | None = None
    for f, c in zip(finestra, cercate, strict=True):
        if not f.strip() and not c.strip():
            continue
        if f.strip() != c.strip():
            return None
        rf, rc = _rientro(f), _rientro(c)
        if "\t" in rf or "\t" in rc:
            return None
        d = len(rf) - len(rc)
        if delta is None:
            delta = d
        elif d != delta:
            return None
    if not delta:  # None (tutto vuoto) o 0 (gia' coperto da spazi_finali)
        return None
    piene_vecchie = [r for r in cercate if r.strip()]
    piene_nuove = [r for r in righe_nuove if r.strip()]
    if not piene_nuove:
        return a_capo.join(righe_nuove)
    if any("\t" in _rientro(r) for r in piene_nuove):
        return None
    minimo_vecchio = min(len(_rientro(r)) for r in piene_vecchie)
    minimo_nuovo = min(len(_rientro(r)) for r in piene_nuove)
    minimo_file = minimo_vecchio + delta
    if minimo_nuovo == minimo_file:
        # Solo old_string era sbagliato: new_string e' gia' allineato al file.
        return a_capo.join(righe_nuove)
    if minimo_nuovo != minimo_vecchio:
        return None  # rientri incoerenti: non si indovina
    if delta < 0 and any(len(_rientro(r)) < -delta for r in piene_nuove):
        return None
    spostate = [
        (" " * delta + r if delta > 0 else r[-delta:]) if r.strip() else r
        for r in righe_nuove
    ]
    return a_capo.join(spostate)


def piu_vicino(testo: str, vecchio: str) -> dict[str, Any] | None:
    """Il tratto del file piu' simile a ``vecchio``, per l'errore di edit_file."""
    cercate = vecchio.replace("\r\n", "\n").split("\n")
    if len(cercate) > 1 and cercate[-1] == "":
        cercate = cercate[:-1]
    righe = testo.replace("\r\n", "\n").split("\n")
    if not righe or len(righe) > MAX_RIGHE_FILE or not vecchio.strip():
        return None
    n = max(1, min(len(cercate), len(righe)))
    bersaglio = "\n".join(cercate)
    stime: list[tuple[float, int]] = []
    for i in range(len(righe) - n + 1):
        finestra = "\n".join(righe[i:i + n])
        m = difflib.SequenceMatcher(None, finestra, bersaglio, autojunk=False)
        stime.append((m.quick_ratio(), i))
    stime.sort(reverse=True)
    migliore: tuple[float, int] | None = None
    for _, i in stime[:CANDIDATI]:
        finestra = "\n".join(righe[i:i + n])
        esatta = difflib.SequenceMatcher(None, finestra, bersaglio, autojunk=False).ratio()
        if migliore is None or esatta > migliore[0]:
            migliore = (esatta, i)
    if migliore is None or migliore[0] < SOMIGLIANZA_MINIMA:
        return None
    somiglianza, i = migliore
    tratto = righe[i:i + n]
    larghezza = len(str(i + len(tratto)))
    mostrate = tratto[:MAX_RIGHE_DIAGNOSI]
    testo_numerato = "\n".join(
        f"{i + k + 1:>{larghezza}} | {r[:200]}" for k, r in enumerate(mostrate)
    )
    differenze = [
        d for d in difflib.ndiff(tratto, cercate)
        if d.startswith(("- ", "+ "))
    ][:MAX_RIGHE_DIAGNOSI]
    fuori: dict[str, Any] = {
        "righe": f"{i + 1}-{i + len(tratto)}",
        "somiglianza": round(somiglianza, 2),
        "testo": testo_numerato,
        # "-" e' il file, "+" e' il tuo old_string.
        "differenze": differenze,
    }
    if len(tratto) > MAX_RIGHE_DIAGNOSI:
        fuori["nota"] = f"mostrate {MAX_RIGHE_DIAGNOSI} righe su {len(tratto)}"
    return fuori
