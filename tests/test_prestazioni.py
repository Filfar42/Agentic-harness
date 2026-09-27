"""Cosa rendeva l'interfaccia lenta, e le regole che lo impediscono.

Non sono micro-ottimizzazioni: sono quattro difetti che si vedevano a occhio
nudo -- il popup di Chrome "la pagina non risponde", secondi di attesa ad ogni
apertura di chat, il pannello che si spalanca da solo. Ognuno aveva una causa
misurabile, e qui si presidia la causa, non il sintomo.

Misure del 30/08/2026 sulla postazione dell'utente (69 conversazioni, 17 MB):
* ``GET /api/sessions/<chat da 2,5 MB>``: **155 ms -> 7,6 ms**;
* un passo da 20.000 token di pensiero: da ~centinaia di MB di eventi SSE a
  qualche decina di kB.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import agent as agent_mod
from core import backend as backend_mod

ROOT = Path(__file__).resolve().parents[1]
WEB = Path(__file__).resolve().parents[1] / "web"
WEB_MOBILE = Path(__file__).resolve().parents[1] / "web_mobile"


# ---------------------------------------------------------------------------
# Il testo in corso viaggia a incrementi
# ---------------------------------------------------------------------------


def _senza_freno(rubinetto: agent_mod.Rubinetto) -> None:
    """Sposta indietro l'orologio del rubinetto: nei test il freno a 10/s
    scarterebbe tutto tranne il primo aggiornamento."""
    rubinetto._ultimo = 0.0


def test_il_primo_pezzo_e_intero_i_successivi_sono_incrementi():
    """La regola per chi legge: se c'e' ``append`` accoda, senno' sostituisce.
    Il primo evento deve quindi portare ``text``, o il client accoderebbe a un
    nodo che non esiste."""
    r = agent_mod.Rubinetto()
    primo = list(r.aggiorna("Devo ", ""))
    assert [type(e).__name__ for e in primo] == ["ReasoningDelta"]
    assert primo[0].text == "Devo " and primo[0].append == ""

    _senza_freno(r)
    dopo = list(r.aggiorna("Devo guardare i file.", ""))
    assert dopo[0].append == "guardare i file."
    assert dopo[0].text == ""


def test_un_testo_riscritto_all_indietro_torna_intero():
    """Il parser puo' riscrivere il testo gia' emesso: succede quando un tag
    ``<think>`` arriva spezzato fra due chunk. Li' accodare mostrerebbe testo
    doppio, e l'unica risposta giusta e' rimandarlo tutto."""
    r = agent_mod.Rubinetto()
    list(r.aggiorna("<thi", ""))
    _senza_freno(r)
    eventi = list(r.aggiorna("tutt'altro", ""))
    assert eventi[0].text == "tutt'altro" and eventi[0].append == ""


def test_il_freno_ritarda_ma_non_perde_niente():
    """Dieci aggiornamenti al secondo: quello saltato non sparisce, viene
    incluso nel successivo insieme a tutto cio' che nel frattempo e' arrivato."""
    r = agent_mod.Rubinetto()
    list(r.aggiorna("a", ""))
    # Subito dopo: il freno e' abbassato, non esce niente.
    assert list(r.aggiorna("ab", "")) == []
    assert list(r.aggiorna("abc", "")) == []
    _senza_freno(r)
    eventi = list(r.aggiorna("abcd", ""))
    # Tutti e tre i pezzi saltati sono qui dentro.
    assert eventi[0].append == "bcd"


def test_niente_di_nuovo_niente_evento():
    r = agent_mod.Rubinetto()
    list(r.aggiorna("uguale", "risposta"))
    _senza_freno(r)
    assert list(r.aggiorna("uguale", "risposta")) == []


def test_i_due_canali_viaggiano_insieme():
    r = agent_mod.Rubinetto()
    eventi = list(r.aggiorna("penso", "rispondo"))
    assert [type(e).__name__ for e in eventi] == ["ReasoningDelta", "ContentDelta"]


def test_ogni_passo_finisce_con_il_testo_intero():
    """La rete di sicurezza degli incrementi.

    Un abbonato lento puo' vedersi scartare dei frame (vedi
    ``_SUBSCRIBER_QUEUE_MAX``), e con soli incrementi resterebbe con un testo
    sbagliato fino alla fine del turno. Il testo completo a fine passo lo
    rimette in pari, e costa un evento per passo.
    """
    sorgente = (Path(__file__).resolve().parents[1] / "core" / "agent.py").read_text(
        encoding="utf-8"
    )
    assert "yield ReasoningDelta(text=parser.reasoning)" in sorgente
    assert "yield ContentDelta(text=parser.answer)" in sorgente


@pytest.mark.parametrize("file", ["app.js", None])
def test_i_client_sanno_leggere_gli_incrementi(file):
    """Desktop e telefono leggono lo stesso flusso: se uno dei due conosce solo
    il campo cumulativo, la risposta gli resta ferma sul primo pezzo."""
    if file:
        js = (WEB / "app.js").read_text(encoding="utf-8")
        assert "if (event.append) turn.appendThinking(event.append);" in js
        assert "if (event.append) turn.appendAnswer(event.append);" in js
    else:
        js = (WEB_MOBILE / "app.js").read_text(encoding="utf-8")
        assert "data.append" in js and "testoInCorso" in js


# ---------------------------------------------------------------------------
# Il modello irraggiungibile si paga una volta, non ad ogni richiesta
# ---------------------------------------------------------------------------


def test_una_sonda_fallita_non_si_ripaga_ad_ogni_lettura(monkeypatch):
    """Il difetto piu' caro dell'apertura di una chat.

    ``session_stats`` chiede le capability del modello ad ogni lettura di
    sessione. Con il modello su un'altra macchina spenta -- il setup di questa
    postazione -- ogni lettura pagava i **sei secondi** di timeout per intero:
    aprire una chat, finire un turno, riallinearsi dopo una sospensione.
    """
    backend_mod.forget_model_info()
    tentativi = []

    def finta_post(*args, **kwargs):
        tentativi.append(1)
        raise OSError("macchina spenta")

    monkeypatch.setattr(backend_mod.get_client(), "post", finta_post)
    b = backend_mod.OllamaBackend("http://spenta:11434")
    for _ in range(5):
        assert b.model_info("qwen") == {}
    assert len(tentativi) == 1, "il fallimento va ricordato, non ripetuto"


def test_la_sonda_a_mano_dimentica_anche_i_fallimenti(monkeypatch):
    """Chi ri-lancia la sonda lo fa perche' ha appena acceso qualcosa:
    rispondergli con la fotografia di trenta secondi fa e' inutile."""
    backend_mod.forget_model_info()
    tentativi = []
    monkeypatch.setattr(
        backend_mod.get_client(), "post",
        lambda *a, **k: tentativi.append(1) or (_ for _ in ()).throw(OSError("giu'")),
    )
    b = backend_mod.OllamaBackend("http://spenta:11434")
    b.model_info("qwen")
    backend_mod.forget_model_info()
    b.model_info("qwen")
    assert len(tentativi) == 2


