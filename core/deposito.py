"""Deposito dei risultati: quello che oggi il troncamento butta via.

Il difetto, in una frase
------------------------
``smart_truncate`` taglia il centro e scrive *"Usa un comando piu' mirato
(grep/head/tail) per vedere questa porzione"*. Per ``read_file`` quella frase e'
**vera**: ``start_line``/``end_line`` esistono e la porzione si rilegge. Per lo
stdout di ``run_command`` e' **falsa**: quel testo non sta piu' da nessuna
parte, e l'unico modo di rivederlo e' rilanciare il comando -- che costa, e non
sempre e' ripetibile. E' una frase falsa esattamente nel punto che vale il
32,7% dei token di risultato dei tool (misura del 23/08/2026), cioe' la specie
di difetto che questo progetto ha gia' pagato due volte: una frase non vera
diventa una convinzione su cui il modello agisce.

Cosa cambia
-----------
Il testo intero va su disco **prima** di essere troncato, e il risultato porta
il percorso. In contesto il costo e' identico a prima -- stessa testa, stessa
coda, stesso budget -- ma la coda non muore: si rilegge con ``read_file`` o ci
si cerca dentro con ``search_files``. **Una difesa con perdita diventa una
difesa senza perdita, a parita' di token.** L'argomento regge da solo, senza
bisogno dell'RLM: sul locale il prefisso sta in KV cache e il guadagno non
sarebbe stato in token comunque.

Tre scelte che sembrano dettagli e non lo sono
----------------------------------------------
* **La cartella e' piatta.** ``list_files`` e ``search_files`` filtrano le
  cartelle che iniziano con un punto, ma il filtro agisce sui *sottolivelli*
  del walk: una cartella nascosta passata come **radice** viene percorsa lo
  stesso. Piatta, quindi, il deposito e' gia' raggiungibile dai tool esistenti
  -- nessun tool nuovo, nessuna eccezione da scrivere in ``tools.py``.
* **Non sta dentro `.analisi/`.** Il banco di prova si svuota all'inizio di
  ogni turno: un handle imbucato li' morirebbe fra un turno e il successivo,
  mentre la cronologia continuerebbe a portarne il puntatore.
* **Si pota a tetto di spazio, dal piu' vecchio.** I riferimenti ancora usati
  dal contesto vengono protetti dal chiamante. Se da soli superano il tetto,
  si conserva l'evidenza: il limite di spazio non rende falsi i puntatori.
"""

from __future__ import annotations

import re
import stat
import unicodedata
from collections.abc import Iterable
from pathlib import Path

SCHEDARIO = ".deposito"

# Tetto di spazio, in MB. A 64 MB ci stanno migliaia di stdout interi: il
# numero non e' tarato su una misura, e' tarato su "non deve dare fastidio".
MAX_MB_DEFAULT = 64

# Tetto al singolo deposito. Un comando che sputa quaranta megabyte non ha un
# problema di contesto, ha un problema di comando: si tiene quello che serve a
# capirlo e si dice che e' stato tagliato anche qui -- **dirlo** e' il punto,
# un file che finisce a meta' senza avvisare e' un'altra frase falsa.
MAX_TESTO_CHARS = 4_000_000

_TAGLIO = "\n\n[... deposito: il testo superava il tetto ed e' stato tagliato qui ...]\n"


def cartella(base: Path) -> Path:
    return Path(base) / SCHEDARIO


def _cartella_sicura(base: Path) -> Path | None:
    """Il deposito e' una cartella fisica del workspace, mai un rimando."""
    try:
        cart = cartella(base)
        if cart.is_symlink() or cart.is_junction():
            return None
        if cart.resolve().parent != Path(base).resolve():
            return None
        return cart
    except (OSError, RuntimeError, ValueError):
        return None


def _files(cart: Path) -> list[tuple[float, int, Path]]:
    """File regolari diretti, senza seguire link o interrompersi su un errore."""
    files = []
    try:
        for path in cart.glob("*.txt"):
            try:
                st = path.lstat()
                if stat.S_ISREG(st.st_mode) and not path.is_junction():
                    files.append((st.st_mtime, st.st_size, path))
            except OSError:
                continue
    except OSError:
        pass
    return files


