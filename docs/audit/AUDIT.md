# Audit di Astra — 6 settembre 2026

Il progetto è stato modificato direttamente e sottoposto a test di regressione. Il codice completo è nel workspace e nell'archivio consegnato; questa relazione distingue i difetti accertati nel sorgente iniziale, le correzioni e i limiti ancora presenti. **Non certifico “zero crash”, latenza minima teorica o determinismo dell'LLM.** Sono proprietà che questo audit, e in particolare una suite con backend simulati, non può dimostrare.

La base confrontata è `D:/Astra-audit-original-20260905`. Non era presente un repository Git: il confronto è fra file e hash, non fra commit. L'inventario accompagna il rapporto e comprende sorgenti Python, entrambi i frontend, test, configurazioni, documentazione e asset. Dipendenze installate, cache e file temporanei non sono sorgenti del progetto.

## FASE 1 — Matrice dei bug e delle vulnerabilità

Le righe si riferiscono al **codice prima dell'intervento**. “Corretto” significa implementato e coperto dai controlli indicati, non immunità da ogni variante futura. Severità legata all'impatto e alle condizioni esplicitate; nessun CVSS inventato. I riferimenti numerici sono della copia iniziale.

| ID | Severità | Evidenza iniziale | Difetto e conseguenza | Intervento |
|---|---|---|---|---|
| C01 | CRITICAL | `server/main.py:1831`, `:1868`; API prive di controllo origine/peer | Il servizio può far eseguire tool con accesso al workspace. Le anteprime hanno un'altra porta ma sono same-site; l'assenza di autenticazione/origin guard lascia accessibili azioni da browser e, se bindato alla LAN, da client remoti. CORS limita la lettura delle risposte, non costituisce autorizzazione delle azioni. | `LocalAccessGuard`: confronto completo origine, Fetch Metadata, host locale contro rebinding; peer remoto richiede segreto configurato. Proxy mobile integra il controllo. Vedi limite proxy fidato sotto. |
| H01 | HIGH | `core/tools.py:3086`, `:1222`; `core/agent.py:2586` | Gli schemi descrivono i tool ma non impongono i tipi all'esecuzione. `replace_all: "false"` è una stringa truthy e può allargare una sostituzione; proprietà sconosciute vengono ignorate. | Validazione manuale ricorsiva dello schema effettivamente usato, senza coercizione, proprietà extra vietate, enum/bounds/profondità. Eseguita anche nel dispatcher. |
| H02 | HIGH | `core/agent.py:2539`, `:2586` | Il parsing permissivo accetta chiavi duplicate e costanti non JSON; root non-oggetto viene convertita in `{}`. `ask_user_question` ha un percorso separato che normalizza input non validi. | Unico confine JSON rigoroso; ask passa la stessa validazione degli altri tool. Errori strutturati, nessuna esecuzione. |
| H03 | HIGH | `core/agent.py:880` e recupero tool nel ciclo | Oggetti che assomigliano a chiamate dentro la risposta testuale possono diventare azioni: un esempio di codice o testo non autorevole supera il confine dati/esecuzione. | Recupero da testo disattivato per default. Solo opt-in esplicito `allow_text_tool_calls=True` per integrazioni legacy; anche lì valgono schema e allowlist. |
| H04 | HIGH | `core/backend.py:568`, `:613`, `:730` | NDJSON malformato viene saltato; EOF senza completamento non è un errore distinto. Tool accumulati possono essere trattati come finiti senza una chiusura valida del protocollo. | Frame UTF-8/JSON rigorosi e limitati; tool rilasciati solo dopo `done:true` o `finish_reason` più `[DONE]`. Nessuna azione su EOF, errore o truncation. |
| H05 | HIGH | `core/backend.py:568`, `:730`; retry nel ciclo | Gestione retry eterogenea: nessuna policy uniforme per 429/503/socket failure, tentativi dell'SDK sommati al recupero del loop. Un retry dopo output osservabile può duplicare contenuti o decisioni. | Un solo proprietario dei retry nel transport, massimo tre tentativi complessivi, backoff esponenziale con full jitter, `Retry-After` limitato, retry solo prima del primo evento osservabile. |
| H06 | HIGH | `core/backend.py:730` | Un nuovo client SDK viene creato a ogni generazione senza chiusura esplicita del client/stream. La liberazione dipende dal GC; in uso continuativo può aumentare la pressione sui socket. | HTTPX condiviso, contesto di risposta chiuso anche su errore/cancel; generatori chiusi dal loop in `finally`. |
| H07 | HIGH | `server/main.py:1831`, `:1868`, `:1151` | Check “turno attivo” e append/avvio non sono una singola ammissione: due POST concorrenti possono appendere due messaggi, poi uno fallisce all'avvio. | Lock di ammissione su chat/answer e operazioni correlate; test con due richieste simultanee verifica un solo messaggio. |
| H08 | HIGH | `server/main.py:1151`, `AppState.save` | Il worker legge impostazioni globali dopo l'avvio. Cambiare cartella/model nella UI può cambiare configurazione o metadati di un turno già legato a un'altra conversazione. | Copia delle impostazioni catturata all'avvio, workspace ricavato dalla sessione; upload cattura la stessa associazione. |
| H09 | HIGH | `core/session.py:77`, `:214` | Nome `.tmp` prevedibile, mancanza di serializzazione per sessione e fallimenti di salvataggio non restituiti al chiamante. Due scrittori interferiscono; il debounce può nascondere il fallimento. | Temporanei univoci, fsync, replace, lock per sessione, esito booleano e `save_error`; HTTP 507 prima dell'avvio se non è possibile salvare. |
| H10 | HIGH | `server/main.py:1151` | L'evento `done` precede il salvataggio completo finale. A disco pieno la UI può mostrare successo mentre la risposta non è persistita. | `done` differito fino all'esito del salvataggio; errore visibile e motivo `persistence_error` se fallisce. Risposta a una domanda aspetta la chiusura del worker precedente. |
| H11 | HIGH | `core/tools.py:1140`, `:1222`, `:431` | Scritture dirette possono lasciare file parziali; edit read/modify/write e scelta nome upload sono vulnerabili alla concorrenza. | Scritture atomiche, lock per percorso, upload con creazione esclusiva e tentativi limitati. Non costituiscono una transazione con processi esterni. |
| H12 | HIGH | `core/sandbox.py:325` | Per liberare porte viene rimosso un container dell'harness appartenente a un altro workspace, anche se attivo. Le operazioni ensure/remove possono correre fra thread. | Nessuna rimozione di workspace altrui per un conflitto: errore esplicito; lifecycle serializzato nello stesso processo. |
| H13 | HIGH | `core/tools.py:1630`, `core/sandbox.py:226` | `capture_output` accumula stdout/stderr prima di troncarli. Un comando verboso può esaurire la memoria molto prima del timeout. | Cattura su file temporanei, controllo di byte e tempo, terminazione del processo/gruppo dove supportata. Il limite non è una quota disco assoluta: vedi residui. |
| H14 | HIGH | `core/agent.py:2539` | Due domande nello stesso batch: la prima sospende e lascia id di tool senza risultato; anche gate del piano/cancel possono spezzare l'adiacenza della cronologia. | Validazione preventiva envelope/id; risultati espliciti per chiamate non eseguite, singola domanda pendente, ripresa con id corrispondente. |
| H15 | HIGH | `core/vault_search.py:89`, `core/delega.py:298`; inizializzazione `run_turn` | Il cercatore può scambiare un `AssistantTurn` con tool per il referto finale ed uscire prima di eseguire la ricerca. Un figlio inizializza il workspace e può cancellare lo scratch del padre. | Il referto richiede `has_tool_calls=False`; figli senza reset dello scratch e con stato/callback ridotti; cancellazione propagata. |
| H16 | HIGH | `server/previewhost.py:184`; ricerca file in `core/tools.py` | Il percorso directory è verificato prima di aggiungere `index.html`: un index symlink può uscire dalla root. Una ricerca può leggere file symlink esterni pur camminando nella root. | Seconda risoluzione dell'index e controllo del singolo file ricercato. Restano TOCTOU con processi ostili esterni e privilegi Windows. |
| M01 | MEDIUM | `core/backend.py:613`; `core/tools.py:838` | Dimensioni controllate dopo l'accumulo: linea di stream senza newline, immagini grandi, letture integrali per ricavarne poche righe. | Cap di frame/stream/argomenti; immagini lette fino al limite+1; letture tool e preview bounded. Altri endpoint diagnostici restano da limitare prima del buffering. |
| M02 | MEDIUM | `server/runner.py:172`, `server/main.py:962` | I generatori SSE sincroni aspettano su queue bloccanti e occupano capacità del threadpool per client inattivi. Con molte connessioni le normali richieste possono attendere. | Stream ASGI asincroni con notifica thread-safe dal worker e keepalive. Test con 64 consumer concurrenti. |
| M03 | MEDIUM | `server/runner.py:85` | Buffer replay senza budget in byte; la coda di un client lento può perdere frame mentre il turno prosegue. | Replay massimo 32 MiB e 64k frame; oltre il limite stop esplicito. Subscriber lento disconnesso e ricostruibile dal replay. |
| M04 | MEDIUM | `web/app.js`, funzione `leggiLoStream` | EOF o frame non JSON possono essere accettati come chiusura normale, lasciando UI desincronizzata senza ricostruzione della risposta. | JSON invalido produce errore; EOF senza `done`/`idle` attiva recupero; buffer limitato e reader cancellato/rilasciato. |
| M05 | MEDIUM | `server/mobile.py:239` | Body letto integralmente, timeout read/write illimitati, inoltro di cookie/credenziali non necessari. Decompressione HTTPX con header Content-Encoding ancora inoltrato può far decodificare due volte. | Limite body, timeout finiti, filtro hop-by-hop e credenziali, origine ricostruita, rimozione Content-Encoding e Set-Cookie upstream. |
| M06 | MEDIUM | `core/agent.py:1236` | La somma che riserva spazio all'output omette gli schemi dei tool. Il budget apparentemente disponibile non corrisponde al payload inviato. | Costo dello schema sottratto nel controllo finale della finestra. La stima non equivale al tokenizer del modello. |
| M07 | MEDIUM | `core/session.py:116`, caricamento metadati/settings/memorie | JSON sintatticamente valido con forma errata può causare `.get`, conversioni o iterazioni non valide; memorie/note caricate possono crescere oltre i limiti di inserimento. | Shape validation e limiti sui dati caricati; errori di storage registrati; preservati i formati di sessione compatibili. |
| M08 | MEDIUM | `core/backend.py:324`, `:344` | Le diagnostiche possono memorizzare un array come scheda modello o iterare `models` numerico; i consumatori `.get` falliscono fuori dalla protezione. | Oggetto e lista verificati prima della cache/iterazione; capability ignota restituita come tale. Numeri uso/token patologici rifiutati. |
| M09 | MEDIUM | `core/prompts.py:38`, `:225`; `core/memory.py` | Contratto ripetitivo, istruzioni di permesso poco coerenti e memoria descritta come vincoli autorevoli. Aumentano costo del prefisso e rischio di seguire contenuti non affidabili. | Contratto condiviso breve, dati separati dalle autorizzazioni, error correction esplicita; prompt lean effettivamente più piccolo. |
| M10 | MEDIUM | `core/tools.py:1630` | Su Windows quoting della shell può alterare comandi con eseguibili/argomenti tra virgolette e restituire un falso fallimento. | Command line nativa `cmd /d /s /c`, senza escaping CRT improprio; test reale con percorso contenente spazi e exit code 3. |
| M11 | MEDIUM | `core/tools.py:1990` | `package.json` root array o `scripts`/dipendenze con tipo errato causano errori nel riconoscimento dell'anteprima. | Parsing object rigoroso e shape guard dei campi; lettura limitata. |
| M12 | MEDIUM | `core/session.py:73` | Solo 16 bit casuali oltre al timestamp al secondo: creazioni concentrate possono condividere id e sovrascrivere la stessa sessione. | UUID completo, con compatibilità di lettura degli id precedenti. |

