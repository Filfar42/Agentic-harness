"""La libreria di concetti: cosa esce dal contesto e non si perde.

Il difetto misurato il 23/08/2026 su 61 sessioni salvate: il riassunto
precedente rientrava nella trascrizione della compattazione successiva, quindi
alla terza compattazione il modello riassumeva il riassunto di un riassunto,
ogni volta attraverso lo stesso imbuto da 700 token.

Qui si prova che il testo esce dalla cronologia **e** resta leggibile, che
nessun file archiviato viene mai riscritto, e che il contesto ne paga solo una
riga.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import libreria  # noqa: E402
from core.compaction import render_messaggio, trascrizione  # noqa: E402
from core.textutils import estimate_tokens  # noqa: E402

RIASSUNTO = (
    "FATTO: aggiunto budgets_for in core/config.py, chiamato da core/agent.py.\n"
    "SCOPERTO: i test giravano sull'interprete sbagliato, serve `uv run pytest`.\n"
    "SCARTATO: passare i budget nel ToolContext -- run_turn li sovrascrive."
)


# ---------------------------------------------------------------------------
# Scrittura: numerata, append-only, e mai una riscrittura
# ---------------------------------------------------------------------------


def test_un_tratto_compattato_finisce_su_disco(tmp_path):
    voce = libreria.archivia(
        tmp_path, riassunto=RIASSUNTO, richieste=["sistema i budget adattivi"]
    )
    assert voce is not None
    assert voce.numero == 1
    assert voce.percorso.startswith(".memoria/001-")

    testo = (tmp_path / voce.percorso).read_text(encoding="utf-8")
    # La richiesta dell'utente si salva **testuale**: e' l'unica frase del
    # blocco che non e' passata da un riassunto.
    assert "«sistema i budget adattivi»" in testo
    assert "uv run pytest" in testo


def test_i_numeri_crescono_e_nessun_file_viene_riscritto(tmp_path):
    prima = libreria.archivia(tmp_path, riassunto="FATTO: uno", richieste=["compito a"])
    dopo = libreria.archivia(tmp_path, riassunto="FATTO: due", richieste=["compito b"])
    assert (prima.numero, dopo.numero) == (1, 2)
    # Il primo file e' rimasto identico: e' la regola che regge tutto il modulo.
    assert "uno" in (tmp_path / prima.percorso).read_text(encoding="utf-8")
    assert "due" not in (tmp_path / prima.percorso).read_text(encoding="utf-8")


def test_lo_stesso_titolo_appende_invece_di_sovrascrivere(tmp_path):
    """Due tratti con lo stesso titolo non devono cancellarsi a vicenda."""
    libreria.archivia(tmp_path, riassunto="FATTO: primo giro", richieste=["stesso compito"])
    libreria.archivia(tmp_path, riassunto="FATTO: secondo giro", richieste=["stesso compito"])
    corpi = [p.read_text(encoding="utf-8") for p in libreria.cartella(tmp_path).glob("*.md")]
    tutto = "\n".join(corpi)
    assert "primo giro" in tutto and "secondo giro" in tutto


def test_la_cartella_si_ignora_da_sola(tmp_path):
    """`*` in un .gitignore suo: git la salta senza toccare quello del progetto."""
    libreria.archivia(tmp_path, riassunto="FATTO: qualcosa", richieste=[])
    assert (libreria.cartella(tmp_path) / ".gitignore").read_text(encoding="utf-8") == "*\n"


def test_un_riassunto_vuoto_non_lascia_niente(tmp_path):
    assert libreria.archivia(tmp_path, riassunto="   ", richieste=["boh"]) is None
    assert not libreria.cartella(tmp_path).exists()


# ---------------------------------------------------------------------------
# L'indice: si rigenera dal disco, e in contesto pesa una riga
# ---------------------------------------------------------------------------


def test_l_indice_si_rilegge_dai_file_senza_chiamare_il_modello(tmp_path):
    """E' l'unica cosa che puo' decadere, perche' e' l'unica ricostruibile."""
    libreria.archivia(tmp_path, riassunto=RIASSUNTO, richieste=["primo compito"])
    libreria.archivia(tmp_path, riassunto=RIASSUNTO, richieste=["secondo compito"])
    elenco = libreria.voci(tmp_path)
    assert [v.numero for v in elenco] == [1, 2]
    assert [v.titolo for v in elenco] == ["primo compito", "secondo compito"]


def test_in_contesto_ne_resta_una_riga(tmp_path):
    """Il baratto: il testo esce, l'indice entra.

    Si misura sul **margine**, non sulla prima voce: il blocco ha una
    spiegazione fissa che si paga una volta sola, mentre ogni tratto in piu'
    costa una riga contro un file intero. E' li' che il baratto conviene, ed e'
    li' che deve essere provato -- con una voce sola il blocco pesa piu' del
    file, ed e' giusto cosi'.
    """
    for i in range(6):
        libreria.archivia(
            tmp_path, riassunto=RIASSUNTO, richieste=[f"compito numero {i}"]
        )
    elenco = libreria.voci(tmp_path)
    blocco = libreria.render_block(elenco)
    su_disco = sum(
        estimate_tokens((tmp_path / v.percorso).read_text(encoding="utf-8"))
        for v in elenco
    )
    assert estimate_tokens(blocco) < su_disco / 3

    # Il costo marginale di un tratto in piu' e' una riga, e si vede.
    una_in_meno = libreria.render_block(elenco[:-1])
    margine = estimate_tokens(blocco) - estimate_tokens(una_in_meno)
    assert margine < 30

    # ...e deve dire come riaprirli, o per il modello quella cartella non
    # esiste (le cartelle col punto non compaiono in nessun elenco).
    assert elenco[0].percorso in blocco
    assert "read_file" in blocco


def test_l_indice_lungo_si_raggruppa_senza_perdere_i_file(tmp_path):
    for i in range(libreria.MAX_VOCI_INDICE + 5):
        libreria.archivia(tmp_path, riassunto=f"FATTO: giro {i}", richieste=[f"compito {i}"])
    elenco = libreria.voci(tmp_path)
    blocco = libreria.render_block(elenco)
    assert len(elenco) == libreria.MAX_VOCI_INDICE + 5
    # Le voci vecchie escono dall'indice, ma il blocco deve dire che ci sono.
    assert "altre 5 voci" in blocco
    assert "compito 0" not in blocco
    assert "compito 28" in blocco


# ---------------------------------------------------------------------------
# Il richiamo: la meta' che rilegge non puo' essere un invito
# ---------------------------------------------------------------------------


def test_l_harness_ripesca_da_solo_cio_che_riguarda_il_punto_aperto(tmp_path):
    libreria.archivia(tmp_path, riassunto=RIASSUNTO, richieste=["sistemare i budget adattivi"])
    libreria.archivia(tmp_path, riassunto="FATTO: altro", richieste=["colorare la barra laterale"])
    elenco = libreria.voci(tmp_path)

    ripescato = libreria.precarico(tmp_path, elenco, "rivedere i budget adattivi del figlio")
    assert "budget" in ripescato
    assert "uv run pytest" in ripescato, "il contenuto, non solo il titolo"
    assert "colorare" not in ripescato


def test_niente_in_comune_niente_precarico(tmp_path):
    """Un precarico che scatta sempre e' contesto sprecato con un altro nome."""
    libreria.archivia(tmp_path, riassunto=RIASSUNTO, richieste=["sistemare i budget"])
    elenco = libreria.voci(tmp_path)
    assert libreria.precarico(tmp_path, elenco, "rinominare il logo") == ""
    assert libreria.precarico(tmp_path, elenco, "") == ""


