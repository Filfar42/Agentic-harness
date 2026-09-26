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


def menu_js() -> str:
    """Il menu delle impostazioni: schema e controlli (dal 26/09/2026)."""
    return (WEB / "impostazioni.js").read_text(encoding="utf-8")


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
    menu = menu_js()
    assert "select.value = nome" not in sorgente
    # "Applica i consigliati" vive nel menu: i valori li rimette nei campi
    # renderHeader, non una mappa scritta a mano dentro il pulsante.
    profilo = blocco(menu, "async function impApplicaProfilo(")
    assert "'#s-temp" not in profilo and "num_ctx" not in profilo
    assert "renderHeader();" in profilo
    # La terza copia era dentro il pulsante della build, che non esiste più:
    # l'immagine appena costruita la seleziona il **server** (``seleziona_
    # immagine``), quindi non c'è più nessun posto da cui il client possa
    # scriverla a mano e dimenticarsi di riallineare il resto.
    for fonte in (sorgente, menu):
        assert "$('#sandbox-build')" not in fonte
        assert "$('#s-docker-image').value =" not in fonte


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
    # ...ma il numero accanto al cursore deve continuare a muoversi mentre si
    # trascina, e da fermo mostrare il valore salvato, non quello che il passo
    # del cursore ha arrotondato.
    menu = menu_js()
    cursore = menu[menu.index("case 'cursore': {"):]
    cursore = cursore[: cursore.index("case 'modello': {")]
    assert "input.addEventListener('input', () => mostra(input.value))" in cursore
    assert "eco.textContent = testo" in cursore
    assert "state.settings[campo.k]" in cursore


def test_il_modello_scelto_dal_server_arriva_fino_ai_widget():
    """Se il modello configurato non c'e' piu' sull'endpoint, il server ne
    sceglie un altro e lo dichiara in /api/models. Ignorarlo lasciava scritto
    ovunque il nome di un modello che nessuno stava usando."""
    # La sonda e' una sola: la chiama l'avvio, la chiama il menu dopo un
    # cambio di indirizzo o di transport, la chiama "Verifica".
    corpo = blocco(js(), "async function sondaBackend(")
    assert "state.settings.model_name = info.model_name" in corpo
    assert "renderHeader();" in corpo
    ricontrollo = blocco(menu_js(), "async function impRicontrollaServer(")
    assert "await sondaBackend();" in ricontrollo


# ---------------------------------------------------------------------------
# Il prompt: quando lo sceglie l'harness e quando vince l'utente
# ---------------------------------------------------------------------------


def test_il_campo_vuoto_vuol_dire_scegli_tu():
    """Svuotare il prompt e' il gesto di ripristino, e deve funzionare.

    Senza questo, un campo vuoto sarebbe un prompt di sistema vuoto: il
    modello partirebbe senza istruzioni e nessuno capirebbe perche'.
    """
    from core.prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_LEAN, is_stock_prompt

    assert is_stock_prompt("")
    assert is_stock_prompt("   \n ")
    assert is_stock_prompt(SYSTEM_PROMPT)
    assert is_stock_prompt(SYSTEM_PROMPT_LEAN)
    assert not is_stock_prompt("Sei un pirata.")


def test_una_copia_vecchia_nel_campo_blocca_le_modifiche_al_prompt():
    """La trappola silenziosa, messa nero su bianco.

    Il testo salvato nelle preferenze vince sempre. Quando il nostro prompt
    cambia, una copia della versione precedente rimasta nel campo smette di
    combaciare e passa per "personalizzato": da quel momento l'harness non
    sceglie piu' fra esteso e snello, e nessuna modifica fatta in prompts.py
    arriva piu' al modello. Il rimedio e' svuotare il campo -- il test qui
    sopra -- e questo serve a ricordare *perche'* serva un rimedio.
    """
    from core.prompts import SYSTEM_PROMPT, is_stock_prompt

    copia_vecchia = SYSTEM_PROMPT.replace("agente di programmazione", "Senior Software Engineer")
    assert copia_vecchia != SYSTEM_PROMPT
    assert not is_stock_prompt(copia_vecchia), (
        "una copia di un nostro prompt vecchio passa per personalizzata: "
        "e' voluto, ma per questo il campo deve poter tornare vuoto"
    )


