# qwen3.8 — anatomia dell'overthinking

Quattro sessioni con `Qwen3.8:27B-Q6_K` (20–22 agosto), **350 passi
dell'assistente**, 588 tool call. Ricostruito dai `<think>` salvati nelle
sessioni. Script: `claude output/analisi_think.py` e `analisi_think2.py`.

| sessione | passi | pensiero | risposte | rapporto |
|---|---|---|---|---|
| 20260820_112642 | 21 | 124.877 | **0** | — |
| 20260820_122131 | 107 | 401.967 | 13.282 | 30× |
| 20260821_132309 | 149 | 1.118.157 | 88.186 | 13× |
| 20260822_105034 | 73 | 782.070 | 7.502 | 104× |
| **totale** | **350** | **2.427.071** | **108.970** | **22×** |

**~607.000 token pensati contro 27.000 scritti.** Per ogni singola tool call
emessa: 4.127 caratteri di ragionamento, cioè circa **1.031 token di pensiero
per ogni azione**.

---

## 1. Non pensa troppo sempre: pensa troppo a volte, e tantissimo

| | |
|---|---|
| mediana di un blocco | 3.394 caratteri |
| p90 | 19.115 |
| p99 | 37.969 |
| massimo | 52.480 |

**Il 10% dei blocchi più lunghi (35 su 350) vale il 40% di tutto il pensiero.
La metà più corta vale l'8%.** Non è una tara diffusa da correggere ovunque: è
una coda grassa di tre dozzine di episodi. Qualunque intervento che agisca
sulla media sbaglia bersaglio; serve qualcosa che tagli la coda.

E la coda non è dove ci si aspetta. Il pensiero **non** si accorcia mentre il
turno avanza, nonostante `think_for_step` abbassi il livello di uno scalino dal
secondo passo:

```
passo 1   mediana 3.327 char        passo 5   mediana 1.562
passo 2   mediana 4.854             passo 6   mediana 4.368
passo 3   mediana 2.686             passo 7   mediana 1.779
passo 4   mediana 2.452             passo 8+  mediana 3.452   (n=135)
```

Al passo 8 e oltre si pensa quanto al passo 1. O lo scalino non viene
applicato, o uno scalino su `native_think: high` non sposta niente.

E soprattutto **il pensiero non segue il tipo di mossa**:

| mossa del passo | mediana pensata |
|---|---|
| `manage_plan` da solo | 693 char |
| `search_files` | 2.564 |
| `read_file` | 2.959 |
| `run_command` | 3.656 |
| **`edit_file`** | **5.253** |

Cinquemila caratteri di ragionamento prima di una modifica meccanica a un file
già letto, quando il punto di piano aperto dice già cosa fare. `manage_plan` —
l'unica mossa che richiede davvero una decisione — è la più economica delle
cinque. È esattamente al contrario di come dovrebbe essere.

---

## 2. La forma dell'overthinking è l'oscillazione, non la ripetizione

Cercando ripetizioni testuali dentro un blocco non si trova quasi niente:
**0,5%** di 8-grammi ripetuti in media, e solo 3 blocchi su 350 sopra il 10%.
Il modello non si riscrive addosso.

Quello che fa è **cambiare idea**:

| | 'wait' / 'actually' / 'hmm' per blocco |
|---|---|
| mediana su tutti i blocchi | 4 |
| media | 11,3 |
| **blocchi sotto i 2.000 caratteri** (n=140) | **0** |
| **blocchi sopra i 15.000 caratteri** (n=56) | **37** |
| massimo | 81 |

Trentasette ripensamenti in un blocco solo. Il **10,1% di tutti i caratteri
pensati** (244.278) sta in frasi che contengono un marcatore di ripensamento.
Un altro **5,9%** (143.026) sono auto-istruzioni — *"I should…", "let me…",
"I need to make sure…"*.

Il blocco corto non ha ripensamenti; il blocco lungo ne ha trentasette. Non
sono due gradi dello stesso comportamento: sono due comportamenti diversi, e
il secondo è un ciclo di revisione che non ha una condizione di uscita.

---

## 3. Loop: cercati, non trovati

Vale la pena dirlo chiaro perché è la conclusione che stavo per dare sbagliata,
prima di guardare il campo `ok` dei risultati invece di indovinarlo dal testo.

- **Catena più lunga di fallimenti identici consecutivi: 2**, sotto la soglia
  di 3 di `LOOP_NUDGE`. La guardia è rimasta zitta ed era giusto così.
- **Somiglianza fra un blocco di pensiero e il successivo: mediana 0,15, zero
  coppie sopra 0,5.** Non sta ri-derivando la stessa cosa a ogni passo.
- Chiamate identiche ripetute **e tutte riuscite** (rifare lavoro già fatto:
  rileggere un file già letto, rilanciare un test già verde): 5% e 2% in due
  sessioni, 0% nelle altre due.