def test_il_precarico_ha_un_tetto(tmp_path):
    lungo = "SCOPERTO: " + ("dettaglio ripetuto sui budget adattivi. " * 400)
    libreria.archivia(tmp_path, riassunto=lungo, richieste=["budget adattivi"])
    elenco = libreria.voci(tmp_path)
    ripescato = libreria.precarico(tmp_path, elenco, "budget adattivi")
    assert len(ripescato) < libreria.MAX_PRECARICO_CHARS + 500
    assert "troncato" in ripescato


# ---------------------------------------------------------------------------
# La catena delle fotocopie, spezzata
# ---------------------------------------------------------------------------


def test_un_riassunto_archiviato_non_si_riassume_una_seconda_volta():
    """Il difetto originale, messo nero su bianco.

    Senza libreria il riassunto precedente rientra -- ed e' giusto, perche'
    altrimenti si perderebbe. Con la libreria e' su disco, e rifarlo passare da
    qui vorrebbe dire riassumere un riassunto.
    """
    messaggi = [{"role": "summary", "content": "FATTO: il lavoro di ieri, in dettaglio"}]

    senza = trascrizione(messaggi)
    assert "il lavoro di ieri, in dettaglio" in senza

    con = trascrizione(messaggi, archiviato=True)
    assert "il lavoro di ieri, in dettaglio" not in con
    assert ".memoria/" in con


def test_il_messaggio_di_compattazione_non_dice_il_falso(tmp_path):
    """"I dettagli non ci sono piu'" e' vero senza libreria e falso con.

    Una frase del prompt che descrive un comportamento dell'harness deve essere
    vera, o diventa una convinzione sbagliata su cui il modello agisce.
    """
    voce = libreria.archivia(tmp_path, riassunto=RIASSUNTO, richieste=["un compito"])

    senza = render_messaggio(RIASSUNTO, ["un compito"])
    assert "non ci sono piu'" in senza

    con = render_messaggio(RIASSUNTO, ["un compito"], voce=voce)
    assert "non ci sono piu'" not in con
    assert voce.percorso in con


# ---------------------------------------------------------------------------
# Il giro intero, dentro un turno vero
# ---------------------------------------------------------------------------


def test_il_turno_archivia_e_mette_l_indice_in_coda(tmp_path):
    """Dal turno al disco al blocco di coda, senza passaggi a mano."""
    from core import agent as agent_mod
    from core.config import GenParams
    from core.tools import ToolContext
    from tests.test_compattazione import BackendDiTurno, cronologia

    be = BackendDiTurno()
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
    assert [e for e in eventi if isinstance(e, agent_mod.HistoryCompacted)]

    elenco = libreria.voci(tmp_path)
    assert len(elenco) == 1
    assert "FATTO" in (tmp_path / elenco[0].percorso).read_text(encoding="utf-8")

    # E il messaggio rimasto in cronologia deve dire dov'e' finito il resto.
    riassunti = [m for m in msgs if m.get("role") == "summary"]
    assert elenco[0].percorso in riassunti[0]["content"]


def test_si_puo_spegnere_e_allora_non_scrive_niente(tmp_path):
    from core import agent as agent_mod
    from core.config import GenParams
    from core.tools import ToolContext
    from tests.test_compattazione import BackendDiTurno, cronologia

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
            libreria_attiva=False,
        )
    )
    assert be.riassunti == 1, "la compattazione deve avvenire lo stesso"
    assert not libreria.cartella(tmp_path).exists()
