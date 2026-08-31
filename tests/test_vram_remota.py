"""Test del rilevamento hardware quando Ollama gira su un'altra macchina.

Il contesto: l'harness sta su una macchina, il modello su un'altra della stessa
rete. Tutto quello che l'harness misurava con ``nvidia-smi`` -- VRAM libera,
occupazione, e di conseguenza il contesto consigliato -- stava misurando la
macchina sbagliata. Il sintomo non era un errore ma un consiglio: "abbassa il
contesto a 8192", su una scheda che ne reggeva 65536.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tests.test_agent_loop as fake
from core import agent as agent_mod
from core import config as config_mod
from core.backend import OllamaBackend
from core.config import GenParams
from core.tools import TOOLS_SCHEMA, ToolContext

fake_ollama = fake.fake_ollama


@pytest.fixture()
def client(fake_ollama, tmp_path, monkeypatch):
    url, _ = fake_ollama
    monkeypatch.setattr(config_mod, "DATA_DIR", tmp_path / "sessions")
    monkeypatch.setattr(config_mod, "MEMORY_FILE", tmp_path / "memories.json")

    from core import memory as memory_mod
    from core import session as session_mod

    monkeypatch.setattr(session_mod, "DATA_DIR", tmp_path / "sessions")
    monkeypatch.setattr(memory_mod, "MEMORY_FILE", tmp_path / "memories.json")

    from server import main as server_main
    from server.runner import RunnerRegistry

    workspace = tmp_path / "ws"
    workspace.mkdir()

    server_main.RUNNERS = RunnerRegistry()
    server_main.STATE = server_main.AppState()
    server_main.STATE.settings.update(
        {
            "api_base": url,
            "transport": "ollama",
            "model_name": "fake:latest",
            "workspace_dir": str(workspace),
            "timeout_seconds": 20,
            # Niente avvio automatico di Docker nei test: e' un effetto
            # collaterale sulla macchina che li esegue, non un comportamento
            # sotto esame qui.
            "docker_autostart": False,
            "num_ctx": 32768,
        }
    )
    with TestClient(server_main.app) as test_client:
        test_client.server = server_main
        yield test_client


# ---------------------------------------------------------------------------
# Locale o remoto
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["http://localhost:11434", "http://127.0.0.1:11434", "http://[::1]:11434", ""],
)
def test_endpoint_locale_riconosciuto(url):
    from server.main import endpoint_is_local

    assert endpoint_is_local(url) is True


@pytest.mark.parametrize(
    "url",
    ["http://192.168.1.50:11434", "http://ollama-box:11434", "https://gpu.lan/v1"],
)
def test_endpoint_remoto_riconosciuto(url):
    """Il caso che rende sbagliato leggere la GPU locale.

    Con l'endpoint remoto ``nvidia-smi`` non e' "non disponibile": e' la
    scheda di un'altra macchina, e leggerla darebbe un numero credibile e
    falso -- il modo peggiore di sbagliare.
    """
    from server.main import endpoint_is_local

    assert endpoint_is_local(url) is False


# ---------------------------------------------------------------------------
# Profilo consigliato
# ---------------------------------------------------------------------------


def test_senza_vram_nota_il_profilo_conferma_il_contesto_attuale(client, monkeypatch):
    """Il bug vero: "applica i consigliati" dimezzava il contesto senza misurare.

    Con la VRAM sconosciuta il minimo di sicurezza (8192) non e' un consiglio,
    e' una modifica travestita da consiglio. Meglio confermare quello che c'e'.
    """
    monkeypatch.setattr(
        client.server, "_vram_snapshot",
        lambda: {"source": "ollama /api/ps", "remote": True, "host": "gpu.lan",
                 "free": None, "total": None, "used": None},
    )
    info = client.get("/api/profile").json()
    assert info["free_vram_mb"] is None
    assert info["values"]["num_ctx"] == 32768        # non 8192
    assert info["vram"]["remote"] is True


def test_con_la_vram_dichiarata_il_contesto_viene_calcolato(client, monkeypatch):
    monkeypatch.setattr(
        client.server, "_vram_snapshot",
        lambda: {"source": "ollama /api/ps", "remote": True, "host": "gpu.lan",
                 "total": 32768, "used": 22528, "free": 10240},
    )
    info = client.get("/api/profile").json()
    assert info["free_vram_mb"] == 10240
    assert info["values"]["num_ctx"] > 8192


def test_il_budget_di_generazione_non_supera_meta_finestra(client, monkeypatch):
    """num_predict e num_ctx condividono la stessa finestra."""
    monkeypatch.setattr(
        client.server, "_vram_snapshot",
        lambda: {"remote": True, "host": "gpu.lan", "total": 32768,
                 "used": 22528, "free": 10240},
    )
    values = client.get("/api/profile").json()["values"]
    assert values["max_tokens"] <= values["num_ctx"] // 2


# ---------------------------------------------------------------------------
# /api/ps come sorgente dell'occupazione
# ---------------------------------------------------------------------------


def test_i_modelli_caricati_arrivano_da_api_ps(client):
    """E' l'unico dato sull'hardware che un endpoint remoto sappia dare."""
    backend = client.server.STATE.backend()
    caricati = backend.loaded_models()
    assert isinstance(caricati, list)


