"""Progetti: identita' della cartella, memoria, chat che ci vivono, wiki.

Un progetto e' un posto di lavoro -- nome, descrizione, cartella, le sue
conversazioni e una memoria che vale per tutte -- e la wiki LLM e' una sua
proprieta' secondaria. Fino al 26/09/2026 si chiamava vault.

I contratti:
- ``core/progetto``: identita' in ``.progetto.json`` (con la conversione dal
  ``.vault.json`` di prima), la memoria a voci con tipo, fonte e autore, e la
  struttura LLM Wiki quando e' accesa;
- ``core/wiki_search``: la sotto-chiamata in sola lettura al manutentore di
  un'altra wiki, con ``run_turn`` iniettabile come per la delega;
- le rotte ``/api/progetti*`` e quelle nuove delle conversazioni (rinomina,
  archivia, sposta).

La scrittura automatica della memoria a fine turno sta in
``tests/test_memoria_progetto.py``; l'interfaccia in ``tests/test_progetti_web.py``.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

import tests.test_agent_loop as _fake_loop

fake_ollama = _fake_loop.fake_ollama

from core import progetto as progetto_mod
from core import wiki_search
from core.agent import AssistantTurn, StepStarted, ToolFinished
from core.tools import NOTES_TOOL, TOOLS_SCHEMA, ToolContext, dispatch


# ---------------------------------------------------------------------------
# La struttura della wiki (secondaria, ma intatta)
# ---------------------------------------------------------------------------


def test_scaffold_crea_la_struttura_llm_wiki(tmp_path):
    base = tmp_path / "kv"
    creati = progetto_mod.scaffold(base)
    assert set(creati) == {"raw", "raw/assets", "wiki", "CLAUDE.md", "wiki/index.md", "wiki/log.md"}
    assert progetto_mod.ha_struttura_wiki(base)
    # Idempotente: la seconda chiamata non crea nulla di nuovo.
    assert progetto_mod.scaffold(base) == []


def test_cartella_vuota_non_ha_la_wiki(tmp_path):
    nuda = tmp_path / "nuda"
    nuda.mkdir()
    assert not progetto_mod.ha_struttura_wiki(nuda)
    assert not progetto_mod.is_registrato(nuda)


def test_info_progetto_conta_fonti_e_pagine(tmp_path):
    base = tmp_path / "kv"
    progetto_mod.scaffold(base)
    (base / "raw" / "articolo.md").write_text("fonte", encoding="utf-8")
    (base / "raw" / "assets" / "img.png").write_bytes(b"x")  # corredo, non fonte
    (base / "wiki" / "Memex.md").write_text("# Memex", encoding="utf-8")
    d = progetto_mod.info_progetto(str(base), "prova").as_dict()
    assert d["nome"] == "prova"
    assert d["fonti"] == 1
    assert d["pagine"] >= 3  # index.md + log.md + Memex.md
    assert d["esiste"] is True


def test_le_fonti_non_sono_solo_i_markdown(tmp_path):
    """In ``raw/`` stanno paper e PDF: contando i soli .md si vedeva "0 fonti"."""
    base = tmp_path / "kv"
    progetto_mod.scaffold(base)
    (base / "raw" / "bush-1945.pdf").write_bytes(b"%PDF")
    (base / "raw" / "intervista.txt").write_text("trascrizione", encoding="utf-8")
    (base / "raw" / "assets" / "figura.png").write_bytes(b"x")
    assert progetto_mod.info_progetto(str(base)).fonti == 2


def test_log_append_only_con_prefisso_parseabile(tmp_path):
    base = tmp_path / "kv"
    progetto_mod.scaffold(base)
    progetto_mod.appendi_log(base, "ingest", "paper-memex.pdf")
    progetto_mod.appendi_log(base, "query", "confronto memex/wikipedia")
    righe = [r for r in progetto_mod.leggi_log_coda(base).splitlines() if r.startswith("## [")]
    assert len(righe) == 2
    assert "| paper-memex.pdf" in righe[0]
    assert all(r.split("] ")[1].startswith(("ingest", "query")) for r in righe)


def test_il_log_non_fa_fallire_un_ingest_riuscito(tmp_path):
    nudo = tmp_path / "senza_wiki"
    nudo.mkdir()
    assert progetto_mod.appendi_log(nudo, "ingest", "x.pdf") is False   # non solleva
    base = tmp_path / "kv"
    progetto_mod.scaffold(base)
    assert progetto_mod.appendi_log(base, "ingest", "bush-1945.pdf") is True


def test_leggi_indice_tronca_con_marcatore(tmp_path):
    base = tmp_path / "kv"
    progetto_mod.scaffold(base)
    indice = progetto_mod.leggi_indice(base, max_chars=50)
    assert len(indice) <= 50 + len("\n[indice troncato]") + 2
    assert "[indice troncato]" in indice


def test_un_indice_su_una_riga_sola_non_sparisce(tmp_path):
    base = tmp_path / "kv"
    progetto_mod.scaffold(base)
    (base / "wiki" / "index.md").write_text("A" * 200, encoding="utf-8")
    testo = progetto_mod.leggi_indice(base, max_chars=50)
    assert "A" * 50 in testo and "[indice troncato]" in testo


def test_struttura_ferma_e_stato_in_coda_sono_separati(tmp_path):
    base = tmp_path / "kv"
    progetto_mod.scaffold(base)
    assert progetto_mod.SCHEMA_FILE in progetto_mod.blocco_struttura(base)
    assert "# Indice attuale della wiki" in progetto_mod.blocco_stato(base)
    assert "# Indice attuale della wiki" not in progetto_mod.blocco_struttura(base)
    assert "<stato_della_wiki>" in progetto_mod.blocco_stato(base)


def test_is_modalita_wiki_accetta_str_e_path(tmp_path):
    base = tmp_path / "kv"
    progetto_mod.scaffold(base)
    altro = tmp_path / "altro"
    altro.mkdir()
    assert progetto_mod.is_modalita_wiki(base)
    assert progetto_mod.is_modalita_wiki(str(base))
    assert not progetto_mod.is_modalita_wiki(altro)
    assert not progetto_mod.is_modalita_wiki("")


def test_wiki_esplicitamente_nullo_ricade_sulla_struttura(tmp_path):
    """``get(chiave, ripiego)`` non scatta su un null: la chiave c'e'."""
    base = tmp_path / "kv"
    progetto_mod.scaffold(base)
    (base / progetto_mod.PROGETTO_FILE).write_text(
        json.dumps({"nome": "kv", "wiki": None}), encoding="utf-8"
    )
    assert progetto_mod.leggi_config(base).wiki is True


# ---------------------------------------------------------------------------
# wiki_search: schema ridotto e sotto-chiamata
# ---------------------------------------------------------------------------


@dataclass
class _Params:
    think: bool | str = True


def _wiki_di_prova(tmp_path: Path) -> Path:
    base = tmp_path / "kv"
    base.mkdir()
    (base / "raw").mkdir()
    (base / "wiki").mkdir()
    (base / "wiki" / "index.md").write_text(
        "# Indice\n\n## Concetti\n\n- [[Memex]] -- wiki/Memex.md\n", encoding="utf-8"
    )
    (base / "wiki" / "Memex.md").write_text(
        "# Memex\n\nIl Memex e' la macchina di Bush (1945).\n", encoding="utf-8"
    )
    return base


def _run_turn_che_risponde(risposta: str, spioni: dict):
    def _run(**kw):
        spioni["workspace"] = kw["tool_ctx"].workspace
        spioni["system_prompt"] = kw["system_prompt"]
        spioni["params"] = kw["params"]
        spioni["nomi_tool"] = [t["function"]["name"] for t in kw["tools_schema"]]
        spioni["ctx"] = kw["tool_ctx"]
        spioni["messaggi"] = kw["ui_messages"]
        yield StepStarted(step=1, total=1)
        yield ToolFinished("c1", "read_file", {}, "ok", 0.0, True)
        yield AssistantTurn(risposta, "", False)

    return _run


def test_schema_ridotto_espone_solo_lettura():
    nomi = [t["function"]["name"] for t in wiki_search.schema_ridotto(TOOLS_SCHEMA)]
    assert nomi == ["list_files", "read_file", "search_files"]


def test_cerca_nella_wiki_gira_sul_progetto_e_cita_le_fonti(tmp_path):
    base = _wiki_di_prova(tmp_path)
    spioni: dict = {}
    esito = wiki_search.cerca_nella_wiki(
        "kv",
        "che cosa e' il memex?",
        backend=object(),
        params=_Params(),
        tools_schema=TOOLS_SCHEMA,
        tool_ctx=ToolContext(workspace=str(tmp_path / "fuori")),
        env_header=None,
        run_turn=_run_turn_che_risponde(
            "Il Memex e' la macchina di Bush (1945). Vedi [[Memex]] in wiki/Memex.md.", spioni,
        ),
        registri=[{"nome": "kv", "path": str(base)}],
    )
    assert "errore" not in esito
    assert esito["progetto"] == "kv" and esito["passi"] == 1
    assert "Bush" in esito["referto"]
    assert spioni["workspace"] == str(base)
    # L'indice sta nel compito, non nel prompt: cambia a ogni ingest.
    assert "cercatore di una wiki personale" in spioni["system_prompt"]
    assert "Indice attuale della wiki" not in spioni["system_prompt"]
    compito = spioni["messaggi"][0]["content"]
    assert "stato_della_wiki" in compito and "Memex" in compito
    assert spioni["params"].think is False
    assert spioni["nomi_tool"] == ["list_files", "read_file", "search_files"]
    assert spioni["ctx"].tool_consentiti == frozenset(wiki_search.TOOL_CERCA)
    assert not spioni["ctx"].puo_usare("write_file")
    assert spioni["ctx"].on_delega is None and spioni["ctx"].on_wiki_search is None
    # Il cercatore non porta con se' la memoria del progetto che lo chiama.
    assert spioni["ctx"].progetto_dir == "" and spioni["ctx"].progetto_memoria == ()


def test_un_progetto_senza_wiki_non_si_consulta(tmp_path):
    """Da quando un progetto puo' essere codice, registrato non vuol dire wiki."""
    codice = tmp_path / "codice"
    codice.mkdir()
    progetto_mod.ensure_progetto(codice, nome="codice")
    esito = wiki_search.cerca_nella_wiki(
        "codice", "dove sta il parser?", backend=object(), params=_Params(),
        tools_schema=[], tool_ctx=None, env_header=None,
        run_turn=lambda **_kw: iter([]), registri=[{"nome": "codice", "path": str(codice)}],
    )
    assert "errore" in esito and "non ha una wiki" in esito["errore"]


