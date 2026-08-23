"""Due guardie nate dalla stessa sessione reale.

Prompt: "Analizza la complessita' di media_mobile_pesata, individua i problemi
di precisione, suggerisci come ottimizzarla". Tre verbi, tutti di sola lettura.
Il modello ha scritto l'analisi, poi ha implementato di sua iniziativa una
`media_mobile_pesata_stream` sbagliata su tutti e tre i casi di riferimento,
non le ha scritto un test, ha lanciato pytest su un altro file, ha visto
"10 passed" e ha dichiarato "lavoro completato".
"""

from __future__ import annotations

import json

import pytest

from core.agent import answered_question_pending
from core.tools import (
    ToolContext,
    dispatch,
    looks_like_readonly_request,
    public_symbols,
    uncovered_symbols,
)


def esito(risposta: str) -> dict:
    return json.loads(risposta)


# --- richieste di sola lettura -----------------------------------------------


def test_riconosce_la_richiesta_analitica_reale():
    assert looks_like_readonly_request(
        "Analizza la complessità computazionale (tempo e spazio) della funzione "
        "media_mobile_pesata e individua eventuali problemi di precisione "
        "aritmetica. Suggerisci come ottimizzarla per uno streaming in tempo reale."
    )
    assert looks_like_readonly_request("Cosa fa la funzione scarti?")
    assert looks_like_readonly_request("Spiegami come funziona la sandbox")
    assert looks_like_readonly_request("Valuta la qualità di questo modulo")


def test_non_blocca_quando_l_utente_vuole_davvero_una_modifica():
    """Il falso positivo e' il danno vero: interromperebbe lavoro legittimo."""
    assert not looks_like_readonly_request("Analizza il codice e correggi i bug")
    assert not looks_like_readonly_request(
        "Spiega cosa fa questa funzione, poi ottimizzala"
    )
    assert not looks_like_readonly_request("In telemetria.py ci sono tre bug, falli passare")
    assert not looks_like_readonly_request("Aggiungi una funzione media_mobile_pesata")
    assert not looks_like_readonly_request("")


def test_l_infinito_non_e_un_ordine():
    """'suggerisci come ottimizzarla' e' il caso che ha rotto tutto: il verbo di
    modifica c'e', ma all'infinito, come oggetto del suggerimento."""
    assert looks_like_readonly_request("Suggerisci come ottimizzarla")
    assert not looks_like_readonly_request("Dimmi cosa non va e poi ottimizzala")


def test_durante_un_analisi_i_tool_di_scrittura_rifiutano(tmp_path):
    ctx = ToolContext(workspace=str(tmp_path), readonly_request=True)
    out = esito(dispatch(ctx, "write_file", {"filepath": "nuovo.py", "content": "x = 1\n"}))
    assert out.get("error"), out
    assert "analizzare" in out["error"]
    assert "ask_user_question" in out["hint"]
    assert not (tmp_path / "nuovo.py").exists()


def test_la_risposta_dell_utente_scioglie_il_vincolo():
    """L'uscita di sicurezza e' chiedere: se l'utente dice di procedere, il
    divieto deve cadere, anche se il suo ultimo messaggio visibile e' ancora
    quello analitico di prima."""
    storia = [{"role": "user", "content": "Analizza la funzione"}]
    assert not answered_question_pending(storia)

    storia.append({"role": "tool", "name": "ask_user_question", "answered": True,
                   "content": '{"user_answer": "sì, implementala"}'})
    assert answered_question_pending(storia)

    # Un nuovo messaggio dell'utente ricomincia da capo.
    storia.append({"role": "user", "content": "Adesso spiegami i test"})
    assert not answered_question_pending(storia)


# --- verifica vacua -----------------------------------------------------------


def test_estrae_i_simboli_pubblici():
    src = "def pubblica():\n    pass\n\nclass Cosa:\n    pass\n\ndef _privata():\n    pass\n"
    assert public_symbols(src) == {"pubblica", "Cosa"}


def test_una_funzione_nuova_senza_test_viene_segnalata(tmp_path):
    """Il caso reale: media_mobile_pesata_stream aggiunta, mai testata, e
    `pytest test_media_mobile.py` verde perche' misura un'altra funzione."""
    ctx = ToolContext(workspace=str(tmp_path))
    (tmp_path / "telemetria.py").write_text("def media_mobile_pesata():\n    pass\n", encoding="utf-8")
    (tmp_path / "test_media_mobile.py").write_text(
        "from telemetria import media_mobile_pesata\n\n"
        "def test_x():\n    assert media_mobile_pesata() is None\n",
        encoding="utf-8",
    )
    dispatch(ctx, "read_file", {"filepath": "telemetria.py"})
    dispatch(ctx, "edit_file", {
        "filepath": "telemetria.py",
        "old_string": "def media_mobile_pesata():\n    pass",
        "new_string": "def media_mobile_pesata():\n    pass\n\n\ndef media_mobile_pesata_stream():\n    pass",
    })

    assert ctx.new_symbols == {"media_mobile_pesata_stream": "telemetria.py"}
    assert uncovered_symbols(ctx) == [("media_mobile_pesata_stream", "telemetria.py")]


