# Prova del piano di lavoro e del ragionamento multi-passo

> **Cosa misura.** La difficoltà sta tutta nella *struttura* del piano e nel
> *numero* di punti, non nel compito: ogni singola azione richiesta è banale —
> scrivere quattro file di testo, uno script di dieci righe, lanciare un
> comando. Nessun algoritmo, nessuna libreria, niente da progettare.
>
> Le dodici voci sono elencate **fuori ordine di esecuzione**, sono più del
> massimo di punti che il piano ammette (quindi vanno raggruppate, non copiate
> una per una), una di esse è condizionata a un esito che si conosce solo dopo
> aver eseguito, e un'altra risulterà non applicabile e va chiusa come saltata.
> È lì che si vede se il modello pianifica o se sta solo trascrivendo un elenco.

---

## Prima di iniziare

| | |
|---|---|
| **Harness** | Quello aggiornato in `C:\Users\filip\Claude\Projects\Agentic harness` (v2.20). Sulla copia in `D:\` metà di quello che segue non esiste. |
| **Conversazione** | Nuova. Una chat con del lavoro dentro darebbe al modello un piano già avviato e il test non misurerebbe la pianificazione da zero. |
| **Impostazioni → Generazione** | `max_agent_loops` a **20**. Con i 12 di serie il turno finisce per esaurimento passi e non si capisce se il piano era sbagliato o solo lungo. |
| **Impostazioni → Efficienza** | Lascia tutto com'è, default compresi. Qui non si sta provando la compattazione. |
| **Workspace** | Una cartella qualsiasi, anche vuota. Il test lavora solo dentro `banco/` e non tocca altro. |

---

## Il prompt

Incolla questo, tale e quale:

```
Lavora dentro la sottocartella `banco/` del workspace, creandola se non c'è.
Ogni singola cosa che ti chiedo qui sotto è banale: la difficoltà è l'ordine e
la quantità, non il contenuto.

Ecco le cose da fare. L'elenco è NUMERATO MA NON IN ORDINE DI ESECUZIONE:
l'ordine giusto lo devi trovare tu.

 1. Esegui `python banco/conta.py` e riportami l'output esatto.
 2. Crea `banco/dati/beta.txt` con 5 righe di testo qualsiasi.
 3. Crea `banco/README.md` con l'elenco dei file che hai prodotto, uno per
    riga, ciascuno con una frase su cosa contiene.
 4. Se `conta.py` riporta almeno un file da più di 100 righe, crea
    `banco/grande.md` con il nome di quel file. Altrimenti no.
 5. Crea `banco/dati/alfa.txt` con 3 righe di testo qualsiasi.
 6. Scrivi `banco/conta.py`: stampa una riga `<nome> <righe>` per ogni file
    `.txt` dentro `banco/dati/`, in ordine alfabetico. Niente argomenti,
    niente librerie esterne.
 7. Crea `banco/dati/gamma.txt` con 6 righe di testo qualsiasi.
 8. Per ogni file `.txt` che risulta avere più di 3 righe, crea
    `banco/note/<nome>.md` contenente il numero di righe del file gemello.
 9. Crea `banco/dati/delta.txt` con 2 righe di testo qualsiasi.
10. Controlla con un comando quale versione di Python gira nel container. Se è
    la 3.12 o superiore crea `banco/moderno.md`, altrimenti `banco/vecchio.md`.
    Dentro ci scrivi la versione esatta che hai letto.
11. Verifica con un comando che `banco/note/` contenga esattamente i file che
    ti aspetti, e riportami l'esito reale del comando.
12. Crea `banco/elenco.txt` con i nomi dei file `.txt` di `banco/dati/`, in
    ordine alfabetico, uno per riga.

Vincoli:
- Non toccare niente fuori da `banco/`.
- Non posso dirti in anticipo quanti file servono al punto 8 né quale dei due
  file serve al punto 10: sono cose che sai solo dopo aver eseguito qualcosa.
- Se una voce risulta non applicabile, chiudila come saltata scrivendo il
  perché. Non fingere di averla fatta e non farla finta.
- Alla fine dimmi, per ciascuna delle 12 voci, se è fatta o saltata e perché.
```

---

## Dove sono le trappole

Non servono a te per valutare — le valuto io sulla sessione — ma sapere che ci
sono evita di scambiare un comportamento corretto per un errore mentre guardi.

**Ordine.** La voce 1 lancia `conta.py`, che la voce 6 non ha ancora scritto e
che legge file creati dalle voci 2, 5, 7 e 9. Eseguirle nell'ordine dato
fallisce al primo comando.

**Quantità.** Dodici voci contro un piano che ne ammette al massimo otto: vanno
raggruppate per obiettivo verificabile, non ricopiate una per una.

**Cose che si sanno solo dopo.** Il punto 8 non è pianificabile in dettaglio
prima di aver contato le righe, e il punto 10 dipende da cosa risponde il
container. Un piano onesto li mette come punti e li dettaglia dopo.

**Una voce da saltare.** Nessuno dei file avrà più di 100 righe, quindi la
voce 4 non è applicabile. Chiuderla come saltata con la motivazione è la
risposta giusta; crearla lo stesso, o non nominarla più, no.

**Esito atteso.** `banco/note/` deve contenere `beta.md` (5) e `gamma.md` (6) e
nient'altro: `alfa` ne ha 3, che non è "più di 3", e `delta` ne ha 2.

---

## Quando ha finito

Salvami lo ZIP della sessione — il JSON in `chat_sessions/` insieme ai file
prodotti in `banco/` — e te lo passo da valutare.
