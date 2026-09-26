"""Compattazione selettiva (fast-jev-compaction con Laya al posto di Jev).

Cosa si protegge qui:

* la coppia chiamata+risultato sparisce sempre insieme (un risultato senza
  chiamata fa rifiutare la cronologia ai template severi: il 500 del 19/09);
* il testo dell'utente e dell'assistente non si tocca mai;
* lo stato mandato al valutatore sta nella finestra di Laya (1024 token);
* un valutatore giu', lento o che risponde male non ferma il turno;
* se la selezione non basta si riassume come prima.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core import agent, selezione
from core.backend import StreamEvent
from core.config import Budgets, GenParams
from core.agent import estimate_messages_tokens
from core.tools import TOOLS_SCHEMA, ToolContext


def _chiamata(cid: str, nome: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"role": "assistant", "content": "", "tool_calls": [{
        "id": cid, "type": "function",
        "function": {"name": nome, "arguments": json.dumps(args)},
    }]}


def _esito(cid: str, nome: str, corpo: dict[str, Any], ok: bool = True) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": cid, "name": nome, "ok": ok,
            "content": json.dumps(corpo)}


def _storia() -> list[dict[str, Any]]:
    grande = "x = 1\n" * 400
    return [
        {"role": "user", "content": "correggi il bug in stats.py"},
        _chiamata("r1", "read_file", {"filepath": "stats.py"}),
        _esito("r1", "read_file", {"filepath": "stats.py", "content": grande}),
        _chiamata("p1", "manage_plan", {"action": "set", "steps": ["a", "b"]}),
        _esito("p1", "manage_plan", {"status": "ok", "piano": "..." * 200}),
        _chiamata("c1", "run_command", {"command": "pytest -q"}),
        _esito("c1", "run_command", {"esito": "FALLITO", "command": "pytest -q",
                                     "returncode": 1, "stdout": "F" * 900}),
        {"role": "assistant", "content": "Il test fallisce per la media."},
        _chiamata("e1", "edit_file", {"filepath": "stats.py", "old_string": "a", "new_string": "b"}),
        _esito("e1", "edit_file", {"status": "ok", "filepath": "stats.py"}),
        _chiamata("r2", "read_file", {"filepath": "stats.py"}),
        _esito("r2", "read_file", {"filepath": "stats.py", "content": grande}),
        _chiamata("c2", "run_command", {"command": "pytest -q"}),
        _esito("c2", "run_command", {"esito": "ok", "command": "pytest -q", "returncode": 0}),
        _chiamata("q1", "ask_user_question", {"question": "va bene?"}),
        {"role": "tool", "tool_call_id": "q1", "name": "ask_user_question", "ok": True,
         "answered": True, "content": json.dumps({"user_answer": "si'"})},
    ]


# ---------------------------------------------------------------------------
# Candidati e fatti
# ---------------------------------------------------------------------------


def test_candidati_escludono_le_risposte_umane():
    msgs = _storia()
    ids = [c.id for c in selezione.candidati(msgs, 0, len(msgs))]
    assert "q1" not in ids
    assert ids == ["r1", "p1", "c1", "e1", "r2", "c2"]


def test_fatti_successivi_sono_certi():
    msgs = _storia()
    cand = {c.id: c for c in selezione.candidati(msgs, 0, len(msgs))}
    selezione.annota_fatti(msgs, list(cand.values()))
    assert cand["r1"].superata          # riletto e modificato dopo
    assert any("modified later" in f for f in cand["r1"].fatti)
    assert cand["c1"].superata          # rilanciato e riuscito dopo
    assert "succeeded" in " ".join(cand["c1"].fatti)
    assert not cand["r2"].superata      # l'ultima lettura e' quella buona
    assert not cand["c2"].superata


def test_le_regole_decidono_senza_valutatore():
    msgs = _storia()
    sel = selezione.seleziona(msgs, 0, len(msgs), obiettivo="correggi il bug in stats.py")
    d = sel.record["decisioni"]
    assert d["r1"] == "elimina"          # lettura superata
    assert d["p1"] == "elimina"          # il piano vive nel blocco di coda
    assert d["c1"] == "tronca"           # comando rilanciato: la chiamata resta
    assert "r2" not in d                 # lettura attuale di un file citato: resta
    assert sel.valutatore == "regole" and sel.domande == 0


def test_decidi_e_la_regola_di_fast_jev():
    assert selezione.decidi(0.1, 0.9) == "tieni"
    assert selezione.decidi(0.9, 0.2) == "tronca"
    assert selezione.decidi(0.2, 0.2) == "elimina"
    assert selezione.decidi(0.5, 0.49) == "tronca"


def test_lo_stato_sta_nella_finestra_di_laya():
    msgs = _storia()
    c = selezione.candidati(msgs, 0, len(msgs))[0]
    c.esito = "y" * 50_000
    stato = selezione.stato_locale(c, "obiettivo " * 400, piano="p " * 400)
    assert len(json.dumps(stato, ensure_ascii=False)) <= selezione.MAX_CARATTERI_STATO
    assert stato["call"]["tool"] == "read_file"


# ---------------------------------------------------------------------------
# Valutatore /v1/systemone (Laya o Jev)
# ---------------------------------------------------------------------------


class _Risposta:
    def __init__(self, status: int, dati: Any) -> None:
        self.status_code = status
        self._dati = dati

    def json(self) -> Any:
        return self._dati


class _ClientFinto:
    def __init__(self, risposte: list[_Risposta]) -> None:
        self.risposte = risposte
        self.corpi: list[dict[str, Any]] = []

    def post(self, url: str, json: Any, headers: Any, timeout: float) -> _Risposta:
        self.corpi.append({"url": url, "json": json, "headers": headers})
        return self.risposte.pop(0) if self.risposte else _Risposta(500, {})


def test_il_client_parla_il_protocollo_di_jev():
    client = _ClientFinto([_Risposta(200, {"answers": {
        "keep_call": {"noul": 0.9}, "keep_result": {"noul": 0.1}}})])
    v = selezione.ValutatoreSystemOne("http://127.0.0.1:8000/v1/systemone", client=client)
    p = v.valuta({"goal": "x"}, {"keep_call": {}, "keep_result": {}})
    assert p == {"keep_call": 0.9, "keep_result": 0.1}
    corpo = client.corpi[0]["json"]
    assert corpo["model"] == "multilingual" and set(corpo) == {"model", "state", "questions"}
    assert "authorization" not in client.corpi[0]["headers"]
    assert v.nome == "laya"


def test_la_chiave_di_jev_viene_solo_dall_ambiente(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "k-test")
    v = selezione.ValutatoreSystemOne("https://api.typesafe.ai/v1/systemone", "jev-latest",
                                      client=_ClientFinto([]))
    assert v.chiave == "k-test" and v.nome == "jev"
    locale = selezione.ValutatoreSystemOne("http://127.0.0.1:8000/v1/systemone",
                                           client=_ClientFinto([]))
    assert locale.chiave == "", "la chiave di Jev non va mai a un altro host"
    finto = selezione.ValutatoreSystemOne("http://altro.host/typesafe.ai/v1/systemone",
                                          client=_ClientFinto([]))
    assert finto.chiave == "" and finto.nome == "laya"


def test_una_risposta_invalida_ripiega_sulle_regole():
    msgs = _storia()
    client = _ClientFinto([_Risposta(200, {"answers": {"keep_call": {"noul": 7}}})])
    v = selezione.ValutatoreSystemOne("http://127.0.0.1:8000/v1/systemone", client=client)
    sel = selezione.seleziona(msgs, 0, len(msgs), obiettivo="correggi", valutatore=v)
    assert sel.errore and sel.dal_valutatore == 0
    # Dopo il primo errore non si insiste: una richiesta sola.
    assert len(client.corpi) == 1
    assert sel.record["decisioni"]["r1"] == "elimina"      # regole


def test_il_valutatore_decide_quando_risponde():
    msgs = _storia()
    tutti_tieni = [_Risposta(200, {"answers": {"keep_call": {"noul": 0.9},
                                                "keep_result": {"noul": 0.9}}})
                   for _ in range(10)]
    v = selezione.ValutatoreSystemOne("http://x/v1/systemone", client=_ClientFinto(tutti_tieni))
    sel = selezione.seleziona(msgs, 0, len(msgs), obiettivo="correggi", valutatore=v)
    assert sel.record["decisioni"] == {} and sel.dal_valutatore == 6 and sel.domande == 12


def test_il_tempo_massimo_ferma_il_valutatore():
    msgs = _storia()
    istanti = iter([0.0, 0.0, 100.0] + [100.0] * 50)
    risposte = [_Risposta(200, {"answers": {"keep_call": {"noul": 0.9},
                                             "keep_result": {"noul": 0.9}}})
                for _ in range(10)]
    v = selezione.ValutatoreSystemOne("http://x/v1/systemone", client=_ClientFinto(risposte))
    sel = selezione.seleziona(msgs, 0, len(msgs), obiettivo="correggi", valutatore=v,
                              tempo_massimo_s=5.0, orologio=lambda: next(istanti))
    assert sel.dal_valutatore == 1       # poi le regole


# ---------------------------------------------------------------------------
# La vista del modello
# ---------------------------------------------------------------------------


def _vista(msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return agent.build_api_messages(msgs, system_prompt="S", env_header=None,
                                    compact_old_tools=True, budgets=Budgets())


def test_elimina_toglie_la_coppia_e_tronca_accorcia():
    msgs = _storia()
    sel = selezione.seleziona(msgs, 0, len(msgs), obiettivo="correggi il bug in stats.py")
    prima = _vista(msgs)
    msgs.insert(len(msgs), sel.record)
    dopo = _vista(msgs)
    ids_chiamate = {c["id"] for m in dopo for c in (m.get("tool_calls") or [])}
    ids_esiti = {m["tool_call_id"] for m in dopo if m["role"] == "tool"}
    assert ids_chiamate == ids_esiti, "mai una chiamata senza risultato o viceversa"
    assert "r1" not in ids_chiamate and "p1" not in ids_chiamate
    c1 = next(m for m in dopo if m.get("tool_call_id") == "c1")
    assert "compattazione selettiva" in c1["content"]
    assert estimate_messages_tokens(dopo) < estimate_messages_tokens(prima)
    testi = [m["content"] for m in dopo if m["role"] in ("user", "assistant") and m["content"]]
    assert "correggi il bug in stats.py" in testi
    assert "Il test fallisce per la media." in testi


def test_le_decisioni_prima_di_un_riassunto_non_valgono_piu():
    msgs = _storia()
    msgs.append({"role": selezione.RUOLO, "decisioni": {"r2": "elimina"}, "content": ""})
    msgs.append({"role": "summary", "content": "<cronologia_compattata>x</cronologia_compattata>"})
    assert selezione.decisioni_attive(msgs) == {}


def test_la_selezione_non_arriva_mai_al_modello():
    msgs = _storia()
    msgs.append({"role": selezione.RUOLO, "decisioni": {}, "content": "", "descrizione": "x"})
    assert all(m["role"] != selezione.RUOLO for m in _vista(msgs))


# ---------------------------------------------------------------------------
# Nel turno: prima la selezione, il riassunto se non basta
# ---------------------------------------------------------------------------


class _Backend:
    """Conta le chiamate di servizio (il riassunto) e risponde al turno."""

    def __init__(self, passi: list[list[StreamEvent]]) -> None:
        self.passi = iter(passi)
        self.servizio = 0

    def scope(self, _nome: str) -> _Backend:
        return self

    def stream(self, messages: Any, tools: Any, params: Any, **_: Any):
        if tools is None:
            self.servizio += 1
            yield StreamEvent("content", text="FATTO: letto stats.py\nAPERTO: nessuno")
            return
        yield from next(self.passi, [StreamEvent("content", text="Fatto.")])


def _lunga(tmp_path: Path, n: int) -> list[dict[str, Any]]:
    """Molte letture ripetute dello stesso file: tutte superate tranne l'ultima."""
    (tmp_path / "stats.py").write_text("x = 1\n" * 300, encoding="utf-8")
    msgs: list[dict[str, Any]] = [{"role": "user", "content": "studia stats.py"}]
    for i in range(n):
        msgs.append(_chiamata(f"r{i}", "read_file", {"filepath": "stats.py"}))
        msgs.append(_esito(f"r{i}", "read_file",
                           {"filepath": "stats.py", "content": f"riga {i}\n" * 300}))
        msgs.append({"role": "assistant", "content": f"Nota {i}."})
    msgs.append({"role": "user", "content": "adesso rispondi"})
    return msgs