def test_un_successo_cancella_il_fallimento_ricordato(monkeypatch):
    backend_mod.forget_model_info()
    b = backend_mod.OllamaBackend("http://ora-accesa:11434")

    class Risposta:
        def raise_for_status(self): pass
        def json(self): return {"capabilities": ["tools"]}

    monkeypatch.setattr(
        backend_mod.get_client(), "post",
        lambda *a, **k: (_ for _ in ()).throw(OSError("giu'")),
    )
    assert b.model_info("qwen") == {}
    # Il TTL non e' ancora scaduto, ma il server e' tornato: si simula lo
    # scadere spostando indietro il ricordo.
    backend_mod._MODEL_INFO_FALLITI[("http://ora-accesa:11434", "qwen")] = (
        time.monotonic() - backend_mod.MODEL_INFO_FALLIMENTO_TTL_S - 1
    )
    monkeypatch.setattr(backend_mod.get_client(), "post", lambda *a, **k: Risposta())
    assert b.model_info("qwen") == {"capabilities": ["tools"]}
    assert ("http://ora-accesa:11434", "qwen") not in backend_mod._MODEL_INFO_FALLITI


# ---------------------------------------------------------------------------
# Meno lavoro per il browser
# ---------------------------------------------------------------------------


def test_la_tendina_di_un_tool_si_costruisce_al_primo_clic():
    """Argomenti e risultato entravano nel DOM anche da tendina chiusa: un
    ``write_file`` da 34 kB per ogni tool di ogni passo, tutto invisibile.

    Le tendine sono diventate righe (web/passi.js), la regola e' rimasta: il
    dettaglio nasce al primo clic, una volta sola. Il comportamento lo prova
    tests/test_passi_web.py; qui resta il guardiano sulla forma."""
    js = (WEB / "passi.js").read_text(encoding="utf-8")
    corpo = js[js.index("function apribile(") :]
    corpo = corpo[: corpo.index("\n  }\n")]
    assert "if (!corpo) {" in corpo
    assert "corpo = costruisci();" in corpo
    # Riaprire non ricostruisce: nasconde e mostra lo stesso nodo.
    assert "corpo.hidden = !corpo.hidden;" in corpo


