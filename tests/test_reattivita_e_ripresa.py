"""Cosa l'utente vede subito, e cosa succede quando la rete cade a meta' turno.

I casi qui dentro vengono tutti dalla sessione di prova del 19/08/2026 con
``Qwen3.8:27b`` (finestra 98k, 32k di generazione, 282 messaggi): non sono
ipotesi, sono cose che sono successe e che si vedevano guardando lo schermo.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tests.test_agent_loop as fake  # noqa: E402
import tests.test_server as server_tests  # noqa: E402
from core import agent as agent_mod  # noqa: E402

# Le due fixture del server HTTP sono gia' scritte: riusarle e' l'unico modo
# perche' un cambio nell'attrezzatura valga anche qui.
fake_ollama = fake.fake_ollama
client = server_tests.client

WEB = Path(__file__).resolve().parents[1] / "web"


# ---------------------------------------------------------------------------
# Consumo e cronologia aggiornati all'invio, non a fine turno
# ---------------------------------------------------------------------------


def test_chat_e_answer_rispondono_con_le_statistiche():
    """Il misuratore del contesto restava fermo al valore precedente al
    messaggio appena inviato: l'unico evento ``state`` lo emetteva il
    ``finally`` del worker, cioe' a turno finito. Su un turno da minuti si
    legge per tutto quel tempo un numero che non e' piu' vero -- e una
    conversazione nuova non compare nemmeno nell'elenco a sinistra."""
    sorgente = (Path(__file__).resolve().parents[1] / "server" / "main.py").read_text(
        encoding="utf-8"
    )
    for rotta in ('@app.post("/api/chat")', '@app.post("/api/answer")'):
        corpo = sorgente[sorgente.index(rotta) :]
        corpo = corpo[: corpo.index("@app.", len(rotta))]
        assert "session_stats(session_id)" in corpo, rotta


def test_il_client_applica_subito_quelle_statistiche():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    for funzione in ("async function send()", "async function submitAnswer("):
        corpo = js[js.index(funzione) :]
        corpo = corpo[: corpo.index("\n}\n")]
        assert "applyStats(data.stats)" in corpo, funzione


def test_quello_che_il_client_sa_gia_non_lo_chiede_al_server():
    """Fra l'invio e il primo byte del modello locale passano i secondi del
    caricamento in VRAM. In quell'attesa la conversazione appena creata non
    compariva a sinistra e il riquadro dell'ultima esecuzione mostrava ancora i
    numeri della chat precedente. Nessuna delle due e' un'informazione che deve
    arrivare dalla rete: il titolo di una chat e' la prima riga che ci ha
    scritto l'utente, e i numeri vecchi si spengono da soli."""
    js = (WEB / "app.js").read_text(encoding="utf-8")
    corpo = js[js.index("async function send()") :]
    corpo = corpo[: corpo.index("\n}\n")]
    invio = corpo.index("await api('/api/chat'")
    assert 0 < corpo.index("resetUsage()") < invio
    assert 0 < corpo.index("aggiungiAllElenco(") < invio


def test_il_riquadro_dell_ultima_esecuzione_si_spegne_cambiando_chat():
    """``renderUsage`` accendeva il blocco e non lo spegneva mai: erano numeri
    senza etichetta di provenienza, e letti nel posto sbagliato non sembrano
    vecchi, sembrano sbagliati."""
    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert "function resetUsage()" in js
    corpo = js[js.index("async function showSession(") :]
    corpo = corpo[: corpo.index("\n}\n")]
    assert "resetUsage()" in corpo


def test_la_riga_ottimistica_non_duplica_e_non_disturba_la_ricerca():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    corpo = js[js.index("function aggiungiAllElenco(") :]
    corpo = corpo[: corpo.index("\n}\n")]
    assert "state.search.q" in corpo, "con la ricerca attiva l'elenco e' un altro"
    assert "state.sessions.some((s) => s.id === sessionId)" in corpo


