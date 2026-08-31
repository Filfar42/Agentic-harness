"""Vault: identita' della cartella, chat che ci vivono dentro, wiki opzionale.

Un vault e' un posto di lavoro -- nome, descrizione, cartella e le sue
conversazioni -- e non piu' "il workspace su cui si accende la modalita'
wiki". Quella modalita' e' diventata una **proprieta'** del vault.

Tre contratti:
- ``core/vault``: scaffold idempotente della struttura LLM Wiki (raw/ immutabile,
  wiki/ dell'agente, schema CLAUDE.md), indice e coda del log;
- ``core/vault_search``: la sotto-chiamata in sola lettura al manutentore --
  senza far girare un modello, ``run_turn`` arriva iniettabile come per la
  delega -- e i suoi errori puliti;
- le rotte ``/api/vaults*`` del server: registro, apertura, rimozione.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

import tests.test_agent_loop as _fake_loop

fake_ollama = _fake_loop.fake_ollama

from core import vault as vault_mod
from core import vault_search
from core.tools import TOOLS_SCHEMA, ToolContext


# ---------------------------------------------------------------------------
# Struttura del vault
# ---------------------------------------------------------------------------


def test_scaffold_crea_la_struttura_llm_wiki(tmp_path):
    base = tmp_path / "kv"
    creati = vault_mod.scaffold(base)
    assert set(creati) == {"raw", "raw/assets", "wiki", "CLAUDE.md", "wiki/index.md", "wiki/log.md"}
    assert vault_mod.is_vault(base)
    # Idempotente: la seconda chiamata non crea nulla di nuovo.
    assert vault_mod.scaffold(base) == []


def test_cartella_vuota_non_e_un_vault(tmp_path):
    nuda = tmp_path / "nuda"
    nuda.mkdir()
    assert not vault_mod.is_vault(nuda)


def test_info_vault_conta_fonti_e_pagine(tmp_path):
    base = tmp_path / "kv"
    vault_mod.scaffold(base)
    (base / "raw" / "articolo.md").write_text("fonte", encoding="utf-8")
    (base / "raw" / "assets" / "img.png").write_bytes(b"x")  # non e' una fonte md
    (base / "wiki" / "Memex.md").write_text("# Memex", encoding="utf-8")
    info = vault_mod.info_vault(str(base), "prova")
    d = info.as_dict()
    assert d["nome"] == "prova"
    assert d["fonti"] == 1
    assert d["pagine"] >= 3  # index.md + log.md + Memex.md


def test_log_append_only_con_prefisso_parseabile(tmp_path):
    base = tmp_path / "kv"
    vault_mod.scaffold(base)
    vault_mod.appendi_log(base, "ingest", "paper-memex.pdf")
    vault_mod.appendi_log(base, "query", "confronto memex/wikipedia")
    coda = vault_mod.leggi_log_coda(base)
    righe = [r for r in coda.splitlines() if r.startswith("## [")]
    assert len(righe) == 2
    assert "| paper-memex.pdf" in righe[0]
    # grepabile come promette lo schema di Karpathy
    assert all(r.split("] ")[1].startswith(("ingest", "query")) for r in righe)


def test_leggi_indice_tronca_con_marcatore(tmp_path):
    base = tmp_path / "kv"
    vault_mod.scaffold(base)
    indice = vault_mod.leggi_indice(base, max_chars=50)
    assert len(indice) <= 50 + len("\n[indice troncato]") + 2
    assert "[indice troncato]" in indice


# ---------------------------------------------------------------------------
# Prompt da manutentore
# ---------------------------------------------------------------------------


def test_blocco_manutenzione_porta_indice_e_log(tmp_path):
    base = tmp_path / "kv"
    vault_mod.scaffold(base)
    blocco = vault_mod.blocco_manutenzione(base)
    assert "# Indice attuale della wiki" in blocco
    assert vault_mod.SCHEMA_FILE in blocco
    assert "_(log vuoto: nessuna operazione registrata)_" in blocco
    # ...e le due meta' sono separabili: la struttura sta ferma, lo stato no.
    assert vault_mod.SCHEMA_FILE in vault_mod.blocco_struttura(base)
    assert "# Indice attuale della wiki" in vault_mod.blocco_stato(base)
    assert "# Indice attuale della wiki" not in vault_mod.blocco_struttura(base)


def test_is_modalita_vault_accetta_str_e_path(tmp_path):
    base = tmp_path / "kv"
    vault_mod.scaffold(base)
    altro = tmp_path / "altro"
    altro.mkdir()
    assert vault_mod.is_modalita_vault(base)
    assert vault_mod.is_modalita_vault(str(base))
    assert not vault_mod.is_modalita_vault(altro)


# ---------------------------------------------------------------------------
# vault_search: schema ridotto e sotto-chiamata
# ---------------------------------------------------------------------------


@dataclass
class _Params:
    think: bool | str = True


class ToolFinished:
    pass


class AssistantTurn:
    def __init__(self, content):
        self.content = content


def _vault_di_prova(tmp_path: Path) -> Path:
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
        yield ToolFinished()
        yield AssistantTurn(risposta)

    return _run


def test_schema_ridotto_espone_solo_lettura():
    nomi = [t["function"]["name"] for t in vault_search.schema_ridotto(TOOLS_SCHEMA)]
    assert nomi == ["list_files", "read_file", "search_files"]
    # Niente tool di scrittura nel sotto-turno: il cercatore non tocca la wiki.
    assert not {"write_file", "edit_file"} & set(nomi)


def test_cerca_nel_vault_gira_sul_vault_e_cita_le_fonti(tmp_path):
    base = _vault_di_prova(tmp_path)
    spioni: dict = {}
    esito = vault_search.cerca_nel_vault(
        "kv",
        "che cosa e' il memex?",
        backend=object(),
        params=_Params(),
        tools_schema=TOOLS_SCHEMA,
        tool_ctx=ToolContext(workspace=str(tmp_path / "fuori")),
        env_header=None,
        run_turn=_run_turn_che_risponde(
            "Il Memex e' la macchina di Bush (1945). Vedi [[Memex]] in wiki/Memex.md.",
            spioni,
        ),
        registri=[{"nome": "kv", "path": str(base)}],
    )
    assert "errore" not in esito
    assert esito["vault"] == "kv"
    assert esito["passi"] == 1
    assert "Bush" in esito["referto"]
    # Il figlio gira sul workspace del vault, non su quello del padre...
    assert spioni["workspace"] == str(base)
    # ...col prompt del cercatore che porta l'indice della wiki...
    assert "# Indice attuale della wiki" in spioni["system_prompt"]
    # ...senza pensiero e coi soli tre tool di lettura.
    assert spioni["params"].think is False
    assert spioni["nomi_tool"] == ["list_files", "read_file", "search_files"]
    # ...e con quei tre tool come **permesso**, non solo come schema: le
    # chiamate scritte come testo non passano dallo schema, e il workspace
    # del cercatore e' la cartella di un altro vault dell'utente.
    assert spioni["ctx"].tool_consentiti == frozenset(vault_search.TOOL_CERCA)
    assert not spioni["ctx"].puo_usare("write_file")
    assert not spioni["ctx"].puo_usare("run_command")
    # Un cercatore non apre altri sotto-turni.
    assert spioni["ctx"].on_delega is None
    assert spioni["ctx"].on_vault_search is None


def test_cerca_nel_vault_nome_sconosciuto_errore_pulito(tmp_path):
    base = _vault_di_prova(tmp_path)
    esito = vault_search.cerca_nel_vault(
        "fantasma",
        "x",
        backend=object(),
        params=_Params(),
        tools_schema=[],
        tool_ctx=None,
        env_header=None,
        run_turn=lambda **_kw: iter([]),
        registri=[{"nome": "kv", "path": str(base)}],
    )
    assert "errore" in esito and "fantasma" in esito["errore"]


def test_cerca_nel_vault_query_vuota_errore_pulito(tmp_path):
    base = _vault_di_prova(tmp_path)
    esito = vault_search.cerca_nel_vault(
        "kv",
        "   ",
        backend=object(),
        params=_Params(),
        tools_schema=[],
        tool_ctx=None,
        env_header=None,
        run_turn=lambda **_kw: iter([]),
        registri=[{"nome": "kv", "path": str(base)}],
    )
    assert "errore" in esito


def test_referto_compatto_json_valido():
    testo = vault_search.referto_compatto(
        {"vault": "kv", "query": "q", "passi": 2, "referto": "risposta"}
    )
    assert '"vault": "kv"' in testo and '"passi": 2' in testo


def test_tool_vault_search_registrato_negli_schemi():
    from core.tools import VAULT_SEARCH_TOOL, TOOL_IMPLS, _ALLOWED_ARGS

    assert VAULT_SEARCH_TOOL in TOOL_IMPLS
    assert VAULT_SEARCH_TOOL in _ALLOWED_ARGS
    nomi = [t["function"]["name"] for t in TOOLS_SCHEMA]
    assert VAULT_SEARCH_TOOL in nomi


# ---------------------------------------------------------------------------
# Rotte /api/vaults*
# ---------------------------------------------------------------------------


@pytest.fixture()
def client_vault(monkeypatch, tmp_path):
    """Client FastAPI con uno STATE isolato in tmp, senza bootstrap reale."""
    import server.main as M
    from fastapi.testclient import TestClient

    ws_normale = tmp_path / "ws"
    ws_normale.mkdir()
    from core.prompts import SYSTEM_PROMPT

    st = M.AppState.__new__(M.AppState)
    st.memories = []
    st.settings = {
        "workspace_dir": str(ws_normale),
        "vaults": [],
        "recent_workspaces": [],
        "sandbox": "subprocess",
        "image_autobuild": False,
        "docker_image": "",
        "confirm_commands": False,
        "native_think": False,
        "web_search": False,
        "system_prompt": SYSTEM_PROMPT,
    }
    st._booted = True
    st._lock = threading.Lock()
    st._last_opened = ""
    st._sessions = {}
    st.persist = lambda: None
    monkeypatch.setattr(M, "STATE", st)
    def _stats_finte(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(M, "session_stats", _stats_finte)
    return TestClient(M.app), st, tmp_path


def test_registra_apri_elenco_rimuovi(client_vault):
    client, st, td = client_vault
    kv = td / "mio_vault"
    kv.mkdir()

    # elenco vuoto
    r = client.get("/api/vaults")
    assert r.status_code == 200
    assert r.json() == {"vaults": []}

    # registrazione di una cartella ancora senza struttura
    r = client.post("/api/vaults", json={"path": str(kv), "nome": "mio"})
    assert r.status_code == 200
    assert r.json()["vault"]["nome"] == "mio"

    # doppia registrazione: idempotente sul registro
    client.post("/api/vaults", json={"path": str(kv), "nome": "altro nome"})
    assert len(st.settings["vaults"]) == 1

    # Registrare scrive l'identita' **nella cartella**, non nelle impostazioni...
    assert vault_mod.is_registrato(kv)
    # ...e non monta la struttura wiki: un vault non e' per forza una wiki, e
    # una cartella qualsiasi non deve ritrovarsi raw/ e wiki/ non chieste.
    assert not vault_mod.ha_struttura_wiki(kv)

    # apertura: diventa il workspace corrente
    r = client.post("/api/vaults/open", json={"nome": "mio"})
    assert r.status_code == 200
    assert st.settings["workspace_dir"] == str(kv.resolve())
    # ...e la risposta porta gia' la schermata iniziale del vault
    assert r.json()["vault"]["nome"] == "mio"
    assert r.json()["sessions"] == []

    # l'elenco mostra il vault attivo
    voci = client.get("/api/vaults").json()["vaults"]
    assert len(voci) == 1
    assert voci[0]["attivo"] is True and voci[0]["wiki"] is False

    # accendere la wiki e' un gesto esplicito, e monta la struttura che le serve
    r = client.patch("/api/vaults", json={"path": str(kv), "wiki": True})
    assert r.status_code == 200
    assert r.json()["vault"]["wiki"] is True and r.json()["vault"]["pagine"] >= 2
    assert vault_mod.ha_struttura_wiki(kv)

    # apertura di un nome inesistente -> 400
    assert client.post("/api/vaults/open", json={"nome": "fantasma"}).status_code == 400

    # rimozione dal registro: la cartella resta dov'era
    r = client.post("/api/vaults/remove", json={"nome": "mio"})
    assert r.status_code == 200 and r.json()["rimossi"] == 1
    assert st.settings["vaults"] == []
    assert (kv / "wiki" / "index.md").exists()


def test_registrazione_cartella_mancante_400(client_vault):
    client, _st, td = client_vault
    r = client.post("/api/vaults", json={"path": str(td / "assente"), "nome": "x"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Rotta: scelta nativa della cartella (/api/vaults/pick)
# ---------------------------------------------------------------------------


def test_vault_pick_registra_la_cartella_scelta(client_vault, monkeypatch):
    client, st, td = client_vault
    import server.main as M

    kv = td / "wiki_della_mamma"
    kv.mkdir()
    monkeypatch.setattr(M, "pick_folder", lambda _initial=None: str(kv))

    r = client.post("/api/vaults/pick")
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert corpo["cancelled"] is False
    assert corpo["vault"]["nome"] == "wiki_della_mamma"
    # L'identita' nasce subito, dentro la cartella: e' quella che rende la
    # cartella un vault. La struttura wiki no -- quella si accende a parte.
    assert (kv / vault_mod.VAULT_FILE).is_file()
    assert not (kv / "wiki").exists()
    assert any(v["nome"] == "wiki_della_mamma" for v in st.settings["vaults"])


def test_vault_pick_annullato_non_tocca_il_registro(client_vault, monkeypatch):
    client, st, _td = client_vault
    import server.main as M

    monkeypatch.setattr(M, "pick_folder", lambda _initial=None: None)
    r = client.post("/api/vaults/pick")
    assert r.status_code == 200 and r.json() == {"cancelled": True}
    assert st.settings["vaults"] == []


def test_vault_pick_dedupe_sullo_stesso_percorso(client_vault, monkeypatch):
    client, st, td = client_vault
    import server.main as M

    kv = td / "unico"
    kv.mkdir()
    monkeypatch.setattr(M, "pick_folder", lambda _initial=None: str(kv))
    client.post("/api/vaults/pick")
    client.post("/api/vaults/pick")
    assert len(st.settings["vaults"]) == 1


def test_vault_pick_dialogo_non_disponibile_501(client_vault, monkeypatch):
    client, _st, _td = client_vault
    import server.main as M
    from server.nativedialog import DialogUnavailable

    def _assente(_initial=None):
        raise DialogUnavailable("nessun display")

    monkeypatch.setattr(M, "pick_folder", _assente)
    assert client.post("/api/vaults/pick").status_code == 501


def test_prompt_di_sistema_diventa_manutentore_su_vault(client_vault):
    _client, st, td = client_vault
    from server.main import AppState

    kv = td / "mio_vault"
    vault_mod.scaffold(kv)
    st.settings["workspace_dir"] = str(kv)
    sp = AppState.system_prompt(st)
    assert "manutentore della wiki" in sp
    assert "## Struttura del vault" in sp

    # Il prompt personalizzato dell'utente vince, ma il blocco vault resta.
    st.settings["system_prompt"] = "Sei l'assistente di casa Rossi."
    sp = AppState.system_prompt(st)
    assert "assistente di casa Rossi" in sp
    assert "manutentore della wiki" not in sp
    assert "## Struttura del vault" in sp


def test_lo_stato_della_wiki_non_entra_nel_prompt_di_sistema(client_vault):
    """Il prefisso deve restare byte-identico fra un passo e l'altro.

    ``blocco_manutenzione`` metteva ``wiki/index.md`` -- fino a 6.000 caratteri,
    misurati ~1.670 token -- nel prompt di sistema. L'indice viene riscritto a
    ogni ingest, cioe' nell'operazione per cui la modalita' wiki esiste: il
    prefisso si invalidava li', e non di 1.670 token ma dal token zero, perche'
    quello che segue un prefisso cambiato va tutto ricalcolato.
    """
    from server.main import AppState

    _client, st, td = client_vault
    kv = td / "mio_vault"
    vault_mod.scaffold(kv)
    st.settings["workspace_dir"] = str(kv)

    (kv / "wiki" / "index.md").write_text(
        "# Indice\n- [[Memex]] la macchina di Bush\n", encoding="utf-8"
    )
    prima = AppState.system_prompt(st)

    # un ingest: l'indice cambia
    (kv / "wiki" / "index.md").write_text(
        "# Indice\n- [[Memex]] la macchina di Bush\n- [[Hypertext]] Nelson 1965\n",
        encoding="utf-8",
    )
    vault_mod.appendi_log(kv, "ingest", "nelson-1965.pdf")
    dopo = AppState.system_prompt(st)

    assert prima == dopo, "il prompt di sistema cambia a ogni ingest: prefisso perso"
    assert "Memex" not in prima
    # ...e lo stato c'e' comunque, dove non costa il prefisso
    stato = vault_mod.blocco_stato(kv)
    assert "Memex" in stato and "Hypertext" in stato
    assert "nelson-1965.pdf" in stato


# ---------------------------------------------------------------------------
# L'identita' vive nella cartella, non nelle impostazioni
# ---------------------------------------------------------------------------


def test_una_cartella_qualsiasi_diventa_vault_senza_diventare_una_wiki(tmp_path):
    """Il difetto del vecchio ``ensure_vault``: dava per scontato che vault e
    wiki fossero la stessa cosa, e piantava raw/ e wiki/ in qualunque cartella
    registrata."""
    base = tmp_path / "progetto"
    base.mkdir()
    vault_mod.ensure_vault(base, nome="Progetto")

    assert vault_mod.is_registrato(base)
    assert vault_mod.leggi_config(base).nome == "Progetto"
    assert vault_mod.leggi_config(base).wiki is False
    assert not (base / "wiki").exists() and not (base / "raw").exists()


def test_il_vault_si_porta_dietro_il_nome_se_lo_sposti(tmp_path):
    """L'identita' sta in .vault.json: sopravvive allo spostamento della
    cartella, alla copia su un'altra macchina, alla reinstallazione."""
    prima = tmp_path / "prima"
    prima.mkdir()
    vault_mod.ensure_vault(prima, nome="Ricerca")
    vault_mod.aggiorna_config(prima, descrizione="Paper e appunti")

    dopo = tmp_path / "spostato"
    prima.rename(dopo)
    config = vault_mod.leggi_config(dopo)
    assert config.nome == "Ricerca" and config.descrizione == "Paper e appunti"


