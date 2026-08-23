"""Potatura degli argomenti, foglio di note e compattazione della cronologia.

Il problema di partenza, misurato su una sessione vera di questo progetto: su
42.126 token inviati al modello, 36.335 erano gli argomenti delle tool call e
33.312 di quelli erano il **contenuto dei file** dentro ``write_file``. Testo
gia' su disco, tenuto in cronologia per sempre in una seconda copia. La
compattazione dei risultati non lo vedeva perche' guardava i messaggi ``tool``,
e quella roba vive nei messaggi ``assistant``.

Qui si prova che quella duplicazione sparisce, che sparisce *solo* quando la
chiamata e' vecchia, e che la compattazione vera -- quella che costa una
generazione -- non lascia mai una tool call senza il suo risultato.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import compaction  # noqa: E402
from core.agent import (  # noqa: E402
    build_api_messages,
    compatta_cronologia,
    context_pressure,
)
from core.backend import StreamEvent  # noqa: E402
from core.config import Budgets, GenParams, budgets_for  # noqa: E402
from core.notes import MAX_NOTES, NoteError, Notes  # noqa: E402
from core.notes import render_block as render_notes  # noqa: E402
from core.textutils import estimate_messages_tokens  # noqa: E402
from core.tools import NOTES_TOOL, ToolContext, dispatch  # noqa: E402


# ---------------------------------------------------------------------------
# Materiale di prova
# ---------------------------------------------------------------------------


def scrittura(call_id: str, path: str, corpo: str) -> list[dict]:
    """Un blocco assistant+tool come lo produce un write_file riuscito."""
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps({"filepath": path, "content": corpo}),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": call_id,
            "name": "write_file",
            "content": json.dumps({"status": "ok", "filepath": path, "action": "creato"}),
            "ok": True,
        },
    ]


def cronologia(quanti: int, corpo_chars: int = 4000) -> list[dict]:
    msgs: list[dict] = [{"role": "user", "content": "scrivi i moduli"}]
    for i in range(quanti):
        msgs += scrittura(f"c{i}", f"mod_{i}.py", "x" * corpo_chars)
    return msgs


class FintoBackend:
    """Riassuntore finto: registra cosa gli e' stato chiesto."""

    def __init__(self, testo: str = "FATTO:\n- moduli scritti") -> None:
        self.testo = testo
        self.chiamate: list[tuple[list[dict], object]] = []

    def stream(self, messages, tools, params):  # noqa: ARG002
        self.chiamate.append((messages, params))
        yield StreamEvent("content", text=self.testo)


class BackendRotto:
    def stream(self, messages, tools, params):  # noqa: ARG002
        yield StreamEvent("error", text="il modello non risponde")


# ---------------------------------------------------------------------------
# Potatura degli argomenti gia' eseguiti
# ---------------------------------------------------------------------------


def test_il_contenuto_scritto_esce_dal_contesto():
    """La voce piu' grossa del contesto era una copia di file gia' su disco."""
    msgs = cronologia(10)
    b = Budgets(tool_result_full_window=2)

    grezzo = estimate_messages_tokens(
        build_api_messages(msgs, system_prompt="", env_header=None,
                           compact_old_tools=False, budgets=b)
    )
    potato = estimate_messages_tokens(
        build_api_messages(msgs, system_prompt="", env_header=None,
                           compact_old_tools=True, budgets=b)
    )
    assert potato < grezzo * 0.35, (grezzo, potato)


def test_le_chiamate_recenti_restano_intatte():
    """Potare una chiamata il cui risultato e' ancora integrale mostrerebbe
    l'esito di un'azione di cui non si vede piu' la richiesta."""
    msgs = cronologia(6)
    api = build_api_messages(
        msgs, system_prompt="", env_header=None,
        compact_old_tools=True, budgets=Budgets(tool_result_full_window=2),
    )
    chiamate = [c for m in api for c in (m.get("tool_calls") or [])]
    assert len(chiamate) == 6
    corpi = [json.loads(c["function"]["arguments"]).get("content", "") for c in chiamate]
    assert all("<omesso" in c for c in corpi[:-2]), "le vecchie vanno potate"
    assert not any("<omesso" in c for c in corpi[-2:]), "le recenti no"


def test_la_potatura_dice_dove_ritrovare_il_testo():
    """Un'omissione muta e' una trappola: il modello deve sapere che il file
    c'e' ancora e come rileggerlo."""
    msgs = cronologia(6)
    api = build_api_messages(
        msgs, system_prompt="", env_header=None,
        compact_old_tools=True, budgets=Budgets(tool_result_full_window=1),
    )
    prima_chiamata = next(c for m in api for c in (m.get("tool_calls") or []))
    primo = json.loads(prima_chiamata["function"]["arguments"])
    assert "read_file" in primo["content"]
    assert primo["filepath"] == "mod_0.py", "il percorso non si tocca mai"


