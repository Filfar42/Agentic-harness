"""Il ciclo esplicito del 25/09: segnali, reti, monitor di avanzamento, ripresa.

Ogni gruppo di test risale a un guasto misurato sui log di agosto-settembre
(vedi ``claude output/RIPROGETTAZIONE_2026-09-25.md``, fase 1):

* F6 -- la chiamata scritta nel testo passava per risposta finale dal secondo
  passo in poi: ``rete_canale``;
* F8 -- "Fatto." accettato senza nessuna scrittura: ``rete_senza_prova``;
* F1/F7 -- turni finiti al tetto dei passi girando a vuoto con forme diverse:
  il monitor di avanzamento (``core/ciclo/avanzamento.py``);
* F1 -- dopo un "continua" le verifiche rosse del turno prima sparivano:
  checkpoint e ``<ripresa>`` (``core/ciclo/ripresa.py``);
* residuo "exactly-once" dell'audit del 6/09 -- una chiamata senza risultato
  (crash a meta' effetto) faceva rifiutare la cronologia ai template severi:
  ``ripara_orfani`` e il diario degli effetti.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from core import agent
from core.backend import StreamEvent
from core.ciclo import avanzamento as av
from core.ciclo import reti, ripresa, segnali
from core.ciclo.stato import StatoTurno
from core.config import GenParams
from core.tools import TOOLS_SCHEMA, ToolContext
from core.verifiche import ROSSA, RegistroVerifiche

# ---------------------------------------------------------------------------
# Segnali nel testo
# ---------------------------------------------------------------------------


def test_le_chiamate_scritte_nel_testo_si_riconoscono_per_nome():
    testo = 'Ora modifico il file:\n{"name": "edit_file", "arguments": {"filepath": "a.py"}}'
    assert segnali.nomi_chiamate_nel_testo(testo) == ["edit_file"]
    tag = '<tool_call>{"name": "run_command", "arguments": {"command": "pytest"}}</tool_call>'
    assert segnali.nomi_chiamate_nel_testo(tag) == ["run_command"]


def test_un_json_qualunque_non_e_una_chiamata():
    assert segnali.nomi_chiamate_nel_testo('{"name": "Mario", "eta": 3}') == []
    assert segnali.nomi_chiamate_nel_testo("Nessun JSON qui.") == []


def test_la_prosecuzione_e_stretta():
    for s in ("continua", "Continua pure", "ok, vai avanti", "prosegui con il punto 3"):
        assert segnali.e_una_prosecuzione(s), s
    for s in ("", "scrivi un parser", "continua" + " x" * 60, "perche' ti sei fermato?"):
        assert not segnali.e_una_prosecuzione(s), s


def test_dichiarazione_e_richiesta_di_modifica():
    assert segnali.dichiara_completamento("Fatto: ho corretto il bug.")
    assert segnali.chiede_modifiche("correggi il bug in stats.py")
    assert not segnali.chiede_modifiche("cosa fa questa funzione?")


# ---------------------------------------------------------------------------
# Reti senza tool
# ---------------------------------------------------------------------------


def _contesto(**kw: Any) -> reti.Contesto:
    stato = kw.pop("stato", None) or StatoTurno(max_passi=20, max_passi_di_servizio=8)
    base: dict[str, Any] = {
        "stato": stato, "passo": 2, "max_passi": 20, "risposta": "", "ragionamento": "",
        "done_reason": "stop", "richiesta": "correggi il bug in stats.py", "piano": None,
        "verifiche": None, "tool_ctx": SimpleNamespace(readonly_request=False, new_symbols={}),
    }
    base.update(kw)
    return reti.Contesto(**base)


def test_canale_la_chiamata_nel_testo_riceve_la_verita_non_l_esecuzione():
    c = _contesto(risposta='{"name": "edit_file", "arguments": {}}',
                  chiamate_nel_testo=["edit_file"])
    iv = reti.decidi_senza_tool(c)
    assert iv is not None and iv.rete == "canale"
    assert "`edit_file`" in iv.sollecito
    assert not iv.chiudi


def test_canale_esaurito_chiude_il_turno_con_un_errore_detto():
    stato = StatoTurno(max_passi=20, max_passi_di_servizio=8)
    stato.scatti["canale"] = reti.MAX_CANALE
    c = _contesto(stato=stato, risposta="{...}", chiamate_nel_testo=["write_file"])
    iv = reti.rete_canale(c)
    assert iv is not None and iv.chiudi == "error"
    assert "function calling" in iv.errore_dopo


def test_senza_prova_fatto_senza_scritture_viene_rimandato():
    c = _contesto(risposta="Fatto: il bug e' corretto.")
    iv = reti.decidi_senza_tool(c)
    assert iv is not None and iv.rete == "senza_prova"


def test_senza_prova_tace_se_c_e_una_scrittura_o_la_richiesta_e_una_domanda():
    stato = StatoTurno(max_passi=20, max_passi_di_servizio=8)
    stato.scritture_riuscite = 1
    assert reti.rete_senza_prova(_contesto(stato=stato, risposta="Fatto.")) is None
    assert reti.rete_senza_prova(
        _contesto(richiesta="cosa fa stats.py?", risposta="Fatto: ecco la spiegazione.")
    ) is None


def test_piano_aperto_rimanda_la_vittoria_prematura():
    passo = SimpleNamespace(id=2, text="aggiungere i test")
    piano = SimpleNamespace(open_steps=[passo], current=passo)
    c = _contesto(risposta="Ho finito tutto.", piano=piano)
    iv = reti.rete_piano_aperto(c)
    assert iv is not None and "2. aggiungere i test" in iv.sollecito


def test_l_uscita_concessa_non_viene_murata():
    """Il monitor offre "chiudi dicendo cosa blocca": dopo, le reti che
    pretendono altro lavoro tacciono. Senza, l'uscita offerta era un muro."""
    stato = StatoTurno(max_passi=20, max_passi_di_servizio=8)
    stato.uscita_concessa = True
    passo = SimpleNamespace(id=1, text="x")
    registro = SimpleNamespace(unresolved=("pytest -q", 2, 1), pendenti=[])
    c = _contesto(stato=stato, risposta="Non riesco: manca la dipendenza X.",
                  piano=SimpleNamespace(open_steps=[passo], current=passo), verifiche=registro)
    assert reti.rete_verifica(c) is None
    assert reti.rete_piano_aperto(c) is None
    assert reti.rete_senza_prova(c) is None


