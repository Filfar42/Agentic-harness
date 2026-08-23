"""Test di run.py: come parte il server e come si ferma.

Due difetti hanno motivato questo modulo.

Il primo: con ``run.py --port 8201 --mobile`` il ponte interno guardava la
8123 di default e trovava il principale spento (health ``ok:false`` visto in
anteprima). La funzione sotto test e' ``upstream_mobile``.

Il secondo: Ctrl+C non spegneva piu' niente. Le rotte SSE sono risposte che
non finiscono mai, e uvicorn allo spegnimento le aspetta -- bastava una
scheda del browser aperta per bloccare tutto, e l'unico modo di uscire era
chiudere la finestra del terminale.

I server non si avviano davvero: si intercetta ``uvicorn.Server.run``, che e'
il punto in cui **entrambi** passano (principale e ponte), qualunque sia la
funzione che li costruisce.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

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


def _intercetta_avvii(run_harness, avvii, ambiente=None):
    """Sostituisce l'avvio vero con una nota su cosa sarebbe partito."""

    def finto_run(self, *args, **kwargs):
        avvii.append((self.config.app, self.config.port))
        if ambiente is not None and self.config.app == "server.mobile:app":
            ambiente["al_avvio_del_ponte"] = os.environ.get("HARNESS_UPSTREAM")

    return patch.object(run_harness.uvicorn.Server, "run", finto_run)


def test_main_imposta_harness_upstream_per_il_thread_del_ponte():
    """main() deve fissare HARNESS_UPSTREAM PRIMA di aprire il thread del ponte.

    Se non lo scrivesse, il ponte leggerebbe il default sbagliato.
    """
    run_harness = _run()
    avvii: list[tuple[str, int]] = []
    ambiente: dict[str, str | None] = {}

    argv_prima = sys.argv
    sys.argv = ["run.py", "--port", "8203", "--mobile", "--mobile-port", "8202", "--no-browser"]
    with (
        _intercetta_avvii(run_harness, avvii, ambiente),
        patch.object(run_harness, "chiave", return_value="chiave-finta"),
        patch.object(run_harness.webbrowser, "open"),
        patch.dict(os.environ, {}, clear=False),
    ):
        os.environ.pop("HARNESS_UPSTREAM", None)
        try:
            run_harness.main()
        finally:
            sys.argv = argv_prima
        # Il ponte parte in un thread: gli si da' un attimo per arrivare.
        for _ in range(200):
            if ("server.mobile:app", 8202) in avvii:
                break
            time.sleep(0.01)

    assert ("server.main:app", 8203) in avvii
    assert ("server.mobile:app", 8202) in avvii
    # Valore atteso esplicito: quando il ponte si e' avviato, l'ambiente
    # doveva gia' puntare alla porta DEL COMANDO.
    assert ambiente["al_avvio_del_ponte"] == "http://127.0.0.1:8203"


def test_main_non_sovrascrive_un_harness_upstream_gia_presente():
    """Se l'utente ha scelto l'upstream a mano, main() non lo tocca."""
    run_harness = _run()
    avvii: list[tuple[str, int]] = []

    argv_prima = sys.argv
    sys.argv = ["run.py", "--port", "8123", "--mobile", "--no-browser"]
    with (
        _intercetta_avvii(run_harness, avvii),
        patch.object(run_harness, "chiave", return_value="chiave-finta"),
        patch.object(run_harness.webbrowser, "open"),
        patch.dict(os.environ, {"HARNESS_UPSTREAM": "http://10.0.0.9:7777"}),
    ):
        try:
            run_harness.main()
        finally:
            sys.argv = argv_prima
        for _ in range(200):
            if len(avvii) >= 2:
                break
            time.sleep(0.01)
        # Valori attesi espliciti, letti DENTRO il contesto perche' patch.dict
        # ripristina l'ambiente all'uscita.
        assert sorted(app for app, _ in avvii) == ["server.main:app", "server.mobile:app"]
        assert os.environ["HARNESS_UPSTREAM"] == "http://10.0.0.9:7777"


# --------------------------------------------------------------- spegnimento


