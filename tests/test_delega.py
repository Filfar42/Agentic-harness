"""Il contratto del referto di delega.

Le tre patch (marker di troncamento, budget dichiarato nel prompt, ramo d'errore
informato) vivono tutte nella coda di ``delega.esegui``. Qui si prova che il
padre riceve sempre abbastanza per decidere se riformulare la domanda o no --
senza far girare un modello: ``run_turn`` arriva come parametro iniettabile, e
il fake lo muta proprio come farebbe il vero ciclo.
"""

from __future__ import annotations

import json

import tests.test_agent_loop as fake
from core.config import GenParams
from core.delega import MAX_PASSI_DELEGA, MAX_REFERTO_CHARS, TOOL_DELEGA, esegui
from core.tools import ToolContext

# Il finto server Ollama sta li' e ci gira il ciclo vero: il sollecito alla
# delega vive nel loop, non in questo modulo, e va provato dove vive.
fake_ollama = fake.fake_ollama


class StepStarted:
    def __init__(self, step=0, total=0):
        self.step = step
        self.total = total


def _fake_run_turn(*, referto=None, passi=1, legge=("core/config.py",), con_asistente=True):
    """Un ``run_turn`` iniettabile: appende i messaggi come farebbe il vero ciclo."""

    def _run(**kw):
        msg = kw["ui_messages"]
        for nome in legge:
            msg.append({"role": "tool", "name": "read_file", "args": {"filepath": nome}})
        for i in range(passi):
            yield StepStarted(step=i + 1, total=passi)
        if con_asistente and referto is not None:
            msg.append({"role": "assistant", "content": referto})

    return _run


def _esegui(tmp_path, *, _run=None, _registra=True, **kw_fake):
    """``_run`` permette di sostituire il finto ciclo per intero (serve a
    guardare cosa riceve il figlio); ``_registra`` spegne la scrittura dei
    fatti, come fa l'impostazione ``spec_delega``."""
    ctx = ToolContext(workspace=str(tmp_path), sandbox="host")
    return esegui(
        "dove sta budgets_for?",
        backend=object(),          # il fake non lo usa: va solo di mano al run_turn
        params=GenParams(model="fake"),
        tools_schema=[],
        tool_ctx=ctx,
        env_header=None,
        run_turn=_run or _fake_run_turn(**kw_fake),
        registra_esiti=_registra,
    )


def test_referto_corto_arriva_intero(tmp_path):
    out = _esegui(tmp_path, referto="sta in core/config.py:105", passi=2)
    assert out["referto"] == "sta in core/config.py:105"
    assert "troncato" not in out      # senza marker quando il referto sta nel budget
    assert out["passi"] == 2


def test_referto_lungo_porta_il_marker(tmp_path):
    """Il troncamento deve essere *visibile*: il padre sa che ha solo una parte."""
    lungo = "x" * (MAX_REFERTO_CHARS + 497)   # oltre il tetto, di una misura nota
    out = _esegui(tmp_path, referto=lungo, passi=3)
    assert out["troncato"] is True
    assert out["referto"].startswith("x" * MAX_REFERTO_CHARS)
    # La misura nel marker e' quella dell'originale, non del pezzo che e' rimasto.
    assert f"era lungo {len(lungo)} caratteri" in out["referto"]


def test_il_padre_sa_cosa_ha_guardato(tmp_path):
    """``file_letti`` serve a fidarsi -- o a non fidarsi: un referto senza aperture e' inventato."""
    out = _esegui(tmp_path, referto="trovato", passi=1, legge=("core/config.py", "server/main.py"))
    assert out["file_letti"] == ["core/config.py", "server/main.py"]


def test_esaurito_con_risposta_e_fla_ggiato(tmp_path):
    """Risposta arrivata col passo 6 gia' speso: si legge, ma segnata come probabile incompleta."""
    out = _esegui(tmp_path, referto="forse qui", passi=MAX_PASSI_DELEGA)
    assert out["referto"] == "forse qui"
    assert out["esaurito"] is True


