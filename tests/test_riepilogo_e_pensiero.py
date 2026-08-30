"""Il turno che finisce i passi senza dire niente, e la traccia del pensiero.

Due difetti misurati il 23/08/2026 sulle quattro sessioni con `Qwen3.8:27B`:

* la sessione `20260820_112642` ha prodotto 21 passi, 46 risultati di tool,
  124.877 caratteri di ragionamento e **zero caratteri di risposta**, mai.
  ``SUMMARY_NUDGE`` non poteva salvarla: ha in guardia ``step < max_steps`` --
  chiede al modello di scrivere al passo *successivo*, e su un turno esaurito
  un passo successivo non c'e' piu';
* il pensiero non si accorciava col passo (mediana 3.327 caratteri al passo 1,
  3.452 al passo 8+) benche' ``think_for_step`` debba scendere. Dai `<think>`
  salvati non si poteva dire se non si applicasse o se non si vedesse, perche'
  il livello *chiesto* non era scritto da nessuna parte.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import agent as agent_mod  # noqa: E402
from core.backend import StreamEvent  # noqa: E402
from core.config import GenParams  # noqa: E402
from core.tools import ToolContext  # noqa: E402


class BackendMuto:
    """Chiama sempre un tool e non scrive mai una parola.

    E' il comportamento osservato nella sessione senza risposte. Distingue la
    chiamata di riepilogo dall'assenza di ``tools``, come la compattazione e
    il referto di chiusura della delega: e' l'unico criterio, e provarlo qui
    vuol dire provare che il ciclo non puo' confondere le due chiamate.
    """

    def __init__(self, testo_finale: str = "Ho letto tre file. Mi sono fermato ai passi.") -> None:
        self.testo_finale = testo_finale
        self.passi = 0
        self.riepiloghi = 0
        self.params_riepilogo = None

    def stream(self, messages, tools, params):  # noqa: ARG002
        if tools is None:
            self.riepiloghi += 1
            self.params_riepilogo = params
            if self.testo_finale:
                yield StreamEvent("content", text=self.testo_finale)
            return
        self.passi += 1
        yield StreamEvent(
            "tool_call",
            tool_call={
                "id": f"c{self.passi}",
                "name": "list_files",
                "arguments": json.dumps({"subfolder": "."}),
            },
        )


class BackendCheRisponde(BackendMuto):
    """Come sopra, ma l'ultimo passo dice qualcosa."""

    def stream(self, messages, tools, params):  # noqa: ARG002
        if tools is None:
            self.riepiloghi += 1
            yield StreamEvent("content", text="riepilogo che non doveva servire")
            return
        self.passi += 1
        yield StreamEvent("content", text="Fatto: ho guardato la cartella.")


def _turno(backend, tmp_path, **kw):
    msgs = [{"role": "user", "content": "guarda cosa c'e' nella cartella"}]
    eventi = list(
        agent_mod.run_turn(
            backend=backend,
            params=GenParams(model="fake", num_ctx=8192, max_tokens=2048, think="high"),
            tools_schema=[
                {"type": "function", "function": {"name": "list_files", "parameters": {}}}
            ],
            tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
            ui_messages=msgs,
            system_prompt="SYS",
            env_header="ENV",
            max_steps=kw.pop("max_steps", 3),
            require_plan=False,
            enable_nudge=False,
            **kw,
        )
    )
    return msgs, eventi


# ---------------------------------------------------------------------------
# Il riepilogo forzato
# ---------------------------------------------------------------------------


