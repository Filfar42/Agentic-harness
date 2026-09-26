"""Il menu delle impostazioni: uno schema, e quello che il menu dice e' vero.

Il menu era cinquanta campi scritti a mano in cinque schede, con spiegazioni
che raccontavano la storia del codice e voci che valevano solo per un server
mostrate con tutti. Dal 26/09/2026 e' costruito da uno schema
(``web/impostazioni.js``): questi test tengono insieme lo schema, ``DEFAULTS``
e le rotte che il menu usa.

Tre famiglie:

* **ogni impostazione ha un posto** -- nello schema o in ``IMP_FUORI_MENU``
  con il perche' -- e i controlli sanno rappresentare i valori di serie;
* **le frasi del menu sono vere**: i numeri che citano (6 punti, 6 e 10
  passi, 5-7 mila token, 60 memorie) sono quelli del codice;
* **le rotte**: valori di serie, prompt, esporta, importa, diagnostica.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import DEFAULTS
from tests.test_server import client, fake_ollama  # noqa: F401

WEB = Path(__file__).resolve().parents[1] / "web"


def menu_js() -> str:
    return (WEB / "impostazioni.js").read_text(encoding="utf-8")


def blocco(sorgente: str, inizio: str, fine: str = "\n}") -> str:
    testo = sorgente[sorgente.index(inizio):]
    return testo[: testo.index(fine) + len(fine)]


def contesto_js():
    """Lo schema e le funzioni pure del menu, eseguiti davvero.

    Si valuta il tratto da ``IMP_SERVER`` a ``IMP_CAMPI`` (definizioni: le
    funzioni dentro lo schema vengono chiamate solo nel browser) piu' le
    funzioni pure che servono ai test.
    """
    quickjs = pytest.importorskip("quickjs")
    js = menu_js()
    inizio = js.index("const IMP_SERVER = ")
    fine = js.index("// Stato della finestra")
    schema = js[inizio:fine]
    funzioni = []
    for nome in ("impNumero", "impTrasforma", "impNormalizza", "impParole", "impRadice",
                 "impTrova", "impEvidenzia", "impFormatta", "impNumeroIt"):
        trovata = re.search(rf"function {nome}\([^)]*\) \{{.*?\n\}}", js, re.S)
        assert trovata, f"funzione {nome} non trovata"
        funzioni.append(trovata.group(0))
    app = (WEB / "app.js").read_text(encoding="utf-8")
    esc = re.search(r"function esc\([^)]*\) \{.*?\n\}", app, re.S).group(0)
    ctx = quickjs.Context()
    ctx.eval("\n".join([esc, schema, *funzioni,
                        "globalThis.IMP_CAMPI = IMP_CAMPI;",
                        "globalThis.IMP_SEZIONI = IMP_SEZIONI;",
                        "globalThis.IMP_FUORI_MENU = IMP_FUORI_MENU;",
                        "globalThis.IMP_DOPO = IMP_DOPO;",
                        "globalThis.IMP_PESO_FISSO = IMP_PESO_FISSO;"]))
    return ctx


def campi(ctx) -> list[dict]:
    # ``?? null``: JSON.stringify lascia fuori le chiavi undefined, e il test
    # deve poter distinguere "assente" da "chiave che non c'e'".
    return json.loads(ctx.eval(
        "JSON.stringify(IMP_CAMPI.map((c) => ({k: c.k, tipo: c.tipo, min: c.min ?? null,"
        " max: c.max ?? null, passo: c.passo ?? null, opzioni: c.opzioni ?? null,"
        " numerico: !!c.numerico, solo: c.solo ?? null, dipende: c.dipende ?? null,"
        " ripristino: c.ripristino ?? null, etichetta: c.etichetta,"
        " descr: typeof c.descr === 'string' ? c.descr : null})))"
    ))


# ---------------------------------------------------------------------------
# Ogni impostazione ha un posto
# ---------------------------------------------------------------------------


def test_ogni_impostazione_ha_un_posto():
    """Una chiave nuova in DEFAULTS non entra senza che qualcuno decida dove va.

    O e' una voce del menu, o sta in ``IMP_FUORI_MENU`` con il motivo. Il
    prompt di sistema non e' in DEFAULTS: ha la sua sezione (Istruzioni).
    """
    ctx = contesto_js()
    nel_menu = [c["k"] for c in campi(ctx)]
    fuori = set(json.loads(ctx.eval("JSON.stringify(Object.keys(IMP_FUORI_MENU))")))
    assert len(nel_menu) == len(set(nel_menu)), "una voce compare due volte nello schema"
    assert not set(nel_menu) & fuori, "una chiave e' sia nel menu sia fuori"
    sconosciute = (set(nel_menu) | fuori) - set(DEFAULTS)
    assert not sconosciute, f"chiavi che DEFAULTS non ha: {sorted(sconosciute)}"
    senza_posto = set(DEFAULTS) - set(nel_menu) - fuori
    assert not senza_posto, (
        f"impostazioni senza una voce e senza un motivo in IMP_FUORI_MENU: {sorted(senza_posto)}"
    )


def test_ogni_voce_fuori_menu_dice_perche():
    ctx = contesto_js()
    motivi = json.loads(ctx.eval("JSON.stringify(IMP_FUORI_MENU)"))
    assert all(isinstance(m, str) and len(m) > 15 for m in motivi.values()), motivi


def test_i_valori_di_serie_stanno_nei_limiti_del_controllo():
    """Un valore di serie fuori dal cursore non si puo' ne' mostrare ne' rimettere.

    Era il caso di "Max passi agentici": il campo arrivava a 40 e il valore
    salvato era 100, quindi il campo nasceva gia' non valido.
    """
    for campo in campi(contesto_js()):
        if campo["tipo"] not in ("numero", "cursore"):
            continue
        valore = DEFAULTS[campo["k"]]
        assert campo["min"] <= valore <= campo["max"], (campo["k"], valore)
        if campo["tipo"] == "cursore":
            passi = (valore - campo["min"]) / campo["passo"]
            assert abs(passi - round(passi)) < 1e-9, f"{campo['k']}: {valore} fuori dalla griglia"


def test_i_passi_per_turno_arrivano_oltre_i_cento():
    """Il valore in uso sulla postazione e' 100: il campo deve poterlo tenere."""
    campo = next(c for c in campi(contesto_js()) if c["k"] == "max_agent_loops")
    assert campo["max"] >= 100


