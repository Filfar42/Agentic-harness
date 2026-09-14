# Loop e contesto: implementazione 2.37.0

Revisione del 14 settembre 2026 in `D:\Astra`. Applica i fix emersi
dall'[analisi iniziale](LOOP_E_CONTESTO_2026-09-12.md) e aggiunge stato attivo
nei checkpoint, recupero degli archivi per contenuto e telemetria delle
chiamate. Non richiede nuove dipendenze o migrazioni manuali delle sessioni.

## Cambiamenti

| Area | Comportamento introdotto |
|---|---|
| Risultati dei tool | Potatura dei corpi mantenendo JSON valido, esito, hash e riferimenti al deposito. I metadati testuali rispettano limiti separati. Un `read_file` recente usa il budget della lettura, senza un secondo taglio del JSON serializzato. |
| Scritture | Gli argomenti vecchi vengono omessi solo dopo una modifica riuscita. Le scritture fallite conservano il tentativo. L'indicazione di recupero distingue il file attuale dal contenuto storico; write/edit restituiscono SHA-256 della versione scritta. |
| Checkpoint | Il riassuntore elabora i nuovi eventi, con un blocco separato di attivita' aperte e vincoli. Le sezioni omesse nel nuovo riassunto vengono riportate deterministicamente. Il checkpoint registra intervallo di origine e archivio. |
| Richieste umane | Richieste visibili e risposte effettive a `ask_user_question` restano testuali. Le risposte non vengono trattate come log invecchiati. |
| Recupero | Ricerca lessicale anche nel corpo degli archivi, con peso per titoli, simboli e percorsi, frammenti attorno alle corrispondenze e deduplicazione. L'archivio e' condiviso dal workspace. |
| Deposito | I riferimenti espliciti della conversazione in esecuzione sono protetti dalla potatura a quota. Lettura, scrittura e potatura rifiutano symlink/junction verso altre directory. |
| Ragionamento | Il livello torna al budget iniziale quando cambia il punto del piano o fallisce un tool. Il watchdog usa il tetto di generazione effettivo; un'interruzione per budget non diventa un completamento riuscito. |
| Finestra | La pressione per compattazione e scarto include gli schemi dei tool. I budget di lettura usano la stessa conversione stimata di 3,6 caratteri/token del resto del runtime. La delega parte dai limiti effettivi del padre. |
| Chiamate ausiliarie | Compattazione, estrazione del pensiero, referto della delega e riepilogo finale rispettano lo stop. Gli stream vengono chiusi e le risposte parziali, troncate o fallite non diventano memoria o riepiloghi pubblicati. |
| Backend | `think=false` viene trasmesso a Ollama, `repeat_penalty=1.0` viene esplicitato sui backend pertinenti. La diagnostica reasoning distingue parametro richiesto, payload e supporto conosciuto, senza affermare un'efficacia verificata. |

## Telemetria senza costo ripetuto a ogni tool

Un collector condiviso copre `main`, `delegate`, `vault_search`, `compaction`,
`memory_extract` e `final_summary`. Ogni chiamata registra durata, prima
uscita, esito, configurazione consentita, uso riportato dal backend e stime
del payload. Non salva prompt, risposte, URL del backend o testo degli errori.

I contatori cumulativi restano disponibili quando vengono espulsi i dettagli
oltre 512 chiamate. Le stime non sostituiscono i contatori mancanti. I campi
`*_reported_calls` permettono di distinguere una somma parziale da una misura
disponibile per tutte le chiamate. Il tempo complessivo del turno comprende
anche tool e gestione del loop; la somma delle durate delle chiamate e' una
misura distinta, che puo' sovrapporsi in caso di parallelismo futuro.

Il totale del turno include le chiamate ausiliarie, anche nei campi legacy
`prompt_tokens`, `completion_tokens` e `total_tokens` quando disponibili.
La sessione espone `turn_telemetry` nel payload API della conversazione.

I dati persistono in `chat_sessions/<id>.telemetry.json`, entro 50 turni e
2 MiB. Lo snapshot viene sostituito a fine turno; i salvataggi intermedi non
risanificano o riserializzano metriche invariate. La lettura supporta anche
il vecchio campo inline. In caso di errore del sidecar i messaggi vengono
comunque salvati, la telemetria rientra nei metadati come fallback e l'errore
resta visibile per consentire il retry. Il sidecar non compare come una chat
ed e' eliminato insieme alla sessione.