def test_una_funzione_nuova_gia_testata_non_viene_segnalata(tmp_path):
    ctx = ToolContext(workspace=str(tmp_path))
    dispatch(ctx, "write_file", {"filepath": "mod.py", "content": "def nuova():\n    return 2\n"})
    assert uncovered_symbols(ctx) == [("nuova", "mod.py")]

    dispatch(ctx, "write_file", {
        "filepath": "test_mod.py",
        "content": "from mod import nuova\n\ndef test_nuova():\n    assert nuova() == 2\n",
    })
    assert uncovered_symbols(ctx) == []


def test_i_simboli_dei_file_di_test_non_contano(tmp_path):
    """Le funzioni definite dentro un test non sono codice da coprire."""
    ctx = ToolContext(workspace=str(tmp_path))
    dispatch(ctx, "write_file", {
        "filepath": "test_cose.py",
        "content": "def test_qualcosa():\n    assert True\n",
    })
    assert ctx.new_symbols == {}
    assert uncovered_symbols(ctx) == []


@pytest.mark.parametrize("nome", ["_interna", "__dunder__"])
def test_i_simboli_privati_non_contano(tmp_path, nome):
    ctx = ToolContext(workspace=str(tmp_path))
    dispatch(ctx, "write_file", {"filepath": "mod.py", "content": f"def {nome}():\n    pass\n"})
    assert ctx.new_symbols == {}


# --- il banco di prova --------------------------------------------------------


def test_dentro_analisi_si_puo_scrivere_anche_in_sola_lettura(tmp_path):
    """Vietare tutto impedisce il danno ma non l'errore: il modello proponeva
    codice mai eseguito. Qui ha dove provarlo."""
    ctx = ToolContext(workspace=str(tmp_path), sandbox="host", readonly_request=True)

    bloccato = esito(dispatch(ctx, "write_file", {"filepath": "vero.py", "content": "x = 1\n"}))
    assert bloccato.get("error")
    assert ".analisi/" in bloccato["hint"]

    ok = esito(dispatch(ctx, "write_file", {
        "filepath": ".analisi/prova.py",
        "content": "print(sum([1, 2, 3]))\n",
    }))
    assert ok.get("error") is None, ok
    assert (tmp_path / ".analisi" / "prova.py").exists()


def test_il_banco_di_prova_si_esclude_da_solo_da_git(tmp_path):
    ctx = ToolContext(workspace=str(tmp_path), sandbox="host", readonly_request=True)
    dispatch(ctx, "write_file", {"filepath": ".analisi/x.py", "content": "pass\n"})
    assert (tmp_path / ".analisi" / ".gitignore").read_text(encoding="utf-8").strip() == "*"


def test_il_codice_di_prova_si_puo_eseguire(tmp_path):
    """Il punto di tutto: la proposta deve poter girare prima di essere scritta."""
    ctx = ToolContext(workspace=str(tmp_path), sandbox="host", readonly_request=True)
    dispatch(ctx, "write_file", {
        "filepath": ".analisi/prova.py",
        "content": "print(round(sum(v * p for v, p in zip([10, 20, 30], [0.25, 0.5, 1])) / 1.75, 3))\n",
    })
    out = esito(dispatch(ctx, "run_command", {"command": "python .analisi/prova.py"}))
    assert out["esito"] == "ok", out
    assert "24.286" in out["stdout"]


def test_gli_scarti_non_risultano_file_del_progetto(tmp_path):
    ctx = ToolContext(workspace=str(tmp_path), sandbox="host", readonly_request=True)
    dispatch(ctx, "write_file", {"filepath": ".analisi/bozza.py", "content": "def idea():\n    pass\n"})
    assert ctx.touched_files == set()      # niente nel pannello dei file toccati
    assert ctx.new_symbols == {}           # e niente da coprire con dei test


def test_un_test_nel_banco_di_prova_non_copre_il_progetto(tmp_path):
    """Altrimenti basterebbe una bozza per zittire il controllo di copertura."""
    ctx = ToolContext(workspace=str(tmp_path), sandbox="host")
    dispatch(ctx, "write_file", {"filepath": "mod.py", "content": "def nuova():\n    return 1\n"})
    dispatch(ctx, "write_file", {
        "filepath": ".analisi/test_finto.py",
        "content": "def test_x():\n    assert nuova() == 1\n",
    })
    assert uncovered_symbols(ctx) == [("nuova", "mod.py")]