def test_client_e_server_contano_gli_stessi_messaggi():
    """Due numeri che devono restare uguali: il client usa il suo per chiedere
    il blocco precedente, il server il suo per decidere cosa mandare."""
    from server import main as server_main

    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert f"const MESSAGGI_PER_PAGINA = {server_main.MESSAGGI_PER_PAGINA};" in js


# ---------------------------------------------------------------------------
# L'anteprima non si spalanca da sola
# ---------------------------------------------------------------------------


def test_entrare_in_una_chat_non_riapre_l_anteprima_di_ieri():
    """Entrare in una conversazione non e' chiedere di vedere qualcosa:
    l'anteprima memorizzata e' il ricordo di cosa l'agente mostrava l'ultima
    volta, e riaprirla ad ogni ingresso voleva dire mezzo schermo occupato da
    una pagina di ieri, da chiudere a mano ogni volta."""
    js = (WEB / "app.js").read_text(encoding="utf-8")
    corpo = js[js.index("async function showSession(") :]
    corpo = corpo[: corpo.index("\n}\n")]
    assert "renderPreview(payload.preview, { apri: !entrando || Boolean(payload.running) });" in corpo
    # Rileggere non e' entrare: a fine turno il pannello non si chiude da solo.
    riallinea = js[js.index("async function riallinea(") :]
    assert "entrando" not in riallinea[: riallinea.index("\n}\n")]


# ---------------------------------------------------------------------------
# Il conto del contesto non si rifa' ad ogni chiamata
# ---------------------------------------------------------------------------


def test_il_contesto_si_ricalcola_solo_quando_cambia(tmp_path, monkeypatch):
    """Ricostruire i messaggi per l'API e stimarli costa quanto tutta la
    cronologia, e ``session_stats`` viene chiamata ad ogni apertura, ad ogni
    fine turno, ad ogni invio e ad ogni riallineamento."""
    from core import config as config_mod
    from core import memory as memory_mod
    from core import session as session_mod

    monkeypatch.setattr(config_mod, "DATA_DIR", tmp_path / "sessions")
    monkeypatch.setattr(session_mod, "DATA_DIR", tmp_path / "sessions")
    monkeypatch.setattr(memory_mod, "MEMORY_FILE", tmp_path / "memories.json")

    from server import main as server_main

    server_main.STATE = server_main.AppState()
    server_main.STATE.settings.update(
        {"workspace_dir": str(tmp_path), "docker_autostart": False, "sandbox": "host"}
    )
    sid = server_main.STATE.last_opened
    server_main.STATE.messages(sid).extend(
        [{"role": "user", "content": "ciao"}, {"role": "assistant", "content": "eccomi"}]
    )

    conti = []
    vero = server_main.agent_mod.build_api_messages

    def contato(*args, **kwargs):
        conti.append(1)
        return vero(*args, **kwargs)

    monkeypatch.setattr(server_main.agent_mod, "build_api_messages", contato)

    primo = server_main.contesto_usato(sid)
    for _ in range(5):
        assert server_main.contesto_usato(sid) == primo
    assert len(conti) == 1, "cinque letture, un solo conto"

    # Ma un messaggio nuovo lo cambia davvero, e il numero deve accorgersene.
    # Il costo non e' necessariamente monotono: al nuovo turno scompare anche
    # l'eventuale nota di coda che impediva di ripetere il testo del turno
    # precedente.
    server_main.STATE.messages(sid).append(
        {"role": "user", "content": "una domanda molto piu' lunga della precedente"}
    )
    assert server_main.contesto_usato(sid) != primo
    assert len(conti) == 2


