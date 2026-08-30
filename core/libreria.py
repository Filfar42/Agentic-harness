"""Libreria di concetti: i riassunti smettono di riassumersi addosso.

## Il difetto che toglie

``compaction.trascrizione`` faceva rientrare il riassunto precedente nella
trascrizione della compattazione successiva, per non accumulare una pila di
riassunti in cronologia. La ragione era buona e il prezzo non era scritto da
nessuna parte: alla seconda compattazione il modello riassume un riassunto,
alla terza il riassunto di un riassunto, ogni volta attraverso lo stesso imbuto
da ``MAX_TOKEN_RIASSUNTO`` token. Una fotocopia di fotocopia.

Erano due esigenze in conflitto -- non accumulare, non perdere -- risolte con
l'unico compromesso possibile finche' l'informazione poteva vivere soltanto
dentro la cronologia. Qui si separano: il testo va **fuori**, su disco, e la
cronologia ne tiene solo una riga. Cosi' non si accumula *e* non decade.

## La regola che fa tutto il lavoro

**Un file archiviato non si riapre mai.** Non si migliora, non si accorcia, non
si fonde con un altro. Un fatto superato si corregge con una voce nuova che
dice cosa smentisce. E' un registro, non una wiki: nel momento in cui si
concede la riscrittura si e' rimesso il decadimento con un altro nome, solo
piu' lentamente e senza accorgersene.

L'unica cosa che ha diritto di decadere e' **l'indice**, perche' e' l'unica
ricostruibile: le prime righe dei file sono su disco e l'indice si rigenera da
li' senza chiamare il modello (``voci``). I file no: quelli sono la fonte.

## Cosa costa

Zero generazioni. Non c'e' un archivista che chiama il modello: si promuove il
riassunto che la compattazione ha gia' prodotto, e che oggi verrebbe buttato
alla compattazione dopo. L'unico costo ricorrente e' l'indice nel blocco di
coda -- una riga per compattazione, e le compattazioni sono rare.

## Dove vive

``<workspace>/.memoria/``. Con il punto davanti, quindi gia' invisibile a
``list_files``, ``search_files`` e all'albero dell'intestazione: la libreria non
deve sporcare il progetto dell'utente. Il rovescio e' che **senza l'indice, per
il modello non esiste**: e' l'unica via d'accesso, ed e' per questo che il
blocco di coda dice anche come rileggere i file.

Non dentro ``.analisi/``, che ``reset_scratch`` cancella ad ogni turno.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

SCHEDARIO = ".memoria"

# Quante voci restano nel blocco di coda. Oltre, le piu' vecchie si
# raggruppano in una riga sola: l'indice puo' impoverirsi senza perdere niente
# -- i file restano tutti, e ``voci`` li rilegge dal disco quando serve.
MAX_VOCI_INDICE = 24

# Tetto al titolo di una voce: e' una riga d'indice, non un riassunto.
MAX_TITOLO_CHARS = 90

# Quanto testo puo' entrare in un precarico d'ufficio. Due file interi
# riempirebbero la finestra che la compattazione ha appena liberato.
MAX_PRECARICO_CHARS = 3_000
MAX_FILE_PRECARICATI = 2

# Parole troppo comuni perche' un incrocio su di esse significhi qualcosa.
_RUMORE = {
    "il", "lo", "la", "i", "gli", "le", "un", "uno", "una", "di", "a", "da",
    "in", "con", "su", "per", "tra", "fra", "e", "o", "che", "non", "del",
    "della", "dei", "delle", "al", "alla", "nel", "nella", "come", "piu",
    "file", "codice", "test", "fare", "sistemare", "aggiungere", "correggere",
}


@dataclass(slots=True)
class Voce:
    """Una riga d'indice, e il file che ci sta sotto."""

    numero: int
    nome: str
    titolo: str

    @property
    def percorso(self) -> str:
        return f"{SCHEDARIO}/{self.nome}"


def cartella(base: Path) -> Path:
    return Path(base) / SCHEDARIO


def _slug(testo: str) -> str:
    piatto = unicodedata.normalize("NFKD", testo or "")
    piatto = piatto.encode("ascii", "ignore").decode("ascii").lower()
    piatto = re.sub(r"[^a-z0-9]+", "-", piatto).strip("-")
    return (piatto[:48] or "concetto").rstrip("-")


