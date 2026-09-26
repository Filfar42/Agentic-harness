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

from core import compaction
from core import config as config_mod
from core.agent import (
    build_api_messages,
    compatta_cronologia,
    context_pressure,
)
from core.backend import StreamEvent
from core.config import Budgets, GenParams, budgets_for
from core.notes import MAX_NOTES, NoteError, Notes
from core.notes import render_block as render_notes
from core.textutils import estimate_messages_tokens
from core.tools import NOTES_TOOL, ToolContext, dispatch


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

    def stream(self, messages, tools, params):
        self.chiamate.append((messages, params))
        yield StreamEvent("content", text=self.testo)


class BackendRotto:
    def stream(self, messages, tools, params):
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
    msgs = [{"role": "user", "content": "prova"}, {"role": "assistant", "content": "", "tool_calls": [{"id": "x", "type": "function", "function": {"name": "run_command", "arguments": json.dumps({"command": "pytest -q"})}}]}, {"role": "tool", "tool_call_id": "x", "name": "run_command", "content": json.dumps({"status": "ok", "returncode": 0}), "ok": True}, *cronologia(4)[1:]]
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

    def stream(self, messages, tools, params):
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


# ---------------------------------------------------------------------------
# Il tetto assoluto: la percentuale da sola ha smesso di funzionare
# ---------------------------------------------------------------------------


def test_la_finestra_efficace_non_supera_il_tetto():
    """Con finestre grandi la soglia in percentuale non e' raggiungibile.

    Misurato sulle 61 sessioni salvate: a 131k il picco piu' alto mai
    raggiunto (90.018 token) e' il 68,7%, sotto lo 0,75. Il tetto riporta la
    decisione su un numero assoluto.
    """
    # Finestra piccola: il tetto non c'entra, decide num_ctx.
    assert config_mod.finestra_efficace(16_384, 32_768) == 16_384
    # Finestra grande: decide il tetto, e la soglia ci cade sopra esatta.
    efficace = config_mod.finestra_efficace(131_072, 32_768)
    assert efficace < 131_072
    # La soglia in percentuale, applicata alla finestra efficace, ricade sul
    # tetto: e' tutto il senso dell'operazione.
    assert abs(efficace * compaction.SOGLIA_DEFAULT - 32_768) < 1
    # Tetto spento: si torna al comportamento di prima, senza sorprese.
    assert config_mod.finestra_efficace(131_072, 0) == 131_072


def test_la_coda_tenuta_sta_sotto_la_soglia_che_ha_fatto_compattare():
    """La trappola della correzione fatta a meta'.

    Se si abbassasse solo la soglia lasciando la coda sul num_ctx vero, la
    coda tenuta (0,35 x 131k = 45k) sarebbe **piu' grande** della soglia che
    ha fatto scattare la compattazione (32k): si compatterebbe per ritrovarsi
    sopra soglia al passo successivo, per sempre.
    """
    efficace = config_mod.finestra_efficace(131_072, 32_768)
    coda = efficace * compaction.CODA_DEFAULT
    assert coda < 32_768, "la coda tenuta deve stare sotto il tetto"


def test_su_finestra_larga_si_compatta_lo_stesso(tmp_path):
    """Il caso reale: 131k di finestra, e la compattazione deve scattare."""
    from core import agent as agent_mod

    be = BackendDiTurno()
    eventi = list(
        agent_mod.run_turn(
            backend=be,
            params=GenParams(model="fake", num_ctx=131_072),
            tools_schema=[],
            tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
            ui_messages=cronologia(14),
            system_prompt="SYS",
            env_header="ENV",
            max_steps=1,
            require_summary=False,
            require_plan=False,
            # Tetto basso quanto la finestra stretta del test qui sopra: e' lo
            # stesso contesto, ed e' il tetto -- non la percentuale -- a doverlo
            # riconoscere come troppo.
            compact_max_tokens=int(4096 * compaction.SOGLIA_DEFAULT),
        )
    )
    assert [e for e in eventi if isinstance(e, agent_mod.HistoryCompacted)]
    assert be.riassunti == 1