Lo spreco c'è ma è piccolo e non è un loop. **Il problema non è che ripete: è
che delibera.**

---

## 4. Fuori tema: uno vero, uno apparente

**Il ragionamento è in inglese: 344 blocchi su 350.** Le richieste dell'utente
sono in italiano (248 su 350) e le risposte tornano in italiano. Il modello fa
un doppio passaggio di lingua a ogni turno — traduce il problema per pensarci,
e ritraduce per rispondere.

**Il 3,2% dei caratteri pensati (78.422) parla dell'harness, non del compito**:
`manage_plan` compare nel 29% dei blocchi, il proprio system prompt nel 15%, i
solleciti nel 4%. Non è enorme, ma è ragionamento speso sulla macchina invece
che sul lavoro.

L'apparente: la copertura lessicale fra pensiero e richiesta è bassissima
(mediana 0,07, sotto 0,25 in 204 blocchi su 245) — **e non significa deriva.**
Due delle quattro sessioni cominciano con un'inquadratura in stile racconto
("Compagni e compagne del Collettivo Siliconico…") le cui parole non hanno
motivo di ricomparire in un pensiero su del codice. La prova che non c'è
deriva la dà la misura dentro i blocchi lunghi: copertura **0,20 nel primo
terzo e 0,21 nell'ultimo**. Un blocco da ventimila caratteri finisce a parlare
della stessa cosa con cui era cominciato. Divaga in profondità, non in
direzione.

---

## 5. Il watchdog scatta, e non cambia niente

**46 interruzioni** nelle quattro sessioni — 26 in una sola. Tutte nella stessa
forma: *"eri a 2001 token di ragionamento senza aver ancora chiamato un
tool"*. Il watchdog fa il suo lavoro, taglia lo stream e sollecita. Poi il
turno dopo ricomincia da capo, e riscatta.

È una diga che non alza il livello a monte: interrompe l'episodio, non cambia
la disposizione. Ventisei volte nella stessa conversazione vuol dire che il
modello ha imparato zero dalle prime venticinque.

---

## 6. Il caso peggiore: una sessione senza una parola

`20260820_112642_b479` — **21 passi, 46 risultati di tool, 124.877 caratteri
pensati, zero caratteri di risposta.** Mai. Due turni (20 passi e 1 passo),
nessuno dei due chiuso con un messaggio. L'ultimo messaggio dell'assistente
finisce esattamente su `</think>` e non prosegue.

L'utente ha visto delle tendine di tool aprirsi e chiudersi, e nient'altro.

`SUMMARY_NUDGE` esiste proprio per questo e non è scattato. Il ramo ha una
condizione che lo esclude nel caso peggiore:

```python
if (require_summary and not summary_requested
        and tools_used and not answer
        and step < max_steps):        # <- qui
```

Un turno che finisce **perché ha esaurito i passi** non può ricevere il
sollecito che chiede il riepilogo: `step == max_steps`. Cioè la rete manca
esattamente nel turno lungo e faticoso, che è il solo in cui l'utente ha
davvero bisogno di sapere cos'è successo. Negli altri file lo si vede al
contrario: turni da 26–29 passi che si chiudono con la risposta perché sono
arrivati alla fine da soli.

---

## Cosa proverei, in ordine di rapporto fra guadagno e rischio

1. **Il riepilogo anche a passi esauriti.** Togliere `step < max_steps` da quel
   ramo non basta (non c'è un passo in cui infilare la chiamata): serve, come
   per il referto di chiusura della delega, **una generazione sola e senza
   tool** quando il turno finisce per `max_steps` senza risposta. Costa una
   chiamata nel caso peggiore e restituisce all'utente il turno che ha pagato.

2. **Il livello di pensiero non è un'impostazione della conversazione: è una
   proprietà della mossa.** I numeri dicono che si pensa di più prima di un
   `edit_file` che prima di un `manage_plan`. Abbassare a `low` quando il passo
   precedente ha già deciso — punto di piano aperto e ultima mossa che ha
   toccato il disco — colpisce dove la coda è grassa senza toccare il primo
   passo, che è quello in cui pensare serve.

3. **Il watchdog deve lasciare un segno oltre il proprio turno.** Alla seconda
   interruzione nello stesso turno, invece di sollecitare e basta, scendere di
   livello per il resto del turno. Oggi la seconda interruzione è l'ultima
   (`max 2 volte`) e dopo di quella non c'è più niente.

4. **Misurare se `think_for_step` sta davvero agendo.** La mediana al passo 8+
   è uguale a quella del passo 1: o non si applica, o uno scalino da `high` non
   si vede. È una riga di log, e senza quella le prime tre proposte sono
   ipotesi.

Quello che **non** proverei: guardie contro i loop, deduplicazione delle
chiamate, tagli alle ripetizioni nel testo. Le ho cercate e non ci sono.
