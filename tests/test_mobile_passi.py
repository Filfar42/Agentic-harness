"""I passi del turno sull'interfaccia mobile: cosa si vede mentre l'agente lavora.

Il vincolo, dal telefono: si deve capire **che sta facendo**, non cosa ha
letto. Quindi in pagina entrano il verbo e il suo oggetto ("legge tools.py"),
mai il contenuto -- ne' il pensiero, ne' il risultato di un tool -- e quando
la risposta arriva le righe si richiudono in una tendina sola.

Le funzioni pure di ``web_mobile/app.js`` vengono estratte dal sorgente ed
eseguite in QuickJS: sono le stesse che disegnano la diretta e la cronologia,
quindi provarle qui vale per tutti e due i percorsi.

Serve ``quickjs`` (``pip install quickjs``): senza, i test saltano.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

quickjs = pytest.importorskip("quickjs")

WEB_MOBILE = Path(__file__).resolve().parents[1] / "web_mobile"
APP_JS = WEB_MOBILE / "app.js"


def _funzione(sorgente: str, nome: str) -> str:
    trovata = re.search(rf"function {nome}\([^)]*\) \{{.*?\n\}}", sorgente, re.DOTALL)
    assert trovata, f"funzione {nome} non trovata in {APP_JS.name}"
    return trovata.group(0)


def _costante(sorgente: str, nome: str) -> str:
    trovata = re.search(rf"const {nome} = \{{.*?\n\}};", sorgente, re.DOTALL)
    assert trovata, f"costante {nome} non trovata in {APP_JS.name}"
    return trovata.group(0)


@pytest.fixture()
def js() -> quickjs.Context:
    sorgente = APP_JS.read_text(encoding="utf-8")
    pezzi = [
        _funzione(sorgente, "troncaTesto"),
        _funzione(sorgente, "nomeFile"),
        _costante(sorgente, "VERBI_TOOL"),
        _funzione(sorgente, "descriviTool"),
        _funzione(sorgente, "toolAndatoBene"),
        _funzione(sorgente, "durataBreve"),
        _funzione(sorgente, "rispostaData"),
    ]
    ctx = quickjs.Context()
    ctx.eval("\n".join(pezzi))
    return ctx


# --------------------------------------------------- il verbo, non il JSON


@pytest.mark.parametrize(
    ("nome", "args", "atteso"),
    [
        ("read_file", {"filepath": "core/tools.py"}, "legge tools.py"),
        ("write_file", {"filepath": "web/app.js"}, "scrive app.js"),
        ("edit_file", {"filepath": "a/b/c/config.py"}, "modifica config.py"),
        ("list_files", {"subfolder": "."}, "elenca il workspace"),
        ("list_files", {}, "elenca il workspace"),
        ("run_command", {"command": "pytest -q"}, "esegue pytest -q"),
        ("web_search", {"query": "ollama repeat_penalty"}, 'cerca sul web "ollama repeat_penalty"'),
        ("web_search", {"query": "come si configura il repeat penalty in ollama"},
         'cerca sul web "come si configura il…"'),
        ("manage_plan", {"action": "complete"}, "piano: complete"),
        ("esplora", {"compito": "dove sta X"}, "manda un esploratore"),
    ],
)
def test_ogni_tool_diventa_un_verbo_leggibile(js, nome, args, atteso) -> None:
    """`read_file {"filepath": "core/tools.py"}` non si legge in mezzo secondo,
    "legge tools.py" si'. Del percorso resta il nome del file, come per le
    gocce del desktop: *cosa*, non *dove*."""
    import json as _json

    assert js.eval(f"descriviTool({_json.dumps(nome)}, {_json.dumps(args)})") == atteso


def test_un_tool_sconosciuto_non_fa_saltare_la_riga(js) -> None:
    """Un tool aggiunto domani e dimenticato qui deve degradare al suo nome,
    non far esplodere il disegno di tutta la conversazione."""
    assert js.eval('descriviTool("tool_di_domani", {})') == "tool_di_domani"
    assert js.eval("descriviTool(undefined, undefined)") == "strumento"


def test_gli_argomenti_lunghi_si_accorciano(js) -> None:
    """Le righe sono a riga singola: un comando di 300 caratteri le farebbe
    scorrere in orizzontale, che sul telefono vuol dire non leggerle."""
    lungo = "python -m pytest tests -q --maxfail=1 -x --tb=long -p no:cacheprovider"
    riga = js.eval(f'descriviTool("run_command", {{"command": "{lungo}"}})')
    assert len(riga) <= 46
    assert riga.endswith("…")


def test_il_contenuto_del_risultato_non_entra_mai_nella_riga(js) -> None:
    """Del risultato di un tool si tiene una cosa sola: se e' andato male."""
    assert js.eval('toolAndatoBene(JSON.stringify({status: "ok"}), undefined)') is True
    assert js.eval('toolAndatoBene(JSON.stringify({error: "manca il file"}), undefined)') is False
    # L'esito esplicito del server vince sul contenuto.
    assert js.eval('toolAndatoBene(JSON.stringify({status: "ok"}), false)') is False
    # Un risultato che non e' JSON non e' un fallimento: e' solo testo.
    assert js.eval('toolAndatoBene("output grezzo", undefined)') is True