# ---------------------------------------------------------------------------
# Gli asset devono poter cambiare
# ---------------------------------------------------------------------------


def test_gli_asset_cambiano_indirizzo_quando_cambiano(client):
    """Il `Cache-Control` da solo non poteva bastare, ed e' il punto.

    Un'intestazione la si legge solo su una risposta, e la risposta arriva solo
    se la richiesta parte: finche' la copia in cache e' considerata fresca --
    per euristica, senza che nessuno l'abbia deciso -- `no-cache` non viene mai
    letto. La correzione non puo' arrivare da sola. L'unica cosa che scavalca
    una cache e' un indirizzo mai visto.
    """
    html = client.get("/").text
    assert client.get("/").headers.get("Cache-Control") == "no-cache"
    impronte = re.findall(r'/static/(app\.js|style\.css)\?v=([0-9a-f]{10})', html)
    assert {n for n, _ in impronte} == {"app.js", "style.css"}, html[:400]
    # E l'impronta segue il file: toccarlo cambia l'indirizzo, senza che
    # nessuno debba ricordarsi di alzare un numero a mano.
    from server import main as sm

    prima = sm._impronta("app.js")
    (WEB / "app.js").touch()
    assert sm._impronta("app.js") != prima

    # Gli asset restano comunque rivalidabili, per chi arriva con un URL vecchio.
    risposta = client.get("/static/app.js")
    assert risposta.headers.get("Cache-Control") == "no-cache"
    assert "etag" in {k.lower() for k in risposta.headers}


# ---------------------------------------------------------------------------
# La goccia di stato: etichetta e colore non possono divergere
# ---------------------------------------------------------------------------


def test_lo_stato_dell_endpoint_si_scrive_in_un_posto_solo():
    """Diceva "online" restando del colore di prima: l'etichetta la riscriveva
    il ricaricamento dei modelli, la classe no. Una goccia che dice una cosa e
    ne colora un'altra e' il caso peggiore, perche' non sembra un errore."""
    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert "function renderStatusPill(" in js
    # Nessun altro percorso scrive dentro la goccia: chi la tocca passa di li'.
    scritture = re.findall(r"\$\('#pill-status'\)\.(className|innerHTML|textContent)", js)
    corpo = js[js.index("function renderStatusPill(") :]
    corpo = corpo[: corpo.index("\n}\n")]
    assert len(scritture) == len(
        re.findall(r"pill\.(className|innerHTML|textContent)", corpo)
    ) or not scritture

    css = (WEB / "style.css").read_text(encoding="utf-8")
    assert ".pill.wait" in css, "«non lo so ancora» e' uno stato, non un avviso"


# ---------------------------------------------------------------------------
# Il pannello delle domande risponde al gesto, non alla fine del ragionamento
# ---------------------------------------------------------------------------


def test_la_risposta_finisce_nel_riquadro_vivo_e_non_nel_primo_della_pagina():
    """``querySelector('.question')`` torna il PRIMO nodo del documento: in una
    conversazione con piu' domande e' una vecchia gia' risposta. La risposta
    finiva incollata sotto quella, e il riquadro in attesa restava coi suoi
    pulsanti fino al ridisegno della cronologia -- cioe' a ragionamento finito.
    """
    js = (WEB / "app.js").read_text(encoding="utf-8")
    corpo = js[js.index("async function submitAnswer(") :]
    corpo = corpo[: corpo.index("\n}\n")]
    assert "$('.question.live')" in corpo
    assert "$('.question')" not in corpo
    # e il riquadro in attesa e' l'unico marcato
    assert "box.classList.add('live')" in js
    assert "box.classList.remove('live')" in js