# Solo read_file: lo schema completo (~3.700 token) su una finestra da 8k
# lascerebbe spazio a poco altro, e il test misurerebbe lo schema.
SCHEMA_PICCOLO = [t for t in TOOLS_SCHEMA if t["function"]["name"] == "read_file"]


def _turno(tmp_path: Path, msgs: list[dict[str, Any]], **kw: Any):
    backend = _Backend([[StreamEvent("content", text="Risposta.")]])
    eventi = list(agent.run_turn(
        backend=backend, params=GenParams(model="fake", num_ctx=8192),
        tools_schema=SCHEMA_PICCOLO, tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
        ui_messages=msgs, system_prompt="S", env_header=None, max_steps=3,
        require_plan=False, require_summary=False, auto_preview=False,
        estratto_pensiero=False, libreria_attiva=False, compact_max_tokens=0, **kw,
    ))
    return eventi, backend


def test_la_selezione_evita_il_riassunto(tmp_path):
    msgs = _lunga(tmp_path, 40)
    eventi, backend = _turno(tmp_path, msgs, selezione="regole")
    compattati = [e for e in eventi if isinstance(e, agent.HistoryCompacted)]
    assert compattati and "Compattazione selettiva" in compattati[0].summary
    assert backend.servizio == 0, "nessun riassunto chiesto al modello"
    assert any(m.get("role") == selezione.RUOLO for m in msgs)


