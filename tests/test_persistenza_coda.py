"""I messaggi si scrivono in coda, non riscrivendo tutta la conversazione.

Prima la cronologia stava dentro il JSON dei metadati: salvare voleva dire
riserializzare e riscrivere tutto. Su una conversazione da 2,5 MB sono ~23 ms
di GIL -- cioe' 23 ms in cui il server non risponde a nessuno -- pagati ad
**ogni tool finito**. Misura del 30/08/2026 sulla chat piu' lunga della
postazione: **23,2 ms -> 0,5 ms**.

Il pericolo di accodare e' scritto nel modulo: i messaggi vengono anche
modificati sul posto. Qui si prova che le due difese funzionano davvero --
l'impronta dell'ultima riga, e la riscrittura di fine turno.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import session as session_mod


@pytest.fixture()
def archivio(tmp_path, monkeypatch):
    """Una cartella di conversazioni usa e getta."""
    monkeypatch.setattr(session_mod, "DATA_DIR", tmp_path)
    session_mod.dimentica_coda()
    session_mod._index_cache.clear()
    session_mod._search_cache.clear()
    session_mod._last_save.clear()
    return tmp_path


def _stato(sid: str, *messaggi: dict) -> dict:
    return {"current_session_id": sid, "messages": list(messaggi)}


def _righe(cartella: Path, sid: str) -> list[dict]:
    testo = (cartella / f"{sid}.jsonl").read_text(encoding="utf-8")
    return [json.loads(r) for r in testo.splitlines() if r.strip()]


# ---------------------------------------------------------------------------
# Due file, e cosa c'e' in ognuno
# ---------------------------------------------------------------------------


def test_i_messaggi_non_stanno_piu_nei_metadati(archivio):
    st = _stato("s1", {"role": "user", "content": "ciao"})
    session_mod.save_session(st, force=True)

    meta = json.loads((archivio / "s1.json").read_text(encoding="utf-8"))
    assert "messages" not in meta
    # ...ma i due numeri che servono alla sidebar si', o l'indice dovrebbe
    # aprire la cronologia di ogni conversazione per contarla.
    assert meta["n_messages"] == 1
    assert meta["pending"] is False
    assert _righe(archivio, "s1") == [{"role": "user", "content": "ciao"}]


def test_un_messaggio_nuovo_si_accoda_e_basta(archivio):
    """La prova che e' un accodamento e non una riscrittura: i byte gia'
    scritti restano **identici**, e il file cresce in fondo."""
    st = _stato("s1", {"role": "user", "content": "primo"})
    session_mod.save_session(st, force=True)
    prima = (archivio / "s1.jsonl").read_bytes()

    st["messages"].append({"role": "assistant", "content": "secondo"})
    session_mod._last_save.clear()
    session_mod.save_session(st, force=True)
    dopo = (archivio / "s1.jsonl").read_bytes()

    assert dopo.startswith(prima)
    assert len(dopo) > len(prima)
    assert len(_righe(archivio, "s1")) == 2


def test_si_rilegge_tutto_quello_che_si_e_scritto(archivio):
    st = _stato("s1")
    for i in range(20):
        st["messages"].append({"role": "user", "content": f"messaggio {i}"})
        session_mod._last_save.clear()
        session_mod.save_session(st, force=True)

    session_mod.dimentica_coda()
    riletto: dict = {}
    assert session_mod.load_session(riletto, "s1")
    assert riletto["messages"] == st["messages"]


# ---------------------------------------------------------------------------
# Le due difese contro le modifiche sul posto
# ---------------------------------------------------------------------------


def test_l_impronta_vede_l_ultimo_messaggio_cambiato(archivio):
    """Prima difesa. Modificare l'ultimo messaggio gia' scritto e poi accodare
    lascerebbe su disco due versioni dello stesso messaggio: l'impronta se ne
    accorge e riscrive, senza che nessuno debba chiederlo."""
    st = _stato("s1", {"role": "assistant", "content": "prima versione"})
    session_mod.save_session(st, force=True)

    st["messages"][0]["content"] = "seconda versione"
    session_mod._last_save.clear()
    session_mod.save_session(st, force=True)

    assert _righe(archivio, "s1") == [{"role": "assistant", "content": "seconda versione"}]


def test_una_cronologia_accorciata_riscrive(archivio):
    """La compattazione sostituisce un tratto di cronologia con un riassunto:
    il conto cala, e accodare lascerebbe su disco quello che il modello non ha
    piu' davanti."""
    st = _stato("s1", *[{"role": "user", "content": str(i)} for i in range(6)])
    session_mod.save_session(st, force=True)

    st["messages"][:] = [{"role": "summary", "content": "riassunto"}]
    session_mod._last_save.clear()
    session_mod.save_session(st, force=True)

    assert _righe(archivio, "s1") == [{"role": "summary", "content": "riassunto"}]


