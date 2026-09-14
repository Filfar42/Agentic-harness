"""Vault: una cartella con un nome, una descrizione e le sue conversazioni.

## Cos'e' un vault

Un posto di lavoro, non una modalita'. Nome, descrizione, cartella, e le chat
che ci sono state fatte dentro: si apre e si trova quello che c'era, come una
scrivania a cui si torna.

Prima era solo l'attivazione della modalita' manutentore-wiki su un workspace
riconosciuto dalla struttura ``raw/`` + ``wiki/``. Quella modalita' resta ed e'
diventata **una proprieta' del vault** (``wiki: true`` in ``.vault.json``), non
la sua definizione: un vault puo' essere una wiki, un progetto di codice, una
raccolta di appunti.

## Dove vive l'identita'

In ``.vault.json``, nella radice del vault -- non nelle impostazioni
dell'harness. Un vault che si autodescrive sopravvive allo spostamento della
cartella, alla copia su un'altra macchina e alla reinstallazione: il registro
nelle preferenze tiene solo l'elenco dei percorsi conosciuti, perche' quello
non si puo' ricavare (nessuno vuole che l'harness scandisca il disco).

Due campi di testo, e non uno, perche' rispondono a due domande diverse:

* ``descrizione`` -- per chi guarda l'elenco: cos'e' questo posto. Non arriva
  mai al modello.
* ``istruzioni`` -- per il modello: come ci si lavora dentro. Entra nel prompt
  di ogni chat del vault.

Tenerli separati e' una difesa concreta: una riga scritta di fretta per
ritrovare la cartella non deve diventare un'istruzione che l'agente segue in
ogni turno per i mesi successivi.

## Il livello wiki (opzionale)

Quando ``wiki`` e' acceso, il workspace e' organizzato secondo il pattern "LLM
Wiki" di Karpathy: tre livelli ben separati.

- ``raw/``     -- fonti grezze (articoli, paper, trascrizioni). **Immutabili**:
                 l'agente le legge ma non le tocca mai.
- ``wiki/``    -- pagine markdown generate e mantenute dall'agente: sintesi,
                 pagine di entita' e concetti, confronti. L'agente possiede
                 questo livello per intero.
- ``CLAUDE.md`` -- lo schema: convenzioni della wiki, flussi di ingest/query/
                 lint. Co-evolve fra utente e agente.

Due file speciali tengono la wiki navigabile: ``wiki/index.md`` (catalogo per
categoria, aggiornato a ogni ingest) e ``wiki/log.md`` (cronologia append-only
con voci ``## [data] operazione | soggetto``, parsebile con grep).

## Perche' un modulo e non solo convenzioni

Il resto dell'harness deve poter *riconoscere* un vault dal workspace corrente
(per accendere la modalita' manutentore) e *interrogarlo* senza aprire una chat
(vault_search). Entrambe le cose chiedono funzioni pure su percorsi, testabili
senza server ne' modello: stanno qui.
"""

from __future__ import annotations

from .atomic import write_text as atomic_write_text

import json
import os
from dataclasses import dataclass, fields, replace
from datetime import date
from pathlib import Path

# L'identita' del vault, nella sua radice. Col punto davanti: non compare negli
# elenchi dell'agente e non sporca la cartella dell'utente.
VAULT_FILE = ".vault.json"

# Tetti sui due testi. La descrizione e' una riga d'elenco; le istruzioni
# entrano nel prefisso di ogni turno del vault, quindi si pagano ad ogni passo
# -- e' lo stesso motivo per cui le note di lavoro sono corte.
MAX_DESCRIZIONE_CHARS = 400
MAX_ISTRUZIONI_CHARS = 2_000

# La memoria del vault. Tre livelli, e la differenza e' la **durata**:
#
# * ``core/notes.py`` -- il foglio della conversazione. Muore con il compito:
#   "il bug era un off-by-one nel parser" fra due settimane non serve a nessuno.
# * qui -- quello che si e' capito **di questo posto**. Vale per tutte le chat
#   del vault e non muore con nessuna di esse: dove stanno le fonti, quale
#   convenzione si e' concordata, cosa e' gia' stato scartato e perche'.
# * ``core/memory.py`` -- i fatti stabili dell'utente, buoni ovunque.
#
# Il tetto e' piu' alto di quello delle note di chat (20) e piu' basso di
# quello delle memorie globali (60): la memoria del vault si accumula per mesi,
# ma viene rispedita ad **ogni passo di ogni chat** del vault, quindi e' il
# posto dove un tetto largo si paga tutti i giorni.
MAX_NOTE_VAULT = 40
MAX_NOTA_VAULT_CHARS = 240

