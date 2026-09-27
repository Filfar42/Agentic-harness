"""I passi del lavoro dell'agente in chat: web/passi.js, eseguito in QuickJS.

Il modulo e' condiviso da desktop e telefono, e ha due strati: le funzioni
pure che dal risultato di un tool ricavano cosa si vede (la riga, il
dettaglio, il riassunto del blocco), e la regia dei blocchi di lavoro
(``Passi.lavoro``) che decide dove finisce un passo e quando un blocco si
chiude. Il primo strato si prova con i dati; il secondo con un DOM minimo,
quanto basta a contare nodi e a cliccare -- la resa grafica la guarda il
browser, non questa suite.

Serve ``quickjs`` (e' nelle dipendenze di sviluppo): senza, i test saltano.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

quickjs = pytest.importorskip("quickjs")

WEB = Path(__file__).resolve().parents[1] / "web"
PASSI_JS = WEB / "passi.js"

# Un DOM minimo: elementi, testo, classi, attributi, eventi di clic, e una
# querySelector che capisce i selettori di classe composti (".a.b"). Nient'altro
# di quello che passi.js usa esiste qui, ed e' voluto: se il modulo cominciasse
# a dipendere da altro, questi test lo direbbero.
DOM = r"""
class Nodo {
  constructor(tag, tipo) {
    this.tagName = String(tag).toUpperCase();
    this.nodeType = tipo || 1;
    this.childNodes = [];
    this.parentNode = null;
    this.dataset = {};
    this.style = {};
    this.attributi = {};
    this.ascoltatori = {};
    this.hidden = false;
    this._classi = [];
    this._testo = '';
    this._html = '';
    const self = this;
    this.classList = {
      add(c) { if (!self._classi.includes(c)) self._classi.push(c); },
      remove(c) { self._classi = self._classi.filter((x) => x !== c); },
      contains(c) { return self._classi.includes(c); },
      toggle(c, forza) {
        const si = forza === undefined ? !self._classi.includes(c) : Boolean(forza);
        if (si) this.add(c); else this.remove(c);
        return si;
      },
    };
  }
  get className() { return this._classi.join(' '); }
  set className(v) { this._classi = String(v || '').split(/\s+/).filter(Boolean); }
  get textContent() {
    if (this.nodeType === 3) return this._testo;
    return this.childNodes.map((n) => n.textContent).join('');
  }
  set textContent(v) {
    if (this.nodeType === 3) { this._testo = String(v); return; }
    this.childNodes.forEach((n) => { n.parentNode = null; });
    this.childNodes = [];
    this._html = '';
    if (v !== '' && v != null) this.appendChild(documento.createTextNode(String(v)));
  }
  set innerHTML(v) { this.textContent = ''; this._html = String(v); }
  get innerHTML() { return this._html; }
  get isConnected() {
    let n = this;
    while (n.parentNode) n = n.parentNode;
    return n === documento.body;
  }
  appendChild(n) {
    if (n.parentNode) n.remove();
    this.childNodes.push(n);
    n.parentNode = this;
    return n;
  }
  append(...nodi) { nodi.forEach((n) => this.appendChild(n)); }
  remove() {
    if (!this.parentNode) return;
    const fratelli = this.parentNode.childNodes;
    fratelli.splice(fratelli.indexOf(this), 1);
    this.parentNode = null;
  }
  setAttribute(k, v) { this.attributi[k] = String(v); }
  getAttribute(k) { return k in this.attributi ? this.attributi[k] : null; }
  addEventListener(tipo, fn) { (this.ascoltatori[tipo] = this.ascoltatori[tipo] || []).push(fn); }
  click() { (this.ascoltatori.click || []).forEach((fn) => fn({})); }
  _combacia(sel) {
    const classi = sel.split('.').filter(Boolean);
    return this.nodeType === 1 && classi.every((c) => this._classi.includes(c));
  }
  querySelectorAll(sel) {
    const trovati = [];
    const visita = (n) => {
      for (const f of n.childNodes) {
        if (f._combacia(sel)) trovati.push(f);
        visita(f);
      }
    };
    visita(this);
    return trovati;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}
var documento = {
  createElement: (tag) => new Nodo(tag, 1),
  createTextNode: (t) => { const n = new Nodo('#text', 3); n._testo = String(t); return n; },
};
documento.body = new Nodo('body', 1);
var intervalli = [];
var window = {
  document: documento,
  setInterval: (fn) => { intervalli.push(fn); return intervalli.length; },
  clearInterval: () => {},
};
var setInterval = window.setInterval;
var clearInterval = window.clearInterval;
"""


@pytest.fixture()
def js() -> quickjs.Context:
    ctx = quickjs.Context()
    ctx.eval(DOM)
    ctx.eval(PASSI_JS.read_text(encoding="utf-8"))
    ctx.eval("var Passi = window.Passi;")
    return ctx


def valuta(ctx: quickjs.Context, espressione: str):
    """Il valore di un'espressione, passato da JSON: oggetti e liste inclusi."""
    return json.loads(ctx.eval(f"JSON.stringify({espressione})"))


def riga(ctx, nome, args, risultato=None, ok=True, durata=0):
    risultato_js = "undefined" if risultato is None else json.dumps(json.dumps(risultato))
    return valuta(ctx, f"Passi.riga({json.dumps(nome)}, {json.dumps(args)}, {risultato_js}, "
                       f"{json.dumps(ok)}, {json.dumps(durata)})")


# ---------------------------------------------------------------------------
# La riga: verbo e oggetto, e del risultato solo quello che vale la riga
# ---------------------------------------------------------------------------


def test_la_riga_dice_il_verbo_e_il_file_non_il_json(js) -> None:
    d = riga(js, "read_file", {"filepath": "core/tools.py"},
             {"filepath": "core/tools.py", "total_lines": 19, "range": "intero file", "content": "x\ny"})
    assert (d["verbo"], d["oggetto"], d["titolo"]) == ("Legge", "tools.py", "core/tools.py")
    assert d["meta"] == "19 righe"
    # Il contenuto del file non entra mai nella riga.
    assert all("x\ny" not in str(v) for v in d.values())


def test_la_riga_di_una_lettura_parziale_dice_quali_righe(js) -> None:
    d = riga(js, "read_file", {"filepath": "a.py"}, {"range": "1200-1221", "total_lines": 3914})
    assert d["meta"] == "righe 1200\u20131221"
    d = riga(js, "read_file", {"filepath": "a.py"}, {"status": "invariato"})
    assert d["meta"] == "già letto"


def test_un_comando_dice_com_e_finito(js) -> None:
    ok = riga(js, "run_command", {"command": "pytest -q"},
              {"esito": "ok", "returncode": 0, "duration_s": 3.08}, True, 3.08)
    assert (ok["meta"], ok["tono"]) == ("ok · 3,1 s", "ok")
    rosso = riga(js, "run_command", {"command": "pytest -q"},
                 {"esito": "FALLITO", "returncode": 1, "duration_s": 3.42}, False, 3.42)
    assert (rosso["meta"], rosso["tono"]) == ("exit 1 · 3,4 s", "err")


def test_una_modifica_conta_le_righe_tolte_e_aggiunte(js) -> None:
    args = {"filepath": "core/agent.py",
            "old_string": "    def invia(self):\n        if self._spedito is None:\n            x = 1",
            "new_string": "    def invia(self):\n        if self._primo:\n            self._primo = False\n            x = 1"}
    d = riga(js, "edit_file", args, {"status": "ok", "replacements": 1})
    assert (d["piu"], d["meno"]) == ("+2", "\u22121")
    # Con replace_all le stesse righe cambiano in ogni occorrenza.
    d = riga(js, "edit_file", args, {"status": "ok", "replacements": 3})
    assert (d["piu"], d["meno"]) == ("+6", "\u22123")


def test_un_errore_del_tool_si_vede_sulla_riga(js) -> None:
    d = riga(js, "edit_file", {"filepath": "a.py", "old_string": "x", "new_string": "y"},
             {"error": "'old_string' non trovato in 'a.py'.", "hint": "rileggi"})
    assert (d["meta"], d["tono"]) == ("errore", "err")
    assert "non trovato" in d["titolo"]
    # L'esito esplicito del server vince sul contenuto.
    assert riga(js, "search_files", {"pattern": "x"}, {"matches": []}, False)["tono"] == "err"
    # Un risultato che non e' JSON non e' un fallimento: e' solo testo.
    d = valuta(js, 'Passi.riga("tool_di_domani", {}, "output grezzo", true, 0)')
    assert d["tono"] == ""


def test_un_tool_in_corso_ha_solo_il_verbo(js) -> None:
    d = riga(js, "run_command", {"command": "pytest -q"})
    assert (d["verbo"], d["oggetto"], d["meta"], d["tono"]) == ("Esegue", "pytest -q", "", "")


def test_un_tool_sconosciuto_non_fa_saltare_la_riga(js) -> None:
    d = valuta(js, 'Passi.riga("tool_di_domani", {"a": 1}, "{}", true, 0)')
    assert d["verbo"] == "tool_di_domani"
    assert valuta(js, "Passi.riga(undefined, undefined, undefined, true, 0)")["verbo"] == "strumento"


@pytest.mark.parametrize(
    ("secondi", "atteso"),
    [(0.04, ""), (0.4, "0,4 s"), (3.42, "3,4 s"), (41, "41 s"), (125, "2 min 5 s"), (-1, "")],
)
def test_la_durata_si_legge_all_italiana(js, secondi, atteso) -> None:
    """Sotto il decimo di secondo non dice niente: "0.01s" su ogni riga era
    rumore."""
    assert js.eval(f"Passi.durata({secondi})") == atteso


# ---------------------------------------------------------------------------
# Il dettaglio, letto per tipo
# ---------------------------------------------------------------------------


def vista(ctx, nome, args, risultato):
    return valuta(ctx, f"Passi.vista({json.dumps(nome)}, {json.dumps(args)}, "
                       f"{json.dumps(json.dumps(risultato))})")


def test_una_lettura_si_apre_con_i_numeri_di_riga_veri(js) -> None:
    v = vista(js, "read_file", {"filepath": "a.py"},
              {"filepath": "a.py", "range": "1200-1202", "total_lines": 3914, "content": "a\nb\nc"})
    assert v["tipo"] == "estratto"
    assert [(r["n"], r["t"]) for r in v["righe"]] == [("1200", "a"), ("1201", "b"), ("1202", "c")]
    assert "righe 1200\u20131202" in v["testa"]


def test_dopo_il_taglio_del_tool_i_numeri_si_fermano(js) -> None:
    """Dopo il segno di smart_truncate le righe non sono piu' quelle del file:
    numerarle sarebbe dire una riga sbagliata."""
    contenuto = "uno\ndue\n\n[... contenuto di a.py: omessi 900 caratteri (~30 righe) dal centro. Usa read_file ...]\n\ncoda"
    v = vista(js, "read_file", {"filepath": "a.py"},
              {"filepath": "a.py", "range": "intero file", "truncated": True, "content": contenuto})
    numeri = [r["n"] for r in v["righe"]]
    assert numeri[:2] == ["1", "2"]
    assert numeri[-1] == ""
    assert any(r.get("nota") for r in v["righe"])


def test_una_modifica_si_apre_come_diff_numerato(js) -> None:
    args = {"filepath": "core/agent.py",
            "old_string": "        if self._spedito is None:\n            self._spedito = testo",
            "new_string": "        if self._primo:\n            self._primo = False\n            self._spedito = testo"}
    risultato = {"status": "ok", "filepath": "core/agent.py", "replacements": 1,
                 "dopo_la_modifica": {"righe": "1211-1216", "testo": (
                     "1211 |     def invia(self, testo):\n"
                     "1213 |         if self._primo:\n"
                     "1214 |             self._primo = False\n"
                     "1215 |             self._spedito = testo")}}
    v = vista(js, "edit_file", args, risultato)
    assert v["tipo"] == "diff"
    assert [(r["segno"], r["n"]) for r in v["righe"]] == [("-", ""), ("+", "1213"), ("+", "1214"), (" ", "1215")]


def test_una_ricerca_si_apre_raggruppata_per_file(js) -> None:
    v = vista(js, "search_files", {"pattern": "_spedito"},
              {"pattern": "_spedito", "matches": [
                  "core/agent.py:1204:        self._spedito = \"\"",
                  "core/agent.py:1213:>        if self._spedito is None:",
                  "(nessuna corrispondenza)"]})
    assert v["tipo"] == "ricerca"
    assert [(r["file"], r["n"]) for r in v["righe"][:2]] == [("core/agent.py", "1204"), ("core/agent.py", "1213")]


def test_un_comando_si_apre_come_terminale(js) -> None:
    v = vista(js, "run_command", {"command": "pytest -q"},
              {"command": "pytest -q", "returncode": 1, "stdout": "F.\n1 failed\n\n", "stderr": "boom"})
    assert (v["tipo"], v["codice"], v["ok"]) == ("terminale", "exit 1", False)
    assert v["uscita"].startswith("F.\n1 failed")
    assert v["uscita"].endswith("--- stderr ---\nboom")


def test_un_errore_si_apre_col_suggerimento_una_volta_sola(js) -> None:
    v = vista(js, "edit_file", {"filepath": "a.py"}, {"error": "non trovato", "hint": "copia da piu_vicino"})
    assert (v["tipo"], v["testo"], v["suggerimento"]) == ("errore", "non trovato", "copia da piu_vicino")
    assert not v.get("nota")


def test_un_tool_senza_vista_sua_si_legge_a_campi(js) -> None:
    v = vista(js, "manage_plan", {"action": "complete", "step_id": 2}, {"plan": ["a"], "nota": "riga 1\nriga 2"})
    assert v["tipo"] == "campi"
    campi = {r["k"]: r["v"] for r in v["righe"]}
    # Le stringhe restano stringhe: niente \n letterali come nel JSON.
    assert campi["nota"] == "riga 1\nriga 2"
    assert campi["action"] == "complete"


# ---------------------------------------------------------------------------
# Il riassunto di un blocco
# ---------------------------------------------------------------------------


def test_il_riassunto_conta_per_tipo_e_dice_l_ultimo_comando(js) -> None:
    passi = [
        {"nome": "read_file", "args": {"filepath": "a"}, "risultato": "{}", "ok": True},
        {"nome": "read_file", "args": {"filepath": "b"}, "risultato": "{}", "ok": True},
        {"nome": "search_files", "args": {"pattern": "x"}, "risultato": "{}", "ok": True},
        {"nome": "edit_file", "args": {"filepath": "a"}, "risultato": json.dumps({"error": "no"}), "ok": False},
        {"nome": "run_command", "args": {"command": "t"}, "risultato": json.dumps({"returncode": 1}), "ok": False},
        {"nome": "run_command", "args": {"command": "t"}, "risultato": json.dumps({"returncode": 0}), "ok": True},
        {"nome": "manage_plan", "args": {"action": "complete"}, "risultato": "{}", "ok": True},
    ]
    r = valuta(js, f"Passi.riassunto({json.dumps(passi)})")
    assert r["testo"] == "2 letture · 1 ricerca · 1 modifica · 2 comandi · 1 altra azione"
    assert (r["comando"], r["errori"]) == ("ok", 1)


# ---------------------------------------------------------------------------
# La frase della striscia del telefono
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("nome", "args", "atteso"),
    [
        ("read_file", {"filepath": "core/tools.py"}, "legge tools.py"),
        ("edit_file", {"filepath": "a/b/c/config.py"}, "modifica config.py"),
        ("list_files", {}, "elenca il workspace"),
        ("run_command", {"command": "pytest -q"}, "esegue pytest -q"),
        ("manage_plan", {"action": "complete"}, "piano: complete"),
    ],
)
def test_la_frase_breve_e_quella_del_telefono(js, nome, args, atteso) -> None:
    assert js.eval(f"Passi.frase({json.dumps(nome)}, {json.dumps(args)})") == atteso