Non ho riscontrato un ciclo letteralmente infinito nel normale `run_turn`: c'erano già `max_steps`, budget dei solleciti e guardie di ripetizione. Il difetto era la copertura dei confini e dei percorsi di sospensione, non l'assenza totale di limiti. Allo stesso modo, il progetto usa FastAPI e JavaScript, **non Streamlit**; non attribuisco al codice re-render Streamlit inesistenti. Il frontend aveva già un accorpamento degli aggiornamenti di circa 80 ms.

### Rischi residui del codice consegnato

Questi punti restano aperti. Vanno considerati prima di trasformare un harness locale in un servizio esposto o multiutente.

| Severità | Residuo | Conseguenza e condizione |
|---|---|---|
| HIGH | Nessun journal transazionale exactly-once per effetti dei tool | Un crash dopo una modifica esterna e prima del risultato persistito lascia un esito incerto. Il runtime non può dedurre se una successiva richiesta debba ripetere l'azione. Per comandi arbitrari non esiste una deduplicazione universale: servono ricevute/idempotency key nei sistemi destinatari. |
| HIGH | Stato in memoria e lock validi per un solo processo | Due worker Uvicorn o due istanze sulla stessa directory non condividono lock, runner e transazioni. JSONL e metadati sono due file, non un commit atomico di database. Deployment supportato in questa revisione: singolo processo per archivio e utente fidato. |
| HIGH | Limiti per turno, non quota globale di servizio | Numero di turni attivi, sessioni residenti e alcune cache non hanno una quota globale. Restano possibili saturazione di RAM/thread sotto carico ostile e necessità di admission control centralizzato. |
| HIGH | La modalità host esegue codice con i privilegi del processo | Regex sui comandi non sono isolamento. Python o un eseguibile può toccare risorse fuori dal workspace. La sandbox Docker deve essere il confine per codice non fidato; rete e volume scrivibile restano capacità reali. |
| HIGH | Anteprima autorizzata a leggere la propria root | Una pagina con script può leggere ed esportare dati accessibili sulla sua origine. L'origine separata protegge le API, non i segreti eventualmente presenti nella cartella servita. Non è stata introdotta una sandbox di egress o una root pubblica copiata e separata. |
| MEDIUM | Deadline/cancel cooperativi sul transport sincrono | Lo stop è verificato fra letture e durante il backoff. Una lettura socket silenziosa può durare fino al read timeout configurato; il budget totale non è un timer che interrompe istantaneamente una syscall in corso. |
| MEDIUM | Cattura subprocess su disco con controllo periodico | Limita la memoria Python e interrompe processi rumorosi, ma il file può superare la soglia fra due controlli. Processi Windows detached non sono tutti controllati da un Job Object; non è un hard cap dell'intero albero in ogni configurazione. |
| MEDIUM | Altre superfici di I/O non uniformemente bounded | Diagnostiche backend usano ancora risposte non-stream prima della validazione; web_search HTML usa buffering e una policy diversa dai retry LLM; lettura JSONL totale, cartelle enormi e dialoghi nativi richiedono budget dedicati. |
| MEDIUM | Compattazione e conteggio token euristici | `estimate_tokens` non usa il tokenizer effettivo; costo immagini, template del server e compattazione possono deviare. `_compact_tool_result(full=True)` può ancora rappresentare un risultato troncato come testo, non un oggetto JSON integro. Non è un percorso di esecuzione, ma riduce la qualità della correzione. |
| MEDIUM | Confinamento path non atomico rispetto a processi ostili esterni | Resolve/check e open/replace non sono un'unica operazione con handle protetto su ogni piattaforma. I test symlink reali richiedono privilegi/Developer Mode Windows. |
| MEDIUM | Prompt injection e successo semantico | Schema e allowlist restringono la forma e i tool disponibili, ma non dimostrano che l'azione scelta serva il compito. Una risposta finale o un test insufficiente non costituiscono prova di correttezza funzionale. |
| MEDIUM | Proxy e trasporto mobile | Un reverse proxy che presenta ogni peer come loopback deve imporre autenticazione e validare host/origine a monte. Il token mobile è un segreto bearer e il server integrato non termina TLS; non è stato validato un deployment Internet. |

