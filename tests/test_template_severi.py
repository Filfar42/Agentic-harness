"""Template severi (Qwen3.8 ufficiale): livelli di pensiero e messaggi system.

Il template del GGUF ufficiale rifiuta con un'eccezione Jinja due cose che
l'harness mandava a ogni messaggio: il livello ``high`` (ammessi solo
``xhigh``/``medium``/``low``) e un system dopo il primo (l'environment). Qui
le parti pure della correzione e il transport Ollama; llama.cpp e il ciclo
end-to-end stanno in ``test_llamacpp.py``.
"""

from __future__ import annotations

import copy
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.backend import (
    _LIVELLI_APPRESI,
    PREFISSO_NOTA_HARNESS,
    OllamaBackend,
    livelli_dal_template,
    livelli_dal_testo,
    livello_piu_vicino,
    messaggi_per_il_filo,
)
from core.config import GenParams

ERRORE = (
    "Jinja Exception: Unexpected reasoning effort high. "
    "Supported types are xhigh (default), medium, and low."
)
QWEN38 = ("xhigh", "medium", "low")


# ---------------------------------------------------------------------------
# La traduzione del livello
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("richiesto", "atteso"),
    [
        ("high", "xhigh"),       # pari distanza da medium e xhigh: vince il piu' alto
        ("max", "xhigh"),
        ("medium", "medium"),    # ammesso: non si tocca
        ("low", "low"),
        ("minimal", "low"),
        ("xhigh", "xhigh"),
    ],
)
def test_il_livello_va_sul_piu_vicino_e_a_pari_sale(richiesto, atteso):
    assert livello_piu_vicino(richiesto, QWEN38) == atteso


def test_fuori_scala_non_si_inventa_un_livello():
    assert livello_piu_vicino("forse", QWEN38) is None
    assert livello_piu_vicino("high", ("auto", "fast")) is None


def test_l_elenco_si_legge_dal_messaggio_d_errore():
    assert livelli_dal_testo(ERRORE) == QWEN38
    # Dentro il corpo JSON di llama-server, com'e' sul filo.
    corpo = json.dumps({"error": {"code": 500, "message": ERRORE}})
    assert livelli_dal_testo(corpo) == QWEN38
    assert livelli_dal_testo("Jinja Exception: System message must be at the beginning.") == ()


def test_l_elenco_si_legge_dal_template():
    frase = (
        "{%- set r = reasoning_effort|default('xhigh') %}"
        "{{- raise_exception('Unexpected reasoning effort ' ~ reasoning_effort ~ "
        "'. Supported types are xhigh (default), medium, and low.') }}"
    )
    assert livelli_dal_template(frase) == QWEN38
    # Solo la tupla del controllo, senza la frase.
    tupla = "{%- if resolved_reasoning_effort not in ('xhigh', 'medium', 'low') %}x{% endif %}"
    assert livelli_dal_template(tupla) == QWEN38
    # Un template che non parla di reasoning_effort non dichiara niente.
    assert livelli_dal_template("{{ messages }} Supported types are a, b") == ()
    assert livelli_dal_template(None) == ()


# ---------------------------------------------------------------------------
# I system al confine di serializzazione
# ---------------------------------------------------------------------------


def _chiamata(cid):
    return {"id": cid, "type": "function", "function": {"name": "f", "arguments": "{}"}}


def test_i_system_in_testa_si_fondono_nel_primo():
    msgs = [
        {"role": "system", "content": "SYS"},
        {"role": "system", "content": "ENV"},
        {"role": "system", "content": "[Nota: turni rimossi]"},
        {"role": "user", "content": "ciao"},
    ]
    out = messaggi_per_il_filo(msgs)
    assert [m["role"] for m in out] == ["system", "user"]
    assert out[0]["content"] == "SYS\n\nENV\n\n[Nota: turni rimossi]"


def test_un_system_a_meta_diventa_una_nota_user_al_suo_posto():
    msgs = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "system", "content": "sollecito"},
        {"role": "user", "content": "u2"},
    ]
    out = messaggi_per_il_filo(msgs)
    assert [m["role"] for m in out] == ["system", "user", "assistant", "user", "user"]
    assert out[3]["content"] == f"{PREFISSO_NOTA_HARNESS} sollecito"


def test_la_nota_non_finisce_mai_fra_tool_calls_e_tool():
    msgs = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "tool_calls": [_chiamata("a"), _chiamata("b")]},
        {"role": "system", "content": "prima dei risultati"},
        {"role": "tool", "tool_call_id": "a", "content": "ra"},
        {"role": "system", "content": "fra i risultati"},
        {"role": "tool", "tool_call_id": "b", "content": "rb"},
        {"role": "assistant", "content": "fine"},
    ]
    out = messaggi_per_il_filo(msgs)
    ruoli = [m["role"] for m in out]
    assert ruoli == ["system", "user", "assistant", "tool", "tool", "user", "user", "assistant"]
    assert [m["content"] for m in out[5:7]] == [
        f"{PREFISSO_NOTA_HARNESS} prima dei risultati",
        f"{PREFISSO_NOTA_HARNESS} fra i risultati",
    ]
    # Anche in coda: un gruppo aperto alla fine resta intero.
    coda = messaggi_per_il_filo(msgs[:5])
    assert [m["role"] for m in coda] == ["system", "user", "assistant", "tool", "user"]


