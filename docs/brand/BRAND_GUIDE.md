# Local Agent Harness — Visual refinement

Direzione: **Graphite / Mineral**. La gerarchia esistente resta riconoscibile; il cambiamento riguarda colore, contrasto, qualità dei componenti e identità del robot. Nessuna nuova dipendenza nel browser, font remoto o immagine raster pesante per la mascotte.

## 1. Interfaccia e movimento

La struttura a tre colonne, i breakpoint, le dimensioni tipografiche e i controlli mantengono le misure precedenti. Il composer include ora una fascia dedicata alla mascotte sopra l'area di testo. Il confronto in `qa/layout-desktop.json` documenta il precedente intervento sulla palette, prima di questa aggiunta.

| Ruolo | Light | Dark |
| --- | --- | --- |
| Fondo | `#F7F9F9` | `#11181B` |
| Superficie | `#FFFFFF` | `#192428` |
| Testo principale | `#1C2B30` | `#EDF3F3` |
| Testo secondario | `#4F646A` | `#B2C3C6` |
| Metadati e placeholder | `#55686D` | `#A5B8BD` |
| Accento / azione | `#0B7169` | `#68D5C4` |
| Testo su azione | `#FFFFFF` | `#102A28` |
| Selezione | `#DCECE8` | `#25413D` |
| Bordo dei controlli | `#72868A` | `#70878B` |

Il mobile mantiene il tema scuro esistente con la stessa famiglia cromatica. Le sue superfici sono adattate alle bolle della conversazione. Verde, ambra e rosso mantengono il significato operativo; il marchio non sostituisce messaggi, indicatori di stato o errori.

La tipografia utilizza lo stack locale già presente: Segoe UI/system sans per l'interfaccia e monospace per codice, percorsi e metadati tecnici. Le dimensioni non cambiano. La leggibilità migliora attraverso contrasto e distinzione tra testo principale, secondario e metadati; non attraverso nuove densità o pesi che alterano gli ingombri.

| Interazione | Risposta |
| --- | --- |
| Hover / focus cromatico | 160–200 ms, curva `cubic-bezier(.2,.75,.25,1)` |
| Pressione dei pulsanti principali | Spostamento di 1 px; nessuna variazione del flusso |
| Modali, tab e menu | 200 ms, entrata di 2 px e opacità da 0,96 a 1 |
| Focus da tastiera | Contorno di 2 px, offset di 2 px; mobile 3 px |
| Testo in scrittura | Cursore color accento, focus del composer chiaramente visibile |
| Preferenza movimento ridotto | Animazioni e transizioni disabilitate, scroll immediato |

Le carte informative restano ferme. I controlli ricevono il feedback; i token in streaming non vengono animati a ogni aggiornamento. Le maschere che sfumavano il testo in fondo al piano sono sostituite da un bordo interno. Il colore dei pulsanti primari scuri usa un testo scuro ad alto contrasto. I controlli secondari prima visibili solo al passaggio del mouse vengono mostrati anche con focus nel contenitore e sui dispositivi senza hover.

Il report `CONTRAST.md` misura 189 combinazioni di testo, superficie e stato. Questo controllo non equivale a una certificazione WCAG completa: una verifica complessiva richiede anche tecnologie assistive, tutte le interazioni e contenuti reali.

## 2. Marchio

Il simbolo statico nasce da una griglia di **32 unità**: testa compatta con angoli morbidi, due occhi verticali e antenna centrale. Gli asset del marchio non hanno bocca, riflessi, ombre o decorazioni e restano invariati. La mascotte animata aggiunge espressioni facciali, come descritto nella sezione 3. L'approcciabilità del marchio deriva dalle proporzioni, la precisione dalla geometria.

Il nome del prodotto resta **Local Agent Harness**. Il wordmark breve “Harness”, già coerente con il nome dell'app mobile, è accompagnato dalla denominazione completa. Non è stato rinominato il prodotto nell'interfaccia.

| Asset in `web/brand/` | Uso |
| --- | --- |
| `mark-light.svg` | Simbolo grafite su superficie chiara |
| `mark-dark.svg` | Simbolo chiaro su superficie scura |
| `mark-mono.svg` | Una tinta, occhi in negativo; stampa o incisione |
| `wordmark-light.svg`, `wordmark-dark.svg` | Marchio esteso con tipografia |
| `favicon.svg` | Simbolo adattivo al tema del browser |
| `app-icon.svg` | App icon su fondo grafite, area sicura maskable |

Favicon PNG trasparenti a 16, 32 e 64 px forniscono i fallback. Le icone PWA sono esportate a 192 e 512 px; l'icona Apple a 180 px. Le immagini desktop a 64 px restano disponibili per i riferimenti esistenti. L'interfaccia usa i nuovi SVG, correggendo anche i precedenti riferimenti a PNG inesistenti.

**Regole di applicazione:**