def _slug(testo: str) -> str:
    piatto = unicodedata.normalize("NFKD", testo or "")
    piatto = piatto.encode("ascii", "ignore").decode("ascii").lower()
    piatto = re.sub(r"[^a-z0-9]+", "-", piatto).strip("-")
    return (piatto[:40] or "risultato").rstrip("-")


def _prossimo_numero(cart: Path) -> int:
    massimo = 0
    for path in cart.glob("*.txt"):
        m = re.match(r"^(\d+)-", path.name)
        if m:
            massimo = max(massimo, int(m.group(1)))
    return massimo + 1


def deposita(base: Path, testo: str, *, etichetta: str, intestazione: str = "") -> str | None:
    """Scrive il testo intero e torna il percorso relativo, o ``None``.

    ``None`` copre sia "non c'era niente da depositare" sia "il disco ha detto
    di no": in entrambi i casi il chiamante tronca come ha sempre fatto e non
    promette niente. Un deposito fallito non deve mai far fallire il tool, che
    e' il lavoro vero.
    """
    testo = testo or ""
    if not testo.strip():
        return None
    if len(testo) > MAX_TESTO_CHARS:
        testo = testo[:MAX_TESTO_CHARS] + _TAGLIO
    try:
        cart = _cartella_sicura(base)
        if cart is None:
            return None
        cart.mkdir(parents=True, exist_ok=True)
        # `*` in un .gitignore della cartella stessa: git la ignora per intero
        # senza toccare il .gitignore del progetto, che e' roba dell'utente.
        marker = cart / ".gitignore"
        try:
            with marker.open("x", encoding="utf-8") as stream:
                stream.write("*\n")
        except FileExistsError:
            pass
        corpo = f"# {intestazione}\n\n{testo}" if intestazione else testo
        # ``open(..., "x")`` e non ``write_text``: fra il calcolo del numero e
        # la scrittura ci puo' stare un altro turno -- ``RUNNERS`` li fa girare
        # in thread di sfondo -- e ``write_text`` sovrascriveva in silenzio il
        # deposito appena creato da quell'altro. Qui il primo che arriva tiene
        # il nome e il secondo prende il successivo.
        primo = _prossimo_numero(cart)
        for tentativo in range(primo, primo + 50):
            nome = f"{tentativo:03d}-{_slug(etichetta)}.txt"
            try:
                with open(cart / nome, "x", encoding="utf-8") as fh:
                    fh.write(corpo)
                break
            except FileExistsError:
                continue
        else:
            return None
    except OSError:
        return None
    return f"{SCHEDARIO}/{nome}"


def pota(
    base: Path, max_mb: int = MAX_MB_DEFAULT, *, protetti: Iterable[str | Path] = (),
) -> int:
    """Porta il deposito sotto il tetto buttando i file piu' vecchi.

    Torna quanti ne ha tolti. Si chiama a inizio turno e non a ogni scrittura:
    e' una stat per file su una cartella piccola, e farlo dodici volte per
    turno non cambierebbe niente se non il numero di syscall.

    ``protetti`` contiene riferimenti relativi al workspace (.deposito/...) o
    assoluti interni al deposito. Questi file restano anche sopra il tetto;
    riferimenti esterni o non validi non proteggono file omonimi del deposito.
    """
    if max_mb <= 0:
        return 0
    cart = _cartella_sicura(base)
    if cart is None or not cart.is_dir():
        return 0
    tetto = int(max_mb) * 1024 * 1024
    radice = cart.resolve()
    percorsi_protetti: set[Path] = set()
    for riferimento in protetti:
        try:
            path = Path(riferimento)
            if not path.is_absolute():
                path = Path(base) / path
            risolto = path.resolve()
            if risolto.parent == radice:
                percorsi_protetti.add(risolto)
        except (OSError, RuntimeError, ValueError, TypeError):
            continue
    files = _files(cart)
    totale = sum(size for _, size, _ in files)
    if totale <= tetto:
        return 0
    tolti = 0
    for _, size, path in sorted(files):
        if totale <= tetto:
            break
        if radice / path.name in percorsi_protetti:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        totale -= size
        tolti += 1
    return tolti


def occupazione(base: Path) -> int:
    """Byte occupati dal deposito. Serve a dirlo, non a decidere."""
    cart = _cartella_sicura(base)
    if cart is None or not cart.is_dir():
        return 0
    return sum(size for _, size, _ in _files(cart))
