"""La memoria del progetto scritta dall'harness a fine turno.

Il difetto da cui nasce: nelle 81 sessioni salvate la memoria del vault non era
mai stata scritta (0 chiamate a ``manage_notes ambito='vault'`` su 2.890), e
una chat nuova del progetto ripartiva da zero. Qui si prova che:

- dopo un turno che ha lavorato, in un progetto, l'harness fa **un** passo in
  piu' e applica quello che il modello risponde;
- quel passo continua la conversazione: stesso prefisso dei passi del turno
  (sistema, environment, cronologia) e stessi schemi dei tool, cosi' il server
  riusa la cache -- con un messaggio in fondo e niente pensiero;
- non scatta dove non deve (fuori da un progetto, turno senza lavoro, stop,
  impostazione spenta) e non scavalca le voci dell'utente;
- una risposta illeggibile o una finestra piena non fanno fallire il turno.
"""

from __future__ import annotations

import json

from core import agent
from core import memoria_progetto as memoria_mod
from core import progetto as progetto_mod
from core.backend import StreamEvent
from core.config import GenParams
from core.plan import Plan
from core.tools import ToolContext
from tests.test_turn_telemetry import ScriptBackend, _usage


def _chiamata(name, arguments, ident="call_1"):
    return StreamEvent("tool_call", tool_call={
        "id": ident, "name": name, "arguments": json.dumps(arguments),
    })


def _testo(testo):
    return StreamEvent("content", text=testo)


SCHEMI = [{"type": "function", "function": {"name": n, "parameters": {}}}
          for n in ("write_file", "read_file", "run_command", "manage_plan")]


def _progetto(tmp_path, **campi):
    base = tmp_path / "progetto"
    base.mkdir()
    progetto_mod.ensure_progetto(base, nome="Casa", **campi)
    return base


def _contesto(base, **extra):
    config = progetto_mod.leggi_config(base)
    return ToolContext(
        workspace=str(base), sandbox="host", progetto_dir=str(base),
        progetto_nome=config.nome, progetto_memoria=config.memoria,
        chat_id="20260926_140000_ffff", chat_titolo="Il parser", **extra,
    )


def _turno(base, script, *, ctx=None, messaggi=None, **extra):
    backend = ScriptBackend(script)
    ctx = ctx or _contesto(base)
    messaggi = messaggi if messaggi is not None else [
        {"role": "user", "content": "sistema il parser"}]
    opzioni = {
        "backend": backend,
        "params": GenParams(model="fake", num_ctx=16384, max_tokens=2048, think="high"),
        "tools_schema": SCHEMI, "tool_ctx": ctx, "ui_messages": messaggi,
        "system_prompt": "SYS", "env_header": "ENV", "max_steps": 4,
        "enable_nudge": False, "require_summary": False, "require_plan": False,
        "plan_gate": False, "compact_history": False, "auto_preview": False,
        "estratto_pensiero": False, "libreria_attiva": False, "spec_delega": False,
        "think_watchdog": False, "initialize_workspace": False,
        "monitor_avanzamento": False,
    }
    opzioni.update(extra)
    eventi = list(agent.run_turn(**opzioni))
    return backend, eventi, messaggi, ctx


RISPOSTA_MEMORIA = json.dumps({"operazioni": [
    {"azione": "aggiungi", "tipo": "decisione",
     "testo": "Il parser gestisce le graffe annidate con una pila, non con le regex"},
    {"azione": "aggiungi", "tipo": "aperto", "testo": "Mancano i test sui file CRLF"},
]})


def _script_che_lavora(risposta_memoria=RISPOSTA_MEMORIA):
    return [
        [_chiamata("write_file", {"filepath": "parser.py", "content": "x = 1\n"}), _usage(100, 10)],
        [_testo("Ho sistemato il parser."), _usage(120, 5)],
        [_testo(risposta_memoria), _usage(130, 40)],
    ]


# ---------------------------------------------------------------------------
# Il caso che mancava: un turno che lavora lascia qualcosa alla chat dopo
# ---------------------------------------------------------------------------


