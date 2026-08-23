"""Vault LLM Wiki: struttura, registro e il tool vault_search.

Tre contratti:
- ``core/vault``: scaffold idempotente della struttura LLM Wiki (raw/ immutabile,
  wiki/ dell'agente, schema CLAUDE.md), indice e coda del log;
- ``core/vault_search``: la sotto-chiamata in sola lettura al manutentore --
  senza far girare un modello, ``run_turn`` arriva iniettabile come per la
  delega -- e i suoi errori puliti;
- le rotte ``/api/vaults*`` del server: registro, apertura, rimozione.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

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

    # apertura: crea la struttura e diventa il workspace corrente
    r = client.post("/api/vaults/open", json={"nome": "mio"})
    assert r.status_code == 200
    assert st.settings["workspace_dir"] == str(kv.resolve())
    assert vault_mod.is_vault(kv)

    # l'elenco mostra il vault attivo con i contatori
    voci = client.get("/api/vaults").json()["vaults"]
    assert len(voci) == 1
    assert voci[0]["attivo"] is True and voci[0]["pagine"] >= 2

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
    # la struttura LLM Wiki nasce subito, non alla prima apertura
    assert (kv / "wiki" / "index.md").is_file()
    assert (kv / "raw").is_dir()
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
    client, st, td = client_vault
    from server.main import AppState

    kv = td / "mio_vault"
    vault_mod.scaffold(kv)
    st.settings["workspace_dir"] = str(kv)
    sp = AppState.system_prompt(st)
    assert "manutentore della wiki" in sp
    assert "## Stato del vault" in sp

    # Il prompt personalizzato dell'utente vince, ma il blocco vault resta.
    st.settings["system_prompt"] = "Sei l'assistente di casa Rossi."
    sp = AppState.system_prompt(st)
    assert "assistente di casa Rossi" in sp
    assert "manutentore della wiki" not in sp
    assert "## Stato del vault" in sp