def test_nome_sconosciuto_e_query_vuota_errori_puliti(tmp_path):
    base = _wiki_di_prova(tmp_path)
    comuni = {"backend": object(), "params": _Params(), "tools_schema": [], "tool_ctx": None,
              "env_header": None, "run_turn": lambda **_kw: iter([]),
              "registri": [{"nome": "kv", "path": str(base)}]}
    fantasma = wiki_search.cerca_nella_wiki("fantasma", "x", **comuni)
    assert "fantasma" in fantasma["errore"]
    assert "errore" in wiki_search.cerca_nella_wiki("kv", "   ", **comuni)


def test_referto_compatto_json_valido():
    testo = wiki_search.referto_compatto(
        {"progetto": "kv", "query": "q", "passi": 2, "referto": "risposta"}
    )
    assert '"progetto": "kv"' in testo and '"passi": 2' in testo


def test_tool_wiki_search_registrato_negli_schemi():
    from core.tools import _ALLOWED_ARGS, TOOL_IMPLS, WIKI_SEARCH_TOOL

    assert WIKI_SEARCH_TOOL == "wiki_search"
    assert WIKI_SEARCH_TOOL in TOOL_IMPLS
    assert _ALLOWED_ARGS[WIKI_SEARCH_TOOL] == {"progetto", "query"}
    assert WIKI_SEARCH_TOOL in [t["function"]["name"] for t in TOOLS_SCHEMA]


# ---------------------------------------------------------------------------
# L'identita' vive nella cartella
# ---------------------------------------------------------------------------


def test_una_cartella_qualsiasi_diventa_progetto_senza_diventare_una_wiki(tmp_path):
    base = tmp_path / "progetto"
    base.mkdir()
    progetto_mod.ensure_progetto(base, nome="Progetto", istruzioni="In italiano.")
    assert progetto_mod.is_registrato(base)
    config = progetto_mod.leggi_config(base)
    assert (config.nome, config.istruzioni, config.wiki) == ("Progetto", "In italiano.", False)
    assert config.creato, "la data di creazione si scrive alla nascita"
    assert not (base / "wiki").exists() and not (base / "raw").exists()


def test_ensure_non_scavalca_un_progetto_che_c_e_gia(tmp_path):
    base = tmp_path / "p"
    base.mkdir()
    progetto_mod.ensure_progetto(base, nome="Primo")
    progetto_mod.ensure_progetto(base, nome="Secondo", istruzioni="x")
    config = progetto_mod.leggi_config(base)
    assert config.nome == "Primo" and config.istruzioni == ""


def test_il_progetto_si_porta_dietro_il_nome_se_lo_sposti(tmp_path):
    prima = tmp_path / "prima"
    prima.mkdir()
    progetto_mod.ensure_progetto(prima, nome="Ricerca")
    progetto_mod.aggiorna_config(prima, descrizione="Paper e appunti")
    dopo = tmp_path / "spostato"
    prima.rename(dopo)
    config = progetto_mod.leggi_config(dopo)
    assert config.nome == "Ricerca" and config.descrizione == "Paper e appunti"