def test_gli_argomenti_leggeri_non_si_toccano():
    """Un comando o un percorso costano poco e dicono cosa e' successo:
    riscrivere il messaggio per risparmiarli costerebbe piu' di quanto rende."""
    msgs = [
        {"role": "user", "content": "prova"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "x", "type": "function",
                "function": {"name": "run_command",
                             "arguments": json.dumps({"command": "pytest -q"})},
            }],
        },
        {"role": "tool", "tool_call_id": "x", "name": "run_command",
         "content": json.dumps({"status": "ok", "returncode": 0}), "ok": True},
    ] + cronologia(4)[1:]
    api = build_api_messages(
        msgs, system_prompt="", env_header=None,
        compact_old_tools=True, budgets=Budgets(tool_result_full_window=1),
    )
    prima_chiamata = next(c for m in api for c in (m.get("tool_calls") or []))
    args = json.loads(prima_chiamata["function"]["arguments"])
    assert args["command"] == "pytest -q"


# ---------------------------------------------------------------------------
# Confini dei blocchi
# ---------------------------------------------------------------------------


def test_i_blocchi_non_separano_una_chiamata_dal_suo_esito():
    msgs = cronologia(3)
    gruppi = compaction.blocchi(msgs)
    assert gruppi[0] == (0, 1)  # il messaggio dell'utente sta da solo
    for inizio, fine in gruppi[1:]:
        assert msgs[inizio]["role"] == "assistant"
        assert all(m["role"] == "tool" for m in msgs[inizio + 1:fine])


def test_dopo_la_compattazione_nessuna_chiamata_resta_scoperta():
    """E' l'unico errore davvero grave possibile qui: una tool_call senza
    risultato fa rifiutare la cronologia dal chat template, e l'errore arriva
    come un 400 opaco molte richieste dopo."""
    msgs = cronologia(14)
    compatta_cronologia(
        msgs, backend=FintoBackend(), params=GenParams(num_ctx=8192),
        budgets=budgets_for(8192), strip_thinking=True,
    )
    api = build_api_messages(msgs, system_prompt="", env_header=None)
    chiamate = {c["id"] for m in api for c in (m.get("tool_calls") or [])}
    esiti = {m.get("tool_call_id") for m in api if m["role"] == "tool"}
    assert chiamate == esiti


# ---------------------------------------------------------------------------
# Compattazione
# ---------------------------------------------------------------------------


def test_la_compattazione_libera_contesto():
    msgs = cronologia(14)
    params = GenParams(num_ctx=8192)
    prima = context_pressure(
        build_api_messages(msgs, system_prompt="", env_header=None), 8192
    )
    esito = compatta_cronologia(
        msgs, backend=FintoBackend(), params=params,
        budgets=budgets_for(8192), strip_thinking=True,
    )
    assert esito is not None
    dopo = context_pressure(
        build_api_messages(msgs, system_prompt="", env_header=None), 8192
    )
    assert dopo < prima


def test_la_cronologia_resta_leggibile_per_l_utente():
    """La compattazione toglie la vista al modello, non i messaggi alla chat.

    E' il motivo per cui ``ui_messages`` resta append-only: una sessione
    salvata si deve poter rileggere per intero anche mesi dopo.
    """
    msgs = cronologia(14)
    quanti = len(msgs)
    compatta_cronologia(
        msgs, backend=FintoBackend(), params=GenParams(num_ctx=8192),
        budgets=budgets_for(8192), strip_thinking=True,
    )
    assert len(msgs) == quanti + 1, "si aggiunge un cartello, non si toglie nulla"
    assert sum(1 for m in msgs if m["role"] == "summary") == 1

    api = build_api_messages(msgs, system_prompt="", env_header=None)
    testi = [m.get("content") or "" for m in api]
    assert any(t.startswith("<cronologia_compattata>") for t in testi)
    # e il modello non rivede piu' i blocchi finiti nel riassunto
    assert sum(1 for m in api if m["role"] == "tool") < 14


def test_la_richiesta_dell_utente_sopravvive_parola_per_parola():
    """Un riassunto che parafrasa la specifica puo' cambiare il compito: la
    copia la fa l'harness, non il modello."""
    msgs = cronologia(14)
    msgs[0]["content"] = "usa asyncio e NON toccare la cartella legacy"
    compatta_cronologia(
        msgs, backend=FintoBackend(), params=GenParams(num_ctx=8192),
        budgets=budgets_for(8192), strip_thinking=True,
    )
    riassunto = next(m for m in msgs if m["role"] == "summary")
    assert "NON toccare la cartella legacy" in riassunto["content"]


