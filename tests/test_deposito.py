"""Il deposito dei risultati: quello che il troncamento buttava via.

Il difetto che lo motiva non e' il costo -- in contesto entrano esattamente gli
stessi token di prima -- ma una frase falsa. `smart_truncate` consiglia
*"Usa un comando piu' mirato (grep/head/tail) per vedere questa porzione"*: per
un file e' vero, per lo stdout di un comando quel testo non sta piu' da nessuna
parte. Ed e' il 32,7% dei token di risultato dei tool (misura del 23/08/2026).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import deposito  # noqa: E402
from core.config import Budgets  # noqa: E402
from core.textutils import smart_truncate  # noqa: E402
from core.tools import ToolContext, tool_run_command, tool_search_files  # noqa: E402


def _ctx(tmp_path, **kw):
    return ToolContext(workspace=str(tmp_path), sandbox="host", **kw)


# ---------------------------------------------------------------------------
# Il modulo
# ---------------------------------------------------------------------------


def test_deposita_torna_un_percorso_relativo_e_scrive_il_testo_intero(tmp_path):
    percorso = deposito.deposita(tmp_path, "riga\n" * 5000, etichetta="stdout pytest")
    assert percorso.startswith(".deposito/")
    testo = (tmp_path / percorso).read_text(encoding="utf-8")
    assert testo.count("riga") == 5000


def test_la_cartella_si_ignora_da_sola(tmp_path):
    """Come `.analisi/` e `.memoria/`: `*` in un .gitignore suo, senza toccare
    quello del progetto, che e' roba dell'utente."""
    deposito.deposita(tmp_path, "x" * 100, etichetta="prova")
    assert (deposito.cartella(tmp_path) / ".gitignore").read_text(encoding="utf-8").strip() == "*"


def test_i_numeri_non_si_riusano(tmp_path):
    a = deposito.deposita(tmp_path, "primo", etichetta="uguale")
    b = deposito.deposita(tmp_path, "secondo", etichetta="uguale")
    assert a != b
    assert (tmp_path / a).read_text(encoding="utf-8") == "primo"
    assert (tmp_path / b).read_text(encoding="utf-8") == "secondo"


def test_un_deposito_fallito_torna_none_e_non_solleva(tmp_path):
    """Il tool e' il lavoro vero: un disco pieno non deve farlo fallire."""
    (tmp_path / deposito.SCHEDARIO).write_text("sono un file, non una cartella", encoding="utf-8")
    assert deposito.deposita(tmp_path, "qualcosa", etichetta="x") is None


def test_niente_da_depositare_torna_none(tmp_path):
    assert deposito.deposita(tmp_path, "   \n ", etichetta="x") is None
    assert not deposito.cartella(tmp_path).exists()


def test_un_testo_smisurato_viene_tagliato_e_lo_dice(tmp_path):
    grosso = "z" * (deposito.MAX_TESTO_CHARS + 10_000)
    percorso = deposito.deposita(tmp_path, grosso, etichetta="enorme")
    testo = (tmp_path / percorso).read_text(encoding="utf-8")
    assert len(testo) < len(grosso)
    assert "tagliato qui" in testo, "un file che finisce a meta' senza avvisare e' un'altra frase falsa"


# ---------------------------------------------------------------------------
# La potatura
# ---------------------------------------------------------------------------


def test_sopra_il_tetto_escono_i_piu_vecchi(tmp_path):
    import os
    import time

    for i in range(5):
        percorso = deposito.deposita(tmp_path, "x" * 300_000, etichetta=f"file{i}")
        # mtime crescenti e distinti: la potatura ordina per eta'.
        os.utime(tmp_path / percorso, (time.time() + i, time.time() + i))
    assert len(list(deposito.cartella(tmp_path).glob("*.txt"))) == 5
    tolti = deposito.pota(tmp_path, max_mb=1)
    rimasti = sorted(p.name for p in deposito.cartella(tmp_path).glob("*.txt"))
    assert tolti > 0
    assert deposito.occupazione(tmp_path) <= 1024 * 1024
    assert rimasti[-1].endswith("file4.txt"), "il piu' recente sopravvive"


def test_sotto_il_tetto_non_si_tocca_niente(tmp_path):
    deposito.deposita(tmp_path, "piccolo", etichetta="a")
    assert deposito.pota(tmp_path, max_mb=64) == 0
    assert len(list(deposito.cartella(tmp_path).glob("*.txt"))) == 1


def test_tetto_zero_vuol_dire_nessun_tetto(tmp_path):
    deposito.deposita(tmp_path, "x" * 200_000, etichetta="a")
    assert deposito.pota(tmp_path, max_mb=0) == 0
    assert len(list(deposito.cartella(tmp_path).glob("*.txt"))) == 1


def test_potare_una_cartella_che_non_esiste_non_e_un_errore(tmp_path):
    assert deposito.pota(tmp_path, max_mb=1) == 0


# ---------------------------------------------------------------------------
# La frase dentro il marcatore di taglio
# ---------------------------------------------------------------------------


def test_senza_deposito_il_consiglio_resta_quello_di_sempre():
    testo = smart_truncate("a" * 5000, 500, label="stdout")
    assert "grep/head/tail" in testo


def test_col_deposito_il_consiglio_dice_dove_sta_il_testo():
    testo = smart_truncate(
        "a" * 5000, 500, label="stdout", consiglio="il testo intero e' in `.deposito/001-x.txt`"
    )
    assert ".deposito/001-x.txt" in testo
    assert "grep/head/tail" not in testo, "la frase falsa non deve restare accanto a quella vera"


# ---------------------------------------------------------------------------
# run_command
# ---------------------------------------------------------------------------