def test_un_turno_che_finisce_i_passi_muto_riceve_una_voce(tmp_path):
    be = BackendMuto()
    msgs, eventi = _turno(be, tmp_path)

    finito = eventi[-1]
    assert finito.reason == "max_steps"
    assert be.riepiloghi == 1, "una chiamata sola, e solo se serviva"

    ultimo = msgs[-1]
    assert ultimo["role"] == "assistant"
    assert ultimo["content"] == be.testo_finale
    # Detto: non e' una risposta come le altre, e' un referto chiesto
    # dall'harness a turno gia' finito.
    assert ultimo["forzato"] is True
    # ...e l'utente lo vede davvero: senza l'evento resterebbe nel file.
    assert any(
        isinstance(e, agent_mod.AssistantTurn) and e.content == be.testo_finale
        for e in eventi
    )


def test_il_riepilogo_non_ha_tool_e_non_pensa(tmp_path):
    """Qui non c'e' niente da decidere: c'e' da raccontare."""
    be = BackendMuto()
    _turno(be, tmp_path)
    assert be.params_riepilogo is not None
    assert be.params_riepilogo.think is False
    assert be.params_riepilogo.max_tokens <= agent_mod.MAX_TOKEN_RIEPILOGO


def test_un_turno_che_ha_gia_risposto_non_paga_una_generazione(tmp_path):
    be = BackendCheRisponde()
    msgs, _ = _turno(be, tmp_path)
    assert be.riepiloghi == 0
    assert not any(m.get("forzato") for m in msgs)


def test_senza_lavoro_niente_riepilogo(tmp_path):
    """Un turno che non ha toccato niente non ha niente da riferire: chiedere
    il referto sarebbe una generazione per farsi inventare una risposta."""

    class BackendCheNonFaNiente(BackendMuto):
        def stream(self, messages, tools, params):  # noqa: ARG002
            if tools is None:
                self.riepiloghi += 1
                yield StreamEvent("content", text="x")
                return
            self.passi += 1
            yield StreamEvent("content", text="")

    be = BackendCheNonFaNiente()
    _turno(be, tmp_path, max_steps=2)
    assert be.riepiloghi == 0


def test_un_riepilogo_fallito_lascia_le_cose_come_stavano(tmp_path):
    be = BackendMuto(testo_finale="")
    msgs, eventi = _turno(be, tmp_path)
    assert be.riepiloghi == 1
    assert not any(m.get("forzato") for m in msgs)
    assert eventi[-1].reason == "max_steps"


def test_si_puo_spegnere_con_require_summary(tmp_path):
    be = BackendMuto()
    _turno(be, tmp_path, require_summary=False)
    assert be.riepiloghi == 0


def test_il_riepilogo_dice_al_modello_che_e_finita():
    from core.prompts import PROMPT_RIEPILOGO_FINALE

    # La frase deve essere vera: se promettesse altri passi il modello
    # scriverebbe intenzioni invece di un consuntivo.
    assert "Non puoi piu' chiamare tool" in PROMPT_RIEPILOGO_FINALE
    assert "in italiano" in PROMPT_RIEPILOGO_FINALE


# ---------------------------------------------------------------------------
# La traccia del pensiero
# ---------------------------------------------------------------------------


def test_ogni_passo_lascia_scritto_quanto_ha_pensato(tmp_path):
    be = BackendMuto()
    msgs, _ = _turno(be, tmp_path)
    tracce = [m["think"] for m in msgs if m.get("role") == "assistant" and "think" in m]
    assert len(tracce) == 3, "una per passo, nessuna persa"
    assert [t["passo"] for t in tracce] == [1, 2, 3]
    for t in tracce:
        assert t["configurato"] == "high"
        assert t["chiamate"] == 1
        assert t["watchdog"] is False
        assert set(t) >= {"passo", "configurato", "usato", "punto_aperto", "pensato", "risposto"}


