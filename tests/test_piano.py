"""Test del piano di lavoro e delle difese contro il ragionamento infinito.

Il caso che ha generato tutto questo, ricostruito da una conversazione vera:
prompt da 7.000 caratteri con cinque step numerati, il modello prova a
risolverli tutti nel canale di pensiero, sbatte contro ``num_predict`` a ~8.200
token e chiude il turno **senza aver emesso una sola tool call**. Due volte di
fila. L'harness non se ne accorgeva: leggeva "niente tool call e niente
risposta" come "ha risposto a parole" e gli rispondeva "esegui l'azione", che
davanti a una risposta vuota non vuol dire niente. La seconda volta non e'
scattato nemmeno quello, e il turno si e' chiuso in silenzio.

Le tre difese, testate qui: il piano (stato fuori dal ragionamento), il
watchdog (interrompe prima del tetto) e il rilevamento del troncamento
(riconosce il caso quando succede lo stesso).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tests.test_agent_loop as fake
from core import agent as agent_mod
from core.backend import OllamaBackend
from core.config import GenParams
from core.plan import DOING, DONE, Plan, PlanError, render_block, render_summary
from core.textutils import chars_for_tokens
from core.tools import PLAN_TOOL, TOOLS_SCHEMA, ToolContext, dispatch
from core.verifiche import RegistroVerifiche

fake_ollama = fake.fake_ollama


def piano(*testi: str) -> Plan:
    p = Plan()
    p.set_steps(list(testi))
    return p


# ---------------------------------------------------------------------------
# Regole del piano
# ---------------------------------------------------------------------------


def test_un_solo_punto_aperto_per_volta():
    """La guardia centrale: e' il "faccio tutto insieme" reso impossibile.

    Senza, il piano diventerebbe un travestimento: cinque punti aperti tutti
    insieme sono esattamente il comportamento che si voleva eliminare, solo con
    una lista accanto.
    """
    p = piano("leggere", "correggere", "testare")
    p.start("1")
    with pytest.raises(PlanError) as errore:
        p.start("2")
    # L'errore deve dire cosa fare, non solo che e' andata male.
    assert "complete" in str(errore.value)
    assert p.current.id == "1"


def test_chiuso_il_primo_si_puo_aprire_il_secondo():
    p = piano("leggere", "correggere")
    p.start("1")
    p.complete("1", note="2 file letti")
    p.start("2")
    assert p.current.id == "2"
    assert p.get("1").status == DONE
    assert p.get("1").note == "2 file letti"


def test_rivedere_il_piano_non_azzera_i_punti_gia_chiusi():
    """Scoprire lavoro nuovo a meta' strada e' normale, ricominciare no.

    Se un action='set' azzerasse i punti chiusi, il modello rifarebbe lavoro
    gia' fatto -- o lo dichiarerebbe da fare e poi lo salterebbe.
    """
    p = piano("leggere", "correggere")
    p.start("1")
    p.complete("1", note="fatto")
    p.set_steps(["leggere", "correggere", "aggiungere i test mancanti"])
    assert p.get("1").status == DONE
    assert p.get("1").note == "fatto"
    assert len(p.steps) == 3


def test_un_punto_riformulato_e_un_punto_nuovo():
    """Il confronto e' sul testo esatto: grezzo, ma senza falsi positivi.

    Riformulare aggiunge un punto; **non** riapre e non cancella quello chiuso,
    che resta a dire cosa e' stato fatto davvero.
    """
    p = piano("leggere i file")
    p.complete("1")
    p.set_steps(["leggere i file di partenza"])
    assert p.get("1").status == DONE
    assert p.get("1").text == "leggere i file"
    nuovo = p.get("2")
    assert nuovo is not None and nuovo.text == "leggere i file di partenza"


def test_un_set_non_puo_cancellare_il_lavoro_gia_chiuso():
    """Il caso reale (sessione 19/08/2026): cinque ``set`` in un'unica
    conversazione, ventitre punti chiusi lungo la strada, sei sopravvissuti nel
    file. Il pannello raccontava un quinto del lavoro fatto, e il modello --
    che quel blocco lo rilegge ad ogni passo -- non sapeva piu' cosa avesse
    gia' finito."""
    p = piano("scrivere todo.py", "scrivere i test")
    p.start("1")
    p.complete("1", "fatto")
    p.start("2")
    p.skip("2", "rimandato")

    # L'utente chiede altro: il modello ripianifica da zero, con parole nuove.
    p.set_steps(["aggiungere la UI web", "aggiornare il README"])

    assert [s.text for s in p.steps] == [
        "scrivere todo.py", "scrivere i test", "aggiungere la UI web", "aggiornare il README",
    ]
    assert [s.status for s in p.steps] == ["done", "skipped", "doing", "todo"] or \
           [s.status for s in p.steps] == ["done", "skipped", "todo", "todo"]
    assert p.get("1").note == "fatto" and p.get("2").note == "rimandato"
    # I numeri dei punti chiusi non si riciclano: il 3 e il 4 sono nuovi.
    assert [s.id for s in p.steps] == ["1", "2", "3", "4"]