def test_le_scelte_contengono_il_valore_di_serie():
    for campo in campi(contesto_js()):
        if campo["opzioni"]:
            valori = [str(v) for v, _ in campo["opzioni"]]
            assert str(DEFAULTS[campo["k"]]) in valori, (campo["k"], valori)


def test_le_opzioni_sono_quelle_che_il_codice_accetta():
    from core.config import THINK_LEVELS
    from server.main import _selezione_da_impostazioni

    opzioni = {c["k"]: [v for v, _ in c["opzioni"]] for c in campi(contesto_js()) if c["opzioni"]}
    assert set(opzioni["transport"]) == {"auto", "ollama", "llamacpp", "openai"}
    assert set(opzioni["native_think"]) <= {"auto", "si", "no", *THINK_LEVELS}
    assert set(THINK_LEVELS) <= set(opzioni["native_think"])
    assert set(opzioni["sandbox"]) == {"docker", "host"}
    for modo in opzioni["compattazione_selettiva"]:
        # un valore che il server non riconosce ricadrebbe su "spenta" in silenzio
        scelto, _ = _selezione_da_impostazioni({"compattazione_selettiva": modo,
                                                 "laya_url": "http://127.0.0.1:1/v1/systemone"})
        assert (scelto == "spenta") == (modo == "spenta"), (modo, scelto)


def test_solo_nomina_server_che_esistono():
    from core.backend import LlamaCppBackend, OllamaBackend, OpenAICompatBackend

    veri = {OllamaBackend.name, LlamaCppBackend.name, OpenAICompatBackend.name}
    ctx = contesto_js()
    nomi = set(json.loads(ctx.eval("JSON.stringify(Object.keys(IMP_SERVER))")))
    assert nomi == veri
    for campo in campi(ctx):
        assert set(campo["solo"] or []) <= veri, campo["k"]
    blocchi = json.loads(ctx.eval(
        "JSON.stringify(IMP_SEZIONI.flatMap((s) => s.blocchi).filter((b) => b.solo).map((b) => b.solo))"
    ))
    assert all(set(solo) <= veri for solo in blocchi)


def test_i_campionatori_che_openai_non_riceve_non_si_mostrano_con_openai():
    """Il transport OpenAI-compatibile manda temperature e top_p e basta."""
    per_chiave = {c["k"]: c for c in campi(contesto_js())}
    for k in ("top_k", "presence_penalty", "repetition_penalty"):
        assert "openai" not in (per_chiave[k]["solo"] or []), k
        assert per_chiave[k]["solo"], k
    for k in ("temperature", "top_p"):
        assert not per_chiave[k]["solo"], k


