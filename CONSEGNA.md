# Consegna finale — 14 settembre 2026

Archivio completo del sorgente di Astra / Local Agent Harness 2.37.0,
comprensivo delle modifiche al loop e al contesto e dell'ultima revisione grafica.

La mascotte ora esegue saltini più ampi, usa espressioni facciali e mostra
nuvolette per quattro secondi. Una seconda copia da 42 px è allineata sopra
l'invio della chat attiva. Le due copie condividono lo stato della conversazione;
durante l'elaborazione il movimento prosegue, rispettando la preferenza di
movimento ridotto. La fascia dedicata tiene mascotte e nuvolette sopra al testo.

## Verifiche della revisione grafica

- 54 test del controller, dello streaming e delle integrazioni: passati.
- 51 test delle colonne e dei controlli del composer: passati.
- 1 test degli asset e della cache: passato.
- Controllo sintattico Node di web/app.js e web/companion.js: passato.
- Ruff su tests/test_companion.py: passato.
- Verifica nel browser Edge: temi chiaro/scuro, messaggio su più righe,
  sidebar nascosta e stretta, allineamento sopra invio, nuvolette temporanee,
  movimento ridotto. Nessun errore JavaScript rilevato.

Evidenze visive e report: artifacts/mascot-*. I rapporti precedenti in docs/
e i log context-* conservano la data e l'ambito delle rispettive verifiche;
non rappresentano una nuova esecuzione della suite completa dopo la grafica.

## Avvio

Estrarre la cartella Astra e seguire README.md. I comandi base sono:

    uv sync --locked --extra dev
    uv run python run.py

Per provare gli stati grafici con dati simulati:

    uv run python scripts/visual_preview.py

Aprire http://127.0.0.1:8137/brand/index.html.

L'archivio include sorgenti, asset, test, skill, documentazione e file delle
dipendenze. Ambienti virtuali, cache, modelli e dati runtime non sono inclusi.
MANIFEST-SHA256.json elenca dimensioni e hash dei file consegnati; la verifica
del pacchetto confronta ogni voce con questi byte e controlla il CRC ZIP.