def test_il_riassuntore_non_riceve_i_corpi_dei_file():
    """Mandare al riassuntore il contesto integrale costerebbe quanto il
    problema che stiamo risolvendo."""
    msgs = cronologia(14, corpo_chars=8000)
    be = FintoBackend()
    compatta_cronologia(
        msgs, backend=be, params=GenParams(num_ctx=8192),
        budgets=budgets_for(8192), strip_thinking=True,
    )
    inviato = be.chiamate[0][0][1]["content"]
    assert "xxxxxxxxxxxxxxxx" not in inviato
    assert "write_file" in inviato


def test_il_riassuntore_non_pensa_e_non_ha_tool():
    """Un modello che ragiona ottomila token per riassumere vanificherebbe la
    compattazione nel momento in cui la fa."""
    msgs = cronologia(14)
    be = FintoBackend()
    compatta_cronologia(
        msgs, backend=be, params=GenParams(num_ctx=8192, think="high", max_tokens=32768),
        budgets=budgets_for(8192), strip_thinking=True,
    )
    _, params = be.chiamate[0]
    assert params.think is False
    assert params.max_tokens <= compaction.MAX_TOKEN_RIASSUNTO


def test_un_riassunto_fallito_non_tocca_la_cronologia():
    """Meglio un contesto pieno che una cronologia buttata senza riassunto."""
    msgs = cronologia(14)
    quanti = len(msgs)
    esito = compatta_cronologia(
        msgs, backend=BackendRotto(), params=GenParams(num_ctx=8192),
        budgets=budgets_for(8192), strip_thinking=True,
    )
    assert esito is None
    assert len(msgs) == quanti
    assert not any(m["role"] == "summary" for m in msgs)


def test_non_si_compatta_una_conversazione_corta():
    """Una generazione e un ricalcolo di KV cache per accorpare tre messaggi
    sono un cattivo affare."""
    msgs = cronologia(1)
    assert compatta_cronologia(
        msgs, backend=FintoBackend(), params=GenParams(num_ctx=65536),
        budgets=budgets_for(65536), strip_thinking=True,
    ) is None


def test_non_si_ricompatta_quello_che_e_gia_fuori_vista():
    """Due compattazioni di fila senza lavoro in mezzo non liberano niente e
    costerebbero un'altra generazione."""
    msgs = cronologia(14)
    be = FintoBackend()
    kw = {"backend": be, "params": GenParams(num_ctx=8192),
          "budgets": budgets_for(8192), "strip_thinking": True}
    assert compatta_cronologia(msgs, **kw) is not None
    assert compatta_cronologia(msgs, **kw) is None
    assert len(be.chiamate) == 1


def test_un_riassunto_precedente_confluisce_nel_nuovo():
    """Altrimenti la cronologia diventa una pila di riassunti di riassunti."""
    testo = compaction.trascrizione(
        [{"role": "summary", "content": "<cronologia_compattata>vecchio</cronologia_compattata>"}]
    )
    assert "RIASSUNTO PRECEDENTE" in testo and "vecchio" in testo


def test_la_trascrizione_non_sfonda_la_finestra():
    """E' quando la cronologia e' enorme che si compatta: una trascrizione piu'
    lunga di num_ctx farebbe fallire la chiamata esattamente quando serve."""
    be = FintoBackend()
    compaction.costruisci_riassunto(
        "riga di lavoro\n" * 20_000, backend=be, params=GenParams(num_ctx=4096)
    )
    inviato = be.chiamate[0][0][1]["content"]
    assert len(inviato) < 4096 * 3.6


# ---------------------------------------------------------------------------
# Foglio di note
# ---------------------------------------------------------------------------


def test_le_note_non_si_duplicano():
    """Riscrivere la stessa nota e' il sintomo di un modello che gira a vuoto:
    non lo si premia con una riga in piu'."""
    n = Notes()
    n.add("i test vanno lanciati da /work")
    n.add("i  test   vanno lanciati da /work")  # spaziatura diversa
    assert len(n) == 1


def test_il_foglio_pieno_chiede_di_fare_spazio():
    n = Notes()
    for i in range(MAX_NOTES):
        n.add(f"nota {i}")
    with pytest.raises(NoteError, match="pieno"):
        n.add("una di troppo")


def test_una_nota_si_toglie_anche_citandola_a_memoria():
    """Un modello che cita una nota la tronca: rifiutare per una virgola
    mancante costa un round-trip per niente."""
    n = Notes()
    n.add("il server va legato a 0.0.0.0, non a 127.0.0.1")
    n.remove("il server va legato a 0.0.0.0")
    assert len(n) == 0


