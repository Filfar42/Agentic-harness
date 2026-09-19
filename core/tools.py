"""Tool dell'agente, sandbox del workspace e schemi di function calling.

Nessuno stato globale: workspace, file toccati, memorie e piano viaggiano in
un :class:`ToolContext`, cosi' i tool sono testabili a se stanti e due turni
concorrenti non si pestano i piedi.

Principi di design degli schemi, tutti pagati con dei bug veri:
  * ogni ``description`` dice **quando** usare il tool, non solo cosa fa;
  * i nomi dei parametri sono espliciti e i default documentati;
  * gli errori tornano al modello in forma azionabile ("il file non esiste,
    usa list_files"), perche' un errore muto e' la causa piu' comune di loop.
"""

from __future__ import annotations

from .process import run_bounded, OutputLimitExceeded

import fnmatch
import hashlib
import json
import logging
import os
import platform
import posixpath
import re
import shlex
import shutil
import stat
import subprocess
import time
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from collections.abc import Callable

from . import sandbox as sandbox_mod
from . import atomic
from . import vault as vault_mod
from .config import Budgets
from .memory import add_memory, remove_memory
from .notes import NoteError, Notes
from . import deposito as deposito_mod
from .plan import DONE, SKIPPED, Plan, PlanError
from .textutils import smart_truncate, truncate_lines
from .tool_validation import validate_arguments
from .jsonsafe import JsonBoundaryError, loads_object

logger = logging.getLogger(__name__)
# Bounded striped locks serialize file read/modify/write transactions made by
# concurrent turns. They do not claim to synchronize unrelated host processes.
_FILE_LOCKS = tuple(threading.RLock() for _ in range(64))
MAX_TEXT_FILE_BYTES = 8 * 1024 * 1024

# Cartelle che non hanno mai valore informativo per l'agente e che, se listate,
# saturano il contesto in un colpo solo.
IGNORED_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "env", "node_modules",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea", ".vscode", "dist",
    "build", ".next", ".turbo", "target", ".tox", "site-packages", ".streamlit",
}
BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".pdf", ".zip",
    ".gz", ".tar", ".7z", ".rar", ".exe", ".dll", ".so", ".dylib", ".pyc",
    ".woff", ".woff2", ".ttf", ".mp3", ".mp4", ".mov", ".sqlite", ".db",
}

# Comandi rifiutati a prescindere: un LLM locale che sbaglia un path puo'
# altrimenti cancellare il disco. Non e' una sandbox vera, e' un guard-rail.
#
# Fuori dalla sandbox (``sandbox = "host"``) questo elenco e' tutto quello che
# c'e' fra il modello e il disco dell'utente, e resta severo com'era: qualunque
# rimozione ricorsiva o forzata viene rifiutata, perche' li' un percorso
# relativo puo' puntare ovunque.
DANGEROUS_PATTERNS = (
    # Il ``-`` davanti al gruppo che contiene ``r``/``f`` e' obbligatorio: senza,
    # ``[a-zA-Z]*[rf]`` combaciava con il **nome del file**, e ``rm file.txt``
    # o ``rm requirements.txt`` chiedevano conferma come un ``rm -rf /``.
    # Verificato su tredici casi. Una guardia che scatta sulle cancellazioni
    # ordinarie insegna a premere "sì" senza leggere, che e' il modo in cui una
    # conferma smette di proteggere.
    re.compile(r"\brm\s+(?:-[a-zA-Z-]+\s+)*-(?:[a-zA-Z]*[rf]|-(?:recursive|force))", re.I),
    re.compile(r"\bdel\s+/[sqf]", re.I),
    re.compile(r"\bformat\s+[a-z]:", re.I),
    re.compile(r"\bmkfs\b", re.I),
    re.compile(r":\(\)\s*\{.*\};\s*:", re.S),  # fork bomb
    re.compile(r"\bshutdown\b|\breboot\b", re.I),
    re.compile(r"\bRemove-Item\b.*-Recurse", re.I),
    re.compile(r">\s*/dev/sd[a-z]", re.I),
)

# Quello che resta vietato **anche dentro la sandbox**, perche' non e' una
# cancellazione di file: e' un attacco alla macchina o al disco a blocchi. Il
# container condivide il kernel con l'host, quindi non sono innocui li' dentro.
IRREPARABILI = (
    re.compile(r"\bmkfs(\.\w+)?\b", re.I),
    re.compile(r"\bformat\s+[a-z]:", re.I),
    re.compile(r":\(\)\s*\{.*\};\s*:", re.S),          # fork bomb
    re.compile(r"\b(shutdown|reboot|halt|poweroff)\b", re.I),
    re.compile(r">\s*/dev/(sd[a-z]|nvme\d|hd[a-z])", re.I),
    re.compile(r"\bdd\b[^|;&]*\bof=/dev/", re.I),
)

# Verbi che cancellano. Servono solo a decidere **su quali argomenti** guardare:
# la decisione non la prende piu' il verbo, la prende il bersaglio.
VERBI_DISTRUTTIVI = frozenset(
    {"rm", "rmdir", "unlink", "shred", "del", "erase", "remove-item", "ri", "rd"}
)
# Separatori di comando dentro una stessa riga di shell.
_SEPARATORI = re.compile(r"\s*(?:&&|\|\||[;|&\n])\s*")


def _bersagli_di(segmento: str) -> list[str]:
    """Gli argomenti non-opzione di un segmento, se il segmento cancella."""
    try:
        pezzi = shlex.split(segmento, posix=True)
    except ValueError:
        # Virgolette non chiuse: non si capisce cosa tocchi. Si tratta come un
        # comando che cancella tutto, cioe' lo si guarda col massimo sospetto.
        pezzi = segmento.split()
    # Assegnazioni d'ambiente in testa (FOO=bar rm ...): non sono il comando.
    while pezzi and re.match(r"^\w+=", pezzi[0]):
        pezzi.pop(0)
    if not pezzi:
        return []
    verbo = pezzi[0].rsplit("/", 1)[-1].lower()
    if verbo not in VERBI_DISTRUTTIVI:
        return []
    return [p for p in pezzi[1:] if not p.startswith("-")]


def _fuori_dal_recinto(bersaglio: str, workdir: str) -> bool:
    """Il bersaglio esce dalla cartella montata, o **e'** la cartella montata?

    Dentro la sandbox il kernel garantisce che l'unica cosa scrivibile sia
    ``workdir``: cancellare li' dentro e' lavoro normale, ed e' il motivo per
    cui il blocco per verbo era diventato una tassa (tre rifiuti di fila su un
    ``rm -r __pycache__`` nella sessione del 19/08/2026, che e' esattamente
    cio' che l'utente aveva chiesto di fare). Restano fuori due casi:

    * il bersaglio punta **sopra** il recinto -- percorso assoluto altrove,
      ``~``, una lettera di unita' Windows, una variabile che non sappiamo
      risolvere, o abbastanza ``..`` da uscire;
    * il bersaglio **e'** il recinto, o il suo contenuto in blocco: quella e'
      la cartella di lavoro dell'utente, non un file di scarto.
    """
    t = bersaglio.strip().strip("\"'")
    if not t:
        return False
    if t.startswith("~") or t.startswith("$") or "$(" in t or "`" in t:
        return True
    if re.match(r"^[A-Za-z]:[\\/]", t) or t.startswith("\\\\"):
        return True
    base = workdir if t.startswith("/") else workdir + "/"
    intero = t if t.startswith("/") else base + t
    norm = posixpath.normpath(intero)
    # ``normpath`` non tocca i glob: '/work/*' resta '/work/*'. Un bersaglio
    # che e' solo un glob sulla radice svuota il progetto tanto quanto la
    # radice stessa, e va trattato allo stesso modo.
    if norm in (workdir, workdir + "/*", workdir + "/.*"):
        return True
    return not norm.startswith(workdir + "/")


def motivo_del_blocco(command: str, *, sandbox: str, workdir: str) -> tuple[str, str] | None:
    """(motivo, suggerimento) se il comando va rifiutato, altrimenti None.

    Il cambio di v2.27: **si guarda il bersaglio, non il verbo**. Prima
    qualunque riga contenente ``rm -r`` veniva rifiutata, anche quando l'utente
    aveva chiesto a parole di ripulire la cartella e il comando girava dentro un
    container che vede solo quella cartella. Un guard-rail che vieta il lavoro
    richiesto non protegge nessuno: insegna ad aggirarlo.
    """
    for rx in IRREPARABILI:
        if rx.search(command):
            return (
                "Comando bloccato: non tocca dei file, tocca la macchina "
                "(filesystem a blocchi, spegnimento o fork bomb).",
                "Non esiste una variante autorizzata: questa non e' un'operazione "
                "sul progetto. Se serve davvero, la fa l'utente a mano.",
            )

    if sandbox != "docker":
        # Senza recinto un percorso relativo puo' puntare ovunque -- ``cd ..``
        # e sei nel disco dell'utente. Li' il verbo torna a essere l'unico
        # segnale affidabile, e la severita' di prima resta giustificata.
        for rx in DANGEROUS_PATTERNS:
            if rx.search(command):
                return (
                    "Comando bloccato dal guard-rail di sicurezza (distruttivo): "
                    "i comandi girano **sulla macchina dell'utente**, senza sandbox.",
                    "Accendi la sandbox Docker nelle impostazioni, oppure chiedi "
                    "conferma all'utente a parole invece di eseguire.",
                )
        return None

    for segmento in _SEPARATORI.split(command):
        for bersaglio in _bersagli_di(segmento):
            if _fuori_dal_recinto(bersaglio, workdir):
                return (
                    f"Comando bloccato: '{bersaglio}' e' fuori dalla cartella di "
                    f"lavoro (o e' la cartella stessa). Dentro {workdir} puoi "
                    "cancellare quello che serve; sopra, no.",
                    "Punta il comando a un percorso dentro la cartella di lavoro. "
                    "Se serve davvero agire fuori, chiedilo all'utente con "
                    "ask_user_question: e' una cosa che deve decidere lui.",
                )
    return None


class WorkspaceError(Exception):
    """Errore recuperabile: viene rimandato al modello come testo.

    Accetta lo stesso ``hint`` di ``_err``, perche' i tool alzano e ritornano
    errori senza una regola fissa e il suggerimento e' la parte utile: dice al
    modello cosa fare invece di riprovare uguale. Prima era una ``Exception``
    nuda, e ``raise WorkspaceError(msg, hint=...)`` -- scritto in due punti di
    ``web_search`` -- moriva con ``TypeError: takes no keyword arguments``:
    il dispatcher lo prendeva per un errore di argomenti e al modello
    arrivava "Argomenti non validi per 'web_search'", che e' falso e non
    aiuta nessuno.
    """

    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


@dataclass
class ToolContext:
    """Stato condiviso fra i tool per un singolo turno."""

    workspace: str
    timeout_s: int = 120
    # Quanto puo' essere lungo il risultato di un tool. Non e' una costante
    # perche' dipende dalla finestra del modello in uso: il default e' la
    # taratura per 16k, il ciclo agentico lo sostituisce con quella vera
    # (``budgets_for(num_ctx)``) all'inizio del turno.
    budgets: Budgets = field(default_factory=Budgets)
    memories: list[dict[str, str]] = field(default_factory=list)
    touched_files: set[str] = field(default_factory=set)
    # File di cui l'agente conosce il contenuto in questa conversazione:
    # letti con read_file, oppure scritti da lui (nel qual caso sa cosa
    # contengono). Serve al guard-rail di ``write_file``.
    known_files: set[str] = field(default_factory=set)
    # Piano di lavoro della conversazione. Vive qui e non nel ciclo agentico
    # perche' deve sopravvivere ai turni: un compito in cinque punti non si
    # esaurisce in un turno, e ricominciare il piano ad ogni messaggio
    # dell'utente sarebbe come non averlo.
    plan: Plan = field(default_factory=Plan)
    # Foglio di note del compito in corso. Come il piano vive nel contesto e
    # non nel ciclo agentico, ma per una ragione in piu': e' cio' che deve
    # restare quando la cronologia viene compattata.
    notes: Notes = field(default_factory=Notes)
    # Torna False se il salvataggio e' fallito: vedi ``memories_changed``.
    on_memories_changed: Callable[[list[dict[str, str]]], bool | None] | None = None
    on_plan_changed: Callable[[Plan], None] | None = None
    on_notes_changed: Callable[[Notes], None] | None = None
    # Come si esegue una delega. Lo inietta il ciclo agentico, che ha in mano
    # backend e parametri: tenerli qui vorrebbe dire far conoscere il modello a
    # un modulo che si occupa di file.
    on_delega: Callable[[str], dict[str, Any]] | None = None
    # Come si interroga un vault LLM Wiki senza cambiare workspace. La monta
    # il ciclo agentico, come per ``on_delega``: anche qui servono backend e
    # parametri del turno per far girare il sotto-turno del cercatore.
    on_vault_search: Callable[..., dict[str, Any]] | None = None
    # I vault noti (la chiave "vaults" delle impostazioni): servono al
    # sotto-turno di vault_search per risolvere il nome in percorso.
    registri_vault: list[dict[str, Any]] = field(default_factory=list)
    # Il vault in cui si sta lavorando, se il workspace ne e' uno. Vuoto
    # altrove: e' quello che rende ``manage_notes ambito='vault'`` possibile
    # qui e un errore pulito altrove.
    vault_dir: str = ""
    # La memoria del vault, caricata all'inizio del turno. Sta nel contesto e
    # non si rilegge da disco ad ogni passo: cambia solo quando la cambia il
    # modello, e in quel caso la riscrive il tool.
    vault_notes: list[str] = field(default_factory=list)
    on_vault_notes_changed: Callable[[list[str]], None] | None = None
    allow_dangerous_commands: bool = False
    # "docker" = i comandi girano in un container che monta solo il workspace.
    # "host" = esecuzione diretta sulla macchina, come prima: l'agente vede
    # tutto il disco, perche' la working directory non e' un recinto.
    sandbox: str = "docker"
    docker_image: str = sandbox_mod.DEFAULT_IMAGE
    sandbox_network: bool = True
    # Intervallo di porte pubblicate dal container verso 127.0.0.1, per far
    # vedere nel browser quello che l'agente avvia. None = anteprime di
    # applicazioni disattivate; i file si vedono lo stesso.
    preview_ports: tuple[int, int] | None = None
    # Se una pagina appartiene a un progetto con un backend riconoscibile,
    # l'harness lo avvia da solo prima di mostrarla. Si puo' spegnere: e' un
    # processo che parte senza che nessuno l'abbia chiesto esplicitamente.
    preview_autostart_backend: bool = True
    # Cosa l'utente sta gia' guardando nel pannello di anteprima. Serve al
    # contesto, non ai tool: senza, il modello non ha modo di sapere che
    # l'anteprima e' gia' aperta e prova a riaprirla.
    preview: dict[str, Any] | None = None
    # Impronta dell'ultima lettura di ogni file, con il passo in cui e'
    # avvenuta: serve a rispondere "invariato" invece di rimandare lo stesso
    # contenuto. Il passo conta perche' i risultati vecchi vengono compattati
    # fuori dal contesto, e a quel punto "usa quello di prima" sarebbe falso.
    read_cache: dict[str, tuple[str, int]] = field(default_factory=dict)
    step: int = 0
    # Comando di verifica ancora rosso, se ce n'e' uno. Lo aggiorna il ciclo
    # agentico dopo ogni run_command: serve al guard sui file di test.
    red_command: str | None = None
    # File di test creati dall'agente in questa sessione. Non sono la specifica
    # dell'utente: sono ipotesi che l'agente si e' scritto da solo, spesso
    # perche' la richiesta non diceva abbastanza. Devono restare correggibili,
    # altrimenti un modello che si e' contraddetto da solo entra in stallo.
    authored_tests: set[str] = field(default_factory=set)
    # Fallimenti consecutivi per comando. Serve a riconoscere il ciclo che
    # costa piu' caro di tutti: lo stesso comando rilanciato all'infinito
    # perche' il modello cerca il bug dove non c'e'. Osservato dal vivo:
    # diciassette passi su trenta contro un'asserzione impossibile.
    comandi_falliti: dict[str, int] = field(default_factory=dict)
    # Quante volte la guardia ha respinto una modifica, per file: dopo il
    # secondo tentativo il messaggio cambia tono e indica l'uscita.
    test_guard_refusals: dict[str, int] = field(default_factory=dict)
    readonly_request: bool = False
    # La goccia "Ricerca online" era accesa quando il turno e' partito. Senza
    # questo flag un modello che ricorda il tool da un turno precedente
    # potrebbe scavalcare l'interruttore: qui, non nello schema, sta la
    # garanzia che spento vuol dire anche non usabile.
    web_search_enabled: bool = False
    # Simboli pubblici comparsi in questo turno: nome -> file che li definisce.
    # Servono a distinguere una verifica verde che misura il codice nuovo da
    # una che misura tutt'altro.
    new_symbols: dict[str, str] = field(default_factory=dict)
    # Verifiche rosse chiuse dichiarandole non pertinenti (``ignore_red``):
    # comando, punto del piano e motivo scritto dal modello. Sono l'uscita di
    # sicurezza del guard-rail, quindi vanno **contate**: un turno che ne usa
    # tre non e' un turno andato bene, e senza questo elenco la differenza fra
    # "tutto verde" e "tre rossi archiviati" non si vedrebbe da nessuna parte.
    rossi_ignorati: list[dict[str, str]] = field(default_factory=list)
    # Punti di piano chiusi in questo passo, in attesa che ``agent`` ne
    # distilli il ragionamento. E' una **casella postale, non una callback**:
    # l'estrazione e' una chiamata al modello, e il modello qui dentro non si
    # conosce (stessa ragione per cui ``on_delega`` e ``on_vault_search``
    # esistono). Farla dentro il tool bloccherebbe il passo a meta' e non
    # comparirebbe in nessun evento; ``agent`` la svuota a tool finiti, quando
    # ha backend, parametri e cronologia sotto mano.
    punti_chiusi: list[dict[str, Any]] = field(default_factory=list)
    # Il testo intero dei risultati troppo lunghi finisce su disco prima di
    # essere troncato, e il risultato ne porta il percorso. Non cambia di un
    # token quello che entra in contesto: cambia che la coda tagliata smette di
    # essere perduta. Vedi ``core/deposito.py``.
    deposito_attivo: bool = True
    deposito_max_mb: int = deposito_mod.MAX_MB_DEFAULT
    # Tracker per le verifiche rosse, impostato da agent.py per permetterne
    # l'azzeramento automatico o manuale quando un comando non e' pertinente.
    verification: Any = None
    # I tool che questo turno puo' eseguire. ``None`` = tutti (il turno
    # normale); un insieme = solo quelli, ed e' cosi' che i sotto-turni --
    # l'esploratore della delega e il cercatore del vault -- restano di sola
    # lettura.
    #
    # Prima il perimetro esisteva solo nello **schema** passato al modello, e
    # lo schema non e' un permesso: ``parse_text_tool_calls`` recupera le
    # chiamate scritte come testo accettando ogni nome di ``TOOL_NAMES`` --
    # che e' per scelta dichiarata il vocabolario dell'harness, non cio' che
    # questo turno puo' fare -- e ``dispatch`` guardava solo ``TOOL_IMPLS``.
    # Un figlio che scriveva ``{"name": "run_command", ...}`` come testo
    # eseguiva comandi. Il percorso non e' teorico: e' quello che l'harness
    # supporta apposta per i modelli che non fanno function calling nativo, e
    # su qwen2.5-coder:7b e' l'unico che si osservi.
    tool_consentiti: frozenset[str] | None = None

    def puo_usare(self, name: str) -> bool:
        """Questo turno ha il permesso di eseguire ``name``?"""
        return self.tool_consentiti is None or name in self.tool_consentiti

    def clear_red_command(self) -> None:
        """Azzera l'intero registro delle verifiche.

        **Le operazioni sul piano non la chiamano piu'.** La chiamavano
        ``complete`` con ``ignore_red`` e ``skip``, e siccome il vecchio
        ``clear()`` svuotava tutto, dichiarare non pertinente *una* verifica ne
        cancellava anche altre: in prova, ignorarne una ne faceva sparire due.
        Resta come reset esplicito -- un turno nuovo, un workspace cambiato --
        dove buttare via tutto e' proprio quello che si vuole.
        """
        self.red_command = None
        if self.verification is not None and hasattr(self.verification, "clear"):
            self.verification.clear()

    def quality_summary(self) -> dict[str, Any]:
        """Stato delle verifiche, indipendente dall'avanzamento del piano.

        E' la lettura che il piano fa del registro: solo lettura. Chiudere un
        punto non cambia com'e' andato un test, e questo metodo e' il posto dove
        quella separazione si vede -- il piano guarda, non tocca.

        Senza registro (sotto-turni, test che costruiscono un ``ToolContext`` a
        mano) resta la vista minima ricavabile da ``red_command``: dice meno, ma
        non mente.
        """
        registro = self.verification
        if registro is not None and hasattr(registro, "riepilogo"):
            return registro.riepilogo()
        pendenti = ([{"comando": self.red_command, "identita": self.red_command,
                      "stato": "rossa", "ambito": "ignoto", "returncode": None,
                      "tentativi": 0}]
                    if self.red_command else [])
        return {
            "stato": "rossa" if pendenti else "non_verificato",
            "pendenti": pendenti,
            "verdi": [], "giustificate": [], "sbagliate": [],
            "suite_rossa": False,
        }

    def verifiche_pendenti(self) -> list[dict[str, Any]]:
        """Le verifiche ancora rosse, come dizionari."""
        return list(self.quality_summary().get("pendenti") or [])

    def giustifica_verifica(self, motivo: str, via: str = "") -> dict[str, Any] | None:
        """Attacca un motivo scritto alla verifica rossa piu' insistente.

        **Una sola.** E' la differenza con il vecchio ``clear_red_command``: li'
        una frase archiviava tutto il registro, qui archivia la verifica che i
        messaggi hanno nominato, e le altre restano dove sono a farsi vedere.
        """
        registro = self.verification
        if registro is None or not hasattr(registro, "giustifica"):
            # Senza registro l'unica cosa che c'e' e' ``red_command``: si spegne
            # quella, che e' quanto il vecchio codice faceva comunque.
            spento = self.red_command
            self.red_command = None
            return {"comando": spento, "identita": spento, "motivo": motivo,
                    "via": via} if spento else None
        verifica = registro.giustifica(None, motivo, via)
        if verifica is None:
            return None
        pendenti = registro.pendenti
        self.red_command = pendenti[0].comando if pendenti else None
        return verifica.to_dict()

    @property
    def base(self) -> Path:
        return Path(self.workspace).resolve()

    def touch(self, path: Path) -> str:
        try:
            rel = path.resolve().relative_to(self.base).as_posix()
        except ValueError:
            rel = str(path)
        # Il banco di prova non e' il progetto: elencarlo fra i file toccati
        # farebbe sembrare modificato del lavoro che nessuno ha chiesto.
        if not is_scratch_path(rel):
            self.touched_files.add(rel)
        return rel

    def memories_changed(self) -> bool:
        """True se il cambiamento e' arrivato dove doveva.

        Il gancio puo' non esserci (sotto-turni, test) e allora non c'e' niente
        da salvare: e' un successo. Se c'e' e torna ``False``, il salvataggio e'
        fallito e chi ha chiamato deve dirlo invece di rispondere "ok".
        """
        if not self.on_memories_changed:
            return True
        return self.on_memories_changed(self.memories) is not False

    def plan_changed(self) -> None:
        if self.on_plan_changed:
            self.on_plan_changed(self.plan)

    def notes_changed(self) -> None:
        if self.on_notes_changed:
            self.on_notes_changed(self.notes)

    def vault_notes_changed(self) -> None:
        if self.on_vault_notes_changed:
            self.on_vault_notes_changed(self.vault_notes)