def test_l_estratto_del_pensiero_e_la_prima_frase(js) -> None:
    testo = "Ecco il punto. In __init__ c'e' la stringa vuota, e il resto e' lungo."
    assert js.eval(f"Passi.estratto({json.dumps(testo)})") == "Ecco il punto. In __init__ c'e' la stringa vuota, e il resto e' lungo."
    lungo = "parola " * 80
    assert len(js.eval(f"Passi.estratto({json.dumps(lungo)})")) <= 160


# ---------------------------------------------------------------------------
# Nodi: il dettaglio nasce al primo clic
# ---------------------------------------------------------------------------


def test_il_dettaglio_di_un_tool_si_costruisce_al_primo_clic(js) -> None:
    """Un write_file da 34 kB non entra nel DOM per non essere guardato: la
    regola della tendina del desktop dalla v2.36, portata sulle righe."""
    js.eval("""
      var p = Passi.passoTool({nome: 'write_file', args: {filepath: 'a.py', content: 'x\\n'.repeat(5000)},
        risultato: JSON.stringify({status: 'ok', action: 'creato', lines: 5000}), ok: true, durata: 0.01});
      documento.body.appendChild(p);
    """)
    assert js.eval("p.querySelectorAll('ps-corpo').length + p.querySelectorAll('.ps-corpo').length") == 0
    js.eval("p.querySelector('.ps-riga').click()")
    assert js.eval("p.querySelectorAll('.ps-corpo').length") == 1
    assert js.eval("p.querySelector('.ps-riga').getAttribute('aria-expanded')") == "true"
    # Al massimo MAX_RIGHE righe: il resto sta nel JSON grezzo.
    assert js.eval("p.querySelectorAll('.ps-ln').length") == 400
    js.eval("p.querySelector('.ps-riga').click()")
    assert js.eval("p.querySelector('.ps-corpo').hidden") is True
    assert js.eval("p.querySelectorAll('.ps-corpo').length") == 1


