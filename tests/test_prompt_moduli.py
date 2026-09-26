"""Il prompt a moduli (A8) e la chiusura del pensiero di Qwen3 (A9), 25/09.

Regola del progetto: ogni frase del prompt che descrive l'harness deve essere
vera. Qui si controlla che i codici d'errore e i campi che il modulo
``ERRORI`` nomina esistano davvero nel codice che li produce: se un giorno un
codice sparisce o cambia nome, il prompt starebbe insegnando al modello a
reagire a una cosa che non arriva mai.
"""

from __future__ import annotations

import re
from pathlib import Path

from core import system_prompt as sp
from core.regia_pensiero import CHIUSURE, FRASE_BUDGET_QWEN3

CORE = Path(__file__).resolve().parents[1] / "core"


def _sorgente_core() -> str:
    return "\n".join(
        f.read_text(encoding="utf-8")
        for f in sorted(CORE.rglob("*.py"))
        if f.name != "system_prompt.py"
    )


def test_i_moduli_sono_nell_ordine_dichiarato():
    testo = sp.SYSTEM_PROMPT_LEAN
    posizioni = [testo.index(m.strip().splitlines()[0]) for m in sp.MODULI]
    assert posizioni == sorted(posizioni)
    assert sp.SYSTEM_PROMPT.startswith(sp.SYSTEM_PROMPT_LEAN.rstrip("\n"))
    assert "# Quale tool chiamare" in sp.SYSTEM_PROMPT
    assert "# Quale tool chiamare" not in sp.SYSTEM_PROMPT_LEAN


def test_ogni_codice_d_errore_nominato_esiste_nel_codice():
    codici = set(re.findall(r"\b([a-z]+(?:_[a-z]+)+)\b", sp.ERRORI))
    nominati = {c for c in codici if c in {
        "invalid_json", "invalid_arguments", "tool_not_allowed", "syntax_guard",
        "esito_sconosciuto",
    }}
    assert len(nominati) == 5, nominati
    sorgente = _sorgente_core()
    for codice in nominati:
        assert re.search(rf'error_code["\']?\s*[:=]\s*["\']{codice}["\']', sorgente), codice


def test_i_campi_della_diagnosi_di_edit_file_esistono():
    assert "'piu_vicino'" in sp.ERRORI and "'aggancio'" in sp.ERRORI
    tools = (CORE / "tools.py").read_text(encoding="utf-8")
    assert "piu_vicino=vicino" in tools
    assert 'avviso["aggancio"]' in tools


def test_l_avviso_sui_passi_senza_effetto_esiste():
    """Il modulo ERRORI promette che l'harness lo segnala: lo fa la rete
    ``riorienta`` del monitor di avanzamento."""
    assert "passi non producono" in sp.ERRORI
    from core.prompts import RIORIENTA_NUDGE

    assert "{passi}" in RIORIENTA_NUDGE


def test_la_chiusura_per_budget_e_la_frase_ufficiale_di_qwen3():
    """Qwen3 Technical Report, thinking budget: la frase inserita quando il
    pensiero raggiunge la soglia. Verbatim, senza la ``</think>`` che mette il
    backend in continuazione."""
    assert FRASE_BUDGET_QWEN3 == (
        "Considering the limited time by the user, I have to give the solution "
        "based on the thinking directly now."
    )
    assert FRASE_BUDGET_QWEN3 in CHIUSURE["budget"]
    assert "</think>" not in CHIUSURE["budget"]


def test_il_promemoria_di_persistenza_c_e_una_volta():
    assert sp.SYSTEM_PROMPT_LEAN.count("continua a lavorare finche'") == 1
