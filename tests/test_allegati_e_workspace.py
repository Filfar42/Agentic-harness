"""Allegati agganciati al messaggio, e la cartella di lavoro come comando.

Tre cambiamenti che hanno una radice sola: la colonna di destra era diventata
un magazzino di riquadri -- allegati, workspace, anteprima, contesto, ultima
esecuzione, file toccati -- e ognuno di quei riquadri era la copia lontana di
qualcosa che stava gia' altrove sullo schermo. Un allegato appartiene al
messaggio con cui parte; la cartella di lavoro e' scritta in alto, quindi e'
li' che si preme per cambiarla.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from tests.test_server import client, current_session, fake_ollama  # noqa: E402,F401

WEB = Path(__file__).resolve().parents[1] / "web"


def _allega(client, session_id: str, nome: str, corpo: bytes = b"x") -> dict:
    response = client.post(
        f"/api/attachments/{session_id}",
        files={"files": (nome, corpo, "text/plain")},
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# L'allegato appartiene al messaggio
# ---------------------------------------------------------------------------


def test_l_allegato_inviato_resta_agganciato_al_messaggio(client):
    session_id = current_session(client)
    _allega(client, session_id, "dati.csv", b"a,b\n")

    client.post(
        "/api/chat",
        json={"session_id": session_id, "prompt": "guarda qui", "attachments": ["dati.csv"]},
    )
    client.get(f"/api/stream/{session_id}")     # lascia finire il turno

    messaggi = client.server.STATE.messages(session_id)
    utente = next(m for m in messaggi if m["role"] == "user")
    assert [a["name"] for a in utente["attachments"]] == ["dati.csv"]
    # e sopravvive alla riapertura della conversazione
    client.server.STATE.drop(session_id)
    riaperta = client.post(f"/api/sessions/{session_id}/open").json()
    utente = next(m for m in riaperta["messages"] if m["role"] == "user")
    assert utente["attachments"][0]["path"] == "allegati/dati.csv"


def test_la_striscia_del_composer_mostra_solo_quelli_non_ancora_partiti(client):
    """Un allegato gia' inviato si vede sotto il suo messaggio: rimetterlo
    nella barra sopra la casella direbbe che e' ancora in coda."""
    session_id = current_session(client)
    _allega(client, session_id, "primo.txt")
    _allega(client, session_id, "secondo.txt")

    stats = client.post(
        "/api/chat",
        json={"session_id": session_id, "prompt": "vai", "attachments": ["primo.txt"]},
    )
    assert stats.status_code == 200
    client.get(f"/api/stream/{session_id}")

    riaperta = client.post(f"/api/sessions/{session_id}/open").json()
    rimasti = [a["name"] for a in riaperta["stats"]["attachments"]]
    assert rimasti == ["secondo.txt"]


def test_un_allegato_non_si_aggancia_due_volte(client):
    """Il secondo messaggio non si riprende i file del primo, nemmeno se il
    client li rimanda per sbaglio."""
    session_id = current_session(client)
    _allega(client, session_id, "unico.txt")
    for _ in range(2):
        client.post(
            "/api/chat",
            json={"session_id": session_id, "prompt": "ancora", "attachments": ["unico.txt"]},
        )
        client.get(f"/api/stream/{session_id}")

    agganci = [
        a["name"]
        for m in client.server.STATE.messages(session_id)
        for a in (m.get("attachments") or [])
    ]
    assert agganci == ["unico.txt"]


def test_nomi_inventati_dal_client_vengono_ignorati(client):
    session_id = current_session(client)
    client.post(
        "/api/chat",
        json={"session_id": session_id, "prompt": "ciao", "attachments": ["mai-caricato.txt"]},
    )
    client.get(f"/api/stream/{session_id}")
    utente = next(m for m in client.server.STATE.messages(session_id) if m["role"] == "user")
    assert "attachments" not in utente