def test_il_segno_arriva_prima_della_chiamata():
    """Fra il click e la fine della POST c'e' un giro di rete: in mezzo i
    pulsanti restavano cliccabili e niente diceva che la scelta era stata
    presa."""
    js = (WEB / "app.js").read_text(encoding="utf-8")
    corpo = js[js.index("function markAnswered(") :]
    assert "await" not in corpo[: corpo.index("\n}\n")]
    scelta = js[js.index("if (!multi) {") :][:400]
    assert scelta.index("classList.add('selected')") < scelta.index("submitAnswer(")


# ---------------------------------------------------------------------------
# Ripresa automatica: una HTTP caduta non e' un turno perso
# ---------------------------------------------------------------------------


def test_si_riprende_su_cio_che_lascia_la_conversazione_valida():
    for testo in (
        "Timeout dopo 300s. Il modello e' probabilmente in fase di caricamento",
        "Errore di rete verso Ollama: ConnectError",
        "HTTP 502: Bad Gateway",
        "connection reset by peer",
        "peer closed connection without sending complete message body",
    ):
        assert agent_mod.errore_riprovabile(testo), testo


def test_non_si_riprende_su_cio_che_si_ripresentera_identico():
    """Un 404 sul modello o un payload rifiutato tornano uguali al secondo
    tentativo: riprovare vuol dire solo far aspettare l'utente prima di dirgli
    la stessa cosa. Il criterio e' stretto di proposito -- un errore nuovo
    chiude il turno e finisce sotto gli occhi di chi guarda."""
    for testo in (
        "HTTP 404: model 'qwen3.8:27b' not found",
        "HTTP 400: invalid options",
        "",
        "Qualcosa di mai visto prima",
    ):
        assert not agent_mod.errore_riprovabile(testo), testo


def test_le_riprese_sono_contate_e_finite():
    assert agent_mod.MAX_RIPRESE_STREAM == 2
    sorgente = (Path(__file__).resolve().parents[1] / "core" / "agent.py").read_text(
        encoding="utf-8"
    )
    assert 'count_nudge("ripresa_stream")' in sorgente


# ---------------------------------------------------------------------------
# Il traceback dell'applicazione
# ---------------------------------------------------------------------------


def test_il_timing_non_passa_da_basehttpmiddleware():
    """``@app.middleware("http")`` monta ``BaseHTTPMiddleware``, che si mette in
    mezzo al corpo della risposta e lo ri-emette. Se il browser stacca a meta',
    quel rimbalzo chiude comunque il corpo con un frammento vuoto e uvicorn si
    ritrova meno byte di quanti ne aveva annunciati::

        RuntimeError: Response content shorter than Content-Length
    """
    sorgente = (Path(__file__).resolve().parents[1] / "server" / "main.py").read_text(
        encoding="utf-8"
    )
    # Solo le righe di codice: il commento sopra la classe spiega proprio
    # perche' quel decoratore non si usa, e nominarlo non e' usarlo.
    codice = "\n".join(r for r in sorgente.splitlines() if not r.lstrip().startswith(("#", "*")))
    assert '@app.middleware("http")' not in codice.replace(
        '``@app.middleware("http")``', ""
    )
    assert "class ServerTiming:" in sorgente
    assert "app.add_middleware(ServerTiming)" in sorgente


def test_l_intestazione_del_tempo_arriva_lo_stesso(client):
    risposta = client.get("/api/sessions")
    assert risposta.status_code == 200
    assert "Server-Timing" in risposta.headers


def test_nessun_avviso_di_sintassi_all_avvio():
    """Il SyntaxWarning su ``\\|`` usciva in cima al log ad ogni avvio del
    server e sembrava un errore dell'applicazione."""
    import warnings

    with warnings.catch_warnings(record=True) as avvisi:
        warnings.simplefilter("always")
        compile(
            (Path(__file__).resolve().parents[1] / "core" / "sandbox.py").read_text(
                encoding="utf-8"
            ),
            "sandbox.py",
            "exec",
        )
    assert not [a for a in avvisi if issubclass(a.category, SyntaxWarning)]