def test_i_punti_chiusi_non_contano_per_il_tetto_del_piano():
    """Dodici cose da fare insieme sono ingestibili; dodici cose gia' fatte
    sono un registro. Contarle insieme costringeva a cancellare la storia per
    poter pianificare ancora."""
    p = Plan()
    p.set_steps([f"punto {i}" for i in range(1, 13)])
    for i in range(1, 13):
        p.complete(str(i))
    p.set_steps([f"nuovo {i}" for i in range(1, 6)])
    assert len(p.open_steps) == 5
    assert len(p.closed_steps) == 12


def test_nel_blocco_di_contesto_la_storia_non_cresce_senza_fine():
    """I chiusi restano nel piano, ma nel testo riletto ad ogni passo entrano
    solo gli ultimi: altrimenti il costo di un passo crescerebbe con la
    lunghezza della conversazione -- il difetto che il piano toglieva."""
    p = Plan()
    p.set_steps([f"punto {i}" for i in range(1, 11)])
    for i in range(1, 10):
        p.complete(str(i))
    blocco = render_block(p)
    assert "1. [fatto]" not in blocco       # i piu' vecchi restano fuori
    assert "9. [fatto]" in blocco           # gli ultimi chiusi ci sono
    assert "10. [da fare]" in blocco        # e tutto cio' che resta aperto
    assert "5 punti chiusi prima di questi" in blocco


def test_il_piano_non_puo_diventare_il_compito_riscritto():
    p = Plan()
    with pytest.raises(PlanError):
        p.set_steps([f"punto {i}" for i in range(30)])
    with pytest.raises(PlanError):
        p.set_steps([])


def test_un_piano_da_disco_malformato_non_impedisce_di_aprire_la_chat():
    p = Plan.from_list([{"text": "buono"}, "spazzatura", {"status": "boh"}, {"text": ""}])
    assert [s.text for s in p.steps] == ["buono"]
    assert p.steps[0].status == "todo"


# ---------------------------------------------------------------------------
# Il tool
# ---------------------------------------------------------------------------


def ctx_vuoto(tmp_path) -> ToolContext:
    return ToolContext(workspace=str(tmp_path), sandbox="host")


def ctx_col_registro(tmp_path) -> tuple[ToolContext, RegistroVerifiche]:
    """Come lo monta il ciclo agentico: il registro vive fuori dai tool.

    Senza registro il ``ToolContext`` sa solo ``red_command``, cioe' un comando
    per volta: basta per il guard sui file di test, non per distinguere due
    rossi indipendenti. I test che parlano di *quali* verifiche restano aperte
    devono montarlo, o misurerebbero la vista ridotta invece del contratto.
    """
    ctx = ToolContext(workspace=str(tmp_path), sandbox="host")
    registro = RegistroVerifiche()
    ctx.verification = registro
    return ctx, registro


def rosso(registro: RegistroVerifiche, ctx: ToolContext, comando: str, code: int = 1) -> None:
    registro.record("run_command", json.dumps(
        {"command": comando, "returncode": code, "esito": "FALLITO"}))
    aperte = registro.pendenti
    ctx.red_command = aperte[0].comando if aperte else None


def test_il_tool_rifiuta_stringhe_al_posto_di_array(tmp_path):
    """Strict arguments must not silently coerce an invalid model call."""
    ctx = ctx_vuoto(tmp_path)
    result = json.loads(dispatch(ctx, PLAN_TOOL, {"action": "set", "steps": "- leggere\n- correggere"}))
    assert result["error_code"] == "invalid_arguments"
    assert ctx.plan.steps == []
    dispatch(ctx, PLAN_TOOL, {"action": "set", "steps": ["leggere", "correggere"]})
    assert [s.text for s in ctx.plan.steps] == ["leggere", "correggere"]