def test_ogni_dipendenza_punta_a_un_interruttore():
    per_chiave = {c["k"]: c for c in campi(contesto_js())}
    for campo in per_chiave.values():
        if campo["dipende"]:
            assert per_chiave[campo["dipende"]]["tipo"] == "interruttore", campo["k"]


def test_i_ricontrolli_dopo_il_salvataggio_sono_di_chiavi_vere():
    ctx = contesto_js()
    chiavi = set(json.loads(ctx.eval("JSON.stringify(Object.keys(IMP_DOPO))")))
    assert chiavi <= set(DEFAULTS) | {"system_prompt"}


def test_i_campi_numerici_salvano_il_tipo_del_default():
    """Un ``<input type="number">`` restituisce una stringa, e il server vuole il
    tipo del default: un intero per num_ctx anche se si scrive "4096.0"."""
    ctx = contesto_js()
    for campo in campi(ctx):
        numerico = campo["tipo"] in ("numero", "cursore") or campo["numerico"]
        if not numerico:
            continue
        k = campo["k"]
        valore = DEFAULTS[k]
        grezzo = f"{valore}.0" if isinstance(valore, int) else str(valore)
        out = ctx.eval(
            f"(() => {{ const c = IMP_CAMPI.find((x) => x.k === {json.dumps(k)});"
            f" return JSON.stringify(impTrasforma(c)({json.dumps(grezzo)})); }})()"
        )
        assert json.loads(out) == valore, (k, out)
        if isinstance(valore, int):
            assert "." not in out, f"{k}: {out} diventerebbe un float e il server lo rifiuterebbe"


def test_un_numero_non_valido_non_si_salva():
    ctx = contesto_js()
    prova = ("(() => { const c = IMP_CAMPI.find((x) => x.k === 'max_agent_loops');"
             " const t = impTrasforma(c); return JSON.stringify([t(''), t('abc'), t('0'),"
             " t('501'), t('12'), t('12,0')]); })()")
    assert ctx.eval(prova) == "[null,null,null,null,12,12]"


# ---------------------------------------------------------------------------
# Ricerca
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("testo", "cercato", "atteso"),
    [
        ("Prima porta", "porta", True),
        ("Quante porte", "porta", True),        # singolare e plurale
        ("Comportamento", "porta", False),      # non dentro le parole
        ("Esporta o importa", "porta", False),
        ("num_ctx contesto", "ctx", True),       # le chiavi valgono a pezzi
        ("num_ctx contesto", "num_ctx", True),
        ("Finestra di contesto", "finestra contesto", True),
        ("Finestra di contesto", "finestra memoria", False),
        ("Penalità di ripetizione", "penalita", True),   # senza accenti
    ],
)
def test_la_ricerca_trova_le_parole_e_non_i_pezzi(testo, cercato, atteso):
    ctx = contesto_js()
    termini = json.dumps(cercato.split())
    trovato = ctx.eval(f"impTrova(impParole({json.dumps(testo)}), {termini})")
    assert trovato is atteso


def test_l_evidenziazione_segna_la_parola_trovata_ed_e_escapata():
    ctx = contesto_js()
    assert ctx.eval("impEvidenzia('Prima porta', ['porte'])") == "Prima <mark>porta</mark>"
    assert "<b>" not in ctx.eval("impEvidenzia('<b>porta</b>', ['porta'])")


# ---------------------------------------------------------------------------
# Le frasi del menu sono vere
# ---------------------------------------------------------------------------


def _descr(k: str) -> str:
    return next(c["descr"] for c in campi(contesto_js()) if c["k"] == k)


def test_il_cancello_del_piano_scatta_dove_dice_il_menu():
    from core.agent import PUNTI_PER_IL_CANCELLO

    assert f"Da {PUNTI_PER_IL_CANCELLO} punti" in _descr("plan_gate")


def test_il_piano_si_chiede_sulle_richieste_lunghe():
    """"Se una richiesta lunga elenca piu' passi, piu' azioni o piu' file"."""
    from core.ciclo.segnali import looks_multi_step

    assert not looks_multi_step("1. leggi a.py 2. correggi b.py")      # corta
    assert looks_multi_step("Fai queste cose, con calma e controllando ogni passo.\n"
                            "1. Leggi il modulo di configurazione e capisci come carica i valori.\n"
                            "2. Correggi il caricamento dei default quando manca il file.\n"
                            "3. Aggiungi un test che lo provi davvero.")
    assert "richiesta lunga" in _descr("require_plan")


