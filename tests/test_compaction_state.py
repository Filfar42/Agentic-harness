"""Checkpoint incrementali: lo stato aperto non dipende dal recupero lessicale."""

from __future__ import annotations

import copy
import json

import pytest

from core import agent, compaction, libreria
from core.config import GenParams, budgets_for
from tests.test_aux_inference import ControlledBackend
from tests.test_compattazione import FintoBackend, cronologia, scrittura


def _compatta(msgs, backend, base=None, **kwargs):
    return agent.compatta_cronologia(
        msgs, backend=backend, params=GenParams(num_ctx=8192), budgets=budgets_for(8192),
        strip_thinking=True, schedario=base, **kwargs,
    )


def _continua(msgs, inizio):
    for i in range(inizio, inizio + 14):
        msgs += scrittura(f"c{i}", f"new_{i}.py", "x" * 4000)


def _ultimo(msgs):
    return next(m for m in reversed(msgs) if m["role"] == "summary")


def test_stato_omesso_dal_nuovo_riassunto_sopravvive_a_tre_checkpoint(tmp_path):
    msgs = cronologia(14)
    msgs[1]["tool_calls"][0]["function"]["arguments"] = json.dumps({
        "filepath": "OLD_RAW_MARKER.py", "content": "x" * 4000,
    })
    be = FintoBackend(
        "FATTO: vecchio lavoro completato\nAPERTO: verificare CHECKSUM_ZIRCONE\n"
        "VINCOLI: preservare i terminatori CRLF",
    )
    assert _compatta(msgs, be, tmp_path)
    stato = _ultimo(msgs)["active_state"]
    be.testo = "FATTO: altri moduli creati"
    for inizio in (14, 28):
        _continua(msgs, inizio)
        assert _compatta(msgs, be, tmp_path)
        inviato = be.chiamate[-1][0][1]["content"]
        assert "CHECKSUM_ZIRCONE" in inviato and "CRLF" in inviato
        assert "OLD_RAW_MARKER" not in inviato
        assert "vecchio lavoro completato" not in inviato
        assert _ultimo(msgs)["active_state"] == stato
        api = agent.build_api_messages(msgs, system_prompt="SYS", env_header=None)
        assert "CHECKSUM_ZIRCONE" in json.dumps(api)
        archivio = (tmp_path / _ultimo(msgs)["archive_path"]).read_text(encoding="utf-8")
        assert "CHECKSUM_ZIRCONE" in archivio and "CRLF" in archivio


def test_aperto_risolto_testualmente_non_risorge_alla_compattazione_successiva(tmp_path):
    msgs = cronologia(14)
    be = FintoBackend("APERTO: verificare CHECKSUM_ZIRCONE")
    assert _compatta(msgs, be, tmp_path)
    _continua(msgs, 14)
    be.testo = "FATTO: verificare CHECKSUM_ZIRCONE\nAPERTO: nessuno"
    assert _compatta(msgs, be, tmp_path)
    assert _ultimo(msgs)["active_state"] == ""
    _continua(msgs, 28)
    be.testo = "FATTO: moduli creati"
    assert _compatta(msgs, be, tmp_path)
    assert "CHECKSUM_ZIRCONE" not in be.chiamate[-1][0][1]["content"]
    assert _ultimo(msgs)["active_state"] == ""


def test_un_nuovo_aperto_non_cancella_quelli_omessi():
    stato = compaction.stato_attivo(
        "APERTO:\n- prima verifica\nVINCOLI: mantenere CRLF",
        "APERTO:\n- seconda verifica\n- prima verifica",
    )
    assert stato.count("prima verifica") == 1
    assert "seconda verifica" in stato and "mantenere CRLF" in stato


def test_formati_markdown_e_intestazioni_conservano_lo_stato():
    testo = (
        "**SCOPERTO:** una cosa gia' nota\n**APERTO:**\n- verificare TLS\n"
        "### VINCOLI\n- non cambiare ABI\nFATTO: concluso altro lavoro\n"
    )
    stato = compaction.stato_attivo(testo)
    assert "verificare TLS" in stato and "non cambiare ABI" in stato
    assert "concluso altro" not in stato and "gia' nota" not in stato