def test_un_punto_si_chiude_anche_con_una_verifica_rossa(tmp_path):
    """Avanzamento e qualita' sono due fatti, e possono contraddirsi.

    Qui il piano si bloccava: nessun punto si chiudeva finche' esisteva un
    comando rosso. Sembrava prudenza, era una confusione fra "ho finito questa
    attivita'" e "la suite e' verde". In TDD il rosso iniziale **e'** il
    risultato atteso di "riprodurre il bug": rifiutare quella chiusura
    impediva al piano di dire la verita', e siccome se ne apre uno per volta il
    modello restava senza mosse legittime. Osservato: rilanciava lo stesso
    comando finche' i passi non finivano.
    """
    ctx, registro = ctx_col_registro(tmp_path)
    dispatch(ctx, PLAN_TOOL, {"action": "set", "steps": ["riprodurre il bug", "correggerlo"]})
    rosso(registro, ctx, "pytest tests/test_bug.py -q")

    risposta = json.loads(dispatch(ctx, PLAN_TOOL, {"action": "complete", "step_id": "1"}))
    assert risposta["status"] == "ok"
    assert ctx.plan.get("1").status == DONE

    # Il rosso non e' sparito: viaggia accanto al risultato, dove il modello lo
    # legge nello stesso passo in cui chiude il punto.
    assert risposta["qualita"]["pendenti"][0]["comando"] == "pytest tests/test_bug.py -q"
    assert any("ancora" in a and "rossa" in a for a in risposta["avvisi"])
    assert registro.pendenti, "chiudere un punto non deve cancellare una verifica"

    # E il punto successivo si apre da solo: era proprio la mossa che mancava.
    assert risposta["aperto_in_automatico"]["id"] == "2"


def test_chiudere_senza_step_id_chiude_il_punto_in_corso(tmp_path):
    """Il punto lo sanno tutti e due: e' quello aperto, ed e' scritto in coda.

    Il modello finiva un punto e chiamava ``complete`` senza ``step_id``. Il
    piano rispondeva "Nel piano non c'e' nessun punto ''" -- formalmente vero e
    praticamente inutile: ce n'e' uno solo in corso per costruzione, e sta nel
    blocco che il modello ha appena letto. Su un modello piccolo quel
    round-trip finisce spesso in un secondo errore invece che nella risposta.
    """
    ctx = ctx_vuoto(tmp_path)
    dispatch(ctx, PLAN_TOOL, {"action": "set", "steps": ["leggere", "correggere"]})
    assert ctx.plan.current.id == "1"

    risposta = json.loads(dispatch(ctx, PLAN_TOOL, {"action": "complete", "note": "4 test verdi"}))
    assert risposta["status"] == "ok"
    assert ctx.plan.get("1").status == DONE
    assert ctx.plan.get("1").note == "4 test verdi"
    # Senza questa riga il modello dovrebbe dedurre al passo dopo su cosa ha
    # agito, che e' proprio il passo che si vuole risparmiare.
    assert risposta["punto_chiuso"] == {"id": "1", "text": "leggere"}
    assert any("step_id non indicato" in a for a in risposta["avvisi"])
    # E il successivo si apre da solo, come con l'id esplicito.
    assert risposta["aperto_in_automatico"]["id"] == "2"


def test_saltare_e_aprire_senza_step_id(tmp_path):
    ctx = ctx_vuoto(tmp_path)
    dispatch(ctx, PLAN_TOOL, {"action": "set", "steps": ["uno", "due"]})

    saltato = json.loads(dispatch(ctx, PLAN_TOOL, {"action": "skip", "note": "non serve piu'"}))
    assert ctx.plan.get("1").status == "skipped"
    assert saltato["punto_chiuso"]["id"] == "1"

    # ``start`` senza id vale "riprendi da dove sei": un no-op sul punto aperto,
    # non un errore.
    ripreso = json.loads(dispatch(ctx, PLAN_TOOL, {"action": "start"}))
    assert ripreso["status"] == "ok"
    assert ctx.plan.get("2").status == DOING


