"""Progetti: una cartella con un nome, le sue conversazioni e una memoria.

## Cos'e' un progetto

Un posto di lavoro a cui si torna. Nome, descrizione, cartella, le chat che ci
sono state fatte dentro e -- la parte che lo rende qualcosa di piu' di una
cartella -- una **memoria** che vale per tutte quelle chat: le decisioni prese,
le convenzioni concordate, le strade gia' scartate, i lavori rimasti aperti.
Una chat nuova del progetto non riparte da zero: parte da li'.

Fino al 26/09/2026 si chiamava "vault" e la memoria c'era gia', ma la scriveva
solo il modello, con un parametro opzionale di ``manage_notes``. Nelle 81
sessioni salvate quel parametro non e' mai stato usato: 25 chiamate a
``manage_notes`` su 2.890, nessuna con ``ambito='vault'``. La memoria non
veniva ignorata, non veniva scritta. Adesso la scrive l'harness a fine turno
(``core/memoria_progetto.py``), il modello puo' ancora aggiungere, e l'utente
la legge, la corregge e ci scrive dalla schermata del progetto.

## Dove vive l'identita'

In ``.progetto.json``, nella radice della cartella -- non nelle impostazioni
dell'harness. Un progetto che si autodescrive sopravvive allo spostamento della
cartella, alla copia su un'altra macchina e alla reinstallazione: il registro
nelle preferenze (``progetti``) tiene solo l'elenco dei percorsi conosciuti,
perche' quello non si puo' ricavare.

I progetti nati come vault hanno ``.vault.json``: si leggono uguale, e al
primo salvataggio diventano ``.progetto.json`` (il file vecchio si toglie, o
la cartella avrebbe due identita' che possono divergere).

Due campi di testo, e non uno, perche' rispondono a due domande diverse:

* ``descrizione`` -- per chi guarda l'elenco: cos'e' questo posto. Non arriva
  mai al modello.
* ``istruzioni`` -- per il modello: come ci si lavora dentro. Entra nel prompt
  di ogni chat del progetto.

Tenerli separati e' una difesa concreta: una riga scritta di fretta per
ritrovare la cartella non deve diventare un'istruzione che l'agente segue in
ogni turno per i mesi successivi.

## La memoria

Voci brevi, ognuna con un **tipo** (decisione, convenzione, fatto, strada
scartata, lavoro aperto), la **chat** da cui viene e l'**autore**: l'harness,
il modello o l'utente. L'autore non e' un dettaglio: una voce scritta o
corretta dall'utente l'harness non la tocca piu' -- ne' la riscrive ne' la
toglie. Chi ha l'ultima parola su cosa e' vero in un progetto e' chi ci lavora.

La memoria va **in coda** al contesto, come il piano e le note (il prefisso
resta byte-identico e il KV cache si riusa), raggruppata per tipo. Il tetto --
40 voci da 240 caratteri -- e' lo stesso della memoria del vault: viene
rispedita a ogni passo di ogni chat del progetto, ed e' il posto in cui un
tetto largo si paga tutti i giorni.

## Il livello wiki (secondario)

Quando ``wiki`` e' acceso, il workspace e' organizzato secondo il pattern "LLM
Wiki" di Karpathy: ``raw/`` (fonti immutabili), ``wiki/`` (pagine mantenute
dall'agente), ``CLAUDE.md`` (lo schema). E' una proprieta' del progetto, non la
sua definizione: la maggior parte dei progetti e' codice.

## Perche' un modulo e non solo convenzioni

Il resto dell'harness deve poter *riconoscere* un progetto dal workspace
corrente e *leggerne* la memoria senza aprire una chat. Tutto questo chiede
funzioni pure su percorsi, testabili senza server ne' modello: stanno qui.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, fields, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .atomic import write_text as atomic_write_text

# L'identita' del progetto, nella sua radice. Col punto davanti: non compare
# negli elenchi dell'agente e non sporca la cartella dell'utente.
PROGETTO_FILE = ".progetto.json"
# Il nome di prima. Si legge ancora; al primo salvataggio sparisce.
VECCHIO_FILE = ".vault.json"
VERSIONE_FILE = 2

# Tetti sui due testi. La descrizione e' una riga d'elenco; le istruzioni
# entrano nel prompt di ogni turno del progetto, quindi si pagano ad ogni passo.
MAX_NOME_CHARS = 120
MAX_DESCRIZIONE_CHARS = 400
MAX_ISTRUZIONI_CHARS = 2_000

# La memoria. Tre fogli nell'harness, e la differenza e' la **durata**:
#
# * ``core/notes.py`` -- il foglio della conversazione. Muore con il compito.
# * qui -- quello che si e' stabilito **in questo progetto**. Vale per tutte le
#   sue chat e non muore con nessuna di esse.
# * ``core/memory.py`` -- i fatti stabili dell'utente, buoni ovunque.
#
# Stesso tetto della memoria del vault (40 x 240): viene rispedita a ogni passo
# di ogni chat del progetto, ed e' li' che un tetto largo si paga.
MAX_VOCI_MEMORIA = 40
MAX_VOCE_CHARS = 240

# I tipi, nell'ordine in cui il blocco li mostra: dal piu' stabile (una
# decisione presa) al piu' volatile (un lavoro aperto, che sparisce appena e'
# fatto). L'ordine conta anche per la cache: quello che cambia piu' spesso sta
# in fondo al blocco.
TIPI_MEMORIA = ("decisione", "convenzione", "fatto", "scartato", "aperto")
TIPO_DI_SERIE = "fatto"
ETICHETTE_TIPO = {
    "decisione": "Decisioni",
    "convenzione": "Convenzioni",
    "fatto": "Fatti",
    "scartato": "Strade scartate",
    "aperto": "Lavori aperti",
}
# Chi ha scritto la voce. ``utente`` vuol dire anche "corretta dall'utente":
# e' l'autore che l'harness non scavalca.
AUTORI = ("harness", "modello", "utente")
AUTORE_PROTETTO = "utente"

# Nomi delle cartelle canoniche dello schema LLM Wiki.
RAW_DIR = "raw"
WIKI_DIR = "wiki"
ASSETS_DIR = "raw/assets"

# Lo schema vive come CLAUDE.md nella radice: e' il nome che gli agent-cli
# (Claude Code, Codex...) si aspettano, e Obsidian lo ignora.
SCHEMA_FILE = "CLAUDE.md"

INDEX_FILE = f"{WIKI_DIR}/index.md"
LOG_FILE = f"{WIKI_DIR}/log.md"


def _adesso() -> str:
    return datetime.now().isoformat(timespec="minutes")


def _pulisci(testo: object) -> str:
    return " ".join(str(testo or "").split())


def _taglia_voce(testo: str) -> str:
    """Il taglio di una voce, con il marcatore. Un posto solo."""
    if len(testo) <= MAX_VOCE_CHARS:
        return testo
    return testo[: MAX_VOCE_CHARS - 1].rstrip() + "…"


def _tipo(valore: object) -> str:
    tipo = str(valore or "").strip().lower()
    # Le forme che un modello scrive davvero: plurali, maschili, l'etichetta.
    sinonimi = {
        "decisioni": "decisione", "convenzioni": "convenzione", "fatti": "fatto",
        "scartata": "scartato", "scartate": "scartato", "strada scartata": "scartato",
        "strade scartate": "scartato", "aperti": "aperto", "aperta": "aperto",
        "lavoro aperto": "aperto", "lavori aperti": "aperto", "da fare": "aperto",
        "todo": "aperto",
    }
    tipo = sinonimi.get(tipo, tipo)
    return tipo if tipo in TIPI_MEMORIA else TIPO_DI_SERIE


def _nuovo_id(esistenti: Iterable[str]) -> str:
    presi = set(esistenti)
    while True:
        candidato = uuid.uuid4().hex[:6]
        if candidato not in presi:
            return candidato


def _id_stabile(testo: str, esistenti: Iterable[str]) -> str:
    """L'id di una nota del vault convertita: uguale a ogni lettura.

    Le note vecchie erano stringhe senza id. Finche' il file non si riscrive
    (una cartella di sola lettura, un progetto mai toccato) la stessa nota
    deve avere lo stesso id a ogni lettura, o una cancellazione dalla
    schermata colpirebbe il nulla.
    """
    presi = set(esistenti)
    base = hashlib.sha1(testo.encode("utf-8")).hexdigest()
    for i in range(0, len(base) - 6):
        candidato = base[i : i + 6]
        if candidato not in presi:
            return candidato
    return _nuovo_id(presi)


@dataclass(frozen=True)
class VoceMemoria:
    """Una cosa stabilita nel progetto, e da dove viene."""

    id: str
    testo: str
    tipo: str = TIPO_DI_SERIE
    autore: str = "harness"
    # La chat da cui viene: id e titolo. Il titolo si copia perche' la chat
    # puo' essere cancellata, e "da dove viene" deve restare leggibile.
    chat: str = ""
    titolo_chat: str = ""
    creata: str = ""
    aggiornata: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "testo": self.testo,
            "tipo": self.tipo,
            "autore": self.autore,
            "chat": self.chat,
            "titolo_chat": self.titolo_chat,
            "creata": self.creata,
            "aggiornata": self.aggiornata,
        }


def _voci_da_grezzo(grezze: object, *, autore_vecchie: str = "modello") -> tuple[VoceMemoria, ...]:
    """Voci valide, senza doppioni, entro i tetti. Non solleva mai.

    Accetta le due forme: gli oggetti di ``.progetto.json`` e le stringhe del
    vecchio ``.vault.json`` (che diventano fatti scritti dal modello, perche'
    era l'unico che poteva scriverle).
    """
    if not isinstance(grezze, list):
        return ()
    fuori: list[VoceMemoria] = []
    testi: set[str] = set()
    ids: set[str] = set()
    for grezza in grezze:
        if isinstance(grezza, str):
            testo = _taglia_voce(_pulisci(grezza))
            if not testo or testo in testi:
                continue
            voce = VoceMemoria(
                id=_id_stabile(testo, ids), testo=testo, tipo=TIPO_DI_SERIE,
                autore=autore_vecchie,
            )
        elif isinstance(grezza, Mapping):
            testo = _taglia_voce(_pulisci(grezza.get("testo")))
            if not testo or testo in testi:
                continue
            ident = str(grezza.get("id") or "").strip()[:12]
            if not ident or ident in ids:
                ident = _id_stabile(testo, ids)
            autore = str(grezza.get("autore") or "").strip().lower()
            voce = VoceMemoria(
                id=ident,
                testo=testo,
                tipo=_tipo(grezza.get("tipo")),
                autore=autore if autore in AUTORI else "harness",
                chat=str(grezza.get("chat") or "")[:64],
                titolo_chat=_pulisci(grezza.get("titolo_chat"))[:120],
                creata=str(grezza.get("creata") or "")[:32],
                aggiornata=str(grezza.get("aggiornata") or "")[:32],
            )
        else:
            continue
        fuori.append(voce)
        testi.add(voce.testo)
        ids.add(voce.id)
        if len(fuori) >= MAX_VOCI_MEMORIA:
            break
    return tuple(fuori)


@dataclass(frozen=True)
class ProgettoConfig:
    """L'identita' di un progetto, come sta scritta in ``.progetto.json``."""

    nome: str
    descrizione: str = ""
    # Le sole che arrivano al modello. Vedi il docstring del modulo.
    istruzioni: str = ""
    wiki: bool = False
    memoria: tuple[VoceMemoria, ...] = ()
    creato: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "versione": VERSIONE_FILE,
            "nome": self.nome,
            "descrizione": self.descrizione,
            "istruzioni": self.istruzioni,
            "wiki": self.wiki,
            "creato": self.creato,
            "memoria": [v.as_dict() for v in self.memoria],
        }

    def voce(self, ident: str) -> VoceMemoria | None:
        return next((v for v in self.memoria if v.id == ident), None)


@dataclass(frozen=True)
class ProgettoInfo:
    """Cosa serve all'UI su un progetto registrato: identita' piu' conteggi."""

    path: str
    nome: str
    descrizione: str = ""
    istruzioni: str = ""
    wiki: bool = False
    # Conteggi volutamente economici: contano i file, non li leggono.
    fonti: int = 0
    pagine: int = 0
    chat: int = 0
    memoria: tuple[VoceMemoria, ...] = ()
    creato: str = ""
    esiste: bool = True

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
            "memoria": [v.as_dict() for v in self.memoria],
            "creato": self.creato,
            "esiste": self.esiste,
        }


