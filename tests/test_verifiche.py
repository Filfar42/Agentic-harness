"""Il registro delle verifiche: cosa conta come verifica, e cosa la chiude.

Il modulo nasce da quattro difetti misurati sul vecchio ``VerificationTracker``,
e ogni test qui sotto ne tiene fermo uno. Non sono casi di scuola: sono la
ricostruzione di cicli visti girare a vuoto.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.agent import RipetizioniTool
from core.verifiche import (
    GIUSTIFICATA,
    PARZIALE,
    SBAGLIATA,
    SUITE,
    VERDE,
    RegistroVerifiche,
    identita_e_ambito,
)


def busta(comando: str, code: int) -> str:
    return json.dumps({"command": comando, "returncode": code,
                       "esito": "ok" if code == 0 else "FALLITO"})


# ---------------------------------------------------------------------------
# Identita'
# ---------------------------------------------------------------------------


def test_lo_stesso_test_lanciato_in_due_modi_e_una_verifica_sola():
    """Il difetto riprodotto: ``pytest x`` rosso, ``python -m pytest x`` verde.

    L'identita' era la stringa scritta, quindi la seconda forma apriva una
    verifica nuova e la prima restava rossa per sempre -- su un comando che
    nessuno aveva piu' intenzione di rilanciare. Il turno non poteva piu'
    chiudersi pulito, e i solleciti continuavano a nominare una riga morta.
    """
    registro = RegistroVerifiche()
    registro.record("run_command", busta("pytest tests/test_bug.py -q", 1))
    assert registro.unresolved is not None

    registro.record("run_command", busta("python -m pytest tests/test_bug.py -q", 0))
    assert registro.unresolved is None, "e' lo stesso test, lanciato con un prefisso diverso"
    assert len(registro.verifiche) == 1


def test_le_opzioni_di_lettura_non_creano_una_verifica_nuova():
    """``-q`` e ``-v`` cambiano come si legge l'output, non cosa si verifica."""
    registro = RegistroVerifiche()
    registro.record("run_command", busta("pytest -q", 1))
    registro.record("run_command", busta("uv run pytest -v --tb=short", 0))
    assert registro.unresolved is None
    assert len(registro.verifiche) == 1


def test_un_sottoinsieme_verde_non_assolve_la_suite_rossa():
    """La regola che l'ambito serve a tenere.

    Far passare tre test non dimostra che la suite passa, e un registro che
    confondesse le due cose darebbe il verde su una misura che non e' stata
    fatta.
    """
    registro = RegistroVerifiche()
    registro.record("run_command", busta("pytest -q", 1))
    registro.record("run_command", busta("pytest tests/test_bug.py -q", 0))

    aperte = registro.pendenti
    assert [v.identita for v in aperte] == ["pytest"]
    assert aperte[0].ambito == SUITE
    assert registro.riepilogo()["suite_rossa"] is True
    assert registro.verifiche["pytest tests/test_bug.py"].ambito == PARZIALE


def test_il_cd_davanti_non_e_una_verifica_diversa():
    identita, ambito, runner = identita_e_ambito("cd /work && pytest -q")
    assert (identita, ambito, runner) == ("pytest", SUITE, "pytest")


# ---------------------------------------------------------------------------
# Cosa conta come verifica
# ---------------------------------------------------------------------------


def test_una_ricerca_senza_corrispondenze_non_e_una_verifica_rossa():
    """``rg`` esce 1 quando non trova niente: e' una risposta, non un guasto.

    Contarla apriva una verifica pendente che il modello non poteva chiudere in
    nessun modo sensato, e il sollecito gli diceva di rileggere lo stderr di un
    comando che non aveva stderr.
    """
    registro = RegistroVerifiche()
    registro.record("run_command", busta("rg -n 'VerificationTracker' core", 1))
    assert registro.unresolved is None
    assert registro.verifiche == {}


def test_pytest_senza_test_raccolti_e_un_comando_sbagliato_non_un_rosso():
    """Exit 5 = nessun test raccolto. La selezione e' sbagliata, non il codice.

    Trattarlo come rosso produce il sollecito piu' inutile che ci sia: cercare
    un bug dove non c'e', su una riga di comando che basterebbe correggere.
    """
    registro = RegistroVerifiche()
    registro.record("run_command", busta("pytest tests/test_inesistente.py -q", 5))
    assert registro.unresolved is None
    assert registro.verifiche["pytest tests/test_inesistente.py"].stato == SBAGLIATA
    # Non sparisce pero': un turno in cui tre verifiche su quattro erano
    # comandi sbagliati non e' un turno andato bene.
    assert registro.riepilogo()["sbagliate"]


def test_un_comando_inesistente_non_resta_rosso_per_sempre():
    registro = RegistroVerifiche()
    registro.record("run_command", busta("cd /work && pytest", 127))
    assert registro.unresolved is None
    registro.record("run_command", busta("pytest -q", 1))
    assert registro.unresolved == ("pytest -q", 1, 1)


def test_un_server_ucciso_dal_timeout_non_e_una_verifica():
    registro = RegistroVerifiche()
    registro.record("run_command", busta("python -m http.server 8200 --bind 0.0.0.0", 1))
    assert registro.unresolved is None


