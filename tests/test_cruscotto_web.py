"""Il cruscotto nel browser: la logica di web/cruscotto.js e di web_mobile, con JS vero.

Il disegno si guarda (il 26/09/2026 e' stato provato in Chromium, chiaro e
scuro, largo e stretto, desktop e telefono); qui si prova lo **stato**: che le
righe della timeline nascano dagli eventi giusti, che i tool si attacchino al
passo giusto, che i punti del grafico scalino di un turno quando ne parte uno
nuovo, che le statistiche del server non calpestino un turno vivo. E' la parte
che, sbagliata, disegna benissimo un numero falso.

Serve ``quickjs`` (e' fra le dipendenze di sviluppo): senza, i test saltano.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

quickjs = pytest.importorskip("quickjs")

RADICE = Path(__file__).resolve().parents[1]
CRUSCOTTO = RADICE / "web" / "cruscotto.js"
APP = RADICE / "web" / "app.js"
MOBILE = RADICE / "web_mobile" / "app.js"
HTML = RADICE / "web" / "index.html"

# Un browser ridotto all'osso: niente elementi (getElementById restituisce
# null, e ``disegna`` esce subito), niente timer. Resta lo stato.
BROWSER = """
var window = {};
var __ora = 1000;
var performance = { now() { return __ora; } };
var document = {
  readyState: 'complete', hidden: false,
  getElementById() { return null; },
  querySelectorAll() { return []; },
  addEventListener() {},
};
var localStorage = { getItem() { return null; }, setItem() {} };
function setTimeout() { return 0; }
function setInterval() { return 1; }
function clearInterval() {}
"""


@pytest.fixture()
def js() -> quickjs.Context:
    ctx = quickjs.Context()
    ctx.eval(BROWSER)
    ctx.eval(CRUSCOTTO.read_text(encoding="utf-8"))
    ctx.eval("var C = window.Cruscotto, S = C._stato;")
    return ctx


def evento(ctx: quickjs.Context, chiamata: str, *argomenti) -> None:
    ctx.eval(f"C.{chiamata}({', '.join(json.dumps(a) for a in argomenti)});")


def metrica(passo: int, **campi) -> dict:
    base = {"passo": passo, "fase": "pensiero", "definitivo": False, "generati": 50,
            "pensiero": 50, "risposta": 0, "chiamate": 0, "tok_s": 25.0, "tok_s_passo": 24.0,
            "attesa_ms": 900, "generazione_ms": 2000, "durata_ms": 2900, "prompt": 12000,
            "prompt_stimato": 11500, "cache": 11000, "prefill_token": 1000, "prefill_ms": 1100,
            "finestra": 65536, "fonte": "server", "turno_inizio": 1_700_000_000.0}
    base.update(campi)
    return base


def test_un_turno_fa_una_riga_per_passo_e_i_tool_vanno_al_passo_giusto(js):
    evento(js, "inizioTurno")
    evento(js, "passo", 1, 40)
    evento(js, "metriche", metrica(1))
    evento(js, "metriche", metrica(1, definitivo=True, fase="fine", generati=120))
    evento(js, "toolInizio", "read_file")
    evento(js, "toolFine", "read_file", 0.25)
    evento(js, "toolFine", "read_file", 0.5)
    evento(js, "passo", 2, 40)
    evento(js, "metriche", metrica(2, fase="risposta", risposta=30, pensiero=0))
    righe = json.loads(js.eval("JSON.stringify(S.righe)"))
    assert [r["passo"] for r in righe] == [1, 2]
    assert righe[0]["tool"] == {"read_file": 2} and righe[0]["tool_ms"] == 750
    assert righe[0]["generati"] == 120 and not righe[0]["vivo"]
    assert righe[1]["vivo"] and righe[1]["tool_ms"] == 0
    # Il passo chiuso diventa un punto del grafico del turno in corso.
    assert json.loads(js.eval("JSON.stringify(S.puntiTurno)")) == [[12000, 24.0]]
    # Il contesto e' quello del passo, non la stima di fine turno.
    assert js.eval("S.contesto.usato") == 12000 and js.eval("S.contesto.esatto") is True


def test_la_compattazione_marca_il_passo_che_viene_dopo(js):
    evento(js, "inizioTurno")
    evento(js, "passo", 1, 10)
    evento(js, "metriche", metrica(1, definitivo=True))
    evento(js, "compattato")
    evento(js, "passo", 2, 10)
    assert js.eval("S.righe[0].compattato") is False
    assert js.eval("S.righe[1].compattato") is True


def test_i_punti_scalano_di_un_turno_quando_ne_parte_uno_nuovo(js):
    # Statistiche a riposo: due turni fa e l'ultimo.
    evento(js, "stats", {"context_used": 9000, "context_window": 65536,
                         "cruscotto": {"punti": [[5000, 30.0, -1], [9000, 28.0, 0]],
                                       "turni": [], "ultimo": None}})
    assert json.loads(js.eval("JSON.stringify(S.punti)")) == [[5000, 30.0, -1], [9000, 28.0, 0]]
    evento(js, "inizioTurno")
    assert json.loads(js.eval("JSON.stringify(S.punti)")) == [[5000, 30.0, -2], [9000, 28.0, -1]]
    # Le statistiche che arrivano a turno vivo (la risposta a POST /api/chat)
    # non sanno ancora del turno nuovo: il loro "ultimo" e' gia' vecchio.
    evento(js, "stats", {"context_used": 9500, "context_window": 65536,
                         "cruscotto": {"punti": [[9000, 28.0, 0]], "turni": [], "ultimo": None}})
    assert json.loads(js.eval("JSON.stringify(S.punti)")) == [[9000, 28.0, -1]]
    assert js.eval("S.vivo") is True


def test_le_statistiche_a_turno_vivo_non_calpestano_la_timeline(js):
    evento(js, "inizioTurno")
    evento(js, "passo", 1, 10)
    evento(js, "metriche", metrica(1))
    evento(js, "stats", {"context_used": 1, "context_window": 65536,
                         "cruscotto": {"punti": [], "turni": [],
                                       "ultimo": {"righe": [{"passo": 7}], "durata_ms": 5}}})
    assert js.eval("S.righe.length") == 1 and js.eval("S.righe[0].passo") == 1
    assert js.eval("S.contesto.usato") == 12000


def test_a_riposo_la_timeline_viene_dallo_storico_del_server(js):
    riga = {"passo": 1, "attesa_ms": 800, "generazione_ms": 4000, "tool_ms": 300,
            "generati": 100, "pensiero": 80, "risposta": 20, "chiamate": 0, "tok_s": 25.0,
            "prompt": 10000, "cache": 9000, "prefill_token": 1000, "finestra": 32768,
            "tool": {"edit_file": 1}}
    evento(js, "stats", {"context_used": 10400, "context_window": 16384,
                         "cruscotto": {"punti": [[10000, 25.0, 0]], "turni": [],
                                       "ultimo": {"righe": [riga], "durata_ms": 5200}}})
    assert js.eval("S.righe.length") == 1 and js.eval("S.righe[0].vivo") is False
    assert js.eval("S.riassunto.tok_s") == pytest.approx(25.0)
    # A riposo il contesto e' la stima del server per il prossimo messaggio,
    # sulla finestra vera del passo (llama.cpp la legge da /props).
    assert js.eval("S.contesto.usato") == 10400 and js.eval("S.contesto.finestra") == 32768


def test_fine_turno_riassume_e_conta_le_correzioni(js):
    evento(js, "inizioTurno")
    evento(js, "passo", 1, 10)
    evento(js, "metriche", metrica(1, definitivo=True))
    evento(js, "fine", {"reason": "completed", "steps": 1,
                        "usage": {"completion_tokens": 300, "eval_ms": 10000,
                                  "total_ms": 14000, "nudges": {"verify": 2, "tool": 0}}})
    assert js.eval("S.vivo") is False
    assert js.eval("S.riassunto.tok_s") == pytest.approx(30.0)
    assert json.loads(js.eval("JSON.stringify(S.riassunto.nudges)")) == [["verify", 2]]


def test_cambiare_chat_azzera_tutto(js):
    evento(js, "inizioTurno")
    evento(js, "passo", 1, 10)
    evento(js, "metriche", metrica(1, definitivo=True))
    evento(js, "reset")
    assert js.eval("S.righe.length + S.punti.length + S.puntiTurno.length + S.traccia.length") == 0
    assert js.eval("S.vivo") is False


def test_una_metrica_senza_start_apre_il_turno(js):
    """Riattacco a turno avanzato: il primo frame puo' non essere ``start``."""
    evento(js, "metriche", metrica(4))
    assert js.eval("S.vivo") is True and js.eval("S.righe[0].passo") == 4


