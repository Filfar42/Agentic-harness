"""La goccia del livello di pensiero nel composer.

Quattro opzioni -- Low, Medium, High, Auto -- e un contratto preciso:
Low/Medium/High sono un override **puntuale** del turno (arriva al backend
come ``"think"`` nelle options della richiesta) senza toccare le impostazioni;
Auto non e' un override: vale ``native_think`` delle impostazioni, come prima
della goccia. La scelta sopravvive alla sospensione su ``ask_user_question``.
"""

from __future__ import annotations

import sys
import time
from typing import Any
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from tests.test_server import client, current_session, fake_ollama  # noqa: F401

WEB = Path(__file__).resolve().parents[1] / "web"


def _ultimo_payload(client, session_id: str, **extra) -> dict:
    """Un turno intero col finto Ollama e il payload dell'ULTIMA richiesta.

    Il finto server registra ogni body arrivato su /api/chat in _Handler.calls:
    e' l'unico posto in cui si vede che cosa il backend ha davvero ricevuto --
    verificare ChatRequest non proverebbe niente sulla strada verso Ollama.
    """
    import tests.test_agent_loop as fake

    # Fra un test e l'altro ci pensa la fixture ``fake_ollama``, che riazzera
    # ``calls`` all'ingresso. Questo svuotamento serve per le chiamate multiple
    # DENTRO lo stesso test: senza, "l'ultima richiesta" e' l'ultima del turno
    # precedente e l'asserzione guarda il payload sbagliato.
    fake._Handler.calls.clear()
    corpo: dict[str, Any] = {"session_id": session_id, "prompt": "ciao"}
    corpo.update(extra)
    started = client.post("/api/chat", json=corpo)
    assert started.status_code == 200, started.text
    scadenza = time.time() + 15
    while time.time() < scadenza:
        if not client.server.RUNNERS.running_ids():
            break
        time.sleep(0.05)
    assert not client.server.RUNNERS.running_ids(), "turno mai finito"
    assert fake._Handler.calls, "nessuna richiesta arrivata al finto Ollama"
    return fake._Handler.calls[-1]


@pytest.fixture()
def script_risposta(monkeypatch):
    """Lo script piu' corto: un passo, una risposta, fine."""
    import tests.test_agent_loop as fake

    # Nessun salva/ripristina a mano: lo fa ``monkeypatch``, ed e' il motivo
    # per cui questa fixture lo usa invece dell'assegnazione diretta.
    monkeypatch.setattr(fake, "SCRIPT", [[{"message": {"content": "Fatto."}}]])
    yield


# ---------------------------------------------------------------------------
# Il percorso vero: HTTP -> worker -> options della richiesta al backend
# ---------------------------------------------------------------------------


class TestLivelloArrivaAlBackend:
    def test_high_arriva_come_think_nelle_options(self, client, fake_ollama, script_risposta):
        payload = _ultimo_payload(
            client, current_session(client), think_level="high"
        )
        assert payload["think"] == "high"

    def test_low_e_medium_arrivano_col_loro_valore(self, client, fake_ollama):
        basso = _ultimo_payload(client, current_session(client), think_level="low")
        assert basso["think"] == "low"
        medio = _ultimo_payload(
            client, current_session(client), think_level="medium"
        )
        assert medio["think"] == "medium"

    def test_auto_non_manda_override(self, client, fake_ollama, script_risposta):
        # "auto" NON viaggia sulla rete: e' l'assenza di scelta, il server fa
        # come da impostazioni. Il frontend omette il campo; qui si verifica
        # il contratto lato server: senza campo, nessun "think" al backend.
        payload = _ultimo_payload(client, current_session(client))
        assert "think" not in payload
        # E il sorgente JS non serializza mai 'auto' come valore: il campo
        # parte solo per low/medium/high.
        js = (WEB / "app.js").read_text(encoding="utf-8")
        assert "thinkLevel === 'auto' ? undefined : state.thinkLevel" in js

    def test_assente_come_auto(self, client, fake_ollama, script_risposta):
        # I vecchi client non mandano il campo: stesso comportamento di "auto".
        payload = _ultimo_payload(client, current_session(client))
        assert "think" not in payload

    def test_valore_sconosciuto_rifiutato(self, client):
        r = client.post(
            "/api/chat",
            json={"session_id": current_session(client), "prompt": "x", "think_level": "ultra"},
        )
        assert r.status_code == 422  # validazione pydantic, non un 500