def test_senza_punti_aperti_lo_step_id_mancante_torna_a_chiedere(tmp_path):
    """Quando l'intenzione non e' univoca non si indovina."""
    ctx = ctx_vuoto(tmp_path)
    vuoto = json.loads(dispatch(ctx, PLAN_TOOL, {"action": "complete"}))
    assert "non c'e' ancora un piano" in vuoto["error"].lower()

    dispatch(ctx, PLAN_TOOL, {"action": "set", "steps": ["uno"]})
    dispatch(ctx, PLAN_TOOL, {"action": "complete", "step_id": "1"})
    finito = json.loads(dispatch(ctx, PLAN_TOOL, {"action": "complete"}))
    assert "gia' chiusi" in finito["error"]

    # Piu' punti aperti e nessuno in corso: il caso in cui indovinare
    # significherebbe chiudere un punto al posto di un altro.
    ctx.plan.set_steps(["a", "b"])
    ambiguo = json.loads(dispatch(ctx, PLAN_TOOL, {"action": "complete"}))
    assert "non so quale intendi" in ambiguo["error"]
    assert "(a)" in ambiguo["hint"] and "(b)" in ambiguo["hint"]


def test_il_risultato_non_rimanda_indietro_il_piano(tmp_path):
    """Il piano sta gia' nel blocco di coda, rispedito integrale ad ogni passo.

    Rimandarlo anche nel risultato del tool vuol dire scrivere la stessa cosa
    due volte nella stessa richiesta. Misurato il 23/08/2026 sulle sessioni
    salvate: ``manage_plan`` valeva il 13,5% di tutti i token di risultato dei
    tool, terzo dopo read_file e run_command, per un'informazione che il
    modello aveva gia' davanti.
    """
    ctx = ctx_vuoto(tmp_path)
    testi = [f"punto numero {i} con un testo lungo abbastanza da pesare" for i in range(8)]
    esito = json.loads(dispatch(ctx, PLAN_TOOL, {"action": "set", "steps": testi}))

    assert esito["status"] == "ok"
    assert "plan" not in esito, "il piano non deve tornare nel risultato"
    # ...ma cio' che il blocco di coda **non** dice deve esserci: che
    # l'operazione e' riuscita e cosa e' cambiato adesso.
    assert esito["punti"] == 8
    assert esito["current"] == "1"
    assert esito["aperto_in_automatico"]["text"] == testi[0]

    # E il risultato deve pesare come una ricevuta, non come una copia.
    assert len(json.dumps(esito)) < len(json.dumps(ctx.plan.to_list())) / 2

    # `show` resta l'eccezione: e' il solo caso in cui il piano *e'* la
    # risposta, ed e' il modello a chiederlo esplicitamente.
    mostrato = json.loads(dispatch(ctx, PLAN_TOOL, {"action": "show"}))
    assert len(mostrato["plan"]) == 8


def test_il_tool_avvisa_chi_ascolta_quando_il_piano_cambia(tmp_path):
    visto: list[int] = []
    ctx = ctx_vuoto(tmp_path)
    ctx.on_plan_changed = lambda p: visto.append(len(p.steps))
    dispatch(ctx, PLAN_TOOL, {"action": "set", "steps": ["a", "b"]})
    assert visto == [2]


def test_un_errore_del_piano_non_avvisa_nessuno(tmp_path):
    """Un piano rifiutato non e' un piano cambiato: niente da persistere."""
    visto: list[int] = []
    ctx = ctx_vuoto(tmp_path)
    ctx.on_plan_changed = lambda p: visto.append(len(p.steps))
    dispatch(ctx, PLAN_TOOL, {"action": "start", "step_id": "9"})
    assert visto == []


# ---------------------------------------------------------------------------
# Blocco di contesto
# ---------------------------------------------------------------------------


def test_il_blocco_dice_su_cosa_stare_e_quanto_tempo_resta():
    p = piano("leggere", "correggere")
    p.start("2")
    blocco = render_block(p, steps_left=5)
    assert "punto 2" in blocco
    assert "non ripianificare" in blocco
    assert "rimasti in questo turno: 5" in blocco


def test_senza_piano_non_si_inietta_niente():
    assert render_block(Plan(), steps_left=3) == ""


def test_il_blocco_del_piano_va_in_fondo_e_non_e_di_sistema():
    """Le due condizioni per non invalidare il KV cache ad ogni 'complete'.

    In testa il piano sposterebbe il prefisso ad ogni modifica; con ruolo
    'system' finirebbe comunque in testa, perche' ``to_ollama_messages`` fonde
    tutti i blocchi di sistema in uno solo davanti.
    """
    msgs = agent_mod.build_api_messages(
        [{"role": "user", "content": "vai"}],
        system_prompt="S",
        env_header="E",
        plan_block="<piano_di_lavoro>\n1. [da fare] x\n</piano_di_lavoro>",
    )
    assert msgs[-1]["role"] == "user"
    assert "piano_di_lavoro" in msgs[-1]["content"]
    assert all("piano_di_lavoro" not in m["content"] for m in msgs if m["role"] == "system")