def test_la_traccia_resta_lunga_quanto_serve(js):
    evento(js, "inizioTurno")
    evento(js, "passo", 1, 10)
    for i in range(3000):
        js.eval(f"C.metriche({json.dumps(metrica(1, tok_s=float(i % 40)))});")
    n = js.eval("S.traccia.length")
    assert 0 < n <= 1200


# ---------------------------------------------------------------------------
# Cuciture con app.js e index.html
# ---------------------------------------------------------------------------


def test_app_js_passa_al_cruscotto_ogni_evento_che_gli_serve():
    sorgente = APP.read_text(encoding="utf-8")
    corpo = sorgente[sorgente.index("function handleEvent("):]
    corpo = corpo[: corpo.index("\n}\n")]
    for chiamata in ("Cruscotto?.inizioTurno()", "Cruscotto?.passo(event.step, event.total)",
                     "Cruscotto?.metriche(event)", "Cruscotto?.toolInizio(event.name)",
                     "Cruscotto?.toolFine(event.name, event.duration_s)",
                     "Cruscotto?.compattato()", "Cruscotto?.fine(event)"):
        assert chiamata in corpo, chiamata
    # Gli id che il vecchio pannello riempiva non esistono piu': nessuno li cerca.
    for vecchio in ("#u-speed", "#usage-block", "#ctx-window", "#u-draft"):
        assert vecchio not in sorgente, vecchio