def test_un_vault_vecchio_senza_file_continua_a_funzionare(tmp_path):
    """Nessuna migrazione: chi aveva la struttura wiki resta in modalita' wiki."""
    base = tmp_path / "kv"
    vault_mod.scaffold(base)
    assert not vault_mod.is_registrato(base)
    config = vault_mod.leggi_config(base)
    assert config.nome == "kv"
    assert config.wiki is True, "la struttura accende la wiki quando il file non c'e'"
    assert vault_mod.is_modalita_vault(base) is True


def test_i_due_testi_non_sono_lo_stesso_campo(tmp_path):
    """La descrizione e' per chi guarda l'elenco, le istruzioni per il modello.

    Tenerli separati e' una difesa concreta: una riga scritta di fretta per
    ritrovare la cartella non deve diventare un ordine che l'agente segue in
    ogni turno per i mesi successivi.
    """
    base = tmp_path / "v"
    base.mkdir()
    vault_mod.ensure_vault(base)
    config = vault_mod.aggiorna_config(
        base,
        descrizione="Roba di lavoro, la cartella grossa",
        istruzioni="Scrivi sempre in italiano e cita i percorsi.",
    )
    blocco = vault_mod.blocco_istruzioni(config)
    assert "cita i percorsi" in blocco
    assert "la cartella grossa" not in blocco


