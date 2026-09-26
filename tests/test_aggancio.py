"""Aggancio tollerante di edit_file (A6 del referto del 25/09).

Guasto F3: il 10,7% delle ``edit_file`` falliva, quasi sempre per un
``old_string`` ricordato a meno di spazi o di una parola. L'aggancio accetta
solo differenze di spazi, solo su un tratto unico; tutto il resto diventa una
diagnosi con il tratto piu' vicino.
"""

from __future__ import annotations

import json

from core import aggancio
from core.tools import ToolContext, tool_edit_file

CODICE = (
    "class Statistiche:\n"
    "    def media(self):\n"
    "        totale = sum(self.valori)\n"
    "        return totale / (len(self.valori) + 1)\n"
    "\n"
    "    def massimo(self):\n"
    "        return max(self.valori)\n"
)
BUG = "        return totale / (len(self.valori) + 1)"
OK = "        return totale / len(self.valori)"


def _edit(tmp_path, old, new, testo=CODICE, nome="stats.py", **kw):
    (tmp_path / nome).write_bytes(testo.encode("utf-8"))
    ctx = ToolContext(workspace=str(tmp_path), sandbox="host")
    esito = json.loads(tool_edit_file(ctx, nome, old, new, **kw))
    return esito, (tmp_path / nome).read_bytes().decode("utf-8")


def test_esatto_resta_com_era(tmp_path):
    esito, dopo = _edit(tmp_path, BUG, OK)
    assert esito["status"] == "ok" and "aggancio" not in esito
    assert OK in dopo and BUG not in dopo


def test_spazi_finali_agganciano_e_lo_dicono(tmp_path):
    esito, dopo = _edit(tmp_path, BUG + "   ", OK)
    assert esito["status"] == "ok"
    assert esito["aggancio"] == "spazi_finali"
    assert dopo == CODICE.replace(BUG, OK)


def test_rientro_sbagliato_solo_nel_vecchio(tmp_path):
    """old_string con 4 spazi in meno, new_string gia' giusto: si applica com'e'."""
    esito, dopo = _edit(tmp_path, BUG[4:], OK)
    assert esito["aggancio"] == "rientro"
    assert dopo == CODICE.replace(BUG, OK)


def test_rientro_sbagliato_in_tutti_e_due(tmp_path):
    """Il caso di Aider: i due blocchi sbagliati insieme. new_string si sposta."""
    vecchio = "totale = sum(self.valori)\nreturn totale / (len(self.valori) + 1)"
    nuovo = "totale = sum(self.valori)\nreturn totale / len(self.valori)"
    esito, dopo = _edit(tmp_path, vecchio, nuovo)
    assert esito["aggancio"] == "rientro"
    assert dopo == CODICE.replace(BUG, OK)


def test_rientri_incoerenti_non_si_indovinano(tmp_path):
    esito, dopo = _edit(tmp_path, BUG[4:], "  " + OK.strip())
    assert "error" in esito
    assert dopo == CODICE


def test_una_parola_diversa_non_aggancia_ma_mostra_il_piu_vicino(tmp_path):
    esito, dopo = _edit(tmp_path, BUG.replace(" + 1)", "+1)"), OK)
    assert "error" in esito and dopo == CODICE
    vicino = esito["piu_vicino"]
    assert vicino["righe"] == "4-4"
    assert vicino["somiglianza"] >= 0.85
    assert "4 | " in vicino["testo"]
    assert any(d.startswith("- ") for d in vicino["differenze"])
    assert "piu_vicino" in esito["hint"]


def test_niente_di_simile_niente_piu_vicino(tmp_path):
    esito, _ = _edit(tmp_path, "import numpy as np\nnp.zeros(3)", "x")
    assert "error" in esito and "piu_vicino" not in esito
    assert "read_file" in esito["hint"]


def test_tollerante_ambiguo_non_modifica_e_dice_le_righe(tmp_path):
    testo = "def a():\n    return 1\n\ndef b():\n    return 1\n"
    esito, dopo = _edit(tmp_path, "return 1  ", "return 2", testo=testo)
    assert "error" in esito and dopo == testo
    assert esito["righe"] == [2, 5]


def test_occorrenze_esatte_multiple_dicono_le_righe(tmp_path):
    testo = "x = 1\ny = 2\nx = 1\n"
    esito, _ = _edit(tmp_path, "x = 1", "x = 3", testo=testo)
    assert "error" in esito and esito["righe"] == [1, 3]


def test_file_crlf_resta_crlf(tmp_path):
    """La lettura normalizza gli a capo per il confronto; la scrittura li
    rimetteva LF su tutto il file. Su Windows: un diff di ogni riga per una
    modifica di una."""
    crlf = CODICE.replace("\n", "\r\n")
    vecchio = "        totale = sum(self.valori)\n" + BUG
    nuovo = "        totale = sum(self.valori)\n" + OK
    esito, dopo = _edit(tmp_path, vecchio, nuovo, testo=crlf)
    assert esito["status"] == "ok" and "aggancio" not in esito
    assert dopo == crlf.replace(BUG, OK)
    assert "\n" not in dopo.replace("\r\n", "")


def test_file_misto_resta_come_lo_scrive_la_normalizzazione(tmp_path):
    misto = "a = 1\r\nb = 2\nc = 3\n"
    esito, dopo = _edit(tmp_path, "b = 2", "b = 5", testo=misto)
    assert esito["status"] == "ok"
    assert dopo == "a = 1\nb = 5\nc = 3\n"


def test_old_string_con_crlf_su_file_lf(tmp_path):
    vecchio = "        totale = sum(self.valori)\r\n" + BUG
    nuovo = "        totale = sum(self.valori)\r\n" + OK
    esito, dopo = _edit(tmp_path, vecchio, nuovo)
    assert esito["status"] == "ok" and esito["aggancio"] == "a_capo"
    assert dopo == CODICE.replace(BUG, OK)


def test_old_string_con_a_capo_finale_consuma_l_a_capo(tmp_path):
    esito, dopo = _edit(tmp_path, BUG + "  \n", OK + "\n")
    assert esito["status"] == "ok"
    assert dopo == CODICE.replace(BUG, OK)


def test_tab_mescolati_non_agganciano_sul_rientro(tmp_path):
    testo = "def f():\n\treturn 1\n"
    esito, dopo = _edit(tmp_path, "    return 1", "    return 2", testo=testo)
    assert "error" in esito and dopo == testo


def test_la_guardia_di_sintassi_vale_anche_per_l_aggancio(tmp_path):
    """Lo sposta-rientro non deve diventare un modo per aggirare la guardia."""
    vecchio = "return totale / (len(self.valori) + 1)"
    nuovo = "return totale / (len(self.valori)"          # parentesi aperta
    esito, dopo = _edit(tmp_path, vecchio, nuovo)
    assert "error" in esito or esito.get("avviso_sintassi")
    if "error" in esito:
        assert dopo == CODICE


def test_cerca_preferisce_il_grado_meno_tollerante():
    testo = "a = 1  \nb = 2\n"
    trovati, modo = aggancio.cerca(testo, "a = 1", "a = 3")
    assert modo == "spazi_finali" and len(trovati) == 1


def test_righe_occorrenze():
    assert aggancio.righe_occorrenze("x\ny\nx\n", "x") == [1, 3]
    assert aggancio.righe_occorrenze("abc", "z") == []
