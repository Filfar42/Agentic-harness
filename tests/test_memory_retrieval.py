"""Il recupero trova evidenze nel corpo e conserva i riferimenti ancora vivi."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core import deposito, libreria


def _archivia(base, corpo, titolo="lavoro archiviato"):
    voce = libreria.archivia(base, riassunto=corpo, richieste=[titolo])
    assert voce is not None
    return voce


def _recupera(base, query):
    return libreria.precarico(base, libreria.voci(base), query)


def test_match_solo_nel_corpo_anche_dopo_molto_testo(tmp_path):
    voce = _archivia(
        tmp_path, "rumore ripetuto. " * 4000 + "\nLa soglia zirconio e' 731 millisecondi.\n",
    )
    testo = _recupera(tmp_path, "ricontrolla zirconio")
    assert voce.percorso in testo
    assert "La soglia zirconio e' 731 millisecondi." in testo
    assert "troncato" in testo and "read_file" in testo
    assert len(testo) <= libreria.MAX_PRECARICO_CHARS


def test_anche_le_voci_uscite_dall_indice_visibile_vengono_cercate(tmp_path):
    vecchia = _archivia(tmp_path, "Lo handshake quarzite usa TLS mutuale.")
    for i in range(libreria.MAX_VOCI_INDICE + 3):
        _archivia(tmp_path, f"un fatto diverso {i}", titolo=f"ciclo {i}")
    elenco = libreria.voci(tmp_path)
    assert vecchia.percorso not in libreria.render_block(elenco)
    assert "TLS mutuale" in libreria.precarico(tmp_path, elenco, "handshake quarzite")


def test_un_match_nel_titolo_precede_una_menzione_incidentale(tmp_path):
    mirata = _archivia(tmp_path, "valore principale 37", "quarzite")
    _archivia(tmp_path, "menzione accessoria di quarzite")
    testo = _recupera(tmp_path, "quarzite")
    assert testo.index(mirata.percorso) < testo.index("menzione accessoria")


def test_percorsi_e_simboli_esatti_hanno_piu_peso(tmp_path):
    mirata = _archivia(tmp_path, "Modificare core/config.py e budgets_for per il difetto.")
    _archivia(tmp_path, "una nota generica", "core config budgets for")
    testo = _recupera(tmp_path, "core/config.py budgets_for")
    assert testo.index(mirata.percorso) < testo.index("una nota generica")
    assert "budgets_for" in testo


def test_separatori_windows_e_posix_individuano_lo_stesso_percorso(tmp_path):
    voce = _archivia(tmp_path, r"Il problema e' in core\config.py: il valore e' 72.")
    assert voce.percorso in _recupera(tmp_path, "core/config.py")


def test_la_ricerca_non_confonde_una_parola_con_un_prefisso(tmp_path):
    _archivia(tmp_path, "configurazione applicata")
    assert _recupera(tmp_path, "config") == ""


def test_le_copie_non_occupano_entrambi_i_posti(tmp_path):
    prima = _archivia(tmp_path, "zirconio valore 97", "primo compito")
    seconda = _archivia(tmp_path, "zirconio valore 97", "secondo compito")
    distinta = _archivia(tmp_path, "zirconio altro dettaglio 51", "terzo compito")
    testo = _recupera(tmp_path, "zirconio")
    assert seconda.percorso in testo and distinta.percorso in testo
    assert prima.percorso not in testo
    assert testo.count("zirconio valore 97") == 1


def test_budget_include_buste_e_riferimenti(tmp_path, monkeypatch):
    monkeypatch.setattr(libreria, "MAX_PRECARICO_CHARS", 800)
    for i in range(3):
        _archivia(tmp_path, f"quarzite {i} " * 800, "un titolo ampio " * 6)
    testo = _recupera(tmp_path, "quarzite")
    assert len(testo) <= 800
    assert testo.count("### .memoria/") == 2
    assert testo.endswith("</libreria_ripescata>")


def test_file_anomali_non_forzano_una_lettura_integrale(tmp_path, monkeypatch):
    monkeypatch.setattr(libreria, "MAX_FILE_RICERCA_CHARS", 300)
    _archivia(tmp_path, "spazio " * 400 + "zirconio")
    assert _recupera(tmp_path, "zirconio") == ""


def test_budget_globale_di_scansione(tmp_path, monkeypatch):
    _archivia(tmp_path, "zirconio vecchio")
    _archivia(tmp_path, "rumore " * 400)
    monkeypatch.setattr(libreria, "MAX_RICERCA_CHARS", 300)
    assert _recupera(tmp_path, "zirconio") == ""


def test_elenco_duplicato_non_duplica_il_precarico(tmp_path):
    voce = _archivia(tmp_path, "zirconio valore 731")
    testo = libreria.precarico(tmp_path, [voce, voce], "zirconio")
    assert testo.count("### .memoria/") == 1


def test_un_nome_manipolato_non_legge_fuori_dalla_memoria(tmp_path):
    (tmp_path / "segreto.md").write_text("zirconio fuori schedario", encoding="utf-8")
    libreria.cartella(tmp_path).mkdir()
    elenco = [libreria.Voce(1, "../segreto.md", "zirconio")]
    assert libreria.precarico(tmp_path, elenco, "zirconio") == ""


def test_nome_con_nul_non_interrompe_il_turno(tmp_path):
    libreria.cartella(tmp_path).mkdir()
    voce = libreria.Voce(1, "001-\0.md", "zirconio")
    assert libreria.precarico(tmp_path, [voce], "zirconio") == ""


def test_equivalenze_unicode_non_rompono_il_punteggio(tmp_path):
    voce = _archivia(tmp_path, "Il servizio e' installato a İSTANBUL.")
    assert voce.percorso in _recupera(tmp_path, "istanbul")


@pytest.mark.parametrize("modulo", [libreria, deposito])
def test_guardia_symlink_di_cartella_senza_privilegi_os(modulo, tmp_path, monkeypatch):
    """Esegue il ramo di rifiuto anche su Windows senza creazione symlink."""
    cart = modulo.cartella(tmp_path)
    cart.mkdir()
    originale = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda p: p == cart or originale(p))
    if modulo is libreria:
        assert modulo.voci(tmp_path) == []
        assert modulo.archivia(tmp_path, riassunto="nuovo", richieste=[]) is None
    else:
        assert modulo.occupazione(tmp_path) == 0
        assert modulo.pota(tmp_path, 1) == 0
        assert modulo.deposita(tmp_path, "nuovo", etichetta="prova") is None
    assert list(cart.iterdir()) == []


def test_guardia_symlink_del_singolo_file_senza_privilegi_os(tmp_path, monkeypatch):
    voce = _archivia(tmp_path, "zirconio valore 731")
    archivio = tmp_path / voce.percorso
    originale = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda p: p == archivio or originale(p))
    assert libreria.voci(tmp_path) == []
    assert libreria.precarico(tmp_path, [voce], "zirconio") == ""


def _link(link: Path, target: Path):
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError as exc:
        pytest.skip(f"Symlink non disponibili: {exc}")


def test_memoria_non_legge_o_scrive_attraverso_symlink(tmp_path):
    esterna = tmp_path / "esterna"
    esterna.mkdir()
    (esterna / "001-segreto.md").write_text("# zirconio\nsegreto", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _link(libreria.cartella(workspace), esterna)
    assert libreria.voci(workspace) == []
    assert libreria.archivia(workspace, riassunto="nuovo", richieste=[]) is None
    assert len(list(esterna.iterdir())) == 1


def test_anche_i_file_di_memoria_symlink_sono_esclusi(tmp_path):
    esterna = tmp_path / "esterna.md"
    esterna.write_text("# zirconio\nsegreto", encoding="utf-8")
    libreria.cartella(tmp_path).mkdir()
    _link(libreria.cartella(tmp_path) / "001-link.md", esterna)
    assert libreria.voci(tmp_path) == []
    voce = libreria.Voce(1, "001-link.md", "zirconio")
    assert libreria.precarico(tmp_path, [voce], "zirconio") == ""


@pytest.mark.parametrize("modulo", [libreria, deposito])
def test_junction_del_workspace_non_e_un_archivio(modulo, tmp_path, monkeypatch):
    cart = modulo.cartella(tmp_path)
    cart.mkdir()
    (cart / "001-esempio.txt").write_text("x" * 1_100_000, encoding="utf-8")
    (cart / "001-esempio.md").write_text("# zirconio\nsegreto", encoding="utf-8")
    originale = Path.is_junction
    monkeypatch.setattr(Path, "is_junction", lambda p: p == cart or originale(p))
    if modulo is libreria:
        assert modulo.voci(tmp_path) == []
        assert modulo.archivia(tmp_path, riassunto="nuovo", richieste=[]) is None
    else:
        assert modulo.occupazione(tmp_path) == 0
        assert modulo.pota(tmp_path, 1) == 0
        assert modulo.deposita(tmp_path, "nuovo", etichetta="prova") is None
    assert len(list(cart.iterdir())) == 2


def test_il_blocco_descrive_l_archivio_del_workspace(tmp_path):
    _archivia(tmp_path, "ricordo")
    testo = libreria.render_block(libreria.voci(tmp_path))
    assert "workspace" in testo and "altre conversazioni" in testo


def test_potatura_preserva_handle_relativi_e_assoluti_anche_sopra_tetto(tmp_path):
    riferimenti = []
    for i in range(3):
        riferimento = deposito.deposita(tmp_path, "x" * 600_000, etichetta=f"file{i}")
        assert riferimento is not None
        riferimenti.append(riferimento)
        os.utime(tmp_path / riferimento, (100 + i, 100 + i))
    protetti = (p for p in [riferimenti[0], tmp_path / riferimenti[1]])
    assert deposito.pota(tmp_path, 1, protetti=protetti) == 1
    assert all((tmp_path / p).exists() for p in riferimenti[:2])
    assert not (tmp_path / riferimenti[2]).exists()
    assert deposito.occupazione(tmp_path) == 1_200_000


def test_un_handle_esterno_o_malformato_non_protegge_omonimi(tmp_path):
    riferimento = deposito.deposita(tmp_path, "x" * 1_100_000, etichetta="prova")
    protetti = [tmp_path / "altro" / Path(riferimento).name, "\0", None]
    assert deposito.pota(tmp_path, 1, protetti=protetti) == 1
    assert not (tmp_path / riferimento).exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows risolve i nomi senza distinguere maiuscole")
def test_handle_con_maiuscole_diverse_protegge_su_windows(tmp_path):
    riferimento = deposito.deposita(tmp_path, "x" * 1_100_000, etichetta="prova")
    assert deposito.pota(tmp_path, 1, protetti=[riferimento.upper()]) == 0
    assert (tmp_path / riferimento).exists()


def test_file_scomparso_non_blocca_pulizia_degli_altri(tmp_path, monkeypatch):
    primo = deposito.deposita(tmp_path, "x" * 1_100_000, etichetta="primo")
    secondo = deposito.deposita(tmp_path, "y" * 1_100_000, etichetta="secondo")
    sparito = tmp_path / primo
    originale = Path.lstat

    def lstat(path):
        if path == sparito:
            raise FileNotFoundError(path)
        return originale(path)

    monkeypatch.setattr(Path, "lstat", lstat)
    assert deposito.pota(tmp_path, 1) == 1
    assert not (tmp_path / secondo).exists()


def test_deposito_symlink_non_viene_potato_o_modificato(tmp_path):
    esterna = tmp_path / "esterna"
    esterna.mkdir()
    prova = esterna / "001-prova.txt"
    prova.write_text("x" * 1_100_000, encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _link(deposito.cartella(workspace), esterna)
    assert deposito.pota(workspace, 1) == 0
    assert deposito.occupazione(workspace) == 0
    assert deposito.deposita(workspace, "nuovo", etichetta="prova") is None
    assert prova.exists() and len(list(esterna.iterdir())) == 1


def test_file_symlink_non_contribuisce_alla_potatura(tmp_path):
    esterna = tmp_path / "esterna.txt"
    esterna.write_text("x" * 1_100_000, encoding="utf-8")
    deposito.cartella(tmp_path).mkdir()
    link = deposito.cartella(tmp_path) / "001-prova.txt"
    _link(link, esterna)
    assert deposito.occupazione(tmp_path) == 0
    assert deposito.pota(tmp_path, 1) == 0
    assert link.is_symlink() and esterna.exists()