# Nomi delle cartelle canoniche dello schema LLM Wiki.
RAW_DIR = "raw"
WIKI_DIR = "wiki"
ASSETS_DIR = "raw/assets"

# Lo schema vive come CLAUDE.md nella radice del vault: e' il nome che gli
# agent-cli (Claude Code, Codex...) si aspettano, e Obsidian lo ignora.
SCHEMA_FILE = "CLAUDE.md"

INDEX_FILE = f"{WIKI_DIR}/index.md"
LOG_FILE = f"{WIKI_DIR}/log.md"


@dataclass(frozen=True)
class VaultConfig:
    """L'identita' di un vault, come sta scritta in ``.vault.json``."""

    nome: str
    descrizione: str = ""
    # Le sole che arrivano al modello. Vedi il docstring del modulo per il
    # perche' non sono lo stesso campo della descrizione.
    istruzioni: str = ""
    wiki: bool = False
    # La memoria del vault: quello che l'agente ha imparato **qui**, valido
    # per tutte le chat di questa cartella. Vedi ``MAX_NOTE_VAULT``.
    note: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "nome": self.nome,
            "descrizione": self.descrizione,
            "istruzioni": self.istruzioni,
            "wiki": self.wiki,
            "note": list(self.note),
        }


@dataclass(frozen=True)
class VaultInfo:
    """Cosa serve all'UI su un vault registrato: identita' piu' conteggi."""

    path: str
    nome: str
    descrizione: str = ""
    istruzioni: str = ""
    wiki: bool = False
    # Conteggi volutamente economici: contano i file, non li leggono.
    fonti: int = 0
    pagine: int = 0
    chat: int = 0
    note: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "nome": self.nome,
            "descrizione": self.descrizione,
            "istruzioni": self.istruzioni,
            "wiki": self.wiki,
            "fonti": self.fonti,
            "pagine": self.pagine,
            "chat": self.chat,
            "note": list(self.note),
        }


def ha_struttura_wiki(workspace: str | Path) -> bool:
    """True se il percorso ha la forma di una wiki LLM: ``wiki/`` piu' ``raw/``.

    Sono i due livelli che distinguono una wiki mantenuta da una raccolta di
    appunti qualsiasi. Serve ancora per due cose: accendere ``wiki`` di serie
    quando si registra una cartella che gia' lo era, e riconoscere i vault
    nati prima di ``.vault.json``.
    """
    base = Path(workspace)
    return (base / WIKI_DIR).is_dir() and (base / RAW_DIR).is_dir()


# Il nome storico. Ha smesso di voler dire "e' un vault" nel momento in cui un
# vault e' diventato una cartella con un nome: adesso vuol dire "ha la forma
# della wiki", che e' cio' che ha sempre davvero controllato.
is_vault = ha_struttura_wiki


def percorso_config(workspace: str | Path) -> Path:
    return Path(workspace) / VAULT_FILE


def is_registrato(workspace: str | Path) -> bool:
    """True se la cartella si dichiara un vault, cioe' ha il suo ``.vault.json``."""
    return percorso_config(workspace).is_file()


def leggi_config(workspace: str | Path) -> VaultConfig:
    """L'identita' del vault. Non fallisce mai: senza file, i valori di serie.

    Il nome di serie e' quello della cartella, e ``wiki`` si accende da solo
    sui vault che ne hanno la struttura: e' cio' che fa funzionare senza
    migrazioni i vault nati prima di ``.vault.json``.
    """
    base = Path(workspace)
    grezzo: dict[str, object] = {}
    try:
        with open(percorso_config(base), encoding="utf-8") as fh:
            letto = json.load(fh)
        if isinstance(letto, dict):
            grezzo = letto
    except (OSError, UnicodeError, ValueError, RecursionError):
        grezzo = {}
    return VaultConfig(
        nome=str(grezzo.get("nome") or base.name or "vault"),
        descrizione=str(grezzo.get("descrizione") or "")[:MAX_DESCRIZIONE_CHARS],
        istruzioni=str(grezzo.get("istruzioni") or "")[:MAX_ISTRUZIONI_CHARS],
        # ``ha_struttura_wiki`` costa due ``is_dir()`` e ``leggi_config`` gira
        # quattro volte per turno: si valuta solo quando serve davvero, cioe'
        # quando la chiave manca. E si tratta ``null`` come "manca": con
        # ``get(chiave, ripiego)`` un ``"wiki": null`` scritto a mano ritornava
        # None -- la chiave c'e' -- e il ripiego sulla struttura non scattava
        # su una cartella che aveva raw/ e wiki/.
        wiki=bool(
            grezzo["wiki"]
            if grezzo.get("wiki") is not None
            else ha_struttura_wiki(base)
        ),
        note=_ripulisci_note(grezzo.get("note")),
    )