## FASE 2 — Attacco da edge-case

### 1. JSON ambiguo nel momento di una scrittura

Il modello produce una chiamata `edit_file` con `replace_all: "false"`, oppure due chiavi `filepath` diverse, oppure `content` con una newline non escaped. Nel codice iniziale la coercizione/truthiness o il decoder permissivo possono cambiare il significato dell'azione. Il caso aggiuntivo di JSON dentro una spiegazione può diventare direttamente un tool.

Ora: ambiguità rifiutata prima del tool, errore con percorso del campo; la cronologia contiene il risultato negativo associato all'id, quindi il modello può correggere al passo successivo. Code fence e trailing comma non vengono “riparati” automaticamente: modificarli potrebbe modificare il codice da scrivere. Gli escape validi vengono preservati. Prove: `test_audit_loop.py`, `test_audit_tools.py`, inclusa correzione reale al secondo passo e controllo che il file sbagliato non esista.

### 2. Server LLM che cade dopo aver emesso una chiamata

Ollama invia testo e un tool ma non `done:true`; oppure l'endpoint compatibile emette delta degli arguments, poi chiude TCP senza `[DONE]`. Una linea precedente è malformata. Nel codice iniziale la linea viene saltata e EOF non costituisce un confine di errore affidabile; i tool accumulati possono sembrare completi. Un retry indipendente dell'SDK e del loop rende il recupero poco prevedibile.