def test_una_wiki_vecchia_senza_file_continua_a_funzionare(tmp_path):
    base = tmp_path / "kv"
    progetto_mod.scaffold(base)
    assert not progetto_mod.is_registrato(base)
    config = progetto_mod.leggi_config(base)
    assert config.nome == "kv" and config.wiki is True


def test_i_due_testi_non_sono_lo_stesso_campo(tmp_path):
    base = tmp_path / "v"
    base.mkdir()
    progetto_mod.ensure_progetto(base)
    config = progetto_mod.aggiorna_config(
        base,
        descrizione="Roba di lavoro, la cartella grossa",
        istruzioni="Scrivi sempre in italiano e cita i percorsi.",
    )
    blocco = progetto_mod.blocco_istruzioni(config)
    assert "cita i percorsi" in blocco and "la cartella grossa" not in blocco
    assert "<progetto nome=" in blocco
    assert progetto_mod.blocco_istruzioni(progetto_mod.ProgettoConfig(nome="x")) == ""


def test_aggiorna_config_non_tocca_la_memoria(tmp_path):
    base = tmp_path / "v"
    base.mkdir()
    progetto_mod.ensure_progetto(base, nome="prima")
    progetto_mod.aggiungi_voce(base, "un fatto che resta")
    progetto_mod.aggiorna_config(base, nome="dopo", memoria=())   # la chiave si ignora
    config = progetto_mod.leggi_config(base)
    assert config.nome == "dopo" and [v.testo for v in config.memoria] == ["un fatto che resta"]


def test_il_nome_non_puo_chiudere_il_suo_attributo():
    config = progetto_mod.ProgettoConfig(
        nome='casa "mia" <script>',
        memoria=(progetto_mod.VoceMemoria(id="a1", testo="una voce"),),
        istruzioni="lavora così",
    )
    for blocco in (progetto_mod.blocco_memoria(config), progetto_mod.blocco_istruzioni(config)):
        prima = blocco.strip().splitlines()[0]
        assert prima.count('"') == 2, prima
        assert "<script>" not in prima


# ---------------------------------------------------------------------------
# Dal vault al progetto: la conversione al primo salvataggio
# ---------------------------------------------------------------------------


def _vault_vecchio(base: Path, note: list[str]) -> None:
    base.mkdir(parents=True, exist_ok=True)
    (base / progetto_mod.VECCHIO_FILE).write_text(json.dumps({
        "nome": "Vecchio", "descrizione": "d", "istruzioni": "i", "wiki": False, "note": note,
    }), encoding="utf-8")


def test_il_vault_di_prima_si_legge_uguale(tmp_path):
    base = tmp_path / "v"
    _vault_vecchio(base, ["I test si lanciano da /work", "Niente pandas"])
    assert progetto_mod.is_registrato(base)
    config = progetto_mod.leggi_config(base)
    assert (config.nome, config.descrizione, config.istruzioni) == ("Vecchio", "d", "i")
    # Le note diventano fatti scritti dal modello: era l'unico che poteva.
    assert [(v.testo, v.tipo, v.autore) for v in config.memoria] == [
        ("I test si lanciano da /work", "fatto", "modello"),
        ("Niente pandas", "fatto", "modello"),
    ]


def test_le_note_convertite_hanno_id_stabili_prima_della_riscrittura(tmp_path):
    """Finche' il file non si riscrive, cancellare dalla schermata deve colpire
    la voce giusta: lo stesso testo, lo stesso id, a ogni lettura."""
    base = tmp_path / "v"
    _vault_vecchio(base, ["uno", "due"])
    prima = [v.id for v in progetto_mod.leggi_config(base).memoria]
    dopo = [v.id for v in progetto_mod.leggi_config(base).memoria]
    assert prima == dopo and len(set(prima)) == 2


def test_al_primo_salvataggio_il_file_vecchio_sparisce(tmp_path):
    base = tmp_path / "v"
    _vault_vecchio(base, ["una nota"])
    ids = [v.id for v in progetto_mod.leggi_config(base).memoria]
    progetto_mod.ensure_progetto(base)            # aprirlo lo converte
    assert (base / progetto_mod.PROGETTO_FILE).is_file()
    assert not (base / progetto_mod.VECCHIO_FILE).exists()
    config = progetto_mod.leggi_config(base)
    assert config.nome == "Vecchio"
    assert [v.id for v in config.memoria] == ids, "gli id sopravvivono alla conversione"


def test_un_file_rotto_non_fa_sparire_il_progetto(tmp_path):
    base = tmp_path / "v"
    base.mkdir()
    (base / progetto_mod.PROGETTO_FILE).write_text("{non e' json", encoding="utf-8")
    assert progetto_mod.is_registrato(base)
    assert progetto_mod.leggi_config(base).nome == "v"


# ---------------------------------------------------------------------------
# La memoria: voci con tipo, fonte e autore
# ---------------------------------------------------------------------------


@pytest.fixture()
def progetto(tmp_path):
    base = tmp_path / "p"
    base.mkdir()
    progetto_mod.ensure_progetto(base, nome="Casa")
    return base


def test_una_voce_si_porta_dietro_tipo_fonte_e_autore(progetto):
    _config, voce = progetto_mod.aggiungi_voce(
        progetto, "Usiamo SQLite, non Postgres: gira su una macchina sola",
        tipo="decisione", autore="harness", chat="20260926_1", titolo_chat="Database",
    )
    riletta = progetto_mod.leggi_config(progetto).voce(voce.id)
    assert riletta is not None
    assert (riletta.tipo, riletta.autore, riletta.chat, riletta.titolo_chat) == (
        "decisione", "harness", "20260926_1", "Database")
    assert riletta.creata and riletta.aggiornata


def test_la_stessa_voce_non_si_accumula(progetto):
    progetto_mod.aggiungi_voce(progetto, "una cosa")
    config, _ = progetto_mod.aggiungi_voce(progetto, "una   cosa")
    assert [v.testo for v in config.memoria] == ["una cosa"]


def test_i_tipi_scritti_come_li_scrive_un_modello(progetto):
    for tipo, atteso in (("Decisioni", "decisione"), ("strada scartata", "scartato"),
                         ("TODO", "aperto"), ("boh", "fatto")):
        _c, voce = progetto_mod.aggiungi_voce(progetto, f"voce {tipo}", tipo=tipo)
        assert voce.tipo == atteso, tipo


def test_la_memoria_piena_chiede_di_fare_spazio(progetto):
    for i in range(progetto_mod.MAX_VOCI_MEMORIA):
        progetto_mod.aggiungi_voce(progetto, f"voce numero {i}")
    with pytest.raises(progetto_mod.MemoriaError):
        progetto_mod.aggiungi_voce(progetto, "una di troppo")


def test_una_voce_troppo_lunga_si_taglia_col_marcatore(progetto):
    _c, voce = progetto_mod.aggiungi_voce(progetto, "x" * 500)
    assert len(voce.testo) == progetto_mod.MAX_VOCE_CHARS and voce.testo.endswith("…")


