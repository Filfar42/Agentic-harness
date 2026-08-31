"""La card ask_user_question dell'interfaccia mobile, con JS vero.

Le funzioni disegnate in ``web_mobile/app.js`` vengono estratte dal sorgente
ed eseguite in QuickJS contro un DOM fittizio: e' l'unico modo di provare che
i bottoni delle opzioni nascono davvero e che il testo scritto in risposta non
viene cancellato quando il polling ridisegna la card con la stessa domanda.

Serve ``quickjs`` (``pip install quickjs``): senza, i test saltano.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

quickjs = pytest.importorskip("quickjs")

APP_JS = Path(__file__).resolve().parents[1] / "web_mobile" / "app.js"


def _funzione(sorgente: str, nome: str) -> str:
    """Estrae dal sorgente la definizione completa di una funzione di primo livello."""
    trovata = re.search(rf"function {nome}\([^)]*\) \{{.*?\n\}}", sorgente, re.DOTALL)
    assert trovata, f"funzione {nome} non trovata in {APP_JS.name}"
    return trovata.group(0)


# DOM fittizio: solo quello che showQuestionCard/hideQuestionCard toccano.
# Le variabili sono ``var`` perche' restino visibili ai eval successivi.
STUB = """
var __inviate = [];
var __bottoni = [];
var __clicks = [];
var __nascosta = true;
function submitAnswer(scelta) { __inviate.push(scelta); }
var __el = {
  'question-card': {
    classList: {
      add(c) { if (c === 'hidden') __nascosta = true; },
      remove(c) { if (c === 'hidden') __nascosta = false; },
      contains(c) { return c === 'hidden' ? __nascosta : false; },
    },
    dataset: {},
  },
  'question-text': { textContent: '' },
  'answer-input': { value: '', placeholder: '', focus() {} },
  'question-options': {
    innerHTML: '',
    appendChild(b) { __bottoni.push(b); },
    querySelectorAll() { return []; },
  },
};
function $(id) { return __el[id]; }
var document = {
  createElement() {
    return {
      type: '', className: '', textContent: '', dataset: {},
      // I bottoni multi-select fanno classList.toggle('on'): serve lo stub.
      classList: {
        toggle(c) { this._s[c] = !this._s[c]; return this._s[c]; },
        contains(c) { return Boolean(this._s[c]); },
        _s: {},
      },
      addEventListener(ev, fn) { if (ev === 'click') __clicks.push(fn); },
    };
  },
};
"""


def contesto() -> quickjs.Context:
    sorgente = APP_JS.read_text(encoding="utf-8")
    js = STUB + _funzione(sorgente, "showQuestionCard") + "\n" + _funzione(sorgente, "hideQuestionCard")
    ctx = quickjs.Context()
    ctx.eval(js)
    return ctx


def test_le_opzioni_diventano_bottoni_che_rispondono() -> None:
    ctx = contesto()
    ctx.eval(
        "showQuestionCard({question: 'Quale formato?', options: ['TOML', 'JSON'],"
        " allow_multiple: false}, false);"
    )
    assert ctx.eval("__bottoni.length") == 2
    assert ctx.eval("__bottoni[0].textContent") == "TOML"
    assert ctx.eval("__bottoni[1].textContent") == "JSON"
    # Il tocco sul primo bottone invia esattamente quel valore.
    ctx.eval("__clicks[0]()")
    assert ctx.eval("JSON.stringify(__inviate)") == '["TOML"]'


def test_scelta_multipla_accumula_e_non_invia_al_tocco() -> None:
    ctx = contesto()
    ctx.eval(
        "showQuestionCard({question: 'Quali?', options: ['A', 'B'],"
        " allow_multiple: true}, false);"
    )
    # Due caselle + il bottone "Rispondi".
    assert ctx.eval("__bottoni.length") == 3
    ctx.eval("__clicks[0]()")  # spunto la prima: non deve inviare nulla
    assert ctx.eval("__inviate.length") == 0


def test_la_stessa_domanda_non_cancella_la_risposta_a_meta_scrittura() -> None:
    ctx = contesto()
    # La card e' gia' aperta sulla stessa domanda (e' il caso del polling che
    # ridisegna la conversazione ogni 5 secondi).
    ctx.eval(
        "showQuestionCard({question: 'Quale formato?'}, false);"
        "__el['answer-input'].value = 'sto scrivendo';"
        "showQuestionCard({question: 'Quale formato?'}, false);"
    )
    assert ctx.eval("__el['answer-input'].value") == "sto scrivendo"
    # Con una domanda DIVERSA invece il campo si azzera: e' una nuova richiesta.
    ctx.eval("showQuestionCard({question: 'Altra domanda'}, false);")
    assert ctx.eval("__el['answer-input'].value") == ""


def test_hide_azzecca_lo_stato_per_la_prossima_domanda() -> None:
    ctx = contesto()
    ctx.eval("showQuestionCard({question: 'Q'}, false); hideQuestionCard();")
    assert ctx.eval("__el['question-card'].dataset.domanda") == ""
    assert ctx.eval("__nascosta") is True


# ---------------------------------------------------------------------------
# Il markdown minimo delle bolle
# ---------------------------------------------------------------------------


def _formattatore():
    """`scriviFormattato` + `inline` su un DOM finto che registra i nodi."""
    sorgente = APP_JS.read_text(encoding="utf-8")
    stub = """