def _titolo(riassunto: str, richieste: list[str]) -> str:
    """La riga d'indice: cosa contiene questo pezzo di lavoro.

    Prima la richiesta dell'utente, se c'e': e' l'unica frase del blocco che
    non e' passata da un riassunto, quindi l'unica di cui si sappia con
    certezza cosa voleva dire. Altrimenti la prima voce ``FATTO``.
    """
    for testo in richieste:
        pulito = " ".join(str(testo or "").split())
        if pulito:
            return pulito[:MAX_TITOLO_CHARS]
    for riga in (riassunto or "").splitlines():
        pulito = riga.strip().lstrip("-* ").strip()
        if pulito.upper().startswith("FATTO"):
            corpo = pulito.split(":", 1)[-1].strip()
            if corpo:
                return corpo[:MAX_TITOLO_CHARS]
    for riga in (riassunto or "").splitlines():
        pulito = riga.strip().lstrip("-* ").strip()
        if pulito:
            return pulito[:MAX_TITOLO_CHARS]
    return "tratto di lavoro"


def voci(base: Path) -> list[Voce]:
    """L'indice, riletto dal disco.

    Non c'e' un file d'indice da tenere allineato: il nome porta il numero e lo
    slug, la prima riga porta il titolo. Un indice che vive in un file a parte
    e' un secondo posto in cui la stessa cosa puo' essere scritta diversa.
    """
    cart = cartella(base)
    if not cart.is_dir():
        return []
    fuori: list[Voce] = []
    for path in sorted(cart.glob("*.md")):
        m = re.match(r"^(\d+)-", path.name)
        if not m:
            continue
        try:
            prima = path.read_text(encoding="utf-8").lstrip().splitlines()[0]
        except (OSError, IndexError):
            continue
        fuori.append(
            Voce(
                numero=int(m.group(1)),
                nome=path.name,
                titolo=prima.lstrip("# ").strip()[:MAX_TITOLO_CHARS] or path.stem,
            )
        )
    return sorted(fuori, key=lambda v: v.numero)


def archivia(base: Path, *, riassunto: str, richieste: list[str]) -> Voce | None:
    """Scrive un tratto compattato nella libreria. Non tocca mai i file esistenti.

    Ritorna la voce d'indice, o ``None`` se non c'era niente da archiviare o il
    disco ha detto di no -- un'archiviazione fallita non deve mai far fallire
    la compattazione, che e' il lavoro vero.
    """
    riassunto = (riassunto or "").strip()
    if not riassunto:
        return None
    corpo: list[str] = []
    if richieste:
        corpo += [
            "## Chiesto dall'utente, testuale",
            *[f"- «{r}»" for r in richieste],
            "",
        ]
    corpo += ["## Cosa ne resta", riassunto, ""]
    return _scrivi(base, titolo=_titolo(riassunto, richieste), corpo=corpo)


def archivia_estratto(
    base: Path, *, punto: str, estratto: str, saltato: bool = False
) -> Voce | None:
    """Archivia cio' che resta del ragionamento di un punto di piano chiuso.

    Stessa cartella e stesse regole della compattazione -- append-only, un file
    archiviato non si riapre mai -- ma un'intestazione diversa, e non e' un
    dettaglio estetico: ``archivia`` scrive "Chiesto dall'utente, testuale", e
    il testo di un punto di piano **non e' una frase dell'utente**, e' una
    frase che il modello si e' scritto da solo. Riusare quella sezione
    metterebbe in archivio, per sempre, una citazione falsa -- lo stesso
    difetto gia' pagato due volte su questo progetto, dove una frase non vera
    diventa una convinzione su cui il modello agisce.

    ``saltato`` finisce nella voce perche' cambia quanta fiducia merita: un
    punto abbandonato dopo una verifica rossa non deve rileggersi fra dieci
    sessioni come una conclusione raggiunta.
    """
    estratto = (estratto or "").strip()
    titolo = " ".join(str(punto or "").split()) or "punto di lavoro"
    if not estratto:
        return None
    etichetta = "saltato" if saltato else "chiuso"
    corpo = [
        f"## Dal ragionamento di un punto {etichetta}",
        estratto,
        "",
    ]
    return _scrivi(base, titolo=titolo, corpo=corpo)