def test_spenta_riassume_come_prima(tmp_path):
    msgs = _lunga(tmp_path, 40)
    eventi, backend = _turno(tmp_path, msgs)
    assert backend.servizio >= 1
    assert not any(m.get("role") == selezione.RUOLO for m in msgs)
    assert any(isinstance(e, agent.HistoryCompacted) for e in eventi)


def test_se_non_basta_si_riassume(tmp_path):
    """Letture tutte diverse e citate: le regole tengono tutto, non basta."""
    msgs: list[dict[str, Any]] = [{"role": "user", "content": " ".join(f"f{i}.py" for i in range(40))}]
    for i in range(40):
        (tmp_path / f"f{i}.py").write_text("y\n", encoding="utf-8")
        msgs.append(_chiamata(f"r{i}", "read_file", {"filepath": f"f{i}.py"}))
        msgs.append(_esito(f"r{i}", "read_file", {"filepath": f"f{i}.py", "content": "z\n" * 600}))
    msgs.append({"role": "user", "content": "rispondi"})
    _eventi, backend = _turno(tmp_path, msgs, selezione="regole")
    assert backend.servizio >= 1
    assert not any(m.get("role") == selezione.RUOLO for m in msgs)


class _Contatore:
    nome = "laya"

    def __init__(self) -> None:
        self.chiamate = 0

    def valuta(self, stato: Any, domande: dict[str, Any]) -> dict[str, float]:
        self.chiamate += 1
        return dict.fromkeys(domande, 0.0)


