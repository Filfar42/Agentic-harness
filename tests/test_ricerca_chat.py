"""Ricerca nelle conversazioni, e le due gocce della barra in alto.

I titoli sono i primi 44 caratteri del primo messaggio: su una raccolta di
prove ripetute cominciano tutti uguali ("Nel workspace deve esistere una
ca..."), e scorrere non e' un modo di ritrovare niente. Quello che distingue
una conversazione dall'altra sta dentro -- un comando lanciato, un errore
trovato, il nome di un allegato.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import session as session_mod  # noqa: E402
from tests.test_server import client, fake_ollama  # noqa: E402,F401

WEB = Path(__file__).resolve().parents[1] / "web"


@pytest.fixture()
def archivio(tmp_path, monkeypatch):
    """Quattro conversazioni su disco, ognuna riconoscibile da una cosa sola."""
    cartella = tmp_path / "sessions"
    cartella.mkdir()
    monkeypatch.setattr(session_mod, "DATA_DIR", cartella)
    session_mod._index_cache.clear()
    session_mod._search_cache.clear()

    def scrivi(ident, titolo, messaggi, giorno):
        (cartella / f"{ident}.json").write_text(
            json.dumps(
                {
                    "id": ident,
                    "title": titolo,
                    "updated_at": f"2026-08-{giorno:02d}T10:00:00",
                    "messages": messaggi,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    scrivi("a", "Nel workspace deve esistere una cartella cantiere", [
        {"role": "user", "content": "Nel workspace deve esistere una cartella cantiere."},
        {"role": "assistant", "content": "Fatto: creato misura.py."},
        {"role": "tool", "name": "run_command", "args": {"command": "pytest -q"},
         "content": "{}"},
    ], 1)
    scrivi("b", "Ci sono due test che non passano", [
        {"role": "user", "content": "Trova il bug nel parser."},
        {"role": "assistant", "content": "Era un off-by-one nel parser del CSV."},
    ], 2)
    scrivi("c", "Analizza il foglio delle spese", [
        {"role": "user", "content": "Guarda l'allegato.",
         "attachments": [{"name": "spese_trimestre.xlsx", "path": "allegati/spese_trimestre.xlsx"}]},
    ], 3)
    scrivi("d", "Avvia il server", [
        {"role": "user", "content": "Fammi vedere la pagina."},
        {"role": "tool", "name": "run_command",
         "args": {"command": "python -m http.server 8200"}, "content": "{}"},
    ], 4)
    return cartella


def ids(risultati):
    return [r["id"] for r in risultati]


# ---------------------------------------------------------------------------
# I quattro modi di ricordarsi una conversazione
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "atteso", "dove"),
    [
        ("cantiere", ["a"], "titolo"),
        ("off-by-one", ["b"], "testo"),
        ("spese_trimestre", ["c"], "allegato"),
        ("http.server", ["d"], "comando"),
    ],
)
def test_si_trova_da_dove_te_lo_ricordi(archivio, query, atteso, dove):
    risultati = session_mod.search_sessions(query)
    assert ids(risultati) == atteso
    assert risultati[0]["match_in"] == dove


def test_l_esito_dice_perche_e_in_elenco(archivio):
    """Una riga trovata "nel testo" senza l'estratto e' un titolo qualunque."""
    trovata = session_mod.search_sessions("off-by-one")[0]
    assert "off-by-one" in trovata["snippet"]
    assert trovata["snippet"].startswith("…")     # c'e' del testo prima


def test_l_estratto_non_lascia_la_parola_fuori_dal_bordo(archivio):
    """La colonna e' stretta: con troppo testo davanti si vedrebbe tutto
    tranne il motivo per cui quella riga e' li'."""
    trovata = session_mod.search_sessions("off-by-one")[0]
    prima = trovata["snippet"].index("off-by-one")
    assert prima <= session_mod.SNIPPET_PRIMA + 1


def test_due_parole_restringono_invece_di_allargare(archivio):
    """Un OR darebbe piu' righe man mano che si aggiunge precisione."""
    assert ids(session_mod.search_sessions("parser")) == ["b"]
    assert ids(session_mod.search_sessions("parser csv")) == ["b"]
    assert session_mod.search_sessions("parser cantiere") == []


