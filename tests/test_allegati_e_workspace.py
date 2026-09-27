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

import pytest

from tests.test_server import (  # noqa: F401
    client,
    current_session,
    fake_ollama,
    read_sse,
    run_and_collect,
)

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
    # Il vecchio riquadro "Consumo" e' diventato il cruscotto (26/09/2026):
    # velocita', turno, contesto, velocita' e contesto. Piano, note e file
    # toccati restano. Le due schede del vault -- identita' e memoria -- sono
    # diventate la schermata del progetto; a destra resta solo la memoria del
    # progetto, e solo dentro una sua chat.
    assert 'id="usage-block"' not in pannello
    for scheda in ("cr-tachimetro", "cr-turno", "cr-contesto", "cr-dispersione",
                   "plan-card", "notes-card", "files-card", "pr-mem-card"):
        assert f'id="{scheda}"' in pannello, scheda
    for sparita in ("vault-card", "vault-mem-card"):
        assert f'id="{sparita}"' not in pannello, sparita
    assert pannello.count("<h4") == 3            # piano, note, file
    assert pannello.count('class="card cr-card"') == 5
    assert '<section class="card cr-card" id="pr-mem-card" aria-label="Memoria del progetto" hidden>' in pannello

    # Sulla schermata del progetto la colonna di destra sparisce per intero:
    # le sue schede parlano di una conversazione, e li' non ce n'e' una.
    css = (WEB / "progetti.css").read_text(encoding="utf-8")
    assert "#app.pr-home-attiva #panel" in css
    js = (WEB / "progetti.js").read_text(encoding="utf-8")
    corpo = js[js.index("function mostraProgettoHome("):]
    corpo = corpo[: corpo.index("\n}")]
    assert "classList.toggle('pr-home-attiva'" in corpo


def test_la_cartella_non_sta_piu_anche_nelle_impostazioni():
    """C'era in due posti e i due facevano lo stesso identico gesto: scegliere
    una cartella e aprirla in Esplora risorse. È rimasto quello in alto, che è
    dove si legge dove si sta lavorando -- cercare una cosa dentro le
    impostazioni per cambiarla è un passaggio in più per una scelta che si fa
    mentre si lavora."""
    html = _html()
    for sparito in ('id="ws-path"', 'id="ws-browse"', 'id="ws-open"'):
        assert sparito not in html, sparito
    # ...e il gesto non si è perso per strada: vive nella tendina in alto.
    js = (WEB / "app.js").read_text(encoding="utf-8")
    corpo = js[js.index("function renderWsMenu("):]
    corpo = corpo[: corpo.index("\n}")]
    assert "browseWorkspace()" in corpo
    assert "/api/workspace/open" in corpo


def test_i_rimedi_dell_ambiente_stanno_nella_tendina_della_cartella():
    """Immagine e container si preparano da soli al cambio di cartella: farli a
    mano è un rimedio, e i rimedi stanno accanto alla cosa da rimediare. Nelle
    impostazioni sembravano due manopole da usare."""
    html = _html()
    assert 'id="sandbox-build"' not in html
    assert 'id="sandbox-restart"' not in html

    js = (WEB / "app.js").read_text(encoding="utf-8")
    corpo = js[js.index("function renderWsMenu("):]
    corpo = corpo[: corpo.index("\n}")]
    assert "'Impostazioni'" in corpo
    assert "/api/prep/container" in corpo and "/api/prep/image" in corpo


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
    blocco = js[js.index("function renderToccati("):]
    blocco = blocco[: blocco.index("\n}\n")]
    assert "fileChips(" in blocco and "stacked" in blocco
    # Una sola funzione disegna la scheda: le stats di fine turno e il file
    # appena scritto dal turno in corso passano entrambi da li'.
    assert "renderToccati(stats.touched_files || [])" in js
    corpo = js[js.index("function aggiungiToccato("):]
    assert "renderToccati(" in corpo[: corpo.index("\n}\n")]
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


# ---------------------------------------------------------------------------
# Gli allegati che non entrano in contesto
# ---------------------------------------------------------------------------


def test_un_allegato_escluso_viene_detto(client, monkeypatch):
    """Il silenzio era la modalita' di guasto peggiore per un allegato.

    ``load_images_b64`` calcolava gia' quali file non erano entrati e il suo
    docstring dice perche' ("l'utente deve sapere che non sono in contesto");
    ``turn_images`` la propagava; la UI ha il commento che promette l'avviso.
    Il server spacchettava la lista e la buttava. Chi allegava quattro immagini
    e ne vedeva commentare tre non aveva modo di accorgersene.
    """
    from core.tools import MAX_IMAGE_BYTES
    from server import main as server_main

    # Vision accesa: senza, ``turn_images`` ritorna due liste vuote e non c'e'
    # niente da escludere.
    monkeypatch.setattr(server_main.AppState, "vision_enabled", lambda self: True)

    session_id = current_session(client)
    _allega(client, session_id, "piccola.png", b"\x89PNG" + b"\x00" * 64)
    _allega(client, session_id, "enorme.png", b"\x89PNG" + b"\x00" * MAX_IMAGE_BYTES)
    client.post(
        "/api/chat",
        json={
            "session_id": session_id,
            "prompt": "che vedi?",
            "attachments": ["piccola.png", "enorme.png"],
        },
    )
    eventi = read_sse(client.get(f"/api/stream/{session_id}"))
    note = [e for e in eventi if e.get("type") == "note"]
    assert note, "nessun avviso sull'allegato rimasto fuori dal contesto"
    messaggio = note[0]["message"]
    assert "enorme.png" in messaggio
    assert "piccola.png" not in messaggio


def test_senza_esclusi_non_si_dice_niente(client, monkeypatch):
    """Un avviso che compare sempre smette di essere un avviso."""
    from server import main as server_main

    monkeypatch.setattr(server_main.AppState, "vision_enabled", lambda self: True)
    session_id = current_session(client)
    _allega(client, session_id, "unica.png", b"\x89PNG" + b"\x00" * 64)
    eventi = run_and_collect(client, session_id, "che vedi?")
    esclusi = [
        e for e in eventi
        if e.get("type") == "note" and "non sono entrati in contesto" in e.get("message", "").lower()
    ]
    assert not esclusi
