"""Parametri consigliati per famiglia di modello.

Perche' esiste
--------------
I default dell'harness erano tarati su ``qwen2.5-coder:7b``: temperatura 0.2,
top_p 0.9, ``num_predict`` 2048, nessun ``top_k``, nessuna ``presence_penalty``.
Su un instruct che risponde e basta funzionano. Su un modello con canale di
pensiero sono sbagliati in un modo che non da' errore, solo risultati peggiori:

* ``num_predict: 2048`` conta **anche i token di pensiero**. Un blocco di
  ragionamento lungo esaurisce il budget prima che il modello arrivi a
  scrivere la risposta o la tool call: il turno finisce troncato a meta' e
  sembra che il modello "non abbia fatto niente".
* non mandare ``top_k`` non vuol dire "usa quello del modello": vuol dire
  lasciare quello di Ollama (40), mentre Qwen per la serie 3.5 ne raccomanda 20.
* la ``presence_penalty`` serve proprio ai modelli di pensiero, che senza
  girano in tondo sulla stessa frase.

Le raccomandazioni vengono dalle model card di Qwen e dalla pagina Ollama del
modello; il numero di contesto invece non puo' venire da li', perche' dipende
dalla VRAM di chi lo esegue: vedi :func:`context_for_vram`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Profile:
    """Parametri consigliati per una famiglia di modelli."""

    name: str
    temperature: float
    top_p: float
    top_k: int
    presence_penalty: float
    # Budget di generazione, token di pensiero inclusi.
    max_tokens: int
    note: str


# Qwen3.5 con canale di pensiero, profilo "coding" della model card:
# temperatura piu' bassa e nessuna presence_penalty, perche' sul codice la
# penalita' spinge il modello a variare identificatori che devono restare uguali.
QWEN35_CODING = Profile(
    name="Qwen3.5 (pensiero, codice)",
    temperature=0.6,
    top_p=0.95,
    top_k=20,
    presence_penalty=0.0,
    max_tokens=16384,
    note=(
        "Profilo 'thinking / coding' della model card Qwen3.5. Il budget di "
        "generazione e' alto perche' comprende il blocco di pensiero: con 2048 "
        "il modello resta senza token prima di arrivare alla tool call."
    ),
)

# Qwen3.8 27B con canale di pensiero. Rispetto al 3.5 quello che cambia
# davvero e' il budget di generazione: e' un modello che pensa piu' a lungo
# prima di agire, e su un ciclo agentico il pensiero e' proprio la parte che
# non si vuole vedere troncata a meta'. 32k di num_predict non sono 32k spesi:
# e' un tetto, non una prenotazione, e ``clamp_generation`` lo riduce comunque
# a meta' finestra.
QWEN38_CODING = Profile(
    name="Qwen3.8 (pensiero, codice)",
    temperature=0.6,
    top_p=0.95,
    top_k=20,
    presence_penalty=0.0,
    max_tokens=32768,
    note=(
        "Profilo 'thinking / coding' della serie Qwen3.x con budget di "
        "generazione largo: su un 27B il solo blocco di pensiero puo' superare "
        "i 4.000 token, e un num_predict stretto lo tronca prima che il modello "
        "arrivi alla tool call."
    ),
)

QWEN3_CODER = Profile(
    name="Qwen3-Coder",
    temperature=0.7,
    top_p=0.8,
    top_k=20,
    presence_penalty=1.0,
    max_tokens=8192,
    note="Instruct senza canale di pensiero: temperatura media e penalita' attiva.",
)

QWEN25_CODER = Profile(
    name="Qwen2.5-Coder",
    temperature=0.2,
    top_p=0.9,
    top_k=40,
    presence_penalty=0.0,
    max_tokens=2048,
    note=(
        "I valori con cui l'harness e' nato. Modello che non fa tool calling "
        "nativo affidabile: serve la rete di recupero delle chiamate testuali."
    ),
)

GENERIC_THINKING = Profile(
    name="Modello con pensiero (generico)",
    temperature=0.6,
    top_p=0.95,
    top_k=20,
    presence_penalty=0.0,
    max_tokens=8192,
    note="Il modello dichiara la capability 'thinking': budget di token largo.",
)

GENERIC_INSTRUCT = Profile(
    name="Instruct (generico)",
    temperature=0.3,
    top_p=0.9,
    top_k=40,
    presence_penalty=0.0,
    max_tokens=4096,
    note="Nessuna raccomandazione specifica nota per questo modello.",
)

# L'ordine conta: la prima espressione che combacia vince. Le versioni
# puntate vanno controllate prima di 'qwen3', o la regola generica se le
# mangerebbe. Attenzione al punto: in una regex `.` combacia con qualunque
# carattere, quindi `qwen3.5` senza escape combacerebbe anche con `qwen3-5` e,
# peggio, `qwen3\.5` non deve essere scritto come `qwen35`.
_RULES: tuple[tuple[re.Pattern[str], Profile], ...] = (
    (re.compile(r"qwen-?3\.8", re.I), QWEN38_CODING),
    (re.compile(r"qwen-?3\.5", re.I), QWEN35_CODING),
    (re.compile(r"qwen-?3-?coder", re.I), QWEN3_CODER),
    (re.compile(r"qwen-?2\.5-?coder", re.I), QWEN25_CODER),
)


def profile_for(model: str, *, thinking: bool | None = None) -> Profile:
    """Profilo consigliato per un nome di modello.

    ``thinking`` e' quello che Ollama dichiara nelle capability: quando il nome
    non e' riconosciuto, e' l'indizio migliore per scegliere fra un budget di
    token largo e uno stretto.
    """
    for pattern, profile in _RULES:
        if pattern.search(model or ""):
            return profile
    return GENERIC_THINKING if thinking else GENERIC_INSTRUCT


# --- dimensionamento del contesto -------------------------------------------
# Il contesto non e' una preferenza: e' memoria. Se non ci sta in VRAM, Ollama
# scarica strati sulla CPU e la generazione rallenta di un ordine di grandezza.
# Meglio un contesto onesto che uno grande e inutilizzabile.

_CTX_STEPS = (8192, 16384, 24576, 32768, 49152, 65536, 98304, 131072)

# Margine lasciato libero sulla scheda: buffer di calcolo, frammentazione e il
# resto del desktop. Senza, il contesto "ci sta" sulla carta e non nei fatti.
_VRAM_HEADROOM_MB = 700

# Costo in MB per token di KV cache quando l'architettura del modello non e'
# nota. Era l'unico valore usato, ed era **sbagliato in modi opposti a seconda
# del modello**: il costo del KV cache scala con il numero di strati e di teste
# KV, quindi un 7B costa circa 0,05 MB/token e un 27B circa 0,19. Una costante
# sola o e' troppo generosa sui modelli grandi (contesto che non ci sta, strati
# scaricati sulla CPU, generazione dieci volte piu' lenta) o troppo avara su
# quelli piccoli. Resta come ripiego, ma la strada buona e'
# ``kv_mb_per_token``, che legge i numeri veri dai metadati GGUF.
_FALLBACK_MB_PER_TOKEN = 0.10

# Byte per elemento del KV cache. 2 = fp16, il default di Ollama. Chi lancia il
# server con OLLAMA_KV_CACHE_TYPE=q8_0 dimezza questo costo, ma la variabile
# non e' esposta dall'API: preferiamo sbagliare per eccesso, perche' il prezzo
# di un contesto piu' piccolo del necessario e' molto piu' basso di quello di
# un modello che finisce mezzo in RAM di sistema.
_KV_BYTES_PER_ELEMENT = 2


def kv_mb_per_token(model_info: dict | None) -> float | None:
    """MB di KV cache per token, dai metadati GGUF di ``/api/show``.

    La formula e' esatta, non una stima: il KV cache tiene una chiave e un
    valore per ogni strato e per ogni testa KV, quindi

        byte/token = 2 (K e V) * strati * teste_kv * dim_testa * byte_elemento

    Ollama espone questi numeri in ``model_info`` con le chiavi GGUF
    ``<arch>.block_count``, ``<arch>.attention.head_count_kv`` e
    ``<arch>.attention.key_length``. Il prefisso ``<arch>`` cambia col modello
    (``qwen3``, ``llama``, ``gemma3``...), quindi le chiavi si cercano per
    suffisso invece che per nome esatto: cablare "qwen3." significherebbe
    riscrivere questa funzione ad ogni famiglia nuova.

    Restituisce None quando i metadati non bastano: il chiamante ripiega sulla
    costante, che e' meglio di un numero inventato con sicurezza.
    """
    if not isinstance(model_info, dict) or not model_info:
        return None

    def find(suffix: str) -> int | None:
        for key, value in model_info.items():
            if key.endswith(suffix) and isinstance(value, (int, float)):
                return int(value)
        return None

    layers = find(".block_count")
    kv_heads = find(".attention.head_count_kv")
    if not layers or not kv_heads:
        return None

    head_dim = find(".attention.key_length")
    if not head_dim:
        # Ricavabile: dimensione dell'embedding diviso il numero di teste di
        # attenzione. Vale per le architetture che non dichiarano key_length.
        embedding = find(".embedding_length")
        heads = find(".attention.head_count")
        if not embedding or not heads:
            return None
        head_dim = embedding // heads

    bytes_per_token = 2 * layers * kv_heads * head_dim * _KV_BYTES_PER_ELEMENT
    return bytes_per_token / (1024 * 1024)


def context_for_vram(free_mb: int | None, mb_per_token: float | None = None) -> int:
    """Contesto che ci sta nella VRAM **libera adesso**.

    Il numero da passare e' la VRAM libera **con il modello gia' caricato**:
    e' l'unica misura onesta, perche' tiene conto della quantizzazione reale,
    degli strati gia' in VRAM e di quello che sta usando il resto del sistema.
    Stimare il peso del modello dal nome sarebbe indovinare.

    ``mb_per_token`` viene da :func:`kv_mb_per_token` quando i metadati del
    modello sono disponibili. Senza, si usa il ripiego -- con l'avvertenza che
    su un modello grande e' ottimista.
    """
    cost = mb_per_token if mb_per_token and mb_per_token > 0 else _FALLBACK_MB_PER_TOKEN
    if not free_mb or free_mb <= _VRAM_HEADROOM_MB:
        return 8192
    affordable = int((free_mb - _VRAM_HEADROOM_MB) / cost)
    for step in reversed(_CTX_STEPS):
        if step <= affordable:
            return step
    return 8192


def clamp_generation(max_tokens: int, num_ctx: int) -> int:
    """Un budget di generazione piu' grande del contesto non esiste.

    ``num_predict`` e ``num_ctx`` condividono la stessa finestra: se il primo
    supera il secondo, il valore in eccesso e' pura fantasia e in piu' non
    lascia spazio al prompt. Meta' finestra e' il tetto ragionevole.
    """
    return max(512, min(int(max_tokens), int(num_ctx) // 2))


def as_settings(profile: Profile, *, num_ctx: int | None = None) -> dict[str, object]:
    """Il profilo tradotto nelle chiavi delle impostazioni dell'harness."""
    values: dict[str, object] = {
        "temperature": profile.temperature,
        "top_p": profile.top_p,
        "top_k": profile.top_k,
        "presence_penalty": profile.presence_penalty,
    }
    if num_ctx:
        values["num_ctx"] = int(num_ctx)
        values["max_tokens"] = clamp_generation(profile.max_tokens, num_ctx)
    else:
        values["max_tokens"] = profile.max_tokens
    return values