## Evidenza riproducibile

Le [sonde originali](context_probes.json) restano la fotografia prima dei
fix. Le [sonde dopo i fix](context_probes_after.json) riportano anche gli
hash dei sorgenti effettivamente eseguiti.

| Sonda senza LLM | Prima | Dopo |
|---|---|---|
| Tre handle del deposito nel risultato vecchio | Tutti persi | Tutti presenti |
| Lettura recente da 11.041 caratteri JSON | 6.026 caratteri, JSON non valido | 11.041 caratteri, JSON valido |
| Tentativo di scrittura fallito | Potatura con indicazione di contenuto salvato | Argomenti conservati |
| Evento vecchio univoco nel secondo input di compattazione | Ripetuto | Assente |
| Dimensione del secondo input sintetico di compattazione | 4.025 caratteri | 2.379 caratteri |
| Ricerca di termine presente solo nel corpo di un archivio | Nessun recupero | Frammento recuperato |

I nuovi test includono tre checkpoint successivi con un'attivita' aperta,
vincoli CRLF, archivio mancante, stato oltre budget, interruzioni durante
ciascuna fase, serializzazione JSON, payload HTTP dei backend, somma di
chiamate principali e ausiliarie, riuso dei callback fra turni e persistenza.
Un test operativo verifica che i salvataggi intermedi non chiamino la
sanificazione o la scrittura della telemetria invariata; non usa soglie
temporali fragili.

Comandi dalla radice:

```powershell
.\.venv\Scripts\python.exe -m ruff check core server tests
.\.venv\Scripts\python.exe -m pytest -q --disable-warnings --tb=short
.\.venv\Scripts\python.exe docs\research\context_probes.py
```

Suite completa: **1.443 passati, 11 saltati, 1 avviso**, in 253,56 secondi.
Esiti in `artifacts/context-full-tests-final.txt`. Dopo la correzione finale
del fallback della telemetria e l'asserzione sugli hash dei byte scritti,
sono passati anche tutti i **38 test d'integrazione** interessati:
`artifacts/context-final-integration-tests.txt`. Ruff su `core server tests`
non segnala errori.

## Limiti e valutazione successiva

Non e' stato eseguito un benchmark con un LLM reale. Queste prove dimostrano
comportamenti del software, non un guadagno percentuale di solve rate,
token/s, latenza GPU o riuso della KV cache. La ricerca lessicale non e' un
retriever semantico e i token stimati non sono il conteggio del tokenizer.

Il prefisso snello senza vault costa ancora circa 4.598 token stimati
inclusi gli schemi. Il suo costo viene ora incluso nelle decisioni di
pressione; questa revisione non introduce una selezione dinamica dei tool.

Lo stato attivo in primo piano ha un limite di 1.800 caratteri. L'eccedenza
resta nell'archivio con un riferimento esplicito e una catena alle fonti
precedenti. Senza archivio recuperabile la compattazione che taglierebbe
questo stato viene rifiutata. La chiusura delle voci dipende ancora dal
riassunto del modello: le omissioni sono protette, le dichiarazioni errate
di risoluzione richiedono una valutazione con compiti reali. Piano e note
persistenti rimangono disponibili come stato del lavoro.

Il recupero scandisce al massimo 2 milioni di caratteri per file e 16 milioni
per giro; il testo precaricato e' limitato a 3.000 caratteri. La protezione
del deposito riguarda i riferimenti della conversazione corrente e puo'
superare la quota disco: non e' una politica di conservazione globale delle
evidenze di tutte le conversazioni.

Il passo sperimentale successivo e' una valutazione appaiata su compiti
identici, finestre da 8k/32k/128k e modelli piccoli/grandi: successo verificato,
richieste violate dopo compattazione, chiamate ripetute, token totali incluse
le chiamate ausiliarie e tempo complessivo. La telemetria introdotta rende
questa misura possibile senza dedurla da soli commenti o trascrizioni.

## Ripristino e revisione

Il backup precedente alle modifiche e' conservato in
`artifacts/before-context-fixes-20260912-204910.zip`. Il workspace non contiene
un repository Git; non sono stati creati commit o effettuati deployment.
Il diff rispetto al backup e' in `artifacts/context-fixes-20260914.patch`;
`artifacts/context-fixes-20260914.zip` contiene sorgenti, test, nota e risultati
pertinenti. `uv.lock` e' incluso nel bundle con la versione aggiornata; non era
nel backup originale e non compare nel diff.
