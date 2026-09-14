# Chiusura dell'audit del 30/08/2026

**31/08/2026 · ramo `audit-fix` · 1.037 test verdi · ruff pulito**

L'audit aveva elencato 3 Critical, 23 High, ~74 righe Med e ~74 Low. Questo
documento dice **cosa è stato fatto di ognuna**, e soprattutto cosa **non** è
stato fatto e perché. Tre categorie:

| | |
|---|---|
| **Corretto** | il difetto c'era, è stato tolto, e c'è un test che lo rifarebbe fallire |
| **Ritirato dopo misura** | il rilievo era una previsione sui costi, l'ho misurata, e il numero dice che non vale la pena. Il numero sta nel codice |
| **Lasciato di proposito** | la correzione costerebbe più del difetto, o non è provabile da qui. La ragione sta nel codice, accanto a ciò che non è stato cambiato |

Fuori perimetro per tua decisione: la chiave API in chiaro (C1) e i due
rilievi di rete (Origin/CSRF sulle rotte che scrivono, guardia su
`--host 0.0.0.0`).

---

## Le cinque cose che si vedono

### 1. Il client HTTP condiviso valeva davvero — 78 ms a richiesta

L'audit lo dava per Med "efficienza". Prima di implementarlo l'ho misurato,
aspettandomi di ritirarlo come gli altri tre rilievi di efficienza. Il
risultato è l'opposto:

```
su localhost, 200 richieste:
  senza client condiviso: 80,12 ms l'una
  con client condiviso:    1,51 ms l'una
```

Su localhost la richiesta non costa quasi niente, quindi *tutto* il tempo è
l'apertura della connessione. E le sonde non sono rare: `props()` gira da
`gen_params()`, cioè **a ogni turno**. Dodici chiamate `httpx.get/post/stream`
di modulo sono diventate un `get_client()` di modulo — di modulo e non di
istanza, perché il server ricostruisce il backend a ogni richiesta HTTP.

### 2. Il frame `done` poteva perdersi — trovato da un test intermittente

`TurnRunner.stream()` fotografa l'arretrato, si abbona, e **poi** guarda se il
turno è finito. Fra quelle due cose il worker può emettere altri frame e
chiudere: quei frame erano già nella coda dell'abbonato, e l'uscita rapida li
buttava via. Fra loro c'è `done`, cioè l'unico frame che dice al client che il
turno è chiuso.

Sul telefono voleva dire **risposta a metà e rotella che gira, per sempre**.

Non era nell'audit. È saltato fuori come un test che falliva una volta ogni
tanto e mai da solo. Il generatore rende la corsa deterministica — fino al
primo `next()` non è successo niente — quindi adesso c'è un test che la
riproduce e che senza la correzione fallisce sempre.

### 3. Il prompt "snello" diceva due volte la stessa cosa

La sezione «Chiedi tutto quello che ti serve in una volta» ripeteva per intero
la regola sul raggruppamento già data sotto «Un passo alla volta». Stessa
istruzione, due paragrafi, ~180 token pagati **a ogni singola richiesta** — nel
prompt che si chiama snello. Tolta insieme alla statistica di sessione che
raccontava al modello un aneddoto sulle sessioni passate dell'harness.

`SYSTEM_PROMPT_LEAN`: 3.001 → 2.822 token.

### 4. L'esito di `run_command` si leggeva cercando una sottostringa

`'"esito": "ok"' not in result[:200]`. Due modi di sbagliare, e tutti e due
dipingono di rosso una tendina verde: il campo `esito` viene dopo `command` e
`stdout` nella busta, quindi con un comando lungo a 200 caratteri non ci si
arriva e **ogni comando riuscito veniva contato come fallito**; e al contrario,
un output che contiene quella stringa per conto suo faceva passare per riuscito
un comando fallito. Adesso si parsa la busta, come già faceva `_esito_del_tool`.

### 5. Il finto modello si ripulisce da solo, e nessun turno sopravvive al test