def test_senza_istruzioni_non_si_aggiunge_niente(tmp_path):
    base = tmp_path / "v"
    base.mkdir()
    vault_mod.ensure_vault(base)
    assert vault_mod.blocco_istruzioni(vault_mod.leggi_config(base)) == ""


def test_le_istruzioni_arrivano_al_modello_anche_senza_wiki(client_vault):
    """E' il senso di aver separato i due campi: un vault che non e' una wiki
    non ha il prompt del manutentore, ma le sue istruzioni contano lo stesso."""
    _client, st, td = client_vault
    from server.main import AppState

    base = td / "progetto"
    base.mkdir()
    vault_mod.ensure_vault(base, nome="Progetto")
    vault_mod.aggiorna_config(base, istruzioni="Non toccare mai la cartella legacy.")
    st.settings["workspace_dir"] = str(base)

    sp = AppState.system_prompt(st)
    assert "Non toccare mai la cartella legacy" in sp
    assert "manutentore della wiki" not in sp


# ---------------------------------------------------------------------------
# Le chat appartengono al vault in cui sono state fatte
# ---------------------------------------------------------------------------


@pytest.fixture()
def archivio_chat(tmp_path, monkeypatch):
    """Tre conversazioni: due in un vault, una fuori."""
    import json

    from core import session as session_mod

    cartella = tmp_path / "sessions"
    cartella.mkdir()
    monkeypatch.setattr(session_mod, "DATA_DIR", cartella)
    session_mod._index_cache.clear()
    session_mod._search_cache.clear()

    kv = tmp_path / "vault_uno"
    kv.mkdir()
    fuori = tmp_path / "altrove"
    fuori.mkdir()

    def scrivi(ident, workspace, giorno):
        (cartella / f"{ident}.json").write_text(
            json.dumps({
                "id": ident,
                "title": f"chat {ident}",
                "updated_at": f"2026-08-{giorno:02d}T10:00:00",
                "workspace_dir": str(workspace),
                "messages": [{"role": "user", "content": "ciao"}],
            }),
            encoding="utf-8",
        )

    scrivi("a", kv, 1)
    scrivi("b", kv, 2)
    scrivi("c", fuori, 3)
    return session_mod, kv, fuori