def test_la_durata_si_legge_a_colpo_d_occhio(js) -> None:
    assert js.eval("durataBreve(7)") == "7s"
    assert js.eval("durataBreve(59)") == "59s"
    assert js.eval("durataBreve(60)") == "1m 0s"
    assert js.eval("durataBreve(135)") == "2m 15s"


# ------------------------------------------- invarianti del client mobile


def test_il_testo_in_diretta_si_legge_dal_campo_giusto() -> None:
    """La regressione per cui dal telefono non si capiva se stesse lavorando.

    ``ContentDelta`` porta ``text`` (cumulativo), come sa la UI desktop. Il
    client mobile leggeva ``data.delta``, che non esiste in nessun evento:
    concatenava ``undefined ?? ""`` ad ogni pezzo, quindi la bolla restava
    vuota e la risposta compariva tutta insieme a turno finito.
    """
    sorgente = APP_JS.read_text(encoding="utf-8")
    # Solo il codice: i commenti raccontano il bug, e nominarlo non e' farlo.
    codice = "\n".join(
        riga for riga in sorgente.splitlines() if not riga.lstrip().startswith(("//", "*", "/*"))
    )
    assert "data.text" in codice
    assert "data.delta" not in codice, "il campo dello stream e' 'text', non 'delta'"


def test_la_striscia_dell_attivita_c_e_ed_e_animata() -> None:
    """Tre pezzi che devono esistere insieme, o la striscia non si vede: il
    nodo nella pagina, la regola CSS e l'animazione che la distingue da una
    barra ferma."""
    html = (WEB_MOBILE / "index.html").read_text(encoding="utf-8")
    css = (WEB_MOBILE / "style.css").read_text(encoding="utf-8")
    for atteso in ('id="attivita"', 'id="attivita-testo"', 'id="attivita-tempo"'):
        assert atteso in html, atteso
    assert 'aria-live="polite"' in html, "chi non guarda lo schermo deve sentirlo"
    for regola in ("@keyframes scorre", "@keyframes battito", "@keyframes pulsa"):
        assert regola in css, regola
    # Chi ha chiesto meno animazioni al sistema operativo le ottiene.
    assert "prefers-reduced-motion" in css


def test_i_passi_sono_piu_piccoli_e_piu_chiari_del_testo() -> None:
    """La richiesta era esplicita: tonalita' piu' chiara e carattere piu'
    piccolo, perche' i passi sono note a margine, non contenuto."""
    css = (WEB_MOBILE / "style.css").read_text(encoding="utf-8")
    blocco = re.search(r"\n\.passo \{(.*?)\n\}", css, re.DOTALL)
    assert blocco, "manca la regola .passo"
    corpo = blocco.group(1)
    misura = re.search(r"font-size:\s*([\d.]+)px", corpo)
    assert misura and float(misura.group(1)) < 15, "i passi devono essere piu' piccoli del testo"
    assert "var(--testo-dim)" in corpo, "i passi devono essere piu' chiari del testo"


# ------------------------------------------- la domanda a cui si e' risposto


def test_la_risposta_data_si_ricava_dal_risultato_del_tool(js) -> None:
    """Sul mobile la card della domanda spariva dopo la risposta e non
    restava traccia di niente: ne' di cosa era stato chiesto, ne' di cosa era
    stato scelto. Sul desktop invece lo scambio resta in chiaro."""
    import json as _json

    singola = _json.dumps({"user_answer": "TOML"})
    assert js.eval(f"rispostaData({_json.dumps(singola)})") == "TOML"

    multipla = _json.dumps({"user_answer": ["TOML", "JSON"]})
    assert js.eval(f"rispostaData({_json.dumps(multipla)})") == "TOML, JSON"

    # Formati che non si riconoscono: meglio niente che un JSON crudo in
    # mezzo alla conversazione.
    assert js.eval('rispostaData("non e\' json")') == ""
    assert js.eval('rispostaData(JSON.stringify({}))') == ""


def test_il_telefono_ascolta_il_bus_globale() -> None:
    """La sincronizzazione in diretta fra i due schermi.

    Prima il telefono si accorgeva di un turno partito dal desktop solo al
    giro di polling, e -- peggio -- appena ``running`` diventava vero il
    polling smetteva di rileggere la conversazione mentre nessuno stream era
    attaccato: restava ferma fino a un refresh a mano. Si vedeva soprattutto
    con ``ask_user_question``, dove il turno si chiude subito e la novita' e'
    tutta nella domanda in sospeso.
    """
    sorgente = APP_JS.read_text(encoding="utf-8")
    assert "function bindGlobalEvents()" in sorgente
    assert 'new EventSource("/api/events")' in sorgente
    # Non basta ascoltare: alla notizia bisogna anche riattaccarsi allo stream,
    # senno' si sa che un turno c'e' ma non lo si vede scorrere.
    assert "if (running && !source) attachStream();" in sorgente


# --------------------------------------- il sommario dei passi mentre lavora


