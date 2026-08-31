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
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import config as config_mod
from core import memory as memory_mod
from core import sandbox as sandbox_mod
from core import session as session_mod
from core import settings as settings_mod
from server import previewhost


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
    # Il server delle anteprime si accende alla prima pagina mostrata e apre
    # una porta vera in ascolto. Un test non deve mai farlo per sbaglio: qui
    # nasce spento, e chi lo prova lo accende da se' e lo spegne dopo.
    monkeypatch.setitem(config_mod.DEFAULTS, "preview_host_port", 0)
    # Le conversazioni. Lo facevano le cinque fixture che creano un client --
    # cioe' esattamente la ripetizione che questo file esiste per togliere -- e
    # un test nuovo che tocchi ``session`` senza passare da quelle scriverebbe
    # fra le chat vere dell'utente.
    monkeypatch.setattr(session_mod, "DATA_DIR", tmp_path / "chat_sessions")
    # Le due cache dell'indice e della ricerca sono di modulo e hanno per
    # chiave il **nome del file**, non il percorso: due test che creano
    # entrambi ``c0.json`` nelle loro tmp_path diverse condividono la voce, e
    # il secondo legge i contenuti del primo. Le fixture che se ne ricordavano
    # le svuotavano a mano -- di nuovo la ripetizione che questo file esiste
    # per togliere.
    session_mod._index_cache.clear()
    session_mod._search_cache.clear()
    # E la memo dei container gia' verificati, che e' stato di modulo: vive
    # oltre il singolo test, e i container dei test sono finti e cambiano
    # istantaneamente. Tre secondi di TTL bastano a far passare per pronto un
    # container che il test successivo si aspetta di dover creare.
    sandbox_mod.dimentica_container()
    yield
    sandbox_mod.dimentica_container()
    previewhost.shutdown()
    previewhost.set_root(tmp_path)
    _aspetta_i_turni()


# Quanto si aspetta che un turno rimasto in volo si chiuda, a fine test.
ATTESA_TURNI_S = 10.0


def _aspetta_i_turni() -> None:
    """Nessun turno deve sopravvivere al test che l'ha avviato.

    I worker sono thread demoni e diverse cose sono globali di modulo: il
    copione del finto Ollama, la memo dei container, ``_spazzate``. Un turno
    ancora in volo le tocca **mentre gira il test successivo**, che fallisce
    per un motivo che non ha niente a che vedere con quello che prova -- e
    fallisce a intermittenza, cioe' nel modo piu' caro da capire. Osservati
    due: ``/api/stream`` senza ``done`` e una spazzata della sandbox saltata
    perche' ``RUNNERS.running_ids()`` non era vuoto.

    ``test_mobile_proxy`` questa attesa ce l'aveva gia', nella sua fixture.
    Sta qui per la ragione dichiarata in testa a questo file: vale anche per
    il prossimo file di test, che nessuno si ricordera' di attrezzare.

    Se ``server.main`` non e' stato importato, il test non ha fatto girare
    niente e non c'e' niente da aspettare.
    """
    server_main = sys.modules.get("server.main")
    if server_main is None:
        return
    registro = getattr(server_main, "RUNNERS", None)
    if registro is None:
        return
    scadenza = time.monotonic() + ATTESA_TURNI_S
    while registro.running_ids() and time.monotonic() < scadenza:
        time.sleep(0.02)