def test_chi_corregge_una_voce_ne_diventa_l_autore(progetto):
    """E' la regola che rende sicura la scrittura automatica."""
    _c, voce = progetto_mod.aggiungi_voce(progetto, "test con pytest", autore="harness")
    _c, corretta = progetto_mod.modifica_voce(progetto, voce.id, testo="test con pytest -q")
    assert corretta.autore == "utente" and corretta.testo == "test con pytest -q"
    with pytest.raises(progetto_mod.MemoriaError):
        progetto_mod.modifica_voce(progetto, voce.id, testo="  ")


def test_il_modello_non_toglie_le_voci_dell_utente(progetto):
    _c, voce = progetto_mod.aggiungi_voce(progetto, "mai toccare legacy/", autore="utente")
    with pytest.raises(progetto_mod.MemoriaError) as errore:
        progetto_mod.togli_voce(progetto, voce.id, autore="modello")
    assert "utente" in str(errore.value)
    # L'utente si'.
    progetto_mod.togli_voce(progetto, voce.id)
    assert progetto_mod.leggi_config(progetto).memoria == ()


def test_una_voce_si_toglie_anche_citandola_a_memoria(progetto):
    progetto_mod.aggiungi_voce(progetto, "Il parser del CSV vuole il separatore esplicito")
    config, _ = progetto_mod.togli_voce(progetto, "Il parser del CSV vuole", autore="modello")
    assert config.memoria == ()


def test_togliere_una_voce_ambigua_dice_che_e_ambigua(progetto):
    progetto_mod.aggiungi_voce(progetto, "la cartella delle prove sta in tmp")
    progetto_mod.aggiungi_voce(progetto, "la cartella dei log sta in var")
    with pytest.raises(progetto_mod.MemoriaError) as errore:
        progetto_mod.togli_voce(progetto, "la cartella")
    assert "2 voci" in str(errore.value)
    with pytest.raises(progetto_mod.MemoriaError) as assente:
        progetto_mod.togli_voce(progetto, "il registro delle spedizioni")
    assert "Nessuna voce corrisponde" in str(assente.value)


def test_una_scrittura_fallita_non_passa_per_riuscita(progetto, monkeypatch):
    progetto_mod.aggiungi_voce(progetto, "questa c'e' davvero")

    def _replace_rotto(_a, _b):
        raise OSError("disco pieno")

    monkeypatch.setattr(os, "replace", _replace_rotto)
    with pytest.raises(progetto_mod.ScritturaError):
        progetto_mod.aggiungi_voce(progetto, "questa non arriva sul disco")
    monkeypatch.undo()
    assert [v.testo for v in progetto_mod.leggi_config(progetto).memoria] == ["questa c'e' davvero"]


def test_il_blocco_raggruppa_per_tipo_dal_piu_stabile(progetto):
    progetto_mod.aggiungi_voce(progetto, "manca la pagina di login", tipo="aperto")
    progetto_mod.aggiungi_voce(progetto, "usiamo FastAPI", tipo="decisione")
    progetto_mod.aggiungi_voce(progetto, "i test da /work", tipo="convenzione")
    blocco = progetto_mod.blocco_memoria(progetto_mod.leggi_config(progetto))
    assert blocco.startswith('<memoria_del_progetto nome="Casa">')
    assert blocco.index("Decisioni:") < blocco.index("Convenzioni:") < blocco.index("Lavori aperti:")
    # Gli id al modello non servono a ogni passo: solo alla scrittura.
    assert "[" not in blocco.split("</memoria_del_progetto>")[0]
    assert progetto_mod.blocco_memoria(progetto_mod.ProgettoConfig(nome="x")) == ""


def test_la_frase_finale_del_blocco_dice_il_vero(progetto):
    """Con la scrittura automatica spenta, "ci pensa l'harness" sarebbe falso."""
    progetto_mod.aggiungi_voce(progetto, "una voce")
    config = progetto_mod.leggi_config(progetto)
    assert "la aggiorna l'harness" in progetto_mod.blocco_memoria(config, automatica=True)
    manuale = progetto_mod.blocco_memoria(config, automatica=False)
    assert "la aggiorna l'harness" not in manuale and "ambito='progetto'" in manuale


# ---------------------------------------------------------------------------
# applica_operazioni: un giro di scrittura automatica
# ---------------------------------------------------------------------------


def test_un_giro_aggiunge_corregge_e_toglie_in_una_scrittura(progetto):
    _c, vecchia = progetto_mod.aggiungi_voce(progetto, "manca il login", tipo="aperto",
                                             autore="harness")
    _c, da_correggere = progetto_mod.aggiungi_voce(progetto, "i test con pytest", autore="harness")
    esito = progetto_mod.applica_operazioni(progetto, [
        {"azione": "aggiungi", "tipo": "decisione", "testo": "Login con sessione, non JWT"},
        {"azione": "modifica", "id": da_correggere.id, "testo": "i test con pytest -q da /work"},
        {"azione": "togli", "id": vecchia.id},
    ], autore="harness", chat="c1", titolo_chat="Login")
    assert esito.cambiata and not esito.rifiutate
    assert [v.testo for v in esito.aggiunte] == ["Login con sessione, non JWT"]
    assert esito.aggiunte[0].chat == "c1" and esito.aggiunte[0].autore == "harness"
    assert esito.modificate[0][1].testo == "i test con pytest -q da /work"
    assert [v.id for v in esito.tolte] == [vecchia.id]
    testi = [v.testo for v in progetto_mod.leggi_config(progetto).memoria]
    assert testi == ["i test con pytest -q da /work", "Login con sessione, non JWT"]


def test_l_harness_non_tocca_le_voci_dell_utente(progetto):
    _c, mia = progetto_mod.aggiungi_voce(progetto, "mai toccare legacy/", autore="utente")
    esito = progetto_mod.applica_operazioni(progetto, [
        {"azione": "modifica", "id": mia.id, "testo": "legacy/ si puo' toccare"},
        {"azione": "togli", "id": mia.id},
    ])
    assert not esito.cambiata
    assert [r["motivo"] for r in esito.rifiutate] == ["voce dell'utente"] * 2
    assert progetto_mod.leggi_config(progetto).voce(mia.id).testo == "mai toccare legacy/"


def test_le_operazioni_sbagliate_si_scartano_una_per_una(progetto):
    progetto_mod.aggiungi_voce(progetto, "gia' presente")
    esito = progetto_mod.applica_operazioni(progetto, [
        {"azione": "aggiungi", "testo": "gia' presente"},
        {"azione": "aggiungi", "testo": "  "},
        {"azione": "togli", "id": "zzzzzz"},
        {"azione": "boh", "testo": "x"},
        {"azione": "aggiungi", "tipo": "scartato", "testo": "Redis e' di troppo per una macchina"},
    ])
    assert [v.testo for v in esito.aggiunte] == ["Redis e' di troppo per una macchina"]
    assert [r["motivo"] for r in esito.rifiutate] == [
        "c'e' gia'", "testo vuoto", "id sconosciuto", "azione sconosciuta"]