def test_la_traccia_registra_il_livello_davvero_chiesto(tmp_path):
    """E' il numero che mancava: senza, non si puo' distinguere fra "lo
    scalino non si applica" e "uno scalino non si vede"."""
    from core.plan import Plan

    piano = Plan()
    piano.set_steps(["fare la cosa", "farne un'altra"])
    piano.avanza()
    assert piano.current is not None

    be = BackendMuto()
    msgs = [{"role": "user", "content": "vai"}]
    list(
        agent_mod.run_turn(
            backend=be,
            params=GenParams(model="fake", num_ctx=8192, max_tokens=2048, think="high"),
            tools_schema=[
                {"type": "function", "function": {"name": "list_files", "parameters": {}}}
            ],
            tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host", plan=piano),
            ui_messages=msgs,
            system_prompt="SYS",
            env_header="ENV",
            max_steps=3,
            require_plan=False,
            require_summary=False,
            enable_nudge=False,
        )
    )
    tracce = [m["think"] for m in msgs if m.get("role") == "assistant" and "think" in m]
    assert [t["punto_aperto"] for t in tracce] == [True, True, True]
    # Passo 1 al livello pieno; dal 2 uno scalino, dal 3 (DEEP_STEP) due.
    assert [t["usato"] for t in tracce] == ["high", "medium", "low"]
    assert all(t["configurato"] == "high" for t in tracce)


def test_senza_piano_aperto_non_si_scende(tmp_path):
    be = BackendMuto()
    msgs, _ = _turno(be, tmp_path, require_summary=False)
    tracce = [m["think"] for m in msgs if m.get("role") == "assistant" and "think" in m]
    assert all(t["punto_aperto"] is False for t in tracce)
    assert all(t["usato"] == "high" for t in tracce)


def test_la_traccia_non_arriva_mai_al_modello(tmp_path):
    """Sta nel file di sessione perche' e' li' che serve a chi analizza. Nel
    contesto sarebbe solo un costo per passo."""
    be = BackendMuto()
    msgs, _ = _turno(be, tmp_path, require_summary=False)
    api = agent_mod.build_api_messages(msgs, system_prompt="SYS", env_header=None)
    assert "punto_aperto" not in json.dumps(api)
    assert "think" not in json.dumps(api)


def test_il_livello_si_scrive_leggibile_anche_quando_e_un_booleano():
    assert agent_mod._nome_livello("HIGH") == "high"
    assert agent_mod._nome_livello(True) == "on"
    assert agent_mod._nome_livello(False) == "off"
    assert agent_mod._nome_livello(None) == "?"


# ---------------------------------------------------------------------------
# ``ultima_risposta``: la guardia che decide se il turno ha parlato
# ---------------------------------------------------------------------------


def test_la_risposta_del_turno_prima_non_conta():
    """L'errore che farebbe passare per 'gia' risposto' un turno rimasto muto."""
    storia = [
        {"role": "user", "content": "prima domanda"},
        {"role": "assistant", "content": "prima risposta"},
        {"role": "user", "content": "seconda domanda"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "x"}]},
        {"role": "tool", "tool_call_id": "x", "name": "list_files", "content": "{}"},
    ]
    assert agent_mod.ultima_risposta(storia) == ""

    storia.append({"role": "assistant", "content": "<think>penso</think>ecco qua"})
    assert agent_mod.ultima_risposta(storia) == "ecco qua"


def test_un_sollecito_dell_harness_non_apre_un_turno_nuovo():
    storia = [
        {"role": "user", "content": "domanda"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "x"}]},
        {"role": "user", "content": "sollecito", "hidden": True},
        {"role": "assistant", "content": "<think>solo pensiero</think>"},
    ]
    assert agent_mod.ultima_risposta(storia) == ""


def test_l_interfaccia_dice_che_il_resoconto_e_forzato():
    """Senza, una chiusura forzata si legge come una chiusura riuscita -- e la
    differenza cambia la mossa successiva dell'utente."""
    web = Path(__file__).resolve().parents[1] / "web"
    js = (web / "app.js").read_text(encoding="utf-8")
    assert "msg.forzato && answer" in js
    corpo = js[js.index("markForzato() {"):]
    corpo = corpo[: corpo.index("\n    },")]
    assert "esauriti" in corpo
    assert ".turn-forzato" in (web / "style.css").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# `native_think: auto` deve dare una scala, non un interruttore