def test_dopo_un_turno_che_lavora_la_memoria_si_aggiorna(tmp_path):
    base = _progetto(tmp_path)
    backend, eventi, messaggi, ctx = _turno(base, _script_che_lavora())

    assert len(backend.requests) == 3, "un passo solo in piu', per la memoria"
    memoria = progetto_mod.leggi_config(base).memoria
    assert [(v.tipo, v.autore, v.chat, v.titolo_chat) for v in memoria] == [
        ("decisione", "harness", "20260926_140000_ffff", "Il parser"),
        ("aperto", "harness", "20260926_140000_ffff", "Il parser"),
    ]
    # Il ToolContext si allinea: e' quello che il turno dopo mette in coda.
    assert [v.testo for v in ctx.progetto_memoria] == [v.testo for v in memoria]

    # Gli eventi: inizio, fine con l'esito, e il turno che si chiude dopo.
    tipi = [type(e).__name__ for e in eventi]
    assert tipi[-3:] == ["MemoriaProgetto", "MemoriaProgetto", "TurnFinished"]
    fine = eventi[-2]
    assert fine.fase == "fine" and fine.progetto == str(base)
    assert fine.esito["riassunto"] == "+2" and len(fine.esito["aggiunte"]) == 2
    assert eventi[-1].reason == "completed"

    # La goccia resta nella cronologia per chi riapre la chat, ma al modello
    # non arriva: ``build_api_messages`` non conosce il ruolo.
    assert messaggi[-1]["role"] == "memoria"
    assert messaggi[-1]["esito"]["riassunto"] == "+2"
    api = agent.build_api_messages(messaggi, system_prompt="SYS", env_header="ENV")
    assert all("esito" not in m for m in api)


def test_il_passo_della_memoria_continua_la_conversazione(tmp_path):
    """Stesso prefisso, stessi schemi, un messaggio in fondo: la cache regge."""
    base = _progetto(tmp_path)
    # Una voce c'e' gia': cosi' i passi del turno hanno il loro blocco di coda.
    progetto_mod.aggiungi_voce(base, "usiamo SQLite", tipo="decisione")
    backend, _eventi, _messaggi, _ctx = _turno(base, _script_che_lavora())
    ultimo_passo, memoria = backend.requests[1], backend.requests[2]
    coda = ultimo_passo["messages"][-1]
    assert coda["role"] == "user" and "<memoria_del_progetto" in coda["content"]

    # Il prefisso del passo precedente, meno il suo blocco di coda (che si
    # sposta sempre in fondo, a ogni passo), e' identico.
    prefisso = ultimo_passo["messages"][:-1]
    assert memoria["messages"][: len(prefisso)] == prefisso
    # In mezzo c'e' solo la risposta finale del turno; in fondo, la richiesta.
    assert memoria["messages"][len(prefisso)]["role"] == "assistant"
    richiesta = memoria["messages"][-1]
    assert richiesta["role"] == "user"
    assert f"<{memoria_mod.TAG_RICHIESTA}" in richiesta["content"]
    # La memoria arriva una volta sola, con gli id: il blocco di coda senza id
    # qui sarebbe un doppione.
    assert "<memoria_del_progetto" not in richiesta["content"]
    assert len(memoria["messages"]) == len(prefisso) + 2
    # Gli schemi ci sono (sono parte del prefisso che il template rende), il
    # pensiero no, e la risposta ha un tetto.
    assert memoria["tools"] == SCHEMI
    assert memoria["params"].think is False
    assert memoria["params"].max_tokens <= memoria_mod.MAX_TOKEN_MEMORIA


def test_la_telemetria_del_turno_conta_il_passo_della_memoria(tmp_path):
    base = _progetto(tmp_path)
    _backend, eventi, _messaggi, _ctx = _turno(base, _script_che_lavora())
    fine = eventi[-1]
    assert fine.telemetry["totals"]["calls"] == 3
    assert "memoria_progetto" in fine.telemetry["totals"]["by_purpose"]


def test_con_due_slot_la_memoria_va_nello_slot_della_conversazione():
    """Nello slot di servizio la cronologia non c'e': si ricalcolerebbe tutta."""
    from core.backend import LlamaCppBackend

    b = LlamaCppBackend("http://127.0.0.1:1")
    b.slot_servizio = 1
    b.props = lambda: {"total_slots": 2}
    assert b.slot_per("memoria_progetto") == b.slot_per("main") == 0
    assert b.slot_per("compact") == 1


def test_la_richiesta_porta_memoria_con_gli_id_e_segna_quelle_dell_utente(tmp_path):
    base = _progetto(tmp_path)
    _c, mia = progetto_mod.aggiungi_voce(base, "mai toccare legacy/", autore="utente")
    _c, sua = progetto_mod.aggiungi_voce(base, "usiamo SQLite", tipo="decisione")
    plan = Plan()
    plan.set_steps(["scrivere il parser", "aggiungere i test CRLF"])
    plan.avanza()
    backend, _e, _m, _ctx = _turno(base, _script_che_lavora('{"operazioni": []}'),
                                    ctx=_contesto(base, plan=plan))
    testo = backend.requests[2]["messages"][-1]["content"]
    assert f"[{mia.id}] fatto (utente: non si tocca): mai toccare legacy/" in testo
    assert f"[{sua.id}] decisione: usiamo SQLite" in testo
    assert "aggiungere i test CRLF" in testo, "i punti aperti del piano aiutano i lavori aperti"


