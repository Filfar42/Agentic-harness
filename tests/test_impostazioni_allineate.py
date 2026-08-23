"""Un'impostazione, tutti i posti che la mostrano.

Il difetto osservato: si sceglie il modello dalla goccia in alto a sinistra, il
modello cambia davvero, ma la tendina in Impostazioni continua a mostrare
quello di prima e va riallineata a mano. La causa non e' la riga mancante in un
punto: e' che il riallineamento era scritto **quattro volte**, una per ogni
pulsante che cambiava qualcosa, e ognuna copriva i campi che si ricordava chi
l'aveva scritta. Qui si controlla che sia rimasta una regola sola.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

WEB = Path(__file__).resolve().parents[1] / "web"


def js() -> str:
    return (WEB / "app.js").read_text(encoding="utf-8")


def blocco(sorgente: str, inizio: str, fine: str = "\n}") -> str:
    testo = sorgente[sorgente.index(inizio):]
    return testo[: testo.index(fine) + len(fine)]


def test_i_campi_si_dichiarano_una_volta_sola():
    """Chi mostra un valore lo dice registrandosi, non facendosi ricordare."""
    sorgente = js()
    assert "const BOUND_FIELDS = []" in sorgente
    corpo = blocco(sorgente, "function bindField(")
    assert "BOUND_FIELDS.push({ id, key })" in corpo
    # e il valore iniziale passa dalla stessa funzione del riallineamento
    assert "applyField({ id, key })" in corpo


def test_il_riallineamento_e_uno_solo_e_copre_tutti():
    sorgente = js()
    assert sorgente.count("function syncSettingsWidgets(") == 1
    corpo = blocco(sorgente, "function syncSettingsWidgets(")
    assert "BOUND_FIELDS.forEach" in corpo
    # ogni percorso che cambia un'impostazione passa da renderHeader: e' li'
    # che il riallineamento deve stare, o resta scoperto qualche pulsante
    assert "syncSettingsWidgets();" in blocco(sorgente, "function renderHeader(")


def test_nessuno_riallinea_piu_i_campi_a_mano():
    """Le quattro copie: la riga dentro pickModel, la mappa dentro 'Applica i
    consigliati', l'immagine dentro il pulsante della build."""
    sorgente = js()
    assert "select.value = nome" not in sorgente
    profilo = blocco(sorgente, "$('#profile-apply').onclick")
    assert "'#s-temp'" not in profilo and "num_ctx" not in profilo
    assert "renderHeader();" in profilo
    build = blocco(sorgente, "$('#sandbox-build').onclick")
    assert "$('#s-docker-image').value" not in build


def test_un_valore_fuori_elenco_non_sparisce_in_silenzio():
    """Assegnare a un <select> un valore che non e' fra le sue <option> non fa
    niente e non da' errore: e' il modo esatto in cui un widget resta indietro
    senza che nessuno se ne accorga."""
    corpo = blocco(js(), "function applyField(")
    assert "node.tagName === 'SELECT'" in corpo
    assert "node.appendChild(opt)" in corpo


def test_il_campo_che_si_sta_scrivendo_non_viene_riscritto():
    """Il salvataggio parte ad ogni tasto: riscrivere il valore nel campo con
    il fuoco sposterebbe il cursore in fondo alla riga ad ogni lettera."""
    corpo = blocco(js(), "function syncSettingsWidgets(")
    assert "document.activeElement" in corpo
    # ...ma il numero accanto al cursore deve continuare a muoversi
    assert re.search(r"echo\.textContent = out", js())


def test_il_modello_scelto_dal_server_arriva_fino_ai_widget():
    """Se il modello configurato non c'e' piu' sull'endpoint, il server ne
    sceglie un altro e lo dichiara in /api/models. Ignorarlo lasciava scritto
    ovunque il nome di un modello che nessuno stava usando."""
    sorgente = js()
    corpo = sorgente[sorgente.index("const refreshModels = async ()"):]
    corpo = corpo[: corpo.index("};")]
    assert "state.settings.model_name = data.model_name" in corpo
    assert "renderHeader();" in corpo
