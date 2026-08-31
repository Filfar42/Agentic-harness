"""Numerazione dei punti del piano e comandi dell'impianto a tre colonne.

Il caso che ha fatto nascere questi test: un piano da dieci punti in cui il
modello si era portato dietro la numerazione della richiesta dell'utente
("1. Creare cartelle...", "2. Controllare..."). Quei numeri sono quelli
*dell'elenco dell'utente*, e divergono da quelli del piano al primo
raggruppamento -- con il risultato che nel pannello si legge "2." accanto a un
punto che per ``manage_plan`` e' il quinto.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.plan import MAX_TEXT_CHARS, Plan, pulisci_testo

WEB = Path(__file__).resolve().parents[1] / "web"


@pytest.mark.parametrize(
    ("grezzo", "atteso"),
    [
        ("1. Creare le cartelle", "Creare le cartelle"),
        ("10) far passare i test", "far passare i test"),
        ("- scrivere conta.py", "scrivere conta.py"),
        ("* scrivere conta.py", "scrivere conta.py"),
        ("• scrivere conta.py", "scrivere conta.py"),
        ("Punto 2: verificare l'output", "verificare l'output"),
        ("3 . strano ma numerato", "strano ma numerato"),
    ],
)
def test_la_numerazione_del_modello_viene_tolta(grezzo, atteso):
    assert pulisci_testo(grezzo) == atteso


@pytest.mark.parametrize(
    "testo",
    [
        "normale senza numero",
        "v1.2 aggiornare le dipendenze",       # non e' una numerazione
        "3D: rifare il rendering",             # nemmeno questo
        "2FA da attivare sul deploy",
    ],
)
def test_non_si_mangia_quello_che_numerazione_non_e(testo):
    assert pulisci_testo(testo) == testo


def test_si_toglie_un_livello_solo():
    """'1. 2. qualcosa' e' gia' un errore del modello: spogliarlo fino
    all'osso nasconderebbe che l'ha scritto cosi'."""
    assert pulisci_testo("1. 2. qualcosa") == "2. qualcosa"


def test_il_piano_numera_da_se():
    """Il numero mostrato e' l'id del punto, cioe' quello che il modello passa
    a manage_plan: pannello e chiamata devono dare la stessa risposta."""
    plan = Plan()
    plan.set_steps(["3. scrivere conta.py", "1. creare i file", "2. eseguire"])
    assert [s.text for s in plan.steps] == [
        "scrivere conta.py", "creare i file", "eseguire",
    ]
    assert [s.id for s in plan.steps] == ["1", "2", "3"]


def test_i_piani_gia_su_disco_vengono_ripuliti_in_lettura():
    """Il bug vero: la pulizia girava solo in scrittura, e una sessione salvata
    prima mostrava due numeri per punto -- quello del modello e quello del
    pannello."""
    plan = Plan.from_list([
        {"id": "1", "text": "1. Creare le cartelle", "status": "done", "note": "ok"},
        {"id": "2", "text": "2. Controllare la versione", "status": "todo"},
    ])
    assert [s.text for s in plan.steps] == ["Creare le cartelle", "Controllare la versione"]
    # e lo stato non si perde per strada
    assert plan.steps[0].status == "done" and plan.steps[0].note == "ok"


