"""Guardia sui file di test: deve bloccare l'indebolimento, non il lavoro.

I casi qui sotto sono presi da due sessioni reali con qwen3.5, in cui la
versione precedente della guardia (blocco dell'intero file finche' la verifica
era rossa) ha mandato il modello in stallo per decine di messaggi.
"""

from __future__ import annotations

import json

import pytest

from core.tools import (
    ToolContext,
    assertion_signatures,
    tool_edit_file,
    tool_read_file,
    tool_write_file,
)


@pytest.fixture
def ctx(tmp_path):
    c = ToolContext(workspace=str(tmp_path))
    c.red_command = "python -m pytest -v"
    return c


SUITE = '''\
def test_media():
    assert media([1, 2, 3]) == 2.0


def test_vuota():
    with pytest.raises(ValueError):
        media([])
'''


def scrivi_suite_utente(tmp_path, ctx, nome="test_stat.py", testo=SUITE):
    """Il test esiste gia' sul disco e non l'ha scritto l'agente."""
    (tmp_path / nome).write_text(testo, encoding="utf-8")
    # L'agente lo ha letto: cosi' write_file non inciampa nell'altro guard-rail.
    tool_read_file(ctx, nome)
    return nome


def esito(risposta: str) -> dict:
    return json.loads(risposta)


# --- cio' che DEVE passare ---------------------------------------------------


def test_aggiungere_un_import_e_permesso(tmp_path, ctx):
    """Il caso che ha bloccato la sessione Test 2.1 cinque volte di fila."""
    nome = scrivi_suite_utente(tmp_path, ctx, testo="from stat import media\n" + SUITE)
    out = esito(
        tool_edit_file(
            ctx,
            nome,
            old_string="from stat import media",
            new_string="from stat import media, scarta_anomali",
        )
    )
    assert out.get("error") is None, out


def test_aggiungere_un_nuovo_test_e_permesso(tmp_path, ctx):
    nome = scrivi_suite_utente(tmp_path, ctx)
    out = esito(
        tool_edit_file(
            ctx,
            nome,
            old_string="def test_media():",
            new_string="def test_nuovo():\n    assert scarta([1]) == [1]\n\n\ndef test_media():",
        )
    )
    assert out.get("error") is None, out


def test_test_scritto_dall_agente_resta_suo(ctx):
    """Il caso della sessione telemetria: quattro rifiuti su un file che
    l'agente aveva creato lui stesso cinque messaggi prima."""
    creato = esito(tool_write_file(ctx, "test_mio.py", SUITE))
    assert creato.get("error") is None, creato
    assert "test_mio.py" in ctx.authored_tests

    out = esito(
        tool_edit_file(
            ctx,
            "test_mio.py",
            old_string="assert media([1, 2, 3]) == 2.0",
            new_string="assert media([1, 2, 3]) == pytest.approx(2.0)",
        )
    )
    assert out.get("error") is None, out


def test_senza_verifica_rossa_non_si_blocca_nulla(tmp_path):
    c = ToolContext(workspace=str(tmp_path))  # red_command = None
    scrivi_suite_utente(tmp_path, c)
    out = esito(
        tool_edit_file(
            c, "test_stat.py", old_string="assert media([1, 2, 3]) == 2.0", new_string="pass"
        )
    )
    assert out.get("error") is None, out


# --- cio' che NON deve passare -----------------------------------------------


def test_indebolire_un_assert_e_bloccato(tmp_path, ctx):
    """La tautologia osservata: `== 2.0` diventa `<= 99`."""
    nome = scrivi_suite_utente(tmp_path, ctx)
    out = esito(
        tool_edit_file(
            ctx,
            nome,
            old_string="assert media([1, 2, 3]) == 2.0",
            new_string="assert media([1, 2, 3]) <= 99",
        )
    )
    assert out.get("error"), out
    assert "asserzione" in out["error"]


def test_edit_chirurgica_su_un_frammento_e_bloccata(tmp_path, ctx):
    """Ne' old_string ne' new_string sono un assert, ma il file dopo ne perde uno.

    E' il motivo per cui il confronto e' fra file prima e file dopo, non fra i
    due frammenti passati a edit_file.
    """
    nome = scrivi_suite_utente(tmp_path, ctx)
    out = esito(tool_edit_file(ctx, nome, old_string="== 2.0", new_string="is not None"))
    assert out.get("error"), out
    assert "== 2.0" in (tmp_path / nome).read_text(encoding="utf-8")


def test_togliere_un_pytest_raises_e_bloccato(tmp_path, ctx):
    nome = scrivi_suite_utente(tmp_path, ctx)
    out = esito(
        tool_edit_file(
            ctx,
            nome,
            old_string="    with pytest.raises(ValueError):\n        media([])",
            new_string="    media([])",
        )
    )
    assert out.get("error"), out


def test_marcare_skip_e_bloccato(tmp_path, ctx):
    nome = scrivi_suite_utente(tmp_path, ctx)
    out = esito(
        tool_edit_file(
            ctx,
            nome,
            old_string="def test_media():",
            new_string="@pytest.mark.skip\ndef test_media():",
        )
    )
    assert out.get("error"), out
    assert "skip" in out["error"]


def test_riscrittura_integrale_che_perde_asserzioni_e_bloccata(tmp_path, ctx):
    nome = scrivi_suite_utente(tmp_path, ctx)
    out = esito(tool_write_file(ctx, nome, "def test_media():\n    assert True\n"))
    assert out.get("error"), out


def test_al_secondo_rifiuto_il_messaggio_indica_l_uscita(tmp_path, ctx):
    nome = scrivi_suite_utente(tmp_path, ctx)
    modifica = {
        "old_string": "assert media([1, 2, 3]) == 2.0",
        "new_string": "assert media([1, 2, 3]) <= 99",
    }
    primo = esito(tool_edit_file(ctx, nome, **modifica))
    secondo = esito(tool_edit_file(ctx, nome, **modifica))
    assert "ask_user_question" in json.dumps(primo)
    assert "2a volta" in json.dumps(secondo)


# --- l'estrattore ------------------------------------------------------------


def test_assert_su_piu_righe_conta_come_uno():
    firme = assertion_signatures(
        "assert calcola(\n    [1, 2, 3],\n    peso=0.5,\n) == 2.0\n"
    )
    assert list(firme.values()) == [1]
    assert "assert calcola( [1, 2, 3], peso=0.5, ) == 2.0" in firme


def test_i_commenti_non_sono_asserzioni():
    assert assertion_signatures("# assert media([1]) == 1\nx = 2\n") == {}