def test_il_monitor_scatta_dove_dice_il_menu():
    from core.ciclo.avanzamento import CHIUDI_DOPO, RIORIENTA_DOPO

    descr = _descr("monitor_avanzamento")
    assert f"A {RIORIENTA_DOPO} lo dice" in descr and f"a {CHIUDI_DOPO} chiude" in descr


def test_la_soglia_di_serie_e_quella_citata():
    from core.config import HISTORY_COMPACT_THRESHOLD

    assert f"A {round(HISTORY_COMPACT_THRESHOLD * 100)}%" in _descr("compact_threshold")


def test_il_ragionamento_automatico_parte_dal_livello_citato():
    from core.config import THINK_AUTO_LEVEL

    nomi = {"low": "basso", "medium": "medio", "high": "alto", "max": "massimo"}
    assert f"a livello {nomi[THINK_AUTO_LEVEL]}" in _descr("native_think")


def test_il_peso_fisso_del_contesto_e_quello_citato():
    """"Prompt di sistema e schemi dei tool ne occupano gia' 5-7 mila": se il
    prompt o gli schemi crescono, la frase va aggiornata insieme."""
    from core.prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_LEAN
    from core.textutils import estimate_tokens
    from core.tools import TOOLS_SCHEMA, TOOLS_SCHEMA_LEAN

    assert "5-7 mila" in contesto_js().eval("IMP_PESO_FISSO")
    snello = estimate_tokens(SYSTEM_PROMPT_LEAN) + estimate_tokens(json.dumps(TOOLS_SCHEMA_LEAN))
    esteso = estimate_tokens(SYSTEM_PROMPT) + estimate_tokens(json.dumps(TOOLS_SCHEMA))
    assert 5000 <= snello <= 7000 and 5000 <= esteso <= 7000, (snello, esteso)


def test_i_limiti_delle_memorie_sono_quelli_del_codice():
    from core.memory import MAX_MEMORIES, MAX_MEMORY_CHARS

    js = menu_js()
    assert f"const IMP_MEMORIE_MAX = {MAX_MEMORIES};" in js
    assert f"const IMP_MEMORIA_CARATTERI = {MAX_MEMORY_CHARS};" in js


def test_il_profilo_mostra_le_chiavi_che_applica():
    from core import profiles

    profilo = profiles.profile_for("qwen3.8:27b", thinking=True)
    chiavi = set(profiles.as_settings(profilo, num_ctx=32768))
    mostrate = re.search(r"const IMP_CHIAVI_PROFILO = \[([^\]]*)\]", menu_js()).group(1)
    assert set(re.findall(r"'([a-z_]+)'", mostrate)) == chiavi


def test_la_regia_del_pensiero_continua_solo_dove_dice_il_menu():
    """"Con Ollama il pensiero si chiude e il modello passa all'azione; con gli
    altri server si interrompe solo oltre il 55%, e il passo si rifa'"."""
    from core.agent import WATCHDOG_RATIO
    from core.backend import LlamaCppBackend, OllamaBackend, OpenAICompatBackend

    assert getattr(OllamaBackend, "supports_think_continuation", False) is True
    for altro in (LlamaCppBackend, OpenAICompatBackend):
        assert getattr(altro, "supports_think_continuation", False) is not True, altro
    descr = _descr("think_watchdog")
    assert "Con Ollama" in descr and f"{round(WATCHDOG_RATIO * 100)}%" in descr


# ---------------------------------------------------------------------------
# Forma del frontend
# ---------------------------------------------------------------------------


def _html() -> str:
    raw = (WEB / "index.html").read_text(encoding="utf-8")
    return re.sub(r"<!--.*?-->", "", raw, flags=re.S)


def test_nessun_campo_scritto_a_mano_nella_pagina():
    """Il menu ha una fonte sola, lo schema: un campo aggiunto a mano in
    index.html salterebbe ricerca, ripristino e visibilita' per server."""
    html = _html()
    assert 'id="s-' not in html
    assert 'id="impostazioni"' in html and 'role="dialog"' in html and 'aria-modal="true"' in html


def test_il_menu_si_carica_prima_di_app_js():
    html = _html()
    assert html.index("/static/impostazioni.js") < html.index("/static/app.js")
    assert "/static/impostazioni.css" in html


