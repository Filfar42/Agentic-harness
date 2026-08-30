"""Test dei budget derivati dalla finestra e del rilevamento VRAM remoto.

Coprono la modifica fatta quando il modello si e' spostato su una seconda
macchina della rete, molto piu' capace di quella che esegue l'harness. Due
categorie di bug, entrambe silenziose:

* **budget tarati su una finestra che non e' piu' quella.** I limiti di
  troncamento erano costanti calibrate su 16k. Su una finestra da 64k
  troncavano un file a 12.000 caratteri avendo spazio per tenerlo intero: il
  modello lavorava su un frammento senza sapere di averne uno.
* **hardware misurato sulla macchina sbagliata.** ``nvidia-smi`` girava in
  locale, cioe' dove il modello non c'e' piu'. Non dava errore: dava un numero
  plausibile e falso, o nessun numero -- e da li' il "profilo consigliato"
  proponeva di **dimezzare** il contesto su una scheda che ne reggeva quattro
  volte tanto.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import agent as agent_mod  # noqa: E402
from core import config as config_mod  # noqa: E402
from core import profiles  # noqa: E402
from core.backend import OllamaBackend  # noqa: E402
from core.config import GenParams, budgets_for, context_scale  # noqa: E402
from core.tools import TOOLS_SCHEMA, ToolContext, dispatch  # noqa: E402

import tests.test_agent_loop as fake  # noqa: E402

fake_ollama = fake.fake_ollama


# ---------------------------------------------------------------------------
# Budget derivati dalla finestra
# ---------------------------------------------------------------------------


def test_alla_finestra_di_riferimento_i_budget_non_cambiano():
    """16k e' la taratura storica: li' i numeri devono restare identici.

    E' il test che rende sicura tutta la modifica: se la scala fosse sbagliata
    di un fattore, si vedrebbe qui prima che altrove.
    """
    b = budgets_for(config_mod.BASE_NUM_CTX)
    assert b.read_file_max_chars == config_mod.READ_FILE_MAX_CHARS
    assert b.tool_result_max_chars == config_mod.TOOL_RESULT_MAX_CHARS
    assert b.tool_result_full_window == config_mod.TOOL_RESULT_FULL_WINDOW
    assert b.scale == 1.0


def test_una_finestra_doppia_raddoppia_i_testi():
    """La scala resta lineare finche' la finestra su cui si compatta cresce."""
    b = budgets_for(32_768)
    assert b.scale == 2.0
    assert b.read_file_max_chars == 2 * config_mod.READ_FILE_MAX_CHARS
    assert b.command_stderr_max_chars == 2 * config_mod.COMMAND_STDERR_MAX_CHARS


def test_oltre_il_tetto_di_compattazione_i_budget_smettono_di_crescere():
    """Il difetto misurato il 30/08/2026, messo nero su bianco.

    ``budgets_for`` scalava su ``num_ctx``, ``finestra_efficace`` tagliava al
    tetto assoluto, e sopra i 32k le due politiche divergevano: a 131.072 il
    codice si impegnava a tenere integrali fino a 384.000 token di risultati in
    una finestra che compatta a 32.767 -- fattore 11,7 -- e la compattazione
    scattava al **terzo passo su dodici**. La curva non era monotona: il punto
    migliore era 32k, e da li' allargare la finestra peggiorava il contesto.

    Adesso i budget si tarano sulla finestra su cui si compatta davvero, quindi
    sopra il tetto restano fermi invece di crescere per uno spazio che non c'e'.
    """
    grande = budgets_for(131_072)
    enorme = budgets_for(262_144)
    assert grande.read_file_max_chars == enorme.read_file_max_chars
    assert grande.tool_result_full_window == enorme.tool_result_full_window
    # e sono comunque piu' larghi di quelli a 16k: il tetto e' un tetto, non un
    # ritorno alla taratura base
    assert grande.read_file_max_chars > budgets_for(config_mod.BASE_NUM_CTX).read_file_max_chars


def test_le_liste_crescono_meno_dei_testi():
    """La voce 301 di un albero informa molto meno del carattere 301 di uno stderr."""
    b = budgets_for(131_072)
    base = budgets_for(config_mod.BASE_NUM_CTX)
    cresciuta_lista = b.list_files_max_entries / base.list_files_max_entries
    cresciuto_testo = b.read_file_max_chars / base.read_file_max_chars
    assert cresciuta_lista <= cresciuto_testo
    assert b.list_files_max_entries <= 3 * config_mod.LIST_FILES_MAX_ENTRIES
    assert b.search_max_matches <= 3 * config_mod.SEARCH_MAX_MATCHES


