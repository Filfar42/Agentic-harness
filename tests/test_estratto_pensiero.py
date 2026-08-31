"""L'estratto del ragionamento alla chiusura di un punto di piano.

Il fatto che motiva il meccanismo, misurato il 23/08/2026 su quattro sessioni
`qwen3.8:27b`: 2.427.071 caratteri pensati contro 108.970 di risposte, e tutto
buttato a fine passo. Dentro non c'e' ripetizione (8-grammi ripetuti 0,5%,
somiglianza fra blocchi consecutivi 0,15): il modello delibera, e deliberando
verifica cose che non sono scritte da nessun'altra parte.

Si distilla alla chiusura del punto e non ad ogni passo per la ragione gia'
misurata su questo progetto: `manage_plan action='complete'` **pretende** una
nota e ne ha ottenute 23 su 24, `manage_notes` la propone ed e' stato usato 0
volte su 42 conversazioni. Il rito batte l'invito.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import agent as agent_mod
from core import libreria, pensiero
from core.backend import StreamEvent
from core.config import GenParams
from core.plan import Plan
from core.prompts import PROMPT_ESTRATTO_PENSIERO
from core.tools import ToolContext

# Sopra `pensiero.MIN_CHARS_PENSIERO`: sotto quella soglia non si chiama
# nessuno, ed e' proprio uno dei comportamenti sotto test.
PENSIERO_LUNGO = (
    "Ho lanciato pytest e falliva con ImportError: cannot import name 'Foo' "
    "da core.bar. Aspetta, in realta' il modulo giusto e' core.baz. "
) * 30


class BackendConPiano:
    """Un passo che pensa a lungo e chiude un punto, poi tace.

    Le chiamate senza `tools` sono tre cose diverse (riassunto, riepilogo
    finale, estratto): si distinguono dal prompt di sistema, che e' l'unico
    criterio disponibile ed e' lo stesso che usano gli altri test.
    """

    def __init__(self, azione: str = "complete", risposta: str = "SCOPERTO: il modulo e' core.baz") -> None:
        self.azione = azione
        self.risposta = risposta
        self.passi = 0
        self.estrazioni: list[dict] = []

    def stream(self, messages, tools, params):
        if tools is None:
            if "SCOPERTO" in str(messages[0].get("content") or "") and "ragionamento" in str(
                messages[0].get("content") or ""
            ):
                self.estrazioni.append(
                    {"params": params, "utente": str(messages[1].get("content") or "")}
                )
                yield StreamEvent("content", text=self.risposta)
                return
            yield StreamEvent("content", text="riepilogo")
            return

        self.passi += 1
        if self.passi == 1:
            yield StreamEvent("content", text=f"<think>{PENSIERO_LUNGO}</think>")
            yield StreamEvent(
                "tool_call",
                tool_call={
                    "id": "c1",
                    "name": "manage_plan",
                    "arguments": json.dumps(
                        {
                            "action": self.azione,
                            "step_id": "1",
                            "note": "verificato con pytest, importa da core.baz",
                        }
                    ),
                },
            )
            return
        yield StreamEvent("content", text="finito")


def _turno(be, tmp_path, *, estratto_pensiero=True, libreria_attiva=True, piano=None):
    if piano is None:
        piano = Plan()
        piano.set_steps(["sistemare l'import di Foo", "far passare i test"])
        piano.avanza()
    msgs = [{"role": "user", "content": "vai"}]
    eventi = list(
        agent_mod.run_turn(
            backend=be,
            params=GenParams(model="fake", num_ctx=8192, max_tokens=2048, think="high"),
            tools_schema=[
                {"type": "function", "function": {"name": "manage_plan", "parameters": {}}}
            ],
            tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host", plan=piano),
            ui_messages=msgs,
            system_prompt="SYS",
            env_header="ENV",
            max_steps=2,
            require_plan=False,
            require_summary=False,
            enable_nudge=False,
            plan_gate=False,
            libreria_attiva=libreria_attiva,
            estratto_pensiero=estratto_pensiero,
        )
    )
    return msgs, eventi, piano


# ---------------------------------------------------------------------------
# Il legame fra un ragionamento e il punto che stava servendo
# ---------------------------------------------------------------------------


def test_la_traccia_dice_quale_punto_non_solo_se_ce_n_era_uno(tmp_path):
    """Senza l'identita' del punto non c'e' modo di sapere quale ragionamento
    apparteneva a quale lavoro: e' l'unico legame esistente."""
    be = BackendConPiano()
    msgs, _, _ = _turno(be, tmp_path)
    tracce = [m["think"] for m in msgs if m.get("role") == "assistant" and "think" in m]
    assert tracce, "nessuna traccia scritta"
    assert tracce[0]["punto"] == "1"
    # Il booleano resta: e' quello che leggono gli script di analisi gia' scritti.
    assert tracce[0]["punto_aperto"] is True


def test_il_campo_punto_non_arriva_mai_al_modello(tmp_path):
    be = BackendConPiano()
    msgs, _, _ = _turno(be, tmp_path)
    api = agent_mod.build_api_messages(msgs, system_prompt="SYS", env_header=None)
    assert '"punto"' not in json.dumps(api)


def test_si_raccolgono_solo_i_blocchi_del_punto_giusto():
    msgs = [
        {"role": "assistant", "content": "<think>del punto 1</think>ok", "think": {"punto": "1"}},
        {"role": "assistant", "content": "<think>del punto 2</think>ok", "think": {"punto": "2"}},
        {"role": "assistant", "content": "<think>senza punto</think>ok", "think": {"punto": None}},
        {"role": "user", "content": "<think>non e' dell'assistente</think>"},
    ]
    assert pensiero.blocchi_del_punto(msgs, "1") == ["del punto 1"]
    assert pensiero.blocchi_del_punto(msgs, "2") == ["del punto 2"]
    assert pensiero.blocchi_del_punto(msgs, "") == []


# ---------------------------------------------------------------------------
# L'estrazione
# ---------------------------------------------------------------------------


def test_chiudere_un_punto_archivia_l_estratto(tmp_path):
    be = BackendConPiano()
    _turno(be, tmp_path)
    assert len(be.estrazioni) == 1, "una chiamata per punto chiuso"
    voci = libreria.voci(tmp_path)
    assert len(voci) == 1
    testo = (libreria.cartella(tmp_path) / voci[0].nome).read_text(encoding="utf-8")
    assert "SCOPERTO: il modulo e' core.baz" in testo
    assert voci[0].titolo == "sistemare l'import di Foo"


def test_l_estrazione_non_pensa_e_ha_un_tetto_stretto(tmp_path):
    """Un modello che pensa ottomila token per distillare vanificherebbe la
    distillazione nel momento in cui la fa."""
    be = BackendConPiano()
    _turno(be, tmp_path)
    params = be.estrazioni[0]["params"]
    assert params.think is False
    assert params.max_tokens <= pensiero.MAX_TOKEN_ESTRATTO


def test_all_estrazione_arriva_il_pensiero_grezzo_e_il_testo_del_punto(tmp_path):
    be = BackendConPiano()
    _turno(be, tmp_path)
    utente = be.estrazioni[0]["utente"]
    assert "sistemare l'import di Foo" in utente
    assert "cannot import name 'Foo'" in utente


def test_anche_un_punto_saltato_viene_distillato(tmp_path):
    """E' il posto piu' probabile in cui nasce uno SCARTATO."""
    be = BackendConPiano(azione="skip", risposta="SCARTATO: la strada via core.bar non esiste")
    _turno(be, tmp_path)
    assert len(be.estrazioni) == 1
    voci = libreria.voci(tmp_path)
    testo = (libreria.cartella(tmp_path) / voci[0].nome).read_text(encoding="utf-8")
    assert "saltato" in testo, "un punto abbandonato non deve leggersi come una conclusione"


def test_un_punto_chiuso_dice_chiuso(tmp_path):
    be = BackendConPiano()
    _turno(be, tmp_path)
    voci = libreria.voci(tmp_path)
    testo = (libreria.cartella(tmp_path) / voci[0].nome).read_text(encoding="utf-8")
    assert "chiuso" in testo and "saltato" not in testo


# ---------------------------------------------------------------------------
# Quando NON si chiama il modello
# ---------------------------------------------------------------------------


def test_niente_estratto_se_il_pensiero_e_corto():
    """Sotto la soglia non si spende una chiamata: la nota che `complete` ha
    gia' preteso dice quanto c'era da dire."""

    class MaiChiamato:
        def stream(self, *a, **k):
            raise AssertionError("non si doveva chiamare il modello")

    assert pensiero.estrai(["corto"], punto="x", backend=MaiChiamato(), params=GenParams(model="f")) == ""


def test_niente_e_una_risposta_valida_e_non_sporca_l_indice(tmp_path):
    be = BackendConPiano(risposta="NIENTE")
    _turno(be, tmp_path)
    assert len(be.estrazioni) == 1, "il modello e' stato interrogato"
    assert libreria.voci(tmp_path) == [], "ma non si archivia una voce vuota"


def test_si_puo_spegnere(tmp_path):
    be = BackendConPiano()
    _turno(be, tmp_path, estratto_pensiero=False)
    assert be.estrazioni == []
    assert libreria.voci(tmp_path) == []


def test_senza_libreria_non_si_estrae(tmp_path):
    """Scriverebbe in una cartella il cui indice non entra in contesto: un
    estratto che nessuno rilegge e' solo una chiamata pagata."""
    be = BackendConPiano()
    _turno(be, tmp_path, libreria_attiva=False)
    assert be.estrazioni == []