- Area di rispetto consigliata: almeno 4 unità della griglia attorno al simbolo, esclusi favicon e slot compatti già definiti.
- Minimo del simbolo: 16 px nelle favicon; 24 px nelle applicazioni UI; slot attuale della sidebar 28 px.
- Non deformare la griglia, cambiare la distanza degli occhi o aggiungere contorni al simbolo.
- Il wordmark usa font locali di sistema. Per una consegna tipografica a stampa, convertire il testo in tracciati con il font scelto e autorizzato; l'SVG incluso conserva testo editabile e può variare leggermente tra sistemi.
- Nei quadrati PWA, rispettare l'area sicura circolare centrale. Non ingrandire il robot fino ai bordi per riempire la tessera.

Rigenerazione locale e senza rete:

```powershell
node scripts/build_brand_assets.cjs
```

Serve Node con `sharp` disponibile. Quando si usa un runtime condiviso, impostare `NODE_PATH` alla sua cartella `node_modules`. Lo script controlla la corrispondenza con la geometria della mascotte e verifica dimensioni e trasparenza degli export.

## 3. Mascotte

`web/companion.js` e `web/companion.css` realizzano il robot con SVG nativo. Due copie sincronizzate accompagnano la conversazione attiva: una nello **slot del logo da 28 px** in alto a sinistra e una da **42 px sopra il tasto di invio**, nell'host `#composer-companion`. La seconda occupa una fascia riservata dentro il composer, senza sovrapporsi all'area di testo. La mascotte animata ha una bocca espressiva; i file statici del marchio mantengono la geometria originale. Sul mobile l'identità resta nelle icone dell'app.

| Stato | Evento | Movimento ed espressione | Nuvoletta |
| --- | --- | --- | --- |
| `welcome` | Bootstrap dell'app, se non è già in corso un turno | Doppio saltino fino a 7 px e rotazioni fino a 12° | Ciao! ✨ |
| `working` | Attività nella conversazione visibile | Oscillazione e saltini continui, con sguardo animato | Ci penso io… |
| `success` | `done: completed`, più di un passo, senza errore del turno | Triplo saltino fino a 10 px ed espressione felice | Fatto! ✨ |
| `idle` | Quiete, annullamento, domanda o limite di passi | Nessuna animazione | Nessuna |
| `resting` | 45 secondi di inattività senza editing | Inclinazione fino a 13°, sguardo curioso e battito degli occhi | Sono qui! |
| `error` | Errore effettivo o conclusione con motivo `error` | Scuotimento laterale e bocca preoccupata; i dettagli restano nel messaggio operativo | Ops, riproviamo? |

Non esiste una schermata di login in questa UI: il saluto è legato all'avvio dell'app. Una riconnessione annunciata non è trattata come fallimento definitivo. Cambiare conversazione azzera l'espressione della chat precedente. Eventi ripetuti non riavviano il gesto. La chiusura tecnica dello stream non annulla una conferma di successo appena ricevuta.

I gesti durano **1.400 ms**, una sola volta. Durante `working`, oscillazione e saltini si ripetono con un ciclo CSS di **1.700 ms** e lo sguardo con un ciclo di **2.100 ms**, finché il turno resta attivo. Le nuvolette compaiono per **4 secondi** all'ingresso nello stato. Non ci sono tracciamento del puntatore o richieste di rete. Il timer d'inattività è singolo, non ricorrente: il focus in un campo sospende il conteggio, mantenendo disponibili i gesti espliciti di saluto, attività, successo ed errore.

Nascondere la scheda cancella il gesto e la nuvoletta in corso. Tornare alla scheda non riproduce vecchi festeggiamenti. Con `prefers-reduced-motion` le animazioni si fermano; espressioni facciali e testo delle nuvolette restano disponibili.

Il controller espone `init`, `setState`, `getState` e `destroy`. `init({mounts: [...]})` collega più host a un unico controller, condividendo stato, timer e listener; resta supportata la forma precedente `init({mount: ...})`. La distruzione libera timer e listener e ripristina i nodi originali di tutti gli host, mantenendone l'identità. La grafica e le nuvolette sono decorative, `aria-hidden`, e non entrano nell'ordine di tabulazione. Le animazioni usano trasformazioni SVG, adatte a un rendering fluido: non è stata effettuata una misura FPS su dispositivi fisici, quindi non viene garantito un valore di 60 fps.

## Anteprima e verifica

```powershell
.venv\Scripts\python.exe scripts/visual_preview.py
```

Aprire `http://127.0.0.1:8137/brand/index.html` per la tavola del marchio e gli stati dimostrativi. `/` mostra il desktop; `/mobile` il client mobile. L'anteprima usa i file UI reali e risposte API simulate; non avvia agenti, modelli o comandi e non modifica preferenze o sessioni reali.

Le modifiche sono nel sorgente di lavoro. Il pacchetto dell'audit tecnico precedente conserva il proprio snapshot; non include automaticamente questo intervento visivo.
