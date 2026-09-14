# Visual refinement — review prima delle modifiche

Perimetro: `web/style.css`, `web/index.html`, riferimenti statici server e test UI. Direzione concordata: graphite + mineral teal, geometria e misure esistenti. Review di sorgente; non sostituisce la verifica visiva nel browser dopo il polish.

## Contrasto e palette

Rapporti calcolati dai colori sRGB originali. Il testo normale, comprese etichette e placeholder, richiede almeno 4.5:1 secondo [WCAG 2.2, contrasto minimo](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html).

| Coppia originale | Contrasto | Intervento |
| --- | ---: | --- |
| `--text-faint #98938a` / sidebar `#f2f0e9` | 2.68:1 | Scurire il testo secondario chiaro |
| `--text-faint #98938a` / pagina `#faf9f5` | 2.90:1 | Verificare metadati e placeholder |
| Dark `--text-faint #7c766d` / superficie `#262523` | 3.40:1 | Schiarire il testo secondario scuro |
| Bianco / accent chiaro `#c96442` | 3.90:1 | Introdurre `--on-accent` per i CTA |
| Bianco / accent scuro `#d97757` | 3.12:1 | Evitare bianco fisso sul teal chiaro |
| Accent chiaro / pagina | 3.70:1 | Verificare link e label attive |
| Warn chiaro `#b07d2b` / pagina | 3.43:1 | Ambra più scura per piccoli testi |

Oltre ai token in testa al CSS, convertire gli hardcode: hover/active `rgba(127,122,112,...)` in `.vault`, `.session`, `.vault-chat`, `.vault-twisty`, `.search-box`; bordo `.vault-new`; rosso alpha di `.search-clear:hover` e `.session-del:hover`; anello bianco 85% del logo dark; backdrop marrone dell'overlay. `#send` e `.btn.primary` usano `#fff` fisso. Il bianco di `.preview-frame` appartiene alla superficie del contenuto esterno e va valutato separatamente.

## Focus e motion

- Manca un trattamento globale `:focus-visible`. Aggiungere un outline distinto dal bordo, senza cambiare dimensioni. I campi con `outline:none` hanno già feedback sul bordo/contenitore: mantenerlo e renderlo riconoscibile. Riferimento: [WCAG Focus Visible](https://www.w3.org/WAI/WCAG22/Understanding/focus-visible.html).
- `.session-del`, `.att-del`, `.vmem-del` partono da `opacity:0` e appaiono solo su hover: renderli visibili anche con `:focus-visible` e con `:focus-within` del contenitore. Conservare `body.turn-live .att-del { display:none; }`.
- Il CSS desktop non contiene `prefers-reduced-motion`; pulse, blink, spin e transizioni transform restano attivi. Aggiungere la variante ridotta mantenendo leggibile lo stato operativo.
- Limitare `transition:all` in `.quick-chip` e `.goccia-web` alle proprietà effettivamente animate. Evitare animazioni di geometria durante il polish.
- Le maniglie sono `div` senza semantica/focus tastiera: il solo CSS non risolve la loro accessibilità. Modificarne l'interazione esce dal perimetro visivo concordato.

## SVG e compatibilità dei test

`server/main.py` monta `/static` con `StaticRivalidati(directory=WEB_DIR)`: nessuna route dedicata a PNG, logo o favicon. Un SVG in `web/` usa la stessa route; controllare risposta 200 e MIME `image/svg+xml`. `Cache-Control:no-cache` vale anche per SVG; l'impronta URL dell'index è applicata soltanto a `app.js` e `style.css`.

Nessun test impone `logo-light.png`/`logo-dark.png`. Si possono cambiare i due `src` mantenendo classi `.logo-chiaro`/`.logo-scuro`, contenitore 28×28 e alternanza CSS per tema. Il test `tests/test_ricerca_chat.py::test_l_icona_della_scheda_e_il_robottino_senza_riquadro` impone invece il link a `favicon-64.png` e file PNG RGBA 16/32/64: conservare questi fallback se si aggiunge la favicon SVG.

Polish compatibile: modificare colori, bordi, ombre, focus e asset; preservare `--sidebar-w`, `--panel-w`, `--bolla-max`, grid, padding/gap, struttura DOM e ID. I test di `test_interfaccia_colonne.py` e `test_ricerca_chat.py` leggono alcune regole CSS come stringhe, incluso il margine dell'ancora e i selettori delle maniglie. Sono utili per regressioni geometriche, ma non verificano contrasto, resa SVG o navigazione da tastiera.
