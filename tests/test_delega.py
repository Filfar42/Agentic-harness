"""Il contratto del referto di delega.

Le tre patch (marker di troncamento, budget dichiarato nel prompt, ramo d'errore
informato) vivono tutte nella coda di ``delega.esegui``. Qui si prova che il
padre riceve sempre abbastanza per decidere se riformulare la domanda o no --
senza far girare un modello: ``run_turn`` arriva come parametro iniettabile, e
il fake lo muta proprio come farebbe il vero ciclo.
"""

from __future__ import annotations

from core.config import GenParams
from core.delega import MAX_PASSI_DELEGA, MAX_REFERTO_CHARS, esegui
from core.tools import ToolContext


class StepStarted:  # noqa: N801 -- il loop conta solo ``type(evento).__name__``
    def __init__(self, step=0, total=0):
        self.step = step
        self.total = total


def _fake_run_turn(*, referto=None, passi=1, legge=("core/config.py",), con_asistente=True):
    """Un ``run_turn`` iniettabile: appende i messaggi come farebbe il vero ciclo."""

    def _run(**kw):
        msg = kw["ui_messages"]
        for nome in legge:
            msg.append({"role": "tool", "name": "read_file", "args": {"filepath": nome}})
        for i in range(passi):
            yield StepStarted(step=i + 1, total=passi)
        if con_asistente and referto is not None:
            msg.append({"role": "assistant", "content": referto})

    return _run


def _esegui(tmp_path, **kw_fake):
    ctx = ToolContext(workspace=str(tmp_path), sandbox="host")
    return esegui(
        "dove sta budgets_for?",
        backend=object(),          # il fake non lo usa: va solo di mano al run_turn
        params=GenParams(model="fake"),
        tools_schema=[],
        tool_ctx=ctx,
        env_header=None,
        run_turn=_fake_run_turn(**kw_fake),
    )


def test_referto_corto_arriva_intero(tmp_path):
    out = _esegui(tmp_path, referto="sta in core/config.py:105", passi=2)
    assert out["referto"] == "sta in core/config.py:105"
    assert "troncato" not in out      # senza marker quando il referto sta nel budget
    assert out["passi"] == 2


def test_referto_lungo_porta_il_marker(tmp_path):
    """Il troncamento deve essere *visibile*: il padre sa che ha solo una parte."""
    lungo = "x" * (MAX_REFERTO_CHARS + 497)   # oltre il tetto, di una misura nota
    out = _esegui(tmp_path, referto=lungo, passi=3)
    assert out["troncato"] is True
    assert out["referto"].startswith("x" * MAX_REFERTO_CHARS)
    # La misura nel marker e' quella dell'originale, non del pezzo che e' rimasto.
    assert f"era lungo {len(lungo)} caratteri" in out["referto"]


def test_il_padre_sa_cosa_ha_guardato(tmp_path):
    """``file_letti`` serve a fidarsi -- o a non fidarsi: un referto senza aperture e' inventato."""
    out = _esegui(tmp_path, referto="trovato", passi=1, legge=("core/config.py", "server/main.py"))
    assert out["file_letti"] == ["core/config.py", "server/main.py"]


def test_esaurito_con_risposta_e_fla_ggiato(tmp_path):
    """Risposta arrivata col passo 6 gia' speso: si legge, ma segnata come probabile incompleta."""
    out = _esegui(tmp_path, referto="forse qui", passi=MAX_PASSI_DELEGA)
    assert out["referto"] == "forse qui"
    assert out["esaurito"] is True


def test_esaurito_senza_risposta_dice_la_causa(tmp_path):
    """Sei passi bruciati senza un ultimo messaggio: la causa va detta e l'appiglio va restituito."""
    out = _esegui(tmp_path, referto=None, con_asistente=False, passi=MAX_PASSI_DELEGA)
    assert "errore" in out and "referto" not in out
    assert out["esaurito"] is True
    assert f"ha esaurito i {MAX_PASSI_DELEGA} passi" in out["errore"]
    assert out["file_letti"] == ["core/config.py"]   # da' al padre un punto da cui riformulare


def test_senza_risposta_ma_con_margine_non_e_esaurimento(tmp_path):
    """Nessun messaggio finale, ma il figlio si e' fermato col passo 1: non e' 'esaurito'."""
    out = _esegui(tmp_path, referto=None, con_asistente=False, passi=1)
    assert "errore" in out and "referto" not in out
    assert out["esaurito"] is False


def test_il_prompt_del_figlio_dichiara_il_budget():
    """Il figlio deve sapere quanto budget ha: altrimenti scrive per chi non tronca."""
    from core.delega import PROMPT_DELEGA

    assert f"al massimo {MAX_PASSI_DELEGA} passi" in PROMPT_DELEGA
    assert f"primi {MAX_REFERTO_CHARS} caratteri" in PROMPT_DELEGA   # 2000, come si vede al figlio