def test_rimuovere_un_allegato_lo_toglie_anche_dal_messaggio(client):
    session_id = current_session(client)
    _allega(client, session_id, "vecchio.txt")
    client.post(
        "/api/chat",
        json={"session_id": session_id, "prompt": "usa questo", "attachments": ["vecchio.txt"]},
    )
    client.get(f"/api/stream/{session_id}")

    client.delete(f"/api/attachments/{session_id}?name=vecchio.txt")
    utente = next(m for m in client.server.STATE.messages(session_id) if m["role"] == "user")
    assert "attachments" not in utente
    assert not (client.workspace / "allegati" / "vecchio.txt").exists()


def test_non_si_rimuove_un_allegato_a_turno_in_corso(client, monkeypatch):
    """Il file e' montato nel workspace: cancellarlo mentre l'agente ci lavora
    trasformerebbe una pulizia in un read_file fallito a meta' ragionamento."""
    session_id = current_session(client)
    _allega(client, session_id, "in-uso.txt")
    monkeypatch.setattr(
        client.server.RUNNERS, "is_running", lambda _id: True
    )
    risposta = client.delete(f"/api/attachments/{session_id}?name=in-uso.txt")
    assert risposta.status_code == 409
    assert (client.workspace / "allegati" / "in-uso.txt").is_file()


# ---------------------------------------------------------------------------
# Cartelle recenti
# ---------------------------------------------------------------------------


def test_la_cartella_scelta_finisce_in_testa_ai_recenti(client, tmp_path):
    prima = client.server.STATE.settings["workspace_dir"]
    nuova = tmp_path / "altro-progetto"
    nuova.mkdir()

    data = client.post("/api/workspace", json={"path": str(nuova)}).json()
    recenti = data["recent_workspaces"]
    assert recenti[0] == client.server.STATE.settings["workspace_dir"]
    assert prima in recenti


def test_la_stessa_cartella_non_compare_due_volte(client, tmp_path):
    una = tmp_path / "andirivieni"
    una.mkdir()
    altra = tmp_path / "seconda"
    altra.mkdir()
    for percorso in (una, altra, una):
        client.post("/api/workspace", json={"path": str(percorso)})
    recenti = client.server.STATE.settings["recent_workspaces"]
    assert len(recenti) == len(set(recenti))
    assert recenti[0].endswith("andirivieni")


def test_l_elenco_dei_recenti_resta_corto(client, tmp_path):
    for i in range(10):
        cartella = tmp_path / f"p{i}"
        cartella.mkdir()
        client.post("/api/workspace", json={"path": str(cartella)})
    recenti = client.server.STATE.settings["recent_workspaces"]
    assert len(recenti) <= client.server.RECENT_WORKSPACES_MAX


def test_una_cartella_sparita_esce_dai_recenti(client, tmp_path):
    """Una scorciatoia che porta a un errore e' peggio di una scorciatoia in
    meno. Vale per un progetto cancellato e per le cartelle temporanee che la
    suite di test lascia dietro di se' passando da /api/workspace."""
    effimera = tmp_path / "sparira"
    effimera.mkdir()
    client.post("/api/workspace", json={"path": str(effimera)})
    assert str(effimera.resolve()) in client.server.STATE.settings["recent_workspaces"]

    effimera.rmdir()
    stabile = tmp_path / "resta"
    stabile.mkdir()
    recenti = client.post("/api/workspace", json={"path": str(stabile)}).json()[
        "recent_workspaces"
    ]
    assert all(Path(p).is_dir() for p in recenti)
    assert str(effimera.resolve()) not in recenti


def test_i_recenti_sopravvivono_al_riavvio():
    """Sono nei DEFAULTS, quindi passano dal filtro di ``persistable``: senza,
    la tendina si svuoterebbe ad ogni chiusura dell'harness."""
    from core.config import DEFAULTS
    from core.settings import persistable

    assert DEFAULTS["recent_workspaces"] == []
    salvati = persistable({**DEFAULTS, "recent_workspaces": ["C:/uno", "C:/due"]})
    assert salvati["recent_workspaces"] == ["C:/uno", "C:/due"]