# ---------------------------------------------------------------------------
# Le impostazioni restano quelle: la goccia e' puntuale, per-turno
# ---------------------------------------------------------------------------


class TestLeImpostazioniNonSiToccano:
    @pytest.mark.parametrize("livello", ["low", "medium", "high"])
    def test_native_think_invariato_dopo_il_turno(self, client, fake_ollama, script_risposta, livello):
        sid = current_session(client)
        # Le impostazioni persistono fra i run: parti da uno stato noto.
        client.post("/api/settings", json={"values": {"native_think": "auto"}})
        _ultimo_payload(client, sid, think_level=livello)
        # Non esiste una GET /api/settings: lo stato vero sta in STATE,
        # ed e' li' che il server guarda quando costruisce i params.
        assert client.server.STATE.settings["native_think"] == "auto"

    @pytest.mark.parametrize("salvata,livello,atteso", [
        ("auto", None, "assente"),   # nessuna fonte: niente pensiero
        ("auto", "high", "high"),    # la goccia vince sull'auto
        ("low", None, "low"),        # solo l'impostazione: vale lei
        ("medium", "high", "high"),  # la goccia vince sull'impostazione
    ])
    def test_matrice_salvato_vs_goccia(self, client, fake_ollama, script_risposta,
                                       salvata, livello, atteso):
        client.post("/api/settings", json={"values": {"native_think": salvata}})
        extra = {"think_level": livello} if livello is not None else {}
        payload = _ultimo_payload(client, current_session(client), **extra)
        if atteso == "assente":
            assert "think" not in payload
        else:
            assert payload["think"] == atteso

    def test_la_scelta_sopravvive_alla_domanda(self, client, fake_ollama):
        """Ripresa dopo ask_user_question: riparte con lo stesso livello."""
        import tests.test_agent_loop as fake

        original = fake.SCRIPT
        fake.SCRIPT = [
            [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "ask_user_question",
                                    "arguments": {"question": "Formato?", "options": ["A"]},
                                }
                            }
                        ],
                    }
                }
            ],
            [{"message": {"content": "Fatto."}}],
        ]
        try:
            sid = current_session(client)
            client.post("/api/chat", json={"session_id": sid, "prompt": "chiedimi",
                                           "think_level": "high"})
            scadenza = time.time() + 15
            while time.time() < scadenza and client.server.RUNNERS.running_ids():
                time.sleep(0.05)
            client.post("/api/answer", json={"session_id": sid, "answer": "A"})
            scadenza = time.time() + 15
            while time.time() < scadenza and client.server.RUNNERS.running_ids():
                time.sleep(0.05)

            richieste = [c for c in fake._Handler.calls if c.get("messages")]
            assert len(richieste) >= 2
            # ENTRAMBE le gambe del turno hanno chiesto pensiero high.
            assert all(c.get("think") == "high" for c in richieste)
        finally:
            fake.SCRIPT = original


# ---------------------------------------------------------------------------
# Il cablaggio nella UI: markup e JS (statici, come il resto dei test web)
# ---------------------------------------------------------------------------


def test_la_goccia_esiste_nel_composer_al_posto_giusto():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    riga = html.index('id="composer-wrap"')
    ricerca = html.index('id="toggle-web-search"', riga)
    invio = html.index('id="send"', riga)
    goccia = html.index('id="toggle-think"', riga)
    # Fra la ricerca online e l'invio, com'e' stato chiesto.
    assert ricerca < goccia < invio
    menu = html.index('id="think-menu"', goccia)
    assert menu > goccia  # la tendina vive nell'ancora della goccia
    assert 'aria-haspopup="listbox"' in html[goccia:goccia + 200]
    assert "<span id=\"think-label\">Auto</span>" in html  # default: Auto


def test_le_quattro_opzioni_hanno_i_nomi_chiesti():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    blocco = js[js.index("LIVELLI_THINK"):js.index("const gocciaThink")]
    for nome in ("'Low'", "'Medium'", "'High'", "'Auto'"):
        assert nome in blocco, f"manca {nome} nella tendina"
    # L'"auto" non parte per la rete: undefined viene scartato da JSON.stringify.
    assert "thinkLevel === 'auto' ? undefined : state.thinkLevel" in js
    # ...e il valore scelto parte nel payload dell'invio.
    assert "think_level:" in js


def test_lo_stile_usa_la_grammatica_dei_menu_esistenti():
    css = (WEB / "style.css").read_text(encoding="utf-8")
    assert ".think-anchor" in css and ".think-menu" in css
