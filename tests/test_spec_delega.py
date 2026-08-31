"""Le spec riutilizzabili dell'esploratore: fatti append-only, derivati rigenerabili.

Il difetto misurato il 23/08/2026: `esplora` viene usato (17 esplorazioni in 4
sessioni) ma falliva -- 11 referti utili contro 6 fallimenti, e **8 su 17**
toccavano il tetto dei sei passi. `referto_di_chiusura` ha tolto il fallimento
peggiore; qui si attacca la causa a monte, cioe' che il figlio non sa quanto
tempo gli resta -- lo stesso difetto che il blocco del piano aveva gia' risolto
per il padre.

La regola che decide cosa e' lecito conservare: **il diritto di essere
raffinato spetta solo a cio' che e' ricostruibile dai fatti**. I fatti non si
riscrivono; le frasi si rifanno da capo ogni volta.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import libreria, spec_delega
from core.prompts import DELEGA_ESEMPIO, DELEGA_NUDGE


def _riempi(tmp_path, quanti, **kw):
    for i in range(quanti):
        spec_delega.registra(tmp_path, domanda=f"dove sta la cosa {i}", passi=3, **kw)


# ---------------------------------------------------------------------------
# I fatti
# ---------------------------------------------------------------------------


def test_un_esito_si_scrive_e_si_rilegge(tmp_path):
    spec_delega.registra(
        tmp_path, domanda="dove viene costruito il blocco del piano", passi=2, riuscito=True
    )
    fatti = spec_delega.leggi(tmp_path)
    assert len(fatti) == 1
    assert fatti[0]["domanda"] == "dove viene costruito il blocco del piano"
    assert fatti[0]["riuscito"] is True


def test_i_fatti_si_accodano_e_non_si_riscrivono(tmp_path):
    spec_delega.registra(tmp_path, domanda="prima", passi=1, riuscito=True)
    spec_delega.registra(tmp_path, domanda="seconda", passi=1, riuscito=False)
    assert [f["domanda"] for f in spec_delega.leggi(tmp_path)] == ["prima", "seconda"]


def test_oltre_la_finestra_escono_i_piu_vecchi(tmp_path):
    """Un tasso calcolato su tutta la storia del workspace racconterebbe
    com'era sei mesi fa."""
    _riempi(tmp_path, spec_delega.MAX_FATTI + 10, riuscito=True)
    fatti = spec_delega.leggi(tmp_path)
    assert len(fatti) == spec_delega.MAX_FATTI
    assert fatti[-1]["domanda"].endswith(str(spec_delega.MAX_FATTI + 9))


def test_i_fatti_stanno_dove_la_libreria_non_li_vede(tmp_path):
    """Una cartella nascosta sola invece di due: il file comincia per punto e
    `libreria.voci()` raccoglie i *.md, quindi non lo incrocia mai."""
    spec_delega.registra(tmp_path, domanda="x", passi=1, riuscito=True)
    libreria.archivia(tmp_path, riassunto="FATTO: qualcosa", richieste=["fai"])
    assert len(libreria.voci(tmp_path)) == 1
    assert (tmp_path / ".memoria" / spec_delega.NOME).exists()


def test_un_file_illeggibile_vale_come_nessun_fatto(tmp_path):
    percorso = tmp_path / ".memoria" / spec_delega.NOME
    percorso.parent.mkdir(parents=True)
    percorso.write_text("{non json", encoding="utf-8")
    assert spec_delega.leggi(tmp_path) == []


def test_una_domanda_vuota_non_si_registra(tmp_path):
    spec_delega.registra(tmp_path, domanda="   ", passi=1, riuscito=True)
    assert spec_delega.leggi(tmp_path) == []


# ---------------------------------------------------------------------------
# I derivati: si ricalcolano, non si conservano
# ---------------------------------------------------------------------------


def test_il_blocco_dice_al_figlio_quanto_tempo_gli_resta(tmp_path):
    blocco = spec_delega.blocco_figlio(None, passi_rimasti=4, totale=6)
    assert "4" in blocco and "6" in blocco


def test_all_ultimo_passo_utile_gli_si_chiede_il_referto(tmp_path):
    blocco = spec_delega.blocco_figlio(None, passi_rimasti=1, totale=6)
    assert "referto" in blocco.lower()
    presto = spec_delega.blocco_figlio(None, passi_rimasti=5, totale=6)
    assert "referto" not in presto.lower(), "al passo 1 sarebbe rumore"