def test_l_elenco_di_un_vault_sono_le_chat_fatte_li_dentro(archivio_chat):
    session_mod, kv, _fuori = archivio_chat
    ids = [s["id"] for s in session_mod.list_sessions(cartella=str(kv))]
    assert sorted(ids) == ["a", "b"]


def test_l_elenco_generale_non_mostra_le_chat_dei_vault(archivio_chat):
    """La scelta: dentro un vault ci si arriva dal vault. Mescolarli vorrebbe
    dire lo stesso posto in due elenchi diversi."""
    session_mod, kv, _fuori = archivio_chat
    ids = [s["id"] for s in session_mod.list_sessions(escludi=[str(kv)])]
    assert ids == ["c"]


def test_lo_stesso_percorso_scritto_diverso_e_lo_stesso_posto(archivio_chat):
    """Su Windows la stessa cartella si scrive in almeno tre modi. Confrontare
    le stringhe grezze farebbe sparire dal vault le chat fatte proprio li'."""
    session_mod, kv, _fuori = archivio_chat
    # Barra finale e un passaggio inutile: la forma in cui un percorso arriva
    # da un'API o da un incolla, non quella in cui e' stato salvato.
    storto = str(kv / "." ) + os.sep
    ids = [s["id"] for s in session_mod.list_sessions(cartella=storto)]
    assert sorted(ids) == ["a", "b"]
    assert session_mod.chiave_cartella(storto) == session_mod.chiave_cartella(str(kv))


