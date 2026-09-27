"""L'interfaccia dei progetti: web/progetti.js, le sue parti in app.js e il telefono.

Il disegno si guarda (il 26/09/2026 in Chromium: chiaro e scuro, largo e
stretto, telefono a 390 px, un turno vero con il llama-server finto che scrive
la memoria); qui si provano le regole che, rotte, non si vedrebbero subito:

- la schermata del progetto prende il posto della chat e spegne la colonna di
  destra, invece di affiancarsi;
- la chat nuova del progetto non duplica l'invio;
- la memoria scritta a fine turno arriva nel thread dopo le gocce dei file, in
  diretta e riaprendo la chat, con il testo scappato;
- "Nuova chat" da dentro un progetto porta fra le libere;
- una riga di conversazione ha una funzione sola, in tutti gli elenchi;
- del vault non resta niente che il browser chiami.

Serve ``quickjs`` per le parti eseguite davvero (e' fra le dipendenze di
sviluppo): senza, quei test saltano.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

RADICE = Path(__file__).resolve().parents[1]
WEB = RADICE / "web"
MOBILE = RADICE / "web_mobile"


def _testo(nome: str, cartella: Path = WEB) -> str:
    return (cartella / nome).read_text(encoding="utf-8")


def _corpo(sorgente: str, firma: str) -> str:
    corpo = sorgente[sorgente.index(firma):]
    return corpo[: corpo.index("\n}\n") + 2]


# ---------------------------------------------------------------------------
# La pagina
# ---------------------------------------------------------------------------


def test_la_pagina_ha_i_pezzi_dei_progetti_e_non_quelli_del_vault():
    html = _testo("index.html")
    for pezzo in ('id="progetto-col"', 'id="progetti-sec"', 'id="progetti"', 'id="progetto-nuovo"',
                  'id="progetto-chip"', 'id="pr-mem-card"', 'id="pr-velo"', 'id="pr-menu"',
                  '/static/progetti.js', '/static/progetti.css'):
        assert pezzo in html, pezzo
    assert "vault" not in html.lower()
    # progetti.js prima di app.js, come impostazioni.js: usa le funzioni di
    # app.js solo quando viene chiamato.
    assert html.index("/static/progetti.js") < html.index("/static/app.js")


def test_nessuno_chiama_piu_le_rotte_del_vault():
    for sorgente in (_testo("app.js"), _testo("progetti.js"), _testo("impostazioni.js"),
                     _testo("app.js", MOBILE)):
        assert "/api/vaults" not in sorgente


def test_gli_asset_dei_progetti_hanno_l_impronta():
    main = (RADICE / "server" / "main.py").read_text(encoding="utf-8")
    corpo = main[main.index("def index() -> Response:"):]
    corpo = corpo[: corpo.index("return Response(")]
    assert '"progetti.js", "progetti.css"' in corpo


def test_la_schermata_prende_il_posto_della_chat_e_spegne_la_colonna_di_destra():
    """``display: flex`` batte ``[hidden]``: la trappola pagata due volte."""
    css = _testo("progetti.css")
    assert "#progetto-col[hidden] { display: none; }" in css
    assert "#chat-col[hidden] { display: none; }" in _testo("style.css")
    assert "#app.pr-home-attiva #panel, #app.pr-home-attiva #panel-grip { display: none; }" in css
    js = _testo("progetti.js")
    corpo = _corpo(js, "function mostraProgettoHome(")
    assert "casa.hidden = !attiva" in corpo and "chat.hidden = !!attiva" in corpo
    # L'anteprima segue la chat: nascosta, non chiusa.
    assert "sincronizzaAnteprima();" in corpo
    sincro = _corpo(_testo("app.js"), "function sincronizzaAnteprima(")
    assert "state.previewAperta && !state.progettoHome" in sincro


def test_ogni_hidden_con_display_esplicito_ha_la_sua_riga():
    """La regola scritta dopo la seconda volta: ogni elemento nascosto con
    ``hidden`` che abbia un ``display`` esplicito ha bisogno della riga
    ``[hidden]``."""
    css = _testo("progetti.css")
    for selettore in ("#progetto-col", ".pr-velo", ".pr-menu", ".pr-chip", "#pr-mem-card"):
        assert f"{selettore}[hidden]" in css, selettore


def test_la_chat_nuova_del_progetto_non_duplica_l_invio():
    corpo = _corpo(_testo("progetti.js"), "async function nuovaChatNelProgetto(")
    assert "await newSession();" in corpo and "await send();" in corpo
    assert "fetch(" not in corpo and "/api/chat" not in corpo


def test_nuova_chat_da_dentro_un_progetto_porta_fra_le_libere():
    """Le chat di un progetto nascono dalla sua schermata; il pulsante in cima
    alla colonna fa una conversazione libera, nell'ultima cartella libera."""
    app = _testo("app.js")
    assert "$('#new-chat').onclick = nuovaChatLibera;" in app
    corpo = _corpo(app, "async function nuovaChatLibera(")
    assert "recent_workspaces" in corpo and "!prDelPercorso(p)" in corpo
    assert corpo.index("useWorkspace(libera)") < corpo.index("await newSession();")