def test_esaurito_senza_risposta_dice_la_causa(tmp_path):
    """Sei passi bruciati senza un ultimo messaggio: la causa va detta e l'appiglio va restituito."""
    out = _esegui(tmp_path, referto=None, con_asistente=False, passi=MAX_PASSI_DELEGA)
    assert "errore" in out and "referto" not in out
    assert out["esaurito"] is True
    assert f"ha esaurito i {MAX_PASSI_DELEGA} passi" in out["errore"]
    assert out["file_letti"] == ["core/config.py"]   # da' al padre un punto da cui riformulare


def test_senza_risposta_ma_con_margine_non_e_esaurimento(tmp_path):
    """Nessun messaggio finale, ma il figlio si e' fermato col passo 1: non e' 'esaurito'."""
    out = _esegui(tmp_path, referto=None, con_asistente=False, passi=1)
    assert "errore" in out and "referto" not in out
    assert out["esaurito"] is False


# ---------------------------------------------------------------------------
# La rete: sei passi di letture non si buttano perche' manca l'ultima frase
# ---------------------------------------------------------------------------


class BackendDiChiusura:
    """Un backend che risponde una volta sola, come la chiamata di chiusura."""

    def __init__(self, testo="core/config.py:105 budgets_for; resta da vedere agent.py"):
        self.testo = testo
        self.chiamate: list[list[dict]] = []

    def stream(self, messages, tools, params):
        self.chiamate.append(messages)
        assert tools is None, "il referto di chiusura non deve avere tool"
        assert params.think is False, "ne' pensiero: c'e' solo da scrivere"

        class _Ev:
            kind = "content"
            text = self.testo

        yield _Ev()


def test_i_passi_bruciati_diventano_un_referto_parziale(tmp_path):
    """Il caso misurato: 3 esplorazioni su 17 hanno letto sei file e restituito
    niente. Quel contenuto era ancora nel contesto del figlio."""
    from core.agent import build_api_messages

    be = BackendDiChiusura()
    ctx = ToolContext(workspace=str(tmp_path), sandbox="host")
    out = esegui(
        "dove sta budgets_for?",
        backend=be,
        params=GenParams(model="fake"),
        tools_schema=[],
        tool_ctx=ctx,
        env_header=None,
        run_turn=_fake_run_turn(referto=None, con_asistente=False, passi=MAX_PASSI_DELEGA),
        build_messages=build_api_messages,
    )
    assert "errore" not in out
    assert out["referto"] == be.testo
    assert out["chiuso_a_forza"] is True
    assert out["esaurito"] is True
    # Il padre deve poterlo leggere come recupero, non come conclusione.
    assert "parziale" in out["nota"]
    assert len(be.chiamate) == 1


def test_senza_niente_di_letto_non_si_paga_una_generazione(tmp_path):
    """Un figlio che non ha aperto niente non ha niente da riferire: chiedergli
    un referto sarebbe una generazione per farsi inventare una risposta."""
    from core.agent import build_api_messages

    be = BackendDiChiusura()
    ctx = ToolContext(workspace=str(tmp_path), sandbox="host")
    out = esegui(
        "dove sta budgets_for?",
        backend=be,
        params=GenParams(model="fake"),
        tools_schema=[],
        tool_ctx=ctx,
        env_header=None,
        run_turn=_fake_run_turn(referto=None, con_asistente=False, passi=2, legge=()),
        build_messages=build_api_messages,
    )
    assert "errore" in out
    assert be.chiamate == [], "nessuna generazione di chiusura senza letture"


def test_l_hint_non_manda_piu_il_padre_a_leggere_da_solo(tmp_path):
    """'o cerca da solo' era l'uscita che il modello prendeva sempre -- ed e' la
    strada che riporta l'esplorazione dentro il contesto del padre."""
    out = _esegui(tmp_path, referto=None, con_asistente=False, passi=1)
    assert "da solo" not in out["hint"]
    assert "piu' stretta" in out["hint"]


