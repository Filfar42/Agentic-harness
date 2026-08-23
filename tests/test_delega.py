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
