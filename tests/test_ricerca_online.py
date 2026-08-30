"""La modalita' "Ricerca online": goccia -> flag -> tool visibile solo allora.

Il vincolo dell'utente: quando la goccia e' SPENTA il modello non solo non puo'
chiamare web_search, non deve nemmeno sapere che esiste -- niente schema,
nessuna riga di prompt. Quando e' ACCESA lo schema c'e', la clausola di prompt
c'e', e una chiamata reale viene eseguita contro un HTML finto di DuckDuckGo
(nessun accesso alla rete nei test).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import tools as tools_mod  # noqa: E402
from core.prompts import append_web_search_clause  # noqa: E402
from core.tools import ToolContext  # noqa: E402
from tests.test_server import (  # noqa: E402,F401
    client,
    fake_ollama,
    read_sse,
    wait_until,
)

# HTML minimo ma fedele al markup dei risultati DDG: link con classe
# result__a e href di redirect uddg=<url codificato>.
HTML_DDG = """
<html><body>
<div class="result">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fguide&amp;rut=abc">
    Guida ufficiale</a>
  <a class="result__snippet">Tutto quello che c'e' da sapere.</a>
</div>
<div class="result">
  <a class="result__a" href="/l/?uddg=https%3A%2F%2Fdocs.example.com%2Fapi&amp;rut=def">
    API reference</a>
  <a class="result__snippet">Gli endpoint documentati.</a>
</div>
</body></html>
"""


class _RispostaFinta:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        pass


@pytest.fixture()
def rete_finta(monkeypatch):
    """Il tool fa `import httpx` dentro la funzione: si patcha il modulo vero."""
    import httpx

    chiamate: list[dict] = []

    def finto_post(url, *, data=None, headers=None, timeout=None,
                   follow_redirects=False):
        chiamate.append({"url": url, "data": dict(data or {})})
        return _RispostaFinta(HTML_DDG)

    monkeypatch.setattr(httpx, "post", finto_post)
    return chiamate


def _ctx(*, acceso: bool = True) -> ToolContext:
    return ToolContext(workspace=".", web_search_enabled=acceso)


# --------------------------------------------------------------------- tool


def test_tool_restituisce_titolo_url_e_snippet(rete_finta):
    """Valori attesi espliciti: i due risultati finti tornano nell'ordine."""
    out = tools_mod.tool_web_search(_ctx(), "fastapi lifespan", max_results=5)
    assert '"count": 2' in out
    assert '"title": "Guida ufficiale"' in out
    # L'URL arriva dal redirect uddg= e va decodificato.
    assert '"url": "https://example.org/guide"' in out
    assert "Tutto quello che c'e' da sapere." in out
    assert '"title": "API reference"' in out
    assert '"url": "https://docs.example.com/api"' in out
    assert '"query": "fastapi lifespan"' in out


def test_max_results_taglia_la_lista(rete_finta):
    out = tools_mod.tool_web_search(_ctx(), "q", max_results=1)
    assert '"count": 1' in out
    assert '"title": "Guida ufficiale"' in out
    assert "API reference" not in out


def test_query_vuota_non_interroga_la_rete(rete_finta):
    with pytest.raises(tools_mod.WorkspaceError):
        tools_mod.tool_web_search(_ctx(), "   ")
    assert not rete_finta


# ------------------------------------------------- visibilita' per il modello


def test_spento_il_tool_non_esiste_ne_in_schema_ne_nel_prompt():
    """Spento non costa niente: fuori dallo schema di base e fuori dal prompt.

    Il perimetro e' lo schema che parte con la richiesta, **non** l'elenco dei
    nomi che l'harness sa eseguire: vedi il test qui sotto.
    """
    nomi_base = {t["function"]["name"] for t in tools_mod.TOOLS_SCHEMA}
    assert "web_search" not in nomi_base
    base = "# Prompt"
    assert append_web_search_clause(base, enabled=False) == base


def test_una_tool_call_scritta_a_mano_per_web_search_viene_riconosciuta():
    """La regressione che rendeva muta la ricerca online sui modelli piccoli.

    ``TOOL_NAMES`` nasceva dal solo ``TOOLS_SCHEMA``, e ``_as_tool_call``
    scarta le chiamate con un nome che non conosce. I modelli che non emettono
    tool call native le scrivono come JSON nel testo -- qwen2.5-coder:7b e'
    uno di quelli -- e le loro ``web_search`` sparivano in silenzio: goccia
    accesa, nessuna ricerca, nessun errore da nessuna parte.
    """
    from core.agent import parse_text_tool_calls

    testo = '{"name": "web_search", "arguments": {"query": "meteo domani"}}'
    chiamate, _ = parse_text_tool_calls(testo)
    assert [c["name"] for c in chiamate] == ["web_search"]


