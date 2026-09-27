"""I passi del turno sull'interfaccia mobile: cosa si vede mentre l'agente lavora.

Il vincolo, dal telefono: si deve capire **che sta facendo** a colpo
d'occhio. Nella riga entrano il verbo e il suo oggetto ("Legge tools.py"),
mai il contenuto; il contenuto -- il risultato di un tool, il pensiero
intero -- entra in pagina solo al tocco sulla riga, e dal vivo del pensiero
si vedono le ultime quattro righe. Le righe e i blocchi sono quelli del
desktop (web/passi.js, provato in tests/test_passi_web.py): qui si prova il
cablaggio del telefono.

Le funzioni pure di ``web_mobile/app.js`` vengono estratte dal sorgente ed
eseguite in QuickJS, insieme a web/passi.js che la pagina carica prima.

Serve ``quickjs`` (``pip install quickjs``): senza, i test saltano.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

quickjs = pytest.importorskip("quickjs")

WEB_MOBILE = Path(__file__).resolve().parents[1] / "web_mobile"
APP_JS = WEB_MOBILE / "app.js"
PASSI_JS = Path(__file__).resolve().parents[1] / "web" / "passi.js"


def _funzione(sorgente: str, nome: str) -> str:
    trovata = re.search(rf"function {nome}\([^)]*\) \{{.*?\n\}}", sorgente, re.DOTALL)
    assert trovata, f"funzione {nome} non trovata in {APP_JS.name}"
    return trovata.group(0)


@pytest.fixture()
def js() -> quickjs.Context:
    sorgente = APP_JS.read_text(encoding="utf-8")
    pezzi = [
        _funzione(sorgente, "descriviTool"),
        _funzione(sorgente, "durataBreve"),
        _funzione(sorgente, "rispostaData"),
    ]
    ctx = quickjs.Context()
    # La pagina carica /comune/passi.js prima di app.js (index.html).
    ctx.eval(PASSI_JS.read_text(encoding="utf-8"))
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
    """Del risultato di un tool la riga tiene poco: quante righe, com'e'
    finito, se e' andato male. Il contenuto no -- quello si apre al tocco."""
    riga = js.eval(
        'JSON.stringify(Passi.riga("read_file", {filepath: "a.py"}, '
        'JSON.stringify({total_lines: 2, content: "SEGRETO\\nALTRO"}), true, 0))'
    )
    assert "SEGRETO" not in riga
    assert js.eval('Passi.riga("read_file", {}, JSON.stringify({error: "manca il file"}), undefined, 0).tono') == "err"
    # L'esito esplicito del server vince sul contenuto.
    assert js.eval('Passi.riga("read_file", {}, JSON.stringify({status: "ok"}), false, 0).tono') == "err"
    # Un risultato che non e' JSON non e' un fallimento: e' solo testo.
    assert js.eval('Passi.riga("read_file", {}, "output grezzo", undefined, 0).tono') == ""


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
    for regola in ("@keyframes scorre", "@keyframes battito"):
        assert regola in css, regola
    # Il passo in corso gira: la rotellina del tool e il pensiero vivo stanno
    # nelle righe condivise (web/passi.css).
    comune = (PASSI_JS.parent / "passi.css").read_text(encoding="utf-8")
    assert "@keyframes ps-gira" in comune
    assert "prefers-reduced-motion" in comune
    # Chi ha chiesto meno animazioni al sistema operativo le ottiene.
    assert "prefers-reduced-motion" in css


def test_i_passi_sono_piu_piccoli_e_piu_chiari_del_testo() -> None:
    """La richiesta era esplicita: tonalita' piu' chiara e carattere piu'
    piccolo, perche' i passi sono note a margine, non contenuto. Le righe sono
    di web/passi.css; il telefono ne sceglie misura e colori."""
    css = (WEB_MOBILE / "style.css").read_text(encoding="utf-8")
    misura = re.search(r"--ps-font:\s*([\d.]+)px", css)
    assert misura and float(misura.group(1)) < 15, "i passi devono essere piu' piccoli del testo"
    assert re.search(r"--ps-medio:\s*var\(--testo-dim\)", css), "i passi devono essere piu' chiari del testo"
    # Righe che si toccano col dito.
    altezza = re.search(r"--ps-riga-h:\s*(\d+)px", css)
    assert altezza and int(altezza.group(1)) >= 40
    comune = (PASSI_JS.parent / "passi.css").read_text(encoding="utf-8")
    riga = re.search(r"\n\.ps-riga, \.ps-piega \{(.*?)\n\}", comune, re.DOTALL)
    assert riga and "var(--ps-medio)" in riga.group(1)


def test_la_pagina_carica_i_passi_condivisi_prima_dell_app() -> None:
    html = (WEB_MOBILE / "index.html").read_text(encoding="utf-8")
    assert '<link rel="stylesheet" href="/comune/passi.css">' in html
    assert html.index('src="/comune/passi.js"') < html.index('src="/static/app.js"')


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


# ------------------------------------------ i blocchi di lavoro sul telefono


def test_il_telefono_usa_la_regia_dei_blocchi_condivisa() -> None:
    """Il sommario "passo N · M strumenti" e le righe fatte a mano non ci sono
    piu': diretta e cronologia passano da Passi.lavoro, la stessa regia del
    desktop (le sue regole sono provate in tests/test_passi_web.py)."""
    sorgente = APP_JS.read_text(encoding="utf-8")
    codice = "\n".join(
        riga for riga in sorgente.splitlines() if not riga.lstrip().startswith(("//", "*", "/*"))
    )
    assert "Passi.lavoro(" in codice
    for evento, chiamata in (
        ('"step"', "lavoro.segnaPasso("),
        ('"reasoning"', "lavoro.appendThinking("),
        ('"tool_start"', "lavoro.avviaTool("),
        ('"tool_end"', "lavoro.concludiTool("),
    ):
        assert re.search(rf"case\s*{evento}:[\s\S]{{0,400}}{re.escape(chiamata)}", codice), evento
    # Un testo del modello chiude il blocco prima della sua bolla.
    assert re.search(r'case\s*"content":[\s\S]{0,1600}lavoro\.chiudi\(\);\s*const bubble = liveBubble\(\)', codice)
    # La cronologia disegna gli stessi blocchi, dai messaggi `tool` salvati.
    inizio = codice.index("function renderConversation(")
    corpo = codice[inizio: codice.index("\nfunction ", inizio + 10)]
    assert "lavoro.concludiTool(" in corpo and "lavoro.pensieroSalvato(" in corpo


def test_al_riaggancio_se_ne_vanno_tutti_i_nodi_del_turno_vivo() -> None:
    """L'arretrato ridisegna il turno da capo: se restasse un blocco chiuso
    della diretta di prima, il turno si vedrebbe due volte."""
    sorgente = APP_JS.read_text(encoding="utf-8")
    corpo = _funzione(sorgente, "pulisciTurnoVivo")
    assert "[data-turno-vivo]" in corpo
    assert "nuovoLavoro(true)" in corpo
    assert "nodo.dataset.turnoVivo" in _funzione(sorgente, "nuovoLavoro")


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