# ---------------------------------------------------------------------------
# Le rotte dell'identita'
# ---------------------------------------------------------------------------


def test_patch_scrive_nella_cartella_e_riallinea_il_registro(client_vault):
    client, st, td = client_vault
    base = td / "vv"
    base.mkdir()
    client.post("/api/vaults", json={"path": str(base), "nome": "prima"})

    r = client.patch("/api/vaults", json={
        "path": str(base), "nome": "dopo", "descrizione": "d", "istruzioni": "i",
    })
    assert r.status_code == 200
    voce = r.json()["vault"]
    assert (voce["nome"], voce["descrizione"], voce["istruzioni"]) == ("dopo", "d", "i")
    # ...e la verita' e' nel file, non nelle impostazioni
    assert vault_mod.leggi_config(base).nome == "dopo"
    assert st.settings["vaults"][0]["nome"] == "dopo"


def test_la_home_del_vault_arriva_in_una_chiamata_sola(client_vault, monkeypatch):
    client, _st, td = client_vault
    import server.main as M

    base = td / "vv"
    base.mkdir()
    client.post("/api/vaults", json={"path": str(base), "nome": "Casa"})
    monkeypatch.setattr(M, "session_list", lambda **_kw: [{"id": "x", "title": "una chat"}])

    r = client.get("/api/vaults/home", params={"path": str(base)})
    assert r.status_code == 200
    corpo = r.json()
    assert corpo["vault"]["nome"] == "Casa"
    assert corpo["sessions"][0]["id"] == "x"


def test_la_home_di_una_cartella_che_non_c_e_da_404(client_vault):
    client, _st, td = client_vault
    r = client.get("/api/vaults/home", params={"path": str(td / "fantasma")})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# L'interfaccia: la schermata iniziale e la scheda d'identita'
# ---------------------------------------------------------------------------

WEB = Path(__file__).resolve().parents[1] / "web"


def _html() -> str:
    return (WEB / "index.html").read_text(encoding="utf-8")


def _js() -> str:
    return (WEB / "app.js").read_text(encoding="utf-8")


def test_la_schermata_del_vault_prende_il_posto_della_chat():
    """Due viste dello stesso spazio, non due cose da guardare insieme."""
    html = _html()
    split = html[html.index('id="main-split"'): html.index('</main>')]
    assert 'id="vault-col"' in split and 'id="chat-col"' in split
    assert split.index('id="vault-col"') < split.index('id="chat-col"')

    corpo = _js()[_js().index("function mostraVaultHome("):]
    corpo = corpo[: corpo.index("\n}")]
    assert "casa.hidden = !attiva" in corpo and "chat.hidden = !!attiva" in corpo


def test_la_schermata_ha_le_tre_cose_dei_progetti():
    """Nome e descrizione, una casella per cominciare, lo storico. In
    quest'ordine: nove volte su dieci un vault si apre per scriverci."""
    html = _html()
    casa = html[html.index('id="vault-col"'): html.index('id="chat-col"')]
    assert 'id="vh-nome"' in casa and 'id="vh-descr"' in casa
    assert 'id="vh-input"' in casa and 'id="vh-send"' in casa
    assert 'id="vh-sessions"' in casa
    assert casa.index('id="vh-input"') < casa.index('id="vh-sessions"')


def test_la_chat_nuova_del_vault_non_duplica_l_invio():
    """Un secondo invio scritto qui sarebbe un secondo posto in cui ricordarsi
    degli allegati, della goccia del web e del livello di pensiero."""
    js = _js()
    corpo = js[js.index("async function nuovaChatNelVault("):]
    corpo = corpo[: corpo.index("\n}")]
    assert "await newSession()" in corpo
    assert "await send()" in corpo
    assert "/api/chat" not in corpo


def test_la_scheda_del_vault_tiene_separati_i_due_testi():
    html = _html()
    card = html[html.index('id="vault-card"'): html.index('id="plan-card"')]
    assert 'id="v-descr"' in card and 'id="v-istr"' in card
    # ...e lo dice, perche' la differenza non si vede da due caselle uguali
    assert "solo per te" in card and "vanno al modello" in card
    assert 'id="v-wiki"' in card and 'id="v-path"' in card


def test_i_campi_del_vault_si_salvano_uscendo_non_a_ogni_tasto():
    js = _js()
    corpo = js[js.index("function bindVaultUI("):]
    corpo = corpo[: corpo.index("\n}\n")]
    assert "node.onblur" in corpo
    assert "oninput" not in corpo


def test_uscire_da_un_vault_costa_quanto_entrarci():
    html = _html()
    assert 'id="vault-exit"' in html
    js = _js()
    assert "esci.onclick = uscireDalVault" in js