def ha_struttura_wiki(workspace: str | Path) -> bool:
    """True se il percorso ha la forma di una wiki LLM: ``wiki/`` piu' ``raw/``.

    Serve per accendere ``wiki`` di serie quando si registra una cartella che
    gia' lo era, e per riconoscere i vault nati prima di ``.vault.json``.
    """
    base = Path(workspace)
    return (base / WIKI_DIR).is_dir() and (base / RAW_DIR).is_dir()


def percorso_config(workspace: str | Path) -> Path:
    return Path(workspace) / PROGETTO_FILE


def percorso_vecchio(workspace: str | Path) -> Path:
    return Path(workspace) / VECCHIO_FILE


def is_registrato(workspace: str | Path) -> bool:
    """True se la cartella si dichiara un progetto (anche col file di prima)."""
    if not str(workspace or "").strip():
        return False
    return percorso_config(workspace).is_file() or percorso_vecchio(workspace).is_file()


def _leggi_grezzo(base: Path) -> tuple[dict[str, Any], bool]:
    """Il contenuto del file d'identita', e se viene dal formato di prima."""
    for percorso, vecchio in ((percorso_config(base), False), (percorso_vecchio(base), True)):
        try:
            with open(percorso, encoding="utf-8") as fh:
                letto = json.load(fh)
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError, ValueError, RecursionError):
            # Un file rotto non deve far sparire il progetto: si ripiega sui
            # valori di serie, come se il file non ci fosse.
            return {}, vecchio
        if isinstance(letto, dict):
            return letto, vecchio
        return {}, vecchio
    return {}, False