Ora: nessun tool è pubblicato prima della chiusura di protocollo verificata. EOF, frame incompleto, id duplicato e `finish_reason=length` non autorizzano azioni. Prima di output visibile un 429/503 o guasto transitorio può avere al massimo tre tentativi, con attesa limitata; dopo output visibile non si ripete automaticamente la generazione. Prove wire HTTP in `test_audit_transport.py`, incluse UTF-8 spezzata, frame sovradimensionati, status, uso numerico patologico e chiusura delle risorse.

### 3. Due richieste, due cartelle e disco pieno alla fine

Due client inviano nello stesso istante sulla medesima chat. La UI cambia workspace mentre il thread sta partendo. Il modello apre due domande nello stesso batch; al momento della chiusura il disco rifiuta il replace. Il codice iniziale può appendere due prompt, far leggere configurazione di un'altra cartella, lasciare id orfani e annunciare la fine prima di scoprire la perdita del salvataggio.

Ora: l'ammissione è serializzata, il secondo avvio riceve 409 senza un secondo prompt; configurazione catturata all'avvio; chiamate sospese chiuse esplicitamente; 507 impedisce di iniziare se il prompt non è salvabile, e `persistence_error` chiude il turno se fallisce il salvataggio finale. Prove concorrenti ASGI in `test_audit_server.py`, test di sospensione in `test_audit_loop.py`, errore di disco in `test_audit_runtime.py`. Il crash dopo un effetto ma prima della sua ricevuta resta il limite exactly-once descritto sopra.