def test_l_elenco_libero_non_viene_coperto_dalle_chat_del_vault():
    """I due elenchi convivono: aprire un vault non fa sparire le recenti.

    Prima l'elenco cambiava sotto i piedi -- dentro un vault diventava quello
    del vault -- e l'unico modo di accorgersene era non ritrovarci una
    conversazione. Ora le chat del vault stanno **annidate sotto di lui**, e
    ``renderSessions`` disegna sempre e solo le libere.
    """
    js = _js()
    corpo = js[js.index("function renderSessions("):]
    corpo = corpo[: corpo.index("\n}")]
    # Nessuna scelta fra due elenchi: l'unica sorgente e' ``state.sessions``
    # (o i risultati della ricerca).
    assert "state.vault ? state.vault.nome" not in corpo
    assert "state.vaultChat" not in corpo
    # Il ramo esiste, ed e' l'altro posto -- quello sotto il vault.
    assert "function ramoVault(" in js
    assert "state.vaultChat[v.path]" in js


def test_le_chat_del_vault_si_aprono_e_si_chiudono_dal_ramo():
    """La freccia guarda dentro, il nome ci entra: due gesti, due bersagli."""
    js = _js()
    assert "function alternaVault(" in js
    assert "state.vaultAperti" in js
    corpo = js[js.index("function renderVaults("):]
    corpo = corpo[: corpo.index("\n}")]
    assert "vault-twisty" in corpo and "alternaVault(v.path)" in corpo


def test_la_scheda_del_vault_si_vede_solo_nella_sua_home():
    """Descrizione e istruzioni sono l'identita' del posto, non della chat.

    Dentro una conversazione il pannello di destra deve parlare di quella
    conversazione: due campi di testo lunghi sopra il piano e le note erano il
    modo piu' rapido di non vedere piu' ne' il piano ne' le note.
    """
    js = _js()
    corpo = js[js.index("function renderVaultCard("):]
    corpo = corpo[: corpo.index("\n}")]
    assert "state.vaultHome" in corpo
    # La memoria del vault ha la regola opposta e resta ovunque.
    assert "renderVaultMemory();" in corpo


def test_la_home_del_vault_prende_il_posto_della_chat_non_le_si_affianca():
    """``display: flex`` batte il ``display: none`` di [hidden]: senza la
    riga esplicita la colonna della chat non si nascondeva, e la schermata
    iniziale del vault si affiancava a meta' schermo."""
    css = (WEB / "style.css").read_text(encoding="utf-8")
    assert "#chat-col[hidden] { display: none; }" in css


# ---------------------------------------------------------------------------
# La memoria del vault: il terzo foglio, e la differenza e' la durata
# ---------------------------------------------------------------------------


def test_i_tre_fogli_hanno_tre_durate(tmp_path):
    """Note di chat: muoiono col compito. Memoria del vault: vale per tutte le
    chat di questa cartella. Memorie a lungo termine: valgono ovunque."""
    base = tmp_path / "v"
    base.mkdir()
    vault_mod.ensure_vault(base)
    vault_mod.aggiungi_nota(base, "Le fonti stanno in raw/, mai toccarle")
    vault_mod.aggiungi_nota(base, "I test si lanciano da /work")

    # La memoria e' del posto: chiudere una chat non la tocca, e rileggerla e'
    # rileggere la cartella.
    assert list(vault_mod.leggi_config(base).note) == [
        "Le fonti stanno in raw/, mai toccarle",
        "I test si lanciano da /work",
    ]


def test_la_stessa_nota_non_si_accumula(tmp_path):
    base = tmp_path / "v"
    base.mkdir()
    vault_mod.ensure_vault(base)
    vault_mod.aggiungi_nota(base, "una cosa")
    config = vault_mod.aggiungi_nota(base, "una   cosa")   # spazi diversi
    assert list(config.note) == ["una cosa"]


def test_il_foglio_pieno_chiede_di_fare_spazio(tmp_path):
    base = tmp_path / "v"
    base.mkdir()
    vault_mod.ensure_vault(base)
    for i in range(vault_mod.MAX_NOTE_VAULT):
        vault_mod.aggiungi_nota(base, f"nota numero {i}")
    with pytest.raises(vault_mod.NotaVaultError):
        vault_mod.aggiungi_nota(base, "una di troppo")


def test_una_nota_si_toglie_anche_citandola_a_memoria(tmp_path):
    """Un modello che cita una nota la tronca: rifiutare per una virgola
    mancante costa un giro per niente."""
    base = tmp_path / "v"
    base.mkdir()
    vault_mod.ensure_vault(base)
    vault_mod.aggiungi_nota(base, "Il parser del CSV vuole il separatore esplicito")
    config = vault_mod.togli_nota(base, "Il parser del CSV vuole")
    assert config.note == ()


def test_la_memoria_del_vault_sopravvive_al_cambio_di_nome(tmp_path):
    """Sta nello stesso file dell'identita': rinominare non deve svuotarla."""
    base = tmp_path / "v"
    base.mkdir()
    vault_mod.ensure_vault(base, nome="prima")
    vault_mod.aggiungi_nota(base, "un fatto che resta")
    vault_mod.aggiorna_config(base, nome="dopo", descrizione="d")
    config = vault_mod.leggi_config(base)
    assert config.nome == "dopo" and list(config.note) == ["un fatto che resta"]


def test_il_blocco_dice_al_modello_che_e_di_un_altro_tempo(tmp_path):
    base = tmp_path / "v"
    base.mkdir()
    vault_mod.ensure_vault(base, nome="Ricerca")
    config = vault_mod.aggiungi_nota(base, "Le fonti stanno in raw/")
    blocco = vault_mod.blocco_note(config)
    assert "Le fonti stanno in raw/" in blocco
    assert "Ricerca" in blocco
    # La differenza col foglio della chat va detta, o i due diventano lo stesso
    assert "muore con lei" in blocco
    # Vuoto = niente blocco: non si paga un titolo per un elenco che non c'e'.
    assert vault_mod.blocco_note(vault_mod.VaultConfig(nome="x")) == ""