def test_un_giro_ha_un_tetto_di_operazioni(progetto):
    ops = [{"azione": "aggiungi", "testo": f"voce {i}"} for i in range(10)]
    esito = progetto_mod.applica_operazioni(progetto, ops)
    assert len(esito.aggiunte) == progetto_mod.MAX_OPERAZIONI_PER_GIRO
    assert len(esito.rifiutate) == 10 - progetto_mod.MAX_OPERAZIONI_PER_GIRO


def test_un_giro_su_un_disco_che_dice_di_no_non_elenca_voci_fantasma(progetto, monkeypatch):
    def _rotto(*_a, **_k):
        raise progetto_mod.ScritturaError("disco pieno")

    monkeypatch.setattr(progetto_mod, "scrivi_config", _rotto)
    esito = progetto_mod.applica_operazioni(progetto, [{"azione": "aggiungi", "testo": "x"}])
    assert esito.errore and not esito.aggiunte and not esito.cambiata


# ---------------------------------------------------------------------------
# manage_notes ambito='progetto'
# ---------------------------------------------------------------------------


def test_il_tool_scrive_nel_progetto_solo_se_c_e_un_progetto(tmp_path, progetto):
    fuori = ToolContext(workspace=str(tmp_path), sandbox="host")
    esito = json.loads(dispatch(fuori, NOTES_TOOL, {
        "action": "add", "text": "x", "ambito": "progetto"}))
    assert "error" in esito and len(fuori.notes) == 0

    dentro = ToolContext(workspace=str(progetto), sandbox="host", progetto_dir=str(progetto),
                         chat_id="c7", chat_titolo="Prove")
    esito = json.loads(dispatch(dentro, NOTES_TOOL, {
        "action": "add", "text": "convenzione: i test si lanciano da /work",
        "ambito": "progetto"}))
    assert esito["status"] == "ok" and esito["ambito"] == "progetto"
    voce = progetto_mod.leggi_config(progetto).memoria[0]
    # Il tipo viaggia come prefisso del testo, non come parametro in piu'.
    assert (voce.tipo, voce.testo, voce.autore, voce.chat) == (
        "convenzione", "i test si lanciano da /work", "modello", "c7")
    assert dentro.progetto_memoria and len(dentro.notes) == 0


def test_un_prefisso_che_non_e_un_tipo_resta_nel_testo(progetto):
    ctx = ToolContext(workspace=str(progetto), sandbox="host", progetto_dir=str(progetto))
    dispatch(ctx, NOTES_TOOL, {"action": "add", "text": "Nota: il parser e' lento",
                               "ambito": "progetto"})
    voce = progetto_mod.leggi_config(progetto).memoria[0]
    assert voce.testo == "Nota: il parser e' lento" and voce.tipo == "fatto"


def test_la_memoria_del_progetto_non_si_svuota_in_blocco(progetto):
    progetto_mod.aggiungi_voce(progetto, "da tenere")
    ctx = ToolContext(workspace=str(progetto), sandbox="host", progetto_dir=str(progetto))
    esito = json.loads(dispatch(ctx, NOTES_TOOL, {"action": "clear", "ambito": "progetto"}))
    assert "error" in esito
    assert [v.testo for v in progetto_mod.leggi_config(progetto).memoria] == ["da tenere"]


def test_vault_come_ambito_lo_ferma_la_validazione_con_i_valori_giusti(progetto):
    ctx = ToolContext(workspace=str(progetto), sandbox="host", progetto_dir=str(progetto))
    esito = json.loads(dispatch(ctx, NOTES_TOOL, {"action": "add", "text": "x", "ambito": "vault"}))
    assert "error" in esito and "progetto" in json.dumps(esito, ensure_ascii=False)


def test_il_blocco_del_progetto_sta_in_coda_e_prima_di_quello_della_chat():
    from core.agent import build_api_messages

    api = build_api_messages(
        [{"role": "user", "content": "ciao"}],
        system_prompt="SYS", env_header="ENV",
        progetto_block="<memoria_del_progetto>", notes_block="<note_di_lavoro>",
        plan_block="<piano>",
    )
    coda = api[-1]
    assert coda["role"] == "user"
    assert coda["content"].index("<memoria_del_progetto>") < coda["content"].index("<note_di_lavoro>")
    assert "memoria_del_progetto" not in api[0]["content"]


# ---------------------------------------------------------------------------
# Le chat appartengono al progetto della cartella su cui lavorano
# ---------------------------------------------------------------------------


@pytest.fixture()
def archivio_chat(tmp_path, monkeypatch):
    """Quattro conversazioni: tre in un progetto (una archiviata), una fuori."""
    from core import session as session_mod

    cartella = tmp_path / "sessions"
    cartella.mkdir()
    monkeypatch.setattr(session_mod, "DATA_DIR", cartella)
    session_mod._index_cache.clear()
    session_mod._search_cache.clear()

    kv = tmp_path / "progetto_uno"
    kv.mkdir()
    fuori = tmp_path / "altrove"
    fuori.mkdir()

    def scrivi(ident, workspace, giorno, **extra):
        (cartella / f"{ident}.json").write_text(json.dumps({
            "id": ident, "title": f"chat {ident}",
            "updated_at": f"2026-08-{giorno:02d}T10:00:00",
            "workspace_dir": str(workspace),
            "messages": [{"role": "user", "content": "ciao"}], **extra,
        }), encoding="utf-8")

    scrivi("a", kv, 1)
    scrivi("b", kv, 2)
    scrivi("z", kv, 4, archiviata=True)
    scrivi("c", fuori, 3)
    return session_mod, kv, fuori


def test_l_elenco_di_un_progetto_sono_le_chat_fatte_li_dentro(archivio_chat):
    session_mod, kv, _fuori = archivio_chat
    assert sorted(s["id"] for s in session_mod.list_sessions(cartella=str(kv))) == ["a", "b"]


def test_le_archiviate_si_chiedono_a_parte(archivio_chat):
    session_mod, kv, _fuori = archivio_chat
    assert [s["id"] for s in session_mod.list_sessions(cartella=str(kv), archiviate=True)] == ["z"]
    assert sorted(s["id"] for s in session_mod.list_sessions(cartella=str(kv), archiviate=None)) \
        == ["a", "b", "z"]


def test_l_elenco_generale_non_mostra_le_chat_dei_progetti(archivio_chat):
    session_mod, kv, _fuori = archivio_chat
    assert [s["id"] for s in session_mod.list_sessions(escludi=[str(kv)])] == ["c"]


def test_lo_stesso_percorso_scritto_diverso_e_lo_stesso_posto(archivio_chat):
    session_mod, kv, _fuori = archivio_chat
    storto = str(kv / ".") + os.sep
    assert sorted(s["id"] for s in session_mod.list_sessions(cartella=storto)) == ["a", "b"]