def test_un_tool_in_corso_non_si_apre_e_poi_si_completa(js) -> None:
    js.eval("""
      var p = Passi.passoTool({nome: 'run_command', args: {command: 'pytest -q'}, id: 'c1'});
      p.querySelector('.ps-riga').click();
    """)
    assert js.eval("p.classList.contains('corso')") is True
    assert js.eval("p.querySelectorAll('.ps-corpo').length") == 0
    js.eval("""Passi.concludiTool(p, {risultato: JSON.stringify({returncode: 0, duration_s: 2.5}),
                                     ok: true, durata: 2.5});""")
    assert js.eval("p.classList.contains('corso')") is False
    assert "ok · 2,5 s" in js.eval("p.querySelector('.ps-meta').textContent")


# ---------------------------------------------------------------------------
# La regia dei blocchi
# ---------------------------------------------------------------------------


@pytest.fixture()
def lav(js) -> quickjs.Context:
    js.eval("""
      var box = documento.createElement('div');
      documento.body.appendChild(box);
      var lav = Passi.lavoro({ inserisci: (n) => box.appendChild(n) });
      function gruppi() { return box.querySelectorAll('.ps-gruppo'); }
      function testa(i) { return gruppi()[i].querySelector('.ps-testa').textContent; }
      function tool(nome, id, esito, secondi) {
        lav.avviaTool({nome, args: {command: 'pytest -q', filepath: 'a.py'}, id});
        lav.concludiTool({nome, args: {command: 'pytest -q', filepath: 'a.py'}, id,
          risultato: JSON.stringify(esito), ok: esito.returncode ? false : true, durata: secondi || 0});
      }
    """)
    return js


