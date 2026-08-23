"""Default, parametri di generazione e budget di contesto.

Un solo posto per i valori predefiniti: ``DEFAULTS`` e' anche la specifica di
cosa e' un'impostazione valida -- ``core/settings.py`` ci fa il merge con quello
salvato su disco e scarta le chiavi che qui non esistono.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

APP_NAME = "Local Agent Harness"
# Unica fonte di verita' della versione. ``pyproject.toml`` deve riportare la
# stessa stringa: era rimasto a 2.11.0 mentre qui si era arrivati a 2.27, e
# nessuno se n'era accorto perche' niente li confrontava. Adesso lo fa un test
# (``tests/test_core.py::test_la_versione_e_la_stessa_in_pyproject``).
#
# Il numero si muove cosi': terza cifra per una correzione, seconda per una
# funzione nuova, prima per un cambio che rompe le sessioni salvate o le
# preferenze. 2.31.0 e' stato il merge del fork Qwen; 2.32.0 aggiunge alla
# sola interfaccia mobile i passi del turno e la striscia dell'attivita'.
APP_VERSION = "2.32.0"

# --- percorsi di persistenza ------------------------------------------------
DATA_DIR = Path("chat_sessions")
MEMORY_FILE = Path("agent_memories.json")
# Skill valide sempre (accanto all'harness) e skill del progetto aperto, che
# viaggiano con il repo. Le seconde vincono sulle prime a parita' di nome.
SKILLS_DIR = Path("skills")
SKILLS_SUBDIR_WORKSPACE = ".agente/skills"
# Preferenze dell'utente fra un avvio e l'altro. Era l'unico pezzo di stato che
# non sopravviveva al riavvio: conversazioni e memorie erano gia' su disco.
SETTINGS_FILE = Path("agent_settings.json")

# --- budget di contesto -----------------------------------------------------
# Limiti di troncamento dei risultati dei tool. Sono la leva piu' efficace sul
# throughput: un `pytest -v` non troncato puo' da solo saturare num_ctx.
#
# I valori qui sotto sono la **taratura di riferimento per una finestra da
# 16k**, non delle costanti universali: sono nati quando l'harness girava su un
# 7B con 16384 token di contesto, e restano il default quando la finestra vera
# non e' nota. Con una finestra piu' grande vanno riscalati -- vedi
# ``budgets_for``. Tenerli fissi su una finestra da 64k significherebbe
# troncare un file a 12.000 caratteri avendo lo spazio per tenerlo intero: il
# modello lavorerebbe su un frammento senza sapere di averne uno, che e' il
# modo piu' silenzioso di sbagliare.
BASE_NUM_CTX = 16_384

TOOL_RESULT_MAX_CHARS = 6_000
READ_FILE_MAX_CHARS = 12_000
COMMAND_STDOUT_MAX_CHARS = 5_000
COMMAND_STDERR_MAX_CHARS = 3_000
LIST_FILES_MAX_ENTRIES = 300
SEARCH_MAX_MATCHES = 80

# Oltre questa percentuale di num_ctx la cronologia viene compattata.
HISTORY_COMPACT_THRESHOLD = 0.75
# I risultati dei tool piu' vecchi di N passi vengono ridotti a un sommario.
TOOL_RESULT_FULL_WINDOW = 3

# Estremi dello scalatore. Sotto: una finestra da 8k non deve ereditare budget
# pensati per 16k, o il primo read_file la riempie da sola. Sopra: il tetto
# esiste perche' un singolo risultato di tool non deve mai poter occupare una
# frazione sproporzionata della finestra, per quanto grande sia.
MIN_BUDGET_SCALE = 0.5
MAX_BUDGET_SCALE = 8.0

# Tetto alla finestra dei risultati integrali. Cresce con il contesto perche'
# e' la difesa piu' efficace contro il costo *vero* della compattazione: ogni
# risultato compattato riscrive un messaggio in mezzo alla cronologia, il
# prefisso diverge da quello del passo precedente e il KV cache di Ollama va
# ricalcolato da li' in poi. Su una finestra larga la compattazione e' un
# risparmio di token pagato con un ricalcolo di prompt: conviene non farla.
MAX_TOOL_RESULT_FULL_WINDOW = 16


@dataclass(frozen=True, slots=True)
class Budgets:
    """Limiti di troncamento per una specifica finestra di contesto.

    Esiste per un motivo preciso: i budget non sono una preferenza dell'utente
    ma una funzione della finestra. Passarli come oggetto invece di leggerli
    da costanti di modulo permette a ``tools.py`` e ``agent.py`` di lavorare
    con la finestra *reale* del turno in corso, senza doversi accordare su una
    variabile globale che cambierebbe sotto i piedi a due turni concorrenti --
    e nell'harness i turni concorrenti esistono davvero (``RUNNERS``).
    """

    tool_result_max_chars: int = TOOL_RESULT_MAX_CHARS
    read_file_max_chars: int = READ_FILE_MAX_CHARS
    command_stdout_max_chars: int = COMMAND_STDOUT_MAX_CHARS
    command_stderr_max_chars: int = COMMAND_STDERR_MAX_CHARS
    list_files_max_entries: int = LIST_FILES_MAX_ENTRIES
    search_max_matches: int = SEARCH_MAX_MATCHES
    tool_result_full_window: int = TOOL_RESULT_FULL_WINDOW
    scale: float = 1.0


def context_scale(num_ctx: int) -> float:
    """Quanto e' piu' larga la finestra rispetto alla taratura di riferimento.

    Scala **lineare** di proposito. La tentazione e' di crescere sublineare
    ("un file da 48.000 caratteri e' comunque troppo"), ma i rapporti scelti a
    16k -- un read_file vale circa un quinto della finestra, uno stdout circa
    un decimo -- sono gli stessi rapporti che si vogliono a 64k. Il numero
    assoluto e' cambiato perche' e' cambiata la finestra, non perche' e'
    cambiato il compito.
    """
    if num_ctx <= 0:
        return 1.0
    raw = float(num_ctx) / float(BASE_NUM_CTX)
    return max(MIN_BUDGET_SCALE, min(MAX_BUDGET_SCALE, raw))


def budgets_for(num_ctx: int) -> Budgets:
    """Budget di troncamento adatti alla finestra passata."""
    scale = context_scale(num_ctx)
    return Budgets(
        tool_result_max_chars=int(TOOL_RESULT_MAX_CHARS * scale),
        read_file_max_chars=int(READ_FILE_MAX_CHARS * scale),
        command_stdout_max_chars=int(COMMAND_STDOUT_MAX_CHARS * scale),
        command_stderr_max_chars=int(COMMAND_STDERR_MAX_CHARS * scale),
        # Le liste crescono piu' piano dei testi: 300 file gia' bastano a
        # capire com'e' fatto un progetto, e la voce 301 informa molto meno del
        # 301-esimo carattere di un errore.
        list_files_max_entries=int(LIST_FILES_MAX_ENTRIES * min(scale, 3.0)),
        search_max_matches=int(SEARCH_MAX_MATCHES * min(scale, 3.0)),
        tool_result_full_window=min(
            MAX_TOOL_RESULT_FULL_WINDOW,
            max(1, int(round(TOOL_RESULT_FULL_WINDOW * scale))),
        ),
        scale=scale,
    )

@dataclass(slots=True)
class GenParams:
    """Parametri di generazione passati al backend."""

    model: str = "qwen2.5-coder:7b"
    temperature: float = 0.2
    top_p: float = 0.9
    # I modelli Qwen recenti raccomandano esplicitamente top_k=20 e una
    # presence_penalty non nulla. Non inviarli non significa "usa i default del
    # modello": significa lasciare quelli di Ollama (top_k 40), che per una
    # MoE con canale di pensiero sono un'altra cosa.
    top_k: int = 40
    presence_penalty: float = 0.0
    # Il default di Ollama e' 1.1: a quel valore penalizza ogni ripetizione,
    # comprese quelle delle parole comuni e degli identificatori. 1.0 e' il
    # neutro e non viene inviato, come presence_penalty: un'opzione in meno
    # nel payload e' un prefisso in meno da invalidare.
    #
    # Il campo si chiama repetition_penalty perche' e' cosi' che lo chiamano
    # l'impostazione, l'interfaccia e la letteratura (vLLM, HF). Sul filo
    # nativo di Ollama la chiave e' un'altra -- ``repeat_penalty`` -- e
    # sbagliarla non da' errore: Ollama scarta in silenzio le opzioni che non
    # conosce, e la manopola sembra funzionare senza fare niente. La
    # traduzione avviene in ``ollama_options`` ed e' coperta da un test.
    repetition_penalty: float = 1.0
    max_tokens: int = 2048
    num_ctx: int = 16384
    num_gpu: int = 999
    keep_alive: str = "30m"
    # False | True | "low" | "medium" | "high" | "max". Ollama accetta sia il
    # booleano sia il livello; il livello permette di pagare il pensiero lungo
    # solo quando serve davvero.
    think: bool | str = False
    seed: int | None = None
    stop: list[str] = field(default_factory=list)

    def ollama_options(self) -> dict[str, Any]:
        """Blocco ``options`` per ``POST /api/chat`` di Ollama.

        Sono questi i parametri che ``/v1/chat/completions`` ignora in
        silenzio: e' il motivo per cui il transport nativo non e' opzionale.
        """
        opts: dict[str, Any] = {
            "temperature": float(self.temperature),
            "top_p": float(self.top_p),
            "top_k": int(self.top_k),
            "num_ctx": int(self.num_ctx),
            "num_predict": int(self.max_tokens),
            "num_gpu": int(self.num_gpu),
        }
        if self.presence_penalty:
            opts["presence_penalty"] = float(self.presence_penalty)
        # Qui il nome cambia: fuori e' repetition_penalty, sul filo di Ollama
        # e' repeat_penalty. Vedi il commento sul campo.
        if self.repetition_penalty != 1.0:
            opts["repeat_penalty"] = float(self.repetition_penalty)
        if self.seed is not None:
            opts["seed"] = int(self.seed)
        if self.stop:
            opts["stop"] = list(self.stop)
        return opts

    @property
    def think_payload(self) -> bool | str | None:
        """Valore da mettere in ``think``, o None per ometterlo del tutto."""
        if isinstance(self.think, str):
            level = self.think.strip().lower()
            if level in {"low", "medium", "high", "max"}:
                return level
            return level in {"si", "sì", "true", "on", "1"}
        return bool(self.think) or None


DEFAULTS: dict[str, Any] = {
    # connessione
    "api_base": "http://localhost:11434",
    # Vuoto di proposito: Ollama non chiede autenticazione e OpenAICompatBackend
    # ripiega da solo su "not-needed". Il vecchio default "ollama" finiva in un
    # campo password e faceva scattare l'allarme "credenziale compromessa" di
    # Chrome, perche' e' una stringa presente negli archivi di data breach.
    "api_key": "",
    "model_name": "qwen2.5-coder:7b",
    "transport": "auto",  # auto | ollama | openai
    # Ollama < 0.8.0 non emette tool call quando stream=true: la richiesta
    # riesce, il testo arriva, ma le tool_calls no. 'auto' rileva la versione
    # del server e sceglie da solo.
    "stream_tools": "auto",  # auto | sempre | mai
    "timeout_seconds": 180,
    # generazione
    "temperature": 0.2,
    "top_p": 0.9,
    # Qwen 3.x raccomanda top_k 20; 40 e' il default di Ollama.
    "top_k": 40,
    "presence_penalty": 0.0,
    # 1.0 e' il neutro: Ollama da solo lascierebbe il suo default (1.1), che
    # penalizza anche le ripetizioni delle parole comuni.
    "repetition_penalty": 1.0,
    "max_tokens": 2048,
    "num_ctx": 16384,
    "num_gpu": 999,
    "keep_alive": "30m",
    # VRAM totale della scheda che esegue il modello, in MB. Serve **solo**
    # quando Ollama gira su un'altra macchina: li' `nvidia-smi` non e'
    # raggiungibile e l'unico dato che l'API espone e' quanto occupano i
    # modelli caricati (`/api/ps`), non quanto e' grande la scheda. Con questo
    # numero dichiarato una volta, il profilo consigliato torna a proporre un
    # contesto sensato invece di ripiegare sul minimo.
    # 0 = non dichiarata (endpoint locale, o si preferisce non stimare).
    "gpu_total_vram_mb": 0,
    # auto | si | no | low | medium | high | max.
    # 'auto' rileva la capability "thinking" del modello (qwen3.x, deepseek-r1)
    # e accende il canale nativo solo dove esiste davvero. I livelli li accetta
    # Ollama dalle versioni recenti: 'max' per progettare, 'low' per le
    # modifiche meccaniche, dove il pensiero lungo e' solo tempo di GPU buttato.
    "native_think": "auto",
    "max_agent_loops": 12,
    # workspace
    "workspace_dir": os.getcwd(),
    # Cartelle su cui si e' lavorato di recente, la piu' fresca in testa e la
    # corrente inclusa. Serve alla tendina in alto a sinistra: chi alterna due
    # o tre progetti non deve ripassare dal selettore di sistema per tornare
    # indietro. Tenuta corta di proposito -- oltre la mezza dozzina non e' piu'
    # un elenco di scorciatoie ma una cronologia da leggere.
    "recent_workspaces": [],
    # Vault LLM Wiki registrati: percorsi di workspace che contengono una wiki
    # mantenuta dall'agente (raw/ + wiki/). La sezione "Vault" della colonna
    # sinistra li elenca; il tool vault_search li interroga senza cambiare
    # workspace. Lista di dict {path, nome}: il nome e' quello scelto
    # dall'utente, non la basename, perche' i vault si riconoscono dal tema.
    "vaults": [],
    "confirm_commands": False,
    # Dove girano i comandi di run_command.
    #   docker = container che monta solo il workspace su /work (predefinito)
    #   host   = esecuzione diretta: l'agente vede tutto il disco
    # Il default e' 'docker' perche' una funzione di sicurezza deve fallire
    # chiusa: meglio un comando che non parte di uno che gira senza recinto.
    "sandbox": "docker",
    "docker_image": "python:3.12-slim",
    "sandbox_network": True,
    # --- anteprime ---
    # Il pannello si apre da solo quando l'agente scrive un file con una resa
    # visiva (.md, .html, .svg, immagini, .pdf). Sui file di codice no: dieci
    # write_file di fila sarebbero dieci anteprime che nessuno ha chiesto.
    # All'avvio l'harness prova ad accendere Docker da solo, e alla scelta di
    # un workspace nuovo costruisce l'immagine se manca. Sono le due attese che
    # l'utente scopriva a meta' del primo turno, sotto forma di errore.
    "docker_autostart": True,
    "image_autobuild": True,
    "preview_enabled": True,
    # Porte pubblicate dal container verso 127.0.0.1, per far vedere nel
    # browser le applicazioni che l'agente avvia. E' una concessione sulla
    # tenuta della sandbox -- misurata: solo questa macchina, solo questo
    # intervallo -- quindi si puo' spegnere.
    "preview_ports_enabled": True,
    "preview_port_base": 8200,
    "preview_port_count": 4,
    # efficienza
    # Se la richiesta ha piu' obiettivi e il modello lavora senza un piano,
    # l'harness glielo chiede. Vedi core/plan.py per il perche'.
    "require_plan": True,
    # Su un piano lungo il turno si ferma e te lo mostra prima di cominciare:
    # correggerlo costa dieci secondi, valutare trenta passi no.
    "plan_gate": True,
    # Interrompe un ragionamento che supera il 55% del budget di generazione
    # senza aver chiamato nessun tool. E' la difesa contro il modo di fallire
    # piu' costoso osservato: ottomila token di pensiero e zero azioni.
    "think_watchdog": True,
    "strip_think_from_context": True,
    "compact_old_tool_results": True,
    # Quando il contesto si riempie, il tratto vecchio viene riassunto da una
    # chiamata dedicata e tolto dalla vista del modello -- nella chat resta.
    # Costa una generazione e un ricalcolo del KV cache, per questo la soglia
    # e' alta: sotto non succede niente.
    "compact_history": True,
    "compact_threshold": HISTORY_COMPACT_THRESHOLD,
    "auto_env_header": True,
    # UI
    "theme_mode": "light",  # light | dark
    "show_right_panel": True,
    "show_left_sidebar": True,
    "expand_thoughts": True,
    # runtime
    "agent_running": False,
    "pending_prompt": None,
    "last_usage": {},
}


def resolve_tristate(value: Any, detected: bool | None) -> bool:
    """Traduce un'impostazione ``auto | si | no`` in un booleano.

    Con ``auto`` vince quello che il server dichiara del modello; se non lo
    sa dire, si sceglie il comportamento prudente (disattivato).
    """
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"si", "sì", "yes", "true", "on", "1"}:
        return True
    if text in {"no", "false", "off", "0"}:
        return False
    return bool(detected)


THINK_LEVELS = ("low", "medium", "high", "max")


def resolve_think(value: Any, detected: bool | None) -> bool | str:
    """Come ``resolve_tristate``, ma conserva il livello di pensiero.

    Un livello esplicito e' un'informazione in piu' rispetto a "acceso":
    schiacciarlo su ``True`` butterebbe via proprio la manopola che serve a
    non pagare un ragionamento lungo per una modifica meccanica.
    """
    text = str(value or "").strip().lower()
    if text in THINK_LEVELS:
        return text
    return resolve_tristate(value, detected)