def leggi_config(workspace: str | Path) -> ProgettoConfig:
    """L'identita' del progetto. Non fallisce mai: senza file, i valori di serie.

    Il nome di serie e' quello della cartella, e ``wiki`` si accende da solo
    sulle cartelle che ne hanno la struttura: e' cio' che fa funzionare senza
    migrazioni i vault nati prima di ``.vault.json``.
    """
    base = Path(workspace)
    grezzo, vecchio = _leggi_grezzo(base)
    memoria_grezza = grezzo.get("note") if vecchio else grezzo.get("memoria")
    return ProgettoConfig(
        nome=_pulisci(grezzo.get("nome"))[:MAX_NOME_CHARS] or base.name or "progetto",
        descrizione=str(grezzo.get("descrizione") or "")[:MAX_DESCRIZIONE_CHARS],
        istruzioni=str(grezzo.get("istruzioni") or "")[:MAX_ISTRUZIONI_CHARS],
        # ``null`` vale come "manca": con ``get(chiave, ripiego)`` un
        # ``"wiki": null`` scritto a mano tornava None e il ripiego sulla
        # struttura non scattava su una cartella che aveva raw/ e wiki/.
        wiki=bool(
            grezzo["wiki"] if grezzo.get("wiki") is not None else ha_struttura_wiki(base)
        ),
        memoria=_voci_da_grezzo(memoria_grezza),
        creato=str(grezzo.get("creato") or "")[:32],
    )