@pytest.mark.parametrize(
    "num_ctx", [4_096, 8_192, 16_384, 24_576, 32_768, 49_152, 65_536, 131_072, 262_144]
)
def test_i_risultati_integrali_non_superano_la_soglia_che_li_fa_compattare(num_ctx):
    """L'invariante che mancava fra ``budgets_for`` e ``finestra_efficace``.

    Non e' una preferenza di taratura: e' la relazione che rende sensata la
    coppia. Se i risultati che ci si impegna a tenere interi pesano piu' della
    quota della soglia, la compattazione scatta per colpa dei budget -- e piu'
    grande e' la finestra, prima scatta.
    """
    b = budgets_for(num_ctx)
    soglia = int(
        config_mod.finestra_efficace(num_ctx) * config_mod.HISTORY_COMPACT_THRESHOLD
    )
    picco = b.tool_result_full_window * (b.read_file_max_chars // 4)
    assert picco <= soglia * config_mod.QUOTA_RISULTATI_INTEGRALI, (
        f"a num_ctx={num_ctx} i {b.tool_result_full_window} risultati tenuti "
        f"integrali pesano {picco} token contro una soglia di {soglia}"
    )
    assert b.tool_result_full_window >= min(
        config_mod.MIN_RISULTATI_INTEGRALI, b.tool_result_full_window
    )
    assert b.tool_result_full_window >= 1


def test_una_finestra_stretta_non_eredita_budget_larghi():
    """Con 4k un solo read_file non deve poter riempire tutta la finestra."""
    b = budgets_for(4096)
    assert b.scale == config_mod.MIN_BUDGET_SCALE
    assert b.read_file_max_chars < config_mod.READ_FILE_MAX_CHARS
    assert b.tool_result_full_window >= 1     # mai zero: azzererebbe il contesto utile


def test_la_scala_ha_un_tetto():
    """Il tetto non e' piu' MAX_BUDGET_SCALE ma la finestra efficace.

    Che e' un tetto piu' basso e piu' onesto: MAX_BUDGET_SCALE limitava la
    crescita a un numero scelto a mano, ``finestra_efficace`` la limita a
    quanto contesto si puo' davvero usare prima di compattare.
    """
    atteso = config_mod.finestra_efficace(10_000_000) / config_mod.BASE_NUM_CTX
    assert context_scale(10_000_000) == pytest.approx(atteso)
    assert context_scale(10_000_000) < config_mod.MAX_BUDGET_SCALE


def test_la_finestra_dei_risultati_integrali_ha_un_tetto():
    """Cresce, ma non all'infinito: oltre un certo punto e' solo peso morto.

    Il tetto vero non e' piu' ``MAX_TOOL_RESULT_FULL_WINDOW`` (che resta come
    limite superiore) ma la quota della soglia: quanti risultati interi ci
    stanno senza far scattare la compattazione da soli.
    """
    b = budgets_for(10_000_000)
    assert b.tool_result_full_window <= config_mod.MAX_TOOL_RESULT_FULL_WINDOW
    assert b.tool_result_full_window >= config_mod.MIN_RISULTATI_INTEGRALI


def test_num_ctx_assurdo_non_fa_esplodere_niente():
    assert context_scale(0) == 1.0
    assert context_scale(-1) == 1.0


# ---------------------------------------------------------------------------
# I budget arrivano davvero fino ai tool
# ---------------------------------------------------------------------------


def test_read_file_tronca_secondo_la_finestra(tmp_path):
    """Il collegamento fra num_ctx e il troncamento reale, non solo il calcolo.

    Senza questo test si potrebbe rifattorizzare ``budgets_for`` alla
    perfezione e lasciare i tool a leggere ancora le costanti.
    """
    grosso = tmp_path / "grosso.py"
    grosso.write_text("x = 1\n" * 20_000, encoding="utf-8")

    stretto = ToolContext(
        workspace=str(tmp_path), sandbox="host", budgets=budgets_for(16_384)
    )
    largo = ToolContext(
        workspace=str(tmp_path), sandbox="host", budgets=budgets_for(32_768)
    )

    corto = dispatch(stretto, "read_file", {"filepath": "grosso.py"})
    lungo = dispatch(largo, "read_file", {"filepath": "grosso.py"})
    assert len(lungo) > len(corto) * 1.8


def test_il_ciclo_agentico_imposta_i_budget_sul_contesto(fake_ollama, tmp_path):
    """run_turn deve sovrascrivere il default del ToolContext, non fidarsene."""
    url, _ = fake_ollama
    ctx = ToolContext(workspace=str(tmp_path), sandbox="host")
    assert ctx.budgets.scale == 1.0          # default: taratura per 16k

    list(
        agent_mod.run_turn(
            backend=OllamaBackend(url, timeout_s=20),
            params=GenParams(model="fake:latest", num_ctx=32_768),
            tools_schema=TOOLS_SCHEMA,
            tool_ctx=ctx,
            ui_messages=[{"role": "user", "content": "ciao"}],
            system_prompt="SYS",
            env_header=None,
            max_steps=3,
        )
    )
    assert ctx.budgets.scale == 2.0


def test_allargare_la_finestra_non_fa_compattare_prima():
    """La non-monotonia misurata il 30/08/2026, chiusa con un test.

    Prima: 16k non compattava mai in dodici passi, 32k nemmeno, 49k al sesto,
    65k al quinto, 128k **al terzo**. Il punto migliore della curva era 32k, e
    da li' in poi ogni aumento di ``num_ctx`` peggiorava il comportamento del
    contesto -- cioe' comprare un modello con la finestra piu' grande lo
    rendeva peggiore, e l'harness non lo diceva.

    La causa erano due politiche sullo stesso parametro che misuravano finestre
    diverse: ``budgets_for`` scalava su ``num_ctx``, ``finestra_efficace``
    tagliava al tetto assoluto. Qui si prova la proprieta' che deve valere
    comunque le si tari: **una finestra piu' grande non puo' compattare prima
    di una piu' piccola.**
    """
    import json as _json

    def passo(i: int, caratteri: int) -> list[dict]:
        return [
            {
                "role": "assistant",
                "content": "<think>ragiono</think>Apro il file.",
                "tool_calls": [
                    {
                        "id": f"c{i}",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": _json.dumps({"filepath": f"m{i}.py"}),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": f"c{i}",
                "name": "read_file",
                "content": _json.dumps({"content": "x" * caratteri, "path": f"m{i}.py"}),
            },
        ]

    def quando_compatta(num_ctx: int, passi: int = 12) -> int:
        b = budgets_for(num_ctx)
        finestra = config_mod.finestra_efficace(num_ctx)
        ui = [{"role": "user", "content": "rifattorizza il modulo"}]
        for s in range(1, passi + 1):
            ui += passo(s, b.read_file_max_chars)
            api = agent_mod.build_api_messages(
                ui, system_prompt="S" * 8_000, env_header=None, budgets=b
            )
            if agent_mod.context_pressure(api, finestra) > config_mod.HISTORY_COMPACT_THRESHOLD:
                return s
        return passi + 1        # non ha compattato

    finestre = [16_384, 32_768, 49_152, 65_536, 98_304, 131_072]
    quando = [quando_compatta(n) for n in finestre]
    assert quando == sorted(quando), (
        "allargare la finestra fa compattare prima: "
        + ", ".join(f"{n}->{q}" for n, q in zip(finestre, quando))
    )


# ---------------------------------------------------------------------------
# Profilo del modello
# ---------------------------------------------------------------------------


def test_qwen38_ha_un_profilo_suo():
    """Prima cadeva su GENERIC_THINKING: nessun errore, solo parametri tiepidi."""
    profilo = profiles.profile_for("qwen3.8:27b-q6_K", thinking=True)
    assert profilo is profiles.QWEN38_CODING
    assert profilo.top_k == 20
    assert profilo.max_tokens >= 16384      # il pensiero non va troncato


def test_la_regola_di_qwen38_non_ruba_le_altre():
    assert profiles.profile_for("qwen3.5:9b") is profiles.QWEN35_CODING
    assert profiles.profile_for("qwen2.5-coder:7b") is profiles.QWEN25_CODER


# ---------------------------------------------------------------------------
# Costo del KV cache dai metadati GGUF
# ---------------------------------------------------------------------------


QWEN3_INFO = {
    "general.architecture": "qwen3",
    "qwen3.block_count": 48,
    "qwen3.attention.head_count": 32,
    "qwen3.attention.head_count_kv": 8,
    "qwen3.attention.key_length": 128,
    "qwen3.embedding_length": 4096,
}


def test_il_costo_del_kv_si_calcola_dai_metadati():
    # 2 (K e V) * 48 strati * 8 teste * 128 * 2 byte = 196.608 byte = 0,1875 MB
    assert profiles.kv_mb_per_token(QWEN3_INFO) == pytest.approx(0.1875)


def test_senza_key_length_si_ricava_dalla_dimensione_dell_embedding():
    info = {k: v for k, v in QWEN3_INFO.items() if not k.endswith("key_length")}
    # head_dim = 4096 / 32 = 128: stesso risultato per altra strada
    assert profiles.kv_mb_per_token(info) == pytest.approx(0.1875)


def test_il_prefisso_dell_architettura_non_e_cablato():
    """Cablare 'qwen3.' significherebbe riscrivere la funzione ad ogni famiglia."""
    llama = {
        "llama.block_count": 48,
        "llama.attention.head_count_kv": 8,
        "llama.attention.key_length": 128,
    }
    assert profiles.kv_mb_per_token(llama) == pytest.approx(0.1875)


def test_metadati_insufficienti_non_producono_un_numero_inventato():
    assert profiles.kv_mb_per_token(None) is None
    assert profiles.kv_mb_per_token({}) is None
    assert profiles.kv_mb_per_token({"qwen3.block_count": 48}) is None


def test_un_modello_grande_merita_un_contesto_piu_piccolo():
    """Il punto di tutta la modifica al dimensionamento.

    Con 10 GB liberi la vecchia costante (0,10 MB/token) prometteva ~95.000
    token; il costo vero di un 27B (0,1875) ne concede circa la meta'. La
    differenza non e' accademica: e' la soglia oltre la quale Ollama scarica
    strati sulla CPU e la generazione rallenta di un ordine di grandezza.
    """
    libera_mb = 10_240
    ottimista = profiles.context_for_vram(libera_mb)                 # ripiego
    reale = profiles.context_for_vram(libera_mb, 0.1875)             # 27B vero
    assert reale < ottimista


def test_senza_vram_nota_si_resta_prudenti():
    assert profiles.context_for_vram(None, 0.1875) == 8192
    assert profiles.context_for_vram(100, 0.1875) == 8192


# ---------------------------------------------------------------------------
# max_tokens e num_ctx sono due impostazioni, e nessuno le confrontava
# ---------------------------------------------------------------------------


def _prompt(token: int) -> list[dict]:
    """Un prompt finto della dimensione voluta, in token stimati."""
    # La stima e' su caratteri: ~3,6 per token. Si punta appena sopra, poi si
    # verifica, perche' l'esattezza qui non serve -- serve l'ordine di
    # grandezza giusto.
    return [{"role": "user", "content": "x " * (token * 2)}]


def test_il_tetto_si_stringe_su_quello_che_resta_nella_finestra():
    """Con finestra 32k, prompt 24k e tetto 16k la somma sfonda di 8k.

    Il server non rifiuta: genera finche' la finestra e' piena e poi si ferma
    dove capita. Se capita dentro gli argomenti di una tool call, quello che
    arriva all'harness e' JSON monco -- ed e' esattamente l'errore
    "Argomenti JSON malformati" che si vedeva senza spiegazione.
    """
    messaggi = _prompt(24000)
    tetto, spazio = agent_mod.tetto_per_la_finestra(messaggi, 32768, 16384)
    assert tetto < 16384
    assert tetto == max(spazio, 0)
    # E la somma, adesso, ci sta: e' l'unica proprieta' che conta.
    assert agent_mod.estimate_messages_tokens(messaggi) + tetto <= 32768


def test_con_spazio_in_abbondanza_il_tetto_resta_quello_scelto():
    """Il taglio deve mordere solo quando serve: altrove e' una manopola
    dell'utente, e riscriverla in silenzio sarebbe la stessa specie di bugia."""
    tetto, spazio = agent_mod.tetto_per_la_finestra(_prompt(2000), 131072, 16384)
    assert tetto == 16384
    assert spazio > 16384


def test_una_finestra_gia_piena_non_da_un_tetto_negativo():
    tetto, spazio = agent_mod.tetto_per_la_finestra(_prompt(40000), 32768, 16384)
    assert tetto == 0
    assert spazio < 0            # il numero vero resta leggibile


def test_senza_finestra_dichiarata_non_si_tocca_niente():
    """``num_ctx`` a zero vuol dire 'non lo so': indovinare sarebbe peggio."""
    assert agent_mod.tetto_per_la_finestra(_prompt(9000), 0, 16384) == (16384, 0)


# ---------------------------------------------------------------------------
# Una chiamata tagliata a meta' non e' una chiamata scritta male
# ---------------------------------------------------------------------------


def test_la_chiamata_troncata_lo_dice_invece_di_dare_la_colpa_al_json():
    """"Riemetti un oggetto JSON valido" e' un consiglio che non puo'
    funzionare quando a mancare non e' la sintassi ma la fine del testo:
    riemettere la stessa chiamata la fa finire nello stesso punto."""
    monco = '{"filepath":"a.md","content":"# Titolo\\nprima riga incompl'
    esito = agent_mod.argomenti_illeggibili(monco, "length", 16384)
    assert "troncata" in esito["error"]
    assert "16384" in esito["causa"]
    assert "Spezzala" in esito["hint"]
    assert esito["received"] == monco

    # JSON davvero scritto male: il consiglio di prima e' quello giusto.
    normale = agent_mod.argomenti_illeggibili("{non json}", "stop", 16384)
    assert normale["error"] == "Argomenti JSON malformati."
    assert "JSON valido" in normale["hint"]