def test_accesso_lo_schema_e_la_clausola_compaino():
    schema = tools_mod.TOOLS_SCHEMA + tools_mod.WEB_SEARCH_TOOLS
    nomi = [t["function"]["name"] for t in schema]
    assert nomi.count("web_search") == 1
    args = schema[-1]["function"]["parameters"]["properties"]
    assert set(args) == {"query", "max_results"}
    clausola = append_web_search_clause("# Prompt", enabled=True)
    assert clausola.startswith("# Prompt")
    assert "web_search" in clausola


def test_una_pagina_senza_risultati_torna_il_suggerimento_giusto(monkeypatch):
    """Il ramo che moriva di TypeError invece di parlare al modello.

    ``WorkspaceError`` era una ``Exception`` nuda: ``raise WorkspaceError(msg,
    hint=...)`` alzava ``TypeError: takes no keyword arguments``, il
    dispatcher lo leggeva come un errore di argomenti, e al modello arrivava
    "Argomenti non validi per 'web_search'" -- cioe' la diagnosi sbagliata,
    proprio quando il motore aveva risposto con una pagina di bot-check.
    """
    import httpx

    def bot_check(url, **kwargs):  # noqa: ARG001 - la firma e' quella di httpx.post
        return _RispostaFinta("<html><body>nessun risultato</body></html>")

    monkeypatch.setattr(httpx, "post", bot_check)
    payload = json.loads(
        tools_mod.dispatch(_ctx(), "web_search", {"query": "qualcosa"})
    )
    # Quel che conta qui e' che l'errore arrivi al modello **come testo**, con
    # il suo hint. Quale dei due messaggi sia -- e questa paginetta e' proprio
    # un bot-check, quindi e' il rifiuto -- lo decidono i due test in fondo.
    assert "motore di ricerca" in payload["error"]
    assert payload["hint"]


def test_dispatch_rifiuta_web_search_se_il_turno_non_lo_ha_chiesto():
    # Il modello non puo' scavalcare la goccia: fuori dal perimetro del turno
    # dispatch trasforma la chiamata nell'errore-testo standard per il modello.
    out = tools_mod.dispatch(_ctx(acceso=False), "web_search", {"query": "q"})
    assert '"error"' in out
    assert "non e' disponibile" in out


# --------------------------------------------------------------- flusso HTTP


def test_flag_dalla_goccia_arriva_al_turno(client):
    # Il difetto vero della sessione 20260822_211656_cdda: il flag arrivava a
    # schema e prompt ma NON al ToolContext, quindi la guardia del tool
    # respingeva la chiamata anche con la goccia accesa.
    stato = client.server.STATE
    session_id = client.post("/api/sessions", json={}).json()["session_id"]
    ctx = stato.tool_ctx(session_id, web_search=True)
    assert ctx.web_search_enabled is True
    assert stato.tool_ctx(session_id, web_search=False).web_search_enabled is False


def test_tool_ctx_del_server_alimenta_la_guardia(client, rete_finta):
    # End-to-end sul dispatcher: con il flag acceso una chiamata reale arriva
    # al parser (rete finta), con quello spento cade sulla guardia.
    stato = client.server.STATE
    session_id = client.post("/api/sessions", json={}).json()["session_id"]

    out = tools_mod.dispatch(
        stato.tool_ctx(session_id, web_search=True),
        "web_search",
        {"query": "voce pinerolese"},
    )
    parsed = json.loads(out)
    assert parsed["query"] == "voce pinerolese"
    assert len(parsed["results"]) == 2

    fuori = tools_mod.dispatch(
        stato.tool_ctx(session_id, web_search=False),
        "web_search",
        {"query": "q"},
    )
    assert "non e' disponibile" in fuori
    r = client.post(
        "/api/chat",
        json={"session_id": session_id, "prompt": "cerca", "web_search": True},
    )
    assert r.status_code == 200, r.text
    assert wait_until(lambda: not client.server.RUNNERS.is_running(session_id))