def test_gli_altri_tool_non_entrano_nel_registro():
    registro = RegistroVerifiche()
    registro.record("read_file", json.dumps({"filepath": "x.py"}))
    assert registro.verifiche == {}


# ---------------------------------------------------------------------------
# Giustificare, non cancellare
# ---------------------------------------------------------------------------


def test_giustificare_un_rosso_non_ne_spegne_altri():
    """Il difetto piu' costoso del vecchio ``clear()``.

    Riprodotto: due rossi indipendenti, se ne dichiara non pertinente uno e
    spariscono entrambi. Il secondo non lo aveva letto nessuno, e il turno si
    chiudeva come se fosse verde.
    """
    registro = RegistroVerifiche()
    registro.record("run_command", busta("pytest -q", 1))
    registro.record("run_command", busta("pytest -q", 1))   # due tentativi: la piu' insistente
    registro.record("run_command", busta("mypy core", 1))
    assert len(registro.pendenti) == 2

    # Senza identita' si giustifica la piu' insistente, che e' quella che i
    # messaggi nominano: il modello archivia cio' che ha letto, non cio' che
    # capita.
    giustificata = registro.giustifica(None, "il container non c'e' in questo ambiente",
                                       via="ignore_red")
    assert giustificata is not None
    assert len(registro.pendenti) == 1
    assert registro.pendenti[0].identita == "mypy core"
    assert registro.riepilogo()["giustificate"][0]["motivo"].startswith("il container")


def test_una_verifica_giustificata_non_ridiventa_rossa_da_sola():
    """Il motivo scritto vale finche' non lo si ritira.

    Senza questa regola, rilanciare per curiosita' una verifica gia' archiviata
    riaprirebbe il blocco che era stato chiuso con una frase -- e il modello
    imparerebbe a non rilanciarla mai piu', che e' il contrario di cio' che si
    vuole.
    """
    registro = RegistroVerifiche()
    registro.record("run_command", busta("pytest -q", 1))
    registro.giustifica(None, "il container non c'e' in questo ambiente")
    registro.record("run_command", busta("pytest -q", 1))
    assert registro.pendenti == []
    assert registro.verifiche["pytest"].stato == GIUSTIFICATA

    # Verde resta verde: se il mondo cambia, il registro se ne accorge.
    registro.record("run_command", busta("pytest -q", 0))
    assert registro.verifiche["pytest"].stato == VERDE


def test_il_riepilogo_distingue_i_tre_esiti():
    registro = RegistroVerifiche()
    registro.record("run_command", busta("pytest tests/test_a.py", 0))
    registro.record("run_command", busta("pytest -q", 1))
    registro.record("run_command", busta("mypy core", 1))
    registro.giustifica("mypy core", "i tipi di terze parti non sono installati qui")

    riepilogo = registro.riepilogo()
    assert riepilogo["stato"] == "rossa"
    assert [v["identita"] for v in riepilogo["pendenti"]] == ["pytest"]
    assert [v["identita"] for v in riepilogo["verdi"]] == ["pytest tests/test_a.py"]
    assert [v["identita"] for v in riepilogo["giustificate"]] == ["mypy core"]


def test_clear_resta_ma_e_un_reset_esplicito():
    registro = RegistroVerifiche()
    registro.record("run_command", busta("pytest -q", 1))
    registro.clear()
    assert registro.verifiche == {}
    assert registro.stato() == "non_verificato"


# ---------------------------------------------------------------------------
# Stallo: le chiamate che falliscono sempre allo stesso modo
# ---------------------------------------------------------------------------


def test_le_chiamate_fallite_adesso_si_contano():
    """Il buco fra i due rilevatori esistenti.

    ``registra`` veniva chiamata solo dentro un ``if ok:``, col ragionamento che
    rifare una chiamata fallita e' legittimo. Lo e' due volte -- un timeout, un
    container lento. Non lo e' alla terza, ed e' esattamente la forma dello
    stallo osservato: ``manage_plan`` rifiutato, richiamato identico, rifiutato.
    """
    ripetizioni = RipetizioniTool()
    args = {"action": "complete", "step_id": "1"}
    assert ripetizioni.registra_fallita("manage_plan", args) == 1
    assert ripetizioni.registra_fallita("manage_plan", args) == 2
    assert ripetizioni.registra_fallita("manage_plan", args) == RipetizioniTool.SOGLIA_STALLO

    # L'ordine delle chiavi non fa una chiamata nuova.
    assert ripetizioni.registra_fallita("manage_plan", {"step_id": "1", "action": "complete"}) == 4


def test_una_modifica_riuscita_azzera_lo_stallo():
    """Non conta quante volte hai riprovato: conta se in mezzo e' cambiato qualcosa."""
    ripetizioni = RipetizioniTool()
    args = {"command": "pytest -q"}
    ripetizioni.registra_fallita("run_command", args)
    ripetizioni.registra_fallita("run_command", args)
    ripetizioni.dimentica_fallimenti()
    assert ripetizioni.registra_fallita("run_command", args) == 1
