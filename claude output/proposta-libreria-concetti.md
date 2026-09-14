# Libreria di concetti — proposta

Obiettivo scelto: **fermare il decadimento dei riassunti**. Non ridurre il contesto,
che a 98k non è un problema. Richiamo su due gambe: precarico d'ufficio + `read_file`.
Cadenza: ogni N passi agentici.

---

## 1. Dove nasce davvero il decadimento

`core/compaction.py`, `trascrizione()`, righe 168-173:

```python
elif ruolo == "summary":
    # Un riassunto precedente entra nel nuovo: altrimenti si
    # accumulerebbero, e la cronologia diventerebbe una pila di
    # riassunti di riassunti.
    righe.append(f"[RIASSUNTO PRECEDENTE] {...}")
```

Il commento ha ragione sul problema che risolve e non dice quello che introduce.
Alla seconda compattazione il modello riassume un riassunto; alla terza, il
riassunto di un riassunto — con `MAX_TOKEN_RIASSUNTO = 700` come imbuto fisso a
ogni giro. Non è un difetto di quel codice: sono **due esigenze in conflitto**
(non accumulare / non perdere) risolte con l'unico compromesso disponibile
finché l'informazione può vivere solo dentro la cronologia.

La libreria le separa, ed è tutto il suo valore: il testo verbatim sta **fuori**
dalla cronologia, quindi non si accumula *e* non decade.

Nota di realtà, da mettere in conto: con `SOGLIA_DEFAULT = 0.75` su 98k il
decadimento **non è ancora mai avvenuto** (0 compattazioni su 42 sessioni). Stai
costruendo prima del danno — che è il momento giusto per costruire e il momento
peggiore per misurare. Vedi §7.

---

## 2. Chi scrive: non la delega

La tentazione è usare `core/delega.py`. Sconsigliato, e non per prudenza: la
delega monta un `run_turn` intero con `MAX_PASSI_DELEGA = 6`, tool, budget
stretti, referto troncato a 2.000 caratteri. L'archiviazione non ha niente da
esplorare — ha già davanti tutto quello che deve scrivere.

Lo schema giusto ce l'hai già ed è `costruisci_riassunto()`: **una chiamata sola,
`think=False`, `temperature=0.1`, nessun tool**. Un modulo nuovo, `core/libreria.py`,
che riusa `trascrizione()` così com'è.

Il figlio non riceve `write_file`. Restituisce testo; **è l'harness a scrivere su
disco**. Coerente con «solo in lettura» di `delega.py` e con il tuo principio del
rito: un file scritto dall'harness non dipende dal fatto che il modello si ricordi
di scriverlo.

---

## 3. Dove vivono i file

`<workspace>/.memoria/NNN-slug.md`.

Tre vincoli che il codice impone già e vanno rispettati:

- **Non dentro `.analisi/`**: `reset_scratch()` la cancella ricorsivamente
  all'inizio di ogni turno (`agent.py:1371`).
- **La cartella col punto è invisibile** a `list_files`, `search_files` e
  all'albero dell'`<environment>` (commento a `tools.py:482`). Qui è una
  proprietà buona, non un problema: la libreria non deve inquinare l'albero del
  progetto. Ma implica che **l'unica via d'accesso è l'indice**. Se l'indice non
  la nomina, per il modello non esiste.
- `is_scratch_path` / `_refuse_readonly_write`: verificare che una richiesta di
  sola lettura (`readonly_request`) non blocchi la scrittura della libreria.
  Serve un `is_memory_path` gemello, oppure la scrittura passa fuori dai tool
  (che è la strada di §2, e allora il problema non si pone).

Domanda aperta: **workspace o progetto?** Nel workspace la libreria segue il
codice; accanto a `chat_sessions/` seguirebbe le conversazioni e varrebbe fra
sessioni diverse sullo stesso progetto. La seconda è più utile e più invadente.

---

## 4. Append-only, sul serio

La regola che fa tutto il lavoro:

> Un file esistente non si riapre mai per migliorarlo. Un fatto superato si
> corregge con un blocco nuovo in coda che dice cosa smentisce.

Se concedi la riscrittura, hai reintrodotto il decadimento con un altro nome —
solo, più lentamente e senza accorgertene. È un log, non un wiki.