## FASE 3 — Refactoring concreto e codice completo

La consegna non è un frammento sostitutivo o uno scheletro: contiene tutti i file di `core`, `server`, `web`, `web_mobile`, la suite, script di avvio, dipendenze e documentazione. `changes.patch` rende verificabile il confronto con la base iniziale. I moduli già adeguati sono conservati: riscrivere CSS, icone o algoritmi senza un difetto non aggiungerebbe affidabilità. Il progetto Python resta annotato nello stile esistente; i nuovi moduli esplicitano tipi e contratti. Non è stata eseguita una certificazione statica completa con mypy/pyright.

| Confine | Implementazione consegnata |
|---|---|
| Rete e protocollo | `core/backend.py`: pool condiviso, retry unico, framing bounded, verifica completamento, cancellazione cooperativa, metriche validate. |
| Dati → azioni | `core/jsonsafe.py`, `core/tool_validation.py`, `core/tools.py`: niente riparazioni ambigue, allowlist, schema e errori utilizzabili dal modello. |
| Loop e memoria | `core/agent.py`, `core/delega.py`, `core/vault_search.py`: batch con id validi, risultati abbinati, sospensione coerente, figli senza reset padre, costo schema nel budget. |
| Filesystem | `core/atomic.py`, `core/session.py`, `core/settings.py` e moduli memoria/vault: replace atomico, transazioni locali protette, errori visibili, shape validation. |
| Processi | `core/process.py`, `core/sandbox.py`: cattura su temporanei e budget, comando Windows verificato, lifecycle Docker serializzato e conflitto porte non distruttivo. |
| API e UI | `server/security.py`, `server/main.py`, `server/runner.py`, `server/mobile.py`, `web/app.js`: accesso, ammissione, SSE asincrono, replay limitato, completamento dopo persistenza. |