Due difetti di isolamento della suite, entrambi con lo stesso modo di
manifestarsi: un test che fallisce **a intermittenza e mai da solo**, per un
motivo che non ha niente a che vedere con quello che prova.

- `SCRIPT` è un globale di modulo che sedici test di `test_agent_loop` e altri
  sei file sostituiscono. Il ripristino era a carico di ognuno di loro. Ora lo
  fa la fixture `fake_ollama`, che quei test chiedono comunque.
- I worker sono thread demoni: un turno rimasto in volo tocca lo stato globale
  *mentre gira il test successivo*. L'attesa c'era solo in `test_mobile_proxy`;
  è passata in `conftest.py`, che è il posto la cui ragione dichiarata è
  «vale anche per il prossimo file di test, che nessuno si ricorderà di
  attrezzare».

---

## Ritirati dopo misura

Quattro rilievi di efficienza dell'audit erano previsioni. Le ho misurate e i
numeri stanno nel codice, accanto a ciò che non è stato cambiato — così il
prossimo che legge non li "corregge" a scatola chiusa.

| Rilievo | Misura | Esito |
|---|---|---|
| tre stime di token per passo | ~7 ms | ritirato |
| `compaction.taglio()` chiama `costo()` per ogni blocco | 0 ms, 7 chiamate (è una ricerca binaria) | ritirato |
| `uncovered_symbols` rilegge i test del workspace | 18 ms su 300 file | ritirato |
| `_compact_tool_result` fa `json.loads` a ogni passo | 2,8 ms su 60 passi, di cui 1,5 di `json.loads` | ritirato |
| `libreria.precarico` ri-tokenizza tutti i titoli | 0,3 ms; `voci()` 3,1 ms su 100 voci | ritirato |

Nello stesso punto c'era però un difetto vero che la misura non copriva:
`_compact_tool_result` tagliava il JSON **già serializzato** per gli errori
lunghi, e il modello riceveva `{"error": "Traceback (most rec` — JSON invalido
in mezzo a risultati tutti ben formati, e proprio sul messaggio che spiegava
cosa fosse andato storto. Corretto: si accorcia il campo, non la busta.

---

## Lasciati di proposito

### Il container gira come root

Passare `--user` con l'uid dell'host sembra ovvio e non si ripaga: quell'uid
dentro l'immagine non ha una voce in `/etc/passwd` né una home scrivibile, e
soprattutto **`pip install` fallisce** — e l'agente lo usa davvero, dentro un
turno. Si scambierebbe un fastidio sui permessi dei file (solo su host Linux)
con un tool che smette di funzionare a metà lavoro. Farlo bene vuol dire home
scrivibile, `PIP_USER` e un venv nell'immagine, e va provato su un host Linux
vero. La ragione è scritta accanto agli argomenti di `docker run`.

### L'immagine di base senza digest

È la scelta **opposta** a quella fatta per il binario di ttyd venti righe più
giù, quindi nel codice c'è scritto perché. Il digest darebbe build
riproducibili; ma questa immagine è il recinto in cui gira l'agente, si
ricostruisce di rado, e un digest fissato oggi vuol dire restare su una Debian
senza le patch dei mesi successivi. Per ttyd non c'è il dilemma: è un binario
preso da internet, e verificarlo non costa niente in aggiornabilità — infatti
adesso si verifica (SHA-256 per architettura, calcolati sui file veri).

### Nessuna Content-Security-Policy

Su due pagine, per due ragioni diverse, scritte in tutti e due i posti.

- **Le anteprime** (`previewhost`) servono pagine scritte dal modello, e il
  loro mestiere è funzionare: script inline, fogli di stile, librerie da un
  CDN. Una CSP stretta le romperebbe tutte; una larga abbastanza da lasciarle
  funzionare non vieta niente. Ciò che protegge è già lì e non è
  un'intestazione: origine separata, niente cookie, niente CORS, `nosniff`,
  radice confinata da `resolve_path`.
- **L'interfaccia** gira su localhost, la serve lo stesso processo che serve
  la sua API, e non ci sono terze parti. L'unico contenuto non nostro sono le
  anteprime, che vivono sull'altra origine: è lì che sta il confine.

