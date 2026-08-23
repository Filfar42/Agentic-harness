"""Test di run.py: l'upstream del ponte segue la porta del comando.

Il difetto che ha motivato questo modulo: con ``run.py --port 8201 --mobile``
il ponte interno guardava la 8123 di default e trovava il principale spento
(health ``ok:false`` visto in anteprima). La funzione sotto test e'
``upstream_mobile``, importata direttamente dallo script di avvio.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import patch

RADICE = Path(__file__).resolve().parent.parent


def _run():
    """Importa run.py come modulo, senza eseguirlo (non ha side-effect top-level)."""
    spec = importlib.util.spec_from_file_location("run_harness", RADICE / "run.py")
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)  # type: ignore[union-attr]
    return modulo


def test_upstream_mobile_di_default_segue_la_porta_del_comando(monkeypatch):
    monkeypatch.delenv("HARNESS_UPSTREAM", raising=False)
    assert _run().upstream_mobile(8123) == "http://127.0.0.1:8123"
    # Il caso che ha rotto l'anteprima: porta atipica -> upstream atipico.
    assert _run().upstream_mobile(8201) == "http://127.0.0.1:8201"


def test_harness_upstream_esplicito_vince_sulla_porta(monkeypatch):
    monkeypatch.setenv("HARNESS_UPSTREAM", "http://192.168.1.50:9000")
    assert _run().upstream_mobile(8201) == "http://192.168.1.50:9000"


def test_main_imposta_harness_upstream_per_il_thread_del_ponte():
    """main() deve fissare HARNESS_UPSTREAM PRIMA di aprire il thread del ponte.

    Lo verifichiamo intercettando uvicorn.run: se main() non scrivesse la
    variabile, il ponte leggerebbe il default sbagliato.
    """
    run_harness = _run()
    avvii: list[tuple[str, int]] = []
    ambiente: dict[str, str | None] = {}

    def finto_uvicorn_run(app: str, host: str, port: int, **_):
        avvii.append((app, port))
        if app == "server.mobile:app":
            ambiente["al_avvio_del_ponte"] = os.environ.get("HARNESS_UPSTREAM")

    argv_prima = sys.argv
    sys.argv = ["run.py", "--port", "8203", "--mobile", "--mobile-port", "8202", "--no-browser"]
    with (
        patch.object(run_harness.uvicorn, "run", side_effect=finto_uvicorn_run),
        patch.object(run_harness.webbrowser, "open"),
        patch.dict(os.environ, {}, clear=False),
    ):
        os.environ.pop("HARNESS_UPSTREAM", None)
        try:
            run_harness.main()
        finally:
            sys.argv = argv_prima

    assert ("server.main:app", 8203) in avvii
    assert ("server.mobile:app", 8202) in avvii
    # Valore atteso esplicito: quando il thread del ponte si e' avviato,
    # l'ambiente doveva gia' puntare alla porta DEL COMANDO.
    assert ambiente["al_avvio_del_ponte"] == "http://127.0.0.1:8203"


def test_main_non_sovrascrive_un_harness_upstream_gia_presente():
    """Se l'utente ha scelto l'upstream a mano, main() non lo tocca."""
    run_harness = _run()
    avvii: list[str] = []

    def finto_uvicorn_run(app: str, **_):
        avvii.append(app)

    argv_prima = sys.argv
    sys.argv = ["run.py", "--port", "8123", "--mobile", "--no-browser"]
    with (
        patch.object(run_harness.uvicorn, "run", side_effect=finto_uvicorn_run),
        patch.object(run_harness.webbrowser, "open"),
        patch.dict(os.environ, {"HARNESS_UPSTREAM": "http://10.0.0.9:7777"}),
    ):
        try:
            run_harness.main()
            # Valori attesi espliciti, letti DENTRO il contesto perche'
            # patch.dict ripristina l'ambiente all'uscita. Con --mobile il
            # finto uvicorn vede sia il ponte sia il principale.
            assert sorted(avvii) == ["server.main:app", "server.mobile:app"]
            assert os.environ["HARNESS_UPSTREAM"] == "http://10.0.0.9:7777"
        finally:
            sys.argv = argv_prima