# ---------------------------------------------------------------------------


def test_auto_si_risolve_in_un_livello_non_in_un_booleano():
    """Il difetto silenzioso: con un booleano `think_for_step` non ha nessuna
    scala su cui scendere, e tutta la modulazione del pensiero e' inerte."""
    import server.main as M
    from core.config import THINK_AUTO_LEVEL

    st = M.AppState.__new__(M.AppState)
    st.settings = {"native_think": "auto"}
    st.thinking_enabled = lambda: True
    assert M.AppState.think_setting(st) == THINK_AUTO_LEVEL
    assert isinstance(M.AppState.think_setting(st), str)

    # Modello senza canale di ragionamento: resta spento, e spento e' un
    # booleano perche' li' non c'e' niente da modulare.
    st.thinking_enabled = lambda: False
    assert M.AppState.think_setting(st) is False

    # Un livello scelto a mano continua a vincere, com'era.
    st.settings = {"native_think": "low"}
    assert M.AppState.think_setting(st) == "low"


def test_con_auto_la_modulazione_adesso_agisce(tmp_path):
    """La prova che i due pezzi si parlano: dal livello di `auto` si scende."""
    from core.config import THINK_AUTO_LEVEL
    from core.plan import Plan

    piano = Plan()
    piano.set_steps(["uno", "due"])
    piano.avanza()

    be = BackendMuto()
    msgs = [{"role": "user", "content": "vai"}]
    list(
        agent_mod.run_turn(
            backend=be,
            params=GenParams(model="fake", num_ctx=8192, max_tokens=2048,
                             think=THINK_AUTO_LEVEL),
            tools_schema=[
                {"type": "function", "function": {"name": "list_files", "parameters": {}}}
            ],
            tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host", plan=piano),
            ui_messages=msgs,
            system_prompt="SYS",
            env_header="ENV",
            max_steps=3,
            require_plan=False,
            require_summary=False,
            enable_nudge=False,
        )
    )
    usati = [m["think"]["usato"] for m in msgs if m.get("role") == "assistant" and "think" in m]
    assert usati == ["high", "medium", "low"]


# ---------------------------------------------------------------------------
# ...e un server che i livelli non li accetta non deve far fallire il passo
# ---------------------------------------------------------------------------


def _backend_ollama():
    from core.backend import OllamaBackend

    return OllamaBackend("http://127.0.0.1:1")


def test_dopo_un_rifiuto_si_manda_il_booleano():
    """Si impara dal server invece di dichiararlo con una tabella di versioni
    che invecchia da sola."""
    be = _backend_ollama()
    p = GenParams(model="m", think="high")

    payload = be.build_payload([], None, p, stream=True)
    assert payload["think"] == "high"

    assert be._livello_rifiutato(payload, 'invalid value for "think"') is True
    # Da qui in poi il pensiero resta acceso: si perde la manopola, non il canale.
    assert be.build_payload([], None, p, stream=True)["think"] is True


def test_un_400_per_un_altro_motivo_non_spegne_la_manopola():
    """Una sola condizione sbaglierebbe: serve che avessimo mandato un livello
    **e** che il server nomini `think` nel motivo."""
    be = _backend_ollama()
    p = GenParams(model="m", think="high")
    payload = be.build_payload([], None, p, stream=True)

    assert be._livello_rifiutato(payload, "model 'boh' not found") is False
    assert be.build_payload([], None, p, stream=True)["think"] == "high"

    # E un 400 che nomina think ma su una richiesta che non ne mandava uno
    # (pensiero acceso col booleano) non deve toccare niente.
    booleano = be.build_payload([], None, GenParams(model="m", think=True), stream=True)
    assert be._livello_rifiutato(booleano, 'unsupported "think"') is False
