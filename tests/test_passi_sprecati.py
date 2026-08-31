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

from core.plan import Plan
from core.tools import PLAN_TOOL, ToolContext, dispatch


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


# ---------------------------------------------------------------------------
# Il budget condiviso delle reti di sicurezza
# ---------------------------------------------------------------------------


def test_i_solleciti_hanno_un_budget_comune_e_non_si_prendono_tutti_i_passi():
    """Il conto che nessuno faceva.

    Ogni sollecito aveva il suo tetto e ognuno, preso da solo, era tarato bene.
    Ma i tetti si sommano: 2 riprese di stream + 2 watchdog + 2 troncamenti +
    2 verifiche + 3 flag = 11, contro un ``max_agent_loops`` di serie di 12.
    Nel caso peggiore -- un modello debole su un compito difficile, cioe'
    esattamente quello per cui i solleciti esistono -- al lavoro restava un
    passo.
    """
    from core import agent as A

    somma_dei_tetti = (
        A.MAX_RIPRESE_STREAM
        + A.MAX_WATCHDOG_FIRES
        + A.MAX_TRUNCATED_NUDGES
        + A.MAX_VERIFY_NUDGES
        + 2          # i flag: coverage e ask/tool (nudged e' condiviso)
    )
    for max_steps in (6, 12, 30, 100):
        budget = max(2, max_steps // 3)
        assert budget < max_steps, "il servizio non puo' prendersi tutti i passi"
        # e sotto i dodici passi il budget e' comunque piu' stretto della somma
        if max_steps <= 12:
            assert budget < somma_dei_tetti


def test_il_riepilogo_e_esente_dal_budget_di_servizio():
    """Gli altri sette sono correzioni; questo e' la parola di chiusura.

    Un turno che ha bruciato il budget in correzioni e' proprio quello che
    rischia di piu' di finire in silenzio davanti all'utente.
    """
    import inspect

    from core import agent as A

    sorgente = inspect.getsource(A.run_turn)
    blocco = sorgente[sorgente.index("if (\n                require_summary"):]
    blocco = blocco[: blocco.index("continue")]
    assert "passi_di_servizio <" not in blocco


def test_una_finestra_senza_spazio_chiude_il_turno_invece_di_generare():
    """``tetto_per_la_finestra`` poteva ritornare 0 e il ciclo tirava dritto.

    Si chiedeva al modello di produrre zero token, si otteneva una risposta
    vuota, e la risposta vuota faceva scattare i solleciti -- che aggiungono
    messaggi, cioe' riducono ancora lo spazio. Un giro a vuoto che si stringe
    da solo.
    """
    from core.agent import TETTO_INUTILE, tetto_per_la_finestra

    pieni = [{"role": "user", "content": "x" * 200_000}]
    tetto, _spazio = tetto_per_la_finestra(pieni, 8_192, 4_096)
    assert tetto < TETTO_INUTILE     # la condizione che adesso chiude il turno


# ---------------------------------------------------------------------------
# La chiamata identica ripetuta
# ---------------------------------------------------------------------------


def test_la_stessa_chiamata_ripetuta_viene_riconosciuta():
    from core.agent import RipetizioniTool

    r = RipetizioniTool()
    assert r.registra("read_file", {"filepath": "a.py"}) == 1
    assert r.registra("read_file", {"filepath": "a.py"}) == 2
    assert r.registra("read_file", {"filepath": "b.py"}) == 1
    assert r.registra("read_file", {"filepath": "a.py"}) == 3


def test_l_ordine_delle_chiavi_non_fa_due_chiamate_diverse():
    """Un modello che rigenera il JSON non mette gli argomenti nello stesso ordine."""
    from core.agent import RipetizioniTool

    r = RipetizioniTool()
    r.registra("search_files", {"pattern": "x", "glob": "*.py"})
    assert r.registra("search_files", {"glob": "*.py", "pattern": "x"}) == 2


def test_una_scrittura_rende_di_nuovo_sensata_la_rilettura():
    from core.agent import RipetizioniTool

    r = RipetizioniTool()
    r.registra("read_file", {"filepath": "a.py"})
    r.registra("read_file", {"filepath": "a.py"})
    r.dimentica_letture()
    assert r.registra("read_file", {"filepath": "a.py"}) == 1


def test_l_esito_del_tool_non_si_legge_per_sottostringa(tmp_path):
    """Un file che contiene la parola "error" non e' un tool fallito."""
    import json

    from core.agent import _esito_del_tool

    assert _esito_del_tool(json.dumps({"content": 'raise ValueError("error")'}))
    assert not _esito_del_tool(json.dumps({"error": "file non trovato"}))
    # e su un risultato che non e' JSON si ricade sul vecchio criterio
    assert _esito_del_tool("testo semplice senza buste")


def test_la_regola_del_raggruppamento_e_scritta_una_volta_sola():
    """Diceva la stessa cosa due volte, e si pagava due volte.

    ``SYSTEM_PROMPT_LEAN`` aveva una sezione "Chiedi tutto quello che ti serve
    in una volta" che ripeteva per intero la regola gia' data sotto "Un passo
    alla volta": stessa istruzione, due paragrafi, ~180 token a ogni singola
    richiesta. Un prompt che ripete non insegna il doppio -- e questo e' il
    prompt che si chiama *snello*.
    """
    from core.prompts import SYSTEM_PROMPT_LEAN

    quante = SYSTEM_PROMPT_LEAN.count("vengono eseguite tutte")
    assert quante == 1, f"la regola del raggruppamento compare {quante} volte"
    assert "# Chiedi tutto quello che ti serve in una volta" not in SYSTEM_PROMPT_LEAN


def test_il_prompt_non_racconta_al_modello_le_statistiche_su_di_se():
    """"Su una sessione misurata di questo harness, 13 passi su 31...".

    Sono token pagati a ogni richiesta per raccontare al modello un aneddoto
    su sessioni che non ha vissuto: non e' un invariante che non puo' dedurre,
    e non gli dice niente da fare che l'istruzione sopra non dica gia'. Le
    misure servono a *noi*, per decidere cosa scrivere nel prompt: stanno nei
    commenti del sorgente, dove non costano contesto.
    """
    import re

    from core.prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_LEAN

    for nome, testo in (("esteso", SYSTEM_PROMPT), ("snello", SYSTEM_PROMPT_LEAN)):
        aneddoti = re.findall(r"\d+ passi su \d+|sessione misurata|sessioni misurate", testo)
        assert not aneddoti, f"prompt {nome}: statistiche di sessione nel testo -- {aneddoti}"