# ---------------------------------------------------------------------------
# Sandbox
# ---------------------------------------------------------------------------


# --- allegati ---------------------------------------------------------------
# I file che l'utente allega alla chat finiscono in una sottocartella del
# workspace: cosi' l'agente li raggiunge con i tool che ha gia' (read_file,
# search_files) senza allargare il sandbox, e restano visibili in Esplora
# risorse anche a chat chiusa.
ATTACHMENTS_DIR = "allegati"
ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024
_UNSAFE_NAME = re.compile(r"[^\w.\- ]", re.UNICODE)


def safe_attachment_name(filename: str) -> str:
    """Nome di file innocuo: niente cartelle, niente caratteri di controllo.

    Il nome arriva dal browser, quindi e' input non fidato: ``../../.bashrc``
    o un nome con separatori di percorso non deve poter uscire dalla cartella
    degli allegati.
    """
    base = Path(str(filename or "").replace("\\", "/")).name
    cleaned = _UNSAFE_NAME.sub("_", base).strip(" .")
    return (cleaned or "allegato")[:120]


def store_attachment(workspace: str | Path, filename: str, data: bytes) -> dict[str, Any]:
    """Salva un allegato, senza mai sovrascrivere un file gia' presente."""
    if len(data) > ATTACHMENT_MAX_BYTES:
        raise WorkspaceError(
            f"'{filename}' supera il limite di "
            f"{ATTACHMENT_MAX_BYTES // (1024 * 1024)} MB per allegato."
        )
    folder = resolve_path(workspace, ATTACHMENTS_DIR)
    folder.mkdir(parents=True, exist_ok=True)

    name = safe_attachment_name(filename)
    stem, suffix = Path(name).stem, Path(name).suffix
    # Exclusive creation is both the collision check and the write boundary.
    # exists()+write_bytes() overwrote concurrent uploads and followed dangling
    # symlinks planted at a candidate filename.
    for counter in range(10_000):
        target = folder / (name if counter == 0 else f"{stem}-{counter}{suffix}")
        try:
            with target.open("xb") as stream:
                stream.write(data)
            break
        except FileExistsError:
            continue
    else:
        raise WorkspaceError("Troppi allegati con lo stesso nome: usa un altro nome.")
    return {
        "name": target.name,
        "path": f"{ATTACHMENTS_DIR}/{target.name}",
        "size": len(data),
        "added_at": time.time(),
    }


# --- il test e' la specifica -------------------------------------------------
# Osservato con qwen3.5: davanti a una verifica rossa il modello ragiona
# "devo correggere il test o la funzione" e sceglie il test, riscrivendolo in
# una tautologia (`assert len(scarta([1,2,3,4,5], k=0.5)) <= 5` su una lista di
# 5 elementi). Il comando torna verde e non misura piu' niente.
#
# La prima versione di questa guardia bloccava l'intero file finche' c'era una
# verifica rossa. Troppo grossolana, e due sessioni lo hanno dimostrato:
#   - cinque rifiuti di fila per aggiungere un `import` mancante, che non
#     indebolisce nessuna specifica (e la guardia stessa, non lasciando altra
#     strada, spingeva il modello verso l'indebolimento di un'asserzione);
#   - quattro rifiuti su un file di test che l'agente aveva scritto lui stesso
#     cinque messaggi prima, su una specifica che l'utente non aveva dato: li'
#     non c'era nessuna specifica dell'utente da proteggere.
#
# Quindi la guardia oggi lavora a due livelli:
#   1. Esenzione per autore: i file di test creati dall'agente in questa
#      sessione non sono la specifica dell'utente, e restano suoi da correggere.
#   2. Livello asserzione: passa tutto cio' che aggiunge (import, fixture,
#      nuovi test, rinomine), si blocca solo cio' che toglie o cambia
#      un'asserzione gia' presente, o che marca un test come skip/xfail.
#
# L'uscita di sicurezza resta quella giusta: se il test e' davvero sbagliato,
# la decisione e' dell'utente, non dell'agente.
_TEST_FILE_RE = re.compile(r"(^|/)(test_[^/]+\.py|[^/]+_test\.py|conftest\.py)$", re.I)

# Cosa consideriamo "asserzione": l'assert nudo, gli helper di unittest e i
# blocchi pytest.raises/warns, che sono asserzioni a tutti gli effetti (togliere
# un `with pytest.raises(ValueError)` cambia la specifica esattamente come
# togliere un assert).
_ASSERT_RE = re.compile(
    r"^(assert\b|self\.assert[A-Za-z]*\s*\(|"
    r"(with\s+)?pytest\.(raises|warns|deprecated_call)\b|"
    r"(with\s+)?self\.assertRaises\b)"
)
_SKIP_MARKER_RE = re.compile(r"@\s*pytest\.mark\.(skip|skipif|xfail)\b")


# --- richieste di sola lettura ----------------------------------------------
# Osservato: "Analizza la complessita' di X, individua i problemi di precisione,
# suggerisci come ottimizzarla" -- tre verbi, tutti di sola lettura. Il modello
# ha scritto l'analisi e poi, nello stesso messaggio, "Implemento una versione
# ottimizzata", lasciando sul disco una funzione nuova e rotta che nessuno
# aveva chiesto. Il deliverable di una domanda e' una risposta.
#
# L'euristica sbaglia volentieri in una sola direzione: se c'e' anche solo il
# sospetto che l'utente voglia una modifica, non si blocca niente. Un falso
# negativo e' lo status quo; un falso positivo interrompe del lavoro legittimo.
_ANALYTIC_RE = re.compile(
    r"\b(analizza|analizzami|spiega|spiegami|spiegare|valuta|valutami|descrivi|"
    r"confronta|individua|suggerisci|consigli|illustra|riassumi|riassumimi|"
    r"commenta|esamina|revisiona|critica|"
    r"cosa\s+fa|come\s+funziona|quali\s+sono|che\s+differenza|perche'|perché|"
    r"analy[sz]e|explain|describe|compare|summari[sz]e|review|what\s+does)\b",
    re.I,
)

# Solo forme *imperative*: e' la distinzione che salva il caso reale. In
# "suggerisci come ottimizzarla" il verbo di modifica e' all'infinito, ed e'
# l'oggetto del suggerimento, non un ordine. "ottimizzala" invece lo e'.
_MUTATING_RE = re.compile(
    r"\b(crea|crealo|creala|scrivi|scrivimi|aggiungi|aggiungici|modifica|modificalo|"
    r"modificala|correggi|correggilo|correggila|sistemalo|sistemala|rinomina|"
    r"implementa|implementalo|implementala|rifattorizza|refactor|cancella|elimina|"
    r"rimuovi|sostituisci|ottimizza|ottimizzalo|ottimizzala|applica|aggiorna|"
    r"estendi|completa|genera|installa|rendi|trasforma|converti|migra|"
    r"fai\s+passare|falli\s+passare|fallo\s+passare|"
    r"create|write|add|fix|implement|refactor|rename|remove|delete|optimi[sz]e)\b",
    re.I,
)


def looks_like_readonly_request(text: str) -> bool:
    """La richiesta chiede di capire, non di cambiare?"""
    body = str(text or "")
    if not body.strip():
        return False
    return bool(_ANALYTIC_RE.search(body)) and not _MUTATING_RE.search(body)


# --- il banco di prova --------------------------------------------------------
# Vietare la scrittura durante un'analisi impedisce il danno, non l'errore.
# Misurato: bloccato dal divieto, il modello ha risposto a parole -- giusto -- ma
# nella risposta ha proposto una funzione che reintroduceva, in un blocco di
# codice, esattamente il bug che aveva appena passato quindici messaggi a
# correggere. Nessuno l'aveva eseguita, quindi nessuno se n'era accorto.
#
# Il codice dentro una risposta non e' verificato da niente. Qui gli si da' un
# posto dove provarlo: una cartella sua, dentro il workspace perche' il
# container monta solo quello, ma con il punto davanti -- e quindi gia' invisibile
# a list_files, search_files e all'albero nell'intestazione -- e svuotata
# all'inizio di ogni analisi. Cosi' il modello puo' proporre solo codice che ha
# visto girare, senza che il progetto dell'utente si sporchi.
SCRATCH_DIR = ".analisi"
# `*` dentro un .gitignore della cartella stessa: git la ignora per intero senza
# che si debba toccare il .gitignore del progetto, che e' roba dell'utente.
_SCRATCH_GITIGNORE = "*\n"


def normalise_rel(relative_path: str) -> str:
    """Percorso in forma canonica: separatori posix, senza './' iniziali.

    Non si usa ``lstrip("./")``: lstrip toglie *caratteri*, non un prefisso, e
    su '.analisi/prova.py' mangerebbe anche il punto della cartella.
    """
    path = str(relative_path or "").replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path.lstrip("/")


def is_scratch_path(relative_path: str) -> bool:
    return normalise_rel(relative_path).split("/")[0] == SCRATCH_DIR


def ensure_scratch(ctx: ToolContext) -> Path:
    """Crea il banco di prova se non c'e' ancora. Idempotente."""
    folder = ctx.base / SCRATCH_DIR
    folder.mkdir(parents=True, exist_ok=True)
    marker = folder / ".gitignore"
    if not marker.exists():
        try:
            marker.write_text(_SCRATCH_GITIGNORE, encoding="utf-8")
        except OSError:
            pass
    return folder


def reset_scratch(ctx: ToolContext) -> None:
    """Svuota il banco di prova. Chiamato all'inizio di ogni analisi.

    Il controllo sul nome non e' pedanteria: qui si cancella ricorsivamente, e
    l'unico modo per cui possa essere sicuro e' che il percorso sia davvero
    ``<workspace>/.analisi`` e non un simbolico che punta altrove.
    """
    folder = ctx.base / SCRATCH_DIR
    try:
        resolved = folder.resolve()
        if resolved.name != SCRATCH_DIR or resolved.parent != ctx.base.resolve():
            return
        if resolved.is_dir():
            shutil.rmtree(resolved)
    except OSError:
        pass


def _refuse_readonly_write(ctx: ToolContext, filepath: str) -> str | None:
    """Errore da restituire se si scrive il disco durante una richiesta di analisi."""
    if not ctx.readonly_request or is_scratch_path(filepath):
        return None
    return _err(
        f"La richiesta era di analizzare, non di cambiare: scrivere "
        f"'{filepath}' non e' quello che ti e' stato chiesto. Il risultato di "
        "una domanda e' una risposta.",
        hint=(
            f"Se vuoi provare un'idea prima di proporla, scrivila sotto "
            f"'{SCRATCH_DIR}/' ed eseguila: quella cartella e' tua, non fa parte "
            "del progetto e viene svuotata ad ogni analisi. Il codice che metti "
            "nella risposta e' meglio che tu l'abbia visto girare. Poi rispondi "
            "a parole: le modifiche al progetto le proponi nella riga 'Poi:', "
            "oppure le chiedi con ask_user_question."
        ),
    )


# --- copertura del codice nuovo ----------------------------------------------
# Osservato: il modello aggiunge `media_mobile_pesata_stream`, non le scrive
# nemmeno un test, lancia `pytest test_media_mobile.py` (che esercita tutt'altra
# funzione), ottiene 10 passed e dichiara "lavoro completato". La barra verde
# non misurava una riga del codice nuovo.
#
# E' la stessa frode dell'asserzione indebolita, per una strada che nessuna
# guardia copriva: non ha toccato nessun test, quindi non c'era niente da
# bloccare. Qui non si blocca: si nega la chiusura in silenzio.
_DEF_RE = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)", re.M)


def public_symbols(source: str) -> set[str]:
    """Nomi di funzioni e classi pubbliche definite in un sorgente."""
    return {n for n in _DEF_RE.findall(source or "") if not n.startswith("_")}


def record_new_symbols(ctx: ToolContext, relative_path: str, before: str, after: str) -> None:
    """Annota i simboli pubblici comparsi in un file di codice in questo turno."""
    if (
        looks_like_test_file(relative_path)
        or is_scratch_path(relative_path)
        or not relative_path.endswith(".py")
    ):
        return
    for name in public_symbols(after) - public_symbols(before):
        ctx.new_symbols[name] = relative_path


def uncovered_symbols(ctx: ToolContext) -> list[tuple[str, str]]:
    """(simbolo, file) aggiunti in questo turno che nessun test nomina.

    Cerca il nome per sottostringa nei file di test del workspace: grossolano,
    ma il falso positivo qui costa solo un sollecito in meno, mai uno di troppo.
    """
    if not ctx.new_symbols:
        return []
    testo = []
    try:
        for path in ctx.base.rglob("*.py"):
            try:
                rel = path.relative_to(ctx.base).as_posix()
            except ValueError:
                continue
            # Un test buttato giu' nel banco di prova non copre il progetto.
            if looks_like_test_file(rel) and not is_scratch_path(rel):
                try:
                    testo.append(path.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    continue
    except OSError:
        return []
    tutti_i_test = "\n".join(testo)
    return [(n, f) for n, f in sorted(ctx.new_symbols.items()) if n not in tutti_i_test]


# Comandi che *misurano* il progetto, distinti da quelli che lo ispezionano.
# `ls`, `cat`, `grep` tornano 0 anche quando non hanno verificato niente:
# trattarli come verifiche verdi farebbe chiudere il turno a meta' lavoro.
_VERIFICATION_RE = re.compile(
    r"\b("
    r"pytest|py_compile|unittest|nose2|tox|"
    r"ruff|flake8|pylint|mypy|pyright|black\s+--check|"
    r"npm\s+(run\s+)?(test|lint|build)|yarn\s+(test|lint|build)|pnpm\s+(test|lint|build)|"
    r"jest|vitest|eslint|tsc|"
    r"cargo\s+(test|check|clippy|build)|go\s+(test|vet|build)|"
    r"make\s+(test|check|lint|build)|gradle\s+test|mvn\s+test|dotnet\s+test"
    r")\b",
    re.I,
)


def looks_like_verification(command: str) -> bool:
    """Il comando misura il progetto (test, lint, type-check, build)?"""
    return bool(_VERIFICATION_RE.search(str(command or "")))


def looks_like_test_file(relative_path: str) -> bool:
    path = str(relative_path or "").replace("\\", "/")
    if _TEST_FILE_RE.search(path):
        return True
    # Anche un file qualunque dentro tests/ e' materiale di specifica.
    return any(part in {"tests", "test"} for part in path.split("/")[:-1])


def _logical_lines(source: str) -> list[str]:
    """Righe con le continuazioni ricongiunte, cosi' un assert su piu' righe
    resta una cosa sola.

    Non e' un parser: ``old_string`` e' un frammento, spesso non compilabile da
    solo, quindi ``ast`` non e' un'opzione. Bilanciare le parentesi e' pero'
    sufficiente per il caso che conta.
    """
    out: list[str] = []
    buffer = ""
    depth = 0
    for raw in (source or "").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            if depth == 0:
                continue
        buffer = f"{buffer} {stripped}".strip() if buffer else stripped
        depth += stripped.count("(") + stripped.count("[") + stripped.count("{")
        depth -= stripped.count(")") + stripped.count("]") + stripped.count("}")
        if depth <= 0 and not stripped.endswith("\\"):
            out.append(re.sub(r"\s+", " ", buffer))
            buffer, depth = "", 0
    if buffer:
        out.append(re.sub(r"\s+", " ", buffer))
    return out


def assertion_signatures(source: str) -> Counter[str]:
    """Multinsieme delle asserzioni presenti in un pezzo di codice.

    Il confronto e' testuale di proposito: cambiare il valore atteso di un
    assert *e'* cambiare la specifica, ed e' esattamente la mossa che vogliamo
    intercettare. Riformattare un assert conta come cambiarlo: e' un falso
    positivo raro, e il messaggio di rifiuto spiega come uscirne.
    """
    found: Counter[str] = Counter()
    for line in _logical_lines(source):
        if _ASSERT_RE.match(line):
            found[line] += 1
    return found


def _skip_markers(source: str) -> int:
    return len(_SKIP_MARKER_RE.findall(source or ""))


def _weakening(before: str, after: str) -> tuple[list[str], bool]:
    """(asserzioni perse, e' stato aggiunto uno skip/xfail)."""
    lost = assertion_signatures(before) - assertion_signatures(after)
    return list(lost.elements()), _skip_markers(after) > _skip_markers(before)


def _refuse_test_edit(
    ctx: ToolContext, filepath: str, before: str = "", after: str = ""
) -> str | None:
    """Errore da restituire se la modifica indebolisce un test dell'utente.

    ``before``/``after`` sono i due lati del confronto: per ``edit_file`` sono
    ``old_string``/``new_string``, per ``write_file`` il contenuto attuale del
    file e quello nuovo.
    """
    if not ctx.red_command or not looks_like_test_file(filepath):
        return None

    # Stessa chiave usata da ctx.touch, altrimenti './test_x.py' e 'test_x.py'
    # sembrerebbero due file diversi e l'esenzione non scatterebbe.
    try:
        rel = resolve_path(ctx.workspace, filepath).resolve().relative_to(ctx.base).as_posix()
    except (WorkspaceError, ValueError, OSError):
        rel = normalise_rel(filepath)

    if rel in ctx.authored_tests:
        # Test scritto dall'agente in questa sessione: non e' la specifica
        # dell'utente, e' materiale suo. Se si e' contraddetto da solo deve
        # poterlo sistemare, non restare in stallo.
        return None

    lost, skipped = _weakening(before, after)
    if not lost and not skipped:
        return None

    ctx.test_guard_refusals[rel] = ctx.test_guard_refusals.get(rel, 0) + 1
    tentativi = ctx.test_guard_refusals[rel]

    if skipped and not lost:
        motivo = (
            f"la modifica marca un test di '{filepath}' come skip/xfail mentre "
            f"la verifica `{ctx.red_command}` e' ancora rossa: spegnere un test "
            "non e' farlo passare."
        )
    else:
        elenco = "\n".join(f"  - {a[:160]}" for a in lost[:4])
        extra = f"\n  ... e altre {len(lost) - 4}" if len(lost) > 4 else ""
        motivo = (
            f"la modifica toglie o cambia {len(lost)} asserzione/i di "
            f"'{filepath}' mentre la verifica `{ctx.red_command}` e' ancora "
            f"rossa:\n{elenco}{extra}\n"
            "Cambiare cio' che il test si aspetta significa cambiare la "
            "specifica per farla combaciare con il codice."
        )

    if tentativi >= 2:
        hint = (
            f"E' la {tentativi}a volta che provi a indebolire questo test: "
            "quello che stai facendo non funzionera' al prossimo tentativo. "
            "Fermati adesso e usa ask_user_question, spiegando quale "
            "asserzione ritieni sbagliata e perche'. Non e' una decisione tua."
        )
    else:
        hint = (
            "Correggi il codice, non il test, poi riesegui lo stesso comando. "
            "Puoi comunque modificare questo file per aggiungere import, "
            "fixture o nuovi test: e' bloccato solo cio' che toglie o cambia "
            "un'asserzione esistente. Se sei convinto che sia il test a essere "
            "sbagliato, fermati e chiedilo all'utente con ask_user_question."
        )
    return _err(motivo, hint=hint)


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
# Tetti volutamente stretti: un'immagine costa molto piu' contesto del suo peso
# in byte, e su una scheda da 8 GB la finestra e' la risorsa scarsa.
MAX_IMAGES_IN_CONTEXT = 4
MAX_IMAGE_BYTES = 4 * 1024 * 1024


def is_image(name: str) -> bool:
    return Path(str(name or "")).suffix.lower() in IMAGE_SUFFIXES


def load_images_b64(
    workspace: str | Path,
    entries: list[dict[str, Any]],
    *,
    limit: int = MAX_IMAGES_IN_CONTEXT,
) -> tuple[list[str], list[str]]:
    """Allegati immagine pronti per il campo ``images`` di Ollama.

    Restituisce ``(immagini_base64, scartate)``. Si scartano silenziosamente
    solo i file illeggibili; quelle escluse per numero o dimensione tornano
    nella seconda lista, perche' l'utente deve sapere che non sono in contesto.
    """
    import base64

    taken: list[str] = []
    skipped: list[str] = []
    for entry in entries:
        name = str(entry.get("name") or "")
        if not is_image(name):
            continue
        if len(taken) >= limit:
            skipped.append(name)
            continue
        try:
            path = resolve_path(workspace, str(entry.get("path") or ""))
            with path.open("rb") as stream:
                data = stream.read(MAX_IMAGE_BYTES + 1)
        except (WorkspaceError, OSError):
            skipped.append(name)
            continue
        if len(data) > MAX_IMAGE_BYTES:
            skipped.append(name)
            continue
        taken.append(base64.b64encode(data).decode("ascii"))
    return taken, skipped


def resolve_path(workspace: str | Path, target: str) -> Path:
    """Risolve ``target`` dentro ``workspace``, rifiutando ogni evasione.

    Corregge il bug della versione precedente, che usava
    ``str(target).startswith(str(base))``: con base ``/work`` un path
    ``/workspace-altrui`` superava il controllo. Qui si usa
    ``Path.is_relative_to``, che confronta i componenti del path.
    """
    if not isinstance(target, str) or "\0" in target:
        raise WorkspaceError("Percorso non valido: usa una stringa senza caratteri NUL.")
    base = Path(workspace).resolve()
    raw = (target or ".").strip().replace("\\", "/")
    # NTFS alternate data streams bypass ordinary file semantics, and reserved
    # device names can refer to devices even under an otherwise confined path.
    if os.name == "nt":
        relative_part = raw[2:] if re.match(r"^[A-Za-z]:", raw) else raw
        if ":" in relative_part or any(
            re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part, re.I)
            for part in relative_part.split("/")
        ):
            raise WorkspaceError("Percorso non valido: stream NTFS e dispositivi Windows vietati.")

    candidate = Path(raw)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (base / candidate).resolve()

    # Un solo controllo: ``resolved != base`` era morto, perche'
    # ``base.is_relative_to(base)`` e' gia' vero e la seconda clausola copre da
    # sola anche il caso della radice. Due condizioni per una domanda sola su un
    # controllo di sicurezza sono peggio di una: chi legge si chiede quale delle
    # due sta facendo il lavoro.
    if not resolved.is_relative_to(base):
        raise WorkspaceError(
            f"Accesso negato: '{target}' e' fuori dal workspace ({base}). "
            "Usa esclusivamente percorsi relativi alla radice del workspace."
        )
    return resolved