# ---------------------------------------------------------------------------
# Aprire una chat non aspetta Docker
# ---------------------------------------------------------------------------


def test_la_spazzata_dei_container_non_sta_nella_richiesta(tmp_path, monkeypatch):
    """La prima apertura di chat in una cartella faceva la spazzata dei
    container rimasti in piedi da prima di un riavvio -- quasi sempre per
    scoprire che era gia' tutto pulito. Su Windows ogni ``docker`` e' un
    processo nuovo da mezzo secondo buono: pagarlo dentro la richiesta e' una
    chat che sembra impiantata appena la apri."""
    import threading

    from core import config as config_mod
    from core import memory as memory_mod
    from core import session as session_mod

    monkeypatch.setattr(config_mod, "DATA_DIR", tmp_path / "sessions")
    monkeypatch.setattr(session_mod, "DATA_DIR", tmp_path / "sessions")
    monkeypatch.setattr(memory_mod, "MEMORY_FILE", tmp_path / "memories.json")

    from server import main as server_main

    server_main.STATE = server_main.AppState()
    server_main.STATE.settings.update(
        {"workspace_dir": str(tmp_path), "docker_autostart": False, "sandbox": "host"}
    )
    fatta = threading.Event()
    dove = {}

    def finta_ferma():
        dove["thread"] = threading.current_thread().name
        fatta.set()
        return False

    monkeypatch.setattr(server_main.STATE, "ferma_anteprime", finta_ferma)

    server_main.smonta_se_serve("")
    assert fatta.wait(5), "la spazzata deve avvenire, solo non qui"
    assert dove["thread"] != threading.current_thread().name

    # E una volta sola: la seconda apertura nella stessa cartella non ripaga
    # niente.
    fatta.clear()
    server_main.smonta_se_serve("")
    assert not fatta.wait(0.3)


# ---------------------------------------------------------------------------
# La pagina non aspetta la rete
# ---------------------------------------------------------------------------


def test_il_bootstrap_non_parla_col_server_del_modello():
    """Tre viaggi di rete prima di rispondere -- ``status``, ``version``,
    ``streams_tool_calls`` -- volevano dire fino a una decina di secondi di
    **pagina bianca** con il modello su una macchina spenta, mentre tutto
    quello che serve a disegnare l'interfaccia era gia' sul disco dell'harness.
    """
    sorgente = (Path(__file__).resolve().parents[1] / "server" / "main.py").read_text(
        encoding="utf-8"
    )
    corpo = sorgente[sorgente.index('@app.get("/api/bootstrap")') :]
    corpo = corpo[: corpo.index("@app.get", 20)]
    for sonda in ("backend.status()", "backend.ping()", "list_models()", "backend.version()"):
        assert sonda not in corpo, f"{sonda} deve stare fuori dal bootstrap"
    assert '"online": None' in corpo