def test_dopo_troppe_letture_di_fila_l_harness_nomina_esplora(fake_ollama, tmp_path):
    """Il tool c'e' e funziona; quello che mancava era nominarlo al momento giusto.

    Misurato il 23/08/2026: `esplora` compare in 4 sessioni su 24, mentre
    l'esplorazione fatta a mano vale il 46% dei token di risultato -- token che
    restano in contesto per tutto il resto del lavoro.
    """
    import tests.test_agent_loop as fake
    from core import agent as agent_mod
    from core.backend import OllamaBackend
    from core.tools import TOOLS_SCHEMA

    (tmp_path / "uno.py").write_text("a = 1\n", encoding="utf-8")

    def lettura(nome):
        return [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "read_file", "arguments": {"filepath": nome}}}
                    ],
                }
            }
        ]

    url, _ = fake_ollama
    originale = fake.SCRIPT
    # Sei passi di sola lettura: il quinto deve far scattare il sollecito.
    fake.SCRIPT = [lettura("uno.py") for _ in range(6)]
    try:
        ui_messages = [{"role": "user", "content": "guarda un po' in giro"}]
        list(
            agent_mod.run_turn(
                backend=OllamaBackend(url, timeout_s=20),
                params=GenParams(model="fake:latest"),
                tools_schema=TOOLS_SCHEMA,
                tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
                ui_messages=ui_messages,
                system_prompt="SYS",
                env_header=None,
                max_steps=6,
                require_plan=False,
                require_summary=False,
            )
        )
    finally:
        fake.SCRIPT = originale

    solleciti = [
        m for m in ui_messages
        if m.get("hidden") and "esplora" in str(m.get("content") or "")
    ]
    assert len(solleciti) == 1, "una volta per turno, non ad ogni lettura"
    # E deve arrivare dopo i risultati dei tool, mai fra una tool_call e il suo
    # risultato: e' l'invariante di tutti i solleciti di questo ciclo.
    posizione = ui_messages.index(solleciti[0])
    assert ui_messages[posizione - 1].get("role") == "tool"


def test_il_sollecito_non_scatta_se_il_modello_sta_lavorando(fake_ollama, tmp_path):
    """Un read_file dentro un passo che scrive e' il ciclo leggi-modifica, non
    esplorazione: sollecitare li' sarebbe rumore."""
    import tests.test_agent_loop as fake
    from core import agent as agent_mod
    from core.backend import OllamaBackend
    from core.tools import TOOLS_SCHEMA

    (tmp_path / "uno.py").write_text("a = 1\n", encoding="utf-8")

    def legge_e_scrive(i):
        return [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "read_file", "arguments": {"filepath": "uno.py"}}},
                        {
                            "function": {
                                "name": "write_file",
                                "arguments": {"filepath": f"nuovo{i}.py", "content": "x = 1\n"},
                            }
                        },
                    ],
                }
            }
        ]

    url, _ = fake_ollama
    originale = fake.SCRIPT
    fake.SCRIPT = [legge_e_scrive(i) for i in range(6)]
    try:
        ui_messages = [{"role": "user", "content": "sistema i file"}]
        list(
            agent_mod.run_turn(
                backend=OllamaBackend(url, timeout_s=20),
                params=GenParams(model="fake:latest"),
                tools_schema=TOOLS_SCHEMA,
                tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
                ui_messages=ui_messages,
                system_prompt="SYS",
                env_header=None,
                max_steps=6,
                require_plan=False,
                require_summary=False,
            )
        )
    finally:
        fake.SCRIPT = originale

    assert not [
        m for m in ui_messages
        if m.get("hidden") and "stai esplorando" in str(m.get("content") or "")
    ]