def _read_text_bounded(path: Path, *, errors: str = "replace") -> str:
    """Read only regular, bounded files; growth during the read remains bounded."""
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise WorkspaceError("Il percorso non e' un file regolare.")
    if metadata.st_size > MAX_TEXT_FILE_BYTES:
        raise WorkspaceError(
            f"Il file supera il limite di {MAX_TEXT_FILE_BYTES // (1024 * 1024)} MiB.",
            hint="Usa un comando mirato per estrarre una porzione in un file piu' piccolo.",
        )
    with path.open("rb") as stream:
        data = stream.read(MAX_TEXT_FILE_BYTES + 1)
    if len(data) > MAX_TEXT_FILE_BYTES:
        raise WorkspaceError("Il file e' cresciuto oltre il limite durante la lettura.")
    return data.decode("utf-8", errors=errors).replace("\r\n", "\n").replace("\r", "\n")


def _is_probably_binary(path: Path) -> bool:
    if path.suffix.lower() in BINARY_SUFFIXES:
        return True
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            return True
        with open(path, "rb") as fh:
            return b"\0" in fh.read(2048)
    except OSError:
        return False


def _ok(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _err(message: str, *, hint: str = "") -> str:
    payload: dict[str, Any] = {"error": message}
    if hint:
        payload["hint"] = hint
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Implementazioni
# ---------------------------------------------------------------------------


def _da_saltare(nome: str) -> bool:
    """Voci che nessun walk dell'harness mostra al modello.

    Vale per ``list_files``, ``find``, ``grep_files`` e l'header d'ambiente. I
    quattro seguivano tre politiche diverse: ``list_files`` aveva
    ``nome in IGNORED_DIRS or nome.startswith(".") and entry.is_dir()``, che per
    la precedenza di ``and`` su ``or`` significa "salta i dot-file solo se sono
    anche cartelle" -- cioe' mai. I ``.env`` e i ``.vault.json`` finivano
    nell'elenco mentre l'header d'ambiente li nascondeva: due risposte diverse
    alla stessa domanda, date allo stesso modello nello stesso turno.
    """
    return nome in IGNORED_DIRS or nome.startswith(".")


def _cerca_per_nome(ctx: ToolContext, root: Path, subfolder: str, pattern: str) -> str:
    """Percorsi che corrispondono a un modello di nome, a qualunque profondita'.

    Ordinati per data di modifica, i piu' recenti prima: su un progetto vero
    "l'ultimo file che ho toccato" e' quasi sempre quello che interessa, e
    metterlo in cima significa che il troncamento taglia via il resto.
    """
    trovati: list[tuple[float, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _da_saltare(d)]
        for name in filenames:
            if _da_saltare(name) or not fnmatch.fnmatch(name, pattern):
                continue
            fpath = Path(dirpath) / name
            try:
                mtime = fpath.stat().st_mtime
            except OSError:
                mtime = 0.0
            trovati.append((mtime, fpath.relative_to(root).as_posix()))
    trovati.sort(reverse=True)
    tetto = ctx.budgets.list_files_max_entries
    percorsi = [rel for _, rel in trovati[:tetto]]
    return _ok(
        {
            "subfolder": subfolder,
            "pattern": pattern,
            "match_count": len(trovati),
            "truncated": len(trovati) > tetto,
            "files": percorsi or ["(nessun file corrisponde)"],
        }
    )


def tool_list_files(
    ctx: ToolContext, subfolder: str = ".", depth: int = 2, pattern: str = ""
) -> str:
    """Albero compatto della cartella, ignorando rumore e binari.

    Con ``pattern`` cambia mestiere: invece dell'albero restituisce i percorsi
    che corrispondono a quel modello di nome, cercati a **qualunque
    profondita'**. E' la domanda "dove sono i test?" o "quali file .csv ci sono
    qui dentro", che senza questa strada si risponde solo camminando l'albero a
    mano e indovinando -- cioe' con piu' chiamate e piu' contesto speso per
    scartare quello che non interessa.
    """
    try:
        root = resolve_path(ctx.workspace, subfolder)
    except WorkspaceError as exc:
        return _err(str(exc))
    if not root.exists():
        return _err(
            f"La cartella '{subfolder}' non esiste.",
            hint="Chiama list_files con subfolder='.' per vedere la radice.",
        )
    if root.is_file():
        return _err(
            f"'{subfolder}' e' un file, non una cartella.",
            hint="Usa read_file per leggerne il contenuto.",
        )

    if pattern:
        return _cerca_per_nome(ctx, root, subfolder, pattern)

    depth = max(1, min(int(depth or 1), 4))
    lines: list[str] = []
    n_entries = 0
    truncated = False

    def walk(directory: Path, prefix: str, level: int) -> None:
        nonlocal n_entries, truncated
        if level > depth or truncated:
            return
        try:
            entries = sorted(
                directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower())
            )
        except OSError:
            return
        for entry in entries:
            if n_entries >= ctx.budgets.list_files_max_entries:
                truncated = True
                return
            if _da_saltare(entry.name):
                continue
            n_entries += 1
            if entry.is_dir():
                lines.append(f"{prefix}{entry.name}/")
                walk(entry, prefix + "  ", level + 1)
            else:
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = 0
                lines.append(f"{prefix}{entry.name}  ({size:,} B)")

    walk(root, "", 1)
    tree = "\n".join(lines) if lines else "(cartella vuota)"
    return _ok(
        {
            "folder": subfolder,
            "depth": depth,
            "entries": n_entries,
            "truncated": truncated,
            "tree": truncate_lines(tree, ctx.budgets.list_files_max_entries, label="albero file"),
        }
    )


def tool_read_file(
    ctx: ToolContext,
    filepath: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    """Legge un file, opzionalmente un intervallo di righe (risparmia contesto)."""
    try:
        path = resolve_path(ctx.workspace, filepath)
    except WorkspaceError as exc:
        return _err(str(exc))
    if not path.exists():
        return _err(
            f"Il file '{filepath}' non esiste.",
            hint="Chiama list_files per verificare il nome esatto prima di riprovare.",
        )
    if path.is_dir():
        return _err(f"'{filepath}' e' una cartella.", hint="Usa list_files.")
    if _is_probably_binary(path):
        return _err(
            f"'{filepath}' sembra un file binario e non e' leggibile come testo."
        )

    try:
        text = _read_text_bounded(path)
    except (OSError, WorkspaceError) as exc:
        return _err(f"Impossibile leggere '{filepath}': {exc}")

    # Rilettura di un file non toccato: e' il caso piu' frequente del ciclo
    # leggi -> modifica -> verifica -> rileggi, e rimandare 6.000 caratteri
    # identici a quelli di tre passi fa e' il singolo spreco piu' grosso che
    # resta. Trenta token bastano a dire "e' quello di prima".
    #
    # Si risponde cosi' solo per una lettura **intera** e solo se il contenuto
    # e' ancora nel contesto: con start_line/end_line il modello sta chiedendo
    # una porzione diversa, e dopo una compattazione il testo non c'e' piu'.
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
    rel_key = path.resolve().as_posix()
    seen = ctx.read_cache.get(rel_key)
    still_in_context = (
        seen is not None
        and seen[0] == digest
        and ctx.step - seen[1] < ctx.budgets.tool_result_full_window
    )
    if not (start_line or end_line) and still_in_context:
        ctx.touch(path)
        return _ok(
            {
                "filepath": filepath,
                "status": "invariato",
                "note": (
                    "Contenuto identico alla lettura precedente in questa "
                    "conversazione: usa quello, e' ancora valido."
                ),
            }
        )
    ctx.read_cache[rel_key] = (digest, ctx.step)

    # ``splitlines`` e non ``count("\n") + 1``: quest'ultimo conta una riga in
    # piu' per ogni file che finisce con un a capo -- cioe' per quasi tutti. E'
    # il numero su cui il modello calcola ``start_line``/``end_line`` per la
    # lettura successiva, quindi sbagliarlo di uno significa chiedere una riga
    # che non esiste e ricevere un intervallo vuoto.
    total_lines = len(text.splitlines())
    sliced = False
    if start_line or end_line:
        all_lines = text.splitlines()
        lo = max(int(start_line or 1), 1) - 1
        hi = min(int(end_line or len(all_lines)), len(all_lines))
        text = "\n".join(all_lines[lo:hi])
        sliced = True

    truncated_text = smart_truncate(
        text, ctx.budgets.read_file_max_chars, label=f"contenuto di {filepath}"
    )
    rel = ctx.touch(path)
    ctx.known_files.add(rel)
    return _ok(
        {
            "filepath": filepath,
            "total_lines": total_lines,
            "range": f"{start_line or 1}-{end_line or total_lines}" if sliced else "intero file",
            "truncated": len(truncated_text) != len(text),
            "content": truncated_text,
        }
    )


def tool_write_file(ctx: ToolContext, filepath: str, content: str = "") -> str:
    """Crea o sovrascrive integralmente un file."""
    refusal = _refuse_readonly_write(ctx, filepath)
    if refusal:
        return refusal
    if not filepath or not str(filepath).strip():
        return _err(
            "Parametro 'filepath' mancante.",
            hint="write_file richiede sia 'filepath' sia 'content'.",
        )
    try:
        path = resolve_path(ctx.workspace, filepath)
    except WorkspaceError as exc:
        return _err(str(exc))

    existed = path.exists()
    if existed:
        try:
            rel_existing = path.resolve().relative_to(ctx.base).as_posix()
        except ValueError:
            rel_existing = str(path)
        if rel_existing not in ctx.known_files:
            # "Guarda prima di agire", reso obbligatorio invece che consigliato.
            # Sovrascrivere un file mai letto e' l'unico modo in cui l'agente
            # puo' distruggere lavoro esistente in un colpo solo, ed e' proprio
            # cio' che un modello piccolo fa quando ha fretta.
            return _err(
                f"'{filepath}' esiste gia' e non l'hai ancora letto in questa "
                "conversazione: sovrascriverlo ne cancellerebbe il contenuto.",
                hint=(
                    f"Chiama prima read_file su '{filepath}'. Se ti serve solo "
                    "cambiare una parte, usa edit_file: e' piu' sicuro e costa "
                    "molti meno token."
                ),
            )

    # Il confronto e' fra le asserzioni gia' sul disco e quelle nel contenuto
    # nuovo: riscrivere un file di test perdendone dei pezzi e' la variante
    # piu' distruttiva della mossa che vogliamo impedire.
    previous_text = ""
    if existed:
        try:
            # ``errors="replace"``: il ``except OSError`` qui attorno non
            # copre ``UnicodeDecodeError``, che e' una ValueError. Un file
            # salvato in latin-1 faceva uscire l'eccezione dal tool e arrivare
            # al dispatch come "errore interno".
            previous_text = _read_text_bounded(path)
        except (OSError, WorkspaceError) as exc:
            return _err(f"Impossibile verificare il contenuto prima di scrivere '{filepath}': {exc}")
    refusal = _refuse_test_edit(ctx, filepath, previous_text, content or "")
    if refusal:
        return refusal

    try:
        previous_size = path.stat().st_size if existed else 0
        if is_scratch_path(filepath):
            # Crea la cartella con il suo .gitignore: il banco di prova non deve
            # mai comparire in `git status` del progetto dell'utente.
            ensure_scratch(ctx)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic.write_text(path, content or "")
    except OSError as exc:
        return _err(f"Scrittura fallita su '{filepath}': {exc}")

    rel = ctx.touch(path)
    ctx.known_files.add(rel)
    record_new_symbols(ctx, rel, previous_text, content or "")
    if not existed and looks_like_test_file(rel):
        # Da qui in poi questo test e' roba sua: puo' correggerlo.
        ctx.authored_tests.add(rel)
    return _ok(
        {
            "status": "ok",
            "action": "sovrascritto" if existed else "creato",
            "filepath": rel,
            "bytes_before": previous_size,
            "bytes_after": len((content or "").encode("utf-8")),
            "sha256": hashlib.sha256((content or "").encode("utf-8")).hexdigest(),
            "lines": (content or "").count("\n") + 1,
        }
    )


def tool_edit_file(
    ctx: ToolContext,
    filepath: str,
    old_string: str,
    new_string: str = "",
    replace_all: bool = False,
) -> str:
    """Sostituzione mirata: molto piu' economica di riscrivere il file intero.

    Senza ``replace_all`` la sostituzione deve essere univoca, ed e' la
    guardia giusta per una modifica chirurgica: due occorrenze vogliono dire
    che il frammento non identifica il punto. Ma per una rinomina la stessa
    guardia costa un round-trip per ogni occorrenza -- otto chiamate per un
    simbolo che compare otto volte -- e allargare il frammento per renderlo
    unico e' lavoro inutile per un'operazione che voleva colpire tutte.
    """
    refusal = _refuse_readonly_write(ctx, filepath)
    if refusal:
        return refusal
    try:
        path = resolve_path(ctx.workspace, filepath)
    except WorkspaceError as exc:
        return _err(str(exc))
    if not path.exists():
        return _err(
            f"Il file '{filepath}' non esiste.",
            hint="Usa write_file per crearlo da zero.",
        )
    if not old_string:
        return _err(
            "Parametro 'old_string' mancante o vuoto.",
            hint="Per creare un file nuovo usa write_file.",
        )

    try:
        # Stessa ragione della lettura in edit_file: UnicodeDecodeError non e'
        # una OSError e sfuggiva al ramo d'errore di questo tool.
        text = _read_text_bounded(path)
    except (OSError, WorkspaceError) as exc:
        return _err(f"Impossibile leggere '{filepath}': {exc}")

    occurrences = text.count(old_string)
    if occurrences == 0:
        return _err(
            f"'old_string' non trovato in '{filepath}'.",
            hint=(
                "Rileggi il file con read_file e copia il testo esatto, "
                "inclusi indentazione e a capo."
            ),
        )
    if occurrences > 1 and not replace_all:
        return _err(
            f"'old_string' compare {occurrences} volte in '{filepath}': la modifica "
            "sarebbe ambigua.",
            hint=(
                "Allarga old_string con righe di contesto per renderlo unico, "
                "oppure passa replace_all=true se le volevi cambiare tutte."
            ),
        )

    # Il confronto e' fra il file prima e il file dopo, non fra i due frammenti:
    # una edit chirurgica come old_string="== 3" / new_string="is not None"
    # indebolisce un assert senza che nessuno dei due frammenti *sia* un assert.
    quante = occurrences if replace_all else 1
    nuovo_testo = text.replace(old_string, new_string or "", quante)
    refusal = _refuse_test_edit(ctx, filepath, text, nuovo_testo)
    if refusal:
        return refusal

    try:
        atomic.write_text(path, nuovo_testo)
    except OSError as exc:
        return _err(f"Scrittura fallita su '{filepath}': {exc}")

    rel = ctx.touch(path)
    ctx.known_files.add(rel)
    record_new_symbols(ctx, rel, text, nuovo_testo)
    return _ok(
        {
            "status": "ok",
            "action": "modificato",
            "sha256": hashlib.sha256(nuovo_testo.encode("utf-8")).hexdigest(),
            "filepath": rel,
            "replacements": quante,
            # Righe del frammento cercato e di quello messo al suo posto,
            # moltiplicate per le sostituzioni: non e' un diff, e i nomi
            # ``removed_lines``/``added_lines`` lo facevano credere. Chi legge
            # e' il modello, e su un nome cosi' avrebbe smesso di guardare il
            # file per fidarsi del conteggio.
            "righe_del_vecchio": len(old_string.splitlines()) * quante,
            "righe_del_nuovo": len((new_string or "").splitlines()) * quante,
        }
    )


# Quante righe di contesto al massimo attorno a una corrispondenza. Oltre,
# tanto vale leggere il file: il contesto serve a capire *se* la corrispondenza
# e' quella giusta, non a sostituire read_file.
MAX_CONTESTO = 4
# Quante corrispondenze oltre il tetto si raccolgono per il deposito. Non e'
# un budget di contesto -- quelle in contesto restano `search_max_matches` --
# ma il punto in cui si smette di leggere il disco per una ricerca che il
# modello ha comunque posto troppo larga.
MAX_OLTRE_TETTO = 20_000


def tool_search_files(
    ctx: ToolContext,
    pattern: str,
    glob: str = "*",
    subfolder: str = ".",
    output_mode: str = "content",
    context_lines: int = 0,
) -> str:
    """Ricerca testuale ricorsiva dentro il workspace.

    ``output_mode`` decide quanto costa la risposta, e la differenza non e'
    cosmetica: nelle sessioni misurate di questo progetto i risultati dei tool
    sono il 44-51% del contesto inviato al modello. Alla domanda "quali file
    nominano X" la risposta utile sono sei percorsi, non sessanta righe di
    codice che il modello dovra' comunque rileggere per intero quando aprira'
    il file giusto.

    * ``files``   -- solo i percorsi che contengono almeno una corrispondenza;
    * ``count``   -- percorso e quante volte, per decidere da dove cominciare;
    * ``content`` -- le righe (come prima), con eventuale contesto attorno.
    """
    if not pattern:
        return _err("Parametro 'pattern' mancante.")
    try:
        root = resolve_path(ctx.workspace, subfolder)
    except WorkspaceError as exc:
        return _err(str(exc))

    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        return _err(f"Regex non valida: {exc}", hint="Usa una ricerca letterale semplice.")

    modo = (output_mode or "content").strip().lower()
    if modo not in {"files", "count", "content"}:
        return _err(
            f"output_mode '{output_mode}' non valido.",
            hint="Valori ammessi: files (solo i percorsi), count, content.",
        )
    contesto = max(0, min(int(context_lines or 0), MAX_CONTESTO))
    # Nelle modalita' compatte il tetto conta *file*, non righe: sessanta
    # percorsi sono un elenco leggibile, sessanta righe di codice no.
    tetto = ctx.budgets.search_max_matches

    matches: list[str] = []
    # Le corrispondenze oltre il tetto. Si raccolgono **solo** con il deposito
    # acceso: senza, continuare il walk dopo il tetto sarebbe I/O pagato per
    # buttare via il risultato. Con il deposito acceso invece il walk prosegue,
    # e quello che oggi il tetto fa sparire finisce su disco.
    oltre: list[str] = []
    # Corrispondenze viste oltre il tetto, depositate o no: e' cio' che
    # distingue "completo" da "troncato".
    scartate = 0
    per_file: dict[str, int] = {}
    files_scanned = 0
    troncato = False
    continua = bool(ctx.deposito_attivo)
    stop = False

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _da_saltare(d)]
        for name in sorted(filenames):
            if _da_saltare(name) or not fnmatch.fnmatch(name, glob or "*"):
                continue
            fpath = Path(dirpath) / name
            # os.walk does not follow directory links, but its file entries can
            # themselves be links. Resolve before even probing binary content.
            try:
                fpath = resolve_path(ctx.workspace, str(fpath))
                if not fpath.is_file():
                    continue
            except (WorkspaceError, OSError, ValueError):
                continue
            if _is_probably_binary(fpath):
                continue
            files_scanned += 1
            rel = (Path(dirpath) / name).relative_to(root).as_posix()
            try:
                righe = _read_text_bounded(fpath, errors="ignore").splitlines()
            except (OSError, WorkspaceError):
                continue
            for i, line in enumerate(righe):
                if not regex.search(line):
                    continue
                per_file[rel] = per_file.get(rel, 0) + 1
                if modo == "content":
                    if contesto:
                        inizio = max(0, i - contesto)
                        fine = min(len(righe), i + contesto + 1)
                        voce = "\n".join(
                            f"{rel}:{n + 1}:{'>' if n == i else ' '} {righe[n].rstrip()[:200]}"
                            for n in range(inizio, fine)
                        )
                    else:
                        voce = f"{rel}:{i + 1}: {line.rstrip()[:200]}"
                    if len(matches) < tetto:
                        matches.append(voce)
                    else:
                        # Vista e non entrata: e' questa che dice se il
                        # risultato e' davvero incompleto.
                        scartate += 1
                        if continua and len(oltre) < MAX_OLTRE_TETTO:
                            oltre.append(voce)
                if modo == "files":
                    break  # basta sapere che il file contiene qualcosa
            # "Troncato" vuol dire che qualcosa e' rimasto fuori, non che il
            # tetto e' stato raggiunto: con **esattamente** ``tetto``
            # corrispondenze e niente oltre il risultato e' completo, e
            # dichiararlo troncato mandava il modello a cercare un resto che
            # non c'era. Il conto delle scartate distingue i due casi anche col
            # deposito spento, dove ``oltre`` resta vuota per costruzione.
            if modo == "content" and scartate:
                troncato = True
                # Senza deposito ci si ferma qui, come si e' sempre fatto. Con
                # il deposito si prosegue, ma non all'infinito: oltre il tetto
                # del deposito ci si ferma e **lo si dice**, perche' un file
                # che finisce a meta' senza avvisare e' una frase falsa in piu'.
                stop = not continua or len(oltre) >= MAX_OLTRE_TETTO
            if modo != "content" and len(per_file) >= tetto:
                troncato = True
                stop = not continua or len(per_file) >= tetto + MAX_OLTRE_TETTO
            if stop:
                break
        if stop:
            break

    comune = {
        "pattern": pattern,
        "glob": glob,
        "files_scanned": files_scanned,
        "file_count": len(per_file),
        "truncated": troncato,
    }

    def _deposita_ricerca(righe_intere: list[str]) -> None:
        """Il risultato intero su disco, e il percorso nel referto.

        Qui la perdita e' meno grave che su ``run_command`` -- una ricerca si
        rilancia, e' deterministica -- ma il costo di rilanciarla su un
        progetto grosso lo paga il modello in un round-trip, e la porzione
        tagliata e' proprio quella che non ha ancora visto.
        """
        # Il taglio e' gia' accertato da ``troncato``: qui non si ricontrolla
        # contro un budget, si deposita e basta. (Passare per
        # ``_deposita_se_tagliato`` con un tetto finto avrebbe funzionato solo
        # per caso, ed e' la specie di trucco che fra sei mesi non si rilegge.)
        if not troncato or not ctx.deposito_attivo:
            return
        percorso = deposito_mod.deposita(
            ctx.base,
            "\n".join(righe_intere),
            etichetta=f"search {pattern}",
            intestazione=(
                f"search_files: pattern={pattern!r} glob={glob!r} modo={modo} "
                f"-- {len(righe_intere):,} righe, {len(per_file):,} file"
            ),
        )
        if percorso:
            comune["deposito"] = percorso
            comune["nota"] = (
                f"In contesto ci sono le prime {tetto}. L'elenco intero e' in "
                f"`{percorso}`: leggilo con read_file o restringi la ricerca."
            )

    if modo == "files":
        # Niente match_count qui: in questa modalita' si smette di contare alla
        # prima corrispondenza di ogni file, e un totale ricavato cosi' sarebbe
        # un numero sbagliato spacciato per una misura.
        tutti = sorted(per_file)
        _deposita_ricerca(tutti)
        return _ok({**comune, "files": tutti[:tetto] or ["(nessuna corrispondenza)"]})
    comune["match_count"] = sum(per_file.values())
    if modo == "count":
        conteggi = [f"{f}: {n}" for f, n in sorted(per_file.items())]
        _deposita_ricerca(conteggi)
        return _ok({**comune, "counts": conteggi[:tetto] or ["(nessuna corrispondenza)"]})
    _deposita_ricerca(matches + oltre)
    return _ok({**comune, "matches": matches or ["(nessuna corrispondenza)"]})


# Quanti rossi identici prima di mettere in dubbio la verifica stessa. Tre e
# non uno: sui primi due il consiglio giusto resta "correggi il codice", ed e'
# quello che salva la maggior parte dei casi. Il dubbio sul test e' l'ultima
# ipotesi da fare, non la prima -- altrimenti si autorizza la scorciatoia
# peggiore, indebolire l'asserzione per veder verde.
ROSSI_PRIMA_DI_DUBITARE = 3

# Segni che il fallimento e' un'asserzione e non un errore di esecuzione. Su un
# ImportError o un SyntaxError non c'e' nessuna asserzione da rileggere.
_SEGNI_ASSERZIONE = ("assertionerror", "assert ", "fail:", "failed:", "assertion")


def file_dell_agente(ctx: ToolContext, *testi: str) -> str | None:
    """Il primo file scritto dall'agente che compare nei testi passati.

    Si guarda ``touched_files`` e non ``authored_tests`` di proposito: quella
    lista contiene solo i file che *sembrano* test per convenzione di nome
    (``test_*.py``, cartella ``tests/``), e nel caso che ha motivato questa
    guardia lo script di verifica si chiamava ``prova.py`` -- scritto
    dall'agente, eseguito dall'agente, e invisibile a quella convenzione.
    Quello che conta qui non e' come si chiama il file: e' che l'ha scritto lui,
    quindi le sue asserzioni sono ipotesi sue e non specifica di nessuno.
    """
    for testo in testi:
        if not testo:
            continue
        for rel in sorted(ctx.touched_files, key=len, reverse=True):
            if rel and (rel in testo or rel.rsplit("/", 1)[-1] in testo):
                return rel
    return None


def dubita_della_verifica(
    ctx: ToolContext, command: str, uscita: str
) -> str | None:
    """Il messaggio da dare quando lo stesso rosso si ripete e il test e' suo.

    Il ciclo che questa funzione interrompe e' documentato: un modello ha speso
    diciassette passi a riscrivere un file di rapporto perche' la sua stessa
    asserzione cercava ``marte.txt.txt`` -- il nome nel CSV aveva gia'
    l'estensione e lui ci aggiungeva la seconda. Ha riletto il sorgente del
    test tre volte senza vederlo, perche' l'harness continuava a ripetergli
    "correggi il codice, non passare ad altro". Aveva ragione due volte su tre;
    la terza no.
    """
    if ctx.comandi_falliti.get(command, 0) < ROSSI_PRIMA_DI_DUBITARE:
        return None
    if not any(segno in uscita.lower() for segno in _SEGNI_ASSERZIONE):
        return None
    rel = file_dell_agente(ctx, command, uscita)
    if not rel:
        return None
    return (
        f"Terzo fallimento identico su questo comando. `{rel}` l'hai scritto tu "
        "in questa conversazione: le sue asserzioni sono ipotesi tue, non la "
        "specifica dell'utente. Prima di toccare ancora il codice, rileggi "
        "l'asserzione che fallisce CARATTERE PER CARATTERE e confrontala con i "
        "dati veri -- stampa i valori che confronta invece di dedurli. Se e' "
        "l'asserzione a essere sbagliata, correggila: e' tua. Se e' giusta, il "
        "bug e' nel codice, e lo stesso errore tre volte dice che lo stai "
        "cercando nel posto sbagliato: isola il problema con un comando piu' "
        "piccolo invece di riprovare."
    )


def tool_run_command(ctx: ToolContext, command: str, timeout_sec: int | None = None) -> str:
    """Esegue un comando nella shell del workspace."""
    if not command or not command.strip():
        return _err("Parametro 'command' mancante.")

    if not ctx.allow_dangerous_commands:
        blocco = motivo_del_blocco(
            command, sandbox=ctx.sandbox, workdir=sandbox_mod.WORKDIR
        )
        if blocco is not None:
            return _err(blocco[0], hint=blocco[1])

    timeout = int(timeout_sec or ctx.timeout_s)
    started = time.monotonic()

    if ctx.sandbox == "docker":
        try:
            result = sandbox_mod.run(
                command,
                ctx.workspace,
                timeout_s=timeout,
                image=ctx.docker_image,
                network=ctx.sandbox_network,
                ports=ctx.preview_ports,
            )
        except sandbox_mod.SandboxError as exc:
            # Non si ripiega sull'host: sarebbe il buco che la sandbox chiude.
            return _err(
                f"Sandbox Docker non utilizzabile: {exc}",
                hint=(
                    "Avvia Docker Desktop e riprova. Se vuoi davvero eseguire i "
                    "comandi direttamente sulla macchina, cambia 'Sandbox' in "
                    "'host' nelle impostazioni: l'agente vedra' tutto il disco."
                ),
            )
        if result.timed_out:
            # Su un comando che *non finisce mai per progetto* il timeout non
            # e' un errore da riparare: e' il tool sbagliato. Senza questo
            # suggerimento il modello spende tre passi ad "aggiustare" un
            # server che stava funzionando benissimo.
            if looks_like_server(command):
                return _err(
                    f"`{command.strip()[:80]}` e' stato ucciso dopo {timeout}s, "
                    "ma e' un processo che non termina per natura.",
                    hint=(
                        "run_command aspetta la fine del comando, quindi non va "
                        "bene per i server. Usa il tool preview con "
                        "action='serve', che lo lascia in background e lo mostra "
                        "all'utente. Ricordati di legarlo a 0.0.0.0."
                    ),
                )
            return _err(
                f"Comando interrotto dopo {timeout}s (timeout).",
                hint="Esegui una variante piu' rapida o aumenta il timeout nelle impostazioni.",
            )
        proc = result
    else:
        # Stessa scelta della sandbox: la radice del workspace sul path di
        # import, cosi' uno script eseguito da una sottocartella (una prova in
        # `.analisi/`) vede i moduli del progetto.
        env = dict(os.environ)
        precedente = env.get("PYTHONPATH")
        env["PYTHONPATH"] = (
            f"{ctx.base}{os.pathsep}{precedente}" if precedente else str(ctx.base)
        )
        try:
            proc = run_bounded(
                command,
                shell=True,
                cwd=str(ctx.base),
                env=env,
                timeout=timeout,
            )
        except OutputLimitExceeded as exc:
            return _err(str(exc), hint="Riduci l'output e controlla gli effetti prima di ripetere il comando.")
        except subprocess.TimeoutExpired:
            return _err(
                f"Comando interrotto dopo {timeout}s (timeout).",
                hint="Esegui una variante piu' rapida o aumenta il timeout nelle impostazioni.",
            )
        except OSError as exc:
            return _err(f"Esecuzione fallita: {exc}")

    elapsed = time.monotonic() - started
    failed = proc.returncode != 0
    if failed:
        ctx.comandi_falliti[command] = ctx.comandi_falliti.get(command, 0) + 1
    else:
        ctx.comandi_falliti.pop(command, None)
    payload: dict[str, Any] = {
        # Un modello piccolo non deduce "e' andata male" da returncode: 1.
        # Glielo si dice a parole, insieme a cosa fare adesso.
        "esito": "FALLITO" if failed else "ok",
        "command": command,
        "returncode": proc.returncode,
        "duration_s": round(elapsed, 2),
    }
    if proc.returncode in (126, 127):
        # Non e' il progetto a essere rotto, e' il comando a non esistere.
        # Dirgli "correggi con edit_file e riesegui" lo manderebbe a cercare un
        # bug che non c'e'.
        payload["esito"] = "COMANDO NON ESEGUIBILE"
        payload["next_step"] = (
            "Questo comando non esiste nel container (exit "
            f"{proc.returncode}). Non e' un test fallito: non cercare bug nel "
            "codice. Installa lo strumento che manca, oppure usa un comando "
            "equivalente che ci sia."
        )
    elif failed:
        # Il dubbio sulla verifica scavalca il "correggi e riesegui": sono due
        # consigli incompatibili, e dopo tre rossi identici e' il secondo ad
        # aver esaurito il suo credito.
        payload["next_step"] = dubita_della_verifica(
            ctx, command, f"{proc.stdout or ''}\n{proc.stderr or ''}"
        ) or (
            "La verifica NON passa. Leggi stderr qui sotto, individua il file e "
            "la riga, correggi con edit_file, poi riesegui esattamente questo "
            "stesso comando. Non passare ad altro e non dichiarare il lavoro "
            "finito finche' non torna esito 'ok'."
        )
    elif looks_like_verification(command):
        # Una verifica verde e' il punto in cui il turno di solito finisce, ed
        # e' anche il punto in cui il modello smette di scrivere: chiude in
        # silenzio e tocca all'harness spendere un'intera chiamata in piu' per
        # farsi dare il riepilogo (SUMMARY_NUDGE, scattato 3 volte su 3 turni
        # nelle sessioni misurate). La regola sta nel system prompt, ma dopo
        # dieci risultati di tool non pesa piu' niente: qui arriva nel punto e
        # nel momento in cui serve, e costa poche decine di token invece di un
        # round-trip. Il condizionale e' voluto: la verifica verde non implica
        # che il lavoro richiesto sia finito.
        payload["next_step"] = (
            "Verifica verde. Se il lavoro chiesto e' finito, chiudi adesso il "
            "turno con un messaggio di testo -- senza altre chiamate a tool -- "
            "in tre righe: 'Fatto:' cosa hai cambiato e in quali file, "
            "'Verifica:' questo comando e il suo esito, 'Poi:' la mossa "
            "successiva che proponi. Se invece manca ancora qualcosa di quello "
            "che ti e' stato chiesto, prosegui senza riepilogare."
        )
    # Il testo intero su disco **prima** del taglio. Qui, e non su read_file,
    # perche' qui la perdita e' irreversibile: un file si rilegge a righe, uno
    # stdout tagliato si recupera solo rilanciando il comando -- che costa e
    # non sempre e' ripetibile. E' anche il 32,7% dei token di risultato dei
    # tool, secondo solo a read_file.
    out_intero = proc.stdout or ""
    err_intero = proc.stderr or ""
    dep_out = _deposita_se_tagliato(
        ctx,
        out_intero,
        tetto=ctx.budgets.command_stdout_max_chars,
        etichetta=f"stdout {command}",
        intestazione=f"run_command: {command} -- stdout, {len(out_intero):,} caratteri",
    )
    dep_err = _deposita_se_tagliato(
        ctx,
        err_intero,
        tetto=ctx.budgets.command_stderr_max_chars,
        etichetta=f"stderr {command}",
        intestazione=f"run_command: {command} -- stderr, {len(err_intero):,} caratteri",
    )
    if dep_out:
        payload["stdout_deposito"] = dep_out
    if dep_err:
        payload["stderr_deposito"] = dep_err
    return _ok(
        {
            **payload,
            "stdout": smart_truncate(
                out_intero,
                ctx.budgets.command_stdout_max_chars,
                label="stdout",
                consiglio=_consiglio_deposito(dep_out),
            ),
            "stderr": smart_truncate(
                err_intero,
                ctx.budgets.command_stderr_max_chars,
                head_ratio=0.35,
                label="stderr",
                consiglio=_consiglio_deposito(dep_err),
            ),
        }
    )


def tool_manage_memory(ctx: ToolContext, action: str, content: str = "") -> str:
    action = (action or "").strip().lower()
    if action == "add":
        ok, message = add_memory(ctx.memories, content)
        if ok:
            if not ctx.memories_changed():
                return _err(
                    "La memoria non e' stata scritta su disco.",
                    hint="Vale per questa conversazione, ma sparisce alla chiusura.",
                )
            return _ok({"status": "ok", "saved": message})
        return _ok({"status": "skipped", "reason": message})
    if action == "remove":
        if remove_memory(ctx.memories, content):
            if not ctx.memories_changed():
                return _err(
                    "La memoria non e' stata tolta dal disco.",
                    hint="Alla riapertura sara' di nuovo li'.",
                )
            return _ok({"status": "ok", "removed": content})
        return _err("Memoria non trovata.", hint="Chiama manage_memory con action='list'.")
    if action == "list":
        return _ok({"memories": [f"[{m['id']}] {m['text']}" for m in ctx.memories]})
    return _err(
        f"Azione '{action}' non supportata.", hint="Valori ammessi: add, remove, list."
    )


PREVIEW_TOOL = "preview"

# Estensioni che hanno una resa visiva. Il resto si mostra come testo, che va
# benissimo per il codice ma non e' il motivo per cui esiste il pannello.
PREVIEW_RENDERABLE = {
    ".md", ".markdown", ".html", ".htm", ".svg", ".pdf",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif",
}
PREVIEW_TEXTUAL = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".css", ".json", ".toml", ".yaml",
    ".yml", ".txt", ".csv", ".sh", ".sql", ".ini", ".cfg", ".rs", ".go",
    ".java", ".c", ".h", ".cpp", ".rb", ".php", ".xml", ".env",
}