def test_il_tool_scrive_nel_vault_solo_se_c_e_un_vault(tmp_path):
    import json

    from core.tools import NOTES_TOOL, ToolContext, dispatch

    base = tmp_path / "v"
    base.mkdir()
    vault_mod.ensure_vault(base)

    # Fuori da un vault: errore pulito, e la nota NON finisce nel foglio della
    # chat di nascosto -- un ripiego silenzioso sarebbe la cosa peggiore.
    fuori = ToolContext(workspace=str(tmp_path), sandbox="host")
    esito = json.loads(dispatch(fuori, NOTES_TOOL, {
        "action": "add", "text": "x", "ambito": "vault",
    }))
    assert "error" in esito
    assert len(fuori.notes) == 0

    dentro = ToolContext(workspace=str(base), sandbox="host", vault_dir=str(base))
    esito = json.loads(dispatch(dentro, NOTES_TOOL, {
        "action": "add", "text": "I test si lanciano da /work", "ambito": "vault",
    }))
    assert esito["status"] == "ok" and esito["ambito"] == "vault"
    # ...su disco, non solo in memoria
    assert list(vault_mod.leggi_config(base).note) == ["I test si lanciano da /work"]
    # ...e il foglio della chat resta separato
    assert len(dentro.notes) == 0


def test_la_memoria_del_vault_non_si_svuota_in_blocco(tmp_path):
    """Il foglio di una chat si butta perche' muore con lei comunque. Questa
    e' il lavoro di mesi, e una clear per sbaglio non ha un annulla."""
    import json

    from core.tools import NOTES_TOOL, ToolContext, dispatch

    base = tmp_path / "v"
    base.mkdir()
    vault_mod.ensure_vault(base)
    vault_mod.aggiungi_nota(base, "da tenere")
    ctx = ToolContext(workspace=str(base), sandbox="host", vault_dir=str(base))
    esito = json.loads(dispatch(ctx, NOTES_TOOL, {"action": "clear", "ambito": "vault"}))
    assert "error" in esito
    assert list(vault_mod.leggi_config(base).note) == ["da tenere"]


def test_il_blocco_del_vault_sta_in_coda_e_prima_di_quello_della_chat():
    """Dal piu' vecchio e stabile al piu' fresco. E in coda, sempre: il
    prefisso deve restare byte-identico o il KV cache si ricalcola."""
    from core.agent import build_api_messages

    api = build_api_messages(
        [{"role": "user", "content": "ciao"}],
        system_prompt="SYS",
        env_header="ENV",
        vault_notes_block="<memoria_del_vault>",
        notes_block="<note_di_lavoro>",
        plan_block="<piano>",
    )
    coda = api[-1]
    assert coda["role"] == "user"
    assert coda["content"].index("<memoria_del_vault>") < coda["content"].index("<note_di_lavoro>")
    assert "memoria_del_vault" not in api[0]["content"]


def test_la_scheda_della_memoria_si_legge_e_si_toglie():
    """Scrive il modello; dall'interfaccia si puo' solo togliere. Un campo
    libero sarebbe un secondo posto da cui la memoria puo' divergere."""
    html = _html()
    assert 'id="vault-mem-card"' in html and 'id="v-mem"' in html
    js = _js()
    corpo = js[js.index("function renderVaultMemory("):]
    corpo = corpo[: corpo.index("\n}")]
    assert "vmem-del" in corpo
    assert "contenteditable" not in corpo


def test_anche_l_anteprima_sparisce_nella_home_del_vault():
    """L'anteprima e' di una chat, come il thread: e' la stessa colonna.

    Era rimasta l'unico pezzo di una conversazione a sopravvivere all'ingresso
    in un vault, affiancata a una schermata con cui non c'entrava niente --
    con il piano, le note e i numeri dell'ultima esecuzione gia' spariti.
    """
    js = _js()
    corpo = js[js.index("function mostraVaultHome("):]
    corpo = corpo[: corpo.index("\n}")]
    assert "sincronizzaAnteprima();" in corpo

    # Nascosta, non chiusa: un'applicazione avviata dall'agente vive dentro
    # quell'iframe, e passare dalla home non deve fermarla.
    assert "function sincronizzaAnteprima(" in js
    sincro = js[js.index("function sincronizzaAnteprima("):]
    sincro = sincro[: sincro.index("\n}")]
    assert "state.previewAperta" in sincro and "state.vaultHome" in sincro
    assert "innerHTML" not in sincro

    # E chiudere davvero deve funzionare anche da nascosta, o l'iframe di una
    # chat chiusa resterebbe vivo.
    chiudi = js[js.index("function closePreview("):]
    chiudi = chiudi[: chiudi.index("\n}")]
    assert "!state.previewAperta" in chiudi
    assert "pane.hidden" not in chiudi.split("if (!pane")[1].split(")")[0]


# ---------------------------------------------------------------------------
# Il ritorno che mentiva
# ---------------------------------------------------------------------------


def test_una_scrittura_fallita_non_passa_per_riuscita(tmp_path, monkeypatch):
    """``scrivi_config`` ritornava la configurazione nuova comunque.

    Chi chiama e' ``aggiungi_nota``, che risponde al modello con l'elenco delle
    note aggiornato: il modello leggeva "registrata", ci costruiva sopra il
    resto del turno, e la nota non esisteva. Una frase che descrive un
    comportamento dell'harness deve essere vera, e qui la frase e' un valore di
    ritorno.
    """
    import os as _os

    base = tmp_path / "kv"
    base.mkdir()
    vault_mod.ensure_vault(base, nome="prova")
    vault_mod.aggiungi_nota(base, "questa c'e' davvero")

    def _replace_rotto(_a, _b):
        raise OSError("disco pieno")

    monkeypatch.setattr(_os, "replace", _replace_rotto)
    with pytest.raises(vault_mod.VaultScritturaError):
        vault_mod.aggiungi_nota(base, "questa non arriva sul disco")
    monkeypatch.undo()

    # sul disco c'e' ancora solo la prima, e nessun .tmp abbandonato
    assert list(vault_mod.leggi_config(base).note) == ["questa c'e' davvero"]
    assert not (base / ".vault.json.tmp").exists()