def test_le_reti_hanno_un_ordine_fisso_e_la_prima_vince():
    nomi = [f.__name__ for f in reti.RETI_SENZA_TOOL]
    assert nomi.index("rete_canale") < nomi.index("rete_verifica") < nomi.index("rete_senza_prova")
    # Una risposta che e' sia una chiamata nel testo sia un "fatto": vince il canale.
    c = _contesto(risposta='Fatto. {"name": "write_file", "arguments": {}}',
                  chiamate_nel_testo=["write_file"])
    assert reti.decidi_senza_tool(c).rete == "canale"


# ---------------------------------------------------------------------------
# Monitor di avanzamento
# ---------------------------------------------------------------------------


def _chiamata(nome: str, args: dict[str, Any], esito: dict[str, Any], ok: bool = True) -> av.Chiamata:
    return av.Chiamata(nome=nome, args=args, ok=ok, esito=esito)


def test_una_lettura_nuova_e_progresso_la_stessa_no():
    a = av.Avanzamento()
    lettura = _chiamata("read_file", {"filepath": "a.py"}, {"filepath": "a.py", "content": "x"})
    assert a.registra_passo(1, [lettura])
    assert not a.registra_passo(2, [lettura])
    invariato = _chiamata("read_file", {"filepath": "b.py"}, {"filepath": "b.py", "status": "invariato"})
    assert not a.registra_passo(3, [invariato])
    assert a.senza_progresso == 2


def test_una_scrittura_conta_solo_se_porta_a_un_contenuto_nuovo():
    a = av.Avanzamento()
    s1 = _chiamata("write_file", {"filepath": "a.py"}, {"filepath": "a.py", "sha256": "1"})
    s2 = _chiamata("write_file", {"filepath": "a.py"}, {"filepath": "a.py", "sha256": "2"})
    assert a.registra_passo(1, [s1])
    assert a.registra_passo(2, [s2])
    assert not a.registra_passo(3, [s1]), "tornare a un contenuto gia' visto non e' progresso"