Formato: `## slug` + una riga di descrizione (che diventa la voce d'indice) +
corpo. Un solo blocco di output del modello, spezzato dall'harness sui `##`.
Slug già esistente → il blocco si appende in coda al file con un separatore
datato, e l'indice **non** cambia riga.

---

## 5. L'indice, e perché può decadere senza danno

Una riga per file: `NNN-slug — descrizione`. Va **in coda**, nella lista `coda`
di `build_api_messages` (`agent.py:444-450`), insieme a note e piano — mai
nell'`env_header`, che sta nel prefisso e deve restare byte-identico o il KV
cache si ricalcola a ogni archiviazione.

Costo: 15-20 token a riga, **rispediti a ogni passo**. A 40 concetti sono ~700
token fissi di finestra: quasi il doppio del foglio note (415) che nessuno usa.
Serve un tetto, e il tetto è il punto scomodo del disegno — un indice che
dimentica è un indice che decade, e avresti rimesso il problema di §1 un piano
più in alto.

La via d'uscita: **l'indice è l'unica cosa che ha diritto di decadere**, perché è
l'unica ricostruibile. Le prime righe dei file sono su disco; se l'indice si
accorcia o si raggruppa per argomento, l'harness può rigenerarlo *senza chiamare
il modello*. I file no: quelli sono la fonte. Quindi tetto sì, con
raggruppamento, e la certezza che nessuna informazione muore lì.

---

## 6. Il richiamo, due gambe

**(a) L'harness, d'ufficio.** Al primo passo dopo un'archiviazione — e dopo una
compattazione — precarica i file la cui descrizione condivide parole con il testo
del punto di piano aperto. Match lessicale, zero modello, zero passi spesi.
Tetto: 2 file, N caratteri, in coda.

Un dettaglio da non sbagliare: il precarico deve **registrarsi in
`ctx.read_cache`** (`tools.py:1005-1023`), altrimenti se il modello poi chiama
`read_file` sullo stesso file si ritrova lo stesso testo due volte nella
finestra. La cache è già tarata sui passi (`ctx.step - seen[1] <
tool_result_full_window`), quindi risponde `"invariato"` solo finché il testo è
davvero ancora lì: quel meccanismo funziona già, va solo alimentato.

**(b) Il modello, con `read_file`.** Nessun tool nuovo, nessun costo di schema.
Va detto una volta nell'indice stesso («questi file si rileggono con `read_file`»),
non nel system prompt.

---

## 7. Il cambio che vale da solo — e che si può misurare

Il pezzo 1 senza la libreria: quando `trascrizione()` incontra un `summary`,
invece di rimasticarlo come `[RIASSUNTO PRECEDENTE]`, **lo scrive su disco e lo
rilegge verbatim**. La catena delle fotocopie si spezza con un `if`.

È ~40 righe, non richiede indice, né archiviatore, né richiamo, e risolve il
problema che hai scelto come obiettivo. E soprattutto è **misurabile oggi**:
abbassi `SOGLIA_DEFAULT` a 0,15 su una sessione vecchia di `chat_sessions/`,
forzi quattro compattazioni di fila e confronti l'ultimo riassunto con la
trascrizione vera. Quanto ha perso al quarto giro è il numero che dice se il
resto della proposta vale la latenza.

Farei questo per primo. Il resto poggia sullo stesso pezzo.

---

## 8. Costi da mettere in conto

| voce | quanto |
|---|---|
| archiviazione ogni N passi | **una generazione intera** — su GPU remota è latenza seriale, come la delega |
| indice in coda | ~700 token a 40 voci, a ogni passo |
| precarico d'ufficio | fino a 2 file per volta, solo dopo archiviazione/compattazione |
| disco | una cartella nuova nel workspace dell'utente, che non si svuota mai |

**Il rischio nuovo, che l'append-only non copre:** 40 file di cui 30 dicono la
stessa cosa con parole diverse. Append-only garantisce che i concetti non
decadano, non che non proliferino. Le uniche difese sono il prompt a produzione
forzata («scrivi solo i concetti che l'indice non contiene già», con l'indice in
pasto) e il tetto. Su `qwen3.8:27b` va misurato, non assunto.

---

## 9. Domande aperte

1. `.memoria/` nel **workspace** (segue il codice) o accanto a `chat_sessions/`
   (segue il progetto, vale fra sessioni)?
2. **N** = ogni quanti passi? Con `max_steps` tipico, N=8 su un compito da 40
   passi sono 5 generazioni in più.
3. Tetto dell'indice: quante voci prima di raggruppare?
4. L'archiviazione va **saltata** se dall'ultima non è successo niente di nuovo
   (nessuna tool call, nessun punto chiuso)? Altrimenti paghi una generazione per
   scrivere «niente di nuovo».