def test_senza_tetto_la_finestra_larga_non_compatta_piu(tmp_path):
    """La prova che il difetto c'era: stesso contesto, tetto spento, niente."""
    from core import agent as agent_mod

    be = BackendDiTurno()
    list(
        agent_mod.run_turn(
            backend=be,
            params=GenParams(model="fake", num_ctx=131_072),
            tools_schema=[],
            tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
            ui_messages=cronologia(14),
            system_prompt="SYS",
            env_header="ENV",
            max_steps=1,
            require_summary=False,
            require_plan=False,
            compact_max_tokens=0,
        )
    )
    assert be.riassunti == 0


# ---------------------------------------------------------------------------
# L'ordine fra le due difese
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("soglia_utente", [0.60, 0.75, 0.85, 0.95])
def test_lo_scarto_dei_turni_resta_l_ultima_spiaggia(soglia_utente):
    """``drop_oldest_turns`` perde informazione; ``compatta_cronologia`` la riassume.

    Il campo nell'interfaccia arriva a 0,95. Con una soglia utente sopra 0,75 e
    una finestra sotto i 32k, lo scarto scattava PRIMA della compattazione: chi
    alzava la soglia per tenersi piu' cronologia se la vedeva buttare via senza
    che nessuno l'avesse riassunta -- l'esatto contrario di quello che aveva
    chiesto.
    """
    from core.config import HISTORY_COMPACT_THRESHOLD, finestra_efficace

    num_ctx = 16_384
    margine = max(HISTORY_COMPACT_THRESHOLD, soglia_utente)
    compatta_a = finestra_efficace(num_ctx) * soglia_utente
    scarta_a = num_ctx * margine
    assert compatta_a <= scarta_a, (
        f"con soglia {soglia_utente} lo scarto dei turni scatta a {scarta_a:.0f} "
        f"token e la compattazione a {compatta_a:.0f}: si perde cronologia che "
        f"sarebbe stata riassunta"
    )


def test_lo_scarto_dei_turni_e_lineare_non_quadratico():
    """80 ms su 800 messaggi, e il costo per messaggio cresceva con la dimensione.

    Ogni ``pop(0)`` ricalcolava la pressione sull'intera lista -- cioe' proprio
    quando questa funzione serve, che e' quando la cronologia e' enorme.
    """
    import time as _t

    from core.agent import drop_oldest_turns

    def cronologia(coppie: int) -> list[dict]:
        msgs: list[dict] = [{"role": "system", "content": "x" * 8_000}]
        for i in range(coppie):
            msgs.append(
                {
                    "role": "assistant",
                    "content": "a" * 400,
                    "tool_calls": [
                        {
                            "id": f"c{i}",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"},
                        }
                    ],
                }
            )
            msgs.append(
                {"role": "tool", "tool_call_id": f"c{i}", "name": "read_file",
                 "content": "x" * 3_000}
            )
        return msgs

    def per_messaggio(coppie: int) -> float:
        msgs = cronologia(coppie)
        t0 = _t.perf_counter()
        drop_oldest_turns(list(msgs), 8_192)
        return (_t.perf_counter() - t0) / len(msgs)

    piccolo = per_messaggio(50)
    grande = per_messaggio(400)
    # Lineare: il costo per messaggio non deve crescere con la dimensione.
    # Quadratico dava un fattore 8; qui si tiene largo per non essere fragile
    # su una macchina lenta.
    assert grande < piccolo * 3, (
        f"costo per messaggio: {piccolo * 1000:.4f} ms a 100 messaggi, "
        f"{grande * 1000:.4f} ms a 800 -- sembra ancora quadratico"
    )


# ---------------------------------------------------------------------------
# Il risultato compattato resta leggibile
# ---------------------------------------------------------------------------


