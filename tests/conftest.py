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

from core import settings as settings_mod  # noqa: E402


@pytest.fixture(autouse=True)
def preferenze_isolate(tmp_path, monkeypatch):
    """Ogni test scrive le sue preferenze in una cartella temporanea."""
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", tmp_path / "impostazioni.json")