def test_solo_la_riscrittura_vede_un_messaggio_vecchio_cambiato(archivio):
    """Seconda difesa, e il suo confine.

    La traccia del pensiero si attacca all'assistente del passo **precedente**,
    che a quel punto non e' piu' l'ultima riga: l'impronta non puo' vederlo
    senza rileggere tutto. Per questo a fine turno si riscrive -- ed e' il solo
    momento in cui serve, perche' li' la cronologia e' ferma.
    """
    st = _stato(
        "s1",
        {"role": "assistant", "content": "penso"},
        {"role": "tool", "content": "fatto"},
    )
    session_mod.save_session(st, force=True)

    st["messages"][0]["think"] = {"tokens": 120}
    session_mod._last_save.clear()
    session_mod.save_session(st, force=True)              # accoda: non la vede
    assert "think" not in _righe(archivio, "s1")[0]

    session_mod._last_save.clear()
    session_mod.save_session(st, force=True, riscrivi=True)   # fine turno
    assert _righe(archivio, "s1")[0]["think"] == {"tokens": 120}


# ---------------------------------------------------------------------------
# Il formato vecchio continua ad aprirsi
# ---------------------------------------------------------------------------


def test_una_conversazione_di_prima_si_apre_e_si_converte(archivio):
    """Nessuna migrazione da lanciare: la coda nasce al primo salvataggio.
    Una conversazione mai piu' aperta resta com'e', e va bene cosi'."""
    vecchia = {
        "id": "s9",
        "title": "Vecchia",
        "messages": [
            {"role": "user", "content": "domanda"},
            {"role": "assistant", "content": "risposta"},
        ],
        "plan": [],
        "notes": [],
    }
    (archivio / "s9.json").write_text(json.dumps(vecchia), encoding="utf-8")

    st: dict = {}
    assert session_mod.load_session(st, "s9")
    assert [m["content"] for m in st["messages"]] == ["domanda", "risposta"]
    assert not (archivio / "s9.jsonl").exists()

    st["current_session_id"] = "s9"
    session_mod.save_session(st, force=True)
    assert _righe(archivio, "s9") == vecchia["messages"]
    assert "messages" not in json.loads((archivio / "s9.json").read_text(encoding="utf-8"))


def test_l_indice_di_una_conversazione_vecchia_conta_lo_stesso(archivio):
    (archivio / "s9.json").write_text(
        json.dumps({"id": "s9", "title": "V", "messages": [{"role": "user", "content": "a"}]}),
        encoding="utf-8",
    )
    righe = session_mod.list_sessions()
    assert righe[0]["n_messages"] == 1


def test_l_indice_non_apre_piu_i_messaggi(archivio):
    """Il conto arriva dai metadati: la sidebar non rilegge megabyte di
    cronologia ad ogni click. La prova e' che l'indice risponde giusto anche
    con la coda tolta di mezzo."""
    st = _stato("s1", *[{"role": "user", "content": "x"} for _ in range(7)])
    session_mod.save_session(st, force=True)
    (archivio / "s1.jsonl").unlink()
    session_mod._index_cache.clear()

    righe = session_mod.list_sessions()
    assert righe[0]["n_messages"] == 7


# ---------------------------------------------------------------------------
# Il resto del mondo continua a funzionare
# ---------------------------------------------------------------------------


def test_la_ricerca_guarda_dentro_la_coda(archivio):
    st = _stato(
        "s1",
        {"role": "user", "content": "parliamo di ponteggi"},
        {"role": "tool", "name": "run_command", "args": {"command": "pytest -q"}},
    )
    session_mod.save_session(st, force=True)

    assert [r["id"] for r in session_mod.search_sessions("ponteggi")] == ["s1"]
    assert [r["id"] for r in session_mod.search_sessions("pytest")] == ["s1"]


def test_cancellare_porta_via_tutti_e_due_i_file(archivio):
    st = _stato("s1", {"role": "user", "content": "ciao"})
    session_mod.save_session(st, force=True)
    session_mod.delete_session("s1")
    assert not (archivio / "s1.json").exists()
    assert not (archivio / "s1.jsonl").exists()