Il loop è un generatore sincrono eseguito in un worker dedicato. Le connessioni SSE e il proxy sono asincroni. Non ho convertito I/O bloccante in `async def` lasciandolo sul thread ASGI: avrebbe peggiorato la reattività. Le tool call arrivano incrementalmente, ma l'esecuzione aspetta il completamento e la validazione; questa attesa protegge da azioni basate su JSON parziale.

L'ordine dei tool nello stesso passo resta sequenziale. Possono condividere stato, cache di lettura, piano e filesystem: parallelizzarli indiscriminatamente introdurrebbe race. Una futura schedulazione parallela richiede metadati affidabili su letture/scritture, separazione dello stato e risultati ricongiunti per id; non basta chiedere al modello che siano “indipendenti”. Concorrenza delle connessioni e isolamento dei turni sono già esercitati nei test.

La latenza ottenuta è valutata tramite regressioni funzionali e concorrenza simulata. Non sono stati misurati TTFT, p50/p95/p99 o throughput su una GPU di produzione, né verificata la cache KV di un modello reale. Il prompt ridotto, il pooling, l'eliminazione dei retry sovrapposti e la rimozione delle attese SSE dal threadpool hanno una motivazione tecnica verificabile; non assegno loro percentuali di guadagno senza benchmark.

### Esecuzione riproducibile

Da una copia pulita del progetto con Python 3.12 o superiore e `uv`:

```powershell
uv sync --locked --extra dev
uv run --locked python -m pytest -p no:cacheprovider -q -ra
uv run --locked python -m ruff check core server tests run.py run_mobile.py --no-cache
uv run --locked python run.py --no-browser
```

Il server parte su `127.0.0.1:8123`. Per l'interfaccia mobile si conserva `python run.py --mobile`; la chiave mobile viene generata come prima. `HARNESS_API_TOKEN`, quando configurato, abilita client remoti dell'API principale che inviano `X-Harness-API-Token`; non disabilita il controllo origine del browser. Nessun servizio è stato pubblicato o avviato in produzione durante l'audit.

Il lockfile rende ripetibile l'ambiente Python verificato. Il Dockerfile mantiene le sue dipendenze/riferimenti preesistenti: non equivale a un'immagine attestata o a un digest immutabile. I controlli svolti non comprendono vulnerability scanning del contenuto delle immagini né test del demone Docker reale.

### Cambiamenti intenzionali dei contratti