def test_il_prompt_del_figlio_dichiara_il_budget():
    """Il figlio deve sapere quanto budget ha: altrimenti scrive per chi non tronca."""
    from core.delega import PROMPT_DELEGA

    assert f"al massimo {MAX_PASSI_DELEGA} passi" in PROMPT_DELEGA
    assert f"primi {MAX_REFERTO_CHARS} caratteri" in PROMPT_DELEGA   # 2000, come si vede al figlio


# ---------------------------------------------------------------------------
# I budget del sotto-turno: la strettata deve arrivare davvero al figlio.
# ---------------------------------------------------------------------------

def test_parametri_figlio_allarga_la_finestra_e_spegne_think():
    """num_ctx x1.5 come margine, think spento, il resto del padre invariato."""
    from core.delega import parametri_figlio

    padre = GenParams(model="m", num_ctx=16384, max_tokens=4096, think="low", temperature=0.4)
    figlio = parametri_figlio(padre)
    assert figlio.num_ctx == 24576
    assert figlio.think is False
    assert figlio.max_tokens == 4096 and figlio.temperature == 0.4 and figlio.model == "m"


def test_budget_stretti_dimezza_e_garantisce_i_minimi():
    from core.config import budgets_for
    from core.delega import budget_stretti

    larghi = budgets_for(16384)
    stretti = budget_stretti(larghi)
    assert stretti.read_file_max_chars < larghi.read_file_max_chars
    assert stretti.search_max_matches < larghi.search_max_matches
    # La finestra integrale non cresce mai: a 16k il padre e' gia' al minimo 3.
    assert stretti.tool_result_full_window <= larghi.tool_result_full_window
    # Dai minimi non si scende mai, nemmeno restringendo una finestra povera.
    stretti_poveri = budget_stretti(budgets_for(4096))
    assert stretti_poveri.read_file_max_chars >= 2500   # MIN_READ_FILE_CHARS
    assert stretti_poveri.search_max_matches >= 10
    assert stretti_poveri.tool_result_full_window == 3


def test_esegui_passa_al_figlio_budget_stretti_e_finestra_larga(tmp_path):
    """La strettata arriva a run_turn *e* resta nel ToolContext dopo il primo passo."""
    visti: dict[str, object] = {}

    def _run(**kw):
        visti["budgets"] = kw["budgets"]
        visti["params"] = kw["params"]
        visti["tool_ctx"] = kw["tool_ctx"]
        msg = kw["ui_messages"]
        msg.append({"role": "tool", "name": "read_file", "args": {"filepath": "core/config.py"}})
        yield StepStarted(step=1, total=1)
        msg.append({"role": "assistant", "content": "referto"})

    ctx = ToolContext(workspace=str(tmp_path), sandbox="host")
    out = esegui(
        "dove sta budgets_for?",
        backend=object(),
        params=GenParams(model="fake"),
        tools_schema=[],
        tool_ctx=ctx,
        env_header=None,
        run_turn=_run,
    )
    assert out["referto"] == "referto"

    from core.config import budgets_for
    from core.delega import budget_stretti

    larghi = budgets_for(16384)
    stretti = visti["budgets"]
    assert stretti.read_file_max_chars < larghi.read_file_max_chars
    assert stretti == budget_stretti(larghi)
    # run_turn sovrascrive tool_ctx.budgets al primo passo: se lo facesse con
    # budgets_for(num_ctx_figlio), qui troveremmo i budget larghi.
    assert visti["tool_ctx"].budgets == stretti
    # La finestra del sotto-turno e' quella allargata, non quella del padre.
    assert visti["params"].num_ctx == 24576


