"""Vault LLM Wiki: un workspace organizzato secondo lo schema di Karpathy.

## Cos'e' un vault

Un workspace che contiene una wiki personale mantenuta dall'agente, sul
modello del pattern "LLM Wiki": tre livelli ben separati.

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

from dataclasses import dataclass
from datetime import date
from pathlib import Path

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
class VaultInfo:
    """Cosa serve all'UI e al tool vault_search su un vault registrato."""

    path: str
    nome: str
    # Conteggi volutamente economici: contano i file, non li leggono.
    fonti: int = 0
    pagine: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "nome": self.nome,
            "fonti": self.fonti,
            "pagine": self.pagine,
        }


def is_vault(workspace: str | Path) -> bool:
    """True se il percorso ha la forma minima di un vault.

    La forma minima e' ``wiki/`` piu' ``raw/``: sono i due livelli che
    distinguono una wiki mantenuta da una raccolta di appunti qualsiasi.
    Chi vuole puo' partire anche da zero -- ``scaffold`` li crea -- ma un
    vault *esistente* si riconosce dalla struttura, non da un flag.
    """
    base = Path(workspace)
    return (base / WIKI_DIR).is_dir() and (base / RAW_DIR).is_dir()


def scaffold(base: Path) -> list[str]:
    """Crea la struttura minima del vault; ritorna cio' che ha creato.

    Non sovrascrive mai nulla di esistente: su un vault gia' avviato e' una
    no-op completa. Lo schema di parte (CLAUDE.md, index.md) viene scritto
    solo se assente, cosi' le convenzioni co-evolute con l'utente sopravvivono.
    """
    creati: list[str] = []
    for rel in (RAW_DIR, ASSETS_DIR, WIKI_DIR):
        target = base / rel
        if not target.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            creati.append(rel)
    schema = base / SCHEMA_FILE
    if not schema.exists():
        schema.write_text(SCHEMA_DEFAULT, encoding="utf-8")
        creati.append(SCHEMA_FILE)
    indice = base / INDEX_FILE
    if not indice.exists():
        indice.write_text(INDEX_DEFAULT, encoding="utf-8")
        creati.append(INDEX_FILE)
    log = base / LOG_FILE
    if not log.exists():
        log.write_text("", encoding="utf-8")
        creati.append(LOG_FILE)
    return creati


def ensure_vault(workspace: str | Path) -> Path:
    """Rende vault il workspace dato e ne ritorna la radice assoluta.

    E' il punto unico in cui un percorso diventa un vault: il server lo chiama
    quando l'utente apre o registra una cartella come vault, cosi' anche una
    cartella appena creata parte con la struttura giusta invece di fallire
    alla prima ingest perche' ``wiki/`` non c'era.
    """
    base = Path(workspace).expanduser().resolve()
    if not base.is_dir():
        raise NotADirectoryError(f"'{base}' non e' una cartella esistente.")
    scaffold(base)
    return base


def conta_file(cartella: Path, suffisso: str = ".md") -> int:
    """Numero di file con quel suffisso sotto una cartella, ricorsivo."""
    if not cartella.is_dir():
        return 0
    return sum(1 for p in cartella.rglob(f"*{suffisso}") if p.is_file())


def info_vault(path: str, nome: str = "") -> VaultInfo:
    """Statistiche economiche su un vault: due conti di directory listing."""
    base = Path(path)
    return VaultInfo(
        path=path,
        nome=nome or base.name,
        fonti=conta_file(base / RAW_DIR),
        pagine=conta_file(base / WIKI_DIR),
    )


def leggi_indice(workspace: str | Path, max_chars: int = 6_000) -> str:
    """Contenuto di ``wiki/index.md`` per il prompt del cercatore.

    L'indice e' la mappa della wiki: leggerlo prima di cercare e' esattamente
    il flusso che lo schema LLM Wiki prescrive, ed evita di far girare la
    ricerca a vuoto su una domanda che una pagina gia' copre.
    """
    indice = Path(workspace) / INDEX_FILE
    try:
        testo = indice.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(testo) > max_chars:
        taglio = testo[:max_chars]
        # Si tronca sull'ultima riga intera: un indice mozzato a meta' riga
        # farebbe credere a una pagina che non c'e'.
        taglio = taglio[: taglio.rfind("\n") + 1]
        return taglio + "\n[indice troncato]"
    return testo


def leggi_log_coda(workspace: str | Path, voci: int = 10) -> str:
    """Ultime voci di ``wiki/log.md``: cosa e' stato fatto di recente.

    Le voci cominciano tutte con ``## [data]``, quindi la coda si prende con
    un filtro di prefisso invece di rileggere tutto il log.
    """
    log = Path(workspace) / LOG_FILE
    try:
        righe = log.read_text(encoding="utf-8", errors="replace").splitlines()
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


def appendi_log(workspace: str | Path, operazione: str, soggetto: str) -> None:
    """Aggiunge una voce in coda a ``wiki/log.md``. Append-only, come da schema."""
    log = Path(workspace) / LOG_FILE
    esiste = log.exists()
    with open(log, "a", encoding="utf-8") as fh:
        if esiste:
            # Il log cresce per blocchi: una riga vuota separa le voci, ma
            # solo se il file non finisce gia' con la separazione giusta.
            try:
                coda = log.read_text(encoding="utf-8")[-2:]
                if coda and not coda.endswith("\n\n"):
                    fh.write("\n" if coda.endswith("\n") else "\n\n")
            except OSError:
                pass
        fh.write(voce_log(operazione, soggetto))


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
    """True se la sessione sta girando dentro un vault riconoscibile."""
    return is_vault(workspace)


def blocco_manutenzione(workspace: str | Path) -> str:
    """Blocco di contesto vault per il prompt di sistema del manutentore.

    Due letture economiche (indice e coda del log): danno al manutentore
    lo stato corrente della wiki senza scandire l'albero ad ogni turno.
    """
    indice = leggi_indice(workspace)
    coda = leggi_log_coda(workspace)
    parti = [
        "\n\n## Stato del vault\n",
        f"Workspace: `{Path(workspace).resolve()}`.",
        "Struttura attesa: `raw/` (fonti, immutabili), `wiki/` (la wiki, tua), "
        f"`{SCHEMA_FILE}` (lo schema).",
        "",
        "### Indice attuale della wiki (`wiki/index.md`)",
        indice,
        "",
        "### Ultime operazioni (`wiki/log.md`)",
        coda if coda.strip() else "_(log vuoto: nessuna operazione registrata)_",
        "",
        "All'inizio di una nuova sessione rileggi anche `" + SCHEMA_FILE + "`: "
        "le convenzioni possono essere cambiate dall'ultima volta.",
    ]
    return "\n".join(parti)