def test_il_riepilogo_conta_quello_che_e_stato_chiuso():
    p = piano("a", "b", "c")
    p.complete("1")
    p.skip("2", note="non serve")
    assert render_summary(p) == "1/3 fatti, 1 saltati, 1 aperti"


# ---------------------------------------------------------------------------
# Riconoscere una richiesta con piu' obiettivi
# ---------------------------------------------------------------------------


MULTI = """Sei un agente sviluppatore. Esegui i seguenti compiti in sequenza rigida.

--- STEP 1: Debugging ---
Analizza ed esegui la suite di test e correggi i tre bug strutturali che trovi.

--- STEP 2: Estensione ---
Aggiungi i campi timeout e fallback, e implementa la propagazione dello skip.
"""


def test_una_richiesta_a_step_numerati_e_multi_obiettivo():
    assert agent_mod.looks_multi_step(MULTI) is True


def test_una_richiesta_lunga_con_molti_verbi_e_multi_obiettivo():
    testo = (
        "Ti chiedo di sistemare il modulo di autenticazione: correggi il bug "
        "sul refresh del token, aggiungi il logging strutturato su ogni "
        "tentativo fallito e scrivi i test che coprono il caso di scadenza. "
        "Verifica poi che la suite resti verde e documenta le scelte fatte "
        "dentro il README del progetto, cosi' resta traccia."
    )
    assert agent_mod.looks_multi_step(testo) is True


@pytest.mark.parametrize(
    "testo",
    [
        "",
        "leggi il file config.py e dimmi cosa fa",
        "correggi il bug alla riga 42 di dag_executor.py",
    ],
)
def test_una_richiesta_semplice_non_pretende_un_piano(testo):
    """Falso positivo = un giro di manage_plan sprecato: fastidioso ma non grave.

    Falso negativo = si torna al problema di partenza. Meglio conservativa,
    ma non al punto di chiedere un piano per leggere un file.
    """
    assert agent_mod.looks_multi_step(testo) is False


# ---------------------------------------------------------------------------
# Modulazione del pensiero
# ---------------------------------------------------------------------------


def test_col_punto_aperto_il_pensiero_scende_di_uno():
    p = piano("a")
    p.start("1")
    assert agent_mod.think_for_step("high", p, step=2) == "medium"


def test_a_passo_avanzato_il_pensiero_scende_di_due():
    """Al terzo passo dentro lo stesso punto la mossa e' gia' stata scelta due
    volte: quello che resta e' esecuzione. Nella sessione del 19/08/2026 il
    modello produceva ancora 41.000 e 49.000 caratteri di solo pensiero prima
    di una edit_file -- non stava decidendo, si stava rileggendo."""
    p = piano("a")
    p.start("1")
    assert agent_mod.think_for_step("max", p, step=3) == "medium"
    assert agent_mod.think_for_step("high", p, step=4) == "low"


def test_il_watchdog_ha_un_tetto_in_token_e_non_solo_una_quota():
    """La quota e' una frazione di ``max_tokens``, quindi cresce con la
    finestra: passando da 16k a 98k di contesto la soglia e' passata da 4.500 a
    17.600 token senza che nessuno l'avesse decisa. Un tetto in token non si
    muove quando si cambia macchina."""
    stretto = agent_mod.watchdog_chars_for_step(8192, step=1)
    largo = agent_mod.watchdog_chars_for_step(32768, step=1)
    # Sulla finestra piccola comanda ancora la quota: comportamento invariato.
    assert stretto == chars_for_tokens(8192 * agent_mod.WATCHDOG_RATIO)
    # Su quella grande comanda il tetto, e il tetto e' molto piu' basso.
    assert largo == chars_for_tokens(agent_mod.THINK_CEILING_FIRST)
    # E i passi successivi al primo hanno molto meno spazio del primo.
    assert agent_mod.watchdog_chars_for_step(32768, step=2) < largo
    assert agent_mod.watchdog_chars_for_step(0, step=1) == 0