def test_stato_abbreviato_ha_un_riferimento_al_testo_integrale(tmp_path):
    msgs = cronologia(14)
    dettaglio = "VINCOLI:\n" + "\n".join(f"- vincolo {i}: " + "x" * 100 for i in range(30))
    assert _compatta(msgs, FintoBackend(dettaglio), tmp_path)
    summary = _ultimo(msgs)
    assert len(summary["active_state"]) <= compaction.MAX_STATO_ATTIVO_CHARS
    assert summary["archive_path"] in summary["active_state"]
    assert "Stato abbreviato" in summary["active_state"]
    archivio = (tmp_path / summary["archive_path"]).read_text(encoding="utf-8")
    assert "vincolo 29:" in archivio
    _continua(msgs, 14)
    assert _compatta(msgs, FintoBackend("FATTO: altro lavoro"), tmp_path)
    nuovo_archivio = (tmp_path / _ultimo(msgs)["archive_path"]).read_text(encoding="utf-8")
    assert summary["archive_path"] in nuovo_archivio


def test_stato_eccessivo_senza_archivio_non_viene_perso_silenziosamente():
    msgs = cronologia(14)
    prima = copy.deepcopy(msgs)
    assert _compatta(msgs, FintoBackend("APERTO: " + "x" * 3000)) is None
    assert msgs == prima


def test_legacy_non_strutturato_resta_nel_fallback_anche_se_archiviato(tmp_path):
    msgs = cronologia(14)
    be = FintoBackend("Ricorda LEGACY_PENDING_MARKER prima della consegna.")
    assert _compatta(msgs, be, tmp_path)
    assert _ultimo(msgs)["archive_path"]
    _continua(msgs, 14)
    be.testo = "FATTO: nuovo lavoro"
    assert _compatta(msgs, be, tmp_path)
    assert "LEGACY_PENDING_MARKER" in be.chiamate[-1][0][1]["content"]


def test_archivio_non_recuperabile_dalla_libreria_non_fa_scartare_il_summary(tmp_path, monkeypatch):
    msgs = cronologia(14)
    be = FintoBackend("SCOPERTO: ONLY_SAVED_FACT")
    assert _compatta(msgs, be, tmp_path)
    _continua(msgs, 14)
    # Simula una .memoria divenuta junction o inaccessibile al recupero.
    monkeypatch.setattr(libreria, "voci", lambda _: [])
    be.testo = "FATTO: altro lavoro"
    assert _compatta(msgs, be, tmp_path)
    assert "ONLY_SAVED_FACT" in be.chiamate[-1][0][1]["content"]


@pytest.mark.parametrize("ending", ["cancel", "close_cancel"])
def test_stop_durante_riassunto_non_archivia_ne_modifica_cronologia(tmp_path, ending):
    msgs = cronologia(14)
    prima = copy.deepcopy(msgs)
    be = ControlledBackend(ending)
    def stop():
        return be.stopped
    assert _compatta(msgs, be, tmp_path, should_stop=stop) is None
    assert msgs == prima and libreria.voci(tmp_path) == []
    assert be.calls[0][3] is stop and be.closed


def test_stop_gia_richiesto_non_avvia_compattazione(tmp_path):
    be = ControlledBackend()
    assert _compatta(cronologia(14), be, tmp_path, should_stop=lambda: True) is None
    assert not be.calls


def test_risposte_umane_effettive_restano_specifica_testuale():
    messaggi = [
        {"role": "user", "content": "crea il report"},
        {"role": "tool", "name": "ask_user_question", "answered": True,
         "content": json.dumps({"question": "quale formato?", "user_answer": "Solo JSON, niente CSV"})},
        {"role": "tool", "name": "ask_user_question",
         "content": json.dumps({"user_answer": "risposta inventata"})},
    ]
    assert compaction.richieste_utente(messaggi) == ["crea il report", "Solo JSON, niente CSV"]


def test_esito_breve_non_taglia_json_ne_handle_lunghi():
    handle = ".deposito/" + "sorgente_" * 60 + ".txt"
    originale = {
        "esito": "FALLITO", "returncode": 1, "stdout_deposito": handle,
        "stderr_deposito": handle.replace(".txt", "-err.txt"), "sha256": "f" * 64,
        "error": "problema " * 1000, "stderr": "diagnostica " * 1000,
    }
    breve = json.loads(compaction._esito_breve(json.dumps(originale)))
    for key in ("esito", "returncode", "stdout_deposito", "stderr_deposito", "sha256"):
        assert breve[key] == originale[key]
    assert "problema" in breve["error"] and len(breve["error"]) < 400
    assert len(breve["stderr"]) <= 300