# Comandi che non finiscono mai: se compaiono in run_command, il timeout li
# uccide e il modello legge "FALLITO" su un processo che stava funzionando.
_SERVER_PATTERNS = (
    "http.server", "uvicorn", "gunicorn", "flask run", "fastapi dev",
    "npm run dev", "npm start", "yarn dev", "pnpm dev", "vite", "next dev",
    "streamlit run", "gradio", "manage.py runserver", "rails s",
)


# ``serve`` sta a parte: come sottostringa combacia con "observe", "preserve",
# "conserve" -- e un comando che contiene una di quelle parole veniva scambiato
# per un server e rifiutato da ``run_command``, che e' un no a un comando
# legittimo e senza spiegazione. Qui vuole i confini di parola.
_SERVER_PAROLE = re.compile(r"\b(serve|serving)\b", re.I)


def looks_like_server(command: str) -> bool:
    lowered = (command or "").lower()
    if any(p in lowered for p in _SERVER_PATTERNS):
        return True
    return bool(_SERVER_PAROLE.search(lowered))


def preview_kind(path: str) -> str:
    """'render' | 'text' | 'none' per un percorso."""
    suffix = Path(path).suffix.lower()
    if suffix in PREVIEW_RENDERABLE:
        return "render"
    if suffix in PREVIEW_TEXTUAL:
        return "text"
    return "none"


def _stato_porta(ctx: ToolContext, port: int) -> str:
    """Stato della porta **dentro** la sandbox. Vedi ``sandbox.port_state``.

    Non si guarda piu' da 127.0.0.1 sull'host, e il motivo merita di restare
    scritto: con una porta pubblicata, Docker ci tiene un proprio proxy in
    ascolto sull'host per tutta la vita del container. Da fuori ogni porta
    dell'intervallo risulta occupata anche quando dentro non c'e' niente --
    ed e' esattamente cio' che ha fatto rimbalzare l'agente fra 8200, 8201 e
    8202 sentendosele dire tutte occupate.
    """
    return sandbox_mod.port_state(
        ctx.workspace, port, image=ctx.docker_image,
        network=ctx.sandbox_network, ports=ctx.preview_ports,
    )


def _attendi_stato(
    ctx: ToolContext, port: int, atteso: tuple[str, ...], seconds: float
) -> str:
    """Aspetta che la porta entri in uno degli stati attesi; ritorna l'ultimo.

    Le transizioni non sono istantanee in nessuna delle due direzioni: un
    server ci mette qualche decimo di secondo a legarsi, e un processo ucciso
    altrettanto a liberare il socket.
    """
    scadenza = time.monotonic() + seconds
    stato = _stato_porta(ctx, port)
    while stato not in atteso and time.monotonic() < scadenza:
        time.sleep(0.3)
        stato = _stato_porta(ctx, port)
    return stato


# ---------------------------------------------------------------------------
# La radice: quale cartella si serve, non quale file
# ---------------------------------------------------------------------------
#
# Un file HTML da solo non e' una pagina: senza il suo foglio di stile, i suoi
# script e le sue immagini si vede un'altra cosa. Percio' l'anteprima di una
# pagina serve una **cartella**, e la scelta di quale cartella decide due cose
# insieme: cosa funziona (i percorsi assoluti come `/css/app.css` si risolvono
# li') e cosa il browser puo' leggere (niente sopra quella cartella).
#
# I marcatori "forti" segnano il confine di un progetto; `index.html` e' un
# indizio debole, buono solo quando non c'e' nient'altro.
_ROOT_MARKERS = ("package.json", "pyproject.toml", ".git", "requirements.txt")


def _rel_dir(base: Path, cartella: Path) -> str:
    rel = cartella.relative_to(base).as_posix()
    return "" if rel == "." else rel


def preview_root(workspace: str | Path, path: str) -> str:
    """La cartella da servire per mostrare ``path``, relativa al workspace.

    Stringa vuota = la radice del workspace. La regola, dal file verso l'alto:

    1. il **primo** confine di progetto che si incontra salendo vince. Nearest,
       non highest: in un monorepo con ``.git`` in cima e ``package.json`` nel
       sito, la radice giusta e' il sito;
    2. se non ce n'e' nessuno, la **piu' alta** cartella della catena che
       contiene un ``index.html`` -- cosi' ``sito/pagine/chi-siamo.html`` viene
       servito da ``sito/`` e il suo ``../css/`` esiste davvero;
    3. altrimenti la cartella del file, che e' sempre meglio del workspace
       intero: quello che non serve, non si espone.

    Non si esce mai dal workspace, perche' la catena si ferma li'.
    """
    base = Path(workspace).resolve()
    try:
        target = resolve_path(base, path)
    except WorkspaceError:
        return ""

    corrente = target if target.is_dir() else target.parent
    catena: list[Path] = []
    while True:
        catena.append(corrente)
        if corrente == base or not corrente.is_relative_to(base):
            break
        corrente = corrente.parent

    for cartella in catena:
        if any((cartella / marcatore).exists() for marcatore in _ROOT_MARKERS):
            return _rel_dir(base, cartella)
    for cartella in reversed(catena):
        if (cartella / "index.html").is_file():
            return _rel_dir(base, cartella)
    return _rel_dir(base, catena[0])