def test_pensiero_e_tool_vanno_nello_stesso_blocco(lav) -> None:
    lav.eval("""
      lav.ora(1000); lav.segnaPasso(1);
      lav.ora(1001); lav.appendThinking('Leggo il test. ');
      lav.ora(1004); tool('read_file', 'c1', {total_lines: 19});
      lav.ora(1005); lav.appendThinking('Rilancio.');
      lav.ora(1009); tool('run_command', 'c2', {returncode: 0, duration_s: 3.1}, 3.1);
      lav.ora(1010); lav.appendThinking('Verde, rispondo.');
      lav.ora(1041); lav.chiudi();          // la risposta comincia
    """)
    assert lav.eval("gruppi().length") == 1
    t = lav.eval("testa(0)")
    assert t.startswith("Ha lavorato 41 s")
    assert "1 lettura · 1 comando" in t
    assert "ultimo comando ok" in t
    # Cinque passi: tre pensieri e due tool.
    assert lav.eval("gruppi()[0].querySelectorAll('.ps-passo').length") == 5
    # Il pensiero chiuso dice quanto e' durato, sull'orologio del server.
    assert lav.eval("gruppi()[0].querySelector('.ps-pensiero').querySelector('.ps-meta').textContent") == "3,0 s"


def test_il_pensiero_rimandato_a_fine_passo_non_apre_un_blocco_nuovo(lav) -> None:
    """A fine passo il server rimanda il pensiero intero (core/agent.py, "Il
    testo completo, una volta per passo"), anche dopo che la risposta ha gia'
    chiuso il blocco. Andava al pensiero di quel passo; aprirne uno nuovo
    avrebbe messo un blocco vuoto sotto la risposta."""
    lav.eval("""
      lav.segnaPasso(1);
      lav.appendThinking('Ho finito. ');
      lav.chiudi();                                   // la risposta comincia
      lav.setThinking('Ho finito. Rispondo con il riassunto.');  // il rimando
    """)
    assert lav.eval("gruppi().length") == 1
    assert "Ho finito." in lav.eval("gruppi()[0].querySelector('.ps-estratto').textContent")
    # Il passo dopo invece apre un blocco nuovo.
    lav.eval("lav.segnaPasso(2); lav.appendThinking('Altro lavoro.');")
    assert lav.eval("gruppi().length") == 2


