"""Le capacita' prese in prestito da un harness piu' grande.

Cinque cose, tutte nate dalla stessa domanda: cosa ha un harness per modelli di
frontiera che serve anche qui. Il filtro applicato e' sempre lo stesso -- vale
solo se costa poco al contesto e non dipende dall'iniziativa del modello, che
su qwen3.8 e' misuratamente scarsa (``manage_notes``, raccomandato a chiare
lettere nel system prompt, chiamato zero volte in sei sessioni).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import delega as delega_mod  # noqa: E402
from core import skills as skills_mod  # noqa: E402
from core.agent import PUNTI_PER_IL_CANCELLO  # noqa: E402
from core.tools import TOOLS_SCHEMA, ToolContext, dispatch  # noqa: E402


@pytest.fixture()
def ws(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "a.py").write_text(
        "def budgets_for(x):\n    return x\n\nbudgets_for(1)\nbudgets_for(2)\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "b.py").write_text("from a import budgets_for\n", encoding="utf-8")
    (tmp_path / "tests" / "test_a.py").write_text("def test_x(): pass\n", encoding="utf-8")
    return tmp_path


@pytest.fixture()
def ctx(ws):
    return ToolContext(workspace=str(ws))


def chiama(ctx, nome, **args):
    return json.loads(dispatch(ctx, nome, args))


# ---------------------------------------------------------------------------
# Ricerche magre
# ---------------------------------------------------------------------------


def test_la_modalita_files_costa_una_frazione(ctx):
    """Alla domanda 'quali file nominano X' la risposta utile sono i percorsi.
    Nelle sessioni misurate i risultati dei tool sono il 44-51% del contesto:
    e' la leva piu' diretta su quel numero."""
    magra = chiama(ctx, "search_files", pattern="budgets_for", output_mode="files")
    grassa = chiama(ctx, "search_files", pattern="budgets_for", output_mode="content")
    assert magra["files"] == ["src/a.py", "src/b.py"]
    assert len(json.dumps(magra)) < len(json.dumps(grassa)) / 1.5


def test_la_modalita_files_non_finge_di_aver_contato(ctx):
    """Si smette al primo colpo per file: un totale ricavato cosi' sarebbe un
    numero sbagliato spacciato per una misura."""
    magra = chiama(ctx, "search_files", pattern="budgets_for", output_mode="files")
    assert "match_count" not in magra
    assert magra["file_count"] == 2


def test_la_modalita_count_dice_da_dove_cominciare(ctx):
    esito = chiama(ctx, "search_files", pattern="budgets_for", output_mode="count")
    assert esito["counts"] == ["src/a.py: 3", "src/b.py: 1"]
    assert esito["match_count"] == 4


def test_il_contesto_attorno_alla_corrispondenza(ctx):
    esito = chiama(
        ctx, "search_files", pattern="return x", output_mode="content", context_lines=1
    )
    blocco = esito["matches"][0]
    assert "def budgets_for" in blocco          # la riga sopra
    assert ">" in blocco                        # la corrispondenza e' segnata


def test_una_modalita_inventata_viene_respinta(ctx):
    assert chiama(ctx, "search_files", pattern="x", output_mode="tutto").get("error")


def test_la_ricerca_per_nome_non_cammina_l_albero(ctx):
    """'dove sono i test' senza esplorare a mano e senza indovinare."""
    esito = chiama(ctx, "list_files", pattern="test_*.py")
    assert esito["files"] == ["tests/test_a.py"]
    assert "tree" not in esito, "con un pattern si vogliono i percorsi, non l'albero"


def test_senza_pattern_list_files_resta_quello_di_prima(ctx):
    assert "tree" in chiama(ctx, "list_files")


# ---------------------------------------------------------------------------
# replace_all
# ---------------------------------------------------------------------------


def test_replace_all_fa_in_una_chiamata_quello_che_ne_costava_tre(ctx, ws):
    esito = chiama(
        ctx, "edit_file", filepath="src/a.py",
        old_string="budgets_for", new_string="budget_per", replace_all=True,
    )
    assert esito["replacements"] == 3
    assert "budgets_for" not in (ws / "src" / "a.py").read_text(encoding="utf-8")


def test_senza_replace_all_l_ambiguita_resta_un_errore(ctx):
    """E' la guardia giusta per una modifica chirurgica: due occorrenze vogliono
    dire che il frammento non identifica il punto."""
    esito = chiama(
        ctx, "edit_file", filepath="src/a.py",
        old_string="budgets_for", new_string="budget_per",
    )
    assert esito.get("error") and "replace_all" in esito.get("hint", "")


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------


def scrivi_skill(cartella: Path, nome: str, termini: str, corpo: str = "istruzioni") -> None:
    cartella.mkdir(parents=True, exist_ok=True)
    (cartella / f"{nome}.md").write_text(
        f"---\nnome: {nome}\ndescrizione: procedura {nome}\ntermini: {termini}\n---\n{corpo}\n",
        encoding="utf-8",
    )


def test_una_skill_entra_solo_quando_la_richiesta_la_nomina(tmp_path):
    scrivi_skill(tmp_path, "rilascio", "release, rilascio, tag")
    tutte = skills_mod.carica([(tmp_path, "harness")])
    assert [s.nome for s in skills_mod.scegli(tutte, "prepara il rilascio")] == ["rilascio"]
    assert skills_mod.scegli(tutte, "correggi un bug in app.js") == []


def test_i_termini_si_cercano_a_inizio_parola(tmp_path):
    """Cercando 'test' si prenderebbe 'contesto', e una skill caricata a
    sproposito costa il doppio: i suoi token piu' l'attenzione che ruba."""
    scrivi_skill(tmp_path, "prove", "test")
    tutte = skills_mod.carica([(tmp_path, "harness")])
    assert skills_mod.scegli(tutte, "il contesto si riempie") == []
    assert skills_mod.scegli(tutte, "lancia i test") != []


def test_una_skill_senza_intestazione_viene_ignorata(tmp_path):
    (tmp_path / "rotta.md").write_text("solo del testo", encoding="utf-8")
    assert skills_mod.carica([(tmp_path, "harness")]) == []


def test_il_progetto_vince_sull_harness(tmp_path):
    """Una convenzione locale che contraddice quella generale e' quasi sempre
    voluta."""
    generale, locale = tmp_path / "g", tmp_path / "l"
    scrivi_skill(generale, "rilascio", "rilascio", corpo="regola generale")
    scrivi_skill(locale, "rilascio", "rilascio", corpo="regola del progetto")
    tutte = skills_mod.carica([(generale, "harness"), (locale, "progetto")])
    assert len(tutte) == 1 and tutte[0].corpo == "regola del progetto"


def test_senza_skill_attive_il_blocco_e_vuoto(tmp_path):
    """Senza skill attive il blocco e' vuoto: sul modello piccolo l'elenco
    delle procedure dormienti finiva nel ragionamento invece che nelle mani."""
    scrivi_skill(tmp_path, "rilascio", "rilascio")
    # Le skill esistono sul disco: e' proprio il caso interessante, perche'
    # prima bastava che esistessero per farle comparire in coda al contesto.
    assert skills_mod.carica([(tmp_path, "harness")])
    assert skills_mod.render_blocco([]) == ""


def test_il_numero_di_skill_attive_ha_un_tetto(tmp_path):
    for i in range(5):
        scrivi_skill(tmp_path, f"s{i}", "comune", corpo="x" * 100)
    tutte = skills_mod.carica([(tmp_path, "harness")])
    assert len(skills_mod.scegli(tutte, "una cosa comune")) == skills_mod.MAX_SKILL_ATTIVE


# ---------------------------------------------------------------------------
# Delega
# ---------------------------------------------------------------------------


def test_l_esploratore_non_puo_scrivere_ne_eseguire():
    """Un sotto-turno che scrive e' un agente autonomo, e su un 27B sarebbe
    lavoro fatto in un contesto che nessuno ha rivisto."""
    nomi = {t["function"]["name"] for t in delega_mod.schema_ridotto(TOOLS_SCHEMA)}
    assert nomi == {"list_files", "read_file", "search_files"}
    assert "run_command" not in nomi and "write_file" not in nomi


def test_senza_gancio_la_delega_lo_dice_invece_di_rompersi(ctx):
    esito = chiama(ctx, "esplora", compito="dove sta la funzione budgets_for")
    assert esito.get("error") and "search_files" in esito.get("hint", "")


def test_un_compito_vago_viene_respinto(ctx):
    """L'esploratore non vede la conversazione: una frase di tre parole gli
    arriva senza niente attorno."""
    ctx.on_delega = lambda _: {"referto": "mai chiamato"}
    assert chiama(ctx, "esplora", compito="cerca").get("error")


def test_il_referto_dice_cosa_ha_guardato(ctx):
    """Un referto sicuro di se' prodotto senza aprire niente e' inventato, e
    questo campo e' l'unico modo di accorgersene."""
    ctx.on_delega = lambda _: {"referto": "sta in a.py", "file_letti": ["src/a.py"]}
    esito = chiama(ctx, "esplora", compito="dove sta budgets_for e chi la usa")
    assert esito["file_letti"] == ["src/a.py"]


def test_l_esploratore_non_delega_a_sua_volta():
    sorgente = (Path(__file__).resolve().parents[1] / "core" / "delega.py").read_text(
        encoding="utf-8"
    )
    assert "abilita_delega=False" in sorgente


# ---------------------------------------------------------------------------
# Cancello sul piano
# ---------------------------------------------------------------------------


def test_la_soglia_del_cancello_lascia_passare_i_compiti_corti():
    """Sotto, rifare il lavoro costa meno che interromperlo. Sopra, un piano
    storto si paga per venti passi."""
    assert 4 <= PUNTI_PER_IL_CANCELLO <= 8