- Parametri sconosciuti e tipi convertibili ma sbagliati sono rifiutati; non vengono ignorati.
- JSON nella risposta testuale non esegue tool, salvo opt-in legacy esplicito.
- Stream compatibili devono segnalare una conclusione valida; EOF non vale successo.
- I conflitti porte non autorizzano a fermare altri workspace.
- `save_session` restituisce un esito e non considera riuscito un salvataggio fallito.
- I timeout del proxy sono finiti. I keepalive SSE mantengono viva una connessione sana.

I test preesistenti che imponevano il comportamento insicuro sono stati aggiornati a questi contratti. Le simulazioni OpenAI usano il filo HTTP e marker finali, invece di oggetti SDK; la fixture Docker Windows esegue direttamente lo script stub per evitare che `cmd` interpreti i pipe dei template Go. Non si è nascosto un fallimento rilassando una verifica di funzionalità equivalente.

## FASE 4 — System prompt revised e self-correction

Il testo completo usato dal runtime è in `core/system_prompt.py` ed è esportato integralmente in `SYSTEM_PROMPT_REVISED.md`. Esistono una base lean e l'estensione con la scelta dei tool; `core/prompts.py` importa le stesse costanti, evitando due contratti divergenti. Prompt custom già salvati dall'utente non vengono riscritti automaticamente.

Il prompt prescrive function calling nativo, tipi esatti, separazione fra istruzioni e dati, controllo degli effetti dopo timeout, una domanda per passo, verifiche pertinenti e arresto esplicito. **L'applicazione impone i confini con codice**: il prompt non può blindare da solo un modello probabilistico.

Il risultato reale di una chiamata `edit_file` con `replace_all` stringa è:

```json
{
  "error": "Argomenti non validi per 'edit_file'.",
  "error_code": "invalid_arguments",
  "tool": "edit_file",
  "retryable": false,
  "details": [
    {
      "path": "$.replace_all",
      "message": "Tipo richiesto: boolean; ricevuto: str."
    }
  ],
  "hint": "Correggi i campi indicati e invia una nuova chiamata conforme allo schema. Nessuna azione e' stata eseguita; non ripetere gli stessi argomenti. Tipo richiesto: boolean; ricevuto: str."
}
```

Questo oggetto viene inserito come messaggio `role=tool` con il `tool_call_id` originale. Al passo seguente il modello vede quale campo correggere e può emettere una nuova chiamata nativa con `replace_all:false`. `retryable:false` impedisce il retry identico automatico; non vieta la correzione degli argomenti. I tentativi restano nel budget dei passi.

Per `invalid_json`, la risposta include una porzione limitata dell'input ricevuto come dato e l'indicazione di riemettere un oggetto valido. Gli arguments storici invalidi vengono normalizzati a `{}` solo nella proiezione API della cronologia: Ollama richiede un oggetto nel proprio formato di messaggi. La traccia UI conserva l'input originale e l'errore, evitando una correzione silenziosa che riscriva la storia dell'azione.

Un errore di protocollo prima del completamento interrompe invece quella generazione: non si inventa una tool call per chiedere al modello di riparare dati che il trasporto non ha consegnato integralmente. Il controllo degli effetti dopo timeout è separato dal retry di rete, perché “non ho ricevuto la risposta” non significa “l'azione non è avvenuta”.

### Fonti tecniche consultate

La distinzione fra timeout per fase/inattività e deadline globale segue la documentazione [HTTPX Timeouts](https://www.python-httpx.org/advanced/timeouts/). Le classi di errore e la portata limitata dei retry di trasporto sono verificabili in [HTTPX Exceptions](https://www.python-httpx.org/exceptions/) e [HTTPX Transports](https://www.python-httpx.org/advanced/transports/). La chiusura delle proprietà di un oggetto richiede una scelta esplicita nello schema, come descritto in [JSON Schema — Objects](https://json-schema.org/understanding-json-schema/reference/object). Il validatore qui implementato supporta il sottoinsieme usato dagli schemi del progetto; non pretende di implementare ogni draft di JSON Schema.

I risultati quantitativi finali, gli skip effettivi e i limiti delle prove sono riportati in `VALIDAZIONE.md` e nel log della suite allegato.