def test_i_termini_possono_stare_in_campi_diversi(archivio):
    """"cantiere pytest": uno nel titolo, l'altro in un comando."""
    assert ids(session_mod.search_sessions("cantiere pytest")) == ["a"]


def test_ricerca_senza_esito_e_ricerca_vuota(archivio):
    assert session_mod.search_sessions("zzz") == []
    assert session_mod.search_sessions("") == []
    assert session_mod.search_sessions("   ") == []


def test_maiuscole_e_minuscole_non_contano(archivio):
    assert ids(session_mod.search_sessions("CANTIERE")) == ["a"]


def test_i_piu_recenti_per_primi(archivio):
    assert ids(session_mod.search_sessions("il")) == ["d", "c", "b"]


# ---------------------------------------------------------------------------
# Indice
# ---------------------------------------------------------------------------


def test_una_conversazione_che_cambia_si_ritrova_col_testo_nuovo(archivio):
    assert session_mod.search_sessions("marmellata") == []
    percorso = archivio / "b.json"
    dati = json.loads(percorso.read_text(encoding="utf-8"))
    dati["messages"].append({"role": "assistant", "content": "Aggiunto il gusto marmellata."})
    percorso.write_text(json.dumps(dati, ensure_ascii=False), encoding="utf-8")
    assert ids(session_mod.search_sessions("marmellata")) == ["b"]


def test_una_conversazione_cancellata_esce_dall_indice(archivio):
    assert ids(session_mod.search_sessions("cantiere")) == ["a"]
    session_mod.delete_session("a")
    assert session_mod.search_sessions("cantiere") == []


def test_il_corpo_dei_file_non_finisce_nell_indice(archivio):
    """Degli argomenti dei tool si tiene la forma dell'azione, non il corpo:
    cercare "def main" e trovare ogni conversazione in cui e' passato un
    write_file sarebbe rumore, e l'indice starebbe in memoria a megabyte."""
    (archivio / "e.json").write_text(json.dumps({
        "id": "e", "title": "Scrittura", "updated_at": "2026-08-05T10:00:00",
        "messages": [{"role": "tool", "name": "write_file",
                      "args": {"filepath": "conta.py", "content": "def zuppa():\n    pass\n"},
                      "content": "{}"}],
    }), encoding="utf-8")
    assert ids(session_mod.search_sessions("conta.py")) == ["e"]     # il percorso si'
    assert session_mod.search_sessions("zuppa") == []                # il corpo no


# ---------------------------------------------------------------------------
# Rotta
# ---------------------------------------------------------------------------


def test_la_rotta_dice_anche_chi_sta_lavorando(client, monkeypatch):
    from core import config as config_mod

    session_id = client.get("/api/bootstrap").json()["session"]["session_id"]
    client.post("/api/chat", json={"session_id": session_id, "prompt": "cerca la zuppa"})
    client.get(f"/api/stream/{session_id}")

    dati = client.get("/api/sessions/search?q=zuppa").json()
    assert [s["id"] for s in dati["sessions"]] == [session_id]
    assert dati["sessions"][0]["running"] is False
    assert config_mod.DATA_DIR.exists()


def test_la_ricerca_vuota_non_restituisce_tutto(client):
    """Una stringa vuota non e' "cercami qualsiasi cosa": la sidebar in quel
    caso mostra l'elenco normale, e mandarle 60 righe con dentro gli estratti
    sarebbe lavoro buttato."""
    assert client.get("/api/sessions/search?q=").json()["sessions"] == []


# ---------------------------------------------------------------------------
# Interfaccia
# ---------------------------------------------------------------------------


def _html() -> str:
    return (WEB / "index.html").read_text(encoding="utf-8")


def test_la_ricerca_sta_in_cima_all_elenco():
    html = _html()
    assert 'id="session-search"' in html
    assert html.index('id="session-search"') < html.index('id="sessions"')


