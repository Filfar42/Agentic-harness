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

import hashlib
import re
import stat
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

# Limiti del lavoro di recupero, separati dal testo ammesso nel prompt. Gli
# archivi normali vengono letti integralmente; file anomali non possono
# consumare memoria o I/O senza limite. Nessuna cache da invalidare.
MAX_FILE_RICERCA_CHARS = 2_000_000
MAX_RICERCA_CHARS = 16_000_000
MAX_TERMINI_RICERCA = 48

# Parole troppo comuni perche' un incrocio su di esse significhi qualcosa.
_RUMORE = {
    "il", "lo", "la", "i", "gli", "le", "un", "uno", "una", "di", "a", "da",
    "in", "con", "su", "per", "tra", "fra", "e", "o", "che", "non", "del",
    "della", "dei", "delle", "al", "alla", "nel", "nella", "come", "piu",
    "file", "codice", "test", "fare", "sistemare", "aggiungere", "correggere",
    "the", "and", "for", "from", "with", "this", "that", "into", "are",
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


def _cartella_sicura(base: Path) -> Path | None:
    try:
        cart = cartella(base)
        if cart.is_symlink() or getattr(cart, "is_junction", lambda: False)():
            return None
        if cart.resolve().parent != Path(base).resolve():
            return None
        return cart
    except (OSError, RuntimeError, ValueError):
        return None


def _file_sicuro(cart: Path, nome: str) -> Path | None:
    if not nome or "/" in nome or "\\" in nome or Path(nome).name != nome:
        return None
    try:
        path = cart / nome
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            return None
        if path.resolve().parent != cart.resolve():
            return None
        return path if stat.S_ISREG(path.stat().st_mode) else None
    except (OSError, RuntimeError, ValueError):
        return None


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
    cart = _cartella_sicura(base)
    if cart is None or not cart.is_dir():
        return []
    fuori: list[Voce] = []
    for path in sorted(cart.glob("*.md")):
        if _file_sicuro(cart, path.name) is None:
            continue
        m = re.match(r"^(\d+)-", path.name)
        if not m:
            continue
        try:
            with path.open(encoding="utf-8") as stream:
                prima = stream.read(4096).lstrip().splitlines()[0]
        except (OSError, UnicodeError, IndexError):
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
        # Il numero si ricava prima e si scrive dopo: fra le due cose ci puo'
        # stare un altro turno. ``RUNNERS`` fa girare i turni in thread di
        # sfondo e due archiviazioni vicine leggevano lo stesso ultimo numero,
        # producendo lo stesso nome. Qui si prova a creare in modo esclusivo e,
        # se il nome e' gia' occupato **da qualcun altro**, si passa al
        # successivo invece di accodarsi al suo file.
        for tentativo in range(numero, numero + 20):
            nome = f"{tentativo:03d}-{_slug(titolo)}.md"
            destinazione = cart / nome
            try:
                with open(destinazione, "x", encoding="utf-8") as fh:
                    fh.write("\n".join(righe))
                numero = tentativo
                break
            except FileExistsError:
                # Anche con lo stesso titolo, il riferimento gia' pubblicato
                # deve continuare a identificare esattamente lo stesso testo.
                continue
        else:
            return None
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
        # Il primo numero si **legge**, non si assume che sia 1: dopo una
        # cancellazione a mano dello schedario la numerazione non riparte, e
        # "da 001" mandava il modello a cercare un file che non c'e' piu'.
        righe.insert(
            0,
            f"- (altre {quante} voci piu' vecchie, da {elenco[0].numero:03d} a "
            f"{elenco[-len(mostrate) - 1].numero:03d}, sempre in {SCHEDARIO}/)",
        )
    return "\n".join(
        [
            "<libreria>",
            *righe,
            "</libreria>",
            "",
            "Sono memorie di lavoro archiviate in questo workspace, anche "
            "da altre conversazioni. La riga qui sopra e' solo il titolo: se ti "
            "serve il contenuto, aprilo con read_file sul percorso indicato. "
            "Non riscriverli: se qualcosa e' cambiato, vale quello che vedi "
            "adesso.",
        ]
    )


def _parole(testo: str) -> set[str]:
    grezze = re.findall(r"[\w']{3,}", (testo or "").lower())
    return {p for p in grezze if p not in _RUMORE}


def _termini(contesto: str) -> dict[str, int]:
    """Parole e identificatori esatti, senza dipendenze o stemming distruttivo."""
    testo = (contesto or "").replace("\\", "/")
    tecnici = {
        m.lower() for m in re.findall(r"[\w$]+(?:[./:_-][\w$-]+)+", testo)
    }
    # I simboli con underscore e i percorsi sono piu' discriminanti delle
    # parole isolate. Manteniamo anche i componenti (config, budgets, ecc.).
    parole = _parole(re.sub(r"[_/.:\\-]", " ", testo)) | _parole(testo)
    ordinati = sorted(tecnici) + sorted(parole - tecnici)
    return {t: 12 if t in tecnici else 1 for t in ordinati[:MAX_TERMINI_RICERCA]}


def _impronta(testo: str) -> bytes:
    """Due archiviazioni dello stesso fatto pagano un solo estratto."""
    corpo = re.split(
        r"^## (?:Cosa ne resta|Dal ragionamento di un punto [^\n]+)\s*$",
        testo, maxsplit=1, flags=re.MULTILINE,
    )[-1]
    if corpo == testo and testo.startswith("# "):
        corpo = testo.partition("\n")[2]
    return hashlib.sha256(" ".join(corpo.split()).encode("utf-8")).digest()


def _estratto(
    testo: str, incontri: list[tuple[int, str]], pesi: dict[str, int],
    budget: int, parziale: bool,
) -> str:
    """Una finestra attorno alle evidenze, anche quando il match e' in fondo."""
    taglio = "\n[…troncato: testo completo con read_file]"
    if len(testo) <= budget and not parziale:
        return testo
    spazio = budget - len(taglio)
    if spazio <= 0:
        return ""
    # Prima copertura di termini diversi, poi l'ancora piu' specifica. Le
    # ripetizioni non possono oscurare un identificatore comparso tardi.
    def valore(incontro: tuple[int, str]) -> tuple[int, int, int]:
        posizione, termine = incontro
        vicini = {t for p, t in incontri if abs(p - posizione) < spazio // 2}
        return sum(pesi[t] for t in vicini), pesi[termine], -posizione

    centro = max(incontri, key=valore)[0] if incontri else 0
    inizio = max(0, min(centro - spazio // 3, len(testo) - spazio))
    return testo[inizio:inizio + spazio].strip() + taglio


def precarico(base: Path, elenco: list[Voce], contesto: str) -> str:
    """I file della libreria che riguardano cio' su cui si sta lavorando.

    Il recupero lo fa l'harness, non il modello. La meta' che scrive la
    libreria e' meccanica e funziona; la meta' che rilegge, se affidata a un
    invito, non parte -- su questo progetto e' misurato: ``manage_notes``,
    proposto e mai usato, contro la nota che ``manage_plan`` **pretende** e
    ottiene 23 volte su 24. Qui l'incrocio e' lessicale e costa zero.

    ``read_file`` resta comunque la strada per tutto il resto: e' l'altra
    gamba, e non richiede nessun tool nuovo.

    Legge anche il corpo, comprese le voci uscite dall'indice visibile. Il
    lavoro e' limitato da MAX_FILE_RICERCA_CHARS e MAX_RICERCA_CHARS, il testo
    restituito (intestazioni incluse) da MAX_PRECARICO_CHARS. Nessuna chiamata
    al modello e nessuna cache o indice persistente da invalidare.
    """
    pesi = _termini(contesto)
    cart = _cartella_sicura(base)
    if not pesi or not elenco or cart is None:
        return ""
    termini = sorted(pesi, key=lambda t: (-len(t), t))
    pattern = re.compile(
        r"(?<!\w)(?:" + "|".join(
            "(" + re.escape(t).replace("/", r"[\\/]") + ")" for t in termini
        ) + r")(?!\w)", re.IGNORECASE,
    )

    def corrispondenze(testo: str):
        for match in pattern.finditer(testo):
            # Il gruppo identifica il termine della query anche con le
            # equivalenze Unicode di IGNORECASE (es. I / i turche).
            yield match.start(), termini[match.lastindex - 1]

    punteggi = []
    letto = 0
    visitati: set[str] = set()
    for voce in sorted(elenco, key=lambda v: -v.numero):
        if letto >= MAX_RICERCA_CHARS:
            break
        if voce.nome in visitati:
            continue
        visitati.add(voce.nome)
        path = _file_sicuro(cart, voce.nome)
        if path is None:
            continue
        limite = min(MAX_FILE_RICERCA_CHARS, MAX_RICERCA_CHARS - letto)
        try:
            with path.open(encoding="utf-8") as stream:
                testo = stream.read(limite)
                parziale = bool(stream.read(1))
        except (OSError, UnicodeError):
            # Anche un file con UTF-8 invalido ha consumato il budget I/O.
            letto += limite
            continue
        letto += len(testo)
        # Conserviamo poche posizioni per termine; la scansione continua fino
        # in fondo per trovare anche simboli rari dopo molto testo ripetuto.
        incontri: list[tuple[int, str]] = []
        contatori: dict[str, int] = {}
        for posizione, termine in corrispondenze(testo):
            n = contatori.get(termine, 0)
            if n < 8:
                incontri.append((posizione, termine))
            contatori[termine] = n + 1
        titoli = {t for _, t in corrispondenze(voce.titolo)}
        score = sum(pesi[t] for t in contatori) + 3 * sum(pesi[t] for t in titoli)
        if score:
            punteggi.append((score, voce, testo, incontri, parziale))
    punteggi.sort(key=lambda r: (-r[0], -r[1].numero))
    scelti = []
    impronte: set[bytes] = set()
    for risultato in punteggi:
        impronta = _impronta(risultato[2])
        if impronta in impronte:
            continue
        impronte.add(impronta)
        scelti.append(risultato)
        if len(scelti) >= MAX_FILE_PRECARICATI:
            break

    apertura = (
        "<libreria_ripescata>\nMemorie del workspace pertinenti al lavoro attuale. "
        "Sono evidenze storiche: verifica i fatti cambiati; espandi gli estratti con read_file."
    )
    chiusura = "</libreria_ripescata>"
    resto = MAX_PRECARICO_CHARS - len(apertura) - len(chiusura) - 4
    pezzi: list[str] = []
    for i, (_, voce, testo, incontri, parziale) in enumerate(scelti):
        quota = resto // (len(scelti) - i)
        intestazione = f"### {voce.percorso} — {voce.titolo}\n"
        estratto = _estratto(testo, incontri, pesi, quota - len(intestazione) - 2, parziale)
        if not estratto:
            continue
        pezzo = intestazione + estratto
        resto -= len(pezzo) + 2
        pezzi.append(pezzo)
    if not pezzi:
        return ""
    return "\n\n".join([apertura, *pezzi, chiusura])