def test_budget_riferimento_valori_attesi_e_coerenza_col_prompt():
    """Le costanti del prompt derivano dal riferimento base, con valori fissi."""
    from core.config import BASE_NUM_CTX, budgets_for
    from core.delega import (
        MAX_LETTURA_FIGLIO,
        MAX_MATCHES_FIGLIO,
        PROMPT_DELEGA,
        budget_riferimento,
        budget_stretti,
    )

    rif = budget_riferimento()
    # Valori attesi espliciti alla taratura base (padre 16k -> figlio x1.5).
    assert BASE_NUM_CTX == 16384
    assert rif.read_file_max_chars == 6000
    assert rif.search_max_matches == 40
    # Le due costanti sono la proiezione del riferimento, e il prompt le cita.
    assert MAX_LETTURA_FIGLIO == rif.read_file_max_chars == 6000
    assert MAX_MATCHES_FIGLIO == rif.search_max_matches == 40
    assert f"ai primi {MAX_LETTURA_FIGLIO} caratteri" in PROMPT_DELEGA
    assert f"a {MAX_MATCHES_FIGLIO} corrispondenze" in PROMPT_DELEGA
    # Il riferimento e' esattamente il budget stretto sulla finestra base...
    assert rif == budget_stretti(budgets_for(BASE_NUM_CTX))
    # ...ed e' memoizzato: chiamate successive restituiscono lo stesso oggetto.
    assert budget_riferimento() is rif


# ---------------------------------------------------------------------------
# Il perimetro di sola lettura del figlio
# ---------------------------------------------------------------------------
#
# Lo schema ridotto dice al figlio quali tool esistono. Non e' un permesso:
# ``parse_text_tool_calls`` recupera le chiamate che il modello scrive come
# testo confrontandole con ``TOOL_NAMES`` -- il vocabolario dell'harness -- e
# ``dispatch`` guardava solo ``TOOL_IMPLS``. Un figlio che scriveva
# ``{"name": "run_command", ...}`` come testo eseguiva comandi. Il percorso non
# e' un caso limite: e' quello che l'harness supporta apposta per i modelli che
# non fanno function calling nativo.


def test_il_figlio_riceve_il_perimetro_come_dato_non_come_schema(tmp_path):
    visto = {}

    def _spia(**kw):
        visto["ctx"] = kw["tool_ctx"]
        return iter(())

    _esegui(tmp_path, _run=_spia)
    ctx = visto["ctx"]
    assert ctx.tool_consentiti == frozenset(TOOL_DELEGA)
    assert ctx.puo_usare("read_file")
    assert not ctx.puo_usare("run_command")
    assert not ctx.puo_usare("write_file")


def test_un_esploratore_non_genera_esploratori(tmp_path):
    """``ctx_figlio`` ereditava le due porte da cui si aprono altri sotto-turni."""
    visto = {}

    def _spia(**kw):
        visto["ctx"] = kw["tool_ctx"]
        return iter(())

    ctx_padre = ToolContext(
        workspace=str(tmp_path),
        sandbox="host",
        on_delega=lambda compito: {"referto": "x"},
        on_vault_search=lambda **kw: {"referto": "y"},
    )
    esegui(
        "dove sta budgets_for?",
        backend=object(),
        params=GenParams(model="fake"),
        tools_schema=[],
        tool_ctx=ctx_padre,
        env_header=None,
        run_turn=_spia,
        registra_esiti=False,
    )
    assert visto["ctx"].on_delega is None
    assert visto["ctx"].on_vault_search is None


def test_dispatch_rifiuta_un_tool_fuori_perimetro(tmp_path):
    """La difesa vera: ``dispatch`` non esegue, non importa da dove arrivi il nome."""
    from core.tools import dispatch

    ctx = ToolContext(
        workspace=str(tmp_path),
        sandbox="host",
        tool_consentiti=frozenset(TOOL_DELEGA),
    )
    esito = json.loads(dispatch(ctx, "run_command", {"command": "echo compromesso"}))
    assert "error" in esito
    assert "run_command" in esito["error"]
    # e il messaggio dice cosa PUO' fare, non solo cosa non puo'
    assert "read_file" in esito.get("hint", "")

    # senza perimetro (il turno normale) lo stesso tool passa
    normale = ToolContext(workspace=str(tmp_path), sandbox="host")
    assert normale.puo_usare("run_command")