def _attributo(valore: str) -> str:
    """Il nome del vault reso innocuo dentro un attributo dei blocchi in coda.

    I blocchi che il modello legge sono pseudo-XML, e il nome del vault e'
    testo scritto dall'utente: uno che contiene una virgoletta chiude
    l'attributo, e da li' in poi quello che segue viene letto come marcatura.
    Non e' una falla di sicurezza -- e' l'utente che nomina la propria
    cartella -- ma e' un blocco che dice una cosa diversa da quella che
    intendeva, ed e' il genere di cosa su cui il modello poi agisce.
    """
    return (
        str(valore or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _taglia_nota(testo: str) -> str:
    """Il taglio di una nota di vault, con il marcatore. Un posto solo."""
    if len(testo) <= MAX_NOTA_VAULT_CHARS:
        return testo
    return testo[: MAX_NOTA_VAULT_CHARS - 1].rstrip() + "…"


def _ripulisci_note(grezze: object) -> tuple[str, ...]:
    """Note valide, senza doppioni, entro i tetti. Non solleva mai."""
    fuori: list[str] = []
    if not isinstance(grezze, list):
        return ()
    for voce in grezze:
        # Stesso taglio di ``aggiungi_nota``, marcatore compreso: prima uno
        # tagliava a MAX-1 mettendo "…" e l'altro a MAX senza niente, cosi' la
        # stessa nota cambiava di un carattere a ogni giro di lettura e
        # riscrittura -- e non sembrava piu' un doppione di se stessa.
        testo = _taglia_nota(" ".join(str(voce or "").split()))
        if testo and testo not in fuori:
            fuori.append(testo)
    return tuple(fuori[:MAX_NOTE_VAULT])


def scrivi_config(workspace: str | Path, config: VaultConfig) -> VaultConfig:
    """Salva ``.vault.json``, con scrittura atomica come le altre preferenze."""
    base = Path(workspace)
    pulito = replace(
        config,
        nome=" ".join(str(config.nome or "").split())[:120] or base.name,
        descrizione=str(config.descrizione or "").strip()[:MAX_DESCRIZIONE_CHARS],
        istruzioni=str(config.istruzioni or "").strip()[:MAX_ISTRUZIONI_CHARS],
        wiki=bool(config.wiki),
        note=_ripulisci_note(list(config.note)),
    )
    try:
        atomic_write_text(percorso_config(base), json.dumps(pulito.as_dict(), indent=2, ensure_ascii=False))
    except OSError as errore:
        # Non si puo' ritornare ``pulito`` come se fosse stato salvato. Chi
        # chiama e' ``aggiungi_nota``, che risponde al modello con l'elenco
        # delle note aggiornato: il modello legge "registrata", ci costruisce
        # sopra il resto del turno, e la nota non esiste. Una frase che
        # descrive un comportamento dell'harness deve essere vera, e qui la
        # frase e' un valore di ritorno.
        raise VaultScritturaError(
            f"Non ho potuto salvare '{percorso_config(base)}': {errore}"
        ) from errore
    return pulito


# I nomi dei campi di ``VaultConfig``, presi dalla dataclass e non scritti a
# mano: e' il filtro di ``aggiorna_config``, e una copia scritta qui sotto
# resterebbe indietro alla prima aggiunta di un campo.
_CAMPI_CONFIG = frozenset(f.name for f in fields(VaultConfig))


def aggiorna_config(workspace: str | Path, **campi: object) -> VaultConfig:
    """Cambia solo i campi passati. Gli altri restano quelli che erano.

    Il filtro guarda i **campi**, non ``hasattr``: quest'ultimo dice di si'
    anche per i metodi, quindi ``aggiorna_config(ws, as_dict="x")`` arrivava
    fino a ``replace()`` e sollevava un ``TypeError`` grezzo invece di essere
    ignorato come qualunque altra chiave sconosciuta.
    """
    corrente = leggi_config(workspace)
    noti = {k: v for k, v in campi.items() if v is not None and k in _CAMPI_CONFIG}
    return scrivi_config(workspace, replace(corrente, **noti))


def scaffold(base: Path) -> list[str]:
    """Crea la struttura minima del vault; ritorna cio' che ha creato.

    Non sovrascrive mai nulla di esistente: su un vault gia' avviato e' una
    no-op completa. Lo schema di parte (CLAUDE.md, index.md) viene scritto
    solo se assente, cosi' le convenzioni co-evolute con l'utente sopravvivono.

    Non solleva: torna cio' che e' riuscito a creare. Su una cartella di sola
    lettura -- un vault su un disco esterno smontato, una cartella di rete
    caduta -- l'eccezione risaliva fino alla rotta che apre il vault, e
    l'utente vedeva un 500 al posto di una wiki incompleta ma leggibile. Il
    resto del modulo e' scritto per non fallire mai; questa funzione era
    l'eccezione, in tutti i sensi.
    """
    creati: list[str] = []
    try:
        for rel in (RAW_DIR, ASSETS_DIR, WIKI_DIR):
            target = base / rel
            if not target.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                creati.append(rel)
        for nome, contenuto in (
            (SCHEMA_FILE, SCHEMA_DEFAULT),
            (INDEX_FILE, INDEX_DEFAULT),
            (LOG_FILE, ""),
        ):
            target = base / nome
            if not target.exists():
                target.write_text(contenuto, encoding="utf-8")
                creati.append(nome)
    except OSError:
        return creati
    return creati


def ensure_vault(workspace: str | Path, *, nome: str = "") -> Path:
    """Rende vault il workspace dato e ne ritorna la radice assoluta.

    E' il punto unico in cui una cartella diventa un vault: scrive
    ``.vault.json`` se manca, e **solo se il vault e' una wiki** monta anche la
    struttura ``raw/`` + ``wiki/``. Una cartella qualsiasi registrata come
    vault non si ritrova due cartelle che non ha chiesto -- era il difetto del
    vecchio ``ensure_vault``, che dava per scontato che vault e wiki fossero la
    stessa cosa.

    Idempotente: su un vault gia' avviato non tocca niente.
    """
    base = Path(workspace).expanduser().resolve()
    if not base.is_dir():
        raise NotADirectoryError(f"'{base}' non e' una cartella esistente.")
    if not is_registrato(base):
        scrivi_config(
            base,
            VaultConfig(nome=nome or base.name, wiki=ha_struttura_wiki(base)),
        )
    if leggi_config(base).wiki:
        scaffold(base)
    return base


def abilita_wiki(workspace: str | Path) -> VaultConfig:
    """Accende la modalita' manutentore e crea la struttura che le serve.

    Le due cose stanno insieme di proposito: accendere il flag senza ``wiki/``
    e ``raw/`` darebbe un manutentore che fallisce alla prima ingest, e la
    frase del prompt che promette i tre livelli sarebbe falsa.
    """
    base = Path(workspace)
    scaffold(base)
    return aggiorna_config(base, wiki=True)


def conta_file(
    cartella: Path, suffisso: str = ".md", *, escludi: tuple[str, ...] = ()
) -> int:
    """Numero di file con quel suffisso sotto una cartella, ricorsivo.

    ``suffisso=""`` conta tutti i file: e' quello che serve per ``raw/``, dove
    le fonti sono PDF, txt, epub e trascrizioni. Contando solo i ``.md`` un
    vault con duecento paper mostrava "0 fonti".

    ``escludi`` sono nomi di sottocartelle da saltare -- ``assets`` in ``raw/``,
    che per struttura del vault e' il posto delle immagini di corredo e non
    delle fonti.
    """
    if not cartella.is_dir():
        return 0
    modello = f"*{suffisso}" if suffisso else "*"
    return sum(
        1
        for p in cartella.rglob(modello)
        if p.is_file()
        and not p.name.startswith(".")
        and not set(p.relative_to(cartella).parts[:-1]) & set(escludi)
    )


def info_vault(path: str, nome: str = "", *, chat: int = 0) -> VaultInfo:
    """Identita' piu' statistiche economiche: due conti di directory listing.

    ``nome`` resta accettato come ripiego per i vault del vecchio registro che
    non hanno ancora un ``.vault.json``; quando il file c'e', vince lui.
    """
    base = Path(path)
    config = leggi_config(base)
    if nome and not is_registrato(base):
        config = replace(config, nome=nome)
    return VaultInfo(
        path=path,
        nome=config.nome,
        descrizione=config.descrizione,
        istruzioni=config.istruzioni,
        wiki=config.wiki,
        # In ``raw/`` si contano tutti i file, non i soli .md: le fonti sono
        # paper, PDF e trascrizioni, e contando solo i markdown un vault con
        # duecento paper mostrava "0 fonti". Fuori restano gli ``assets``, che
        # per struttura del vault sono le immagini di corredo. In ``wiki/``
        # solo i .md, che sono le pagine -- li' un allegato non e' una pagina.
        fonti=(
            conta_file(base / RAW_DIR, "", escludi=(Path(ASSETS_DIR).name,))
            if config.wiki
            else 0
        ),
        pagine=conta_file(base / WIKI_DIR) if config.wiki else 0,
        chat=chat,
        note=config.note,
    )


def leggi_indice(workspace: str | Path, max_chars: int = 6_000) -> str:
    """Contenuto di ``wiki/index.md`` per il prompt del cercatore.

    L'indice e' la mappa della wiki: leggerlo prima di cercare e' esattamente
    il flusso che lo schema LLM Wiki prescrive, ed evita di far girare la
    ricerca a vuoto su una domanda che una pagina gia' copre.
    """
    indice = Path(workspace) / INDEX_FILE
    try:
        with indice.open(encoding="utf-8", errors="replace") as stream:
            testo = stream.read(max(0, max_chars) + 1)
    except OSError:
        return ""
    if len(testo) > max_chars:
        taglio = testo[:max_chars]
        # Si tronca sull'ultima riga intera: un indice mozzato a meta' riga
        # farebbe credere a una pagina che non c'e'.
        #
        # ...ma solo se una riga intera c'e'. Con ``rfind`` a -1 -- nessun a
        # capo nei primi ``max_chars`` caratteri, che e' il caso di un indice
        # scritto su una riga sola -- il ``+1`` faceva ``taglio[:0]`` e
        # l'indice spariva del tutto, lasciando la sola dicitura "troncato".
        ultimo_a_capo = taglio.rfind("\n")
        if ultimo_a_capo > 0:
            taglio = taglio[: ultimo_a_capo + 1]
        return taglio + "\n[indice troncato]"
    return testo


def leggi_log_coda(workspace: str | Path, voci: int = 10) -> str:
    """Ultime voci di ``wiki/log.md``: cosa e' stato fatto di recente.

    Le voci cominciano tutte con ``## [data]``, quindi la coda si prende con
    un filtro di prefisso invece di rileggere tutto il log.
    """
    log = Path(workspace) / LOG_FILE
    try:
        with log.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 65536))
            righe = stream.read(65536).decode("utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    marcatori = [r for r in righe if r.startswith("## [")]
    if not marcatori:
        return ""
    return "\n".join(marcatori[-voci:])


def voce_log(operazione: str, soggetto: str) -> str:
    """Una riga di log nel formato parseabile dello schema."""
    oggi = date.today().isoformat()
    return f"## [{oggi}] {operazione} | {soggetto}\n"


def appendi_log(workspace: str | Path, operazione: str, soggetto: str) -> bool:
    """Aggiunge una voce in coda a ``wiki/log.md``. Append-only, come da schema.

    Ritorna False se non ha potuto scrivere -- cartella ``wiki/`` assente,
    disco pieno -- invece di sollevare: e' un'annotazione, e far fallire
    un'operazione riuscita perche' non si e' potuto annotarla sarebbe peggio
    del non annotarla. Prima sollevava ``FileNotFoundError`` su un vault senza
    ``wiki/``, e il ``try`` interno proteggeva solo la rilettura.
    """
    log = Path(workspace) / LOG_FILE
    try:
        coda = ""
        if log.exists():
            # Solo gli ultimi due byte: prima si rileggeva l'INTERO file per
            # guardarli, su un log append-only che cresce per mesi.
            with open(log, "rb") as fh:
                try:
                    fh.seek(-2, os.SEEK_END)
                except OSError:      # file piu' corto di due byte
                    fh.seek(0)
                coda = fh.read().decode("utf-8", errors="replace")
        with open(log, "a", encoding="utf-8") as fh:
            # Il log cresce per blocchi: una riga vuota separa le voci, ma solo
            # se il file non finisce gia' con la separazione giusta.
            if coda and not coda.endswith("\n\n"):
                fh.write("\n" if coda.endswith("\n") else "\n\n")
            fh.write(voce_log(operazione, soggetto))
    except OSError:
        return False
    return True


# --- testi di parte -------------------------------------------------------
# Sono il punto di partenza, non la verita': lo schema dice esplicitamente di
# co-evolverlo. Per questo vengono scritti solo se assenti.


SCHEMA_DEFAULT = """\
# Schema del vault (LLM Wiki)

Questo workspace e' un vault secondo il pattern LLM Wiki: tre livelli.

- `raw/` -- fonti immutabili. Leggi, non modificare mai.
- `wiki/` -- pagine markdown che TU scrivi e mantieni: sintesi, entita',
  concetti, confronti. Questo livello e' tuo per intero.
- `wiki/index.md` -- catalogo per categoria: ogni pagina con link e una riga
  di riassunto. Aggiornalo a ogni ingest.
- `wiki/log.md` -- cronologia append-only. Una voce per operazione, col
  formato `## [YYYY-MM-DD] ingest|query|lint | soggetto`.

# Operazioni

**Ingest** -- l'utente deposita una fonte in `raw/` e te lo segnala: leggila,
discuti i punti chiave, scrivi/aggiorna le pagine toccate, aggiorna l'indice,
aggiungi una voce di log. Una fonte puo' toccare 10-15 pagine: va bene.

**Query** -- rispondi partendo dall'indice, poi approfondisci le pagine.
Le risposte buone diventano pagine nuove: non lasciare le analisi nella chat.

**Lint** -- a richiesta: contraddizioni fra pagine, affermazioni superate,
orfani senza link in entrata, concetti citati ma senza pagina, cross-reference
mancanti.

# Regole ferree

1. `raw/` e' sacro: mai una scrittura dentro.
2. Ogni pagina nuova entra nell'indice e riceve i backlink dalle pagine che
   la citano.
3. Quando una fonte nuova contraddice una pagina esistente, correggi la
   pagina e annota la contraddizione nel log -- non lasciarle convivere.
4. Link in stile Obsidian `[[Pagina]]`: la graph view e' parte del prodotto.
"""

INDEX_DEFAULT = """\
# Indice della wiki

Catalogo di tutte le pagine, per categoria. Una riga per pagina: link,
riassunto in una frase, fonte principale. Aggiornato a ogni ingest.

## Fonti

_(vuota: deposita la prima fonte in `raw/`)_

## Entita'

_(vuota)_

## Concetti

_(vuota)_
"""


# ------------------------------------------------------------------ #


VAULT_SYSTEM_PROMPT = """\
Sei il manutentore della wiki di un vault LLM Wiki: il workspace in cui \
lavori e' organizzato secondo lo schema di Karpathy.

## I tre strati
- ``raw/`` -- le fonti dell'utente: articoli, paper, note grezze. Sono \
IMMUTABILI: leggile, mai modificarle ne' cancellarle.
- ``wiki/`` -- la wiki che scrivi e mantieni tu: pagine di sintesi, pagine \
di entita' e concetti, confronti, overview. La cross-referenza e' tua.
- ``CLAUDE.md`` -- lo schema del vault: convenzioni sui nomi, formato delle \
pagine, flussi concordati con l'utente. Leggilo all'inizio di ogni sessione \
e rispettalo; se l'utente cambia una regola, aggiornalo tu.

## Le tue operazioni
- **ingest**: l'utente deposita una fonte in ``raw/`` e te la segnala. \
Leggila, discuti i punti chiave, poi aggiorna TUTTO il necessario: pagina \
di sintesi della fonte, pagine di entita'/concetti toccate, [[link]] in \
entrata e uscita, ``wiki/index.md``, voce in coda a ``wiki/log.md`` col \
prefisso ``## [data] ingest | soggetto``. Una fonte puo' toccare 10-15 \
pagine: fallo senza lamentarti, il librokeeping e' il tuo mestiere.
- **query**: l'utente fa domande sulla wiki. Parti da ``wiki/index.md``, \
apri le pagine serventi, rispondi citando i percorsi. Se la risposta ha \
valore durevole (un confronto, un'analisi), proponi di archiviarla come \
nuova pagina wiki e, se l'utente conferma, falla subito.
- **lint**: controllo di salute su richiesta. Cerca contraddizioni fra \
pagine, affermazioni superate da fonti piu' recenti, orfani senza link in \
entrata, concetti citati ma privi di pagina, cross-reference mancanti, \
dati lacunosi che una ricerca web colmerebbe. Proponi correzioni e nuove \
domande da investigare.

## Regole ferree
- Ogni pagina creata entra in ``wiki/index.md`` con link e one-liner, e \
viene raggiunta da almeno un link da un'altra pagina.
- Quando una nuova fonte contraddice una claim esistente, non cancellare \
in silenzio: segnala il conflitto all'utente e annota entrambe le versioni \
con le date.
- Ogni operazione conclusa si chiude con una riga in ``wiki/log.md``.
- Non toccare nulla fuori dal vault: ``raw/`` si legge, il resto del mondo \
non esiste.
- L'utente cura le fonti e fa le domande giuste; tutto il resto -- riassunti, \
link, indici, log, coerenza -- e' responsabilita' tua.\
"""


def is_modalita_vault(workspace: str | Path) -> bool:
    """True se qui l'agente deve fare il manutentore della wiki.

    Non e' piu' "sto dentro un vault": un vault puo' essere un progetto di
    codice, e li' il prompt del bibliotecario sarebbe fuori posto. E' il flag
    ``wiki`` del vault -- che su una cartella senza ``.vault.json`` ricade
    sulla struttura, quindi i vault di prima continuano a comportarsi uguale.
    """
    return leggi_config(workspace).wiki


class NotaVaultError(ValueError):
    """Uso non valido della memoria del vault."""


class VaultScritturaError(OSError):
    """``.vault.json`` non e' stato salvato.

    Esiste perche' il silenzio era la modalita' di guasto peggiore: la funzione
    ritornava la configurazione nuova anche quando il disco aveva ancora quella
    vecchia, e il modello riceveva "nota registrata" su una nota che non c'era.
    """


def aggiungi_nota(workspace: str | Path, testo: str) -> VaultConfig:
    """Una riga in piu' nella memoria del vault. Idempotente sui doppioni."""
    pulito = " ".join(str(testo or "").split())
    if not pulito:
        raise NotaVaultError("La nota e' vuota.")
    pulito = _taglia_nota(pulito)
    corrente = leggi_config(workspace)
    if pulito in corrente.note:
        # Riscrivere la stessa nota e' un sintomo di un modello che gira a
        # vuoto: non lo si premia con una riga in piu'.
        return corrente
    if len(corrente.note) >= MAX_NOTE_VAULT:
        raise NotaVaultError(
            f"La memoria del vault e' piena ({MAX_NOTE_VAULT} note). Togli "
            "quelle superate con action='remove' prima di aggiungerne altre."
        )
    return scrivi_config(workspace, replace(corrente, note=(*corrente.note, pulito)))


def togli_nota(workspace: str | Path, riferimento: str) -> VaultConfig:
    """Toglie una nota citandola per testo, anche solo per il suo inizio.

    Per prefisso e non solo esatta: un modello che cita una nota a memoria la
    tronca, e rifiutare per una virgola mancante costa un giro per niente.
    """
    corrente = leggi_config(workspace)
    ago = " ".join(str(riferimento or "").split())
    if not ago:
        raise NotaVaultError("Serve la nota da togliere.")
    candidate = [n for n in corrente.note if n == ago] or [
        n for n in corrente.note if n.startswith(ago[:40])
    ]
    if not candidate:
        raise NotaVaultError(f"Nessuna nota corrisponde a '{riferimento}'.")
    if len(candidate) > 1:
        # "Nessuna corrisponde" era falso e mandava il modello nella direzione
        # sbagliata: cambiava testo invece di essere piu' preciso, e girava a
        # vuoto. Qui gli si dice il vero problema e gli si danno gli inizi da
        # cui scegliere.
        inizi = "; ".join(f"'{n[:50]}...'" for n in candidate[:4])
        raise NotaVaultError(
            f"'{riferimento}' corrisponde a {len(candidate)} note: {inizi}. "
            "Cita piu' testo per dire quale."
        )
    tenute = tuple(n for n in corrente.note if n != candidate[0])
    return scrivi_config(workspace, replace(corrente, note=tenute))


def blocco_note(config: VaultConfig) -> str:
    """La memoria del vault, da mettere **in coda** come il piano e le note.

    Stessa ragione di sempre: il prefisso resta byte-identico fra un passo e
    l'altro, ed e' cio' che rende riusabile il KV cache.
    """
    if not config.note:
        return ""
    righe = [f"- {n}" for n in config.note]
    return "\n".join(
        [
            f'<memoria_del_vault nome="{_attributo(config.nome)}">',
            *righe,
            "</memoria_del_vault>",
            "",
            "Cose imparate lavorando in questa cartella, in conversazioni "
            "anche molto piu' vecchie di questa. Valgono come se le avessi "
            "appena lette. Se ne scopri una nuova che varra' ancora fra un "
            "mese, scrivila qui con manage_notes ambito='vault' -- il foglio "
            "di questa chat invece muore con lei.",
        ]
    )


def blocco_istruzioni(config: VaultConfig) -> str:
    """Le istruzioni del vault, per il prompt di sistema.

    Solo ``istruzioni``: la ``descrizione`` serve a ritrovare la cartella
    nell'elenco e non deve diventare un ordine permanente per l'agente.
    """
    testo = (config.istruzioni or "").strip()
    if not testo:
        return ""
    return (
        f'\n\n<vault nome="{_attributo(config.nome)}">\n'
        "Istruzioni scritte dall'utente per il lavoro in questa cartella. "
        "Valgono per tutta la conversazione.\n"
        f"{testo}\n"
        "</vault>"
    )


def blocco_struttura(workspace: str | Path) -> str:
    """La parte del contesto vault che NON cambia fra un passo e l'altro.

    Solo percorsi e nomi di cartella: due ingest di fila producono lo stesso
    testo identico, quindi puo' stare nel prompt di sistema senza rompere il
    prefisso. Lo *stato* -- indice e log -- sta in ``blocco_stato``, che va in
    coda come il piano e le note.
    """
    return "\n".join(
        [
            "\n\n## Struttura del vault\n",
            f"Workspace: `{Path(workspace).resolve()}`.",
            "`raw/` (fonti, immutabili), `wiki/` (la wiki, tua), "
            f"`{SCHEMA_FILE}` (lo schema).",
            "",
            f"All'inizio di una nuova sessione rileggi `{SCHEMA_FILE}`: "
            "le convenzioni possono essere cambiate dall'ultima volta.",
        ]
    )


def blocco_stato(workspace: str | Path) -> str:
    """Indice e coda del log, da mettere **in coda** con ruolo ``user``.

    Stesso posto e stessa ragione del piano, delle note e della memoria del
    vault: il prefisso deve restare byte-identico fra un passo e l'altro, ed e'
    cio' che rende riusabile il KV cache.

    Prima questo blocco stava nel prompt di **sistema** (misurato: 6.682
    caratteri, circa 1.670 token) e conteneva ``wiki/index.md`` -- il file che
    ogni ingest riscrive. Il prefisso si invalidava sull'operazione per cui la
    modalita' wiki esiste, e non di 1.670 token: dal token zero, perche' quello
    che segue un prefisso cambiato va tutto ricalcolato.
    """
    indice = leggi_indice(workspace)
    coda = leggi_log_coda(workspace)
    if not indice.strip() and not coda.strip():
        return ""
    return "\n".join(
        [
            "<stato_del_vault>",
            "### Indice attuale della wiki (`wiki/index.md`)",
            indice if indice.strip() else "_(indice vuoto o illeggibile)_",
            "",
            "### Ultime operazioni (`wiki/log.md`)",
            coda if coda.strip() else "_(log vuoto: nessuna operazione registrata)_",
            "</stato_del_vault>",
        ]
    )


def blocco_manutenzione(workspace: str | Path) -> str:
    """Nome storico: struttura piu' stato, tutto insieme.

    Resta per chi lo chiamava, ma **non va nel prompt di sistema**: e' la somma
    di un pezzo fermo e di uno che cambia a ogni ingest, e mescolarli e' cio'
    che rompeva il prefisso. Chi costruisce il prompt usi ``blocco_struttura``
    in testa e ``blocco_stato`` in coda.
    """
    stato = blocco_stato(workspace)
    return blocco_struttura(workspace) + (("\n\n" + stato) if stato else "")