@pytest.fixture()
def js_sommario() -> quickjs.Context:
    """La sola funzione sommarioPassi piu' le variabili che legge."""
    sorgente = APP_JS.read_text(encoding="utf-8")
    ctx = quickjs.Context()
    ctx.eval(_funzione(sorgente, "sommarioPassi"))
    ctx.eval(_funzione(sorgente, "durataBreve"))
    return ctx


def test_il_sommario_vivo_dice_passo_e_strumenti_del_passo(js_sommario) -> None:
    """Il testo era 'x passi | y strumenti' col totale del turno: a meta'
    lavoro quel numero non torna con le righe aperte sotto. Ora e' 'passo N
    · M strumenti' e M conta i tool del SOLO passo corrente."""
    import json as _json

    ctx = js_sommario
    ctx.eval("""
var sum = { textContent: "" };
gruppoPassi = { querySelector: function (sel) {
  return sel === ".passi-sommario" ? sum : null; } };
contaTool = 0; inizioTurno = null; contaSecondi = 0;
""")
    scenari = [(2, 3), (1, 1), (4, 0), (0, 0)]
    visti = []
    for passi, tool in scenari:
        ctx.eval(f"contaPassi = {passi}; contaToolPasso = {tool}; sum.textContent = '';")
        ctx.eval("sommarioPassi(false)")
        visti.append(ctx.eval("sum.textContent"))

    attesi = [
        "passo 2 · 3 strumenti",
        "passo 1 · 1 strumento",  # il singolare: uno strumento si vede
        "passo 4",                # passo senza tool: niente coda vuota
        "sta lavorando…",         # nessun passo ancora: resta generico
    ]
    assert visti == attesi


def test_il_sommario_chiuso_parla_per_l_intero_turno(js_sommario) -> None:
    """A turno finito la riga resta un riepilogo: totale passi, totale
    strumenti e durata. La modifica tocca solo la riga VIVA."""
    ctx = js_sommario
    ctx.eval("""
var sum = { textContent: "" };
gruppoPassi = { querySelector: function (sel) {
  return sel === ".passi-sommario" ? sum : null; } };
contaPassi = 5; contaTool = 9; contaToolPasso = 0;
inizioTurno = Date.now() - 95000; contaSecondi = 95;
sommarioPassi(true);
""")
    assert ctx.eval("sum.textContent").startswith("5 passi")
    assert "9 strumenti" in ctx.eval("sum.textContent")


def test_il_contatore_per_passo_si_resetta_al_giro_nuovo() -> None:
    """Se contaToolPasso non riparte da zero a ogni evento step, il numero
    cresce per sempre ed e' di nuovo il totale del turno -- il bug di prima."""
    sorgente = APP_JS.read_text(encoding="utf-8")
    codice = "\n".join(
        riga for riga in sorgente.splitlines() if not riga.lstrip().startswith("//")
    )
    # Nasce, si azzera allo start del turno...
    assert "let contaToolPasso = 0;" in codice
    # ...si azzera a ogni nuovo passo (il caso 'step' dell'event stream)...
    assert re.search(r'case\s*"step":[\s\S]{0,200}contaToolPasso = 0', codice)
    # ...e cresce solo quando parte davvero un tool.
    assert re.search(r'case\s*"tool_start":\s*\{[\s\S]{0,120}contaToolPasso \+= 1', codice)


# ------------------------------------- modello e workspace nella barra mobile


def test_la_barra_superiore_mostra_modello_e_workspace_sola_lettura() -> None:
    """Nella topbar della chat devono stare modello e workspace: informativi,
    non pulsanti -- niente cursor:pointer, niente onclick nel cablaggio."""
    html = (WEB_MOBILE / "index.html").read_text(encoding="utf-8")
    for pezzo in ('class="chat-meta"', 'id="chat-model"', 'id="chat-workspace"'):
        assert pezzo in html, pezzo

    css = (WEB_MOBILE / "style.css").read_text(encoding="utf-8")
    assert ".chat-meta" in css

    app_js = (Path(__file__).resolve().parents[1] / "web_mobile" / "app.js").read_text(
        encoding="utf-8"
    )
    # Popolati dal payload della sessione, mai modificabili dall'utente.
    assert '$("chat-model")' in app_js
    assert '$("chat-workspace")' in app_js
    blocco_meta = app_js.split('$("chat-model")', 1)[1][:400]
    assert "textContent" in blocco_meta
    assert "onclick" not in blocco_meta


def test_il_payload_della_sessione_porta_modello_e_workspace_di_chat() -> None:
    """I due valori arrivano da open_payload: il modello e' quello selezionato
    ora, il workspace e' quello DELLA CHAT (workspace_di), non il globale."""
    main_py = (
        Path(__file__).resolve().parents[1] / "server" / "main.py"
    ).read_text(encoding="utf-8")
    inizio = main_py.index("def open_payload")
    fine = main_py.index("\ndef ", inizio + 10)
    corpo = main_py[inizio:fine]
    assert '"model"' in corpo
    assert '"session_workspace"' in corpo
    assert "workspace_di(" in corpo, "il workspace deve essere quello della chat"