def test_rinominare_non_fa_salire_la_chat_in_cima(archivio_chat):
    session_mod, _kv, _fuori = archivio_chat
    assert session_mod.aggiorna_metadati("a", titolo_utente="Il login")
    riga = next(s for s in session_mod.list_sessions(archiviate=None) if s["id"] == "a")
    assert riga["title"] == "Il login"
    assert riga["updated_at"] == "2026-08-01T10:00:00"
    # Titolo vuoto: torna quello ricavato dalla prima riga.
    session_mod.aggiorna_metadati("a", titolo_utente="", titolo_ricavato="ciao")
    riga = next(s for s in session_mod.list_sessions(archiviate=None) if s["id"] == "a")
    assert riga["title"] == "ciao"
    assert not session_mod.aggiorna_metadati("mai-esistita", archiviata=True)


def test_il_titolo_scelto_sopravvive_al_salvataggio(archivio_chat):
    session_mod, kv, _fuori = archivio_chat
    stato: dict = {}
    assert session_mod.load_session(stato, "a")
    stato["titolo_utente"] = "Scelto da me"
    stato["messages"].append({"role": "assistant", "content": "ok"})
    assert session_mod.save_session(stato, force=True)
    riletto: dict = {}
    session_mod.load_session(riletto, "a")
    assert riletto["titolo_utente"] == "Scelto da me"
    riga = next(s for s in session_mod.list_sessions(cartella=str(kv)) if s["id"] == "a")
    assert riga["title"] == "Scelto da me"


def test_la_goccia_della_memoria_non_conta_come_messaggio():
    from core.session import conta_visibili

    assert conta_visibili([
        {"role": "user", "content": "x"}, {"role": "assistant", "content": "y"},
        {"role": "memoria", "esito": {}},
    ]) == 2


def test_l_ultima_risposta_si_legge_dalla_coda_del_file(archivio_chat):
    session_mod, _kv, _fuori = archivio_chat
    stato: dict = {}
    session_mod.load_session(stato, "b")
    stato["messages"] += [
        {"role": "assistant", "content": "<think>ragiono</think>Ho finito il parser."},
        {"role": "memoria", "esito": {}},
    ]
    session_mod.save_session(stato, force=True)
    assert session_mod.ultima_risposta_salvata("b") == "Ho finito il parser."


# ---------------------------------------------------------------------------
# Le impostazioni: la chiave di prima si rinomina alla lettura
# ---------------------------------------------------------------------------


def test_la_chiave_vaults_diventa_progetti(tmp_path):
    from core.settings import load_settings

    file = tmp_path / "agent_settings.json"
    file.write_text(json.dumps({"vaults": [{"path": "/x", "nome": "X"}]}), encoding="utf-8")
    settings = load_settings(file)
    assert settings["progetti"] == [{"path": "/x", "nome": "X"}]
    assert "vaults" not in settings
    assert settings["memoria_progetto"] is True


# ---------------------------------------------------------------------------
# Le rotte /api/progetti*
# ---------------------------------------------------------------------------


@pytest.fixture()
def client_progetti(monkeypatch, tmp_path):
    """Client FastAPI con uno STATE isolato in tmp, senza bootstrap reale."""
    import server.main as M
    from fastapi.testclient import TestClient

    from core import session as session_mod
    from core.prompts import SYSTEM_PROMPT

    ws_normale = tmp_path / "ws"
    ws_normale.mkdir()
    sessioni = tmp_path / "sessions"
    sessioni.mkdir()
    monkeypatch.setattr(session_mod, "DATA_DIR", sessioni)
    session_mod._index_cache.clear()
    session_mod._search_cache.clear()

    st = M.AppState.__new__(M.AppState)
    st.memories = []
    import copy

    from core.config import DEFAULTS

    st.settings = {
        **copy.deepcopy(DEFAULTS),
        "workspace_dir": str(ws_normale),
        "progetti": [],
        "recent_workspaces": [str(ws_normale)],
        "sandbox": "subprocess",
        "image_autobuild": False,
        "docker_image": "",
        "confirm_commands": False,
        "native_think": False,
        "web_search": False,
        "memoria_progetto": True,
        "system_prompt": SYSTEM_PROMPT,
    }
    st._booted = True
    st._lock = threading.Lock()
    st._last_opened = ""
    st._sessions = {}
    st.persist = lambda: None
    monkeypatch.setattr(M, "STATE", st)
    monkeypatch.setattr(M, "session_stats", lambda *_a, **_k: {})
    monkeypatch.setattr(M, "maybe_prepare_workspace", lambda: None)
    return TestClient(M.app), st, tmp_path


def test_crea_apri_elenco_togli(client_progetti):
    client, st, td = client_progetti
    kv = td / "mio"
    kv.mkdir()

    assert client.get("/api/progetti").json() == {"progetti": []}

    r = client.post("/api/progetti", json={
        "path": str(kv), "nome": "Mio", "istruzioni": "In italiano."})
    assert r.status_code == 200, r.text
    assert r.json()["progetto"]["nome"] == "Mio" and r.json()["esisteva"] is False
    # doppia registrazione: idempotente sul registro, e l'identita' vince
    r = client.post("/api/progetti", json={"path": str(kv), "nome": "Altro"})
    assert r.json()["esisteva"] is True and r.json()["progetto"]["nome"] == "Mio"
    assert len(st.settings["progetti"]) == 1
    assert progetto_mod.is_registrato(kv) and not progetto_mod.ha_struttura_wiki(kv)

    r = client.post("/api/progetti/open", json={"nome": "Mio"})
    assert r.status_code == 200
    assert st.settings["workspace_dir"] == str(kv.resolve())
    corpo = r.json()
    assert corpo["progetto"]["nome"] == "Mio" and corpo["sessions"] == []
    assert corpo["riprendi"] is None and corpo["libreria"] == []
    assert corpo["memoria_automatica"] is True

    voci = client.get("/api/progetti").json()["progetti"]
    assert voci[0]["attivo"] is True and voci[0]["wiki"] is False

    r = client.patch("/api/progetti", json={"path": str(kv), "wiki": True})
    assert r.json()["progetto"]["wiki"] is True and progetto_mod.ha_struttura_wiki(kv)

    assert client.post("/api/progetti/open", json={"nome": "fantasma"}).status_code == 400

    r = client.post("/api/progetti/remove", json={"nome": "Mio"})
    assert r.json()["rimossi"] == 1 and st.settings["progetti"] == []
    # La cartella e la sua identita' restano: registrandola si ritrova tutto.
    assert progetto_mod.leggi_config(kv).nome == "Mio"