def test_un_server_irraggiungibile_non_fa_esplodere_il_pannello():
    """Diagnostica: un server spento non deve impedire di aprire le impostazioni."""
    backend = OllamaBackend("http://127.0.0.1:1", timeout_s=1)
    assert backend.loaded_models() == []


def test_gpu_remota_senza_vram_dichiarata_spiega_cosa_fare(client, monkeypatch):
    """"Non disponibile" non basta: qui la causa e' nota e la soluzione anche."""
    monkeypatch.setattr(
        client.server, "_vram_snapshot",
        lambda: {"remote": True, "host": "gpu.lan", "free": None,
                 "total": None, "used": None},
    )
    data = client.get("/api/gpu").json()
    assert data["available"] is False
    assert data["remote"] is True
    assert "dichiara la VRAM" in data["detail"]


# ---------------------------------------------------------------------------
# La tendina dei modelli dopo un cambio di endpoint
# ---------------------------------------------------------------------------


def test_i_modelli_si_possono_rileggere_senza_ricaricare_la_pagina(client):
    """Il sintomo: cambiato endpoint, il modello nuovo "non compariva".

    L'elenco arrivava solo da /api/bootstrap, cioe' una volta sola al
    caricamento della pagina, e restava quello della macchina precedente.
    """
    data = client.get("/api/models").json()
    assert data["online"] is True
    assert "fake:latest" in data["models"]
    assert data["url"]


# ---------------------------------------------------------------------------
# Contatori dei nudge
# ---------------------------------------------------------------------------


def test_i_solleciti_finiscono_nelle_statistiche_del_turno(fake_ollama, tmp_path, monkeypatch):
    """Non per curiosita': per poterli togliere sui dati invece che a intuito.

    Le reti di sicurezza costano un round-trip quando scattano e zero quando
    non scattano. La domanda utile non e' "servono in teoria" ma "questo
    modello le fa scattare", e senza contarle non ha risposta.
    """
    url, _ = fake_ollama
    monkeypatch.setattr(
        fake, "SCRIPT", [[{"message": {"content": "Potresti creare tu config.py."}}]]
    )

    eventi = list(
        agent_mod.run_turn(
            backend=OllamaBackend(url, timeout_s=20),
            params=GenParams(model="fake:latest"),
            tools_schema=TOOLS_SCHEMA,
            tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
            ui_messages=[{"role": "user", "content": "crea il file config.py"}],
            system_prompt="SYS",
            env_header=None,
            max_steps=2,
        )
    )

    finito = [e for e in eventi if isinstance(e, agent_mod.TurnFinished)][-1]
    assert finito.usage["nudges"]["tool"] == 1


def test_un_turno_pulito_non_riporta_solleciti(fake_ollama, tmp_path):
    """Se non scatta niente la chiave non c'e': la UI non deve mostrare "0"."""
    url, _ = fake_ollama
    eventi = list(
        agent_mod.run_turn(
            backend=OllamaBackend(url, timeout_s=20),
            params=GenParams(model="fake:latest", num_ctx=8192),
            tools_schema=TOOLS_SCHEMA,
            tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
            ui_messages=[{"role": "user", "content": "cosa c'e' nel progetto?"}],
            system_prompt="SYS",
            env_header="ENV",
            max_steps=4,
        )
    )
    finito = [e for e in eventi if isinstance(e, agent_mod.TurnFinished)][-1]
    assert "nudges" not in finito.usage
