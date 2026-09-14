# Tool boundary audit — 2026-09-06

Riferimenti alla copia originale `D:/Astra-audit-original-20260905`, prima del refactoring.

| Severità | Riferimento originale | Difetto dimostrabile | Intervento |
|---|---|---|---|
| HIGH | core/tools.py:3104–3106 | Il dispatcher scarta parametri sconosciuti senza validare tipi, obbligatorietà o enum. `replace_all="false"` viene valutato come vero; `ignore_red="false"` può aggirare il blocco della verifica. Input non dict fallisce prima del catch. | Validazione ricorsiva strict dallo stesso schema inviato al modello; unknown/missing/tipi/limiti vengono rifiutati prima di effetti. |
| HIGH | core/tools.py:1391–1397 | `search_files` confina la cartella iniziale ma legge file symlink che possono puntare fuori workspace. Il probe binario legge anch'esso il target esterno. | Risoluzione e confinamento di ogni file prima del probe e della lettura. |
| HIGH | core/tools.py:445–449 | Upload: `exists()` seguito da `write_bytes()` perde la gara tra upload concorrenti; un link pendente sembra inesistente e viene seguito. | Creazione esclusiva `xb`, collision retry limitato, nome alternativo. |
| HIGH | core/tools.py:1200,1292 | Scritture in-place: un guasto tronca il file originale; edit concorrenti perdono aggiornamenti. Scrivere su hardlink modifica anche l'altro percorso. | File temporaneo univoco e sostituzione atomica; lock per transazione attraverso dispatcher. Test hardlink verifica separazione dei contenuti. |
| HIGH | core/tools.py:1186–1188 | Errore di lettura precedente viene convertito in testo vuoto: write_file procede senza poter controllare contenuto e guardie dei test. | Rifiuto della scrittura quando la lettura preliminare fallisce. |
| HIGH | core/tools.py:1076,1259,1397; core/sandbox.py:227; core/tools.py:1635 | File e stdout vengono caricati interamente in RAM prima del troncamento: budget token non equivale a budget memoria. | Letture tool limitate a file regolari <=8 MiB; processo bounded assegnato al coordinatore. |
| HIGH | core/sandbox.py:413–421 | Avviare un workspace elimina container attivi di altri workspace per liberarne le porte. | Fix lifecycle/conflitti implementato dal coordinatore: verificare suite sandbox nel risultato integrato. |
| MEDIUM | core/spec_delega.py:99,120–124 | Read/append/write senza lock e tempfile fisso: perdita aggiornamenti o interferenza tra turni. `int(passi)` può fallire fuori dal catch; JSON UTF-8 invalido sfugge al catch. | Lock sulla transazione, atomic helper, lettura bounded e tipi rigidi per fatti salvati; warning sui guasti di scrittura. |
| MEDIUM | core/sandbox.py:358–364 | Check/remove/create concorrenti sullo stesso nome container non serializzati. | Fix lifecycle assegnato e implementato dal coordinatore. |
| MEDIUM | core/tools.py:1990–1994 | `package.json` sintatticamente valido ma non object o con `dependencies` non object genera errori inattesi durante riconoscimento backend. | Segnalato al coordinatore; non corretto in questo subtask. |

## Contratto di self-correction

`core.tools.validate_tool_arguments(name, args)` ritorna `None` se conforme; altrimenti un object con `error` stringa, `error_code`, `tool`, `retryable: false`, `details: [{path, message}]` e `hint`. L'errore di validazione dichiara che nessuna azione è stata eseguita. Il loop deve consentire una nuova chiamata corretta entro un budget finito, senza rieseguire automaticamente la stessa mutazione. La funzione copre anche `ask_user_question`, che il loop gestisce separatamente.

## Verifica eseguita

`python -m pytest -q tests/test_audit_tools.py tests/test_spec_delega.py tests/test_core.py`: **135 passed, 3 skipped**, 5.17 s. Due skip sono test di symlink reali: questo host Windows non concede creazione symlink; il terzo era preesistente. I test includono 12 upload concorrenti, 12 edit indipendenti concorrenti, 40 append concorrenti, errore simulato durante replace e hardlink reale. Avviso pytest: cache non scrivibile; nessun test fallito.

## Limiti che restano da dichiarare

- I lock Python coordinano thread dello stesso processo; non costituiscono transazioni tra più processi o con editor esterni.
- `resolve()` seguito da `open()` non chiude ogni TOCTOU con un processo locale ostile che sostituisce directory/junction tra controllo e uso. Per isolamento forte occorrono filesystem broker/handle relativi o processi isolati.
- Il motore `re` della ricerca può avere backtracking patologico; limitare dimensioni non equivale a timeout deterministico del match. Il lavoro va isolato in un processo con deadline o migrato a un motore con limiti verificabili.
- `_leggi_un_po()` usato dall'anteprima legge ancora il file intero prima dello slicing nella versione ispezionata; fix affidato al coordinatore.
- Host mode esegue shell con i diritti dell'utente. Le regex contro comandi distruttivi sono euristiche e aggirabili; non autorizzano a dichiarare un confine di sicurezza.
- Docker può ancora avere egress di rete e mount workspace scrivibile, secondo configurazione. L'assenza di crash assoluta non deriva dai test locali o dai prompt.
- In `vault_search`, i percorsi diretti a vault non registrati sono accettati esplicitamente: è una scelta di prodotto da includere nel modello di fiducia, non una allowlist dei soli vault registrati.
