"""Il ciclo del rosso ostinato, e il piano su una richiesta scritta in prosa.

Entrambi nascono dalla stessa sessione misurata: trenta passi, nessun piano, e
diciassette di quei passi spesi contro una singola asserzione impossibile.

L'asserzione era, in un file di verifica scritto dall'agente stesso::

    assert rows[-1][0] + ".txt" in md

dove ``rows[-1][0]`` valeva gia' ``"marte.txt"``: il test cercava
``marte.txt.txt``, che non poteva esserci. Il rapporto era corretto dalla prima
stesura. Il modello lo ha riscritto cinque volte e ha riletto il sorgente del
test tre volte senza vedere il ``+ ".txt"`` -- anche perche' l'harness, ad ogni
rosso, gli ripeteva "correggi il codice e riesegui, non passare ad altro".
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.agent import (  # noqa: E402
    MIN_ARTEFATTI,
    artefatti_nominati,
    looks_multi_step,
)
from core.tools import (  # noqa: E402
    ROSSI_PRIMA_DI_DUBITARE,
    ToolContext,
    dubita_della_verifica,
)


# ---------------------------------------------------------------------------
# Dubitare della propria verifica
# ---------------------------------------------------------------------------

USCITA_ASSERZIONE = "FAIL: test_rapporto_md: File piu' lungo non menzionato\n"
COMANDO = "python3 cantiere/prova.py"


def ctx_con_rossi(
    tmp_path, quanti: int, *, scritto="cantiere/prova.py", comando=COMANDO
) -> ToolContext:
    ctx = ToolContext(workspace=str(tmp_path))
    if scritto:
        ctx.touched_files.add(scritto)
    ctx.comandi_falliti[comando] = quanti
    return ctx


def test_i_primi_due_rossi_non_fanno_dubitare(tmp_path):
    """Sui primi tentativi il consiglio giusto resta 'correggi il codice', ed e'
    quello che salva la maggior parte dei casi. Il dubbio sul test e' l'ultima
    ipotesi, non la prima: anticiparlo autorizzerebbe la scorciatoia peggiore."""
    for quanti in (1, ROSSI_PRIMA_DI_DUBITARE - 1):
        ctx = ctx_con_rossi(tmp_path, quanti)
        assert dubita_della_verifica(ctx, COMANDO, USCITA_ASSERZIONE) is None


def test_al_terzo_rosso_il_messaggio_cambia(tmp_path):
    ctx = ctx_con_rossi(tmp_path, ROSSI_PRIMA_DI_DUBITARE)
    messaggio = dubita_della_verifica(ctx, COMANDO, USCITA_ASSERZIONE)
    assert messaggio is not None
    assert "cantiere/prova.py" in messaggio
    assert "l'hai scritto tu" in messaggio
    # deve dire *cosa* fare, non solo che c'e' un dubbio
    assert "CARATTERE PER CARATTERE" in messaggio


def test_vale_anche_per_un_file_che_non_si_chiama_test(tmp_path):
    """E' il caso che ha motivato la guardia: lo script si chiamava `prova.py`,
    quindi non finiva in ``authored_tests``, che riconosce i test dal nome.
    Quello che conta non e' come si chiama: e' che l'ha scritto lui."""
    ctx = ctx_con_rossi(tmp_path, ROSSI_PRIMA_DI_DUBITARE)
    assert not ctx.authored_tests
    assert dubita_della_verifica(ctx, COMANDO, USCITA_ASSERZIONE) is not None


def test_il_file_si_riconosce_anche_solo_dall_uscita(tmp_path):
    """`pytest -q` non nomina il file nel comando: lo nomina nel rapporto."""
    ctx = ctx_con_rossi(
        tmp_path, ROSSI_PRIMA_DI_DUBITARE,
        scritto="tests/test_slug.py", comando="pytest -q",
    )
    messaggio = dubita_della_verifica(
        ctx, "pytest -q", "FAILED tests/test_slug.py::test_accenti - AssertionError"
    )
    assert messaggio is not None and "tests/test_slug.py" in messaggio


@pytest.mark.parametrize(
    "uscita",
    [
        "ModuleNotFoundError: No module named 'csv2'",
        "  File \"x.py\", line 3\n    def (\nSyntaxError: invalid syntax",
        "Connection refused",
    ],
)
def test_su_un_errore_che_non_e_un_asserzione_non_si_dubita(tmp_path, uscita):
    """Su un ImportError non c'e' nessuna asserzione da rileggere: il consiglio
    resterebbe fuori bersaglio quanto quello che sostituisce."""
    ctx = ctx_con_rossi(tmp_path, ROSSI_PRIMA_DI_DUBITARE + 2)
    assert dubita_della_verifica(ctx, COMANDO, uscita) is None