def test_il_client_disegna_prima_e_sonda_dopo():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    corpo = js[js.index("async function boot()") :]
    corpo = corpo[: corpo.index("\n}\n")]
    # Senza await: e' l'unica cosa dell'avvio che dipende da un'altra macchina.
    assert "\n  sondaBackend();" in corpo
    assert "await sondaBackend" not in corpo
    sonda = js[js.index("async function sondaBackend(") :]
    sonda = sonda[: sonda.index("\n}\n")]
    assert "api('/api/backend')" in sonda
    assert "renderStatusPill(info.online" in sonda
    assert "fillModels(info.models)" in sonda


# ---------------------------------------------------------------------------
# Il costo del blocco fisso: misurato, non dichiarato
# ---------------------------------------------------------------------------


def test_il_percorso_snello_costa_davvero_meno():
    """Il commento che invecchia da solo, chiuso con un test.

    Non fissa i valori -- i testi cambiano, ed e' giusto -- ma il RAPPORTO che
    giustifica la scelta automatica in ``pick_system_prompt``. Se un giorno lo
    snello smettesse di essere piu' leggero, questo lo direbbe invece di
    lasciare in piedi tre commenti che spiegano un vantaggio inesistente.

    Storia: i commenti dichiaravano "prompt snello 612 token" e "schemi ~1.790".
    Misurati il 30/08/2026: 3.001 e 4.318, sbagliati di 4,9x e 2,4x.
    """
    from core.prompts import costi_del_prefisso

    c = costi_del_prefisso()
    snello = c["prompt_snello"] + c["schemi_snelli"]
    esteso = c["prompt_esteso"] + c["schemi_estesi"]
    assert snello < esteso, (
        f"il percorso 'snello' costa {snello} token contro {esteso}: la scelta "
        f"automatica sta ottimizzando al contrario"
    )


def test_gli_schemi_pesano_piu_del_prompt():
    """Dove conviene tagliare, se si vuole tagliare.

    Vale in entrambi i percorsi, ed e' il motivo per cui togliere dallo schema
    i tool inutilizzabili (web_search spento, wiki_search senza wiki) rende
    piu' che accorciare il prompt.
    """
    from core.prompts import costi_del_prefisso

    c = costi_del_prefisso()
    assert c["schemi_estesi"] > c["prompt_esteso"]
    assert c["schemi_snelli"] > c["prompt_snello"]


def test_il_prompt_snello_e_piu_corto_di_quello_esteso():
    """The revised contract is shared; only the extended prompt adds examples."""
    from core.prompts import costi_del_prefisso

    c = costi_del_prefisso()
    assert c["prompt_snello"] < c["prompt_esteso"]


# ---------------------------------------------------------------------------
# Le cache che mancavano
# ---------------------------------------------------------------------------


def test_props_non_ritenta_a_ogni_turno_con_il_server_spento(monkeypatch):
    """La cache teneva solo i successi.

    ``clamp_num_ctx`` chiama ``props`` da ``gen_params()``, cioe' a ogni turno:
    con llama-server spento erano quattro secondi di timeout prima che partisse
    qualsiasi cosa. E' la stessa correzione gia' fatta per ``model_info``.
    """
    from core.backend import LlamaCppBackend

    tentativi = {"n": 0}

    def _rifiuta(*_a, **_k):
        tentativi["n"] += 1
        raise OSError("connection refused")

    b = LlamaCppBackend("http://spento:8080", "", 5.0)
    monkeypatch.setattr(backend_mod.get_client(), "get", _rifiuta)
    for _ in range(5):
        assert b.props() == {}
    assert tentativi["n"] == 1, (
        f"{tentativi['n']} tentativi per cinque chiamate: il fallimento non e' "
        f"memorizzato"
    )