# ---------------------------------------------------------------------------
# Il backend: riconoscerlo, non chiederlo
# ---------------------------------------------------------------------------
#
# "Un HTML senza il backend avviato non funziona come dovrebbe" e' il secondo
# modo in cui l'anteprima mentiva: la pagina si vedeva, ma ogni fetch verso
# l'API del progetto tornava un errore, e la pagina sembrava rotta.
#
# L'informazione per capirlo ce l'abbiamo gia' sul disco -- e' il principio di
# questo harness: quello che si puo' sapere gratis non si chiede al modello.

_BACKEND_FILES = ("app.py", "main.py", "server.py", "api.py", "asgi.py", "wsgi.py")
_RE_FASTAPI = re.compile(r"^\s*(\w+)\s*=\s*FastAPI\s*\(", re.M)
_RE_FLASK = re.compile(r"^\s*(\w+)\s*=\s*Flask\s*\(", re.M)


def _leggi_un_po(path: Path, limite: int = 40_000) -> str:
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            return stream.read(limite)
    except OSError:
        return ""


def preview_backend(root: str | Path, port: int) -> dict[str, str] | None:
    """Il comando che fa vivere questo progetto, se se ne riconosce uno.

    Volutamente conservativo: un riconoscimento sbagliato costa un avvio
    fallito e un ripiego sullo statico (rumore), mentre un riconoscimento
    mancato costa solo quello che si aveva prima. Nel dubbio, None.
    """
    root = Path(root)
    porta = int(port)

    manage = root / "manage.py"
    if manage.is_file() and "django" in _leggi_un_po(manage).lower():
        return {
            "command": f"python manage.py runserver 0.0.0.0:{porta}",
            "kind": "django",
            "why": "manage.py di Django",
        }

    for nome in _BACKEND_FILES:
        sorgente = root / nome
        if not sorgente.is_file():
            continue
        testo = _leggi_un_po(sorgente)
        trovato = _RE_FASTAPI.search(testo)
        if trovato:
            return {
                "command": (
                    f"uvicorn {sorgente.stem}:{trovato.group(1)} "
                    f"--host 0.0.0.0 --port {porta}"
                ),
                "kind": "fastapi",
                "why": f"{nome} dichiara un'app FastAPI",
            }
        trovato = _RE_FLASK.search(testo)
        if trovato:
            return {
                # La CLI di Flask e non `python app.py`: qui la porta la
                # decidiamo noi, mentre un `app.run()` scritto nel file la
                # fissa a 5000 e si lega a 127.0.0.1 -- che dentro un
                # container vuol dire invisibile.
                "command": (
                    f"python -m flask --app {sorgente.stem}:{trovato.group(1)} "
                    f"run --host 0.0.0.0 --port {porta}"
                ),
                "kind": "flask",
                "why": f"{nome} dichiara un'app Flask",
            }

    pacchetto = root / "package.json"
    if pacchetto.is_file():
        try:
            dati = loads_object(_leggi_un_po(pacchetto) or "{}")
        except JsonBoundaryError:
            dati = {}
        scripts = dati.get("scripts")
        scripts = scripts if isinstance(scripts, dict) else {}
        deps = {**(dati.get("dependencies") if isinstance(dati.get("dependencies"), dict) else {}),
                **(dati.get("devDependencies") if isinstance(dati.get("devDependencies"), dict) else {})}
        if "vite" in deps and "dev" in scripts:
            return {
                "command": f"npm run dev -- --host 0.0.0.0 --port {porta}",
                "kind": "vite",
                "why": "package.json con Vite",
            }
        for script in ("dev", "start"):
            if script in scripts:
                return {
                    # PORT e HOST sono la convenzione che rispettano express,
                    # next e quasi tutto il resto. Se questo progetto non la
                    # rispetta, la porta resta chiusa e si ripiega sullo
                    # statico: il modo giusto in cui sbagliare.
                    "command": f"PORT={porta} HOST=0.0.0.0 npm run {script}",
                    "kind": "node",
                    "why": f"package.json con lo script '{script}'",
                }
    return None


def _libera_la_porta(ctx: ToolContext, porta: int) -> bool:
    """Prova a liberare ``porta`` dentro il container. True se ci riesce.

    Partire su una porta occupata significa vedere il server morire con
    "address already in use" e un traceback che non dice quasi niente. Il
    tentativo e' mirato -- prima l'anteprima precedente, poi un eventuale
    orfano rimasto -- e sta dentro l'intervallo pubblicato, che esiste solo
    per questo.
    """
    if _stato_porta(ctx, porta) == sandbox_mod.PORT_FREE:
        return True
    sandbox_mod.stop_background(
        ctx.workspace, image=ctx.docker_image,
        network=ctx.sandbox_network, ports=ctx.preview_ports,
    )
    if _stato_porta(ctx, porta) != sandbox_mod.PORT_FREE:
        sandbox_mod.kill_port_listener(
            ctx.workspace, porta, image=ctx.docker_image,
            network=ctx.sandbox_network, ports=ctx.preview_ports,
        )
    return _attendi_stato(
        ctx, porta, (sandbox_mod.PORT_FREE,), 3.0
    ) == sandbox_mod.PORT_FREE


def _avvia_sulla_porta(
    ctx: ToolContext, comando: str, porta: int, wait_s: int
) -> tuple[str, str]:
    """Avvia ``comando`` e aspetta la porta. Ritorna ``(stato, log)``.

    Lo stato e' uno di ``sandbox.PORT_*``, oppure ``"morto"`` se l'avvio stesso
    e' fallito. Chi chiama decide cosa farne: il tool ``serve`` lo racconta
    come errore, l'avvio automatico del backend ci ripiega sullo statico.
    """
    try:
        sandbox_mod.start_background(
            comando, ctx.workspace, image=ctx.docker_image,
            network=ctx.sandbox_network, ports=ctx.preview_ports,
        )
    except sandbox_mod.SandboxError as exc:
        # Un avvio fallito a meta' puo' aver comunque lasciato un processo in
        # piedi: si ripulisce prima di raccontarlo, o il tentativo successivo
        # trovera' la porta occupata senza capire da chi.
        sandbox_mod.stop_background(
            ctx.workspace, image=ctx.docker_image,
            network=ctx.sandbox_network, ports=ctx.preview_ports,
        )
        sandbox_mod.kill_port_listener(
            ctx.workspace, porta, image=ctx.docker_image,
            network=ctx.sandbox_network, ports=ctx.preview_ports,
        )
        return "morto", f"Avvio fallito: {exc}"

    stato = _attendi_stato(
        ctx, porta,
        (sandbox_mod.PORT_OPEN, sandbox_mod.PORT_LOCAL_ONLY),
        max(1, min(int(wait_s or 12), 60)),
    )
    log = sandbox_mod.background_log(
        ctx.workspace, image=ctx.docker_image,
        network=ctx.sandbox_network, ports=ctx.preview_ports,
    )
    return stato, log


def _diagnosi_porta(ctx: ToolContext, porta: int, stato: str) -> str:
    """Perche' non risponde. Non e' piu' un'ipotesi: ``/proc/net/tcp`` dice su
    quale indirizzo il processo si e' legato, quindi i due casi si distinguono
    invece di doverli indovinare dal fatto che sia vivo."""
    if stato == sandbox_mod.PORT_LOCAL_ONLY:
        return (
            f"Il processo e' in ascolto sulla porta {porta}, ma solo su "
            "127.0.0.1: dentro un container quello e' un indirizzo privato "
            "e dal browser non lo raggiunge nessuno. Rilancialo legandolo "
            f"a 0.0.0.0 (`--host 0.0.0.0`, `--bind 0.0.0.0`, o "
            f"`-b 0.0.0.0:{porta}`)."
        )
    if sandbox_mod.background_alive(
        ctx.workspace, image=ctx.docker_image,
        network=ctx.sandbox_network, ports=ctx.preview_ports,
    ):
        return (
            f"Il processo e' vivo ma non si e' legato alla porta {porta}: "
            "controlla che il comando usi davvero quella porta, e leggi il "
            "log qui sotto."
        )
    return (
        "Il processo e' gia' morto: leggi il log qui sotto, di solito "
        "e' un import mancante o un errore di sintassi."
    )


def _anteprima_statica(ctx: ToolContext, rel: str) -> dict[str, Any]:
    """Il payload di un file mostrato dal server statico dell'anteprima."""
    return {
        "kind": preview_kind(rel),
        "path": rel,
        # La cartella servita su `/`: la calcola l'harness, una volta sola,
        # e il client la usa per sapere quali salvataggi lo riguardano.
        "root": preview_root(ctx.workspace, rel),
        "title": Path(rel).name,
    }


def _prova_il_backend(
    ctx: ToolContext, rel: str, wait_s: int
) -> tuple[dict[str, Any] | None, str]:
    """Se il progetto di ``rel`` ha un backend, prova ad avviarlo.

    Ritorna ``(payload, nota)``: il payload c'e' solo se il backend risponde
    davvero. In tutti gli altri casi si ripiega sullo statico -- una pagina
    senza dati e' meno peggio di un pannello vuoto -- e la nota racconta al
    modello cos'e' successo, che e' l'unico modo perche' possa rimediare.
    """
    if not ctx.preview_autostart_backend:
        return None, ""
    if ctx.sandbox != "docker" or not ctx.preview_ports:
        return None, ""
    lo, _hi = ctx.preview_ports
    try:
        radice = resolve_path(ctx.workspace, preview_root(ctx.workspace, rel))
    except WorkspaceError:
        return None, ""
    spia = preview_backend(radice, lo)
    if not spia:
        return None, ""

    if not _libera_la_porta(ctx, lo):
        return None, (
            f"Questo progetto ha un backend ({spia['why']}), ma la porta {lo} "
            "e' occupata da qualcosa che non riesco a fermare: la pagina e' "
            "mostrata come file statico."
        )

    stato, log = _avvia_sulla_porta(ctx, spia["command"], lo, wait_s)
    if stato == sandbox_mod.PORT_OPEN:
        return (
            {
                "kind": "app",
                "port": lo,
                # Un backend decide da se' i propri indirizzi: il percorso del
                # file sul disco non e' l'URL della pagina (in Flask sta in
                # templates/, in Vite viene generata). Quindi si apre la
                # radice e si lascia decidere a lui.
                "url_path": "/",
                "title": f"{Path(rel).name} — {spia['kind']} su :{lo}",
                "command": spia["command"],
                "mode": "serve",
                "auto_backend": True,
            },
            f"Avviato da solo il backend di questo progetto ({spia['why']}): "
            f"`{spia['command']}`. E' gia' in esecuzione e gia' sullo schermo "
            "dell'utente: non riavviarlo. Per fermarlo, preview action='stop'.",
        )

    # Non ha risposto: si smonta quello che e' rimasto in piedi, senno' il
    # prossimo tentativo trova la porta occupata senza capire da chi.
    sandbox_mod.stop_background(
        ctx.workspace, image=ctx.docker_image,
        network=ctx.sandbox_network, ports=ctx.preview_ports,
    )
    coda = f"\n--- log ---\n{smart_truncate(log, 1200, label='log')}" if log.strip() else ""
    return None, (
        f"Questo progetto ha un backend ({spia['why']}) e ho provato ad "
        f"avviarlo con `{spia['command']}`, ma non risponde sulla porta {lo}: "
        "la pagina e' mostrata come file statico, quindi tutto cio' che "
        "chiede al server non funzionera'. Spesso mancano le dipendenze "
        "(installale con run_command), poi riprova con preview action='serve'."
        + coda
    )


def tool_preview(
    ctx: ToolContext,
    action: str,
    path: str = "",
    command: str = "",
    port: int = 0,
    url_path: str = "/",
    wait_s: int = 12,
) -> str:
    """Mostra qualcosa nel pannello di anteprima dell'utente.

    Due mestieri diversi sotto lo stesso tool, perche' per chi guarda sono la
    stessa cosa ("fammi vedere"): un file del workspace, oppure un processo che
    l'agente avvia e che resta in piedi.
    """
    action = (action or "").strip().lower()

    if action == "file":
        try:
            target = resolve_path(ctx.workspace, path)
        except WorkspaceError as exc:
            return _err(str(exc))
        if not target.is_file():
            return _err(f"'{path}' non esiste o non e' un file.")
        rel = target.resolve().relative_to(ctx.base).as_posix()
        kind = preview_kind(rel)
        if kind == "none":
            return _err(
                f"Non so mostrare un file '{Path(rel).suffix or 'senza estensione'}'.",
                hint=(
                    "L'anteprima rende documenti (.md, .html, .svg, .pdf, "
                    "immagini) e mostra come testo i file di codice."
                ),
            )

        # Una pagina si mostra viva: cartella servita su un'origine vera, e --
        # se il progetto ne dichiara uno -- il suo backend in esecuzione.
        # L'utente non deve chiedere due volte la stessa cosa, e il modello non
        # deve ricordarsi di una procedura in due passi.
        if Path(rel).suffix.lower() in {".html", ".htm"}:
            payload, nota = _prova_il_backend(ctx, rel, wait_s)
            if payload is not None:
                return _ok({"status": "ok", "preview": payload, "note": nota})
            risposta: dict[str, Any] = {
                "status": "ok",
                "preview": _anteprima_statica(ctx, rel),
            }
            if nota:
                risposta["note"] = nota
            return _ok(risposta)

        return _ok({"status": "ok", "preview": _anteprima_statica(ctx, rel)})

    if action in {"serve", "stop", "logs", "terminal", "gui"}:
        if ctx.sandbox != "docker":
            return _err(
                "Le anteprime di applicazioni richiedono la sandbox Docker.",
                hint="Con l'esecuzione diretta sull'host non c'e' nessuna porta da pubblicare.",
            )
        if not ctx.preview_ports:
            return _err(
                "Le anteprime di applicazioni sono disattivate.",
                hint="L'utente puo' abilitarle in Impostazioni -> Sandbox.",
            )
        lo, hi = ctx.preview_ports

        # Terminale e schermo hanno bisogno di roba che nell'immagine di serie
        # non c'e'. Chiederlo qui, all'etichetta dell'immagine, distingue "non
        # funziona" da "questa immagine e' di prima": il secondo caso si
        # risolve con un pulsante, ma solo se qualcuno lo dice.
        if action in {"terminal", "gui"}:
            manca = "terminal" if action == "terminal" else "gui"
            if manca not in sandbox_mod.image_features(ctx.docker_image):
                return _err(
                    "L'immagine di questo workspace non ha lo schermo e il "
                    "terminale per il browser.",
                    hint=(
                        "E' stata costruita prima che esistessero. L'utente "
                        "puo' rifarla da Impostazioni -> Connessione -> "
                        "'Costruisci l'immagine'; se il Dockerfile.sandbox del "
                        "workspace e' vecchio, va prima cancellato cosi' viene "
                        "riscritto."
                    ),
                )

        if action == "stop":
            fermato = sandbox_mod.stop_background(
                ctx.workspace, image=ctx.docker_image,
                network=ctx.sandbox_network, ports=ctx.preview_ports,
            )
            return _ok({"status": "ok", "stopped": fermato, "preview": None})

        if action == "logs":
            return _ok(
                {
                    "status": "ok",
                    "log": smart_truncate(
                        sandbox_mod.background_log(
                            ctx.workspace, image=ctx.docker_image,
                            network=ctx.sandbox_network, ports=ctx.preview_ports,
                        ),
                        ctx.budgets.command_stdout_max_chars,
                        label="log dell'anteprima",
                    ),
                }
            )

        # --- serve / terminal / gui ---
        #
        # Le tre azioni si separano solo qui: cosa mandare in esecuzione e su
        # quale porta. Da qui in giu' -- liberare la porta, avviare, aspettare
        # che risponda, diagnosticare -- e' esattamente la stessa strada, ed e'
        # il motivo per cui aggiungere un terminale non ha voluto dire
        # aggiungere un secondo meccanismo.
        if action == "terminal":
            porta = int(port or lo)
            comando = sandbox_mod.terminal_command(porta)
            pagina, titolo = "/", f"terminale su :{porta}"
        elif action == "gui":
            if not command.strip():
                return _err(
                    "Serve 'command': il programma con la finestra da mostrare.",
                    hint="Per esempio command='python gioco.py'. Non serve che "
                         "sappia niente di X: ci pensa l'harness.",
                )
            porta = int(port or lo)
            comando = sandbox_mod.gui_command(command, porta)
            pagina, titolo = sandbox_mod.NOVNC_PAGE, f"schermo di {command}"
        else:
            if not command.strip():
                return _err("Serve 'command': il comando che avvia l'applicazione.")
            comando = command
            pagina, titolo = url_path or "/", ""
            porta = int(port or 0)
        if not (lo <= porta <= hi):
            return _err(
                f"La porta {porta or '(non indicata)'} non e' pubblicata dal container.",
                hint=(
                    f"Usa una porta fra {lo} e {hi}, e passala anche al comando. "
                    "Fuori da quell'intervallo il browser non puo' raggiungere "
                    "l'applicazione."
                ),
            )

        if not _libera_la_porta(ctx, porta):
            return _err(
                f"La porta {porta} e' occupata da un processo che non "
                "riesco a fermare.",
                hint=(
                    f"Usa un'altra porta fra {lo} e {hi}. Se sono tutte "
                    "occupate, l'utente puo' premere 'Ricrea il container' "
                    "in Impostazioni -> Sandbox: butta via il container con "
                    "tutto quello che ci gira dentro."
                ),
            )

        stato, log = _avvia_sulla_porta(ctx, comando, porta, wait_s)
        if stato == "morto":
            return _err(f"Avvio dell'anteprima fallito: {log}")
        if stato == sandbox_mod.PORT_OPEN:
            return _ok(
                {
                    "status": "ok",
                    "preview": {
                        "kind": "app",
                        "port": porta,
                        "url_path": pagina,
                        "title": titolo or f"applicazione su :{porta}",
                        "command": comando,
                        "mode": action,
                    },
                    "log": smart_truncate(log, 1200, label="log"),
                }
            )

        suggerimento = _diagnosi_porta(ctx, porta, stato)
        return _err(
            f"L'applicazione non risponde sulla porta {porta}.",
            hint=suggerimento + (f"\n--- log ---\n{smart_truncate(log, 1500, label='log')}" if log.strip() else ""),
        )

    return _err(
        f"Azione '{action}' non supportata.",
        hint="Valori ammessi: file, serve, terminal, gui, stop, logs.",
    )


PLAN_TOOL = "manage_plan"


def _deposita_se_tagliato(
    ctx: ToolContext,
    testo: str,
    *,
    tetto: int,
    etichetta: str,
    intestazione: str = "",
) -> str | None:
    """Deposita il testo intero **solo se** il troncamento lo taglierebbe.

    Sotto il budget non si scrive niente: la sessione mediana non ha un
    problema di contesto (picco 8.059 token, misura del 23/08/2026) e non deve
    pagare una scrittura su disco per turno in cambio di niente.
    """
    if not ctx.deposito_attivo or tetto <= 0 or len(testo or "") <= tetto:
        return None
    return deposito_mod.deposita(
        ctx.base, testo, etichetta=etichetta, intestazione=intestazione
    )


def _consiglio_deposito(percorso: str | None) -> str | None:
    """La frase che sostituisce quella falsa dentro il marcatore di taglio."""
    if not percorso:
        return None
    return (
        f"il testo intero e' in `{percorso}`: leggilo con read_file "
        "o cercaci dentro con search_files"
    )


def _annota_chiusura(
    ctx: ToolContext, step: Any, *, saltato: bool, era_gia_chiuso: bool
) -> None:
    """Imbuca un punto appena chiuso perche' ``agent`` ne distilli il pensiero.

    Vale anche sui punti **saltati**: e' li' che nasce piu' spesso uno
    ``SCARTATO``, cioe' il fatto che serve a non rifare una strada gia' provata.
    ``saltato`` viaggia insieme perche' finisce scritto nella voce archiviata:
    un punto abbandonato dopo una verifica rossa non deve rileggersi, fra dieci
    sessioni, come una conclusione raggiunta.

    ``complete`` su un punto gia' chiuso e' permesso -- ``Plan.complete`` non
    guarda lo stato di partenza, e va bene cosi': e' una mossa idempotente e
    rifiutarla costerebbe un round-trip per niente. Ma **archiviarla** due
    volte no: la libreria e' append-only, quindi un secondo estratto non
    correggerebbe il primo, gli si affiancherebbe, e l'indice che il modello
    rilegge ad ogni passo si riempirebbe di doppioni. Idempotente sul piano
    deve voler dire idempotente anche in archivio.
    """
    if era_gia_chiuso:
        return
    ctx.punti_chiusi.append(
        {"id": step.id, "text": step.text, "saltato": bool(saltato)}
    )