def _attributo(valore: str) -> str:
    """Il nome del progetto reso innocuo dentro un attributo dei blocchi.

    I blocchi che il modello legge sono pseudo-XML, e il nome e' testo scritto
    dall'utente: una virgoletta chiuderebbe l'attributo, e da li' in poi quello
    che segue verrebbe letto come marcatura.
    """
    return (
        str(valore or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


class ScritturaError(OSError):
    """``.progetto.json`` non e' stato salvato.

    Esiste perche' il silenzio era la modalita' di guasto peggiore: una
    funzione che ritorna la configurazione nuova quando il disco ha ancora
    quella vecchia fa leggere al modello "registrata" su una voce che non c'e'.
    """


class MemoriaError(ValueError):
    """Uso non valido della memoria del progetto."""


def scrivi_config(workspace: str | Path, config: ProgettoConfig) -> ProgettoConfig:
    """Salva ``.progetto.json`` con scrittura atomica, e toglie il file vecchio."""
    base = Path(workspace)
    pulito = replace(
        config,
        nome=_pulisci(config.nome)[:MAX_NOME_CHARS] or base.name,
        descrizione=str(config.descrizione or "").strip()[:MAX_DESCRIZIONE_CHARS],
        istruzioni=str(config.istruzioni or "").strip()[:MAX_ISTRUZIONI_CHARS],
        wiki=bool(config.wiki),
        memoria=_voci_da_grezzo([v.as_dict() for v in config.memoria]),
        creato=config.creato or _adesso(),
    )
    try:
        atomic_write_text(
            percorso_config(base),
            json.dumps(pulito.as_dict(), indent=2, ensure_ascii=False),
        )
    except OSError as errore:
        raise ScritturaError(f"Non ho potuto salvare '{percorso_config(base)}': {errore}") from errore
    # La conversione dal vault finisce qui: il file nuovo e' sul disco, quello
    # vecchio si toglie. Se non si puo' (cartella di sola lettura per i
    # cancellamenti, antivirus) resta li' innocuo: vince sempre il nuovo.
    vecchio = percorso_vecchio(base)
    if vecchio.is_file():
        try:
            vecchio.unlink()
        except OSError:
            pass
    return pulito


# I nomi dei campi di ``ProgettoConfig``, presi dalla dataclass e non scritti a
# mano: e' il filtro di ``aggiorna_config``.
_CAMPI_CONFIG = frozenset(f.name for f in fields(ProgettoConfig)) - {"memoria"}


def aggiorna_config(workspace: str | Path, **campi: object) -> ProgettoConfig:
    """Cambia solo i campi passati. Gli altri restano quelli che erano.

    La memoria non passa di qui: ha le sue funzioni, che sanno chi scrive.
    """
    corrente = leggi_config(workspace)
    noti = {k: v for k, v in campi.items() if v is not None and k in _CAMPI_CONFIG}
    return scrivi_config(workspace, replace(corrente, **noti))


def ensure_progetto(
    workspace: str | Path,
    *,
    nome: str = "",
    descrizione: str = "",
    istruzioni: str = "",
    wiki: bool | None = None,
) -> Path:
    """Rende progetto la cartella data e ne ritorna la radice assoluta.

    E' il punto unico in cui una cartella diventa un progetto: scrive
    ``.progetto.json`` se manca, e **solo se il progetto e' una wiki** monta
    anche la struttura ``raw/`` + ``wiki/``. Una cartella qualsiasi non si
    ritrova due cartelle che non ha chiesto.

    Su un progetto gia' registrato non tocca l'identita': i campi passati
    valgono solo per quello nuovo. Un vault di prima riceve qui il suo file
    nuovo, cioe' la conversione avviene aprendolo, non con una migrazione.
    """
    base = Path(workspace).expanduser().resolve()
    if not base.is_dir():
        raise NotADirectoryError(f"'{base}' non e' una cartella esistente.")
    if not percorso_config(base).is_file():
        corrente = leggi_config(base)
        if is_registrato(base):
            scrivi_config(base, corrente)
        else:
            scrivi_config(
                base,
                ProgettoConfig(
                    nome=_pulisci(nome) or base.name,
                    descrizione=descrizione,
                    istruzioni=istruzioni,
                    wiki=ha_struttura_wiki(base) if wiki is None else bool(wiki),
                ),
            )
    if leggi_config(base).wiki:
        scaffold(base)
    return base


def abilita_wiki(workspace: str | Path) -> ProgettoConfig:
    """Accende la modalita' manutentore e crea la struttura che le serve.

    Le due cose stanno insieme di proposito: accendere il flag senza ``wiki/``
    e ``raw/`` darebbe un manutentore che fallisce alla prima ingest, e la
    frase del prompt che promette i tre livelli sarebbe falsa.
    """
    base = Path(workspace)
    scaffold(base)
    return aggiorna_config(base, wiki=True)


def is_modalita_wiki(workspace: str | Path) -> bool:
    """True se qui l'agente deve fare il manutentore della wiki.

    E' il flag ``wiki`` del progetto, che su una cartella senza file
    d'identita' ricade sulla struttura: i vault di prima si comportano uguale.
    """
    if not str(workspace or "").strip():
        return False
    return leggi_config(workspace).wiki


def info_progetto(path: str, nome: str = "", *, chat: int = 0) -> ProgettoInfo:
    """Identita' piu' statistiche economiche: due conti di directory listing.

    ``nome`` resta accettato come ripiego per le voci del registro che non
    hanno ancora un file d'identita'; quando il file c'e', vince lui.
    """
    base = Path(path)
    esiste = base.is_dir()
    config = leggi_config(base)
    if nome and not is_registrato(base):
        config = replace(config, nome=nome)
    return ProgettoInfo(
        path=path,
        nome=config.nome,
        descrizione=config.descrizione,
        istruzioni=config.istruzioni,
        wiki=config.wiki,
        # In ``raw/`` si contano tutti i file, non i soli .md: le fonti sono
        # paper, PDF e trascrizioni. Fuori restano gli ``assets``.
        fonti=(
            conta_file(base / RAW_DIR, "", escludi=(Path(ASSETS_DIR).name,))
            if config.wiki and esiste
            else 0
        ),
        pagine=conta_file(base / WIKI_DIR) if config.wiki and esiste else 0,
        chat=chat,
        memoria=config.memoria,
        creato=config.creato,
        esiste=esiste,
    )


# ---------------------------------------------------------------------------
# La memoria del progetto
# ---------------------------------------------------------------------------


def aggiungi_voce(
    workspace: str | Path,
    testo: str,
    *,
    tipo: str = TIPO_DI_SERIE,
    autore: str = "modello",
    chat: str = "",
    titolo_chat: str = "",
) -> tuple[ProgettoConfig, VoceMemoria]:
    """Una voce in piu'. Idempotente sui doppioni: torna quella che c'era."""
    pulito = _taglia_voce(_pulisci(testo))
    if not pulito:
        raise MemoriaError("La voce e' vuota.")
    corrente = leggi_config(workspace)
    gia = next((v for v in corrente.memoria if v.testo == pulito), None)
    if gia is not None:
        # Riscrivere la stessa voce e' un sintomo di un modello che gira a
        # vuoto: non lo si premia con una riga in piu'.
        return corrente, gia
    if len(corrente.memoria) >= MAX_VOCI_MEMORIA:
        raise MemoriaError(
            f"La memoria del progetto e' piena ({MAX_VOCI_MEMORIA} voci). Togli "
            "quelle superate prima di aggiungerne altre."
        )
    adesso = _adesso()
    voce = VoceMemoria(
        id=_nuovo_id(v.id for v in corrente.memoria),
        testo=pulito,
        tipo=_tipo(tipo),
        autore=autore if autore in AUTORI else "modello",
        chat=str(chat or "")[:64],
        titolo_chat=_pulisci(titolo_chat)[:120],
        creata=adesso,
        aggiornata=adesso,
    )
    salvato = scrivi_config(workspace, replace(corrente, memoria=(*corrente.memoria, voce)))
    return salvato, voce


def modifica_voce(
    workspace: str | Path,
    ident: str,
    *,
    testo: str | None = None,
    tipo: str | None = None,
    autore: str = AUTORE_PROTETTO,
) -> tuple[ProgettoConfig, VoceMemoria]:
    """Corregge una voce. Chi la corregge ne diventa l'autore.

    E' la regola che rende sicura la scrittura automatica: una voce corretta a
    mano diventa dell'utente, e l'harness non la tocca piu'.
    """
    corrente = leggi_config(workspace)
    voce = corrente.voce(str(ident or "").strip())
    if voce is None:
        raise MemoriaError(f"Nessuna voce con id '{ident}'.")
    nuovo_testo = voce.testo if testo is None else _taglia_voce(_pulisci(testo))
    if not nuovo_testo:
        raise MemoriaError("Il testo della voce e' vuoto: per toglierla usa la cancellazione.")
    if any(v.testo == nuovo_testo and v.id != voce.id for v in corrente.memoria):
        raise MemoriaError("C'e' gia' una voce con questo testo.")
    nuova = replace(
        voce,
        testo=nuovo_testo,
        tipo=voce.tipo if tipo is None else _tipo(tipo),
        autore=autore if autore in AUTORI else voce.autore,
        aggiornata=_adesso(),
    )
    memoria = tuple(nuova if v.id == voce.id else v for v in corrente.memoria)
    return scrivi_config(workspace, replace(corrente, memoria=memoria)), nuova


def _trova(corrente: ProgettoConfig, riferimento: str) -> VoceMemoria:
    """Una voce per id, o per testo (anche solo il suo inizio).

    Per prefisso e non solo esatta: un modello che cita una voce a memoria la
    tronca, e rifiutare per una virgola mancante costa un giro per niente. Se
    l'inizio combacia con piu' voci si dice quali, invece di indovinare.
    """
    ago = _pulisci(riferimento)
    if not ago:
        raise MemoriaError("Serve la voce da togliere: il suo id o il suo testo.")
    per_id = corrente.voce(ago)
    if per_id is not None:
        return per_id
    candidate = [v for v in corrente.memoria if v.testo == ago] or [
        v for v in corrente.memoria if v.testo.startswith(ago[:40])
    ]
    if not candidate:
        raise MemoriaError(f"Nessuna voce corrisponde a '{riferimento}'.")
    if len(candidate) > 1:
        inizi = "; ".join(f"[{v.id}] '{v.testo[:50]}...'" for v in candidate[:4])
        raise MemoriaError(
            f"'{riferimento}' corrisponde a {len(candidate)} voci: {inizi}. "
            "Cita l'id o piu' testo per dire quale."
        )
    return candidate[0]


def togli_voce(
    workspace: str | Path, riferimento: str, *, autore: str = AUTORE_PROTETTO
) -> tuple[ProgettoConfig, VoceMemoria]:
    """Toglie una voce. Il modello non toglie quelle scritte dall'utente."""
    corrente = leggi_config(workspace)
    voce = _trova(corrente, riferimento)
    if voce.autore == AUTORE_PROTETTO and autore != AUTORE_PROTETTO:
        raise MemoriaError(
            "Questa voce l'ha scritta o corretta l'utente: non la togli tu. "
            "Se e' superata, diglielo nella risposta."
        )
    memoria = tuple(v for v in corrente.memoria if v.id != voce.id)
    return scrivi_config(workspace, replace(corrente, memoria=memoria)), voce


@dataclass
class EsitoMemoria:
    """Cosa e' cambiato nella memoria in un giro di scrittura automatica."""

    aggiunte: list[VoceMemoria] = field(default_factory=list)
    modificate: list[tuple[VoceMemoria, VoceMemoria]] = field(default_factory=list)
    tolte: list[VoceMemoria] = field(default_factory=list)
    rifiutate: list[dict[str, str]] = field(default_factory=list)
    errore: str = ""

    @property
    def cambiata(self) -> bool:
        return bool(self.aggiunte or self.modificate or self.tolte)

    def as_dict(self) -> dict[str, Any]:
        return {
            "aggiunte": [v.as_dict() for v in self.aggiunte],
            "modificate": [
                {"prima": prima.as_dict(), "dopo": dopo.as_dict()}
                for prima, dopo in self.modificate
            ],
            "tolte": [v.as_dict() for v in self.tolte],
            "rifiutate": list(self.rifiutate),
            "errore": self.errore,
        }


# Tetto alle operazioni di un solo giro: un turno che vuole cambiare dieci
# voci non sta distillando, sta riscrivendo la memoria. Le prime passano, le
# altre si scartano dicendolo.
MAX_OPERAZIONI_PER_GIRO = 6


def applica_operazioni(
    workspace: str | Path,
    operazioni: Iterable[Mapping[str, Any]],
    *,
    autore: str = "harness",
    chat: str = "",
    titolo_chat: str = "",
) -> EsitoMemoria:
    """Applica un giro di operazioni in una lettura e una scrittura sola.

    Le operazioni sono ``{"azione": "aggiungi"|"modifica"|"togli", ...}``.
    Quelle non valide si scartano una per una, con il motivo: una voce sbagliata
    non deve far perdere le altre del giro. Le voci dell'utente non si toccano.
    """
    esito = EsitoMemoria()
    corrente = leggi_config(workspace)
    memoria = list(corrente.memoria)
    adesso = _adesso()

    def rifiuta(op: Mapping[str, Any], motivo: str) -> None:
        esito.rifiutate.append({
            "azione": str(op.get("azione") or "")[:20],
            "testo": _pulisci(op.get("testo") or op.get("id") or "")[:120],
            "motivo": motivo,
        })

    for n, op in enumerate(operazioni):
        if not isinstance(op, Mapping):
            continue
        if n >= MAX_OPERAZIONI_PER_GIRO:
            rifiuta(op, f"oltre le {MAX_OPERAZIONI_PER_GIRO} operazioni di un giro")
            continue
        azione = str(op.get("azione") or op.get("action") or "").strip().lower()
        if azione in ("aggiungi", "add"):
            testo = _taglia_voce(_pulisci(op.get("testo")))
            if not testo:
                rifiuta(op, "testo vuoto")
                continue
            if any(v.testo == testo for v in memoria):
                rifiuta(op, "c'e' gia'")
                continue
            if len(memoria) >= MAX_VOCI_MEMORIA:
                rifiuta(op, f"memoria piena ({MAX_VOCI_MEMORIA} voci)")
                continue
            voce = VoceMemoria(
                id=_nuovo_id(v.id for v in memoria), testo=testo, tipo=_tipo(op.get("tipo")),
                autore=autore, chat=str(chat or "")[:64], titolo_chat=_pulisci(titolo_chat)[:120],
                creata=adesso, aggiornata=adesso,
            )
            memoria.append(voce)
            esito.aggiunte.append(voce)
        elif azione in ("modifica", "aggiorna", "update"):
            ident = str(op.get("id") or "").strip()
            idx = next((i for i, v in enumerate(memoria) if v.id == ident), None)
            if idx is None:
                rifiuta(op, "id sconosciuto")
                continue
            prima = memoria[idx]
            if prima.autore == AUTORE_PROTETTO and autore != AUTORE_PROTETTO:
                rifiuta(op, "voce dell'utente")
                continue
            testo = _taglia_voce(_pulisci(op.get("testo"))) if op.get("testo") else prima.testo
            if any(v.testo == testo and v.id != ident for v in memoria):
                rifiuta(op, "doppione di un'altra voce")
                continue
            dopo = replace(
                prima, testo=testo,
                tipo=_tipo(op.get("tipo")) if op.get("tipo") else prima.tipo,
                autore=autore, chat=str(chat or prima.chat)[:64],
                titolo_chat=_pulisci(titolo_chat or prima.titolo_chat)[:120],
                aggiornata=adesso,
            )
            if dopo.testo == prima.testo and dopo.tipo == prima.tipo:
                continue
            memoria[idx] = dopo
            esito.modificate.append((prima, dopo))
        elif azione in ("togli", "rimuovi", "remove"):
            ident = str(op.get("id") or "").strip()
            voce = next((v for v in memoria if v.id == ident), None)
            if voce is None:
                rifiuta(op, "id sconosciuto")
                continue
            if voce.autore == AUTORE_PROTETTO and autore != AUTORE_PROTETTO:
                rifiuta(op, "voce dell'utente")
                continue
            memoria = [v for v in memoria if v.id != ident]
            esito.tolte.append(voce)
        else:
            rifiuta(op, "azione sconosciuta")

    if esito.cambiata:
        try:
            scrivi_config(workspace, replace(corrente, memoria=tuple(memoria)))
        except ScritturaError as errore:
            # Niente e' cambiato sul disco: l'esito deve dirlo, non elencare
            # voci che non esistono.
            return EsitoMemoria(rifiutate=esito.rifiutate, errore=str(errore))
    return esito


def blocco_memoria(config: ProgettoConfig, *, automatica: bool = True) -> str:
    """La memoria del progetto, da mettere **in coda** come il piano e le note.

    Raggruppata per tipo, dal piu' stabile al piu' volatile. Stessa ragione di
    sempre per la coda: il prefisso resta byte-identico fra un passo e l'altro.

    ``automatica`` cambia l'ultima frase, che descrive un comportamento
    dell'harness e quindi deve essere vera: con la scrittura automatica spenta
    dire al modello "ci pensa l'harness" gli toglierebbe l'unico motivo per
    scriverla lui.
    """
    if not config.memoria:
        return ""
    righe = [f'<memoria_del_progetto nome="{_attributo(config.nome)}">']
    for tipo in TIPI_MEMORIA:
        voci = [v for v in config.memoria if v.tipo == tipo]
        if not voci:
            continue
        righe.append(f"{ETICHETTE_TIPO[tipo]}:")
        righe.extend(f"- {v.testo}" for v in voci)
    righe += [
        "</memoria_del_progetto>",
        "",
        "Cose stabilite in questo progetto, anche in conversazioni precedenti a "
        "questa: valgono come se le avessi appena lette. Se una voce "
        "contraddice quello che vedi adesso, fidati di quello che vedi e dillo "
        "nella risposta. "
        + (
            "La memoria la aggiorna l'harness alla fine dei turni in cui si "
            "lavora: non serve che la riscrivi tu."
            if automatica
            else "Se stabilisci qualcosa che varra' anche nelle prossime chat, "
            "aggiungilo con manage_notes ambito='progetto'."
        ),
    ]
    return "\n".join(righe)


def blocco_istruzioni(config: ProgettoConfig) -> str:
    """Le istruzioni del progetto, per il prompt di sistema.

    Solo ``istruzioni``: la ``descrizione`` serve a ritrovare la cartella
    nell'elenco e non deve diventare un ordine permanente per l'agente.
    """
    testo = (config.istruzioni or "").strip()
    if not testo:
        return ""
    return (
        f'\n\n<progetto nome="{_attributo(config.nome)}">\n'
        "Istruzioni scritte dall'utente per il lavoro in questo progetto. "
        "Valgono per tutta la conversazione.\n"
        f"{testo}\n"
        "</progetto>"
    )


def scaffold(base: Path) -> list[str]:
    """Crea la struttura minima della wiki; ritorna cio' che ha creato.

    Non sovrascrive mai nulla di esistente: su una wiki gia' avviata e' una
    no-op completa. Lo schema di parte (CLAUDE.md, index.md) viene scritto
    solo se assente, cosi' le convenzioni co-evolute con l'utente sopravvivono.

    Non solleva: torna cio' che e' riuscito a creare. Su una cartella di sola
    lettura -- un progetto su un disco esterno smontato, una cartella di rete
    caduta -- l'eccezione risaliva fino alla rotta che apre il progetto, e
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




def conta_file(
    cartella: Path, suffisso: str = ".md", *, escludi: tuple[str, ...] = ()
) -> int:
    """Numero di file con quel suffisso sotto una cartella, ricorsivo.

    ``suffisso=""`` conta tutti i file: e' quello che serve per ``raw/``, dove
    le fonti sono PDF, txt, epub e trascrizioni. Contando solo i ``.md`` un
    progetto wiki con duecento paper mostrava "0 fonti".

    ``escludi`` sono nomi di sottocartelle da saltare -- ``assets`` in ``raw/``,
    che per struttura della wiki e' il posto delle immagini di corredo e non
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
    del non annotarla. Prima sollevava ``FileNotFoundError`` su un progetto senza
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
# Schema della wiki (LLM Wiki)

Questo progetto e' una wiki secondo il pattern LLM Wiki: tre livelli.

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


WIKI_SYSTEM_PROMPT = """\
Sei il manutentore della wiki di questo progetto: il workspace in cui \
lavori e' organizzato secondo lo schema LLM Wiki di Karpathy.

## I tre strati
- ``raw/`` -- le fonti dell'utente: articoli, paper, note grezze. Sono \
IMMUTABILI: leggile, mai modificarle ne' cancellarle.
- ``wiki/`` -- la wiki che scrivi e mantieni tu: pagine di sintesi, pagine \
di entita' e concetti, confronti, overview. La cross-referenza e' tua.
- ``CLAUDE.md`` -- lo schema della wiki: convenzioni sui nomi, formato delle \
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
- Non toccare nulla fuori dal progetto: ``raw/`` si legge, il resto del mondo \
non esiste.
- L'utente cura le fonti e fa le domande giuste; tutto il resto -- riassunti, \
link, indici, log, coerenza -- e' responsabilita' tua.\
"""




def blocco_struttura(workspace: str | Path) -> str:
    """La parte del contesto della wiki che NON cambia fra un passo e l'altro.

    Solo percorsi e nomi di cartella: due ingest di fila producono lo stesso
    testo identico, quindi puo' stare nel prompt di sistema senza rompere il
    prefisso. Lo *stato* -- indice e log -- sta in ``blocco_stato``, che va in
    coda come il piano e le note.
    """
    return "\n".join(
        [
            "\n\n## Struttura della wiki\n",
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
    progetto: il prefisso deve restare byte-identico fra un passo e l'altro, ed e'
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
            "<stato_della_wiki>",
            "### Indice attuale della wiki (`wiki/index.md`)",
            indice if indice.strip() else "_(indice vuoto o illeggibile)_",
            "",
            "### Ultime operazioni (`wiki/log.md`)",
            coda if coda.strip() else "_(log vuoto: nessuna operazione registrata)_",
            "</stato_della_wiki>",
        ]
    )