def test_le_versioni_di_serie_precedenti_restano_di_serie():
    """Il 25/09 il prompt e' diventato a moduli. Chi aveva salvato la versione
    2.38 identica non l'ha personalizzata: deve continuare a ricevere quella
    nuova, non restare inchiodato alla vecchia."""
    from core.prompts import SYSTEM_PROMPT, is_stock_prompt
    from core.system_prompt import PROMPT_DI_SERIE_PRECEDENTI

    assert PROMPT_DI_SERIE_PRECEDENTI
    for vecchio in PROMPT_DI_SERIE_PRECEDENTI:
        assert vecchio != SYSTEM_PROMPT
        assert is_stock_prompt(vecchio)
        assert is_stock_prompt(vecchio + "\n\n")


def test_ogni_campo_numerico_converte_prima_di_salvare():
    """Un ``<input type="number">`` restituisce una STRINGA.

    Salvarla cosi' funziona per tutta la sessione -- ``int("128")`` non da'
    errore -- e si annulla da sola al riavvio successivo, quando
    ``load_settings`` scarta il valore perche' il tipo non combacia col default
    e rimette il default senza dirlo a nessuno. E' il difetto peggiore da
    diagnosticare: l'utente alza il tetto del deposito, lo vede funzionare,
    riavvia, e ritrova 64.

    Osservato su ``deposito_max_mb``, l'unico campo numerico su quattordici a
    cui mancava ``Number`` -- e il test che copriva questo file verificava il
    *meccanismo* BOUND_FIELDS, non la trasformazione.
    """
    # Dal 26/09/2026 i campi li costruisce lo schema e li lega impLega: la
    # conversione non e' piu' scritta campo per campo ma viene dal tipo del
    # controllo, e il test la esegue davvero (vedi test_menu_impostazioni).
    from core.config import DEFAULTS
    from tests.test_menu_impostazioni import campi, contesto_js

    ctx = contesto_js()
    scoperti = []
    for campo in campi(ctx):
        if campo["tipo"] not in ("numero", "cursore") and not campo["numerico"]:
            continue
        # Il valore come arriva dal controllo: una stringa, dentro i limiti.
        grezzo = str(DEFAULTS[campo["k"]])
        out = ctx.eval(
            f"(() => {{ const c = IMP_CAMPI.find((x) => x.k === '{campo['k']}');"
            f" return typeof impTrasforma(c)('{grezzo}'); }})()"
        )
        if out != "number":
            scoperti.append(campo["k"])
    assert not scoperti, (
        "questi campi numerici si salvano come stringa e torneranno al valore "
        f"di serie al prossimo riavvio, senza avvisare: {scoperti}"
    )
    lega = blocco(menu_js(), "function impLega(")
    assert "impTrasforma(campo)" in lega


def test_la_palette_non_ha_variabili_fantasma():
    """Una custom property non definita e senza ripiego diventa `unset`.

    Per una proprieta' ereditabile come ``color`` significa "eredita", non
    "usa il default": ``--text-dim`` era usata tre volte e definita zero, e le
    righe dei vault nascevano gia' a colore pieno -- quindi :hover e .active,
    che portano a ``var(--text)``, non cambiavano niente di visibile.
    """
    # Tutti i fogli: il menu delle impostazioni (impostazioni.css) usa i token
    # definiti in style.css, e un nome sbagliato li' e' lo stesso difetto.
    css = "\n".join(f.read_text(encoding="utf-8") for f in sorted(WEB.glob("*.css")))
    # Solo gli usi **senza ripiego**: `var(--preview-w, 50%)` e' il modo giusto
    # di leggere una variabile che scrive il JS, e non e' un difetto.
    senza_ripiego = set(re.findall(r"var\(\s*(--[a-z0-9-]+)\s*\)", css))
    definite = set(re.findall(r"^\s*(--[a-z0-9-]+)\s*:", css, re.M))
    fantasma = sorted(senza_ripiego - definite)
    assert not fantasma, (
        "custom property usate senza definizione e senza ripiego: la "
        f"dichiarazione diventa `unset` e il valore viene ereditato: {fantasma}"
    )
