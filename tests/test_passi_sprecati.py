"""Il piano non deve costare piu' passi del lavoro che organizza.

Misura di partenza, su una sessione vera di questo harness (31 passi, compito
risolto correttamente):

    contabilita' del piano, zero lavoro   13 passi
    lavoro vero                           16 passi
    solo testo                             2 passi

e 27 passi su 31 contenevano **una sola** chiamata. La sequenza si ripeteva
identica per ognuno dei sei punti::

    manage_plan start id=N     <- un round-trip per aprire
    write_file ...
    run_command ...
    manage_plan complete id=N  <- un round-trip per chiudere

Meta' di quella spesa chiedeva al modello un'informazione che l'harness aveva
gia' in mano: qual e' il punto successivo. Ora lo apre da se'.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.plan import Plan  # noqa: E402
from core.tools import PLAN_TOOL, ToolContext, dispatch  # noqa: E402


@pytest.fixture()
def ctx():
    return ToolContext(workspace=tempfile.mkdtemp())


def piano(ctx, *punti: str) -> dict:
    return json.loads(dispatch(ctx, PLAN_TOOL, {"action": "set", "steps": list(punti)}))


def test_il_primo_punto_si_apre_da_solo(ctx):
    """Scrivere il piano e poi chiedere il permesso di cominciarlo erano due
    round-trip per una decisione senza alternative."""
    esito = piano(ctx, "uno", "due", "tre")
    assert esito["current"] == "1"
    assert esito["aperto_in_automatico"] == {"id": "1", "text": "uno"}


def test_chiudere_un_punto_apre_il_successivo(ctx):
    piano(ctx, "uno", "due", "tre")
    esito = json.loads(
        dispatch(ctx, PLAN_TOOL, {"action": "complete", "step_id": "1", "note": "fatto"})
    )
    assert esito["current"] == "2"
    assert esito["aperto_in_automatico"]["text"] == "due"


def test_vale_anche_per_i_punti_saltati(ctx):
    """Un punto saltato e' chiuso quanto uno fatto: se non avanzasse, la
    scorciatoia costerebbe di piu' della strada lunga."""
    piano(ctx, "uno", "due", "tre")
    esito = json.loads(
        dispatch(ctx, PLAN_TOOL, {"action": "skip", "step_id": "1", "note": "non serve"})
    )
    assert esito["current"] == "2"


def test_l_apertura_automatica_viene_detta_nel_risultato(ctx):
    """Il blocco del piano lo direbbe comunque, ma un passo dopo -- cioe'
    proprio nel passo che si vuole risparmiare."""
    piano(ctx, "uno", "due")
    esito = json.loads(
        dispatch(ctx, PLAN_TOOL, {"action": "complete", "step_id": "1", "note": "ok"})
    )
    assert "Punto 2 aperto" in esito["next_step"]
    assert "non serve action='start'" in esito["next_step"].lower()
    assert "stesso passo" in esito["next_step"]


def test_la_regola_di_un_punto_per_volta_resta(ctx):
    """L'automatismo rende gratuita la regola, non la sostituisce: aprire un
    secondo punto a mano deve continuare a essere un errore."""
    piano(ctx, "uno", "due", "tre")
    esito = json.loads(dispatch(ctx, PLAN_TOOL, {"action": "start", "step_id": "3"}))
    assert esito.get("error"), "il punto 1 e' in corso: il 3 non si puo' aprire"


def test_si_puo_ancora_saltare_fuori_sequenza(ctx):
    """Dopo aver chiuso, il punto in fila e' aperto ma non e' un vincolo:
    start su un altro punto resta la via d'uscita."""
    piano(ctx, "uno", "due", "tre")
    dispatch(ctx, PLAN_TOOL, {"action": "complete", "step_id": "1", "note": "ok"})
    dispatch(ctx, PLAN_TOOL, {"action": "skip", "step_id": "2", "note": "dopo"})
    esito = json.loads(dispatch(ctx, PLAN_TOOL, {"action": "start", "step_id": "3"}))
    assert esito["current"] == "3"


def test_l_ultimo_punto_chiuso_non_apre_niente_e_lo_dice(ctx):
    piano(ctx, "uno")
    esito = json.loads(
        dispatch(ctx, PLAN_TOOL, {"action": "complete", "step_id": "1", "note": "ok"})
    )
    assert esito["current"] is None
    assert "aperto_in_automatico" not in esito
    assert "messaggio di chiusura" in esito["next_step"]


def test_ripianificare_non_strappa_il_punto_in_corso(ctx):
    """Con un punto gia' aperto, `avanza` non deve fare niente: sposterebbe il
    lavoro sotto i piedi al modello a meta' di un punto."""
    piano(ctx, "uno", "due")
    esito = piano(ctx, "uno", "due", "tre")
    assert esito["current"] == "1"
    assert "aperto_in_automatico" not in esito


def test_il_piano_resta_una_macchina_a_stati_pura():
    """L'avanzamento e' una politica del tool, non del modello dati: `complete`
    da solo non deve aprire niente, o i test del piano misurerebbero due cose
    insieme."""
    p = Plan()
    p.set_steps(["uno", "due"])
    p.start("1")
    p.complete("1", "ok")
    assert p.current is None
    assert p.avanza().id == "2"


# ---------------------------------------------------------------------------
# Quanto costa, in passi
# ---------------------------------------------------------------------------


def test_sei_punti_costano_una_sola_chiamata_di_apertura(ctx):
    """Il conto che ha motivato tutto: prima servivano 6 start + 6 complete in
    passi loro. Ora le aperture sono zero e le chiusure viaggiano insieme al
    lavoro."""
    punti = [f"punto {i}" for i in range(1, 7)]
    piano(ctx, *punti)
    aperture = 0
    for i in range(1, 7):
        esito = json.loads(
            dispatch(ctx, PLAN_TOOL, {"action": "complete", "step_id": str(i), "note": "ok"})
        )
        if esito.get("current") and i < 6:
            assert esito["aperto_in_automatico"]["id"] == str(i + 1)
        aperture += 1 if esito.get("richiede_start") else 0
    assert aperture == 0


def test_la_regola_del_raggruppamento_nomina_scritture_e_piano():
    """Diceva 'letture, ricerche e verifiche': il modello la leggeva alla
    lettera e scriveva quattro file in quattro passi."""
    from core.prompts import PLAN_NUDGE, SYSTEM_PROMPT_LEAN

    assert "quattro write_file" in SYSTEM_PROMPT_LEAN
    assert "contabilita'" in SYSTEM_PROMPT_LEAN
    assert "STESSO PASSO" in PLAN_NUDGE
    assert "si apre" in PLAN_NUDGE