def test_un_estrazione_fallita_non_fa_fallire_la_chiusura(tmp_path):
    class BackendRotto(BackendConPiano):
        def stream(self, messages, tools, params):
            if tools is None:
                yield StreamEvent("error", text="boom")
                return
            yield from BackendConPiano.stream(self, messages, tools, params)

    be = BackendRotto()
    _, _, piano = _turno(be, tmp_path)
    assert piano.get("1").status == "done", "il punto si chiude comunque"
    assert libreria.voci(tmp_path) == []


# ---------------------------------------------------------------------------
# Il risultato del tool e il prompt
# ---------------------------------------------------------------------------


def test_l_estratto_non_torna_nel_risultato_del_tool(tmp_path):
    """`manage_plan` e' stato snellito apposta: valeva il 13,5% dei token di
    risultato. L'estratto va su disco in silenzio."""
    be = BackendConPiano()
    msgs, _, _ = _turno(be, tmp_path)
    risultati = [str(m.get("content") or "") for m in msgs if m.get("role") == "tool"]
    assert risultati
    assert not any("SCOPERTO" in r for r in risultati)


def test_il_prompt_chiede_l_italiano():
    """344 blocchi su 350 pensano in inglese, ma `libreria.precarico` ripesca
    incrociando le parole italiane del punto aperto con i titoli: un estratto
    in inglese scriverebbe una libreria che nessun precarico sa ritrovare."""
    assert "In italiano" in PROMPT_ESTRATTO_PENSIERO
    assert "SCOPERTO" in PROMPT_ESTRATTO_PENSIERO
    assert "SCARTATO" in PROMPT_ESTRATTO_PENSIERO
    assert "FATTO" not in PROMPT_ESTRATTO_PENSIERO, "il fatto sta gia' nel piano"