def test_il_banco_riparte_vuoto_ad_ogni_analisi(tmp_path):
    from core.tools import reset_scratch

    ctx = ToolContext(workspace=str(tmp_path), sandbox="host", readonly_request=True)
    dispatch(ctx, "write_file", {"filepath": ".analisi/vecchio.py", "content": "pass\n"})
    assert (tmp_path / ".analisi" / "vecchio.py").exists()

    reset_scratch(ctx)
    assert not (tmp_path / ".analisi").exists()
    assert list(tmp_path.iterdir()) == [], "non deve aver cancellato altro"


def test_reset_non_segue_un_collegamento_fuori_dal_workspace(tmp_path):
    """Qui si cancella ricorsivamente: il controllo sul percorso non e' pedanteria."""
    from core.tools import reset_scratch

    prezioso = tmp_path / "roba-importante"
    prezioso.mkdir()
    (prezioso / "non-perdere.txt").write_text("dati", encoding="utf-8")

    ws = tmp_path / "ws"
    ws.mkdir()
    try:
        (ws / ".analisi").symlink_to(prezioso, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink non disponibili su questo sistema")

    reset_scratch(ToolContext(workspace=str(ws)))
    assert (prezioso / "non-perdere.txt").exists()


def test_normalise_rel_non_mangia_il_punto_della_cartella():
    """Regressione: lstrip('./') toglie *caratteri*, non un prefisso, e su
    '.analisi/x.py' si portava via anche il punto -- il banco di prova non
    veniva mai riconosciuto e ogni scrittura veniva rifiutata."""
    from core.tools import is_scratch_path, normalise_rel

    assert normalise_rel(".analisi/x.py") == ".analisi/x.py"
    assert normalise_rel("./src/mod.py") == "src/mod.py"
    # Su Windows '.\analisi' e' 'analisi' nella cartella corrente, non '.analisi'.
    assert normalise_rel(".\\analisi\\x.py") == "analisi/x.py"
    assert normalise_rel(".\\.analisi\\x.py") == ".analisi/x.py"
    assert is_scratch_path(".analisi/x.py")
    assert is_scratch_path("./.analisi/sotto/x.py")
    assert not is_scratch_path("analisi/x.py")
    assert not is_scratch_path("src/.analisi-finta/x.py")


def test_una_prova_puo_importare_i_moduli_del_progetto(tmp_path):
    """Senza questo il banco e' inutile: Python mette in testa a sys.path la
    cartella *dello script*, non quella da cui l'hai lanciato, quindi una prova
    in `.analisi/` non vedrebbe niente del progetto."""
    ctx = ToolContext(workspace=str(tmp_path), sandbox="host", readonly_request=True)
    (tmp_path / "telemetria.py").write_text("def raddoppia(x):\n    return x * 2\n", encoding="utf-8")

    dispatch(ctx, "write_file", {
        "filepath": ".analisi/prova.py",
        "content": "from telemetria import raddoppia\nprint(raddoppia(21))\n",
    })
    out = esito(dispatch(ctx, "run_command", {"command": "python .analisi/prova.py"}))
    assert out["esito"] == "ok", out
    assert "42" in out["stdout"]


def test_la_prova_smaschera_la_proposta_sbagliata(tmp_path):
    """Il caso reale, in miniatura: il codice che il modello aveva proposto in
    risposta -- pesi zippati in ordine cronologico, quindi peso 1 al valore piu'
    vecchio -- non coincide con la funzione vera. Eseguirlo lo dice subito."""
    ctx = ToolContext(workspace=str(tmp_path), sandbox="host", readonly_request=True)
    (tmp_path / "telemetria.py").write_text(
        "def pesata(serie, finestra, decadimento):\n"
        "    pesi = [decadimento ** (finestra - 1 - k) for k in range(finestra)]\n"
        "    return [round(sum(v * p for v, p in zip(serie[j - finestra + 1:j + 1], pesi))\n"
        "                  / sum(pesi), 3) for j in range(finestra - 1, len(serie))]\n",
        encoding="utf-8",
    )
    dispatch(ctx, "write_file", {
        "filepath": ".analisi/prova.py",
        "content": (
            "from telemetria import pesata\n"
            "def proposta(serie, finestra, d):\n"
            "    pesi = [d ** i for i in range(finestra)]\n"
            "    return [round(sum(a * b for a, b in zip(serie[j - finestra + 1:j + 1], pesi))\n"
            "                  / sum(pesi), 3) for j in range(finestra - 1, len(serie))]\n"
            "s = [10, 20, 30, 40, 50]\n"
            "assert proposta(s, 3, 0.5) == pesata(s, 3, 0.5)\n"
        ),
    })
    out = esito(dispatch(ctx, "run_command", {"command": "python .analisi/prova.py"}))
    assert out["esito"] == "FALLITO"
    assert "AssertionError" in out["stderr"]