def test_le_note_sopravvivono_alla_compattazione():
    """E' l'unico motivo per cui esistono: vivono nel blocco di coda, non
    nella cronologia."""
    msgs = cronologia(14)
    compatta_cronologia(
        msgs, backend=FintoBackend(), params=GenParams(num_ctx=8192),
        budgets=budgets_for(8192), strip_thinking=True,
    )
    n = Notes()
    n.add("il parser fallisce sui file senza newline finale")
    api = build_api_messages(
        msgs, system_prompt="", env_header=None, notes_block=render_notes(n)
    )
    assert "newline finale" in api[-1]["content"]


def test_le_note_stanno_in_coda_e_non_nel_prefisso():
    """In testa invaliderebbero il KV cache ad ogni nota scritta."""
    n = Notes()
    n.add("una nota")
    api = build_api_messages(
        [{"role": "user", "content": "ciao"}],
        system_prompt="S", env_header="E", notes_block=render_notes(n),
    )
    assert api[0]["content"] == "S" and api[1]["content"] == "E"
    assert api[-1]["role"] == "user" and "una nota" in api[-1]["content"]


def test_il_tool_delle_note_passa_dal_dispatcher(tmp_path):
    ctx = ToolContext(workspace=str(tmp_path))
    cambi: list[int] = []
    ctx.on_notes_changed = lambda n: cambi.append(len(n))

    esito = json.loads(dispatch(ctx, NOTES_TOOL, {"action": "add", "text": "scoperta"}))
    assert esito["status"] == "ok" and esito["count"] == 1
    assert cambi == [1]

    # Il tool non rimanda indietro tutte le note: sono gia' nel blocco di coda
    # ad ogni passo, e ripeterle qui sarebbe pagarle due volte.
    assert "notes" not in esito

    assert json.loads(dispatch(ctx, NOTES_TOOL, {"action": "boh"})).get("error")


# ---------------------------------------------------------------------------
# Il ciclo agentico
# ---------------------------------------------------------------------------


class BackendDiTurno:
    """Backend minimo: risponde con del testo, e riassume quando glielo si chiede.

    Distingue le due cose dall'assenza di ``tools``: e' esattamente il criterio
    che usa la compattazione, e provarlo qui vuol dire provare che il turno non
    puo' confondere una chiamata con l'altra.
    """

    def __init__(self) -> None:
        self.riassunti = 0
        self.turni = 0

    def stream(self, messages, tools, params):  # noqa: ARG002
        if tools is None:
            self.riassunti += 1
            yield StreamEvent("content", text="FATTO:\n- sette moduli scritti")
            return
        self.turni += 1
        yield StreamEvent("content", text="Fatto: ho scritto i moduli.")


def test_il_turno_compatta_da_solo_a_meta_ciclo(tmp_path):
    """E' qui che il contesto esplode: un compito da venti passi puo' saturare
    la finestra senza che l'utente scriva una riga. Aspettare la fine del turno
    vorrebbe dire aiutarlo dopo che e' morto."""
    from core import agent as agent_mod

    be = BackendDiTurno()
    # Finestra stretta di proposito: dopo la potatura degli argomenti serve una
    # cronologia molto piu' lunga per arrivare a soglia, che e' esattamente il
    # risultato voluto -- ma qui il soggetto e' la compattazione, non quanto e'
    # difficile provocarla.
    msgs = cronologia(14)
    eventi = list(
        agent_mod.run_turn(
            backend=be,
            params=GenParams(model="fake", num_ctx=4096),
            tools_schema=[],
            tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
            ui_messages=msgs,
            system_prompt="SYS",
            env_header="ENV",
            max_steps=1,
            require_summary=False,
            require_plan=False,
        )
    )
    compattazioni = [e for e in eventi if isinstance(e, agent_mod.HistoryCompacted)]
    assert len(compattazioni) == 1
    evento = compattazioni[0]
    assert evento.tokens_after < evento.tokens_before
    assert evento.summary.startswith("FATTO")
    assert be.riassunti == 1


def test_sotto_soglia_non_succede_niente(tmp_path):
    """La compattazione costa una generazione e il KV cache: non deve scattare
    per abitudine."""
    from core import agent as agent_mod

    be = BackendDiTurno()
    list(
        agent_mod.run_turn(
            backend=be,
            params=GenParams(model="fake", num_ctx=65536),
            tools_schema=[],
            tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
            ui_messages=cronologia(2),
            system_prompt="SYS",
            env_header="ENV",
            max_steps=1,
            require_summary=False,
            require_plan=False,
        )
    )
    assert be.riassunti == 0


def test_si_puo_spegnere(tmp_path):
    from core import agent as agent_mod

    be = BackendDiTurno()
    list(
        agent_mod.run_turn(
            backend=be,
            params=GenParams(model="fake", num_ctx=4096),
            tools_schema=[],
            tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
            ui_messages=cronologia(14),
            system_prompt="SYS",
            env_header="ENV",
            max_steps=1,
            require_summary=False,
            require_plan=False,
            compact_history=False,
        )
    )
    assert be.riassunti == 0