def test_lo_stato_si_legge_anche_senza_colore():
    """Fra due grigi diversi, a colpo d'occhio, non c'e' differenza -- e un
    colore solo escluderebbe chi non li distingue. Ogni stato ha una forma."""
    js = (WEB / "app.js").read_text(encoding="utf-8")
    riga = next(r for r in js.splitlines() if r.startswith("const PLAN_ICONS"))
    glifi = re.findall(r"'([^']+)'", riga)
    assert len(glifi) == 4 and len(set(glifi)) == 4, "quattro stati, quattro forme diverse"
    # il marcatore e' un elemento a se', leggibile dagli screen reader
    assert 'class="plan-mark"' in js and "aria-label" in js

    css = (WEB / "style.css").read_text(encoding="utf-8")
    # Il punto in corso e' il piu' forte dei quattro. Da quando anche "fatto" e
    # "saltato" hanno una tinta, lo sfondo non basta piu' a distinguerlo: la
    # barretta a sinistra ce l'ha solo lui, ed e' quella a rispondere alla
    # domanda che ci si fa guardando il pannello.
    assert ".plan-step.doing {" in css
    assert "background: var(--accent-soft)" in css
    # La barretta e' un livello di **sfondo**, ed e' l'unica forma che non
    # produce spigoli: un `box-shadow: inset` segue il `border-radius` e sugli
    # angoli si addensa in due cunei; un `::before` posizionato sta sopra lo
    # sfondo e non viene ritagliato da niente, quindi sborda dalla curva. Un
    # livello di background viene dipinto dentro la box e ritagliato dal
    # raggio per costruzione: gli angoli della barretta sono quelli della riga.
    doing = css[css.index(".plan-step.doing {") :]
    doing = doing[: doing.index("}")]
    assert "linear-gradient(var(--accent), var(--accent))" in doing
    assert "3px 100% no-repeat" in doing
    codice = "\n".join(r for r in css.splitlines() if not r.lstrip().startswith(("/*", "*", "-")))
    assert "box-shadow: inset" not in codice
    assert ".plan-step.doing::before" not in codice
    # e "saltato" e' giallo, non grigio: e' una decisione, non un "non fatto"
    assert ".plan-step.skipped .plan-mark { color: var(--warn); }" in css
    # "fatto" ha un secondo segno oltre al colore: il testo barrato
    fatto = css[css.index(".plan-step.done .plan-text {"):]
    assert "line-through" in fatto[: fatto.index("}")]


def test_il_taglio_resta_dopo_la_pulizia():
    lungo = "1. " + "x" * (MAX_TEXT_CHARS + 50)
    assert len(pulisci_testo(lungo)) == MAX_TEXT_CHARS


def test_lo_stato_sopravvive_alla_ripianificazione_anche_con_i_numeri():
    """set_steps confronta il testo esatto: se la pulizia non fosse applicata
    da entrambi i lati, un punto chiuso tornerebbe da fare solo perche' la
    seconda volta il modello ha scritto '1. ' invece di niente."""
    plan = Plan()
    plan.set_steps(["creare i file", "eseguire"])
    plan.start("1")
    plan.complete("1", "fatto")
    plan.set_steps(["1. creare i file", "2. eseguire", "3. verificare"])
    assert plan.steps[0].status == "done"
    assert plan.steps[0].note == "fatto"


# ---------------------------------------------------------------------------
# Impianto a tre colonne
# ---------------------------------------------------------------------------


def test_le_maniglie_stanno_fuori_dalle_colonne():
    """Dentro un contenitore che scorre scorrerebbero con il contenuto: il
    pannello destro ha overflow-y:auto, quindi la maniglia deve essere figlia
    di #app e agganciata alle variabili di larghezza."""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    prima_di_main = html[: html.index('<main id="main">')]
    assert 'id="sidebar-grip"' in prima_di_main
    assert 'id="panel-grip"' in prima_di_main
    assert prima_di_main.index("</aside>") < prima_di_main.index('id="sidebar-grip"')

    css = (WEB / "style.css").read_text(encoding="utf-8")
    assert "#sidebar-grip { left: calc(var(--sidebar-w)" in css
    assert "#panel-grip { right: calc(var(--panel-w)" in css
    assert "#app.no-sidebar #sidebar-grip, #app.no-panel #panel-grip { display: none; }" in css


def test_un_solo_meccanismo_di_trascinamento():
    """Anteprima, sidebar, pannello e il confine fra i due elenchi usano lo
    stesso divisorio: quattro copie della stessa logica vorrebbero dire
    correggerla in quattro posti.

    L'ultimo arrivato e' orizzontale, ma la differenza sta tutta in due
    parametri (da dove si ricava la misura, quale classe va sul body): se un
    giorno il conto qui sotto cresce senza che ``bindGrip`` resti uno solo,
    e' il segno che qualcuno ne ha riscritta una copia.
    """
    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert js.count("function bindGrip(") == 1
    assert js.count("setPointerCapture") == 1
    # e tutti e quattro passano di li'
    assert js.count("bindGrip($('#") == 4


def test_l_interruttore_del_pannello_e_nella_barra_in_alto():
    """In fondo alla sidebar spariva proprio quando si chiudeva la sidebar."""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert html.count('id="toggle-panel"') == 1
    topbar = html[html.index('<header id="topbar">'): html.index("</header>")]
    assert 'id="toggle-panel"' in topbar
    assert 'id="toggle-sidebar"' in topbar