def test_la_creazione_puo_creare_la_cartella(client_progetti):
    client, st, td = client_progetti
    r = client.post("/api/progetti", json={
        "path": str(td), "nome": "Nuovo lavoro", "crea_cartella": "Nuovo: lavoro?"})
    assert r.status_code == 200, r.text
    nuova = td / "Nuovo lavoro"
    assert nuova.is_dir() and progetto_mod.leggi_config(nuova).nome == "Nuovo lavoro"
    assert st.settings["progetti"][0]["path"] == str(nuova.resolve())
    assert client.post("/api/progetti", json={
        "path": str(td), "crea_cartella": "..."}).status_code == 400
    assert client.post("/api/progetti", json={
        "path": str(td / "assente"), "nome": "x"}).status_code == 400


def test_un_progetto_senza_cartella_resta_in_elenco_per_poterlo_togliere(client_progetti):
    client, st, td = client_progetti
    st.settings["progetti"] = [{"path": str(td / "scollegato"), "nome": "Disco esterno"}]
    voci = client.get("/api/progetti").json()["progetti"]
    assert voci == [{
        "path": str(td / "scollegato"), "nome": "Disco esterno", "esiste": False,
        "attivo": False, "chat": 0, "memoria": [], "wiki": False, "descrizione": "",
        "istruzioni": "",
    }]


def test_la_scelta_della_cartella_non_registra_niente(client_progetti, monkeypatch):
    """La finestra "Nuovo progetto" riceve il percorso; il progetto nasce con
    "Crea", con il nome e le istruzioni che ha deciso l'utente."""
    client, st, td = client_progetti
    import server.main as M

    kv = td / "wiki_della_mamma"
    (kv / "raw").mkdir(parents=True)
    (kv / "wiki").mkdir()
    monkeypatch.setattr(M, "pick_folder", lambda _initial=None: str(kv))
    corpo = client.post("/api/progetti/pick").json()
    assert corpo == {"cancelled": False, "path": str(kv.resolve()), "gia_progetto": False,
                     "nome": "wiki_della_mamma", "wiki": True}
    assert st.settings["progetti"] == [] and not progetto_mod.is_registrato(kv)

    monkeypatch.setattr(M, "pick_folder", lambda _initial=None: None)
    assert client.post("/api/progetti/pick").json() == {"cancelled": True}

    from server.nativedialog import DialogUnavailable

    def _assente(_initial=None):
        raise DialogUnavailable("nessun display")

    monkeypatch.setattr(M, "pick_folder", _assente)
    assert client.post("/api/progetti/pick").status_code == 501


def test_patch_scrive_nella_cartella_e_riallinea_il_registro(client_progetti):
    client, st, td = client_progetti
    base = td / "vv"
    base.mkdir()
    client.post("/api/progetti", json={"path": str(base), "nome": "prima"})
    r = client.patch("/api/progetti", json={
        "path": str(base), "nome": "dopo", "descrizione": "d", "istruzioni": "i"})
    voce = r.json()["progetto"]
    assert (voce["nome"], voce["descrizione"], voce["istruzioni"]) == ("dopo", "d", "i")
    assert progetto_mod.leggi_config(base).nome == "dopo"
    assert st.settings["progetti"][0]["nome"] == "dopo"


def test_la_memoria_si_scrive_corregge_e_toglie_dalla_schermata(client_progetti):
    client, _st, td = client_progetti
    base = td / "vv"
    base.mkdir()
    client.post("/api/progetti", json={"path": str(base), "nome": "P"})

    r = client.post("/api/progetti/memoria", json={
        "path": str(base), "testo": "mai toccare legacy/", "tipo": "convenzione"})
    assert r.status_code == 200
    voce = r.json()["progetto"]["memoria"][0]
    assert (voce["autore"], voce["tipo"]) == ("utente", "convenzione")

    _c, dell_harness = progetto_mod.aggiungi_voce(base, "usiamo SQLite", autore="harness")
    r = client.patch("/api/progetti/memoria", json={
        "path": str(base), "id": dell_harness.id, "testo": "usiamo SQLite in WAL"})
    corretta = next(v for v in r.json()["progetto"]["memoria"] if v["id"] == dell_harness.id)
    assert corretta["testo"] == "usiamo SQLite in WAL" and corretta["autore"] == "utente"

    r = client.delete("/api/progetti/memoria", params={"path": str(base), "id": voce["id"]})
    assert [v["id"] for v in r.json()["progetto"]["memoria"]] == [dell_harness.id]
    assert client.delete("/api/progetti/memoria",
                         params={"path": str(base), "id": "nessuno"}).status_code == 404
    assert client.post("/api/progetti/memoria", json={
        "path": str(base), "testo": "  "}).status_code == 400


def test_la_home_porta_riprendi_da_qui_e_libreria(client_progetti):
    client, _st, td = client_progetti
    from core import libreria as libreria_mod
    from core import session as session_mod

    base = td / "vv"
    base.mkdir()
    client.post("/api/progetti", json={"path": str(base), "nome": "P"})
    stato = {"current_session_id": "20260926_100000_aaaa", "workspace_dir": str(base.resolve()),
             "messages": [{"role": "user", "content": "rifai il login"},
                          {"role": "assistant", "content": "Fatto il form, manca la sessione."}],
             "plan": [{"id": "1", "text": "form", "status": "done", "note": ""},
                      {"id": "2", "text": "sessione", "status": "todo", "note": ""}],
             "checkpoint": {"motivo": "max_steps"}}
    session_mod.save_session(stato, force=True)
    libreria_mod.archivia(base, riassunto="Il login usa la sessione", richieste=["rifai il login"])

    corpo = client.get("/api/progetti/home", params={"path": str(base)}).json()
    riprendi = corpo["riprendi"]
    assert riprendi["session"]["id"] == "20260926_100000_aaaa"
    assert riprendi["stato"] == "max_steps"
    assert riprendi["piano"] == {"totale": 2, "fatti": 1,
                                 "aperti": [{"id": "2", "text": "sessione", "status": "todo"}]}
    assert riprendi["ultima_risposta"] == "Fatto il form, manca la sessione."
    assert len(corpo["libreria"]) == 1
    nome = corpo["libreria"][0]["nome"]
    letta = client.get("/api/progetti/libreria", params={"path": str(base), "nome": nome}).json()
    assert "Il login usa la sessione" in letta["testo"]
    assert client.get("/api/progetti/libreria", params={
        "path": str(base), "nome": "../agent_settings.json"}).status_code == 404
    assert client.get("/api/progetti/home",
                      params={"path": str(td / "fantasma")}).status_code == 404


# ---------------------------------------------------------------------------
# Conversazioni: rinomina, archivia, sposta
# ---------------------------------------------------------------------------


def _chat_salvata(st, ident: str, cartella: Path, testo: str = "ciao") -> None:
    from core import session as session_mod

    stato = {"current_session_id": ident, "workspace_dir": str(cartella),
             "messages": [{"role": "user", "content": testo}]}
    session_mod.save_session(stato, force=True)