def test_un_errore_lungo_resta_json_valido_dopo_la_compattazione():
    """Si accorcia il campo, non la busta.

    ``json.dumps(payload)[:400]`` -- com'era -- taglia la stringa serializzata:
    un errore lungo arrivava al modello come `{"error": "Traceback (most rec`,
    JSON invalido in mezzo a risultati tutti ben formati. E capitava esattamente
    nel caso peggiore, perche' l'unico messaggio che il modello non riusciva a
    leggere era quello che gli spiegava cosa fosse andato storto.
    """
    from core.agent import _compact_tool_result

    lungo = {"error": "Traceback (most recent call last):\n" + "  File x, line 1\n" * 60}
    out = _compact_tool_result(json.dumps(lungo, ensure_ascii=False), full=False)
    ricostruito = json.loads(out)  # solleva se la busta e' rotta
    assert "Traceback" in ricostruito["error"]
    assert len(out) < 600, f"{len(out)} caratteri: la compattazione non ha compattato"


def test_un_errore_non_testuale_non_rompe_la_busta():
    """``error`` non e' sempre una stringa: certi tool ci mettono un oggetto."""
    from core.agent import _compact_tool_result

    out = _compact_tool_result(
        json.dumps({"error": {"code": 12, "msg": "x" * 900}}), full=False
    )
    errore = json.loads(out)["error"]
    assert errore["code"] == 12
    assert len(errore["msg"]) < 400


# ---------------------------------------------------------------------------
# Script inline potati (A7a del 25/09)
# ---------------------------------------------------------------------------


def _cronologia_comandi(esiti: list[str]) -> list[dict]:
    """Script lunghi lanciati con run_command, uno per passo, con l'esito dato."""
    script = "python3 - <<'EOF'\n" + "\n".join(f"print({i})" for i in range(120)) + "\nEOF"
    msgs: list[dict] = [{"role": "user", "content": "analizza i dati"}]
    for i, esito in enumerate(esiti):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [{
            "id": f"c{i}", "type": "function",
            "function": {"name": "run_command",
                         "arguments": json.dumps({"command": script})},
        }]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "name": "run_command",
                     "ok": True, "content": json.dumps({
                         "esito": esito, "command": script,
                         "returncode": 0 if esito == "ok" else 1, "stdout": "0\n1\n"})})
    return msgs


def test_gli_script_vecchi_riusciti_tengono_solo_la_prima_riga():
    """B2: run_command.command era il 34,9% degli argomenti inviati."""
    msgs = _cronologia_comandi(["ok"] * 6)
    api = build_api_messages(msgs, system_prompt="", env_header=None,
                             compact_old_tools=True, budgets=Budgets(tool_result_full_window=2))
    comandi = [json.loads(c["function"]["arguments"])["command"]
               for m in api for c in (m.get("tool_calls") or [])]
    assert all(c.startswith("python3 - <<'EOF' <omesso: script di 122 righe") for c in comandi[:-2])
    assert all("print(119)" in c for c in comandi[-2:]), "le recenti restano intere"


def test_uno_script_fallito_resta_intero():
    """E' la cosa da correggere: il modello deve poterlo rileggere."""
    msgs = _cronologia_comandi(["FALLITO"] + ["ok"] * 5)
    api = build_api_messages(msgs, system_prompt="", env_header=None,
                             compact_old_tools=True, budgets=Budgets(tool_result_full_window=2))
    comandi = [json.loads(c["function"]["arguments"])["command"]
               for m in api for c in (m.get("tool_calls") or [])]
    assert "print(119)" in comandi[0]
    assert "<omesso" in comandi[1]


def test_un_comando_corto_non_si_tocca():
    msgs = _cronologia_comandi(["ok"] * 6)
    for m in msgs:
        for c in m.get("tool_calls") or []:
            c["function"]["arguments"] = json.dumps({"command": "pytest -q"})
    api = build_api_messages(msgs, system_prompt="", env_header=None,
                             compact_old_tools=True, budgets=Budgets(tool_result_full_window=2))
    comandi = [json.loads(c["function"]["arguments"])["command"]
               for m in api for c in (m.get("tool_calls") or [])]
    assert comandi == ["pytest -q"] * 6


# ---------------------------------------------------------------------------
# Copie superate, compattate allo scatto (A7b del 25/09)
# ---------------------------------------------------------------------------