def test_un_testo_del_modello_spezza_il_blocco(lav) -> None:
    lav.eval("""
      lav.segnaPasso(1); tool('read_file', 'c1', {total_lines: 3});
      lav.chiudi();                        // "Ora lancio i test."
      tool('run_command', 'c2', {returncode: 1}, 1.5);
      lav.chiudi();
    """)
    assert lav.eval("gruppi().length") == 2
    assert "ultimo comando fallito" in lav.eval("testa(1)")


def test_il_blocco_vivo_piega_i_passi_vecchi(lav) -> None:
    lav.eval("""
      lav.segnaPasso(1);
      for (let i = 0; i < 7; i++) tool('read_file', 'c' + i, {total_lines: 1});
    """)
    assert lav.eval("box.querySelector('.ps-piega').hidden") is False
    assert "3 passi prima" in lav.eval("box.querySelector('.ps-piega').textContent")
    assert lav.eval("gruppi()[0].querySelectorAll('.ps-passo').filter((n) => !n.hidden).length") == 4
    lav.eval("box.querySelector('.ps-piega').click()")
    assert lav.eval("gruppi()[0].querySelectorAll('.ps-passo').filter((n) => !n.hidden).length") == 7
    # Chiuso, il blocco e' una riga sola che si apre sulla traccia intera.
    lav.eval("lav.chiudi(); gruppi()[0].querySelector('.ps-testa').click();")
    assert lav.eval("gruppi()[0].querySelector('.ps-traccia').hidden") is False
    assert lav.eval("gruppi()[0].querySelectorAll('.ps-piega').length") == 0