def test_una_riga_tronca_non_porta_via_la_conversazione(archivio):
    """Il guadagno nascosto della coda: un processo morto durante la scrittura
    costa **l'ultimo messaggio**. Con un file unico costava tutta la chat, che
    diventava un JSON illeggibile."""
    st = _stato("s1", {"role": "user", "content": "primo"}, {"role": "user", "content": "secondo"})
    session_mod.save_session(st, force=True)
    with open(archivio / "s1.jsonl", "a", encoding="utf-8") as fh:
        fh.write('{"role": "user", "content": "tron')

    session_mod.dimentica_coda()
    riletto: dict = {}
    assert session_mod.load_session(riletto, "s1")
    assert [m["content"] for m in riletto["messages"]] == ["primo", "secondo"]


def test_se_la_coda_non_si_scrive_i_messaggi_restano_nei_metadati(archivio, monkeypatch):
    """La rete che rende la conversione senza rischi.

    Con i messaggi fuori dai metadati, scrivere i metadati dopo una coda
    fallita vorrebbe dire un file che dichiara una conversazione i cui
    messaggi non esistono da nessuna parte. Finche' la coda non c'e' davvero,
    i metadati continuano a portarsi dietro tutto.
    """
    monkeypatch.setattr(
        session_mod, "_scrivi_messaggi", lambda *a, **k: False
    )
    st = _stato("s1", {"role": "user", "content": "non perdermi"})
    session_mod.save_session(st, force=True)

    meta = json.loads((archivio / "s1.json").read_text(encoding="utf-8"))
    assert meta["messages"] == [{"role": "user", "content": "non perdermi"}]

    # (niente monkeypatch.undo(): porterebbe via anche la cartella finta della
    # fixture. La lettura non passa da ``_scrivi_messaggi``.)
    session_mod.dimentica_coda()
    riletto: dict = {}
    assert session_mod.load_session(riletto, "s1")
    assert riletto["messages"][0]["content"] == "non perdermi"


def test_fra_coda_e_metadati_vince_chi_ha_piu_messaggi(archivio):
    """Puo' succedere solo dopo una scrittura fallita, ed e' il caso in cui
    scegliere male costa messaggi persi."""
    st = _stato("s1", {"role": "user", "content": "uno"})
    session_mod.save_session(st, force=True)
    meta = json.loads((archivio / "s1.json").read_text(encoding="utf-8"))
    meta["messages"] = [
        {"role": "user", "content": "uno"},
        {"role": "assistant", "content": "due"},
    ]
    (archivio / "s1.json").write_text(json.dumps(meta), encoding="utf-8")

    session_mod.dimentica_coda()
    riletto: dict = {}
    session_mod.load_session(riletto, "s1")
    assert [m["content"] for m in riletto["messages"]] == ["uno", "due"]


def test_una_riga_tronca_sparisce_dal_file_al_salvataggio_dopo(tmp_path, monkeypatch):
    """Scartata alla lettura, ma sul disco restava.

    La riga rotta la lascia un processo morto durante l'append. La lettura la
    salta -- giusto -- ma poi registrava «risultano scritti N messaggi», e la
    scrittura successiva accodava **dopo** la riga rotta: quella restava lì per
    sempre, e ogni lettura futura pagava lo stesso scarto.
    """
    from core import session as sess

    monkeypatch.setattr(sess, "DATA_DIR", tmp_path)
    sess.ensure_dirs()
    sess.dimentica_coda()

    stato = {
        "current_session_id": "s1",
        "title": "Prova",
        "messages": [{"role": "user", "content": "uno"}],
    }
    sess.save_session(stato, force=True)
    coda = sess.messages_path("s1")
    with open(coda, "a", encoding="utf-8") as fh:
        fh.write('{"role": "assistant", "cont')      # morto a metà append

    sess.dimentica_coda()
    riletti = sess._leggi_messaggi("s1")
    assert len(riletti) == 1, "la riga rotta non è stata scartata"

    stato["messages"] = [*riletti, {"role": "assistant", "content": "due"}]
    sess.save_session(stato, force=True)

    righe = [r for r in coda.read_text(encoding="utf-8").splitlines() if r.strip()]
    assert len(righe) == 2, righe
    for r in righe:
        json.loads(r)     # solleva se la riga rotta è ancora lì
