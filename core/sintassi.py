"""Guardia di sintassi sulle scritture: una modifica non rompe un file sano.

E' la prima regola dell'Agent-Computer Interface di SWE-agent: un linter che
gira a ogni modifica e **non la lascia passare** se il file risultante non e'
sintatticamente valido. Su un modello piccolo e' la differenza fra un errore
che costa un passo -- il rifiuto dice riga, colonna e contesto, e il file sul
disco resta quello di prima -- e uno che ne costa dieci: un'indentazione
sbagliata scritta sul disco, un test che fallisce con un traceback lontano
dalla causa, tre tentativi di "riparare" il sintomo.

Tre scelte che tengono bassi i falsi allarmi:

* si **rifiuta** solo quando il file *prima* era valido e *dopo* non lo e'.
  Un file gia' rotto, o uno nuovo, si scrive lo stesso, con un avviso: in quei
  casi l'harness non ha niente di sano da proteggere, e bloccare una bozza a
  meta' toglierebbe al modello il modo di finirla;
* il parser e' quello dell'harness, non quello del progetto. Se il progetto
  usa una sintassi piu' nuova (``except A, B:`` di Python 3.14), il rifiuto
  sarebbe sbagliato: per questo la **stessa identica** scrittura ripetuta una
  seconda volta passa, con l'avviso. E' l'uscita ``ignore_red`` del piano,
  applicata qui;
* solo formati che la libreria standard sa leggere senza ambiguita': Python,
  JSON (non JSONC: ``tsconfig``, ``jsconfig`` e ``.vscode/`` ammettono i
  commenti e restano fuori), TOML.
"""

from __future__ import annotations

import ast
import hashlib
import json
import sys
import tomllib
import warnings
from dataclasses import dataclass
from pathlib import PurePosixPath

# Righe di contesto attorno all'errore, per parte. Il modello deve vedere il
# punto senza dover rileggere il file: e' la meta' del valore del rifiuto.
RIGHE_CONTESTO = 3
# JSON che ammettono commenti e virgole finali (JSONC): json.loads li
# dichiarerebbe rotti anche quando l'editor che li legge li accetta.
_JSONC_NOMI = ("tsconfig", "jsconfig")
_JSONC_CARTELLE = (".vscode",)


@dataclass(frozen=True, slots=True)
class ErroreSintassi:
    formato: str
    riga: int
    colonna: int
    messaggio: str
    contesto: str

    def come_dict(self) -> dict[str, object]:
        return {
            "formato": self.formato,
            "riga": self.riga,
            "colonna": self.colonna,
            "messaggio": self.messaggio,
            "contesto": self.contesto,
        }


def formato_di(percorso: str) -> str | None:
    """Il formato controllabile di un percorso, o None."""
    p = PurePosixPath(percorso.replace("\\", "/"))
    suffisso = p.suffix.lower()
    if suffisso in (".py", ".pyw"):
        return "python"
    if suffisso == ".toml":
        return "toml"
    if suffisso in (".json", ".ipynb"):
        nome = p.name.lower()
        if nome.startswith(_JSONC_NOMI) or any(c in p.parts for c in _JSONC_CARTELLE):
            return None
        return "json"
    return None


def _contesto(testo: str, riga: int) -> str:
    righe = testo.splitlines()
    if not righe or riga <= 0:
        return ""
    da = max(1, riga - RIGHE_CONTESTO)
    a = min(len(righe), riga + RIGHE_CONTESTO)
    larghezza = len(str(a))
    return "\n".join(
        f"{'>' if n == riga else ' '}{n:>{larghezza}} | {righe[n - 1]}"
        for n in range(da, a + 1)
    )


def controlla(testo: str, formato: str) -> ErroreSintassi | None:
    """None se ``testo`` e' valido per ``formato``, altrimenti il primo errore."""
    try:
        if formato == "python":
            # I SyntaxWarning (escape non validi e simili) non sono errori:
            # il file gira, e non si rifiuta una modifica per un avviso.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                ast.parse(testo)
        elif formato == "json":
            json.loads(testo)
        elif formato == "toml":
            tomllib.loads(testo)
        else:
            return None
    except SyntaxError as exc:
        riga = int(exc.lineno or 0)
        return ErroreSintassi(formato, riga, int(exc.offset or 0), str(exc.msg), _contesto(testo, riga))
    except json.JSONDecodeError as exc:
        return ErroreSintassi(formato, exc.lineno, exc.colno, exc.msg, _contesto(testo, exc.lineno))
    except tomllib.TOMLDecodeError as exc:
        # tomllib mette riga e colonna solo nel testo del messaggio.
        messaggio = str(exc)
        riga = 0
        if "(at line " in messaggio:
            try:
                riga = int(messaggio.rsplit("(at line ", 1)[1].split(",", 1)[0])
            except ValueError:
                riga = 0
        return ErroreSintassi(formato, riga, 0, messaggio, _contesto(testo, riga))
    except (ValueError, RecursionError, MemoryError) as exc:
        # ValueError: byte nulli nel sorgente Python. Ricorsione: annidamento
        # patologico. In entrambi i casi il file non e' leggibile dal parser.
        return ErroreSintassi(formato, 0, 0, f"{type(exc).__name__}: {exc}"[:300], "")
    return None


def impronta(percorso: str, testo: str) -> str:
    return hashlib.sha256(f"{percorso}\0{testo}".encode("utf-8", "replace")).hexdigest()


@dataclass(frozen=True, slots=True)
class Verdetto:
    """Cosa fare di una scrittura: ``rifiuta`` con l'errore, o scrivi con
    ``avviso`` (che puo' essere None)."""

    rifiuta: bool
    errore: ErroreSintassi | None


def valuta(
    percorso: str, prima: str | None, dopo: str, insistite: set[str]
) -> Verdetto:
    """Decide sulla scrittura di ``dopo`` al posto di ``prima`` (None = file nuovo).

    ``insistite`` e' la memoria delle scritture gia' rifiutate una volta in
    questo turno: la stessa scrittura, ripetuta identica, passa.
    """
    formato = formato_di(percorso)
    if formato is None:
        return Verdetto(False, None)
    errore = controlla(dopo, formato)
    if errore is None:
        return Verdetto(False, None)
    era_sano = prima is not None and controlla(prima, formato) is None
    if not era_sano:
        return Verdetto(False, errore)
    chiave = impronta(percorso, dopo)
    if chiave in insistite:
        return Verdetto(False, errore)
    insistite.add(chiave)
    return Verdetto(True, errore)


def rifiuto(percorso: str, errore: ErroreSintassi) -> str:
    """Il risultato del tool quando la scrittura e' respinta."""
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    return json.dumps(
        {
            "error": (
                f"Modifica NON applicata: '{percorso}' e' {errore.formato} valido, "
                f"e dopo questa modifica non lo sarebbe piu' (riga {errore.riga}"
                f", colonna {errore.colonna}: {errore.messaggio})."
            ),
            "error_code": "syntax_guard",
            "sintassi": errore.come_dict(),
            "hint": (
                "Il file sul disco e' ancora quello di prima. Correggi il punto "
                "indicato nel contesto (indentazione, parentesi, virgole, "
                "virgolette) e rimanda la modifica. Il controllo usa il parser "
                f"dell'harness (Python {python}): se sei certo che la sintassi "
                "sia valida per il progetto, ripeti identica la stessa chiamata "
                "e verra' eseguita."
            ),
            "retryable": True,
        },
        ensure_ascii=False,
    )