def test_una_riga_di_conversazione_ha_una_funzione_sola():
    app = _testo("app.js")
    js = _testo("progetti.js")
    assert js.count("function rigaSessione(") == 1
    assert "rigaSessione(" in _corpo(app, "function renderSessions(")
    assert "rigaSessione(item)" in _corpo(js, "function ramoProgetto(")
    # Cancellare non e' piu' un clic su una x da 18 px: passa dal menu e da
    # una conferma.
    assert "session-del" not in app and "session-del" not in js
    elimina = _corpo(js, "async function eliminaChat(")
    assert "finestraConferma(" in elimina and "pericolo: true" in elimina


def test_la_memoria_arriva_nel_thread_dopo_le_gocce_dei_file():
    app = _testo("app.js")
    storia = _corpo(app, "function renderHistory(")
    ramo = storia[storia.index("if (msg.role === 'memoria')"):]
    ramo = ramo[: ramo.index("return;")]
    assert ramo.index("t.showFiles();") < ramo.index("nodoMemoria(")
    assert "case 'memoria':" in app and "eventoMemoria(event, turn, setStatus);" in app
    diretta = _corpo(_testo("progetti.js"), "function eventoMemoria(")
    assert diretta.count("turn.showFiles();") == 2
    assert "window.Cruscotto?.toolInizio('memoria del progetto')" in diretta


def test_la_memoria_si_corregge_e_si_toglie_con_le_rotte_dell_utente():
    js = _testo("progetti.js")
    assert "method: 'PATCH', body: JSON.stringify({ path, id: v.id, testo, tipo })" in js
    assert "/api/progetti/memoria?path=" in js and "method: 'DELETE'" in js
    # Chi corregge una voce ne diventa l'autore: la finestra lo dice prima.
    assert "Corretta da te, la voce diventa tua" in js
    # Togliere chiede conferma sul posto.
    assert "Togliere questa voce?" in _corpo(js, "function prConfermaTogli(")


def test_le_istruzioni_si_scrivono_nella_loro_scheda_e_non_nelle_impostazioni():
    """Un posto solo per un testo che va al modello."""
    js = _testo("progetti.js")
    impostazioni = _corpo(js, "function finestraImpostazioniProgetto(")
    assert "istruzioni" not in impostazioni.split("const salva")[1]
    assert "salvaProgetto({ istruzioni: area.value })" in js


def test_la_colonna_ha_il_suo_divisorio_e_passa_da_bindgrip():
    app = _testo("app.js")
    assert "bindGrip($('#progetti-grip')" in app
    assert "function setProgettiHeight(" in app


# ---------------------------------------------------------------------------
# Le parti pure, eseguite davvero
# ---------------------------------------------------------------------------

quickjs = pytest.importorskip("quickjs")

STUB = r"""
var window = {};
var state = { progetti: [], memoriaNuove: new Set() };
function esc(text) {
  return String(text ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
function el(tag, className, html) {
  return { tag, className, innerHTML: html || '', querySelector() { return {}; } };
}
function relTime() { return '3h'; }
"""


@pytest.fixture()
def js() -> quickjs.Context:
    ctx = quickjs.Context()
    ctx.eval(STUB)
    ctx.eval(_testo("progetti.js"))
    return ctx


def test_la_stessa_cartella_scritta_in_due_modi_e_lo_stesso_progetto(js):
    assert js.eval(r"prChiave('C:\\Users\\Filip\\Progetto\\')") == js.eval(r"prChiave('c:/users/filip/progetto')")
    assert js.eval("prChiave('/home/a/B/')") == "/home/a/B"
    js.eval(r"state.progetti = [{path: 'C:\\Users\\filip\\P', nome: 'P'}];")
    assert js.eval(r"prDelPercorso('c:\\users\\filip\\p\\').nome") == "P"
    assert js.eval("prDelPercorso('/altrove')") is None