def test_una_lista_sola_per_due_sorgenti():
    """Due funzioni di disegno vorrebbero dire aggiungere il pallino della chat
    in esecuzione in due posti -- e prima o poi in uno dei due manchera'."""
    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert js.count("function renderSessions(") == 1
    corpo = js[js.index("function renderSessions("):]
    corpo = corpo[: corpo.index("\nfunction ")]
    assert "state.search.results" in corpo and "state.sessions" in corpo


def test_l_evidenziazione_non_apre_una_porta_all_html():
    """Si marca dopo aver scappato, mai prima: costruire i tag e poi passarli
    all'escape li cancellerebbe, e costruirli attorno al testo grezzo
    lascerebbe passare quello che c'era scritto nella conversazione."""
    js = (WEB / "app.js").read_text(encoding="utf-8")
    corpo = js[js.index("function marca("):]
    corpo = corpo[: corpo.index("\n}") + 2]
    assert "esc(testo)" in corpo
    assert "esc(t)" in corpo            # anche i termini cercati
    assert corpo.index("esc(testo)") < corpo.index("<mark>")


# ---------------------------------------------------------------------------
# Le due gocce della barra in alto
# ---------------------------------------------------------------------------


def test_ogni_goccia_ha_la_sua_ancora():
    """L'ancora e' larga quanto la goccia: e' quello che rende `right: 0` il
    bordo destro del percorso invece che quello della colonna, ed e' quello
    che le rimette su due righe (da inline-flex finivano affiancate, e il
    bordo di una toccava la freccia dell'altra)."""
    html = _html()
    assert html.count('class="chip-anchor"') == 2
    css = (WEB / "style.css").read_text(encoding="utf-8")
    blocco = css[css.index(".chip-anchor {"):]
    blocco = blocco[: blocco.index("}")]
    assert "width: fit-content" in blocco and "position: relative" in blocco
    assert "display: block" in blocco
    assert ".ws-menu { min-width: 240px; left: auto; right: 0; }" in css


def test_le_gocce_non_si_troncano_da_sole():
    """Regressione: `max-width: 100%` sulla goccia e margini negativi sulla
    goccia insieme si mangiavano 12-16px, e i due nomi comparivano con i
    puntini anche quando lo spazio c'era tutto. Il rientro sta sull'ancora."""
    css = (WEB / "style.css").read_text(encoding="utf-8")
    for regola in (".model-chip {", ".ws-chip {"):
        blocco = css[css.index(regola):]
        blocco = blocco[: blocco.index("}")]
        assert "max-width: 100%" in blocco, regola
        # Solo gli orizzontali: quello verticale allinea la goccia al testo che
        # ha sostituito e con la larghezza non c'entra.
        valori = next(r for r in blocco.splitlines() if "margin:" in r)
        valori = valori.split("margin:")[1].split(";")[0].split()
        orizzontali = [valori[1], valori[3] if len(valori) > 3 else valori[1]]
        assert not any(v.startswith("-") for v in orizzontali), \
            f"{regola} ha ancora un margine negativo orizzontale"
    ancora = css[css.index(".chip-anchor {"):]
    assert "margin-left: -" in ancora[: ancora.index("}")]


def test_l_icona_della_scheda_e_il_robottino_senza_riquadro():
    html = _html()
    assert 'href="/static/favicon-64.png"' in html
    assert "logo-dark-64" not in html
    for nome in ("favicon-16.png", "favicon-32.png", "favicon-64.png"):
        percorso = WEB / nome
        assert percorso.is_file(), nome
        # trasparente davvero: il PNG ha il canale alfa e gli angoli sono vuoti
        from struct import unpack
        with open(percorso, "rb") as fh:
            testa = fh.read(26)
        assert testa[12:16] == b"IHDR"
        larghezza, altezza = unpack(">II", testa[16:24])
        assert testa[25] == 6, "il PNG non ha il canale alfa"
        assert larghezza == altezza == int(nome.split("-")[1].split(".")[0])