def test_al_primo_passo_il_pensiero_resta_quello_configurato():
    """E' li' che si legge la richiesta nuova: e' il passo che deve pensare."""
    p = piano("a")
    p.start("1")
    assert agent_mod.think_for_step("high", p, step=1) == "high"


def test_senza_punto_aperto_non_si_abbassa_niente():
    assert agent_mod.think_for_step("high", piano("a"), step=4) == "high"
    assert agent_mod.think_for_step("high", None, step=4) == "high"


def test_un_pensiero_booleano_non_ha_manopole():
    assert agent_mod.think_for_step(True, piano("a"), step=4) is True
    assert agent_mod.think_for_step(False, piano("a"), step=4) is False


def test_il_livello_minimo_non_va_sotto_zero():
    p = piano("a")
    p.start("1")
    assert agent_mod.think_for_step("low", p, step=5) == "low"


def test_i_parametri_del_passo_non_si_ricreano_se_non_cambia_niente():
    """Stesso oggetto: i backend e i test che confrontano l'identita' reggono."""
    params = GenParams(model="x", think=True)
    assert agent_mod.params_for_step(params, piano("a"), 3) is params


def test_i_parametri_del_passo_cambiano_solo_il_pensiero():
    p = piano("a")
    p.start("1")
    params = GenParams(model="x", think="max", temperature=0.6, num_ctx=32768)
    ridotto = agent_mod.params_for_step(params, p, 2)
    assert ridotto.think == "high"
    assert ridotto.temperature == 0.6 and ridotto.num_ctx == 32768
    assert params.think == "max"          # l'originale non viene mutato


# ---------------------------------------------------------------------------
# Watchdog e troncamento, contro il finto Ollama
# ---------------------------------------------------------------------------


def esegui(url, tmp_path, *, prompt, ctx=None, **kwargs):
    ui = [{"role": "user", "content": prompt}]
    eventi = list(
        agent_mod.run_turn(
            backend=OllamaBackend(url, timeout_s=20),
            params=kwargs.pop("params", GenParams(model="fake:latest")),
            tools_schema=TOOLS_SCHEMA,
            tool_ctx=ctx or ToolContext(workspace=str(tmp_path), sandbox="host"),
            ui_messages=ui,
            system_prompt="SYS",
            env_header=None,
            max_steps=kwargs.pop("max_steps", 3),
            **kwargs,
        )
    )
    return ui, eventi


def test_il_watchdog_interrompe_un_ragionamento_senza_azioni(fake_ollama, tmp_path, monkeypatch):
    """Il caso vero: pensa, pensa, e il turno finisce senza niente.

    Il finto modello qui emette un ragionamento lunghissimo e nessuna tool
    call. Senza watchdog il turno arriverebbe in fondo a mani vuote.
    """
    url, _ = fake_ollama
    monkeypatch.setattr(
        fake, "SCRIPT",
        [[{"message": {"content": "<think>" + ("pianifico tutto. " * 400)}}]],
    )
    ui, eventi = esegui(
        url, tmp_path,
        prompt="ciao",
        params=GenParams(model="fake:latest", max_tokens=512),
        max_steps=2,
    )
    nascosti = [m["content"] for m in ui if m.get("hidden")]
    assert any("Ti ho interrotto" in c for c in nascosti)

    finito = [e for e in eventi if isinstance(e, agent_mod.TurnFinished)][-1]
    assert finito.usage["nudges"]["think_watchdog"] >= 1


def test_il_ragionamento_interrotto_non_resta_in_cronologia(fake_ollama, tmp_path, monkeypatch):
    """Rimetterlo in contesto costerebbe migliaia di token per rileggere
    proprio il giro di pensieri che stiamo spezzando."""
    url, _ = fake_ollama
    monkeypatch.setattr(
        fake, "SCRIPT",
        [[{"message": {"content": "<think>" + ("pianifico tutto. " * 400)}}]],
    )
    ui, _ = esegui(
        url, tmp_path,
        prompt="ciao",
        params=GenParams(model="fake:latest", max_tokens=512),
        max_steps=2,
    )
    # Prima del sollecito del watchdog non deve esserci nessun assistant: il
    # pensiero interrotto e' stato buttato, non archiviato. (All'ultimo passo
    # il watchdog non scatta piu' e quello che il modello produce viene
    # registrato normalmente: e' il comportamento voluto, ha smesso di essere
    # un'interruzione.)
    primo_nudge = next(
        i for i, m in enumerate(ui)
        if m.get("hidden") and "Ti ho interrotto" in m["content"]
    )
    assert not [m for m in ui[:primo_nudge] if m.get("role") == "assistant"]