def test_esc_nel_menu_non_arriva_al_documento():
    """Sul documento Esc chiude anche l'anteprima e le tendine: chiudere le
    impostazioni non deve portarsi dietro il pannello che sta sotto."""
    corpo = blocco(menu_js(), "function impTastiera(")
    esc_ramo = corpo[corpo.index("event.key === 'Escape'"):]
    esc_ramo = esc_ramo[: esc_ramo.index("return;")]
    assert "event.stopPropagation()" in esc_ramo


def test_ctrl_virgola_apre_il_menu():
    corpo = blocco(menu_js(), "function impCollega(")
    assert "event.key === ','" in corpo and "(event.ctrlKey || event.metaKey)" in corpo


def test_il_testo_si_salva_quando_si_smette_di_scrivere():
    """L'indirizzo del server salvato a ogni tasto ricostruiva il backend e
    rifaceva i controlli di prontezza per ogni cifra di un IP."""
    app = (WEB / "app.js").read_text(encoding="utf-8")
    corpo = blocco(app, "function bindField(")
    assert "ritardo" in corpo and "setTimeout(salva, ritardo)" in corpo
    assert "if (ritardo) node.addEventListener('change', salva)" in corpo
    ritardi = re.search(r"const IMP_RITARDO = \{([^}]*)\}", menu_js()).group(1)
    for tipo in ("testo", "modello", "segreto", "numero"):
        assert re.search(rf"{tipo}: [1-9]\d{{2,}}", ritardi), tipo


def test_un_salvataggio_fallito_non_resta_nello_stato():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    corpo = blocco(app, "async function saveSettings(")
    assert "catch (error)" in corpo and "state.settings[k] = prima[k]" in corpo
    assert "throw error;" in corpo


# ---------------------------------------------------------------------------
# Rotte
# ---------------------------------------------------------------------------


def test_i_valori_di_serie_non_contengono_stato_ne_percorsi(client):
    dati = client.get("/api/settings/meta").json()
    defaults = dati["defaults"]
    assert defaults["num_ctx"] == DEFAULTS["num_ctx"]
    for chiave in ("workspace_dir", "recent_workspaces", "progetti", "mobile_token",
                   "agent_running", "pending_prompt", "last_usage"):
        assert chiave not in defaults, chiave
    assert "api_key" in dati["fuori_dallo_scambio"]


def test_l_esportazione_non_porta_segreti_ne_percorsi(client):
    client.post("/api/settings", json={"values": {"api_key": "sk-segreta", "temperature": 0.4}})
    dati = client.get("/api/settings/export").json()
    assert dati["formato"] == "astra-impostazioni" and dati["versione"] == 1
    valori = dati["impostazioni"]
    assert valori["temperature"] == 0.4
    for chiave in ("api_key", "mobile_token", "workspace_dir", "recent_workspaces",
                   "progetti", "docker_image", "agent_running", "last_usage"):
        assert chiave not in valori, chiave
    assert "sk-segreta" not in json.dumps(dati)
    # il prompt di serie non si esporta: congelarlo sull'altra macchina e' la
    # trappola descritta in core/settings.py
    assert "system_prompt" not in valori


def test_l_esportazione_porta_il_prompt_personalizzato(client):
    client.post("/api/settings", json={"values": {"system_prompt": "Sei un pirata."}})
    assert client.get("/api/settings/export").json()["impostazioni"]["system_prompt"] == "Sei un pirata."


def test_la_prova_d_importazione_non_cambia_niente(client):
    prima = client.get("/api/bootstrap").json()["settings"]
    risposta = client.post("/api/settings/import", json={
        "impostazioni": {"formato": "astra-impostazioni", "versione": 1, "impostazioni": {
            "temperature": 0.33, "api_key": "sk-x", "inventata": 1, "num_ctx": "tanto",
            "top_k": prima["top_k"],
        }},
        "prova": True,
    }).json()
    assert risposta["diverse"] == ["temperature"]
    assert risposta["valori"] == {"temperature": 0.33}
    motivi = {i["chiave"]: i["motivo"] for i in risposta["ignorate"]}
    assert set(motivi) == {"api_key", "inventata", "num_ctx"}
    assert "segreto" in motivi["api_key"] and "atteso int" in motivi["num_ctx"]
    dopo = client.get("/api/bootstrap").json()["settings"]
    assert dopo == prima