def test_la_tinta_del_progetto_e_stabile_e_sta_nelle_sei(js):
    tinte = [js.eval(f"prTinta({json.dumps(n)})") for n in ("Casa", "Magazzino", "OB", "", "é")]
    assert all(0 <= t < 6 for t in tinte)
    assert js.eval("prTinta('Casa')") == tinte[0]
    assert js.eval("prIniziale('  ómega')") == "Ó" and js.eval("prIniziale('')") == "·"


def test_la_goccia_della_memoria_scappa_il_testo(js):
    esito = {
        "aggiunte": [{"id": "a", "tipo": "decisione", "testo": "<img src=x onerror=alert(1)>"}],
        "modificate": [{"prima": {"id": "b", "tipo": "fatto", "testo": "vecchio & basta"},
                        "dopo": {"id": "b", "tipo": "fatto", "testo": "nuovo"}}],
        "tolte": [{"id": "c", "tipo": "aperto", "testo": "fatto <b>davvero</b>"}],
        "riassunto": "+1 · 1 modificata · 1 tolta",
    }
    html = js.eval(f"nodoMemoria({json.dumps(esito)}).innerHTML")
    assert "<img" not in html and "&lt;img src=x" in html
    assert "vecchio &amp; basta" in html and "<del>fatto &lt;b&gt;davvero&lt;/b&gt;</del>" in html
    assert "Memoria del progetto aggiornata" in html and "+1 · 1 modificata · 1 tolta" in html


def test_una_goccia_senza_cambiamenti_dice_perche(js):
    nodo = js.eval("(() => { const n = nodoMemoria({aggiunte: [], modificate: [], tolte: []}, "
                   "'La finestra di contesto e\\' troppo piena'); return n.className + '|' + n.innerHTML; })()")
    classe, html = nodo.split("|", 1)
    assert "muta" in classe and "troppo piena" in html


def test_il_titolo_della_chat_si_accorcia_senza_doppi_puntini(js):
    assert js.eval("prAccorcia('Sistema il parser: le graffe annidate non si...', 20)") == "Sistema il parser:…"
    assert js.eval("prAccorcia('breve', 20)") == "breve"


def test_i_tipi_della_memoria_sono_quelli_del_server():
    from core.progetto import ETICHETTE_TIPO, MAX_VOCE_CHARS, MAX_VOCI_MEMORIA, TIPI_MEMORIA

    js = _testo("progetti.js")
    trovati = re.findall(r"\{ k: '(\w+)', gruppo: '([^']+)'", js)
    assert [k for k, _ in trovati] == list(TIPI_MEMORIA)
    assert dict(trovati) == ETICHETTE_TIPO
    assert f"const PR_MAX_VOCE = {MAX_VOCE_CHARS};" in js
    assert f"const PR_MAX_VOCI = {MAX_VOCI_MEMORIA};" in js


def test_il_limite_delle_istruzioni_e_quello_del_server():
    from core.progetto import MAX_ISTRUZIONI_CHARS

    assert f"const PR_MAX_ISTRUZIONI = {MAX_ISTRUZIONI_CHARS};" in _testo("progetti.js")


# ---------------------------------------------------------------------------
# Il telefono
# ---------------------------------------------------------------------------


def test_il_telefono_raggruppa_per_progetto_e_legge_la_memoria():
    html = _testo("index.html", MOBILE)
    for pezzo in ('id="progetti-mobile"', 'id="memoria-foglio"', 'id="chat-progetto"'):
        assert pezzo in html, pezzo
    # Le libere restano l'unico contenuto di #session-list: i progetti stanno
    # in un contenitore loro, sopra.
    assert html.index('id="progetti-mobile"') < html.index('id="session-list"')
    app = _testo("app.js", MOBILE)
    carica = _corpo(app, "async function loadSessions(")
    assert 'api("/api/sessions?tutte=1")' in carica and 'api("/api/progetti")' in carica
    assert "!s.progetto" in _corpo(app, "function renderSessions(")
    # In sola lettura: dal telefono la memoria non si scrive.
    assert "/api/progetti/memoria" not in app
    assert 'case "memoria":' in app and 'm.role === "memoria"' in app