def test_i_due_interruttori_hanno_lo_stesso_glifo():
    """Stessa azione su una colonna diversa: la colonna la dice la posizione,
    non il simbolo. Due glifi diversi obbligherebbero a impararli entrambi."""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    topbar = html[html.index('<header id="topbar">'): html.index("</header>")]
    glifi = re.findall(r'id="toggle-(?:sidebar|panel)"[^>]*>(.*?)</button>', topbar)
    assert len(glifi) == 2 and glifi[0] == glifi[1] == "\u2630"


def test_la_goccia_del_modello_esiste_e_ha_una_sola_lista():
    """La tendina delle impostazioni e la goccia in alto devono elencare le
    stesse cose, o una delle due mente appena cambia l'endpoint."""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'id="model-chip"' in html and 'id="model-menu"' in html
    # il titolo sta dentro la goccia, cosi' il bersaglio del clic e' il nome
    chip = html[html.index('id="model-chip"'): html.index('id="model-menu"')]
    assert 'id="top-title"' in chip

    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert "state.models = list" in js
    assert js.count("state.models") >= 2


def test_le_schede_lunghe_scorrono_invece_di_allungare_il_pannello():
    css = (WEB / "style.css").read_text(encoding="utf-8")
    blocco = css[css.index("#plan, #notes {"):]
    blocco = blocco[: blocco.index("}") + 1]
    assert "max-height" in blocco and "overflow-y: auto" in blocco

    js = (WEB / "app.js").read_text(encoding="utf-8")
    # la sfumatura va accesa da entrambe le schede, non solo dal piano
    assert js.count("  segnalaScorrimento(root);") == 2  # le chiamate, non la definizione
    # e l'ascoltatore si aggancia una volta sola per elemento
    assert "dataset.scrollBound" in js


# ---------------------------------------------------------------------------
# Avanzamento del piano
# ---------------------------------------------------------------------------
# "Fatto" e "da fare" erano due grigi diversi, e a colpo d'occhio due grigi
# sono lo stesso grigio: la spunta da 11px era l'unica differenza fra una riga
# chiusa e una aperta, e il conteggio 2/6 accanto al titolo era la cosa meno
# visibile della scheda.


def test_la_barra_dice_a_che_punto_e_prima_di_leggere():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    scheda = html[html.index('id="plan-card"'): html.index('id="notes-card"')]
    assert 'id="plan-bar-done"' in scheda and 'id="plan-bar-skipped"' in scheda
    # sopra i punti, non in fondo: e' la prima cosa da leggere
    assert scheda.index('id="plan-bar"') < scheda.index('id="plan"')

    css = (WEB / "style.css").read_text(encoding="utf-8")
    for regola in (".plan-bar {", ".plan-bar-done {", ".plan-bar-skipped {"):
        assert regola in css, regola


def test_i_saltati_non_contano_come_fatti():
    """Sommarli direbbe che e' stato fatto qualcosa che non e' stato fatto;
    tenerli fuori del tutto direbbe che manca ancora da fare. Un segmento
    proprio, in ambra."""
    js = (WEB / "app.js").read_text(encoding="utf-8")
    blocco = js[js.index("function renderPlan("):]
    blocco = blocco[: blocco.index("\nfunction ")]
    assert "s.status === 'skipped'" in blocco
    assert "$('#plan-bar-done').style.width" in blocco
    assert "$('#plan-bar-skipped').style.width" in blocco
    # e il conteggio per esteso resta a portata di puntatore
    assert "da fare`" in blocco


def test_le_tinte_di_stato_esistono_in_tutte_e_due_le_palette():
    """Sul tema scuro la stessa percentuale di colore sparisce: i valori
    stanno nelle variabili, cosi' la geometria resta una sola."""
    css = (WEB / "style.css").read_text(encoding="utf-8")
    chiaro = css[css.index(":root {"): css.index('html[data-theme="dark"]')]
    scuro = css[css.index('html[data-theme="dark"]'):]
    scuro = scuro[: scuro.index("}")]
    for variabile in ("--tint-ok", "--tint-warn"):
        assert variabile in chiaro, f"{variabile} manca nella palette chiara"
        assert variabile in scuro, f"{variabile} manca nella palette scura"
    assert ".plan-step.done { background: var(--tint-ok); }" in css
    assert ".plan-step.skipped { background: var(--tint-warn); }" in css
    # "da fare" resta senza tinta: e' l'assenza di sfondo a dire "non ancora"
    assert ".plan-step.todo { background" not in css