def test_l_avviso_compare_solo_se_i_fatti_lo_reggono(tmp_path):
    _riempi(tmp_path, 6, riuscito=False, esaurito=True)
    blocco = spec_delega.blocco_figlio(tmp_path, passi_rimasti=4, totale=6)
    assert "esplorazioni hanno finito i passi" in blocco


def test_e_sparisce_da_solo_quando_l_evidenza_cambia(tmp_path):
    """E' questo che rende *evidence-backed* una parola verificabile invece
    che una promessa: il derivato non e' conservato, e' ricalcolato."""
    _riempi(tmp_path, 6, riuscito=False, esaurito=True)
    assert "finito i passi" in spec_delega.blocco_figlio(tmp_path, passi_rimasti=4, totale=6)
    _riempi(tmp_path, 20, riuscito=True)
    assert "finito i passi" not in spec_delega.blocco_figlio(tmp_path, passi_rimasti=4, totale=6)


def test_con_pochi_fatti_non_si_dice_niente(tmp_path):
    """Sotto la soglia il tasso e' rumore, e un avviso su rumore e' una frase
    che il modello impara a saltare."""
    _riempi(tmp_path, 2, riuscito=False, esaurito=True)
    assert spec_delega.tasso_esaurimento(tmp_path) == (0, 0)
    assert "finito i passi" not in spec_delega.blocco_figlio(tmp_path, passi_rimasti=4, totale=6)


def test_l_esempio_e_l_ultima_delega_riuscita_dentro_il_budget(tmp_path):
    spec_delega.registra(tmp_path, domanda="vecchia buona", passi=2, riuscito=True)
    spec_delega.registra(tmp_path, domanda="fallita", passi=6, riuscito=False, esaurito=True)
    spec_delega.registra(
        tmp_path, domanda="recuperata a forza", passi=6, riuscito=True, chiuso_a_forza=True
    )
    assert spec_delega.esempio_riuscito(tmp_path) == "vecchia buona"


def test_senza_esempi_riusciti_non_si_inventa_niente(tmp_path):
    _riempi(tmp_path, 5, riuscito=False, esaurito=True)
    assert spec_delega.esempio_riuscito(tmp_path) == ""


def test_l_esempio_si_attacca_al_sollecito(tmp_path):
    """Nel punto in cui il modello sta gia' leggendo, non in coda al contesto:
    li' sarebbe pagato a ogni passo per servire di rado."""
    testo = DELEGA_NUDGE.format(quante=5) + DELEGA_ESEMPIO.format(domanda="dove sta budgets_for")
    assert "dove sta budgets_for" in testo
    assert "esplora" in testo


# ---------------------------------------------------------------------------
# L'aggancio alla delega
# ---------------------------------------------------------------------------


def test_una_delega_riuscita_lascia_il_suo_fatto(tmp_path):
    from tests.test_delega import _esegui

    _esegui(tmp_path, referto="sta in core/config.py:105", passi=2)
    fatti = spec_delega.leggi(tmp_path)
    assert len(fatti) == 1
    assert fatti[0]["riuscito"] is True
    assert fatti[0]["passi"] == 2
    assert fatti[0]["esaurito"] is False


def test_una_delega_esaurita_lascia_il_suo(tmp_path):
    from core.delega import MAX_PASSI_DELEGA
    from tests.test_delega import _esegui

    _esegui(tmp_path, referto=None, passi=MAX_PASSI_DELEGA, legge=())
    fatti = spec_delega.leggi(tmp_path)
    assert len(fatti) == 1
    assert fatti[0]["riuscito"] is False
    assert fatti[0]["esaurito"] is True


def test_il_figlio_riceve_il_blocco_a_ogni_passo(tmp_path):
    """La leva e' informazione in contesto, non una riga di prompt: deve
    arrivargli aggiornata ogni volta, come il piano al padre."""
    from core.delega import MAX_PASSI_DELEGA
    from tests.test_delega import _esegui

    visti = []

    def _run(**kw):
        coda = kw.get("blocco_coda")
        for restanti in (MAX_PASSI_DELEGA - 1, 0):
            visti.append(coda(restanti) if coda else "")
        kw["ui_messages"].append({"role": "assistant", "content": "trovato in x.py:1"})
        return iter(())

    _esegui(tmp_path, _run=_run)
    assert f"{MAX_PASSI_DELEGA - 1} su {MAX_PASSI_DELEGA}" in visti[0]
    assert "referto" in visti[1].lower(), "all'ultimo passo utile glielo si chiede"


def test_si_puo_spegnere(tmp_path):
    from tests.test_delega import _esegui

    _esegui(tmp_path, referto="sta in core/config.py:105", passi=2, _registra=False)
    assert spec_delega.leggi(tmp_path) == []