def _letture(ordine: list[str], chars: int = 2_000) -> list[dict]:
    """Letture intere in sequenza, ognuna con un contenuto diverso."""
    msgs: list[dict] = [{"role": "user", "content": "studia"}]
    for i, fp in enumerate(ordine):
        cid = f"r{i}"
        msgs.append({"role": "assistant", "content": "", "tool_calls": [{
            "id": cid, "type": "function",
            "function": {"name": "read_file", "arguments": json.dumps({"filepath": fp})},
        }]})
        msgs.append({"role": "tool", "tool_call_id": cid, "name": "read_file", "ok": True,
                     "content": json.dumps({"filepath": fp, "content": f"v{i}\n" * (chars // 3)})})
    return msgs


def _contenuti(api: list[dict]) -> dict[str, str]:
    return {m["tool_call_id"]: m["content"] for m in api if m.get("role") == "tool"}


def test_senza_scatto_le_copie_superate_restano():
    """Prima del primo scatto niente si tocca: il prefisso resta quello."""
    from core.agent import copie_superate, zona_integrale

    msgs = _letture(["a.py", "a.py"])
    b = Budgets(tool_result_full_window=4, tool_result_full_tokens=100_000)
    pos = [i for i, m in enumerate(msgs) if m["role"] == "tool"]
    zona, scatto = zona_integrale(msgs, pos, b)
    assert scatto == -1
    assert copie_superate(msgs, zona, scatto) == set()


def test_la_copia_superata_dentro_la_zona_viene_compattata():
    """Otto letture, zona integrale di sei: r2 legge a.py, r4 lo rilegge prima
    dello scatto. r2 e' superata ed esce dalla zona; r4 resta intera."""
    from core.agent import copie_superate, zona_integrale

    msgs = _letture(["x.py", "y.py", "a.py", "z.py", "a.py", "w.py", "k.py", "q.py"], chars=900)
    b = Budgets(tool_result_full_window=6, tool_result_full_tokens=1_500)
    pos = [i for i, m in enumerate(msgs) if m["role"] == "tool"]
    zona, scatto = zona_integrale(msgs, pos, b)
    assert scatto >= 0 and pos[2] in zona
    assert copie_superate(msgs, zona, scatto) == {pos[2]}
    api = build_api_messages(msgs, system_prompt="", env_header=None,
                             compact_old_tools=True, budgets=b)
    c = _contenuti(api)
    assert len(c["r2"]) < len(c["r4"]) / 2, "la copia vecchia e' compattata"
    assert c["r4"].count("v4") > 100, "l'ultima lettura resta intera"


def test_una_lettura_parziale_non_supera_quella_intera():
    from core.agent import copie_superate

    msgs = _letture(["a.py", "a.py"], chars=300)
    args = json.loads(msgs[3]["tool_calls"][0]["function"]["arguments"])
    args["start_line"] = 1
    msgs[3]["tool_calls"][0]["function"]["arguments"] = json.dumps(args)
    pos = [i for i, m in enumerate(msgs) if m["role"] == "tool"]
    assert copie_superate(msgs, set(pos), fino_a=pos[-1]) == set()


def test_fra_due_scatti_la_vista_dei_vecchi_messaggi_non_cambia():
    """La proprieta' che giustifica il "solo allo scatto": una rilettura
    nuova non deve riscrivere un messaggio a meta' cronologia."""
    b = Budgets(tool_result_full_window=4, tool_result_full_tokens=3_000)
    base = ["a.py", "b.py", "c.py", "d.py", "e.py", "f.py"]
    msgs = _letture(base, chars=600)
    pos = [i for i, m in enumerate(msgs) if m["role"] == "tool"]
    from core.agent import zona_integrale

    _zona, scatto_prima = zona_integrale(msgs, pos, b)
    prima = build_api_messages(msgs, system_prompt="", env_header=None,
                               compact_old_tools=True, budgets=b)
    dopo_msgs = _letture([*base, "b.py"], chars=600)
    pos2 = [i for i, m in enumerate(dopo_msgs) if m["role"] == "tool"]
    _zona2, scatto_dopo = zona_integrale(dopo_msgs, pos2, b)
    dopo = build_api_messages(dopo_msgs, system_prompt="", env_header=None,
                              compact_old_tools=True, budgets=b)
    if scatto_dopo == scatto_prima:
        assert dopo[: len(prima)] == prima