def test_la_barra_del_testo_fatto_non_cancella_la_riga():
    """Deve dire "chiuso", non rendere illeggibile cosa e' stato fatto: la
    riga e' grigio chiaro, non del colore del testo."""
    css = (WEB / "style.css").read_text(encoding="utf-8")
    blocco = css[css.index(".plan-step.done .plan-text {"):]
    blocco = blocco[: blocco.index("}")]
    assert "text-decoration-color: var(--border-strong)" in blocco


def test_la_pagina_smette_di_essere_la_fonte_viva_quando_lo_stream_finisce():
    """Perche' la sincronizzazione fra desktop e telefono si bloccava.

    Il bus globale ridisegna la conversazione solo se questa pagina non sta
    gia' ricevendo un turno dal vivo -- giusto, senno' si cancellerebbe il
    turno mentre scorre. Ma il controller dell'attacco non veniva mai
    azzerato a fine stream: dopo il primo turno la pagina diceva per sempre
    "sto disegnando io", e un messaggio scritto dal telefono non compariva
    piu' fino a un ricaricamento a mano.
    """
    sorgente = (WEB / "app.js").read_text(encoding="utf-8")
    assert "function attaccatoAUnoStream()" in sorgente
    # Da quando una caduta e' un riattacco e non una fine, l'uscita di
    # ``attachStream`` e' **una sola** -- il ciclo finisce quando lo stream si
    # chiude davvero -- ed e' li' che il controller si azzera. L'altra strada
    # (``mio = false``) e' il caso in cui un altro attach ha gia' preso il
    # posto di questo: azzerarlo li' cancellerebbe il controller di qualcun
    # altro, ed e' il motivo per cui non lo fa.
    assert sorgente.count("if (state.attachAbort === controller) state.attachAbort = null;") == 1
    corpo = sorgente[sorgente.index("async function attachStream(") :]
    corpo = corpo[: corpo.index("\n}\n")]
    assert "if (!mio) {" in corpo
    # E la domanda "posso ridisegnare?" resta una sola, dentro la rilettura.
    riallinea = sorgente[sorgente.index("async function riallinea(") :]
    riallinea = riallinea[: riallinea.index("\n}\n")]
    assert "attaccatoAUnoStream()) return;" in riallinea
    assert "showSession(payload);" in riallinea


def test_il_confine_fra_conversazioni_e_vault_si_trascina():
    """Quanto spazio meritino i vault dipende da quanti ne hai.

    Con le chat annidate sotto ogni vault la meta' fissa e' una scelta che va
    bene a nessuno: chi ha un vault solo vuole vedere le conversazioni, chi ne
    ha sei vuole il contrario. La maniglia e' la ``.col-grip`` girata di 90
    gradi -- un solo gesto da imparare per tutti e quattro i divisori.
    """
    html = (WEB / "index.html").read_text(encoding="utf-8")
    css = (WEB / "style.css").read_text(encoding="utf-8")
    js = (WEB / "app.js").read_text(encoding="utf-8")

    # Sta **fra** i due elenchi, non sopra o sotto entrambi.
    assert html.index('id="sessions"') < html.index('id="vaults-grip"')
    assert html.index('id="vaults-grip"') < html.index('id="vaults-sec"')

    assert "cursor: ns-resize" in css
    assert "flex: 0 0 var(--vaults-h" in css
    # Il cursore non torna freccia uscendo dagli 8px della maniglia.
    assert "body.resizing-rows" in css

    # Nessuno dei due lati si puo' annullare: un divisorio che puo' far
    # sparire una meta' e' un interruttore travestito.
    corpo = js[js.index("function setVaultsHeight("):]
    corpo = corpo[: corpo.index("\n}")]
    assert "VAULTS_H_MIN" in corpo and "CONVERSAZIONI_H_MIN" in corpo
    # E la misura sopravvive al ricaricamento.
    assert "localStorage.setItem(VAULTS_H_KEY" in corpo