### `/api/bootstrap` manda la chiave API alla pagina

La manda perché **è il pannello impostazioni a modificarla** (`#s-api-key`).
Non è una fuga: è il campo. Quello che era un difetto vero — il campo
*sembrava* mascherato e in Firefox mostrava la chiave in chiaro, perché
`-webkit-text-security` lì non esiste — è corretto.

---

## Il resto, per area

**`core/backend.py`** — client condiviso; il secondo tentativo dopo un 400 non
gira più dentro il `with` della risposta fallita (teneva aperta una
connessione del pool per tutta una generazione); `x or None` scritto come si
legge, con il perché (`None` = "il template è un indizio, non una
dichiarazione").

**`core/agent.py`** — `isinstance` sugli eventi al posto del confronto sul nome
della classe (e i finti `run_turn` dei test ora cedono gli eventi veri: erano
proprio i sosia a rendere invisibile un rinominamento); l'unione `AgentEvent`
elenca tutti e 13 gli eventi, con un test che la tiene allineata a
`_EVENT_NAMES`; i tre rami dei solleciti non accodano più messaggi `assistant`
vuoti.

**`core/session.py`** — un lucchetto sulle due cache di modulo: `get` seguito
da `move_to_end` sono due operazioni, e fra le due un altro thread poteva
sfrattare la voce — `KeyError`, cioè la ricerca dell'utente che muore con un
500 su un'ottimizzazione. Il test lo riproduce in modo deterministico (due sole
chat, cache da una, switch interval al minimo). Una riga tronca nella coda
adesso sparisce dal file al salvataggio dopo, invece di restarci per sempre.

**`core/tools.py`** — `wait_s` dichiarato nello schema (era una manopola che
esisteva e che il modello non poteva girare), con un test che tiene allineati
`_ALLOWED_ARGS` e gli schemi; `total_lines` contato come lo conta un editor;
`looks_like_server` non scambia più "observe" per un server.

**`core/vault.py`** — il nome del vault non può più chiudere l'attributo del
blocco che il modello legge; `scaffold` non solleva su cartella non
scrivibile; un solo taglio per le note (erano due politiche, e la stessa nota
cambiava di un carattere a ogni giro).

**`server/`** — la schermata di prontezza tiene una fotografia di 5 secondi
(era `backend.status()` a 4 s di timeout più due sottoprocessi docker a ogni
chat aperta), buttata da tutto ciò che può cambiare il verdetto; `session_stats`
non fa più fallire un salvataggio riuscito quando la chat aperta è stata
cancellata altrove; niente più attributi privati letti da fuori la classe.

**Frontend** — `aria-live` sulla riga di stato del turno (non sul thread: lì
uno screen reader rileggerebbe la risposta a ogni token), `role`/`aria-selected`
sulle schede, `color-scheme`, colonne che si stringono sotto i 1100px invece di
restare fisse, il pannello di errore all'avvio che non distrugge più la pagina
e ha un "Riprova". Il renderer markdown conserva il livello dei titoli e usa un
segnaposto che un documento non può contenere. Sul telefono grassetto, codice
inline ed elenchi diventano nodi veri — costruiti con `createElement`, mai
`innerHTML`, quindi non riaprono niente.

**Configurazione** — `pyproject.toml` e `requirements.txt` non possono più
divergere (c'è un test); il contesto di build della sandbox esclude
`node_modules`, `.venv`, `.git` e compagnia, con un file che si chiama
`Dockerfile.sandbox.dockerignore` di proposito, così non tocca i `docker build`
che fai tu nella stessa cartella.

**Suite** — 997 → 1.037 test. Il test di `test_avvio` che non poteva fallire
adesso guarda anche l'output stampato; `test_anteprima` è passato da 14,6 s a
3,7 s; i tre test di corsa in `test_server` da 1,85 s a 0,55 s l'uno, con un
ritardo applicato una volta sola invece che dopo ogni chunk — che è anche il
modello giusto di ciò che provano.