# Budget minuscoli: e' il taglio che si vuole provare, non la taglia reale.
STRETTI = Budgets(command_stdout_max_chars=400, command_stderr_max_chars=400)


def _echo_lungo(ctx, quante=2000):
    return json.loads(
        tool_run_command(
            ctx,
            f'{sys.executable} -c "print(\'riga\' * {quante})"',
        )
    )


def test_uno_stdout_tagliato_finisce_intero_su_disco(tmp_path):
    ctx = _ctx(tmp_path, budgets=STRETTI)
    out = _echo_lungo(ctx)
    assert "stdout_deposito" in out, "lo stdout superava il budget e doveva essere depositato"
    intero = (tmp_path / out["stdout_deposito"]).read_text(encoding="utf-8")
    assert intero.count("riga") >= 2000
    assert len(out["stdout"]) < len(intero)


def test_il_risultato_dice_dove_e_finito(tmp_path):
    ctx = _ctx(tmp_path, budgets=STRETTI)
    out = _echo_lungo(ctx)
    assert out["stdout_deposito"] in out["stdout"], "il percorso va dentro il marcatore di taglio"
    assert "grep/head/tail" not in out["stdout"]


def test_uno_stdout_corto_non_deposita_niente(tmp_path):
    """La sessione mediana non ha un problema di contesto (picco 8.059 token):
    non deve pagare una scrittura su disco per turno in cambio di niente."""
    ctx = _ctx(tmp_path, budgets=STRETTI)
    out = json.loads(tool_run_command(ctx, f'{sys.executable} -c "print(\'corto\')"'))
    assert "stdout_deposito" not in out
    assert not deposito.cartella(tmp_path).exists()


def test_si_puo_spegnere(tmp_path):
    ctx = _ctx(tmp_path, budgets=STRETTI, deposito_attivo=False)
    out = _echo_lungo(ctx)
    assert "stdout_deposito" not in out
    assert not deposito.cartella(tmp_path).exists()
    assert "grep/head/tail" in out["stdout"], "spento, il comportamento e' quello di sempre"


# ---------------------------------------------------------------------------
# search_files
# ---------------------------------------------------------------------------


def _progetto(tmp_path, file=40, righe=30):
    for i in range(file):
        (tmp_path / f"f{i:03d}.py").write_text(
            "\n".join(f"ago {i} {n}" for n in range(righe)), encoding="utf-8"
        )


def test_le_corrispondenze_oltre_il_tetto_finiscono_su_disco(tmp_path):
    _progetto(tmp_path)
    ctx = _ctx(tmp_path, budgets=Budgets(search_max_matches=10))
    out = json.loads(tool_search_files(ctx, "ago"))
    assert out["truncated"] is True
    assert len(out["matches"]) == 10, "in contesto entrano solo le prime, come prima"
    intero = (tmp_path / out["deposito"]).read_text(encoding="utf-8")
    assert intero.count("ago") > 1000, "ma sul disco ci sono tutte"


def test_spento_la_ricerca_si_ferma_al_tetto_come_prima(tmp_path):
    """Continuare il walk senza depositare sarebbe I/O pagato per buttarlo."""
    _progetto(tmp_path)
    ctx = _ctx(tmp_path, budgets=Budgets(search_max_matches=10), deposito_attivo=False)
    out = json.loads(tool_search_files(ctx, "ago"))
    assert out["truncated"] is True
    assert "deposito" not in out
    assert not deposito.cartella(tmp_path).exists()


def test_una_ricerca_che_non_trabocca_non_deposita(tmp_path):
    _progetto(tmp_path, file=1, righe=3)
    ctx = _ctx(tmp_path, budgets=Budgets(search_max_matches=100))
    out = json.loads(tool_search_files(ctx, "ago"))
    assert out["truncated"] is False
    assert "deposito" not in out


def test_anche_le_modalita_compatte_depositano(tmp_path):
    _progetto(tmp_path, file=40, righe=2)
    ctx = _ctx(tmp_path, budgets=Budgets(search_max_matches=5))
    out = json.loads(tool_search_files(ctx, "ago", output_mode="files"))
    assert out["truncated"] is True
    assert len(out["files"]) == 5
    intero = (tmp_path / out["deposito"]).read_text(encoding="utf-8")
    assert intero.count(".py") >= 40


# ---------------------------------------------------------------------------
# I tool esistenti ci arrivano davvero
# ---------------------------------------------------------------------------


def test_il_deposito_e_leggibile_e_cercabile_senza_tool_nuovi(tmp_path):
    """Il filtro sulle cartelle nascoste agisce sui sottolivelli del walk: una
    cartella nascosta passata come **radice** viene percorsa lo stesso. Per
    questo il deposito e' piatto -- e' cio' che lo rende raggiungibile senza
    toccare una riga di tools.py."""
    from core.tools import tool_read_file

    percorso = deposito.deposita(tmp_path, "riga con l'ago\n" * 50, etichetta="prova")
    ctx = _ctx(tmp_path)

    letto = json.loads(tool_read_file(ctx, percorso))
    assert "ago" in letto["content"]

    trovato = json.loads(tool_search_files(ctx, "ago", subfolder=deposito.SCHEDARIO))
    assert trovato["match_count"] == 50


def test_list_files_non_lo_mostra(tmp_path):
    """Voluto: il deposito non deve sporcare l'albero del progetto che il
    modello legge a ogni turno nell'environment header."""
    from core.tools import tool_list_files

    deposito.deposita(tmp_path, "x" * 100, etichetta="prova")
    (tmp_path / "vero.py").write_text("pass", encoding="utf-8")
    elenco = json.loads(tool_list_files(ctx=_ctx(tmp_path), subfolder="."))
    assert deposito.SCHEDARIO not in json.dumps(elenco)