def test_comandi_muti_ricerche_vuote_e_fallimenti_non_sono_progresso():
    a = av.Avanzamento()
    assert not a.registra_passo(1, [_chiamata("run_command", {"command": "ls -la"},
                                               {"esito": "ok", "command": "ls -la"})])
    assert not a.registra_passo(2, [_chiamata("search_files", {"pattern": "foo"},
                                               {"matches": ["(nessuna corrispondenza)"]})])
    assert not a.registra_passo(3, [_chiamata("run_command", {"command": "pytest"},
                                               {"esito": "fallito"}, ok=False)])
    assert a.registra_passo(4, [_chiamata("run_command", {"command": "pytest -q"},
                                           {"esito": "ok", "command": "pytest -q"})])
    assert not a.registra_passo(5, [_chiamata("run_command", {"command": "pytest -q"},
                                               {"esito": "ok", "command": "pytest -q"})])


def test_soglie_del_monitor():
    a = av.Avanzamento()
    for passo in range(1, av.RIORIENTA_DOPO + 1):
        a.passo_a_vuoto(f"passo {passo}")
    assert a.da_riorientare and not a.da_chiudere
    a.riorientato = True
    assert not a.da_riorientare
    for _ in range(av.CHIUDI_DOPO - av.RIORIENTA_DOPO):
        a.passo_a_vuoto()
    assert a.da_chiudere
    assert len(a.ultime) <= 5


def test_il_riorientamento_concede_l_uscita_e_scatta_una_volta():
    stato = StatoTurno(max_passi=30, max_passi_di_servizio=8)
    for _ in range(av.RIORIENTA_DOPO):
        stato.avanzamento.passo_a_vuoto("read_file(a.py) ok")
    c = reti.ContestoDopo(stato=stato, passo=7, max_passi=30, recuperate=False,
                          tool_ctx=SimpleNamespace(plan=None))
    note = reti.note_dopo_tool(c)
    assert [n.conteggio for n in note] == ["riorienta"]
    assert stato.uscita_concessa
    assert "read_file(a.py)" in note[0].testo
    assert reti.note_dopo_tool(c) == []


# ---------------------------------------------------------------------------
# Checkpoint, ripresa, diario degli effetti, orfani
# ---------------------------------------------------------------------------


def test_la_ripresa_vale_solo_dopo_uno_stop_e_con_un_continua():
    cp = {"versione": 1, "motivo": "max_steps"}
    assert ripresa.va_ripreso(cp, "continua")
    assert not ripresa.va_ripreso(cp, "fammi un'altra cosa")
    assert not ripresa.va_ripreso({"motivo": "completed"}, "continua")
    assert not ripresa.va_ripreso(None, "continua")


def test_le_verifiche_rosse_tornano_nel_registro():
    registro = RegistroVerifiche()
    cp = {"verifiche_rosse": [
        {"identita": "pytest -q", "comando": "pytest -q", "ambito": "suite",
         "returncode": 1, "tentativi": 3},
        {"comando": "senza identita"},
    ]}
    assert ripresa.ripristina_verifiche(registro, cp) == 1
    v = registro.verifiche["pytest -q"]
    assert v.stato == ROSSA and v.tentativi == 3
    assert registro.unresolved is not None


def test_l_intento_ha_lo_sha_di_prima_e_un_contenuto_vuoto(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    rec = ripresa.intento("c1", "edit_file", {"filepath": "a.py"}, str(tmp_path))
    assert rec["role"] == "intento" and rec["content"] == ""
    assert rec["sha_prima"] == hashlib.sha256(b"x = 1\n").hexdigest()
    cmd = ripresa.intento("c2", "run_command", {"command": "pytest"}, str(tmp_path))
    assert cmd["command"] == "pytest" and "sha_prima" not in cmd


def _assistente_con(chiamate: list[tuple[str, str, dict[str, Any]]]) -> dict[str, Any]:
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": i, "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
        for i, n, a in chiamate
    ]}


