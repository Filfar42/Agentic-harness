"""Isolamento comune a tutta la suite.

Le preferenze vere della macchina non c'entrano niente con i test, e finche'
nessuno le ha spostate la suite le usava per davvero: le **leggeva** all'avvio
di ogni ``AppState`` -- quindi l'esito dipendeva da come l'utente aveva
lasciato l'interfaccia -- e le **riscriveva** ad ogni rotta che chiama
``STATE.persist()``. Osservato: con la sandbox lasciata su 'host' cinque test
di prontezza fallivano senza che fosse rotto niente, e un giro di ``pytest``
riempiva l'elenco delle cartelle recenti di percorsi temporanei.

Sta qui e non nelle cinque fixture che creano un client perche' e' una regola
sola: vale anche per il prossimo file di test, che nessuno si ricordera' di
attrezzare.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import memory as memory_mod  # noqa: E402
from core import settings as settings_mod  # noqa: E402


@pytest.fixture(autouse=True)
def preferenze_isolate(tmp_path, monkeypatch):
    """Ogni test scrive le sue preferenze in una cartella temporanea."""
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", tmp_path / "impostazioni.json")
    # Stessa regola per la memoria a lungo termine: senza questo i test di
    # /api/memories leggevano e riscrivevano il file REALE del workspace.
    monkeypatch.setattr(memory_mod, "MEMORY_FILE", tmp_path / "memorie.json")
    # E per i marchi "c'e' un'app viva nella sandbox": vivono fuori dal
    # workspace (il container li cancellerebbe) e finivano nella home vera,
    # dove restavano. Non davano rossi solo perche' ogni test usa una
    # tmp_path diversa, quindi un'impronta diversa: bastava un test che
    # riusasse un percorso per portarsi dietro lo stato del run precedente.
    monkeypatch.setenv("HARNESS_BG_LIVE", str(tmp_path / "sandbox-bg"))