def test_l_archivio_non_attribuisce_all_utente_le_parole_del_modello(tmp_path):
    """Il testo di un punto di piano non e' una frase dell'utente. Riusare la
    sezione della compattazione metterebbe in archivio una citazione falsa."""
    libreria.archivia_estratto(tmp_path, punto="fare la cosa", estratto="SCOPERTO: x")
    voci = libreria.voci(tmp_path)
    testo = (libreria.cartella(tmp_path) / voci[0].nome).read_text(encoding="utf-8")
    assert "Chiesto dall'utente" not in testo


def test_un_file_archiviato_non_si_riapre_mai(tmp_path):
    """Stessa regola della compattazione: nome occupato -> si appende."""
    libreria.archivia_estratto(tmp_path, punto="stessa cosa", estratto="SCOPERTO: primo")
    libreria.archivia_estratto(tmp_path, punto="stessa cosa", estratto="SCOPERTO: secondo")
    voci = libreria.voci(tmp_path)
    assert len(voci) == 2, "due voci, numeri diversi"
    testi = [(libreria.cartella(tmp_path) / v.nome).read_text(encoding="utf-8") for v in voci]
    assert any("primo" in t for t in testi) and any("secondo" in t for t in testi)


def test_richiudere_un_punto_gia_chiuso_non_lo_riarchivia(tmp_path):
    """`complete` e' idempotente sul piano e va bene cosi'. Ma la libreria e'
    append-only: un secondo estratto non correggerebbe il primo, gli si
    affiancherebbe, e l'indice che il modello rilegge ad ogni passo si
    riempirebbe di doppioni."""
    from core.tools import tool_manage_plan

    piano = Plan()
    piano.set_steps(["fare la cosa"])
    ctx = ToolContext(workspace=str(tmp_path), sandbox="host", plan=piano)
    tool_manage_plan(ctx, "complete", step_id="1", note="fatta")
    assert len(ctx.punti_chiusi) == 1
    tool_manage_plan(ctx, "complete", step_id="1", note="fatta di nuovo")
    assert len(ctx.punti_chiusi) == 1, "imbucato una volta sola"