def test_orfano_con_intento_e_file_cambiato_dice_che_l_effetto_c_e_stato(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("prima\n", encoding="utf-8")
    msgs: list[dict[str, Any]] = [
        {"role": "user", "content": "modifica a.py"},
        _assistente_con([("c1", "edit_file", {"filepath": "a.py"}),
                         ("c2", "read_file", {"filepath": "a.py"})]),
    ]
    msgs.append(ripresa.intento("c1", "edit_file", {"filepath": "a.py"}, str(tmp_path)))
    f.write_text("dopo\n", encoding="utf-8")        # l'effetto e' avvenuto, poi il crash
    assert ripresa.ripara_orfani(msgs, str(tmp_path)) == 2
    esiti = [m for m in msgs if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in esiti] == ["c1", "c2"]
    corpo = json.loads(esiti[0]["content"])
    assert corpo["error_code"] == "esito_sconosciuto"
    assert corpo["effetto"].startswith("avvenuto")
    senza_intento = json.loads(esiti[1]["content"])
    assert "Non si sa se" in senza_intento["hint"]
    # Una seconda passata non ripara niente: l'operazione e' idempotente.
    assert ripresa.ripara_orfani(msgs, str(tmp_path)) == 0


def test_orfano_con_file_identico_dice_che_si_puo_ripetere(tmp_path):
    (tmp_path / "a.py").write_text("uguale\n", encoding="utf-8")
    msgs = [_assistente_con([("c1", "write_file", {"filepath": "a.py"})])]
    msgs.append(ripresa.intento("c1", "write_file", {"filepath": "a.py"}, str(tmp_path)))
    ripresa.ripara_orfani(msgs, str(tmp_path))
    corpo = json.loads(msgs[-1]["content"])
    assert corpo["effetto"].startswith("non avvenuto")


def test_la_domanda_in_sospeso_non_e_un_orfano():
    msgs = [
        _assistente_con([("q1", "ask_user_question", {"question": "quale?"})]),
        {"role": "pending_question", "tool_call_id": "q1", "question": "quale?"},
    ]
    assert ripresa.ripara_orfani(msgs) == 0


def test_l_istantanea_contiene_solo_fatti():
    stato = StatoTurno(max_passi=10, max_passi_di_servizio=4)
    stato.solleciti["verify"] = 2
    foto = stato.istantanea(motivo="max_steps", passi=10)
    assert foto["versione"] == 1 and foto["motivo"] == "max_steps"
    assert set(foto) >= {"verifiche_rosse", "falliti", "scritti", "solleciti", "senza_progresso"}
    json.dumps(foto)  # serializzabile: finisce nella sessione


# ---------------------------------------------------------------------------
# Integrazione con run_turn
# ---------------------------------------------------------------------------


class ScriptBackend:
    def __init__(self, steps: list[list[StreamEvent]]) -> None:
        self.steps = iter(steps)
        self.requests: list[Any] = []

    def stream(self, messages: Any, tools: Any, params: Any, **_: Any):
        self.requests.append(messages)
        yield from next(self.steps, [StreamEvent("content", text="Terminato.")])


def _call(name: str, args: dict[str, Any], ident: str) -> StreamEvent:
    return StreamEvent("tool_call", tool_call={"id": ident, "name": name,
                                               "arguments": json.dumps(args)})


def _gira(tmp_path: Path, steps: list[list[StreamEvent]], richiesta: str, **opzioni: Any):
    backend = ScriptBackend(steps)
    messaggi = opzioni.pop("messaggi", None) or [{"role": "user", "content": richiesta}]
    kwargs: dict[str, Any] = {
        "backend": backend, "params": GenParams(model="fake"), "tools_schema": TOOLS_SCHEMA,
        "tool_ctx": ToolContext(workspace=str(tmp_path), sandbox="host"),
        "ui_messages": messaggi, "system_prompt": "SYS", "env_header": None,
        "max_steps": 12, "require_plan": False, "compact_history": False,
        "auto_preview": False, "estratto_pensiero": False, "libreria_attiva": False,
    }
    kwargs.update(opzioni)
    eventi = list(agent.run_turn(**kwargs))
    return eventi, messaggi, backend


def test_la_chiamata_nel_testo_al_secondo_passo_non_chiude_il_turno(tmp_path):
    """F6: prima il JSON diventava la risposta finale e il file restava intatto."""
    (tmp_path / "a.txt").write_text("vecchio\n", encoding="utf-8")
    passi = [
        [_call("read_file", {"filepath": "a.txt"}, "c1")],
        [StreamEvent("content", text=json.dumps(
            {"name": "write_file", "arguments": {"filepath": "a.txt", "content": "nuovo\n"}}))],
        [_call("write_file", {"filepath": "a.txt", "content": "nuovo\n"}, "c2")],
        [StreamEvent("content", text="Fatto: a.txt ora contiene 'nuovo'.")],
    ]
    eventi, messaggi, _ = _gira(tmp_path, passi, "sostituisci il contenuto di a.txt con 'nuovo'")
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "nuovo\n"
    assert any(m.get("hidden") and "`write_file`" in m.get("content", "") for m in messaggi)
    assert eventi[-1].reason == "completed"


def test_il_diario_scrive_l_intento_prima_dell_effetto(tmp_path):
    passi = [[_call("write_file", {"filepath": "b.txt", "content": "ciao\n"}, "w1")],
             [StreamEvent("content", text="Fatto: creato b.txt.")]]
    _eventi, messaggi, _ = _gira(tmp_path, passi, "crea b.txt con 'ciao'")
    ruoli = [(m.get("role"), m.get("tool_call_id")) for m in messaggi]
    i_intento = ruoli.index(("intento", "w1"))
    i_esito = ruoli.index(("tool", "w1"))
    assert i_intento < i_esito
    assert messaggi[i_intento]["sha_prima"] is None   # il file non esisteva
    assert not messaggi[i_intento].get("hidden")


def test_l_intento_non_arriva_mai_al_modello(tmp_path):
    passi = [[_call("write_file", {"filepath": "b.txt", "content": "x\n"}, "w1")],
             [_call("read_file", {"filepath": "b.txt"}, "r1")],
             [StreamEvent("content", text="Fatto: creato b.txt.")]]
    _eventi, _messaggi, backend = _gira(tmp_path, passi, "crea b.txt")
    for richiesta in backend.requests:
        assert all(m.get("role") != "intento" for m in richiesta)


def test_il_checkpoint_esce_dal_turno_e_la_ripresa_rimette_i_rossi(tmp_path):
    passi = [[_call("run_command", {"command": "python -c \"import sys; sys.exit(1)\""}, "r1")]] * 3
    eventi, messaggi, _ = _gira(tmp_path, passi, "fai passare la verifica", max_steps=3,
                                require_summary=False)
    fine = eventi[-1]
    assert fine.reason == "max_steps"
    cp = fine.checkpoint
    assert cp["motivo"] == "max_steps" and cp["verifiche_rosse"]

    messaggi.append({"role": "user", "content": "continua"})
    passi2 = [[StreamEvent("content", text="Ancora rosso: serve la dipendenza X.")]]
    _e2, messaggi2, backend2 = _gira(tmp_path, passi2, "", messaggi=messaggi,
                                     checkpoint_precedente=cp, max_steps=4)
    note = [m for m in messaggi2 if m.get("kind") == "ripresa"]
    assert note and "<ripresa>" in note[0]["content"]
    visto = json.dumps(backend2.requests[0], ensure_ascii=False)
    assert "<ripresa>" in visto


def test_il_monitor_chiude_un_turno_che_gira_a_vuoto(tmp_path):
    """F1/F7: la stessa lettura, all'infinito. Le reti di forma scattano una
    volta e tacciono; il monitor di effetto no."""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    passi = [[_call("read_file", {"filepath": "a.py"}, f"r{i}")] for i in range(40)]
    eventi, messaggi, _ = _gira(tmp_path, passi, "trova il bug in a.py", max_steps=40,
                                require_summary=False)
    fine = eventi[-1]
    assert fine.reason == "stallo"
    assert fine.steps < 40
    assert any(m.get("hidden") and "<avanzamento>" not in m.get("content", "")
               and "passi" in m.get("content", "") for m in messaggi)


def test_senza_monitor_il_turno_arriva_al_tetto(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    passi = [[_call("read_file", {"filepath": "a.py"}, f"r{i}")] for i in range(40)]
    eventi, _messaggi, _ = _gira(tmp_path, passi, "trova il bug in a.py", max_steps=14,
                                 require_summary=False, monitor_avanzamento=False)
    assert eventi[-1].reason == "max_steps"