def test_se_nemmeno_il_tetto_basta_il_valutatore_non_viene_disturbato(tmp_path):
    """Il testo pesa piu' della quota: togliere tutte le chiamate non basta, e
    fare 2 domande a chiamata a Laya sarebbe tempo perso prima del riassunto."""
    msgs: list[dict[str, Any]] = [{"role": "user", "content": "studia " + "parole " * 3000}]
    for i in range(30):
        msgs.append(_chiamata(f"r{i}", "read_file", {"filepath": "a.py"}))
        msgs.append(_esito(f"r{i}", "read_file", {"filepath": "a.py", "content": "z\n" * 50}))
        msgs.append({"role": "assistant", "content": "analisi " * 120})
    msgs.append({"role": "user", "content": "rispondi"})
    contatore = _Contatore()
    b = Budgets()

    def pressione(m: list[dict[str, Any]]) -> float:
        api = agent.build_api_messages(m, system_prompt="S", env_header=None, budgets=b)
        return agent.context_pressure(api, 8192)

    assert pressione(msgs) > selezione.QUOTA_OBIETTIVO
    esito = agent.proponi_selezione(msgs, budgets=b, strip_thinking=True, finestra=8192,
                                    valutatore=contatore, pressione=pressione)
    assert esito is None and contatore.chiamate == 0


def test_letto_e_mai_ripreso_e_il_vicolo_cieco():
    """Il segnale osservabile che l'oracolo del replay usa col futuro."""
    msgs: list[dict[str, Any]] = [
        {"role": "user", "content": "sistema il parser"},
        _chiamata("r1", "read_file", {"filepath": "src/vecchio_modulo.py"}),
        _esito("r1", "read_file", {"filepath": "src/vecchio_modulo.py", "content": "x" * 900}),
        _chiamata("r2", "read_file", {"filepath": "src/parser.py"}),
        _esito("r2", "read_file", {"filepath": "src/parser.py", "content": "y" * 900}),
        {"role": "assistant", "content": "Il difetto e' in parser.py, riga 40."},
    ]
    cand = {c.id: c for c in selezione.candidati(msgs, 0, len(msgs))}
    selezione.annota_fatti(msgs, list(cand.values()))
    assert cand["r1"].ripreso is False and cand["r2"].ripreso is True
    assert "its subject was never mentioned again" in cand["r1"].fatti
    stato = selezione.stato_locale(cand["r1"], "sistema il parser")
    assert "its subject was never mentioned again" in stato["later"]
    # Il fatto va al valutatore, ma le regole da sole non tolgono per questo:
    # nel replay toglieva troppo di cio' che serviva dopo (30-48%).
    sel = selezione.seleziona(msgs, 0, len(msgs), obiettivo="sistema il parser")
    assert sel.record["decisioni"].get("r1") != "elimina"
    assert "r2" not in sel.record["decisioni"]