def test_schema_del_turno_segue_il_flag(client):
    stato = client.server.STATE
    spento = [t["function"]["name"] for t in stato.tools_schema(False)]
    acceso = [t["function"]["name"] for t in stato.tools_schema(True)]
    assert "web_search" not in spento
    assert "web_search" in acceso
    assert stato.system_prompt(False).count("web_search") == 0
    assert "web_search" in stato.system_prompt(True)


def test_la_goccia_sopravvive_a_una_domanda_dell_agente(client):
    """Il permesso non si perde a meta' compito.

    Un turno che passa da ``ask_user_question`` si ferma e riparte da
    ``/api/answer``, che il flag non ce l'ha: senza memoria della goccia, la
    ripresa girava con la ricerca spenta e il modello si prendeva un
    "modalita' non attiva" subito dopo la risposta dell'utente.
    """
    stato = client.server.STATE
    session_id = client.post("/api/sessions", json={}).json()["session_id"]

    r = client.post(
        "/api/chat",
        json={"session_id": session_id, "prompt": "cerca", "web_search": True},
    )
    assert r.status_code == 200, r.text
    assert wait_until(lambda: not client.server.RUNNERS.is_running(session_id))
    # E' questo il valore che /api/answer rilegge quando riprende il turno.
    assert stato.web_search_di(session_id) is True

    spento = client.post("/api/sessions", json={}).json()["session_id"]
    client.post("/api/chat", json={"session_id": spento, "prompt": "ciao"})
    assert wait_until(lambda: not client.server.RUNNERS.is_running(spento))
    assert stato.web_search_di(spento) is False


def test_senza_vault_registrati_il_tool_del_vault_non_compare(client):
    """Stessa regola della ricerca online: un tool che puo' solo fallire non
    si descrive ad ogni passo."""
    stato = client.server.STATE
    stato.settings["vaults"] = []
    assert "vault_search" not in [t["function"]["name"] for t in stato.tools_schema()]

    stato.settings["vaults"] = [{"path": "/tmp/vault", "nome": "appunti"}]
    assert "vault_search" in [t["function"]["name"] for t in stato.tools_schema()]


# ---------------------------------------------------------------------------
# Porta in faccia contro elenco vuoto: due cause, due consigli opposti
# ---------------------------------------------------------------------------

# Quello che l'endpoint HTML manda quando ha deciso che sei un bot: nessun
# blocco `result__a`, e una pagina corta.
HTML_RIFIUTO = """
<html><body><p>Unfortunately, bots use DuckDuckGo too.
Please try again later.</p></body></html>
"""

# Una pagina di risultati **vera** che pero' non ha trovato niente: il guscio
# del sito c'e' tutto, ed e' proprio la taglia a distinguerla dal rifiuto.
HTML_ZERO_RISULTATI = (
    "<html><head><title>ricerca</title></head><body>"
    + "<div class='site'>impaginazione e menu</div>" * 200
    + "<p>Nessun risultato.</p></body></html>"
)


@pytest.fixture()
def rete_che_rifiuta(monkeypatch):
    import httpx

    def finto_post(url, **_):
        return _RispostaFinta(HTML_RIFIUTO)

    monkeypatch.setattr(httpx, "post", finto_post)


@pytest.fixture()
def rete_senza_risultati(monkeypatch):
    import httpx

    def finto_post(url, **_):
        return _RispostaFinta(HTML_ZERO_RISULTATI)

    monkeypatch.setattr(httpx, "post", finto_post)


def test_il_limite_di_frequenza_non_si_cura_riformulando(rete_che_rifiuta):
    """Nella sessione del 29/08 la prima ricerca e' passata e le venti dopo
    no: il modello ha speso venti passi ad accorciare la query contro una
    pagina che non conteneva risultati per **nessuna** query. Il consiglio
    "riformula con parole piu' comuni" era quello sbagliato."""
    with pytest.raises(tools_mod.WorkspaceError) as errore:
        tools_mod.tool_web_search(_ctx(), "Endress Hauser")
    assert "rifiutato" in str(errore.value)
    assert "Non riformulare" in errore.value.hint


def test_un_elenco_davvero_vuoto_invita_ancora_a_riformulare(rete_senza_risultati):
    """L'altro caso esiste e il suo consiglio resta buono: distinguere serve a
    questo, non a sostituire un messaggio con un altro."""
    with pytest.raises(tools_mod.WorkspaceError) as errore:
        tools_mod.tool_web_search(_ctx(), "wh1190b aerotermo criogenico")
    assert "risultati interpretabili" in str(errore.value)
    assert "Riformula" in errore.value.hint