def test_le_note_della_chat_entrano_nella_richiesta(tmp_path):
    """Sono "cosa ha capito" il modello, e muoiono con la chat: la materia
    prima migliore per la memoria."""
    base = _progetto(tmp_path)
    ctx = _contesto(base)
    ctx.notes.add("il CRLF rompe il conteggio delle colonne")
    backend, _e, _m, _ctx = _turno(base, _script_che_lavora('{"operazioni": []}'), ctx=ctx)
    assert "il CRLF rompe il conteggio delle colonne" in backend.requests[2]["messages"][-1]["content"]


def test_l_harness_non_scavalca_le_voci_dell_utente(tmp_path):
    base = _progetto(tmp_path)
    _c, mia = progetto_mod.aggiungi_voce(base, "mai toccare legacy/", autore="utente")
    risposta = json.dumps({"operazioni": [{"azione": "togli", "id": mia.id}]})
    _b, eventi, _m, _ctx = _turno(base, _script_che_lavora(risposta))
    assert progetto_mod.leggi_config(base).voce(mia.id) is not None
    assert eventi[-2].esito["rifiutate"][0]["motivo"] == "voce dell'utente"
    assert eventi[-2].esito["riassunto"] == "nessun cambiamento"


def test_legge_la_memoria_dal_disco_non_dal_contesto_del_turno(tmp_path):
    """L'utente puo' averla corretta dalla schermata mentre il turno girava."""
    base = _progetto(tmp_path)
    ctx = _contesto(base)
    _c, corretta = progetto_mod.aggiungi_voce(base, "scritta durante il turno", autore="utente")
    backend, _e, _m, _ctx = _turno(base, _script_che_lavora('{"operazioni": []}'), ctx=ctx)
    assert corretta.id in backend.requests[2]["messages"][-1]["content"]


# ---------------------------------------------------------------------------
# Quando non scatta
# ---------------------------------------------------------------------------


def test_un_turno_che_non_ha_lavorato_non_paga_un_passo_in_piu(tmp_path):
    base = _progetto(tmp_path)
    backend, eventi, messaggi, _ctx = _turno(base, [[_testo("Ciao!"), _usage(10, 2)]])
    assert len(backend.requests) == 1
    assert not any(type(e).__name__ == "MemoriaProgetto" for e in eventi)
    assert messaggi[-1]["role"] == "assistant"


def test_fuori_da_un_progetto_non_scatta(tmp_path):
    backend, _eventi, _m, _ctx = _turno(
        tmp_path, _script_che_lavora(),
        ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
    )
    assert len(backend.requests) == 2


def test_con_l_impostazione_spenta_non_scatta(tmp_path):
    base = _progetto(tmp_path)
    backend, _e, _m, _ctx = _turno(base, _script_che_lavora(), memoria_progetto=False)
    assert len(backend.requests) == 2
    assert progetto_mod.leggi_config(base).memoria == ()


def test_dopo_uno_stop_non_scatta(tmp_path):
    base = _progetto(tmp_path)
    fermato = {"si": False}

    class Fermante(ScriptBackend):
        def stream(self, messages, tools, params, **kwargs):
            if len(self.requests) == 1:
                fermato["si"] = True
            yield from super().stream(messages, tools, params, **kwargs)

    backend = Fermante(_script_che_lavora())
    list(agent.run_turn(
        backend=backend, params=GenParams(model="fake", num_ctx=16384),
        tools_schema=SCHEMI, tool_ctx=_contesto(base),
        ui_messages=[{"role": "user", "content": "sistema il parser"}],
        system_prompt="SYS", env_header=None, max_steps=4, require_summary=False,
        require_plan=False, plan_gate=False, compact_history=False, auto_preview=False,
        estratto_pensiero=False, libreria_attiva=False, spec_delega=False,
        think_watchdog=False, initialize_workspace=False, monitor_avanzamento=False,
        should_stop=lambda: fermato["si"],
    ))
    assert len(backend.requests) <= 2
    assert progetto_mod.leggi_config(base).memoria == ()


# ---------------------------------------------------------------------------
# Quando va storto, il turno resta finito
# ---------------------------------------------------------------------------


def test_una_risposta_illeggibile_non_tocca_niente_e_lo_dice(tmp_path):
    base = _progetto(tmp_path)
    _b, eventi, messaggi, _ctx = _turno(base, _script_che_lavora("Certo! Ecco cosa terrei..."))
    assert progetto_mod.leggi_config(base).memoria == ()
    fine = eventi[-2]
    assert fine.fase == "fine" and "non era leggibile" in fine.motivo
    assert eventi[-1].reason == "completed"
    assert messaggi[-1]["role"] == "assistant", "niente goccia per un giro fallito"


