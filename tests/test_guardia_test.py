"""Guardia sui file di test: deve bloccare l'indebolimento, non il lavoro.

I casi qui sotto sono presi da due sessioni reali con qwen3.5, in cui la
versione precedente della guardia (blocco dell'intero file finche' la verifica
era rossa) ha mandato il modello in stallo per decine di messaggi.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_agent_loop import fake_ollama  # noqa: F401
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


# ---------------------------------------------------------------------------
# Il tier di esecuzione del frontend: acceso, non solo scritto
# ---------------------------------------------------------------------------


def test_quickjs_e_una_dipendenza_dichiarata():
    """I diciannove test che ESEGUONO il JS del mobile saltavano sempre.

    ``test_mobile_passi.py`` e ``test_mobile_ui.py`` caricano il vero
    ``web_mobile/app.js`` in un contesto QuickJS e ne chiamano le funzioni: e'
    l'unico posto in cui il frontend viene eseguito invece che cercato per
    stringhe. ``pytest.importorskip`` li faceva saltare in silenzio, e
    ``quickjs`` non era in nessun manifesto -- quindi su un'installazione
    standard saltavano **sempre**, e del frontend restava verificata solo la
    forma del sorgente. E' il buco da cui e' passato ``deposito_max_mb``.
    """
    import tomllib

    radice = Path(__file__).resolve().parents[1]
    dati = tomllib.loads((radice / "pyproject.toml").read_text(encoding="utf-8"))
    dev = dati["project"]["optional-dependencies"]["dev"]
    assert any(d.startswith("quickjs") for d in dev), (
        "quickjs non e' fra le dipendenze di sviluppo: i test che eseguono il "
        "JavaScript del mobile salteranno su ogni installazione pulita"
    )


def test_il_lint_copre_le_regole_che_hanno_gia_trovato_difetti():
    """Tre convenzioni erano scritte nel codice e non applicate dal linter.

    - ``RUF021`` (precedenza fra ``and`` e ``or``) trova da solo il difetto per
      cui ``list_files`` mostrava i dot-file mentre l'header d'ambiente li
      nascondeva.
    - ``BLE`` rende vivi i ``# noqa: BLE001`` gia' scritti: senza, non
      sopprimevano niente e un catch cieco nuovo entrava senza giustificarsi.
    - ``PLW0603`` lo stato globale mutabile, in un processo che gira i turni in
      thread di sfondo.
    """
    import tomllib

    radice = Path(__file__).resolve().parents[1]
    dati = tomllib.loads((radice / "pyproject.toml").read_text(encoding="utf-8"))
    select = set(dati["tool"]["ruff"]["lint"]["select"])
    for regola in ("RUF", "BLE", "PLW"):
        assert regola in select, f"il linter non applica {regola}"


def test_ogni_argomento_consentito_e_dichiarato_nello_schema():
    """La lista dei permessi e lo schema devono dire la stessa cosa.

    ``_ALLOWED_ARGS`` decide cosa ``dispatch`` lascia passare; lo schema decide
    cosa il modello sa di poter mandare. Un argomento nella prima e non nel
    secondo e' una manopola che esiste e che nessuno puo' girare -- era il caso
    di ``preview(wait_s=...)``: il codice lo leggeva, il modello non poteva
    saperlo, e con un server lento a partire l'unica mossa che gli restava era
    spendere un altro passo per riaspettare gli stessi dodici secondi.

    Al contrario, un argomento dichiarato e non consentito e' peggio: il
    modello lo manda perche' glielo abbiamo promesso, e ``dispatch`` lo scarta
    in silenzio.
    """
    from core.tools import _ALLOWED_ARGS, TOOLS_SCHEMA, WEB_SEARCH_TOOLS

    schemi = {
        s["function"]["name"]: set(s["function"]["parameters"].get("properties") or {})
        for s in [*TOOLS_SCHEMA, *WEB_SEARCH_TOOLS]
    }
    disallineati = {}
    for nome, consentiti in _ALLOWED_ARGS.items():
        dichiarati = schemi.get(nome)
        assert dichiarati is not None, f"{nome} non ha uno schema"
        if consentiti != dichiarati:
            disallineati[nome] = (
                sorted(consentiti - dichiarati),
                sorted(dichiarati - consentiti),
            )
    assert not disallineati, (
        "argomenti disallineati (solo consentiti / solo dichiarati): " + repr(disallineati)
    )


# ---------------------------------------------------------------------------
# Il finto modello non si porta dietro il copione del test precedente
# ---------------------------------------------------------------------------
#
# I due test qui sotto vanno in coppia e **in quest'ordine**: il primo sporca
# il globale di proposito e non lo rimette a posto, il secondo verifica di
# averlo trovato pulito. È l'unico modo di provare l'isolamento: una fixture
# che ripulisce non si può osservare da dentro il test che la usa.


def test_uno_sporca_il_copione_e_non_lo_rimette_a_posto(fake_ollama):
    import tests.test_agent_loop as fake

    fake.SCRIPT = [[{"message": {"content": "copione sporco"}}]]
    assert fake.SCRIPT != fake._SCRIPT_DI_SERIE


def test_due_lo_trova_pulito(fake_ollama):
    """Sedici test di ``test_agent_loop`` e sei altri file sostituiscono
    ``SCRIPT``. Finché il ripristino era a carico di ognuno di loro, bastava
    dimenticare un ``try/finally`` per rompere il **primo test successivo** --
    che non ha niente a che vedere col colpevole ed è quello che si va a
    guardare."""
    import tests.test_agent_loop as fake

    assert fake.SCRIPT == fake._SCRIPT_DI_SERIE


def test_ogni_evento_del_ciclo_ha_un_nome_sul_filo():
    """``AgentEvent`` e ``_EVENT_NAMES`` devono elencare le stesse classi.

    Il primo è il contratto scritto, il secondo quello che viaggia davvero:
    ``event_to_sse`` traduce un evento sconosciuto in ``"unknown"`` e lo manda
    lo stesso, quindi un evento nuovo non rompe niente -- arriva al browser
    come un tipo che nessun ramo gestisce, e sparisce. ``HistoryCompacted`` e
    ``NotesUpdated`` mancavano dall'unione pur essendo emessi da sempre.
    """
    from core import agent as agent_mod
    from server import main as server_main

    dichiarati = set(agent_mod.AgentEvent.__args__)
    tradotti = set(server_main._EVENT_NAMES)
    assert dichiarati == tradotti, (
        f"solo in AgentEvent: {sorted(c.__name__ for c in dichiarati - tradotti)}; "
        f"solo in _EVENT_NAMES: {sorted(c.__name__ for c in tradotti - dichiarati)}"
    )