def _scrivi(base: Path, *, titolo: str, corpo: list[str]) -> Voce | None:
    """Il gesto di scrittura, comune a tutti i modi di archiviare.

    Ritorna la voce d'indice, o ``None`` se il disco ha detto di no -- una
    archiviazione fallita non deve mai far fallire il lavoro che l'ha
    prodotta, ne' la compattazione ne' la chiusura di un punto.
    """
    titolo = (titolo or "").strip()[:MAX_TITOLO_CHARS] or "tratto di lavoro"
    esistenti = voci(base)
    numero = (esistenti[-1].numero if esistenti else 0) + 1
    nome = f"{numero:03d}-{_slug(titolo)}.md"
    righe = [f"# {titolo}", "", *corpo]

    try:
        cart = cartella(base)
        cart.mkdir(parents=True, exist_ok=True)
        # `*` in un .gitignore della cartella stessa: git la ignora per intero
        # senza toccare il .gitignore del progetto, che e' roba dell'utente.
        marker = cart / ".gitignore"
        if not marker.exists():
            marker.write_text("*\n", encoding="utf-8")
        destinazione = cart / nome
        if destinazione.exists():
            # Non si sovrascrive mai. Se il nome e' occupato -- due tratti con
            # lo stesso titolo -- si aggiunge, non si sostituisce.
            with open(destinazione, "a", encoding="utf-8") as fh:
                fh.write("\n---\n\n" + "\n".join(righe[1:]))
        else:
            destinazione.write_text("\n".join(righe), encoding="utf-8")
    except OSError:
        return None
    return Voce(numero=numero, nome=nome, titolo=titolo)


def render_block(elenco: list[Voce]) -> str:
    """L'indice, da mettere **in coda** insieme a piano e note.

    Mai nell'intestazione d'ambiente: quel blocco sta nel prefisso ed e'
    byte-identico fra un passo e l'altro: e' cio' che rende riusabile il KV
    cache. Un indice che cresce ad ogni compattazione lo farebbe divergere, e
    si pagherebbe un prompt eval intero per aggiungere una riga.
    """
    if not elenco:
        return ""
    mostrate = elenco[-MAX_VOCI_INDICE:]
    righe = [f"- {v.percorso} — {v.titolo}" for v in mostrate]
    if len(elenco) > len(mostrate):
        quante = len(elenco) - len(mostrate)
        righe.insert(
            0,
            f"- (altre {quante} voci piu' vecchie, da 001 a "
            f"{elenco[-len(mostrate) - 1].numero:03d}, sempre in {SCHEDARIO}/)",
        )
    return "\n".join(
        [
            "<libreria>",
            *righe,
            "</libreria>",
            "",
            "Sono tratti di questa conversazione gia' usciti dal contesto, "
            "salvati per intero. La riga qui sopra e' solo il titolo: se ti "
            "serve il contenuto, aprilo con read_file sul percorso indicato. "
            "Non riscriverli: se qualcosa e' cambiato, vale quello che vedi "
            "adesso.",
        ]
    )


def _parole(testo: str) -> set[str]:
    grezze = re.findall(r"[\w']{4,}", (testo or "").lower())
    return {p for p in grezze if p not in _RUMORE}


def precarico(base: Path, elenco: list[Voce], contesto: str) -> str:
    """I file della libreria che riguardano cio' su cui si sta lavorando.

    Il recupero lo fa l'harness, non il modello. La meta' che scrive la
    libreria e' meccanica e funziona; la meta' che rilegge, se affidata a un
    invito, non parte -- su questo progetto e' misurato: ``manage_notes``,
    proposto e mai usato, contro la nota che ``manage_plan`` **pretende** e
    ottiene 23 volte su 24. Qui l'incrocio e' lessicale e costa zero.

    ``read_file`` resta comunque la strada per tutto il resto: e' l'altra
    gamba, e non richiede nessun tool nuovo.
    """
    parole = _parole(contesto)
    if not parole or not elenco:
        return ""
    punteggi: list[tuple[int, Voce]] = []
    for v in elenco:
        comuni = len(parole & _parole(v.titolo))
        if comuni:
            punteggi.append((comuni, v))
    if not punteggi:
        return ""
    punteggi.sort(key=lambda coppia: (-coppia[0], -coppia[1].numero))

    pezzi: list[str] = []
    speso = 0
    for _, v in punteggi[:MAX_FILE_PRECARICATI]:
        try:
            testo = (cartella(base) / v.nome).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        resto = MAX_PRECARICO_CHARS - speso
        if resto <= 200:
            break
        if len(testo) > resto:
            testo = testo[:resto].rstrip() + "\n[…troncato: il resto con read_file]"
        speso += len(testo)
        pezzi.append(f"### {v.percorso}\n{testo}")
    if not pezzi:
        return ""
    return "\n\n".join(
        [
            "<libreria_ripescata>",
            "Riguardano il punto su cui stai lavorando: te li rimetto davanti "
            "io, non serve rileggerli.",
            *pezzi,
            "</libreria_ripescata>",
        ]
    )