var __creati = [];
var document = {
  createElement(tag) {
    var n = {tag: tag, textContent: '', figli: []};
    __creati.push(n);
    return n;
  },
  createTextNode(t) { return {tag: '#text', textContent: t, figli: []}; },
};
function nuovoNodo() {
  return {
    tag: 'div', textContent: '', figli: [],
    appendChild(c) { this.figli.push(c); this.textContent += c.textContent; },
  };
}
"""
    ctx = quickjs.Context()
    ctx.eval(stub)
    ctx.eval(_funzione(sorgente, "inline"))
    ctx.eval(_funzione(sorgente, "scriviFormattato"))
    # appendChild sui nodi creati da createElement: serve dopo la definizione
    ctx.eval("""
function reset() { __creati = []; }
""")
    return ctx


def test_grassetto_e_codice_diventano_nodi_veri():
    """Il markdown dell'agente arrivava sul telefono come testo grezzo:
    `**grassetto**` con gli asterischi, in ogni risposta, tutti i giorni."""
    ctx = _formattatore()
    ctx.eval("var n = nuovoNodo(); scriviFormattato(n, 'usa **questo** e `pip install`');")
    tag = ctx.eval("n.figli.map(function (f) { return f.tag; }).join(',')")
    assert tag == "#text,strong,#text,code"
    assert ctx.eval("n.textContent") == "usa questo e pip install"


def test_il_testo_del_modello_finisce_solo_in_textContent():
    """La ragione per cui questa formattazione si poteva fare.

    Si costruiscono nodi, mai HTML: niente `innerHTML` da nessuna parte, quindi
    non c'è escaping da ricordarsi e la superficie che `textContent` chiudeva
    resta chiusa.
    """
    sorgente = APP_JS.read_text(encoding="utf-8")
    for nome in ("scriviFormattato", "inline"):
        corpo = _funzione(sorgente, nome)
        assert "innerHTML" not in corpo, f"{nome} costruisce HTML"

    ctx = _formattatore()
    ctx.eval("var n = nuovoNodo(); scriviFormattato(n, '<img src=x onerror=alert(1)>');")
    assert ctx.eval("n.figli.length") == 1
    assert ctx.eval("n.figli[0].tag") == "#text"


def test_gli_elenchi_prendono_il_punto():
    ctx = _formattatore()
    ctx.eval("var n = nuovoNodo(); scriviFormattato(n, '- uno\\n- due');")
    assert ctx.eval("n.textContent") == "• uno\n• due"