def _punto_sottinteso(plan: Any, action: str) -> tuple[Any, str] | None:
    """Su quale punto agire quando il modello non l'ha detto.

    Il caso: il modello finisce un punto e chiama ``complete`` senza
    ``step_id``. Il piano rispondeva "Nel piano non c'e' nessun punto ''" --
    formalmente vero e praticamente inutile, perche' il punto lo sapevano tutti
    e due: e' quello aperto, ce n'e' uno solo per volta per costruzione
    (``Plan.start``), ed e' scritto nel blocco di coda che il modello ha appena
    letto. Chiedere di ripetere un'informazione che l'harness possiede gia' e'
    un round-trip speso per niente, e su un modello piccolo e' un round-trip
    che spesso finisce in un secondo errore.

    Stessa famiglia della coercizione di ``steps`` da testo a lista poco piu'
    sopra: quando l'intenzione e' univoca, rifiutare e' corretto e inutile.
    Quando **non** e' univoca -- nessun punto aperto, o piu' d'uno senza che
    nessuno sia in corso -- non si indovina: si torna a chiedere.
    """
    corrente = plan.current
    aperti = plan.open_steps
    if action in ("complete", "skip"):
        if corrente is not None:
            return (corrente, "quello in corso")
        if len(aperti) == 1:
            return (aperti[0], "l'unico rimasto aperto")
        return None
    # ``start``: senza id vale "riprendi da dove sei", che e' un no-op se un
    # punto e' gia' aperto e apre il primo rimasto se non lo e'.
    if corrente is not None:
        return (corrente, "quello gia' in corso")
    if aperti:
        return (aperti[0], "il primo rimasto da fare")
    return None


def _e_chiuso(ctx: ToolContext, step_id: str) -> bool:
    """Lo stato del punto **prima** della mossa: dopo, sono tutti chiusi."""
    step = ctx.plan.get(str(step_id))
    return step is not None and step.status in (DONE, SKIPPED)


def tool_manage_plan(
    ctx: ToolContext,
    action: str,
    steps: Any = None,
    step_id: str = "",
    note: str = "",
    ignore_red: bool = False,
) -> str:
    """Crea e aggiorna il piano di lavoro della conversazione.

    Le regole che contano non sono qui ma in ``core/plan.py`` (un punto aperto
    per volta) e in questa funzione (non si chiude un punto con una verifica
    rossa). Sono guardie, non consigli: tornano come errore del tool, che il
    modello e' costretto a leggere, invece che come una riga di prompt che puo'
    ignorare.
    """
    action = (action or "").strip().lower()
    # Il punto che l'harness ha aperto da se' chiudendo il precedente. Va detto
    # nel risultato e non lasciato al blocco del piano: li' il modello lo
    # leggerebbe un passo dopo, cioe' proprio nel passo che si vuole evitare.
    aperto: Any = None
    # Quello che il modello deve sapere ma che non impedisce l'operazione. Da
    # quando il piano non si blocca piu' su una verifica rossa, e' qui che
    # passa la differenza fra "fatto" e "fatto bene": il punto si chiude, e
    # accanto c'e' scritto cosa resta rosso.
    avvisi: list[str] = []
    # Il punto sottinteso si risolve **prima** di entrare nelle azioni, cosi'
    # che complete, skip e start vedano tutti lo stesso id e la nota finisca sul
    # punto giusto.
    if action in ("start", "complete", "skip") and not str(step_id or "").strip():
        sottinteso = _punto_sottinteso(ctx.plan, action)
        if sottinteso is None:
            if not ctx.plan.steps:
                return _err(
                    "Non c'e' ancora un piano.",
                    hint="Scrivilo con action='set' e l'elenco dei punti; il primo si apre da solo.",
                )
            if not ctx.plan.open_steps:
                return _err(
                    "Tutti i punti del piano sono gia' chiusi.",
                    hint=(
                        "Se resta del lavoro aggiungilo con action='add', "
                        "altrimenti scrivi il messaggio di chiusura."
                    ),
                )
            elenco = ", ".join(f"{s.id} ({s.text})" for s in ctx.plan.open_steps[:5])
            return _err(
                "Nessun punto e' in corso, quindi non so quale intendi.",
                hint=f"Passa step_id. Punti aperti: {elenco}.",
            )
        punto, perche = sottinteso
        step_id = punto.id
        avvisi.append(
            f"step_id non indicato: ho agito sul punto {punto.id} ('{punto.text}'), "
            f"{perche}. La prossima volta puoi ometterlo di nuovo, ma dirlo "
            "costa meno di un fraintendimento."
        )
    chiuso_ora: Any = None
    try:
        if action == "set":
            if isinstance(steps, str):
                # Alcuni modelli passano l'elenco come testo a righe invece che
                # come array. Rifiutare sarebbe formalmente corretto e
                # praticamente inutile: l'intenzione e' chiarissima.
                steps = [r.strip(" -*\t") for r in steps.splitlines()]
            if not isinstance(steps, list):
                return _err(
                    "Con action='set' serve 'steps': l'elenco dei punti del piano.",
                    hint='Esempio: steps=["correggere _has_cycle", "far passare i test"]',
                )
            # Gli elementi passano cosi' come sono: una stringa porta il tipo
            # nel prefisso ('diagnosi: ...'), un oggetto nel campo 'tipo'.
            ctx.plan.set_steps([s if isinstance(s, dict) else str(s) for s in steps])
            # Anche qui: il primo punto lo apre l'harness. Scrivere il piano e
            # poi chiedere il permesso di cominciarlo erano due round-trip per
            # una decisione che non ha alternative.
            aperto = ctx.plan.avanza()
        elif action == "add":
            # ``steps`` puo' arrivare come lista: e' il tipo che ``set`` usa, e
            # un modello che ha appena scritto il piano ci ricasca. ``str()``
            # su una lista produceva un punto chiamato letteralmente ``['x']``.
            if isinstance(steps, (list, tuple)):
                nuovi = [
                    s if isinstance(s, dict) else str(s).strip()
                    for s in steps
                    if isinstance(s, dict) or str(s).strip()
                ]
                if not nuovi:
                    return _err(
                        "Nessun punto da aggiungere.",
                        hint="Passa steps=['testo del punto'] oppure note='...'.",
                    )
                for testo in nuovi:
                    ctx.plan.add(testo)
            else:
                testo = str(steps or note or step_id or "").strip()
                if not testo:
                    return _err(
                        "Nessun punto da aggiungere.",
                        hint="Passa steps=['testo del punto'] oppure note='...'.",
                    )
                ctx.plan.add(testo)
        elif action == "ipotesi":
            # Il registro delle prove di un punto di diagnosi. Non apre e non
            # chiude niente: e' lo stato del ragionamento scritto fuori dal
            # pensiero, che altrimenti si butta ad ogni turno e si ricostruisce
            # da capo ad ogni passo del debugging.
            ctx.plan.annota_ipotesi(str(note or ""), str(step_id or ""))
        elif action == "start":
            ctx.plan.start(step_id)
        elif action == "complete":
            # Qui stava il gancio fra il piano e il ciclo di verifica: nessun
            # punto si chiudeva finche' esisteva un comando rosso. Confondeva
            # due cose diverse -- aver finito un'attivita' e avere la suite
            # verde -- e su un lavoro in TDD la confusione costava il turno
            # intero: il rosso iniziale **e'** il risultato atteso di
            # "riprodurre il bug", il modello non poteva chiudere quel punto,
            # non poteva aprire il successivo (un punto per volta, plan.py), e
            # rilanciava lo stesso comando finche' i passi non finivano.
            #
            # Adesso il punto si chiude e la verifica resta rossa, scritta nel
            # registro e allegata a questo risultato. Il controllo forte non
            # sparisce: si sposta sulla chiusura del turno, dove il riepilogo
            # deve dichiarare i rossi rimasti, e sul registro, che non li
            # dimentica piu' quando un punto si chiude.
            ignore_red = bool(ignore_red)
            motivo = str(note or "").strip()
            rosso = ctx.red_command
            if ignore_red and rosso:
                if len(motivo) < 12:
                    # Non si rifiuta piu' la chiusura: si rifiuta la
                    # **giustificazione**. Il punto si chiude lo stesso e la
                    # verifica resta rossa, che e' esattamente cio' che accade
                    # quando non si sa dire perche' non conta. La frase resta il
                    # prezzo di archiviare un rosso, ma non e' piu' il prezzo di
                    # andare avanti: era quella coincidenza a creare lo stallo.
                    avvisi.append(
                        f"`{rosso}` resta rossa: ignore_red senza un motivo "
                        "scritto non giustifica niente. Se davvero non riguarda "
                        "il codice del progetto, richiama complete con "
                        "note='...' e una frase intera."
                    )
                else:
                    giustificata = ctx.giustifica_verifica(motivo, via="ignore_red")
                    if giustificata is not None:
                        comando = giustificata.get("comando") or rosso
                        # Il rosso giustificato resta scritto nel piano, dove si
                        # vede: un punto chiuso cosi' non deve somigliare a uno
                        # chiuso davvero.
                        note = f"[verifica rossa giustificata: {comando}] {motivo}"
                        ctx.rossi_ignorati.append(
                            {"step": str(step_id), "comando": comando,
                             "motivo": motivo, "via": "ignore_red"}
                        )
            gia_chiuso = _e_chiuso(ctx, step_id)
            chiuso = ctx.plan.complete(step_id, note)
            chiuso_ora = chiuso
            _annota_chiusura(ctx, chiuso, saltato=False, era_gia_chiuso=gia_chiuso)
            aperto = ctx.plan.avanza()
        elif action == "skip":
            # ``skip`` azzerava il registro delle verifiche esattamente come
            # ``ignore_red``, ma senza chiedere niente e senza contare. Adesso
            # non lo tocca affatto: rinunciare a un punto e' una decisione sul
            # lavoro, non un giudizio su un test, e usarla per far sparire un
            # rosso era l'uscita a costo zero accanto a quella che pretende una
            # frase. Su questo progetto la differenza fra un rito e un invito e'
            # misurata, e un'uscita gratuita da un guard-rail diventa la strada.
            motivo = str(note or "").strip()
            gia_chiuso = _e_chiuso(ctx, step_id)
            chiuso = ctx.plan.skip(step_id, motivo)
            chiuso_ora = chiuso
            _annota_chiusura(ctx, chiuso, saltato=True, era_gia_chiuso=gia_chiuso)
            aperto = ctx.plan.avanza()
        elif action == "show":
            return _ok({"plan": ctx.plan.to_list()})
        else:
            return _err(
                f"Azione '{action}' non supportata.",
                hint="Valori ammessi: set, start, complete, skip, add, ipotesi, show.",
            )
    except PlanError as exc:
        return _err(str(exc))

    ctx.plan_changed()
    # Il piano **non** torna qui dentro. Sta gia' nel blocco di coda, rispedito
    # integrale ad ogni passo da ``plan.render_block``: rimandarlo anche nel
    # risultato del tool significa scrivere la stessa cosa due volte nella
    # stessa richiesta. Misurato il 23/08/2026 sulle sessioni salvate:
    # ``manage_plan`` valeva il **13,5% di tutti i token di risultato dei
    # tool**, terzo dopo read_file e run_command, per un'informazione che il
    # modello aveva gia' davanti.
    #
    # Quello che resta e' cio' che il blocco di coda **non** dice: che
    # l'operazione e' riuscita, e cosa e' cambiato adesso -- ``current`` e
    # ``aperto_in_automatico`` vanno letti in questo passo, non al prossimo.
    esito: dict[str, Any] = {
        "status": "ok",
        "action": action,
        "punti": len(ctx.plan.steps),
        "current": ctx.plan.current.id if ctx.plan.current else None,
    }
    if chiuso_ora is not None:
        # Con lo step_id sottinteso il modello non sa da solo su cosa ha agito:
        # senza questa riga dovrebbe dedurlo dal blocco di coda al passo dopo,
        # che e' esattamente il passo che si vuole risparmiare.
        esito["punto_chiuso"] = {"id": chiuso_ora.id, "text": chiuso_ora.text}
    if ctx.rossi_ignorati:
        # Torna al modello ad ogni operazione sul piano: se ne ha gia'
        # archiviati due, deve saperlo prima di archiviarne un terzo.
        esito["rossi_ignorati"] = len(ctx.rossi_ignorati)
    # Lo stato della qualita' viaggia **accanto** all'avanzamento, non dentro.
    # E' il punto dell'intera separazione: senza questo campo, "il piano non si
    # blocca piu' sui rossi" vorrebbe dire solo che i rossi si vedono meno.
    qualita = ctx.quality_summary()
    pendenti = qualita.get("pendenti") or []
    if pendenti:
        esito["qualita"] = qualita
        prima = pendenti[0]
        comando = prima.get("comando") or prima.get("identita") or ""
        avvisi.append(
            f"Il punto e' aggiornato, la qualita' no: `{comando}` e' ancora "
            f"rossa ({len(pendenti)} verifica/he pendenti). Non rieseguirla "
            "senza aver cambiato qualcosa; se questo rosso e' il risultato "
            "atteso di questo punto, vai avanti col piano e sistemalo dove "
            "previsto. Il riepilogo finale dovra' dichiararlo."
        )
    if avvisi:
        esito["avvisi"] = avvisi
    if aperto is not None:
        esito["aperto_in_automatico"] = {"id": aperto.id, "text": aperto.text}
        esito["next_step"] = (
            f"Punto {aperto.id} aperto: {aperto.text}. Non serve action='start'. "
            "Fai la prossima azione concreta, e quando l'ultima azione di questo "
            "punto lo conclude chiudilo nello stesso passo."
        )
    elif ctx.plan.finished:
        esito["next_step"] = (
            "Tutti i punti sono chiusi. Se il lavoro chiesto e' davvero finito, "
            "scrivi adesso il messaggio di chiusura; se e' rimasto fuori "
            "qualcosa, aggiungilo al piano invece di farlo di nascosto."
        )
    return _ok(esito)


DELEGA_TOOL = "esplora"


def tool_esplora(ctx: ToolContext, compito: str = "") -> str:
    """Manda un esploratore a rispondere a una domanda sul workspace.

    Quello che l'esploratore legge non entra nel tuo contesto: torna solo il
    referto. E' il motivo per cui esiste.
    """
    if ctx.on_delega is None:
        return _err(
            "La delega non e' disponibile in questa sessione.",
            hint="Cerca da solo con search_files e read_file.",
        )
    compito = str(compito or "").strip()
    if len(compito) < 12:
        return _err(
            "Il compito e' troppo vago per essere delegato.",
            hint=(
                "Scrivi una domanda chiusa e autosufficiente: l'esploratore "
                "non vede la conversazione, vede solo questa frase."
            ),
        )
    return _ok(ctx.on_delega(compito))


VAULT_SEARCH_TOOL = "vault_search"


def tool_vault_search(
    ctx: ToolContext,
    vault: str = "",
    query: str = "",
) -> str:
    """Interroga la wiki di un vault tramite l'agente manutentore.

    Il cercatore gira nel workspace del vault, non in quello corrente: legge
    l'indice e le pagine, e torna solo il referto con i link alle pagine.
    Non puo' scrivere -- una chat normale consulta il vault, non lo modifica;
    la manutenzione si fa aprendo il vault come workspace.
    """
    if ctx.on_vault_search is None:
        return _err(
            "La ricerca nel vault non e' disponibile in questa sessione.",
            hint="Nessun vault registrato: registralo dalla sezione Vault.",
        )
    if not str(vault or "").strip():
        return _err(
            "Manca il nome del vault.",
            hint="Passa 'vault' col nome di uno dei vault registrati.",
        )
    if len(str(query or "").strip()) < 8:
        return _err(
            "La query e' troppo vaga per essere delegata.",
            hint=(
                "Scrivi una domanda chiusa e autosufficiente: il cercatore "
                "non vede la conversazione, vede solo questa frase."
            ),
        )
    return _ok(ctx.on_vault_search(vault=vault.strip(), query=query.strip()))


NOTES_TOOL = "manage_notes"


def tool_manage_notes(
    ctx: ToolContext, action: str, text: str = "", ambito: str = "chat"
) -> str:
    """Foglio di note del compito in corso, o memoria del vault.

    Separato da ``manage_plan`` di proposito: il piano dice a che punto sei,
    le note dicono cosa hai capito. Mescolarli riempirebbe il piano di scoperte
    e lo renderebbe illeggibile proprio quando serve di piu'.

    Separato anche da ``manage_memory``: quella vale per il progetto e
    sopravvive a tutte le sessioni, questa muore con il compito.

    ``ambito='vault'`` scrive invece nella memoria del vault: stesso gesto,
    altra durata. Non e' un terzo tool perche' la differenza fra i tre fogli
    e' **quanto vivono**, non cosa ci si scrive -- e uno schema in piu' si
    paga in finestra ad ogni passo di ogni turno.
    """
    action = (action or "").strip().lower()
    if (ambito or "chat").strip().lower() == "vault":
        return _note_del_vault(ctx, action, text)
    try:
        if action == "add":
            nota = ctx.notes.add(text)
            esito: dict[str, Any] = {"status": "ok", "added": nota.to_dict()}
        elif action == "remove":
            nota = ctx.notes.remove(text)
            esito = {"status": "ok", "removed": nota.to_dict()}
        elif action == "clear":
            esito = {"status": "ok", "cleared": ctx.notes.clear()}
        elif action == "show":
            return _ok({"notes": ctx.notes.to_list()})
        else:
            return _err(
                f"Azione '{action}' non supportata.",
                hint="Valori ammessi: add, remove, clear, show.",
            )
    except NoteError as exc:
        return _err(str(exc))

    ctx.notes_changed()
    # Non si rimandano indietro tutte le note: sono gia' nel blocco di coda ad
    # ogni passo, e ripeterle qui sarebbe pagarle due volte per niente.
    return _ok({**esito, "action": action, "count": len(ctx.notes)})


def _note_del_vault(ctx: ToolContext, action: str, text: str) -> str:
    """La memoria del vault: quello che si e' capito **del posto**.

    Vale per tutte le conversazioni della cartella e non muore con nessuna di
    esse. Vive in ``.vault.json``, cioe' dentro il vault: la memoria segue la
    cartella, come il suo nome.
    """
    if not ctx.vault_dir:
        return _err(
            "Qui non c'e' nessun vault: questa cartella non ne fa parte.",
            hint="Senza ambito='vault' la nota va nel foglio di questa chat.",
        )
    try:
        if action == "add":
            config = vault_mod.aggiungi_nota(ctx.vault_dir, text)
            esito: dict[str, Any] = {"status": "ok", "added": text}
        elif action == "remove":
            config = vault_mod.togli_nota(ctx.vault_dir, text)
            esito = {"status": "ok", "removed": text}
        elif action == "show":
            return _ok({"notes": list(vault_mod.leggi_config(ctx.vault_dir).note)})
        elif action == "clear":
            # Niente svuotamento in blocco della memoria del vault. Il foglio
            # di una chat si butta perche' muore con lei comunque; questa e'
            # il lavoro di mesi, e una `clear` per sbaglio non ha un annulla.
            return _err(
                "La memoria del vault non si svuota in blocco.",
                hint="Togli le note superate una per una con action='remove'.",
            )
        else:
            return _err(
                f"Azione '{action}' non supportata.",
                hint="Valori ammessi con ambito='vault': add, remove, show.",
            )
    except vault_mod.NotaVaultError as exc:
        return _err(str(exc))
    except vault_mod.VaultScritturaError as exc:
        # La nota non e' finita sul disco. Va detto: prima ``scrivi_config``
        # ritornava la configurazione nuova comunque, e il modello riceveva
        # "registrata" su una nota che non c'era.
        return _err(
            f"Non ho potuto salvare la memoria del vault: {exc}",
            hint="La nota NON e' stata registrata. Controlla i permessi della "
            "cartella del vault, o riprova.",
        )

    ctx.vault_notes = list(config.note)
    ctx.vault_notes_changed()
    return _ok({**esito, "action": action, "ambito": "vault", "count": len(config.note)})


# ---------------------------------------------------------------------------
# Ricerca online
# ---------------------------------------------------------------------------

WEB_SEARCH_TOOL = "web_search"

# L'endpoint HTML di DuckDuckGo non richiede chiavi ne' JavaScript: e' la
# scelta piu' povera che funzioni. Un User-Agent da browser vero serve: senza,
# l'endpoint risponde con una pagina di bot-check senza risultati.
_DDG_ENDPOINT = "https://html.duckduckgo.com/html/"
_DDG_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_MAX_WEB_RESULTS = 10

# I blocchi risultato dell'endpoint HTML: link, titolo e snippet. Regex e non
# un parser HTML per non aggiungere una dipendenza al progetto: la struttura
# di questa pagina e' stabile da anni e un eventuale cambio si vede subito
# nel messaggio d'errore azionabile che il tool rimanda al modello.
_RE_LINK = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
    re.DOTALL,
)
_RE_SNIPPET = re.compile(
    r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', re.DOTALL
)
_RE_TAG = re.compile(r"<[^>]+>")


def _ddg_decode_url(href: str) -> str:
    """Converte l'href in un URL diretto.

    DuckDuckGo avvolge molti risultati in un redirect ``/l/?uddg=``: seguire
    quel link costerebbe all'agente un passo in piu' per ogni pagina letta.
    """
    href = href.strip()
    marker = "uddg="
    if marker in href:
        raw = href.split(marker, 1)[1].split("&", 1)[0]
        from urllib.parse import unquote

        return unquote(raw)
    if href.startswith("//"):
        return "https:" + href
    return href


def _strip_tags(html_fragment: str) -> str:
    text = _RE_TAG.sub(" ", html_fragment)
    text = (
        text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        .replace("&quot;", '"').replace("&#x27;", "'").replace("&#39;", "'")
        .replace("&nbsp;", " ")
    )
    return re.sub(r"\s+", " ", text).strip()