def test_rinomina_e_archivia_una_conversazione(client_progetti):
    client, st, td = client_progetti
    _chat_salvata(st, "20260926_110000_bbbb", td / "ws", "prima riga della chat")

    r = client.patch("/api/sessions/20260926_110000_bbbb", json={"titolo": "  Il mio titolo "})
    assert r.status_code == 200
    righe = client.get("/api/sessions").json()["sessions"]
    assert righe[0]["title"] == "Il mio titolo"

    client.patch("/api/sessions/20260926_110000_bbbb", json={"archiviata": True})
    corpo = client.get("/api/sessions").json()
    assert corpo["sessions"] == [] and corpo["archiviate"] == 1
    archiviate = client.get("/api/sessions", params={"archiviate": True}).json()["sessions"]
    assert [s["id"] for s in archiviate] == ["20260926_110000_bbbb"]


def test_spostare_una_chat_in_un_progetto_le_cambia_cartella(client_progetti):
    client, st, td = client_progetti
    base = td / "vv"
    base.mkdir()
    client.post("/api/progetti", json={"path": str(base), "nome": "P"})
    _chat_salvata(st, "20260926_120000_cccc", td / "ws")

    r = client.post("/api/sessions/20260926_120000_cccc/sposta", json={"progetto": str(base)})
    assert r.status_code == 200, r.text
    assert r.json()["progetto"]["nome"] == "P"
    home = client.get("/api/progetti/home", params={"path": str(base)}).json()
    assert [s["id"] for s in home["sessions"]] == ["20260926_120000_cccc"]
    assert client.get("/api/sessions").json()["sessions"] == []

    # E fuori: nell'ultima cartella libera usata.
    r = client.post("/api/sessions/20260926_120000_cccc/sposta", json={})
    assert r.status_code == 200 and r.json()["progetto"] is None
    assert [s["id"] for s in client.get("/api/sessions").json()["sessions"]] == [
        "20260926_120000_cccc"]

    # Un progetto non registrato non e' una destinazione.
    altra = td / "altra"
    altra.mkdir()
    assert client.post("/api/sessions/20260926_120000_cccc/sposta",
                       json={"progetto": str(altra)}).status_code == 400


def test_il_telefono_riceve_il_progetto_di_ogni_chat(client_progetti):
    client, st, td = client_progetti
    base = td / "vv"
    base.mkdir()
    client.post("/api/progetti", json={"path": str(base), "nome": "P"})
    _chat_salvata(st, "20260926_130000_dddd", base.resolve())
    _chat_salvata(st, "20260926_130001_eeee", td / "ws")
    righe = {s["id"]: s for s in client.get("/api/sessions", params={"tutte": True}).json()["sessions"]}
    assert righe["20260926_130000_dddd"]["progetto"]["nome"] == "P"
    assert righe["20260926_130001_eeee"]["progetto"] is None


# ---------------------------------------------------------------------------
# Il prompt di sistema
# ---------------------------------------------------------------------------


def test_il_prompt_diventa_manutentore_solo_con_la_wiki(client_progetti):
    _client, st, td = client_progetti
    from server.main import AppState

    kv = td / "mia_wiki"
    progetto_mod.scaffold(kv)
    st.settings["workspace_dir"] = str(kv)
    sp = AppState.system_prompt(st)
    assert "manutentore della wiki" in sp and "## Struttura della wiki" in sp

    st.settings["system_prompt"] = "Sei l'assistente di casa Rossi."
    sp = AppState.system_prompt(st)
    assert "assistente di casa Rossi" in sp and "manutentore della wiki" not in sp
    assert "## Struttura della wiki" in sp


def test_lo_stato_della_wiki_non_entra_nel_prompt_di_sistema(client_progetti):
    from server.main import AppState

    _client, st, td = client_progetti
    kv = td / "mia_wiki"
    progetto_mod.scaffold(kv)
    st.settings["workspace_dir"] = str(kv)
    (kv / "wiki" / "index.md").write_text("# Indice\n- [[Memex]]\n", encoding="utf-8")
    prima = AppState.system_prompt(st)
    (kv / "wiki" / "index.md").write_text("# Indice\n- [[Memex]]\n- [[Hypertext]]\n",
                                          encoding="utf-8")
    progetto_mod.appendi_log(kv, "ingest", "nelson-1965.pdf")
    assert AppState.system_prompt(st) == prima
    assert "Memex" not in prima


def test_le_istruzioni_arrivano_al_modello_anche_senza_wiki(client_progetti):
    _client, st, td = client_progetti
    from server.main import AppState

    base = td / "codice"
    base.mkdir()
    progetto_mod.ensure_progetto(base, nome="Codice", istruzioni="Non toccare mai legacy/.")
    st.settings["workspace_dir"] = str(base)
    sp = AppState.system_prompt(st)
    assert "Non toccare mai legacy/." in sp and "manutentore della wiki" not in sp


def test_il_turno_riceve_il_progetto_nel_contesto_dei_tool(client_progetti):
    _client, st, td = client_progetti
    from server.main import AppState

    base = td / "codice"
    base.mkdir()
    progetto_mod.ensure_progetto(base, nome="Codice")
    progetto_mod.aggiungi_voce(base, "usiamo SQLite", tipo="decisione")
    st.settings["workspace_dir"] = str(base)
    st._sessions["s1"] = {"current_session_id": "s1", "messages": [
        {"role": "user", "content": "rifai il login"}], "titolo_utente": ""}
    ctx = AppState.tool_ctx(st, "s1")
    assert ctx.progetto_dir == str(base) and ctx.progetto_nome == "Codice"
    assert [v.testo for v in ctx.progetto_memoria] == ["usiamo SQLite"]
    assert (ctx.chat_id, ctx.chat_titolo) == ("s1", "rifai il login")


def test_lo_stato_della_wiki_arriva_al_modello_in_coda(tmp_path, fake_ollama):
    """Anche alle wiki nate prima del file d'identita', riconosciute dalla struttura."""
    from core import agent as agent_mod
    from core.backend import OllamaBackend
    from core.config import GenParams

    url, _ = fake_ollama
    base = tmp_path / "wiki_vecchia"
    progetto_mod.scaffold(base)
    assert not progetto_mod.is_registrato(base)
    (base / "wiki" / "index.md").write_text("# Indice\n- [[Memex]] di Bush\n", encoding="utf-8")

    import tests.test_agent_loop as fake

    fake._Handler.calls.clear()
    ctx = ToolContext(workspace=str(base), sandbox="host")
    list(agent_mod.run_turn(
        backend=OllamaBackend(url, timeout_s=20), params=GenParams(model="fake:latest"),
        tools_schema=TOOLS_SCHEMA, tool_ctx=ctx,
        ui_messages=[{"role": "user", "content": "che dice la wiki del memex?"}],
        system_prompt="SYS", env_header=None, max_steps=1,
    ))
    prima = fake._Handler.calls[0]
    coda = [m for m in prima["messages"] if m.get("role") == "user"][-1]["content"]
    assert "stato_della_wiki" in coda and "Memex" in coda
    sistema = " ".join(m["content"] for m in prima["messages"] if m.get("role") == "system")
    assert "Memex" not in sistema