def test_nessun_system_dopo_il_primo_su_forme_a_caso():
    """Proprieta': qualunque sequenza esce con system solo in testa e con ogni
    assistant+tool_calls seguito subito dai suoi tool."""
    import random

    rnd = random.Random(7)
    for _ in range(300):
        msgs = []
        for _ in range(rnd.randint(0, 12)):
            ruolo = rnd.choice(["system", "user", "assistant", "tool_group"])
            if ruolo == "tool_group":
                msgs.append({"role": "assistant", "content": "", "tool_calls": [_chiamata("x")]})
                for _ in range(rnd.randint(1, 3)):
                    if rnd.random() < 0.5:
                        msgs.append({"role": "system", "content": "s"})
                    msgs.append({"role": "tool", "tool_call_id": "x", "content": "r"})
            else:
                msgs.append({"role": ruolo, "content": ruolo})
        out = messaggi_per_il_filo(msgs)
        assert all(m["role"] != "system" for m in out[1:])
        for i, m in enumerate(out):
            if m["role"] == "assistant" and m.get("tool_calls"):
                assert out[i + 1]["role"] == "tool"
            if m["role"] == "tool":
                assert out[i - 1]["role"] in ("tool", "assistant")


def test_la_conversione_non_tocca_i_messaggi_ricevuti():
    msgs = [
        {"role": "system", "content": "SYS"},
        {"role": "system", "content": "ENV"},
        {"role": "user", "content": "u"},
        {"role": "system", "content": "nota"},
    ]
    prima = copy.deepcopy(msgs)
    messaggi_per_il_filo(msgs)
    assert msgs == prima


def test_senza_system_da_spostare_la_lista_resta_quella():
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "u"}]
    assert messaggi_per_il_filo(msgs) == msgs
    assert messaggi_per_il_filo([]) == []


# ---------------------------------------------------------------------------
# Ollama: stesso template, stesso sintomo
# ---------------------------------------------------------------------------


class _Ollama(BaseHTTPRequestHandler):
    corpi: list[dict] = []
    # Livelli che il server accetta nel campo ``think``: None = tutti.
    accetta: tuple | None = None

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        corpo = json.loads(self.rfile.read(length) or b"{}")
        type(self).corpi.append(corpo)
        think = corpo.get("think")
        accetta = type(self).accetta
        if isinstance(think, str) and accetta is not None and think not in accetta:
            dati = json.dumps({"error": ERRORE.replace("high", think)}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(dati)))
            self.end_headers()
            self.wfile.write(dati)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        for riga in (
            {"message": {"role": "assistant", "content": "ok"}, "done": False},
            {"message": {"role": "assistant", "content": ""}, "done": True,
             "done_reason": "stop", "prompt_eval_count": 3, "eval_count": 1},
        ):
            self.wfile.write(json.dumps(riga).encode() + b"\n")


@pytest.fixture()
def finto_ollama():
    _Ollama.corpi = []
    _Ollama.accetta = None
    _LIVELLI_APPRESI.clear()
    server = HTTPServer(("127.0.0.1", 0), _Ollama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", _Ollama
    server.shutdown()
    server.server_close()


def _stream(url, think):
    backend = OllamaBackend(url, 30.0)
    params = GenParams(model="qwen3.8:27b", think=think)
    msgs = [{"role": "system", "content": "SYS"}, {"role": "system", "content": "ENV"},
            {"role": "user", "content": "ciao"}]
    return backend, params, list(backend.stream(msgs, None, params))


def test_ollama_impara_l_elenco_dal_500_e_riprova(finto_ollama):
    url, handler = finto_ollama
    handler.accetta = QWEN38
    backend, params, eventi = _stream(url, "high")
    assert not [e for e in eventi if e.kind == "error"]
    assert [c["think"] for c in handler.corpi] == ["high", "xhigh"]
    assert backend.livello_inviato(params) == "xhigh"
    # Un solo system anche qui (lo faceva gia' to_ollama_messages).
    assert [m["role"] for m in handler.corpi[-1]["messages"]] == ["system", "user"]


def test_ollama_con_livello_ammesso_non_cambia_niente(finto_ollama):
    url, handler = finto_ollama
    handler.accetta = QWEN38
    _, _, eventi = _stream(url, "medium")
    assert not [e for e in eventi if e.kind == "error"]
    assert [c["think"] for c in handler.corpi] == ["medium"]


def test_ollama_senza_template_severo_manda_high_come_prima(finto_ollama):
    url, handler = finto_ollama
    _, _, eventi = _stream(url, "high")
    assert not [e for e in eventi if e.kind == "error"]
    assert [c["think"] for c in handler.corpi] == ["high"]


# ---------------------------------------------------------------------------
# L'errore resta in chat
# ---------------------------------------------------------------------------

WEB = Path(__file__).resolve().parents[1] / "web"


def test_un_turno_con_il_solo_errore_non_e_vuoto():
    """``isEmpty`` contava solo drawer, domande e risposta: il turno con il
    solo riquadro d'errore veniva rimosso a fine stream."""
    js = (WEB / "app.js").read_text(encoding="utf-8")
    inizio = js.index("isEmpty() {")
    assert ".error-box" in js[inizio: inizio + 600]


def test_la_cronologia_ridisegna_l_errore_salvato():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    inizio = js.index("function renderHistory(")
    corpo = js[inizio: js.index("function sentinellaPrecedenti", inizio)]
    assert "msg.role === 'error'" in corpo
    assert "error-box" in corpo