def test_una_tool_call_al_posto_della_risposta_vale_come_niente(tmp_path):
    base = _progetto(tmp_path)
    script = _script_che_lavora()
    script[2] = [_chiamata("read_file", {"filepath": "x"}, "c9"), _usage(130, 5)]
    _b, eventi, _m, _ctx = _turno(base, script)
    assert progetto_mod.leggi_config(base).memoria == ()
    assert "risposto" in eventi[-2].motivo


def test_senza_spazio_per_la_risposta_si_rinuncia_prima_di_chiedere(tmp_path, monkeypatch):
    """Il tetto passa da ``tetto_per_la_finestra`` come i passi normali: se non
    resta spazio per una risposta utile, meglio nessuna chiamata che una
    risposta tagliata a meta' -- che sarebbe JSON illeggibile."""
    base = _progetto(tmp_path)
    monkeypatch.setattr(memoria_mod, "MAX_TOKEN_MEMORIA", 100)   # sotto TETTO_INUTILE
    backend, eventi, _m, _ctx = _turno(base, _script_che_lavora())
    assert len(backend.requests) == 2
    assert "troppo piena" in eventi[-2].motivo
    assert eventi[-1].reason == "completed"


# ---------------------------------------------------------------------------
# I pezzi puri
# ---------------------------------------------------------------------------


def test_cosa_conta_come_lavoro():
    def turno(*messaggi):
        return [{"role": "user", "content": "fai"}, *messaggi]

    scrittura = {"role": "tool", "name": "write_file", "content": '{"status": "ok"}'}
    scrittura_fallita = {"role": "tool", "name": "edit_file", "ok": False,
                         "content": '{"error": "old_string non trovato"}'}
    comando_rosso = {"role": "tool", "name": "run_command", "ok": True,
                     "content": '{"esito": "FALLITO", "returncode": 1}'}
    comando_rifiutato = {"role": "tool", "name": "run_command", "ok": False,
                         "content": '{"error": "argomenti non validi"}'}
    punto_chiuso = {"role": "tool", "name": "manage_plan", "args": {"action": "complete"},
                    "content": '{"status": "ok"}'}
    piano_letto = {"role": "tool", "name": "manage_plan", "args": {"action": "show"},
                   "content": '{"status": "ok"}'}
    lettura = {"role": "tool", "name": "read_file", "content": '{"content": "x"}'}

    assert memoria_mod.turno_ha_lavorato(turno(scrittura))
    assert memoria_mod.turno_ha_lavorato(turno(comando_rosso)), "un rosso e' una strada scartata"
    assert memoria_mod.turno_ha_lavorato(turno(punto_chiuso))
    assert not memoria_mod.turno_ha_lavorato(turno(scrittura_fallita))
    assert not memoria_mod.turno_ha_lavorato(turno(comando_rifiutato))
    assert not memoria_mod.turno_ha_lavorato(turno(piano_letto, lettura))
    # Solo il turno in corso: una scrittura del turno prima non conta.
    assert not memoria_mod.turno_ha_lavorato(
        [{"role": "user", "content": "a"}, scrittura, {"role": "user", "content": "b"}, lettura])
    # ...ma un sollecito nascosto non apre un turno nuovo.
    assert memoria_mod.turno_ha_lavorato(
        turno(scrittura, {"role": "user", "hidden": True, "content": "sollecito"}, lettura))


def test_interpreta_tollera_la_busta_non_il_contenuto():
    ops = [{"azione": "aggiungi", "testo": "x"}]
    assert memoria_mod.interpreta(json.dumps({"operazioni": ops})) == ops
    assert memoria_mod.interpreta("```json\n" + json.dumps({"operazioni": ops}) + "\n```") == ops
    assert memoria_mod.interpreta("Ecco:\n" + json.dumps({"operazioni": ops}) + "\nFine.") == ops
    assert memoria_mod.interpreta(json.dumps(ops)) == ops
    assert memoria_mod.interpreta('{"operazioni": []}') == []
    assert memoria_mod.interpreta("niente da aggiungere") is None
    assert memoria_mod.interpreta("") is None
    assert memoria_mod.interpreta('{"altro": 1}') is None


def test_il_riassunto_della_goccia():
    assert memoria_mod.riassunto_esito({"aggiunte": [1, 2], "modificate": [1], "tolte": [1, 2]}) \
        == "+2 · 1 modificata · 2 tolte"
    assert memoria_mod.riassunto_esito({}) == "nessun cambiamento"