def test_il_watchdog_si_puo_spegnere(fake_ollama, tmp_path, monkeypatch):
    url, _ = fake_ollama
    monkeypatch.setattr(
        fake, "SCRIPT",
        [[{"message": {"content": "<think>" + ("pianifico tutto. " * 400) + "</think>ecco"}}]],
    )
    ui, _ = esegui(
        url, tmp_path,
        prompt="ciao",
        params=GenParams(model="fake:latest", max_tokens=512),
        max_steps=2,
        think_watchdog=False,
    )
    assert not any("Ti ho interrotto" in (m.get("content") or "") for m in ui)


def test_un_ragionamento_normale_non_viene_toccato(fake_ollama, tmp_path):
    """Il watchdog deve essere invisibile finche' il modello si comporta."""
    url, _ = fake_ollama
    _ui, eventi = esegui(
        url, tmp_path,
        prompt="cosa c'e' nel progetto?",
        params=GenParams(model="fake:latest", max_tokens=8192),
        max_steps=4,
    )
    finito = [e for e in eventi if isinstance(e, agent_mod.TurnFinished)][-1]
    assert "think_watchdog" not in finito.usage.get("nudges", {})


def test_la_generazione_troncata_viene_riconosciuta(fake_ollama, tmp_path, monkeypatch):
    """Il done_reason arrivava da Ollama dal primo giorno: mancava chi lo leggesse."""
    url, _handler = fake_ollama
    monkeypatch.setattr(fake, "SCRIPT", [[{"message": {"content": "<think>a meta' del pensier"}}]])
    monkeypatch.setattr(
        fake, "_DONE_CHUNK", {**fake._DONE_CHUNK, "done_reason": "length"}
    )
    ui, eventi = esegui(
        url, tmp_path,
        prompt="ciao",
        # watchdog spento: qui si prova l'altra difesa, quella che scatta
        # quando il tetto e' gia' stato colpito.
        think_watchdog=False,
        max_steps=2,
    )
    nascosti = [m["content"] for m in ui if m.get("hidden")]
    assert any("troncato" in c for c in nascosti)
    finito = [e for e in eventi if isinstance(e, agent_mod.TurnFinished)][-1]
    assert finito.usage["nudges"]["truncated"] >= 1


# ---------------------------------------------------------------------------
# Il piano preteso
# ---------------------------------------------------------------------------


def test_su_una_richiesta_multi_obiettivo_senza_piano_l_harness_insiste(fake_ollama, tmp_path):
    url, _ = fake_ollama
    ui, eventi = esegui(url, tmp_path, prompt=MULTI, max_steps=3)
    nascosti = [m["content"] for m in ui if m.get("hidden")]
    assert any("manage_plan" in c for c in nascosti)
    finito = [e for e in eventi if isinstance(e, agent_mod.TurnFinished)][-1]
    assert finito.usage["nudges"]["plan"] == 1


def test_con_un_piano_gia_scritto_non_si_insiste(fake_ollama, tmp_path):
    url, _ = fake_ollama
    ctx = ToolContext(workspace=str(tmp_path), sandbox="host")
    ctx.plan.set_steps(["correggere i bug", "estendere il motore"])
    ui, _ = esegui(url, tmp_path, prompt=MULTI, ctx=ctx, max_steps=3)
    assert not any("Scrivilo adesso" in (m.get("content") or "") for m in ui)


def test_su_una_richiesta_semplice_non_si_pretende_niente(fake_ollama, tmp_path):
    url, _ = fake_ollama
    ui, _ = esegui(url, tmp_path, prompt="cosa c'e' nel progetto?", max_steps=3)
    assert not any("manage_plan" in (m.get("content") or "") for m in ui)


def test_il_piano_si_puo_pretendere_di_meno(fake_ollama, tmp_path):
    url, _ = fake_ollama
    ui, _ = esegui(url, tmp_path, prompt=MULTI, require_plan=False, max_steps=3)
    assert not any("manage_plan" in (m.get("content") or "") for m in ui)