def test_togliere_una_nota_ambigua_dice_che_e_ambigua(tmp_path):
    """"Nessuna nota corrisponde" era falso: ne corrispondevano troppe.

    Il modello leggeva "nessuna", cambiava testo invece di essere piu' preciso,
    e girava a vuoto.
    """
    base = tmp_path / "kv"
    base.mkdir()
    vault_mod.ensure_vault(base)
    vault_mod.aggiungi_nota(base, "la cartella delle prove sta in tmp")
    vault_mod.aggiungi_nota(base, "la cartella dei log sta in var")

    with pytest.raises(vault_mod.NotaVaultError) as errore:
        vault_mod.togli_nota(base, "la cartella")
    messaggio = str(errore.value)
    assert "2 note" in messaggio
    assert "Nessuna nota corrisponde" not in messaggio

    # e una davvero assente continua a dire che e' assente
    with pytest.raises(vault_mod.NotaVaultError) as assente:
        vault_mod.togli_nota(base, "il registro delle spedizioni")
    assert "Nessuna nota corrisponde" in str(assente.value)


def test_un_indice_su_una_riga_sola_non_sparisce(tmp_path):
    """``rfind`` a -1 piu' uno faceva ``taglio[:0]``: restava la sola dicitura."""
    base = tmp_path / "kv"
    vault_mod.scaffold(base)
    (base / "wiki" / "index.md").write_text("A" * 200, encoding="utf-8")
    testo = vault_mod.leggi_indice(base, max_chars=50)
    assert "A" * 50 in testo
    assert "[indice troncato]" in testo


def test_le_fonti_di_un_vault_non_sono_solo_i_markdown(tmp_path):
    """In ``raw/`` stanno paper e PDF: contando i soli .md si vedeva "0 fonti"."""
    base = tmp_path / "kv"
    vault_mod.scaffold(base)
    (base / "raw" / "bush-1945.pdf").write_bytes(b"%PDF")
    (base / "raw" / "intervista.txt").write_text("trascrizione", encoding="utf-8")
    (base / "raw" / "assets" / "figura.png").write_bytes(b"x")   # corredo, non fonte
    assert vault_mod.info_vault(str(base)).fonti == 2


def test_il_log_non_fa_fallire_un_ingest_riuscito(tmp_path):
    """Annotare e' un di piu': se non si puo', non si butta via l'operazione."""
    nudo = tmp_path / "senza_wiki"
    nudo.mkdir()
    assert vault_mod.appendi_log(nudo, "ingest", "x.pdf") is False   # non solleva

    base = tmp_path / "kv"
    vault_mod.scaffold(base)
    assert vault_mod.appendi_log(base, "ingest", "bush-1945.pdf") is True
    assert "bush-1945.pdf" in vault_mod.leggi_log_coda(base)


def test_wiki_esplicitamente_nullo_ricade_sulla_struttura(tmp_path):
    """``get(chiave, ripiego)`` non scatta su un null: la chiave c'e'."""
    import json as _json

    base = tmp_path / "kv"
    vault_mod.scaffold(base)
    (base / ".vault.json").write_text(
        _json.dumps({"nome": "kv", "wiki": None}), encoding="utf-8"
    )
    assert vault_mod.leggi_config(base).wiki is True


def test_lo_stato_della_wiki_arriva_al_modello_in_coda(tmp_path, fake_ollama):
    """Uscito dal prompt di sistema, deve comunque arrivare -- e in coda.

    E deve arrivare anche ai vault nati **prima** di ``.vault.json``, che la
    modalita' wiki riconosce dalla struttura: quelli non hanno ``vault_dir``, e
    leggerlo da li' li avrebbe lasciati senza indice.
    """
    from core import agent as agent_mod
    from core.backend import OllamaBackend
    from core.config import GenParams
    from core.tools import TOOLS_SCHEMA, ToolContext

    url, _ = fake_ollama
    base = tmp_path / "wiki_vecchia"
    vault_mod.scaffold(base)                      # raw/ + wiki/, niente .vault.json
    (base / ".vault.json").unlink(missing_ok=True)
    assert not vault_mod.is_registrato(base)
    assert vault_mod.is_modalita_vault(base)      # riconosciuta dalla struttura
    (base / "wiki" / "index.md").write_text(
        "# Indice\n- [[Memex]] la macchina di Bush\n", encoding="utf-8"
    )

    import tests.test_agent_loop as fake

    fake._Handler.calls.clear()
    ctx = ToolContext(workspace=str(base), sandbox="host")
    messaggi = [{"role": "user", "content": "che dice la wiki del memex?"}]
    list(
        agent_mod.run_turn(
            backend=OllamaBackend(url, timeout_s=20),
            params=GenParams(model="fake:latest"),
            tools_schema=TOOLS_SCHEMA,
            tool_ctx=ctx,
            ui_messages=messaggi,
            system_prompt="SYS",
            env_header=None,
            max_steps=1,
        )
    )
    # La **prima** chiamata: l'ultima e' il riepilogo forzato di fine passi,
    # che costruisce un array di messaggi tutto suo.
    prima = fake._Handler.calls[0]
    coda = [m for m in prima["messages"] if m.get("role") == "user"][-1]["content"]
    assert "stato_del_vault" in coda
    assert "Memex" in coda
    # ...e non in testa: il prefisso deve restare byte-identico fra i passi
    sistema = " ".join(m["content"] for m in prima["messages"] if m.get("role") == "system")
    assert "Memex" not in sistema