def test_slots_viaggia_con_l_autenticazione(monkeypatch):
    """``/props`` mandava l'Authorization e ``/slots`` no, contro la sua regola."""
    from core.backend import LlamaCppBackend

    visti: list[dict] = []

    class _Vuota:
        status_code = 200

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {}

    def _get(url, **kw):
        visti.append({"url": url, "headers": kw.get("headers")})
        return _Vuota()

    b = LlamaCppBackend("http://server:8080", "chiave-segreta", 5.0)
    monkeypatch.setattr(backend_mod.get_client(), "get", _get)
    b.server_num_ctx()
    slots = [v for v in visti if v["url"].endswith("/slots")]
    assert slots, "/slots non e' stato interrogato"
    assert slots[0]["headers"], "/slots parte senza header di autenticazione"


def test_il_container_verificato_non_si_riverifica_a_ogni_operazione(monkeypatch):
    """``_attendi_stato`` faceva fino a 40 giri, e ognuno due processi docker.

    Un solo ``preview action='serve'`` avviava circa centoventi processi docker
    per aspettare che una porta rispondesse.
    """
    import tempfile
    from pathlib import Path as _P

    from core import sandbox as sb

    ws = _P(tempfile.mkdtemp())
    sb.dimentica_container()
    chiamate = {"n": 0}

    def _finto(args, timeout=None):
        chiamate["n"] += 1

        class _P2:
            returncode = 0
            stdout = "id-finto" if args[:1] == ["ps"] else ""
            stderr = ""

        return _P2()

    monkeypatch.setattr(sb, "_run_docker", _finto)
    monkeypatch.setattr(sb, "_running_fingerprint", lambda _n: sb._fingerprint(
        sb.DEFAULT_IMAGE, True, None, sb.MEM_LIMIT, sb.PIDS_LIMIT
    ))
    sb.ensure_container(ws)
    dopo_il_primo = chiamate["n"]
    for _ in range(10):
        sb.ensure_container(ws)
    assert chiamate["n"] == dopo_il_primo, (
        f"{chiamate['n'] - dopo_il_primo} processi docker in piu' per dieci "
        f"operazioni sullo stesso container"
    )
    sb.dimentica_container()


# ---------------------------------------------------------------------------
# Il client condiviso verso il backend
# ---------------------------------------------------------------------------


def test_tutte_le_richieste_al_backend_passano_dal_client_condiviso():
    """Nessuna ``httpx.get``/``post``/``stream`` di modulo in ``core/backend``.

    Ognuna di quelle funzioni costruisce un client usa-e-getta: apre una
    connessione TCP, la usa una volta e la chiude. Su localhost -- dove sta il
    backend nel caso normale -- l'apertura costa piu' della richiesta: misurate,
    80.1 ms contro 1.5 ms. Una sola chiamata dimenticata qui non rompe niente e
    non si vede: paga solo il suo handshake, in silenzio. Per questo la si
    cerca nel sorgente invece di aspettarsi che salti fuori da sola.
    """
    import re

    sorgente = (ROOT / "core" / "backend.py").read_text(encoding="utf-8")
    fuori = re.findall(r"(?<!\.)\bhttpx\.(get|post|stream)\s*\(", sorgente)
    assert not fuori, (
        f"{len(fuori)} chiamate httpx di modulo ({', '.join(sorted(set(fuori)))}): "
        f"vanno passate da get_client(), o non c'e' pooling"
    )


def test_il_client_e_lo_stesso_per_backend_diversi():
    """Il pool serve solo se sopravvive all'istanza del backend.

    Il server ricostruisce il backend a ogni richiesta HTTP -- e' la stessa
    ragione per cui ``_MODEL_INFO_CACHE`` e' di modulo. Un client di istanza
    verrebbe buttato insieme al backend che l'ha creato: sarebbe di nuovo una
    connessione per richiesta, con in piu' l'illusione di avere un pool.
    """
    uno = backend_mod.OllamaBackend("http://localhost:11434")
    due = backend_mod.OllamaBackend("http://altra-macchina:11434")
    assert uno is not due
    assert backend_mod.get_client() is backend_mod.get_client()
    # E dopo la chiusura se ne costruisce uno nuovo, invece di usarne uno chiuso.
    vecchio = backend_mod.get_client()
    backend_mod.chiudi_client()
    assert backend_mod.get_client() is not vecchio