# Segnali che la pagina non e' un risultato vuoto ma una porta in faccia.
#
# Servono perche' i due casi arrivavano al modello con la stessa frase, e il
# suggerimento allegato -- "riformula con parole piu' comuni" -- e' il consiglio
# giusto per uno e disastroso per l'altro. Nella sessione del 29/08 la prima
# ricerca e' passata, la seconda ha trovato il limite di frequenza, e da li'
# il modello ha speso venti passi ad accorciare la query ("Endress+Hauser
# trasmettitore di pressione" -> "Endress Hauser" -> "Burkert") contro una
# pagina che non conteneva risultati per nessuna query.
_DDG_RIFIUTO = (
    "anomaly",
    "unusual traffic",
    "are you a robot",
    "bots use duckduckgo",
    "rate limit",
    "too many requests",
    "captcha",
    "challenge-platform",
)


def _ddg_rifiuta(page: str) -> bool:
    """La pagina e' un rifiuto del motore, non un elenco vuoto?

    Due indizi, e basta uno. Il primo sono le parole del controllo anti-bot.
    Il secondo e' la taglia: la pagina dei risultati -- anche quando i
    risultati sono zero -- porta con se' l'intero guscio del sito, decine di
    migliaia di caratteri. Un rifiuto e' una paginetta.

    Non e' una diagnosi certa e non deve esserlo: il costo di sbagliarla e'
    un consiglio meno preciso, il costo di non farla e' quello gia' pagato.
    """
    basso = page.lower()
    if any(segnale in basso for segnale in _DDG_RIFIUTO):
        return True
    return len(page) < 4000


def tool_web_search(
    ctx: ToolContext, query: str, max_results: int = 5
) -> str:
    """Cerca su DuckDuckGo e restituisce titolo, URL e snippet dei risultati.

    La richiesta parte dal **processo del server**, non dal container della
    sandbox: e' il server ad avere la rete configurata, e cosi' la ricerca
    funziona anche con le anteprime di rete disattivate.
    """
    # Guardia anti-scartocco: lo schema condizionale nasconde il tool al
    # modello quando la goccia e' spenta, ma un modello che se lo ricorda da
    # un turno precedente potrebbe comunque chiamarlo. Il perimetro vale qui.
    if not getattr(ctx, "web_search_enabled", False):
        raise WorkspaceError(
            "Il tool web_search non e' disponibile in questo turno: la "
            "modalita' ricerca online non era attiva per questo messaggio. "
            "Rispondi con cio' che sai e non insistere."
        )
    import httpx

    query = str(query or "").strip()
    if not query:
        raise WorkspaceError("Query vuota: passa il testo da cercare in 'query'.")
    try:
        max_results = int(max_results)
    except (TypeError, ValueError):
        max_results = 5
    max_results = max(1, min(max_results, _MAX_WEB_RESULTS))

    try:
        response = httpx.post(
            _DDG_ENDPOINT,
            data={"q": query},
            headers={
                "User-Agent": _DDG_UA,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "it,en;q=0.8",
            },
            timeout=min(float(ctx.timeout_s), 20.0),
            follow_redirects=True,
        )
        response.raise_for_status()
    except Exception as exc:  # rete, DNS, timeout: tutti recuperabili
        raise WorkspaceError(
            f"La ricerca online non ha risposto: {type(exc).__name__}: {exc}. "
            "Riprovare puo' bastare (e' spesso un limite temporaneo); se "
            "persiste, dillo all'utente invece di insistere."
        ) from exc

    page = response.text
    titles = [(m.group(1), m.group(2)) for m in _RE_LINK.finditer(page)]
    snippets = [m.group(1) for m in _RE_SNIPPET.finditer(page)]
    if not titles:
        # Pagina senza risultati: due cause diversissime, e il consiglio giusto
        # e' opposto. Vedi ``_ddg_rifiuta``.
        if _ddg_rifiuta(page):
            raise WorkspaceError(
                "Il motore di ricerca ha rifiutato la richiesta (limite di "
                "frequenza o controllo anti-bot), non ha detto che non ci sono "
                "risultati.",
                hint=(
                    "Non riformulare: la query non c'entra, e riprovare subito "
                    "verra' rifiutato di nuovo. Vai avanti con quello che sai, "
                    "o di' all'utente che la ricerca online e' momentaneamente "
                    "bloccata."
                ),
            )
        raise WorkspaceError(
            "Il motore di ricerca non ha restituito risultati interpretabili.",
            hint="Riformula la query con parole piu' comuni e riprova.",
        )

    results: list[dict[str, str]] = []
    for pos, (href, title) in enumerate(titles[: max_results * 2]):
        url = _ddg_decode_url(href)
        if not url.startswith("http"):
            continue
        snippet = _strip_tags(snippets[pos]) if pos < len(snippets) else ""
        results.append(
            {
                "title": _strip_tags(title)[:200],
                "url": url[:300],
                "snippet": snippet[:400],
            }
        )
        if len(results) >= max_results:
            break

    payload = {
        "query": query,
        "engine": "duckduckgo",
        "count": len(results),
        "results": results,
        "nota": (
            "Snippet del motore di ricerca: per il testo completo apri "
            "l'URL con preview action='file' dopo averlo scaricato via "
            "run_command (curl/python), oppure affidati allo snippet."
        ),
    }
    # Si tronca **dentro**, non la busta: ``smart_truncate`` su un JSON gia'
    # serializzato produce JSON invalido, e il modello riceve qualcosa che non
    # sa leggere proprio quando il risultato e' abbondante. Qui si tagliano i
    # risultati finche' la busta ci sta, e si dice quanti ne mancano.
    tetto = ctx.budgets.tool_result_max_chars
    fuori = 0
    while len(_ok(payload)) > tetto and len(payload["results"]) > 1:
        payload["results"].pop()
        fuori += 1
    if fuori:
        payload["troncati"] = fuori
        payload["nota"] = (
            f"{fuori} risultati non entrano nel budget di questo turno e sono "
            "stati tolti. " + str(payload["nota"])
        )
    return _ok(payload)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

ASK_USER_TOOL = "ask_user_question"

# Numero massimo di opzioni proposte: oltre, la scelta smette di essere una
# scelta e diventa un modulo da compilare.
MAX_QUESTION_OPTIONS = 4


def normalise_question(args: dict[str, Any]) -> dict[str, Any]:
    """Ripulisce gli argomenti di ``ask_user_question``.

    I modelli piccoli passano ``options`` in forme molto diverse: lista di
    stringhe, lista di dict ``{label, description}``, o una singola stringa con
    le alternative separate da virgola. Qui si normalizza tutto a una lista di
    ``{label, description}``.
    """
    question = str(args.get("question") or "").strip()
    raw_options = args.get("options")

    if isinstance(raw_options, str):
        raw_options = [part.strip() for part in raw_options.split(",") if part.strip()]
    if not isinstance(raw_options, list):
        raw_options = []

    options: list[dict[str, str]] = []
    for item in raw_options[:MAX_QUESTION_OPTIONS]:
        if isinstance(item, dict):
            label = str(item.get("label") or item.get("value") or "").strip()
            description = str(item.get("description") or "").strip()
        else:
            label, description = str(item).strip(), ""
        if label:
            options.append({"label": label[:120], "description": description[:240]})

    return {
        "question": question,
        "options": options,
        "allow_multiple": bool(args.get("allow_multiple")),
    }


TOOL_IMPLS: dict[str, Callable[..., str]] = {
    "list_files": tool_list_files,
    "web_search": tool_web_search,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "edit_file": tool_edit_file,
    "search_files": tool_search_files,
    "run_command": tool_run_command,
    "manage_memory": tool_manage_memory,
    PLAN_TOOL: tool_manage_plan,
    NOTES_TOOL: tool_manage_notes,
    DELEGA_TOOL: tool_esplora,
    VAULT_SEARCH_TOOL: tool_vault_search,
    PREVIEW_TOOL: tool_preview,
}

# Parametri accettati da ciascun tool: filtra gli argomenti allucinati dal
# modello, che altrimenti farebbero esplodere la chiamata con TypeError.
_ALLOWED_ARGS: dict[str, set[str]] = {
    "list_files": {"subfolder", "depth", "pattern"},
    "read_file": {"filepath", "start_line", "end_line"},
    "write_file": {"filepath", "content"},
    "edit_file": {"filepath", "old_string", "new_string", "replace_all"},
    "search_files": {"pattern", "glob", "subfolder", "output_mode", "context_lines"},
    "run_command": {"command", "timeout_sec"},
    "manage_memory": {"action", "content"},
    PLAN_TOOL: {"action", "steps", "step_id", "note", "ignore_red"},
    NOTES_TOOL: {"action", "text", "ambito"},
    DELEGA_TOOL: {"compito"},
    VAULT_SEARCH_TOOL: {"vault", "query"},
    PREVIEW_TOOL: {"action", "path", "command", "port", "url_path", "wait_s"},
    ASK_USER_TOOL: {"question", "options", "allow_multiple"},
    WEB_SEARCH_TOOL: {"query", "max_results"},
}


def validate_tool_arguments(name: str, args: Any) -> dict[str, Any] | None:
    """Validate a tool call without executing it, including question tools.

    Error fields preserve the existing ``error``/``hint`` protocol and add
    machine-readable diagnostics for bounded self-correction in the agent loop.
    """
    schema = _TOOL_PARAMETER_SCHEMAS.get(name) if isinstance(name, str) else None
    if schema is None:
        return {"error": "Tool sconosciuto.", "error_code": "unknown_tool",
                "tool": str(name)[:80], "retryable": False,
                "hint": "Scegli un tool presente nello schema di questo turno."}
    details = validate_arguments(args, schema)
    if not details and isinstance(args, dict):
        for field_name in ("filepath", "subfolder", "path", "command"):
            value = args.get(field_name)
            if isinstance(value, str) and "\0" in value:
                details.append({"path": f"$.{field_name}", "message": "Carattere NUL vietato."})
        if name == "read_file" and args.get("start_line", 1) > args.get("end_line", 2**63):
            details.append({"path": "$.end_line", "message": "Deve essere >= start_line."})
    if not details:
        return None
    return {"error": f"Argomenti non validi per '{name}'.", "error_code": "invalid_arguments",
            "tool": name, "retryable": False, "details": details,
            "hint": "Correggi i campi indicati e invia una nuova chiamata conforme allo schema. "
                    "Nessuna azione e' stata eseguita; non ripetere gli stessi argomenti. "
                    + " ".join(d["message"] for d in details)}


def dispatch(ctx: ToolContext, name: str, args: dict[str, Any]) -> str:
    """Authorize and validate before entering a tool's side-effect boundary."""
    impl = TOOL_IMPLS.get(name) if isinstance(name, str) else None
    if impl is None:
        return _err(
            f"Tool '{name}' inesistente.",
            hint="Tool disponibili: " + ", ".join(sorted(TOOL_IMPLS)),
        )
    # Il permesso si controlla **qui**, dove si esegue, e non nello schema.
    # Lo schema dice al modello cosa esiste; il recupero delle chiamate
    # scritte come testo (``parse_text_tool_calls``) non lo consulta, e in un
    # sotto-turno di sola lettura era l'unica cosa che separasse
    # l'esploratore da ``run_command``.
    if not ctx.puo_usare(name):
        return _err(
            f"Tool '{name}' non disponibile in questo turno.",
            hint="Strumenti di questo turno: "
            + ", ".join(sorted(ctx.tool_consentiti or ())),
        )
    invalid = validate_tool_arguments(name, args)
    if invalid is not None:
        return _ok(invalid)
    try:
        # Keep file transactions ordered across contexts sharing a workspace.
        if name in {"read_file", "write_file", "edit_file"}:
            path = resolve_path(ctx.workspace, args["filepath"])
            with _FILE_LOCKS[hash(str(path)) % len(_FILE_LOCKS)]:
                result = impl(ctx, **args)
        else:
            result = impl(ctx, **args)
    except WorkspaceError as exc:
        return _err(str(exc), hint=getattr(exc, "hint", ""))
    except Exception as exc:
        logger.exception("Unexpected failure in tool %s", name)
        # C'era un ramo ``except TypeError`` separato che diceva al modello
        # "argomenti non validi" e gli allegava l'elenco dei parametri ammessi.
        # Ma ``clean`` e' gia' filtrato su ``_ALLOWED_ARGS`` due righe sopra,
        # quindi un TypeError da firma sbagliata non arriva quasi mai qui --
        # mentre ci arriva **ogni** TypeError sollevato DENTRO
        # l'implementazione: un None in un confronto, una concatenazione fra
        # tipi diversi trenta righe piu' giu'.
        #
        # Al modello arrivava un ordine esplicito e falso, e il modello faceva
        # la cosa che gli era stata detta: riprovare con altri argomenti. E ci
        # riprovava finche' non finivano i passi, per un difetto dell'harness
        # con cui gli argomenti non c'entravano niente. Un messaggio falso e
        # generico verrebbe ignorato; uno falso e *specifico* viene seguito.
        return _err(
            f"Errore interno nel tool '{name}': {type(exc).__name__}: {exc}",
            hint="Non e' un problema degli argomenti: non riprovare la stessa "
            "chiamata cambiandoli. Prova un'altra strada, oppure segnala il "
            "guasto nella risposta.",
        )

    return result


# ---------------------------------------------------------------------------
# Schemi di function calling
# ---------------------------------------------------------------------------

TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "Elenca l'albero di file e cartelle del workspace. USALO PER PRIMO "
                "ogni volta che devi orientarti nel progetto, prima di leggere o "
                "scrivere qualsiasi file, e ogni volta che l'utente nomina un file "
                "di cui non conosci il percorso esatto. Con 'pattern' cerca invece i "
                "file per nome a qualunque profondita'. Ignora automaticamente "
                ".git, node_modules, .venv e le cartelle di build."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subfolder": {
                        "type": "string",
                        "description": "Sottocartella relativa alla radice. Default '.' (radice).",
                    },
                    "depth": {
                        "type": "integer",
                        "description": "Livelli di profondita' da esplorare, 1-4. Default 2.",
                    },
                    "pattern": {
                        "type": "string",
                        "description": (
                            "Modello di nome file, es. 'test_*.py' o '*.csv'. Se "
                            "lo passi non ricevi l'albero ma solo i percorsi che "
                            "corrispondono, cercati a qualunque profondita' e "
                            "ordinati dal piu' recente. E' il modo giusto di "
                            "rispondere a 'dove sono i test' senza esplorare a mano."
                        ),
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Legge il contenuto testuale di un file del workspace. OBBLIGATORIO "
                "prima di modificare un file esistente: non indovinare mai il "
                "contenuto. Per file grandi passa start_line/end_line per leggere "
                "solo la porzione che ti serve invece dell'intero file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "Percorso relativo, sintassi POSIX (es. 'src/api/main.py').",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "Prima riga da leggere, 1-based. Opzionale.",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Ultima riga da leggere, inclusa. Opzionale.",
                    },
                },
                "required": ["filepath"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Crea un file nuovo o ne sostituisce integralmente il contenuto. "
                "USALO quando l'utente chiede di creare, generare o riscrivere un "
                "file: scrivi il codice direttamente qui, NON incollarlo come testo "
                "in chat. Per una modifica puntuale a un file esistente preferisci "
                "edit_file, che consuma molti meno token. "
                "ATTENZIONE: se il file esiste gia' e non l'hai ancora letto in "
                "questa conversazione, la chiamata viene rifiutata -- prima "
                "read_file, per non cancellare lavoro che non hai visto."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "Percorso relativo del file da creare o sovrascrivere.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Contenuto completo del file. Nessun segnaposto, nessun '...'.",
                    },
                },
                "required": ["filepath", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Sostituisce un frammento esatto di testo dentro un file esistente. "
                "E' il tool DA PREFERIRE per correggere un bug, rinominare un "
                "simbolo o aggiungere una funzione a un file gia' scritto. "
                "old_string deve comparire una volta sola: se non e' unico, "
                "includi righe di contesto attorno -- oppure passa "
                "replace_all=true se le volevi cambiare tutte."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Percorso relativo del file."},
                    "old_string": {
                        "type": "string",
                        "description": "Testo esatto da sostituire, indentazione inclusa.",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "Testo sostitutivo. Stringa vuota per cancellare.",
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": (
                            "true per cambiare TUTTE le occorrenze invece di "
                            "pretenderne una sola. E' il modo di fare una "
                            "rinomina in una chiamata invece che in otto."
                        ),
                    },
                },
                "required": ["filepath", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": (
                "Cerca una espressione regolare in tutti i file del workspace "
                "(equivale a 'grep -rn'). USALO quando devi trovare dove e' "
                "definita o usata una funzione, una classe, una costante o una "
                "stringa, invece di leggere i file uno a uno. Se ti serve solo sapere "
                "in QUALI file sta, passa output_mode='files': stessa "
                "risposta, una frazione del contesto."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regex o testo letterale da cercare (case-insensitive).",
                    },
                    "glob": {
                        "type": "string",
                        "description": "Filtro sui nomi file, es. '*.py'. Default '*'.",
                    },
                    "subfolder": {
                        "type": "string",
                        "description": "Sottocartella da cui partire. Default '.'.",
                    },
                    "output_mode": {
                        "type": "string",
                        "enum": ["files", "count", "content"],
                        "description": (
                            "Quanto ti torna indietro. 'files': solo i percorsi "
                            "che contengono qualcosa -- USALO quando la domanda e' "
                            "*dove* sta una cosa, costa una frazione degli altri. "
                            "'count': percorso e quante volte. 'content' (default): "
                            "le righe che corrispondono."
                        ),
                    },
                    "context_lines": {
                        "type": "integer",
                        "description": (
                            "Righe da mostrare sopra e sotto ogni corrispondenza, "
                            "0-4. Solo con output_mode='content'."
                        ),
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Esegue un comando di shell nella radice del workspace e ne "
                "restituisce stdout, stderr ed exit code. Il comando gira in un "
                "container Linux isolato in cui il workspace e' montato su /work: "
                "usa percorsi relativi, non percorsi Windows, e non tentare di "
                "uscire dalla cartella (fuori non c'e' nulla). USALO PER "
                "VERIFICARE il tuo lavoro dopo ogni modifica: 'python -m py_compile "
                "file.py', 'pytest -q', 'npm test', 'ruff check .', 'git status'. "
                "Un task di scrittura codice non e' completo finche' un comando di "
                "verifica non e' passato."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Comando completo da eseguire nella shell.",
                    },
                    "timeout_sec": {
                        "type": "integer",
                        "description": "Timeout in secondi. Opzionale, default dalle impostazioni.",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": PREVIEW_TOOL,
            "description": (
                "Mostra qualcosa nel pannello di anteprima dell'utente. "
                "action='file' con path per far vedere un documento del "
                "workspace (markdown, HTML, SVG, PDF, immagini, o codice come "
                "testo): utile quando quello che hai prodotto si capisce meglio "
                "guardandolo che leggendone il nome. Su una pagina HTML basta "
                "questo e basta una volta: l'harness serve tutta la sua "
                "cartella (quindi CSS, script e immagini si vedono) e, se il "
                "progetto dichiara un backend (Flask, FastAPI, Django, Vite, "
                "package.json), lo avvia da solo -- non serve un action='serve' "
                "dopo. "
                "action='serve' per avviare un'applicazione o uno script che "
                "resta in esecuzione (server web, dashboard, demo) e mostrarla "
                "dal vivo: USA QUESTO E NON run_command, perche' run_command ha "
                "un timeout e ucciderebbe il processo dopo pochi secondi. Il "
                "comando deve mettersi in ascolto su 0.0.0.0 -- dentro un "
                "container 127.0.0.1 non e' raggiungibile da fuori -- e sulla "
                "porta che passi in 'port'. "
"action='gui' con command per un programma che apre una FINESTRA "
                "(tkinter, pygame, Qt, matplotlib interattivo): l'harness gli "
                "mette attorno uno schermo e lo fa vedere nel pannello, dove "
                "l'utente ci puo' anche cliccare dentro. Scrivi il comando "
                "normale, senza occuparti di X o di DISPLAY. "
                "action='terminal' apre una shell dentro la sandbox nel "
                "pannello: serve all'UTENTE, per guardare o provare a mano; tu "
                "continua a usare run_command. "
                "action='logs' per rileggere l'output del processo, "
                "action='stop' per fermarlo."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["file", "serve", "gui", "terminal", "logs", "stop"],
                        "description": "Cosa fare.",
                    },
                    "path": {
                        "type": "string",
                        "description": (
                            "Solo per action='file': percorso del file relativo "
                            "alla radice del workspace."
                        ),
                    },
                    "command": {
                        "type": "string",
                        "description": (
                            "Per action='serve': comando che avvia "
                            "l'applicazione, gia' legato a 0.0.0.0 (es. "
                            "`python -m http.server 8200 --bind 0.0.0.0`, "
                            "`uvicorn app:app --host 0.0.0.0 --port 8200`). "
                            "Per action='gui': il programma con la finestra, "
                            "scritto come lo lanceresti da terminale (es. "
                            "`python gioco.py`)."
                        ),
                    },
                    "port": {
                        "type": "integer",
                        "description": (
                            "Solo per action='serve': la porta su cui "
                            "l'applicazione ascolta. Deve stare nell'intervallo "
                            "pubblicato dal container, indicato nel blocco "
                            "<environment>."
                        ),
                    },
                    "url_path": {
                        "type": "string",
                        "description": (
                            "Percorso da aprire nell'anteprima, se non e' la "
                            "radice (es. '/docs')."
                        ),
                    },
                    # Era gia' in ``_ALLOWED_ARGS`` e gia' letto da
                    # ``tool_preview``, ma non dichiarato qui: una manopola che
                    # esisteva e che il modello non poteva girare. Con un
                    # server lento a partire l'unica cosa che poteva fare era
                    # riprovare -- cioe' spendere un altro passo per aspettare
                    # gli stessi dodici secondi.
                    "wait_s": {
                        "type": "integer",
                        "description": (
                            "Secondi di attesa perche' la porta risponda "
                            "(default 12, massimo 60). Alzalo solo se sai che "
                            "quel server ci mette di piu' a partire."
                        ),
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": PLAN_TOOL,
            "description": (
                "Piano di lavoro visibile all'utente, che sopravvive fra i "
                "turni. CHIAMALO PER PRIMO, prima di qualunque altra cosa, "
                "quando la richiesta contiene piu' di una azione (elenchi "
                "numerati, 'step', piu' verbi all'imperativo): action='set' "
                "con 3-8 punti, uno per obiettivo verificabile, non uno per "
                "comando. Il primo punto si apre da solo, e ogni "
                "action='complete' (o 'skip') apre il successivo: non serve "
                "chiamare 'start' se non per saltare a un punto fuori "
                "sequenza. Chiudi il punto NELLO STESSO PASSO dell'ultima "
                "azione che lo conclude: e' una chiamata in piu' in quel "
                "passo, non un passo in piu'. Rivedere il piano a meta' "
                "lavoro e' normale: con action='set' passa SOLO cio' che resta "
                "da fare -- i punti gia' fatti o saltati restano nel piano coi "
                "loro numeri e non si possono ne' riscrivere ne' cancellare, "
                "quindi non serve rielencarli (se lo fai vengono ignorati). Il "
                "piano ti viene rimostrato ad ogni passo, quindi non devi "
                "ricordartelo ne' ripianificare: leggilo e fai la mossa "
                "successiva. Ogni punto comincia col suo TIPO, che decide "
                "quanto ragionerai su quel punto: 'esegui: ...' (modifica o "
                "comando gia' deciso), 'indaga: ...' (leggere e capire), "
                "'diagnosi: ...' (qualcosa e' rotto e non sai perche'), "
                "'progetta: ...' (scegliere un approccio). Nei punti di "
                "diagnosi usa action='ipotesi' per tenere il registro delle "
                "prove."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["set", "start", "complete", "skip", "add", "ipotesi", "show"],
                        "description": "Operazione da eseguire sul piano.",
                    },
                    "steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Solo per action='set': i punti ANCORA DA FARE, in "
                            "ordine. Ogni punto e' un obiettivo con un esito "
                            "verificabile (es. 'far passare i 4 test di "
                            "test_dag_executor.py'), non un comando, e comincia "
                            "col tipo: 'esegui: ', 'indaga: ', 'diagnosi: ' o "
                            "'progetta: '. Quelli gia' chiusi non vanno "
                            "rimessi: restano da soli."
                        ),
                    },
                    "step_id": {
                        "type": "string",
                        "description": (
                            "Numero del punto su cui agire, per start, "
                            "complete e skip. Puoi ometterlo: senza, vale il "
                            "punto attualmente in corso."
                        ),
                    },
                    "note": {
                        "type": "string",
                        "description": (
                            "Con action='ipotesi': una riga 'ipotesi -> prova -> "
                            "esito' (riscriverla con la stessa ipotesi ne "
                            "aggiorna l'esito). "
                            "Esito in una riga, per complete e skip "
                            "(es. '4 test verdi' oppure 'saltato: manca "
                            "pytest-asyncio nella sandbox')."
                        ),
                    },
                    "ignore_red": {
                        "type": "boolean",
                        "description": (
                            "Solo per action='complete': imposta a true se la "
                            "verifica rossa attuale non e' pertinente al codice "
                            "(es. vincolo o dipendenza di ambiente non disponibile)."
                        ),
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_memory",
            "description": (
                "Gestisce la memoria a lungo termine, che sopravvive fra le "
                "sessioni. Chiamalo con action='add' NON APPENA scopri un fatto "
                "stabile: comando di test del progetto, framework adottato, "
                "preferenza di stile dell'utente, vincolo architetturale. "
                "action='remove' per informazioni diventate obsolete, "
                "action='list' per rileggerle."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "remove", "list"],
                        "description": "Operazione da eseguire.",
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "Fatto da memorizzare, una frase sola e autoconsistente "
                            "(es. 'I test si lanciano con: uv run pytest -q'). "
                            "Per 'remove' passa il testo o l'id della memoria."
                        ),
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": DELEGA_TOOL,
            "description": (
                "Manda un esploratore a rispondere a una domanda sul "
                "workspace. Lui apre i file, cerca e ragiona; a te torna SOLO "
                "la sua risposta, e tutto quello che ha letto non entra mai "
                "nel tuo contesto. USALO quando per rispondere servirebbe "
                "aprire piu' file di quanti te ne servano davvero: 'dove e' "
                "definita X e chi la usa', 'questo progetto come lancia i "
                "test', 'esiste gia' qualcosa che fa Y'. Non usarlo per una "
                "cosa che sai gia' o che si risolve con un read_file solo: "
                "costa un giro di modello. L'esploratore non puo' scrivere ne' "
                "eseguire comandi, e non vede questa conversazione."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "compito": {
                        "type": "string",
                        "description": (
                            "La domanda, chiusa e autosufficiente. Lui non "
                            "sa niente di quello che vi siete detti: mettici "
                            "dentro tutto il contesto che serve, e di' anche "
                            "in che forma vuoi la risposta."
                        ),
                    },
                },
                "required": ["compito"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": VAULT_SEARCH_TOOL,
            "description": (
                "Interroga un vault LLM Wiki registrato: l'agente manutentore "
                "della wiki cerca nelle sue pagine e ti torna solo la risposta "
                "con i link alle pagine usate. USALO quando la domanda riguarda "
                "il contenuto di un vault e tu non hai il vault aperto come "
                "workspace. Il cercatore non vede questa conversazione e non "
                "puo' scrivere: la manutenzione della wiki si fa aprendo il "
                "vault come workspace."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "vault": {
                        "type": "string",
                        "description": (
                            "Nome del vault in cui cercare, cosi' come compare "
                            "nella sezione Vault."
                        ),
                    },
                    "query": {
                        "type": "string",
                        "description": (
                            "La domanda sulla wiki, chiusa e autosufficiente: "
                            "il cercatore non sa niente di quello che vi siete "
                            "detti. Di' anche in che forma vuoi la risposta."
                        ),
                    },
                },
                "required": ["vault", "query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": NOTES_TOOL,
            "description": (
                "Foglio di appunti del compito in corso, visibile all'utente. "
                "action='add' con una riga NON APPENA capisci qualcosa che non "
                "si ricava rileggendo un file: perche' un test falliva, quale "
                "strada hai gia' provato e scartato, un vincolo detto "
                "dall'utente, un numero misurato. Il piano dice a che punto "
                "sei, le note dicono cosa hai capito: non mescolarli. "
                "action='remove' per una nota diventata falsa -- una nota "
                "sbagliata e' peggio di nessuna nota. Quando il contesto si "
                "riempie la cronologia viene riassunta e i dettagli spariscono: "
                "queste note restano parola per parola, e sono l'unica cosa "
                "scritta da te che sopravvive."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "remove", "clear", "show"],
                        "description": "Operazione da eseguire sul foglio.",
                    },
                    "text": {
                        "type": "string",
                        "description": (
                            "La nota, una frase sola e autoconsistente (es. "
                            "'i test vanno lanciati da /work o falliscono "
                            "sull'import di core'). Per 'remove' passa il "
                            "numero della nota."
                        ),
                    },
                    "ambito": {
                        "type": "string",
                        "enum": ["chat", "vault"],
                        "description": (
                            "Dove scrivere. 'chat' (di serie) e' il foglio di "
                            "questa conversazione: muore con lei. 'vault' e' la "
                            "memoria della cartella: vale in TUTTE le chat di "
                            "questo vault e non muore mai. Usa 'vault' per "
                            "quello che varra' ancora fra un mese -- una "
                            "convenzione concordata, dove stanno le cose, una "
                            "strada scartata e perche'. Fuori da un vault non "
                            "esiste e torna errore."
                        ),
                    },
                },
                "required": ["action"],
            },
        },
    },
]

