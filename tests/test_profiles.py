"""Test dei profili per modello e dei parametri di generazione.

Il bug che questi test bloccano non da' mai un errore: manda una richiesta
valida con i parametri sbagliati e restituisce risultati peggiori senza dirlo.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import profiles
from core.config import GenParams, resolve_think


# --- riconoscimento della famiglia ------------------------------------------


def test_qwen35_is_not_mistaken_for_qwen3():
    """'qwen3.5' contiene 'qwen3': l'ordine delle regole non e' un dettaglio."""
    assert profiles.profile_for("qwen3.5:9b") is profiles.QWEN35_CODING
    assert profiles.profile_for("qwen3-coder:30b") is profiles.QWEN3_CODER
    assert profiles.profile_for("qwen2.5-coder:7b") is profiles.QWEN25_CODER


def test_an_unknown_model_falls_back_on_its_capabilities():
    assert profiles.profile_for("modello-mai-visto", thinking=True) is profiles.GENERIC_THINKING
    assert profiles.profile_for("modello-mai-visto", thinking=False) is profiles.GENERIC_INSTRUCT


def test_qwen35_gets_the_parameters_its_model_card_recommends():
    p = profiles.QWEN35_CODING
    assert (p.temperature, p.top_p, p.top_k) == (0.6, 0.95, 20)
    # Il budget di generazione comprende il pensiero: 2048 non basta.
    assert p.max_tokens >= 8192


# --- parametri che prima non venivano mai inviati ----------------------------


def test_top_k_is_actually_sent_to_ollama():
    opts = GenParams(top_k=20).ollama_options()
    assert opts["top_k"] == 20


def test_presence_penalty_is_sent_only_when_set():
    """A zero non si manda: sarebbe rumore in un prefisso che vogliamo stabile."""
    assert "presence_penalty" not in GenParams(presence_penalty=0.0).ollama_options()
    assert GenParams(presence_penalty=1.5).ollama_options()["presence_penalty"] == 1.5


def test_repetition_penalty_defaults_to_neutral_and_is_sent_when_changed():
    """Il neutro e' 1.0, non il default Ollama (1.1): a riposo non va nel payload,
    appena l'impostazione si muove va."""
    assert GenParams().repetition_penalty == 1.0
    assert "repeat_penalty" not in GenParams().ollama_options()
    opts = GenParams(repetition_penalty=1.2).ollama_options()
    assert opts["repeat_penalty"] == 1.2


def test_la_penalita_di_ripetizione_viaggia_col_nome_che_ollama_conosce():
    """Il bug piu' silenzioso del fork: la manopola girava e non faceva niente.

    Sul transport nativo l'opzione si chiama ``repeat_penalty``; il nome della
    letteratura (``repetition_penalty``, vLLM/HF) Ollama non lo conosce e --
    come tutte le opzioni sconosciute -- lo scarta senza dire niente. Nessun
    errore, nessun avviso: solo un'impostazione che sembra funzionare.
    """
    opts = GenParams(repetition_penalty=1.15).ollama_options()
    assert "repetition_penalty" not in opts
    assert opts["repeat_penalty"] == 1.15


# --- livelli di pensiero ----------------------------------------------------


def test_think_levels_survive_until_the_request():
    assert GenParams(think="high").think_payload == "high"
    assert GenParams(think="max").think_payload == "max"
    assert GenParams(think=True).think_payload is True
    # Spento significa omettere il campo, non mandarlo a false.
    assert GenParams(think=False).think_payload is None


def test_a_level_is_not_squashed_to_a_boolean():
    assert resolve_think("high", None) == "high"
    assert resolve_think("si", None) is True
    assert resolve_think("no", True) is False
    assert resolve_think("auto", True) is True


# --- dimensionamento del contesto -------------------------------------------


def test_context_grows_with_the_free_vram():
    stretta = profiles.context_for_vram(1200)
    larga = profiles.context_for_vram(12000)
    assert stretta < larga
    assert stretta >= 8192


def test_no_vram_left_means_the_smallest_context():
    assert profiles.context_for_vram(300) == 8192
    assert profiles.context_for_vram(None) == 8192


def test_generation_budget_never_exceeds_the_window():
    """num_predict e num_ctx condividono la stessa finestra."""
    assert profiles.clamp_generation(16384, 8192) == 4096
    assert profiles.clamp_generation(2048, 65536) == 2048


def test_applying_a_profile_clamps_the_budget_to_the_window():
    values = profiles.as_settings(profiles.QWEN35_CODING, num_ctx=8192)
    assert values["max_tokens"] <= 8192 // 2
    assert values["top_k"] == 20