# ---------------------------------------------------------------------------
# I budget del sotto-turno: la strettata deve arrivare davvero al figlio.
# ---------------------------------------------------------------------------

def test_parametri_figlio_allarga_la_finestra_e_spegne_think():
    """num_ctx x1.5 come margine, think spento, il resto del padre invariato."""
    from core.delega import parametri_figlio

    padre = GenParams(model="m", num_ctx=16384, max_tokens=4096, think="low", temperature=0.4)
    figlio = parametri_figlio(padre)
    assert figlio.num_ctx == 24576
    assert figlio.think is False
    assert figlio.max_tokens == 4096 and figlio.temperature == 0.4 and figlio.model == "m"


def test_budget_stretti_dimezza_e_garantisce_i_minimi():
    from core.config import budgets_for
    from core.delega import budget_stretti

    larghi = budgets_for(16384)
    stretti = budget_stretti(larghi)
    assert stretti.read_file_max_chars < larghi.read_file_max_chars
    assert stretti.search_max_matches < larghi.search_max_matches
    # La finestra integrale non cresce mai: a 16k il padre e' gia' al minimo 3.
    assert stretti.tool_result_full_window <= larghi.tool_result_full_window
    # Dai minimi non si scende mai, nemmeno restringendo una finestra povera.
    stretti_poveri = budget_stretti(budgets_for(4096))
    assert stretti_poveri.read_file_max_chars >= 2500   # MIN_READ_FILE_CHARS
    assert stretti_poveri.search_max_matches >= 10
    assert stretti_poveri.tool_result_full_window == 3


def test_esegui_passa_al_figlio_budget_stretti_e_finestra_larga(tmp_path):
    """La strettata arriva a run_turn *e* resta nel ToolContext dopo il primo passo."""
    visti: dict[str, object] = {}

    def _run(**kw):
        visti["budgets"] = kw["budgets"]
        visti["params"] = kw["params"]
        visti["tool_ctx"] = kw["tool_ctx"]
        msg = kw["ui_messages"]
        msg.append({"role": "tool", "name": "read_file", "args": {"filepath": "core/config.py"}})
        yield StepStarted(step=1, total=1)
        msg.append({"role": "assistant", "content": "referto"})

    ctx = ToolContext(workspace=str(tmp_path), sandbox="host")
    out = esegui(
        "dove sta budgets_for?",
        backend=object(),
        params=GenParams(model="fake"),
        tools_schema=[],
        tool_ctx=ctx,
        env_header=None,
        run_turn=_run,
    )
    assert out["referto"] == "referto"

    from core.config import budgets_for
    from core.delega import budget_stretti

    larghi = budgets_for(16384)
    stretti = visti["budgets"]
    assert stretti.read_file_max_chars < larghi.read_file_max_chars
    assert stretti == budget_stretti(larghi)
    # run_turn sovrascrive tool_ctx.budgets al primo passo: se lo facesse con
    # budgets_for(num_ctx_figlio), qui troveremmo i budget larghi.
    assert visti["tool_ctx"].budgets == stretti
    # La finestra del sotto-turno e' quella allargata, non quella del padre.
    assert visti["params"].num_ctx == 24576


def test_budget_riferimento_valori_attesi_e_coerenza_col_prompt():
    """Le costanti del prompt derivano dal riferimento base, con valori fissi."""
    from core.config import BASE_NUM_CTX, budgets_for
    from core.delega import (
        MAX_LETTURA_FIGLIO,
        MAX_MATCHES_FIGLIO,
        PROMPT_DELEGA,
        budget_riferimento,
        budget_stretti,
    )

    rif = budget_riferimento()
    # Valori attesi espliciti alla taratura base (padre 16k -> figlio x1.5).
    assert BASE_NUM_CTX == 16384
    assert rif.read_file_max_chars == 6000
    assert rif.search_max_matches == 40
    # Le due costanti sono la proiezione del riferimento, e il prompt le cita.
    assert MAX_LETTURA_FIGLIO == rif.read_file_max_chars == 6000
    assert MAX_MATCHES_FIGLIO == rif.search_max_matches == 40
    assert f"ai primi {MAX_LETTURA_FIGLIO} caratteri" in PROMPT_DELEGA
    assert f"a {MAX_MATCHES_FIGLIO} corrispondenze" in PROMPT_DELEGA
    # Il riferimento e' esattamente il budget stretto sulla finestra base...
    assert rif == budget_stretti(budgets_for(BASE_NUM_CTX))
    # ...ed e' memoizzato: chiamate successive restituiscono lo stesso oggetto.
    assert budget_riferimento() is rif