def test_ctrl_c_avvisa_gli_stream_prima_di_mettersi_ad_aspettare():
    """Il gancio che fa funzionare Ctrl+C.

    Uvicorn, preso il segnale, aspetta che finiscano le risposte in corso. Le
    rotte SSE non finiscono per mestiere: senza qualcuno che le avvisi, il
    server resta li' e sembra che Ctrl+C sia ignorato. Il gestore del segnale
    e' l'ultimo momento utile -- lo spegnimento della lifespan viene dopo,
    quando si e' gia' bloccati.
    """
    import signal

    from server import runner as runner_mod

    run_harness = _run()
    runner_mod.dimentica_spegnimento()
    svegliati: list[str] = []
    runner_mod.al_spegnimento(lambda: svegliati.append("sveglia"))

    ponte = SimpleNamespace(should_exit=False)
    server = run_harness.ServerCheSiFermaDavvero(
        run_harness.uvicorn.Config("server.main:app")
    )
    server._compagni = [ponte]
    try:
        server.handle_exit(signal.SIGINT, None)

        assert runner_mod.SPEGNIMENTO.is_set(), "gli stream non sono stati avvisati"
        assert svegliati == ["sveglia"], "le code aperte vanno svegliate, non lasciate nel get()"
        assert server.should_exit is True, "e uvicorn deve comunque fermarsi"
        assert ponte.should_exit is True, "anche il ponte del telefono"
    finally:
        runner_mod.dimentica_spegnimento()


def test_il_tetto_all_attesa_e_impostato_su_entrambi_i_server():
    """La cintura oltre alle bretelle: se un domani qualcuno aggiunge una
    risposta lunga e si scorda di ascoltare lo spegnimento, si aspetta questo
    e poi si chiude comunque."""
    run_harness = _run()
    configurazioni: list[object] = []

    def finto_run(self, *args, **kwargs):
        configurazioni.append(self.config)

    argv_prima = sys.argv
    sys.argv = ["run.py", "--mobile", "--no-browser"]
    with (
        patch.object(run_harness.uvicorn.Server, "run", finto_run),
        patch.object(run_harness, "chiave", return_value="chiave-finta"),
        patch.object(run_harness.webbrowser, "open"),
    ):
        try:
            run_harness.main()
        finally:
            sys.argv = argv_prima
        for _ in range(200):
            if len(configurazioni) >= 2:
                break
            time.sleep(0.01)

    assert len(configurazioni) == 2
    for config in configurazioni:
        assert config.timeout_graceful_shutdown == run_harness.TIMEOUT_SPEGNIMENTO


@pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "Serve un gruppo di processi e un SIGINT vero: su Windows si fa con "
        "GenerateConsoleCtrlEvent e una console dedicata, che in una suite non "
        "si puo' allestire. Il gancio resta coperto dai test qui sopra."
    ),
)
def test_ctrl_c_spegne_il_server_anche_con_un_browser_attaccato():
    """La prova vera, con un processo vero e un segnale vero.

    E' l'unico test che avrebbe scoperto il difetto: tutto il resto passava,
    perche' il blocco si vede solo quando c'e' **un client collegato al bus**
    -- cioe' sempre, nell'uso normale, e mai in un test che non ne apre uno.
    """
    import socket
    import subprocess
    import threading
    import urllib.request

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        porta = s.getsockname()[1]

    processo = subprocess.Popen(
        [sys.executable, "run.py", "--no-browser", "--port", str(porta)],
        cwd=str(RADICE),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        def raggiungibile():
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{porta}/api/sessions", timeout=1)
                return True
            except Exception:
                return False

        scadenza = time.monotonic() + 20
        while not raggiungibile() and time.monotonic() < scadenza:
            time.sleep(0.2)
        assert raggiungibile(), "il server non si e' alzato"

        # Il browser: una connessione al bus globale che non finisce mai.
        def resta_attaccato():
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{porta}/api/events", timeout=30
                ).read()
            except Exception:
                pass

        threading.Thread(target=resta_attaccato, daemon=True).start()
        time.sleep(1.5)

        import signal as sig_mod

        os.killpg(os.getpgid(processo.pid), sig_mod.SIGINT)
        try:
            processo.wait(timeout=15)
        except subprocess.TimeoutExpired:
            raise AssertionError(
                "Ctrl+C non ha spento il server: e' rimasto ad aspettare uno "
                "stream SSE che non finisce mai"
            ) from None
    finally:
        if processo.poll() is None:
            processo.kill()
            processo.wait(timeout=10)