def test_la_pagina_carica_il_cruscotto_prima_di_app_js_con_l_impronta():
    html = HTML.read_text(encoding="utf-8")
    assert html.index("/static/cruscotto.js") < html.index("/static/app.js")
    assert "/static/cruscotto.css" in html
    main = (RADICE / "server" / "main.py").read_text(encoding="utf-8")
    elenco = main[main.index("for nome in (\"app.js\""):]
    elenco = elenco[: elenco.index("):")]
    for nome in ("cruscotto.js", "cruscotto.css", "impostazioni.js", "impostazioni.css"):
        assert nome in elenco, nome


def test_ogni_id_che_il_cruscotto_cerca_esiste_nella_pagina():
    sorgente = CRUSCOTTO.read_text(encoding="utf-8")
    html = HTML.read_text(encoding="utf-8")
    cercati = set(re.findall(r"\$id\('([\w-]+)'\)", sorgente))
    assert len(cercati) > 20
    mancanti = sorted(i for i in cercati if f'id="{i}"' not in html)
    assert not mancanti, mancanti


# ---------------------------------------------------------------------------
# Telefono
# ---------------------------------------------------------------------------


def _funzioni(nomi: list[str]) -> str:
    sorgente = MOBILE.read_text(encoding="utf-8")
    pezzi = []
    for nome in nomi:
        trovata = re.search(rf"^function {nome}\([^)]*\) \{{.*?^\}}", sorgente, re.MULTILINE | re.DOTALL)
        assert trovata, nome
        pezzi.append(trovata.group(0))
    return "\n".join(pezzi)


def test_il_telefono_tiene_goccia_e_contesto_dalle_metriche():
    ctx = quickjs.Context()
    ctx.eval("""
      var __el = {};
      function $(id) { return null; }
      var misure = { tokS: null, viva: false, usato: 0, finestra: 0, esatto: false,
        cache: null, draftN: null, draftOk: null };
      function disegnaGoccia() {}
      function disegnaContesto() {}
    """)
    ctx.eval(_funzioni(["fmtUno", "fmtMigliaia", "applicaMetriche", "applicaStats", "dettaglioMisure"]))
    ctx.eval(f"applicaMetriche({json.dumps(metrica(2, fase='risposta', tok_s=27.4, draft_n=100, draft_accettati=71))});")
    assert ctx.eval("misure.viva") is True and ctx.eval("misure.tokS") == 27.4
    assert ctx.eval("misure.usato") == 12000 and ctx.eval("misure.finestra") == 65536
    dettaglio = ctx.eval("dettaglioMisure()")
    # QuickJS non ha le convenzioni italiane dei numeri: virgola o punto.
    assert re.search(r"27[,.]4 tok/s adesso", dettaglio)
    assert "MTP 71%" in dettaglio and "cache 92%" in dettaglio
    ctx.eval("""applicaStats({context_used: 13000, context_window: 65536,
      cruscotto: {turni: [{tok_s: 25.5}], ultimo: {righe: [{cache: 12000, draft_n: 10, draft_accettati: 7}]}}});""")
    assert ctx.eval("misure.viva") is False and ctx.eval("misure.tokS") == 25.5
    assert "(ultimo turno)" in ctx.eval("dettaglioMisure()")