# ---------------------------------------------------------------------------
# Dove stanno le cose sullo schermo
# ---------------------------------------------------------------------------


def _html() -> str:
    return (WEB / "index.html").read_text(encoding="utf-8")


def _pannello() -> str:
    html = _html()
    return html[html.index('<aside id="panel">'): html.index("</aside>", html.index('<aside id="panel">'))]


def test_gli_allegati_stanno_sopra_la_casella_dentro_lo_stesso_contenitore():
    html = _html()
    composer = html[html.index('<div id="composer">'): html.index('class="composer-hint"')]
    assert 'id="attach-strip"' in composer
    assert composer.index('id="attach-strip"') < composer.index("<textarea")

    css = (WEB / "style.css").read_text(encoding="utf-8")
    assert "#composer {" in css and "flex-direction: column" in css
    assert ".composer-row {" in css


def test_la_colonna_di_destra_non_ha_piu_i_riquadri_traslocati():
    pannello = _pannello()
    for sparito in ('id="attachments"', 'id="att-add"', 'id="preview-card"',
                    'id="preview-open"', 'id="usage-card"'):
        assert sparito not in pannello, sparito
    # workspace: il percorso e i due pulsanti non stanno piu' qui
    assert 'id="ws-path"' not in pannello
    assert 'id="ws-browse"' not in pannello
    # contesto e consumo del turno in una scheda sola
    assert 'id="usage-block"' in pannello
    # quattro schede in tutto: piano, note, consumo, file toccati. Erano sette.
    assert pannello.count("<h4") == 4


def test_il_workspace_si_cambia_dalle_impostazioni_sotto_al_modello():
    html = _html()
    conn = html[html.index('id="tab-conn"'): html.index('id="tab-gen"')]
    assert 'id="ws-path"' in conn and 'id="ws-browse"' in conn and 'id="ws-open"' in conn
    assert conn.index('id="s-model"') < conn.index('id="ws-path"')


def test_il_percorso_in_alto_e_il_comando_per_cambiarlo():
    html = _html()
    topbar = html[html.index('<header id="topbar">'): html.index("</header>")]
    chip = topbar[topbar.index('id="ws-chip"'): topbar.index('id="ws-menu"')]
    assert 'id="top-sub"' in chip          # il bersaglio del clic e' il percorso
    assert 'id="ws-menu"' in topbar

    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert "state.settings.recent_workspaces" in js
    assert "'/api/workspace/pick'" in js
    assert js.count("function browseWorkspace(") == 1   # un solo selettore


def test_le_due_tendine_non_restano_aperte_insieme():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    modello = js[js.index("function bindModelChip("):]
    modello = modello[: modello.index("\n// ---")]
    assert "toggleWsMenu(false)" in modello


def test_i_file_toccati_sono_gocce_che_aprono_l_anteprima():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    blocco = js[js.index("const files = stats.touched_files"):]
    blocco = blocco[: blocco.index("\n  // La lista arriva")]
    assert "fileChips(" in blocco and "stacked" in blocco
    assert "file-row" not in js          # niente piu' righe di solo testo

    css = (WEB / "style.css").read_text(encoding="utf-8")
    assert ".file-chips.stacked" in css


def test_l_anteprima_si_apre_col_file_senza_un_secondo_clic():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    blocco = js[js.index("function renderPreview("):]
    blocco = blocco[: blocco.index("\n/** Mostra un file")]
    assert "openPreview(payload)" in blocco
    assert "preview-card" not in js


@pytest.mark.parametrize("regola", [".att-chip", ".att-chips", "body.turn-live .att-del"])
def test_le_gocce_degli_allegati_hanno_una_forma_sola(regola):
    css = (WEB / "style.css").read_text(encoding="utf-8")
    assert regola in css


def test_la_croce_sparisce_finche_l_agente_lavora():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert re.search(r"classList\.toggle\('turn-live', busyHere\)", js)