def test_la_cronologia_misura_con_i_ts_dei_messaggi(lav) -> None:
    """Ridisegnando una chat salvata non c'e' un orologio che scorre: i tempi
    sono i ``ts`` dei messaggi. Il blocco comincia dal messaggio prima."""
    lav.eval("""
      lav.pensieroSalvato('Leggo il file.', 100, 104);
      lav.concludiTool({nome: 'read_file', args: {filepath: 'a.py'}, risultato: '{}', ok: true, durata: 0.01},
                       {inizio: 104, fine: 104.5});
      lav.pensieroSalvato('Fatto.', 104.5, 110);
      lav.chiudi();
    """)
    assert lav.eval("testa(0)").startswith("Ha lavorato 10 s")
    metas = lav.eval("gruppi()[0].querySelectorAll('.ps-pensiero').map((p) => p.querySelector('.ps-meta').textContent).join('|')")
    assert metas == "4,0 s|5,5 s"


def test_senza_ore_il_blocco_non_inventa_durate(lav) -> None:
    lav.eval("""
      lav.concludiTool({nome: 'read_file', args: {filepath: 'a.py'}, risultato: '{}', ok: true}, {});
      lav.chiudi();
    """)
    t = lav.eval("testa(0)")
    assert t.startswith("Ha lavorato")
    assert " s " not in t.split("·")[0] + " "


def test_un_blocco_di_soli_pensieri_dice_ha_pensato(lav) -> None:
    lav.eval("lav.pensieroSalvato('Rispondo.', 10, 15); lav.chiudi();")
    assert lav.eval("testa(0)").startswith("Ha pensato 5,0 s")


# ---------------------------------------------------------------------------
# La pagina del desktop
# ---------------------------------------------------------------------------


def test_il_desktop_carica_i_passi_prima_di_app_js_con_l_impronta() -> None:
    radice = WEB.parent
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert html.index("/static/passi.js") < html.index("/static/app.js")
    assert "/static/passi.css" in html
    main = (radice / "server" / "main.py").read_text(encoding="utf-8")
    elenco = main[main.index('for nome in ("app.js"'):]
    elenco = elenco[: elenco.index("):")]
    for nome in ("passi.js", "passi.css"):
        assert nome in elenco, nome


def test_le_vecchie_tendine_dei_tool_non_ci_sono_piu() -> None:
    """Tool e pensieri passano tutti dai blocchi: nessuna strada laterale che
    ridisegni la tendina di prima (e il suo JSON) per un caso dimenticato."""
    app = (WEB / "app.js").read_text(encoding="utf-8")
    for vecchio in ("function toolDrawer(", "TOOL_ICON", "function argPreview(", "'.think.live'"):
        assert vecchio not in app, vecchio
    css = (WEB / "style.css").read_text(encoding="utf-8")
    for vecchio in (".tool-name", ".tool-json", "\n.think {"):
        assert vecchio not in css, vecchio