TOOLS_SCHEMA.append(
    {
        "type": "function",
        "function": {
            "name": ASK_USER_TOOL,
            "description": (
                "Fa una domanda all'utente e ATTENDE la sua risposta prima di "
                "proseguire. Usalo quando una scelta cambia il risultato e non "
                "puoi dedurla dal workspace: quale libreria o framework "
                "adottare, se sovrascrivere un file esistente, quale fra piu' "
                "interpretazioni della richiesta e' quella giusta, se procedere "
                "con un'operazione irreversibile. Proponi 2-4 opzioni concrete "
                "e metti per prima quella che consigli. NON usarlo per chiedere "
                "permesso di leggere file o eseguire verifiche (quelle falle e "
                "basta), ne' per informazioni che un tool ti puo' dare."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "La domanda, chiara e autoconsistente, una frase sola.",
                    },
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Da 2 a 4 risposte possibili, brevi e mutuamente "
                            "esclusive. La prima sia quella che consigli. "
                            "Omettilo per lasciare la risposta libera."
                        ),
                    },
                    "allow_multiple": {
                        "type": "boolean",
                        "description": "true se l'utente puo' scegliere piu' opzioni insieme.",
                    },
                },
                "required": ["question"],
            },
        },
    }
)
# La ricerca online NON sta in TOOLS_SCHEMA: finche' l'utente non accende la
# modalita' dalla goccia nel composer, il modello non deve nemmeno sapere che
# esiste. Il server aggiunge questo blocco agli schemi solo per i turni in cui
# il flag e' attivo (vedi AppState.tools_schema in server/main.py).
WEB_SEARCH_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": WEB_SEARCH_TOOL,
            "description": (
                "Fa una ricerca sul web e restituisce i primi risultati "
                "(titolo, URL, snippet). Usalo quando la risposta dipende da "
                "fatti attuali o esterni al workspace: versioni recenti di una "
                "libreria, notizie, documentazione non presente nel progetto, "
                "prezzi o date. Non serve per cose che sai gia' o che sono nel "
                "workspace: in quel caso e' solo tempo perso."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Testo da cercare. Query brevi e specifiche "
                            "funzionano meglio di domande intere."
                        ),
                    },
                    "max_results": {
                        "type": "integer",
                        "description": (
                            "Quanti risultati tornano, 1-10. Default 5: "
                            "aumentalo solo se i primi non bastano."
                        ),
                    },
                },
                "required": ["query"],
            },
        },
    }
]

# Tutti i tool che l'harness sa eseguire, schema condizionale compreso.
#
# Non e' la stessa cosa dello schema mandato al modello: quello e' il permesso
# di *questo* turno, questo e' il vocabolario dell'harness. La distinzione non
# e' accademica -- ``_as_tool_call`` scarta ogni chiamata il cui nome non stia
# qui, ed e' la strada che percorrono i modelli che non emettono tool call
# native e le scrivono come testo (qwen2.5-coder:7b fra questi). Costruendo
# l'insieme dal solo ``TOOLS_SCHEMA``, ``web_search`` restava fuori: con la
# goccia accesa il modello lo chiamava, il parser lo buttava via senza dire
# niente, e la ricerca online semplicemente non succedeva.
#
# Il perimetro vero -- spento vuol dire inutilizzabile -- lo tiene
# ``ToolContext.web_search_enabled`` dentro ``tool_web_search``, che e' il
# posto dove un permesso si controlla.
_TOOL_PARAMETER_SCHEMAS = {
    tool["function"]["name"]: tool["function"]["parameters"]
    for tool in (*TOOLS_SCHEMA, *WEB_SEARCH_TOOLS)
}
for _parameters in _TOOL_PARAMETER_SCHEMAS.values():
    _parameters["additionalProperties"] = False
for _tool_name, _field_name, _minimum, _maximum in (
    ("list_files", "depth", 1, 4),
    ("read_file", "start_line", 1, 2**31 - 1),
    ("read_file", "end_line", 1, 2**31 - 1),
    ("search_files", "context_lines", 0, 4),
    ("run_command", "timeout_sec", 1, 3600),
    (PREVIEW_TOOL, "port", 1, 65535),
    (PREVIEW_TOOL, "wait_s", 1, 60),
    (WEB_SEARCH_TOOL, "max_results", 1, 10),
):
    _TOOL_PARAMETER_SCHEMAS[_tool_name]["properties"][_field_name].update(
        {"minimum": _minimum, "maximum": _maximum}
    )
for _parameters in _TOOL_PARAMETER_SCHEMAS.values():
    for _field_name, _property in _parameters["properties"].items():
        if _property.get("type") == "string":
            _property["maxLength"] = 2 * 1024 * 1024 if _field_name in {
                "content", "old_string", "new_string"
            } else 32768
            if _field_name in _parameters["required"] and _field_name not in {
                "content", "new_string"
            }:
                _property["minLength"] = 1
        elif _property.get("type") == "array":
            _property["maxItems"] = 128

TOOL_NAMES = frozenset(
    t["function"]["name"] for t in (*TOOLS_SCHEMA, *WEB_SEARCH_TOOLS)
)


# ---------------------------------------------------------------------------
# Schemi snelli per i modelli che non hanno bisogno di essere imboccati
# ---------------------------------------------------------------------------
#
# Gli schemi completi sono il blocco fisso piu' grande della richiesta -- piu'
# del system prompt, in entrambi i percorsi -- e si pagano **ad ogni singolo
# passo agentico**. Quanto costino lo dice ``prompts.costi_del_prefisso()`` e
# non un numero scritto qui: la versione precedente di questo commento diceva
# "~1.790 token" per uno schema che ne occupa 4.318, e "612" per un prompt che
# ne occupa 3.001. Buona parte di quel peso sono istruzioni pedagogiche
# ("USALO PER VERIFICARE il tuo lavoro dopo ogni modifica: 'pytest -q', ...")
# che servono a un modello che non collega la richiesta al tool giusto.
#
# I nomi e i tipi dei parametri **non si toccano mai**: quelli sono il
# contratto, e accorciarli produrrebbe chiamate malformate. Si accorciano solo
# le descrizioni in prosa.
LEAN_TOOL_DESCRIPTIONS: dict[str, str] = {
    DELEGA_TOOL: "Manda un esploratore a rispondere a una domanda sul workspace.",
    WEB_SEARCH_TOOL: (
        "Cerca sul web e torna titolo, URL e snippet dei primi risultati. "
        "Per fatti attuali o esterni al workspace."
    ),
    VAULT_SEARCH_TOOL: (
        "Interroga un vault LLM Wiki registrato: torna solo la risposta "
        "dell'agente cercatore, coi link alle pagine usate."
    ),
    "list_files": "Elenca file e cartelle di una sottocartella del workspace.",
    "read_file": (
        "Legge un file del workspace. Obbligatorio prima di modificarne uno "
        "esistente. start_line/end_line per leggerne solo una porzione."
    ),
    "search_files": "Cerca un'espressione regolare nei file del workspace.",
    "write_file": (
        "Crea un file o lo riscrive per intero. Per un file esistente serve "
        "averlo prima letto con read_file."
    ),
    "edit_file": (
        "Sostituisce una porzione esatta di un file. old_string deve comparire "
        "una volta sola."
    ),
    "run_command": (
        "Esegue un comando di shell nel container del workspace, montato su "
        "/work, e ne restituisce stdout, stderr ed exit code."
    ),
    "manage_memory": "Salva, elenca o rimuove un fatto stabile sul progetto.",
    NOTES_TOOL: (
        "Foglio di appunti del compito in corso. Scrivici quello che scopri "
        "mentre lavori e che non si ricava rileggendo un file: perche' un test "
        "falliva, quale strada hai gia' provato e scartato, un vincolo detto "
        "dall'utente. Il piano dice a che punto sei, le note dicono cosa hai "
        "capito. Quando il contesto si riempie, la cronologia viene riassunta "
        "e i dettagli spariscono: le note restano, e sono l'unica cosa scritta "
        "da te che sopravvive parola per parola."
    ),
    PREVIEW_TOOL: (
        "Mostra qualcosa all'utente. action='file' con path per un documento "
        "del workspace: se e' una pagina HTML l'harness ne serve tutta la "
        "cartella e avvia da solo il backend del progetto, se ne riconosce "
        "uno. action='serve' con command e port per un'applicazione "
        "che resta in esecuzione -- non usare run_command per quelle, il "
        "timeout le uccide, e lega il processo a 0.0.0.0 o dal browser non si "
        "raggiunge. action='gui' con command per un programma con una finestra "
        "(tkinter, pygame, Qt): allo schermo ci pensa l'harness. "
        "action='terminal' apre una shell nel pannello, per l'utente. "
        "'logs' e 'stop' per il resto."
    ),
    # Come per ask_user_question, questa descrizione resta lunga anche nella
    # variante snella. E' il tool che decide se il turno sara' ordinato o un
    # unico ragionamento da ottomila token: accorciarlo qui significherebbe
    # toglierlo di fatto proprio ai modelli capaci, che sono quelli che
    # ricevono lo schema lean.
    PLAN_TOOL: (
        "Piano di lavoro visibile all'utente, che sopravvive fra i turni. "
        "CHIAMALO PER PRIMO quando la richiesta contiene piu' di una azione: "
        "action='set' con 3-8 punti verificabili. Il primo si apre da solo e "
        "ogni 'complete' (o 'skip') apre il successivo: 'start' serve solo per "
        "saltare fuori sequenza. Chiudi il punto nello stesso passo "
        "dell'ultima azione che lo conclude, non in un passo a parte. "
        "Il piano ti viene rimostrato ad ogni passo: leggilo e fai la mossa "
        "successiva, non ripianificare. Ogni punto comincia col tipo: "
        "'esegui: ', 'indaga: ', 'diagnosi: ' o 'progetta: '; nei punti di "
        "diagnosi tieni il registro con action='ipotesi'."
    ),
    # Questa descrizione resta lunga di proposito, contro la regola delle
    # altre. E' il tool che il modello non chiama mai da solo: accorciarla
    # significa toglierlo di fatto dal suo repertorio, ed e' esattamente
    # quello che si e' osservato quando era ridotta a una riga.
    ASK_USER_TOOL: (
        "Ferma il turno e chiede all'utente di scegliere, con 2-4 opzioni "
        "concrete e per prima quella che consigli. CHIAMALO ogni volta che "
        "stai per decidere tu qualcosa che l'utente non ha specificato e che "
        "non si deduce dal workspace: quale definizione dare a un requisito "
        "vago ('anomalo', 'valido', 'recente'), quale formato o libreria "
        "usare, quale interpretazione dare a una richiesta ambigua, o prima "
        "di sovrascrivere e cancellare. Se ti stai dicendo 'devo scegliere "
        "fra A e B', quella scelta non e' tua: chiedila. Non usarlo per "
        "leggere file o lanciare verifiche, quelle falle e basta."
    ),
}


def lean_tools_schema(schema: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Gli stessi tool, con le descrizioni ridotte all'essenziale."""
    out: list[dict[str, Any]] = []
    for tool in schema or TOOLS_SCHEMA:
        name = tool["function"]["name"]
        short = LEAN_TOOL_DESCRIPTIONS.get(name)
        if not short:
            out.append(tool)
            continue
        fn = dict(tool["function"])
        fn["description"] = short
        out.append({**tool, "function": fn})
    return out


TOOLS_SCHEMA_LEAN = lean_tools_schema()
# La versione snella del blocco condizionale: se il turno gira con gli schemi
# ridotti, anche il tool appeso deve esserlo, senno' web_search sarebbe l'unica
# voce con la descrizione lunga -- e la piu' verbosa dell'elenco.
WEB_SEARCH_TOOLS_LEAN = lean_tools_schema(WEB_SEARCH_TOOLS)


SNAPSHOT_DEPTH = 3
SNAPSHOT_MAX_ENTRIES = 140


def workspace_snapshot(
    workspace: str,
    *,
    depth: int = SNAPSHOT_DEPTH,
    max_entries: int = SNAPSHOT_MAX_ENTRIES,
) -> str:
    """Albero del workspace per l'environment header.

    Va **piu' in profondita' della sola radice** perche' questo albero e' il
    modo con cui l'agente "guarda il workspace" senza spendere un giro di
    generazione: se qui c'e' solo il primo livello, per sapere cosa contiene
    ``core/`` deve chiamare ``list_files``, cioe' pagare una chiamata al
    modello per un'informazione che costa una lettura di directory.

    Resta **deterministico** a parita' di contenuto del disco: ordinamento
    stabile, nessun timestamp, nessun contatore variabile. E' la condizione
    perche' il prefisso del prompt sia byte-identico fra i passi agentici e il
    KV cache venga riusato.
    """
    root = Path(workspace)
    if not root.is_dir():
        return "(workspace inesistente)"

    lines: list[str] = []
    truncated = False
    n_dirs = 0
    n_files = 0

    def walk(directory: Path, prefix: str, level: int) -> None:
        nonlocal truncated, n_dirs, n_files
        if level > depth or truncated:
            return
        try:
            entries = sorted(
                directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower())
            )
        except OSError:
            return
        for entry in entries:
            if _da_saltare(entry.name):
                continue
            if len(lines) >= max_entries:
                truncated = True
                return
            if entry.is_dir():
                n_dirs += 1
                lines.append(f"{prefix}{entry.name}/")
                walk(entry, prefix + "  ", level + 1)
            else:
                n_files += 1
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = 0
                lines.append(f"{prefix}{entry.name} ({size:,} B)")

    walk(root, "", 1)
    if not lines:
        return "(workspace vuoto)"

    tree = "\n".join(lines)
    footer = f"\n[{n_dirs} cartelle, {n_files} file entro {depth} livelli]"
    if truncated:
        footer = (
            f"\n[elenco troncato a {max_entries} voci: il progetto e' grande, "
            "usa search_files invece di esplorare a mano]"
        )
    return tree + footer


def platform_summary() -> str:
    return f"{platform.system()} {platform.release()} / Python {platform.python_version()}"