def test_saltare_un_punto_non_cancella_una_verifica(tmp_path):
    """``skip`` azzerava il registro. Erano due fatti diversi cuciti insieme.

    Rinunciare a un punto e' una decisione sul lavoro; com'e' andato un test e'
    una misura. Usare la prima per far sparire la seconda era l'uscita a costo
    zero accanto a quella che pretende una frase -- e su questo progetto
    un'uscita gratuita da un guard-rail diventa la strada.
    """
    ctx, registro = ctx_col_registro(tmp_path)
    dispatch(ctx, PLAN_TOOL, {"action": "set", "steps": ["step 1", "step 2"]})
    rosso(registro, ctx, "pytest -q")

    dispatch(ctx, PLAN_TOOL, {"action": "skip", "step_id": "1",
                              "note": "non risolvibile nell'ambiente"})
    assert ctx.plan.get("1").status == "skipped"
    assert [v.comando for v in registro.pendenti] == ["pytest -q"]
    assert ctx.red_command == "pytest -q"


def test_ignore_red_giustifica_una_verifica_sola(tmp_path):
    """L'uscita era troppo larga: una frase archiviava l'intero registro.

    Riprodotto: con due rossi indipendenti aperti, dichiararne non pertinente
    uno li faceva sparire tutti e due, perche' ``clear_red_command`` chiamava
    ``clear()`` sul tracker. Il secondo non lo aveva guardato nessuno.
    """
    ctx, registro = ctx_col_registro(tmp_path)
    dispatch(ctx, PLAN_TOOL, {"action": "set", "steps": ["step 1", "step 2"]})
    rosso(registro, ctx, "pytest -q")
    rosso(registro, ctx, "pytest -q")          # due tentativi: e' la piu' insistente
    rosso(registro, ctx, "ruff check .")

    risposta = json.loads(dispatch(ctx, PLAN_TOOL, {
        "action": "complete", "step_id": "1", "ignore_red": True,
        "note": "la suite richiede il container Docker, qui non c'e'",
    }))
    assert risposta["status"] == "ok"
    assert ctx.plan.get("1").status == DONE

    stati = {v.identita: v.stato for v in registro.verifiche.values()}
    assert stati["pytest"] == "giustificata"
    assert stati["ruff . check"] == "rossa", "l'altro rosso non era in discussione"

    # Il rosso archiviato si conta e si vede: nel piano, dove l'utente legge.
    assert risposta["rossi_ignorati"] == 1
    assert "verifica rossa giustificata" in ctx.plan.get("1").note
    assert ctx.rossi_ignorati[0]["comando"] == "pytest -q"


def test_ignore_red_senza_motivo_non_giustifica_ma_non_blocca(tmp_path):
    """La frase resta il prezzo di archiviare un rosso, non di andare avanti.

    Prima ``ignore_red`` senza nota rifiutava la chiusura, e la coincidenza fra
    "non sai dire perche' non conta" e "non puoi proseguire" era meta' dello
    stallo. Adesso il punto si chiude e la verifica resta rossa: che e'
    esattamente cio' che significa non saper dire perche' non conta.

    Il prezzo dell'archiviazione resta misurato: il ``complete`` che *pretende*
    la nota ne ha ottenute 23 su 24 in tre sessioni, il ``manage_notes`` che la
    propone e' stato usato 0 volte su 42 conversazioni.
    """
    ctx, registro = ctx_col_registro(tmp_path)
    dispatch(ctx, PLAN_TOOL, {"action": "set", "steps": ["step 1", "step 2"]})
    rosso(registro, ctx, "pytest -q")

    nudo = json.loads(dispatch(ctx, PLAN_TOOL, {
        "action": "complete", "step_id": "1", "ignore_red": True}))
    assert nudo["status"] == "ok"
    assert ctx.plan.get("1").status == DONE
    assert [v.comando for v in registro.pendenti] == ["pytest -q"]
    assert any("non giustifica" in a for a in nudo["avvisi"])
    assert "rossi_ignorati" not in nudo

    breve = json.loads(dispatch(ctx, PLAN_TOOL, {
        "action": "complete", "step_id": "2", "ignore_red": True, "note": "boh"}))
    assert breve["status"] == "ok"
    assert registro.pendenti, "'boh' non e' un motivo"

    dispatch(ctx, PLAN_TOOL, {"action": "add", "steps": ["step 3"]})
    buono = json.loads(dispatch(ctx, PLAN_TOOL, {
        "action": "complete", "step_id": "3", "ignore_red": True,
        "note": "Docker non disponibile in questo ambiente"}))
    assert buono["rossi_ignorati"] == 1
    assert not registro.pendenti