def test_su_un_file_che_non_ha_scritto_lui_non_si_dubita(tmp_path):
    """Le asserzioni dell'utente sono la specifica: metterle in dubbio e'
    esattamente il contrario di quello che deve fare."""
    ctx = ctx_con_rossi(tmp_path, ROSSI_PRIMA_DI_DUBITARE + 5, scritto=None)
    assert dubita_della_verifica(ctx, COMANDO, USCITA_ASSERZIONE) is None


def test_il_contatore_si_azzera_quando_torna_verde(tmp_path):
    """Altrimenti un comando riparato e poi rotto di nuovo per un motivo
    diverso partirebbe gia' con il sospetto addosso."""
    from core.tools import tool_run_command

    ctx = ToolContext(workspace=str(tmp_path), sandbox="host", timeout_s=20)
    for _ in range(ROSSI_PRIMA_DI_DUBITARE):
        tool_run_command(ctx, "python3 -c \"raise SystemExit(1)\"")
    assert ctx.comandi_falliti["python3 -c \"raise SystemExit(1)\""] == ROSSI_PRIMA_DI_DUBITARE

    esito = json.loads(tool_run_command(ctx, "python3 -c \"pass\""))
    assert esito["esito"] == "ok"
    assert "python3 -c \"pass\"" not in ctx.comandi_falliti


# ---------------------------------------------------------------------------
# Il piano su una richiesta che non ordina, descrive
# ---------------------------------------------------------------------------

# La richiesta vera della sessione: nessun elenco numerato, nessun verbo
# all'imperativo in fila, e otto artefatti da consegnare.
PROSA = """Nel workspace deve esistere una cartella `cantiere/`. Quando avrai finito
voglio trovarci dentro una piccola pipeline che si regge da sola.

Dentro `cantiere/grezzi/` quattro file di testo intitolati a un pianeta.
`cantiere/misura.py`, chiamato senza argomenti dalla radice del workspace,
guarda dentro quell'archivio e ne ricava `cantiere/misure.csv`.
`cantiere/rapporto.md` racconta quanti file ci sono e il totale delle righe.
Se un file sta oltre il doppio della mediana, in `cantiere/lunghi/` ci sara'
un omonimo `.txt`. `cantiere/prova.py` mette alla prova tutto questo.
`cantiere/LEGGIMI.md` chiude, con due righe su come si rifa' tutto da zero."""


def test_gli_artefatti_bastano_a_far_scattare_il_piano():
    """Il buco misurato: prosa che descrive un risultato invece di ordinare
    azioni passava sotto i due segnali esistenti senza toccarli."""
    assert len(artefatti_nominati(PROSA)) >= MIN_ARTEFATTI
    assert looks_multi_step(PROSA)


def test_un_artefatto_nominato_due_volte_non_conta_due_volte():
    """`cantiere/lunghi/` e `lunghi/` sono la stessa cartella, e `cantiere/`
    accanto a `cantiere/misura.py` e' il contenitore di una cosa gia' contata."""
    trovati = artefatti_nominati(
        "in `cantiere/` c'e' `cantiere/misura.py`, e `cantiere/lunghi/` "
        "cioe' la cartella `lunghi/`"
    )
    assert trovati == {"cantiere/misura.py", "cantiere/lunghi/"}


@pytest.mark.parametrize(
    "richiesta",
    [
        # un obiettivo solo, per quanto lungo sia il testo
        "Leggi core/agent.py e spiegami come funziona il ciclo agentico, "
        "soffermandoti su come vengono gestiti gli errori dei tool e su cosa "
        "succede quando il modello smette di rispondere a meta' di un passo. "
        "Non modificare niente, voglio solo capire.",
        # un bug in un file, con la sua verifica: e' un lavoro solo
        "Nel file web/app.js c'e' un bug: quando si trascina il divisorio "
        "dell'anteprima oltre il bordo destro la larghezza diventa negativa e "
        "il pannello sparisce del tutto. Correggilo e controlla che il doppio "
        "clic continui a comportarsi come prima.",
    ],
)
def test_niente_piano_dove_l_obiettivo_e_uno(richiesta):
    """Un falso positivo costa un giro di manage_plan per niente: la soglia sta
    a quattro artefatti proprio per lasciar passare questi."""
    assert len(richiesta) >= 220, "il caso va provato sopra la soglia di lunghezza"
    assert not looks_multi_step(richiesta)


def test_le_radici_d_azione_si_cercano_a_inizio_parola():
    """'gestendo' conteneva 'estend' e 'documento' contiene 'document': il
    confronto per sottostringa faceva scattare il sollecito su richieste con un
    obiettivo solo."""
    richiesta = (
        "Scrivi un modulo core/slug.py con una funzione che trasforma un titolo "
        "in uno slug URL-safe, gestendo accenti e punteggiatura, e il suo "
        "tests/test_slug.py con qualche caso limite. Poi lancia la suite per "
        "controllare che passi tutto quanto senza errori."
    )
    assert len(richiesta) >= 220
    assert not looks_multi_step(richiesta)