def test_l_importazione_applica_e_persiste(client):
    from core import settings as settings_mod

    risposta = client.post("/api/settings/import", json={"impostazioni": {
        "temperature": 0.33, "max_agent_loops": 100, "api_key": "sk-x"}})
    assert risposta.status_code == 200
    dati = risposta.json()
    assert sorted(dati["diverse"]) == ["max_agent_loops", "temperature"]
    assert dati["settings"]["temperature"] == 0.33
    assert dati["settings"]["api_key"] != "sk-x"
    salvate = json.loads(settings_mod.SETTINGS_FILE.read_text(encoding="utf-8"))
    assert salvate["max_agent_loops"] == 100


def test_un_file_che_non_e_un_oggetto_viene_rifiutato(client):
    risposta = client.post("/api/settings/import", json={"impostazioni": [1, 2, 3]})
    assert risposta.status_code == 400
    assert "oggetto JSON" in risposta.json()["detail"]


def test_esportare_e_reimportare_non_cambia_niente(client):
    client.post("/api/settings", json={"values": {"temperature": 0.45, "plan_gate": False}})
    esportate = client.get("/api/settings/export").json()
    prova = client.post("/api/settings/import", json={"impostazioni": esportate, "prova": True}).json()
    assert prova["diverse"] == [] and prova["ignorate"] == []


def test_il_prompt_dice_se_e_di_serie_e_quale(client):
    info = client.get("/api/settings/prompt").json()
    assert info["di_serie"] is True
    assert info["attivo"] in ("esteso", "snello")
    assert info["base_attiva"] == info["base"][info["attivo"]]
    assert info["token_effettivo"] > 0

    client.post("/api/settings", json={"values": {"system_prompt": "Sei un pirata."}})
    info = client.get("/api/settings/prompt").json()
    assert info["di_serie"] is False and info["attivo"] == "personalizzato"
    assert info["base_attiva"] == "Sei un pirata."
    assert info["effettivo"].startswith("Sei un pirata.")

    client.post("/api/settings", json={"values": {"system_prompt": ""}})
    assert client.get("/api/settings/prompt").json()["di_serie"] is True


def test_la_diagnostica_copre_i_segreti_e_non_usa_la_rete(client, monkeypatch):
    from core import settings as settings_mod

    client.post("/api/settings", json={"values": {"api_key": "sk-segreta", "max_agent_loops": 77}})

    def vietato(*_a, **_k):
        raise AssertionError("la diagnostica non deve interrogare il server del modello")

    monkeypatch.setattr(client.server.STATE, "backend", vietato)
    dati = client.get("/api/diagnostics").json()
    assert dati["diverse_dal_default"]["api_key"] == "(impostata)"
    assert dati["diverse_dal_default"]["max_agent_loops"] == 77
    assert "sk-segreta" not in json.dumps(dati)
    assert dati["percorsi"]["impostazioni"] == str(settings_mod.SETTINGS_FILE.resolve())
    assert dati["app"]["versione"]
    assert isinstance(dati["conversazioni"], int) and isinstance(dati["memorie"], int)


def test_menu_e_importazione_passano_dalla_stessa_porta():
    """Le conseguenze di un cambio (backend, container, prontezza, cartella)
    stanno in ``_applica_impostazioni``: una seconda porta che le copiasse se ne
    dimenticherebbe una."""
    sorgente = (Path(__file__).resolve().parents[1] / "server" / "main.py").read_text(encoding="utf-8")
    menu = blocco(sorgente, "def update_settings(", "\n\n\n")
    importa = blocco(sorgente, "def import_settings(", "\n\n\n")
    assert "_applica_impostazioni(" in menu and "_applica_impostazioni(" in importa
    assert sorgente.count("STATE.forget_backend()\n") >= 1
    assert "forget_backend" not in menu and "forget_backend" not in importa


def test_i_ridisegni_non_tolgono_il_fuoco():
    """Il menu si riallinea a ogni salvataggio e a ogni controllo della goccia
    (venti secondi): riscrivere i controlli ogni volta toglieva il fuoco a chi
    li usava con la tastiera. I segmenti si aggiornano sul posto; stati,
    profilo e piedi delle sezioni si riscrivono solo se cambiano."""
    js = menu_js()
    segmenti = blocco(js, "function impSegmenti(")
    assert "const uguali = bottoni.length === opzioni.length" in segmenti
    for funzione in ("function impDisegnaStatoServer(", "function impDisegnaStatoSandbox(",
                     "function impDisegnaProfilo(", "function impPiedeSezione("):
        assert "impAggiornaHtml(" in blocco(js, funzione), funzione
    assert "nodo._html === html" in blocco(js, "function impAggiornaHtml(")
