# Verifica del raffinamento visivo

Verifica eseguita il 7 settembre 2026 sul sorgente di lavoro. L'audit tecnico precedente conserva il proprio snapshot e i propri risultati.

## Risultati

| Verifica | Esito |
| --- | --- |
| Contrasti di palette e stati | 189/189 combinazioni superano le soglie |
| Test UI, mascotte, mobile, streaming e cache | 143 passati in 97,42 s |
| Test su colonne e indicatori di piano | 35 passati in 0,32 s |
| Ruff sui file Python interessati | Nessun errore |
| Geometria dei contenitori desktop a 1280×720 | Identica al CSS originale |
| Dichiarazioni di geometria e tipografia CSS confrontate | 860 desktop e 137 mobile invariate |
| Mobile a 390×844 | Nessun overflow orizzontale; composer dentro la viewport |
| Apertura chat mobile tramite Enter | Verificata nel browser |
| Focus del tab delle impostazioni, tema scuro | Contorno di 2 px, offset 2 px |
| Gesto di successo nel browser | Termina; `data-gesture` assente e animazione `none` |
| Asset vettoriali e PNG | Caricamento verificato; dimensioni e alpha controllati dal generatore |

I 48 test della mascotte, compresi nel gruppo da 143, eseguono JavaScript reale con QuickJS. Coprono timer finiti, editing, visibilità, reduced motion dinamico, ripristino dei nodi, eventi ripetuti, esiti del turno, cambio di conversazione e stream abbandonati. I test di cache verificano che anche i nuovi file `companion.js` e `companion.css` abbiano URL versionati e siano serviti correttamente.

Il test preesistente sul marcatore del piano vietava erroneamente qualsiasi ombra interna nell'intero CSS. Il controllo ora si applica al selettore del marcatore a cui si riferisce; resta valido per la barretta del passo in corso. Il nuovo bordo delle aree scorrevoli sostituisce una maschera che attenuava il testo.

## Evidenze

- `qa/ui-tests.txt` e `qa/ui-tests.xml`: output e risultati dei 143 test.
- `qa/layout-tests.txt`: risultati dei 35 test sui pannelli.
- `qa/layout-desktop.json`: misure DOM prima/dopo di app, sidebar, main, topbar, panel, composer e thread.
- `qa/css-geometry.json`: confronto delle dichiarazioni di layout e tipografia, senza aggiunte o rimozioni.
- `qa/desktop-light.png`, `qa/desktop-dark.png`: interfaccia desktop.
- `qa/settings-dark.png`: impostazioni e focus visibile.
- `qa/mobile-list.png`, `qa/mobile-chat.png`: interfaccia mobile.
- `qa/brand-dark.png`: tavola del marchio.
- `CONTRAST.md`: matrice completa delle combinazioni misurate.

Gli screenshot mostrano dati simulati, serviti con `scripts/visual_preview.py` usando i file UI reali. Il confronto DOM usa lo stesso markup e gli stessi dati con il CSS originale e quello rifinito. Il confronto statico CSS controlla il multinsieme delle dichiarazioni geometriche; non è una prova automatica di ogni possibile stato del layout.

## Riproduzione

```powershell
.venv\Scripts\python.exe scripts/check_visual_contrast.py --write-report
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/test_companion.py tests/test_ricerca_chat.py tests/test_mobile_ui.py tests/test_mobile_passi.py tests/test_mobile_proxy.py tests/test_reattivita_e_ripresa.py tests/test_interfaccia_colonne.py
.venv\Scripts\python.exe scripts/visual_preview.py
```

La conformità WCAG complessiva e un valore di 60 fps su dispositivi fisici non sono attestati da questi controlli. La verifica riguarda palette, interazioni selezionate, struttura esistente e affidabilità del controller decorativo. Non è stato rieseguito il collaudo completo dell'infrastruttura LLM, non modificata da questo intervento.

L'archivio `harness-visual-refinement-20260907.zip` contiene asset, file d'integrazione e documentazione di questa revisione. È un supplemento al progetto esistente, non un pacchetto di installazione autonomo.
