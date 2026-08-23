# Local Agent Harness

Harness agentico per modelli locali (Ollama / vLLM): workspace su disco, tool
di lettura-scrittura-esecuzione, memoria a lungo termine, interfaccia web.

```bash
uv sync --extra dev                 # oppure: pip install -r requirements.txt
uv run python run.py                # apre http://127.0.0.1:8123
uv run python run.py --mobile       # + interfaccia per il telefono, in LAN
```

`--extra dev` non e' un dettaglio: senza, `pytest` non entra nel `.venv` e la
suite non si puo' nemmeno lanciare. E il comando va dato **dentro l'ambiente
del progetto** (`uv run`, o il python del `.venv`): un `python -m pytest` preso
dal PATH di sistema trova un altro interprete, senza le dipendenze, e produce
una schermata di errori che sembrano bug del codice e non lo sono.

Serve un server Ollama attivo e un modello con capability `tools`:

```bash
ollama pull qwen3:8b
```

## Struttura

```
run.py                 avvio (uvicorn + apertura del browser); --mobile solleva anche il ponte
run_mobile.py          il solo ponte del telefono, se il principale gira gia' altrove
server/main.py         API HTTP e streaming SSE degli eventi dell'agente
server/runner.py       turni in background, slegati dalla connessione
server/prep.py         lavori lunghi: avvio di Docker, build dell'immagine
server/nativedialog.py selettore cartelle del sistema operativo
server/mobile.py       ponte per il telefono: proxa /api/* sul principale, chiede la chiave
web/                   frontend: index.html, style.css, app.js, logo (nessuna CDN)
web_mobile/            frontend del telefono: elenco, conversazione, risposta alle domande
core/
  config.py            default, GenParams, budget di contesto
  textutils.py         parser <think> incrementale, troncamento, stima token
  session.py           persistenza sessioni (scrittura atomica, debounced)
  memory.py            memoria a lungo termine
  tools.py             tool, sandbox del workspace, schemi di function calling
  backend.py           transport Ollama nativo + fallback OpenAI-compatible
  prompts.py           system prompt ed environment header
  agent.py             ciclo agentico a eventi, contesto, sospensione
  profiles.py          parametri consigliati per famiglia di modello
  plan.py              piano di lavoro della conversazione e sue regole
  sandbox.py           esecuzione dei comandi in container Docker
  settings.py          preferenze persistenti fra un avvio e l'altro
  vault.py             vault LLM Wiki: struttura, schema, prompt del manutentore
  vault_search.py      sotto-turno che interroga una wiki senza cambiare workspace
tests/                 644 test, nessun modello reale richiesto
```

`core/` non importa nulla del livello di presentazione: il ciclo agentico e' un
generatore di eventi guidabile da una CLI, da un test o da un frontend.

## Tool disponibili

| Tool | Uso |
|---|---|
| `list_files` | albero del workspace, filtra `.git`, `node_modules`, `.venv` |
| `read_file` | lettura, con `start_line`/`end_line` opzionali |
| `write_file` | crea o sovrascrive |
| `edit_file` | sostituzione mirata, rifiuta i match ambigui |
| `search_files` | `grep -rn` con filtro glob |
| `run_command` | shell nel workspace, guard-rail sui comandi distruttivi |
| `manage_memory` | memoria persistente fra le sessioni |
| `ask_user_question` | **sospende il turno** e aspetta una scelta dell'utente |
| `web_search` | ricerca sul web. **Compare solo** se la goccia "Ricerca online" e' accesa per quel messaggio |
| `vault_search` | interroga un vault LLM Wiki registrato senza cambiare workspace. Compare solo se ce n'e' almeno uno |

Tutti i percorsi sono confinati nel workspace da `resolve_path`, che usa
`Path.is_relative_to` (non un confronto di stringhe).

### Cartella di lavoro

Il percorso e' scritto in alto a sinistra, sotto il nome del modello, ed e'
anche il comando per cambiarlo: premendolo si apre un elenco delle **ultime
cartelle usate** (chi alterna due progetti ci torna con un clic) piu' le due
azioni che restano -- scegliere una cartella nuova e aprire questa in Esplora
risorse. Le stesse cose stanno in *Impostazioni > Connessione*, subito sotto al
modello: sono le due scelte che dicono chi lavora e su cosa. Le cartelle che
nel frattempo sono sparite escono dall'elenco da sole.

**Scegli un'altra cartella** apre il selettore del sistema operativo: su
Windows il dialogo di Esplora risorse (`IFileOpenDialog`), sul Mac il Finder,
su Linux zenity/kdialog. Il browser non puo' comunicare a una pagina web il
percorso di una cartella scelta dall'utente, quindi il dialogo lo apre il
server -- che gira sulla stessa macchina.

Su una macchina senza ambiente grafico l'endpoint risponde 501 e l'interfaccia
rimanda alle Impostazioni, dove il percorso si scrive a mano.

### ask_user_question

L'agente propone 2-4 opzioni, il turno si ferma e la domanda compare in chat
come card cliccabile (c'e' sempre anche un campo per rispondere liberamente).

Lo stato di attesa vive **dentro la cronologia della conversazione**, non in
memoria del server: si puo' ricaricare la pagina, passare a un'altra chat e
tornare, o riavviare l'harness, e la domanda e' ancora li'. Nella lista delle
conversazioni una chat in attesa e' segnalata da un pallino.

### Allegati

Graffetta nel composer, o trascina i file sopra la barra di scrittura. Finiscono
in **`allegati/`** dentro il workspace, quindi l'agente li raggiunge con i tool
che ha gia' e tu li ritrovi in Esplora risorse anche a chat chiusa. I nomi
vengono sanificati (niente separatori di percorso) e un file gia' presente non
viene mai sovrascritto: diventa `nome-1.txt`.

Prima dell'invio le gocce stanno **sopra la casella di scrittura, dentro lo
stesso contenitore**, come nelle app di messaggistica; premuto invio restano
**agganciate al messaggio** con cui sono partite, dal lato di chi scrive. Non
sono un elenco che vive in un angolo dello schermo: un file allegato e' parte
di una richiesta precisa, e in una chat lunga "quale messaggio portava quel
CSV?" e' una domanda che si risponde da sola solo se il file sta nella riga
giusta. La × che li rimuove compare a ciclo finito: il file e' montato nel
workspace, e cancellarlo mentre l'agente ci lavora trasformerebbe una pulizia
in un `read_file` fallito a meta' ragionamento (il server rifiuta con 409).

Nel contesto entra **solo l'elenco** -- nome, percorso, dimensione. Il
contenuto no: un CSV da poche centinaia di KB saturerebbe da solo la finestra
di un modello locale. Se all'agente serve un allegato, lo apre con `read_file`
e paga i token quando c'e' un motivo.

### Finestre e terminale nell'anteprima

Il pannello sa mostrare una cosa sola: un iframe su una porta pubblicata.
Quindi un'applicazione con una finestra e una shell diventano entrambe "un
server HTTP su una porta".

- `preview action='gui'` con `command`: l'harness mette attorno al programma
  uno schermo finto (Xvfb), lo fa leggere da x11vnc e lo serve come pagina con
  noVNC. Nel pannello la finestra si vede **e ci si clicca dentro** -- provato
  in Chromium con un'app tkinter. Il comando si scrive come lo si lancerebbe da
  terminale: di X e di `DISPLAY` non deve sapere niente ne' l'agente ne' tu.
- `preview action='terminal'`: una shell della sandbox nel pannello (ttyd). E'
  **tua**, non dell'agente -- lui continua a usare `run_command` -- e serve per
  guardare, provare a mano, o pilotare un programma che chiede input.

Due dettagli che non sono ovvi:

**L'iframe delle applicazioni ha `allow-same-origin`, quello dei file no.** Con
l'origine opaca il browser applica il CORS ai moduli ES e alle `fetch`, e sia
noVNC (`core/rfb.js`) sia ttyd (`/token`) vengono rifiutati da `origin: null`.
Un'applicazione sta su `127.0.0.1:82xx`, un'origine **diversa** da quella
dell'harness, quindi il permesso non le da' accesso ne' al DOM ne' alle
risposte delle API. Un'anteprima di file arriva invece da `/api/preview/file`,
cioe' dalla nostra stessa origine: li' `allow-scripts` e `allow-same-origin`
insieme annullerebbero il sandbox, e restano separati.

**La porta VNC non esce dal container** (`-localhost`): fuori ci va solo noVNC,
sulla porta che hai gia' concesso.

Servono nell'immagine: `xvfb x11vnc novnc websockify libtk8.6 libtcl8.6` e
`ttyd` (dal repository, o dal binario ufficiale se la distribuzione non lo ha).
Sono nel `Dockerfile.sandbox` di serie, che si chiude con una **verifica** --
`python -c "import tkinter"`, `ttyd --version`, la pagina di noVNC -- cosi' un
pezzo mancante fa fallire la build invece che l'agente due giorni dopo. Le
immagini costruite prima non ce l'hanno: l'etichetta `harness-sandbox-features`
lo dice, e il tool risponde "ricostruisci l'immagine" invece di un `command not
found` dentro un log.

Nota su tkinter: `python:*-slim` **compila** `_tkinter` ma poi butta via le
librerie Tcl/Tk a run time (la riga che rimarca cosa tenere esclude apposta
tkinter). Si ripara con `libtk8.6 libtcl8.6`, non con `python3-tk`, che
installerebbe tkinter per il python di Debian invece che per quello
dell'immagine.

### Adattamento al modello

L'harness legge le capability che Ollama dichiara (`thinking`, `vision`,
`tools`) e cambia comportamento di conseguenza. Tre cose che ne dipendono:

**Il system prompt.** Due versioni con gli stessi invarianti e diversa quantita'
di spiegazione. Quella estesa (1538 token) ha la tabella "frase dell'utente ->
tool da chiamare" e il ciclo di lavoro raccontato passo per passo: stampelle
che su `qwen2.5-coder` cambiano davvero il comportamento. Quella snella
(970 token) le toglie, perche' su un modello che ragiona sono due problemi
insieme — occupano contesto ad ogni richiesta, e un modello capace che legge
"cosa c'e' nel progetto -> list_files" tende a *eseguire la tabella* invece di
ragionare sul problema. La scelta e' automatica finche' non riscrivi il prompt:
appena lo tocchi, vince il tuo.

**I parametri di generazione** (`core/profiles.py`). I default erano tarati su
`qwen2.5-coder` e su un modello di pensiero sono sbagliati in modo silenzioso:
nessun errore, solo risultati peggiori.

| | prima | Qwen3.5 |
|---|---|---|
| temperature | 0.2 | 0.6 |
| top_p | 0.9 | 0.95 |
| top_k | *mai inviato* (40 di Ollama) | 20 |
| num_predict | 2048 | il massimo che la finestra consente |

`num_predict` e' il piu' importante: conta **anche i token di pensiero**. Con
2048 un blocco di ragionamento lungo esaurisce il budget prima che il modello
arrivi a scrivere la risposta o la tool call, e il turno sembra "non aver fatto
niente". Il pulsante *Applica i consigliati* scrive il profilo nelle
impostazioni; il `num_ctx` proposto viene dalla VRAM libera adesso, non da una
tabella.

**Le immagini.** Se il modello dichiara `vision`, gli allegati immagine
vengono passati nel campo `images` della richiesta e sulla loro goccia compare
un contrassegno. Se non ce l'ha, restano file come gli altri: mandare
un'immagine a un modello che non la capisce non da' errore, da' una risposta
vaga e nessuna spiegazione.

### Livelli di pensiero

Oltre ad acceso/spento, Ollama accetta `low`, `medium`, `high`, `max`. `max`
per progettare, `low` per le modifiche meccaniche, dove il ragionamento lungo
e' solo tempo di GPU. Su `auto` l'harness accende il canale solo se il modello
lo dichiara.

### Se hai poca VRAM

Con 8 GB e un modello da ~6,6 GB il contesto e' la risorsa scarsa: restano
circa 1,4 GB per la KV cache. Due variabili d'ambiente di Ollama valgono piu'
di qualsiasi impostazione dell'harness:

    OLLAMA_FLASH_ATTENTION=1
    OLLAMA_KV_CACHE_TYPE=q8_0

La seconda dimezza la memoria della KV cache con una perdita di qualita'
trascurabile, e spesso e' la differenza fra 8k e 16k di finestra utilizzabile.

Il costo fisso di ogni richiesta e' di circa 2.700 token (prompt snello +
schemi dei tool snelli + albero del workspace): con `num_ctx` a 8192 restano
~5.500 token per la conversazione vera, ed e' il motivo per cui sotto quella
soglia il tool calling degrada.

### Sandbox dei comandi

`run_command` gira dentro un container Docker che monta **solo** la cartella di
lavoro, su `/work`. Non e' un filtro sul testo del comando: e' il kernel che
non espone il resto del disco.

Serve perche' la versione precedente si affidava a `cwd` piu' un elenco di
regex "distruttive", e nessuna delle due cose e' un confine. `cd ..`,
`type C:\Users\...`, `curl | sh`: la working directory non ha mai impedito
niente. Contro un modello che vaga -- e contro un prompt injection dentro un
file che l'agente legge -- non c'e' denylist che tenga.

| Impostazione | Effetto |
|---|---|
| `sandbox: docker` | predefinito. Comandi nel container, workspace su `/work` |
| `sandbox: host` | esecuzione diretta: l'agente vede tutto il disco |
| `docker_image` | `python:3.12-slim` di serie; puntala a un'immagine tua se servono altri strumenti |
| `sandbox_network` | rete del container, accesa di serie (pip, git) |

Il container e' **uno per workspace** e resta vivo fra un comando e l'altro:
avviarne uno per comando costerebbe mezzo secondo a botta. Il primo avvio
scarica l'immagine, quindi puo' richiedere un minuto.

#### Prima volta con Docker

1. Installa **Docker Desktop** ([docs.docker.com](https://docs.docker.com/desktop/setup/install/windows-install/)). Su Windows serve WSL2 e la virtualizzazione abilitata nel BIOS; l'installazione per-utente non richiede diritti di amministratore. Gratuito per uso personale.
2. Avvialo e aspetta che l'icona diventi verde. La pastiglia in alto a destra nell'harness passa da rossa a verde.
3. Impostazioni -> Connessione -> **"Costruisci l'immagine"**.

Il passo 3 non e' facoltativo: `python:3.12-slim` contiene Python e pip e
**nient'altro**. Senza, il primo `pytest -q` dell'agente dentro la sandbox
risponde "not found" e sembra un bug dell'harness. Il pulsante crea un
`Dockerfile.sandbox` nel workspace (git, pytest, ruff, le dipendenze del
progetto), costruisce l'immagine e la seleziona. La prima build richiede
qualche minuto; le successive sono in cache.

Il `Dockerfile.sandbox` e' tuo: modificalo se ti serve altro (nodejs, un
compilatore) e ripremi il pulsante.

**Se Docker non risponde, i comandi non partono.** Non c'e' ripiego automatico
sull'host: sarebbe esattamente il buco che si sta chiudendo. La pastiglia in
alto a destra dice sempre in che stato sei, e l'errore spiega come uscirne.

I tool sui file (`read_file`, `write_file`, `edit_file`) restano confinati da
`resolve_path` come prima: quelli non passano dalla shell.

### Fermare l'agente

Mentre un turno e' in corso il tasto invia diventa uno stop. L'harness non
uccide il thread: alza una bandiera che il ciclo controlla nei tre punti in cui
fermarsi e' sicuro -- prima di chiamare il modello, ad ogni token in arrivo,
e prima di eseguire ogni tool.

Un tool gia' partito viene lasciato finire: un `write_file` interrotto a meta'
lascerebbe un file monco sul disco. Le tool call rimaste in sospeso ricevono un
risultato "interrotto dall'utente", cosi' non restano `tool_call_id` scoperti e
la conversazione resta valida per il turno successivo. In pratica lo stop e'
istantaneo, perche' quasi tutto il tempo di un turno se ne va nella
generazione, ed e' li' che il controllo scatta piu' spesso.

## Comportamento dell'agente

- **Guarda il workspace prima di parlare.** Il blocco `<environment>` del
  system prompt contiene l'albero del progetto (3 livelli, con le dimensioni)
  letto dal disco all'inizio di ogni turno: l'agente sa gia' cosa esiste senza
  spendere una chiamata a `list_files`, e senza indovinare.
- **Non sovrascrive cio' che non ha letto.** `write_file` su un file esistente
  mai letto in questa conversazione viene **rifiutato**, con l'indicazione di
  usare prima `read_file` o, meglio, `edit_file`. E' l'unico modo in cui un
  modello frettoloso puo' distruggere lavoro in un colpo solo.
- **Non lascia una verifica rossa.** Un `run_command` con exit code diverso da
  zero torna `"esito": "FALLITO"` con l'istruzione di cosa fare, e la tendina
  del tool diventa rossa. Se il turno starebbe per chiudersi con quella verifica
  ancora rotta, l'harness rimanda l'agente a lavorare (fino a due volte); se lo
  stesso comando fallisce tre volte allo stesso modo gli dice di cambiare
  approccio invece di insistere. Solo la **riesecuzione dello stesso comando**
  chiude il conto: un sotto-test verde non dimostra che la suite passi.
- **Non arrotonda.** Se il turno finisce comunque in rosso, il riepilogo e'
  obbligato a dichiararlo invece di scrivere "tutto a posto".
- **Chiude sempre con un riepilogo.** Se un turno usa dei tool e finisce senza
  scrivere nulla, l'harness sollecita il riepilogo una volta: `Fatto:` /
  `Verifica:` / `Poi:`. Nessun turno muto davanti a tendine chiuse.
- **Propone il passo successivo.** L'ultima riga del riepilogo e' sempre una
  proposta: un test da scrivere, un caso limite emerso, un problema notato di
  sfuggita.
- **Insiste da solo.** Se chiedi un'operazione sul workspace e il modello
  risponde a parole, l'harness inietta un sollecito invece di lasciare che sia
  tu a doverlo ripetere.

## Impostazioni che contano

- **Transport `ollama`** — usa `POST /api/chat`. E' l'unico endpoint dove
  `num_ctx`, `num_gpu` e `keep_alive` hanno effetto: su `/v1/chat/completions`
  Ollama li ignora senza segnalarlo.
- **`num_ctx >= 8192`** — system prompt e schemi dei tool occupano gia' ~2.400
  token. Sotto questa soglia il tool calling degrada.
- **`keep_alive`** — tenere il modello in VRAM elimina 4-15 s di ricarica ad
  ogni passo agentico. E' l'impostazione con l'effetto percepito maggiore.
- **Canale thinking nativo** — su `auto` si accende da solo se il modello
  dichiara la capability `thinking` (qwen3, deepseek-r1), e in quel caso la
  clausola `<think>` sparisce dal prompt: chiedere il ragionamento due volte
  peggiora e basta. Su un instruct normale resta spento.

## Test

```bash
uv run pytest tests -q              # 664 verdi
```

Va lanciato con l'interprete del progetto. Con un python di sistema si ottiene
`ModuleNotFoundError: No module named 'fastapi'` su una decina di file: e'
l'ambiente sbagliato, non la suite rotta.

I test dell'interfaccia mobile eseguono il JavaScript vero in QuickJS e si
saltano da soli se manca (`pip install quickjs`).

La suite gira su Windows, Linux e macOS. I due punti in cui il sistema si
sente: il finto `docker` e' un `docker.cmd` su Windows (`CreateProcess` non
legge lo shebang) e un test di semantica della shell POSIX si salta da solo
su `nt`, perche' `cmd.exe` non usa 127 per "comando inesistente".

I test girano contro un finto server Ollama locale: streaming NDJSON, tag
`<think>` spezzati fra chunk, esecuzione reale dei tool, sospensione e ripresa
su `ask_user_question`, stabilita' del prefisso del prompt (precondizione del
KV cache), regole del piano, watchdog sul ragionamento, anteprime e rotte HTTP
del backend. La sandbox si prova con un finto `docker` messo sul PATH. Nessun
modello richiesto.

## Storia delle revisioni

**v2.32.0** — dal telefono si vede cosa sta facendo il modello.

Il difetto sotto tutto: il client mobile leggeva `data.delta` sugli eventi
`content`, e quel campo non esiste — si chiama `text` ed e' cumulativo, come
sa la UI desktop. La risposta restava invisibile fino a turno finito, ed era
il motivo principale per cui dal telefono sembrava non stesse succedendo
niente.

Sopra, due livelli nuovi, e nessuno dei due mostra contenuto. La **striscia
dell'attivita'** sta sopra il composer: una riga animata che dice cosa sta
facendo e da quanto, accesa gia' al tocco di invio (fra l'invio e il primo
token passano secondi, caricamento in VRAM compreso). Le **righe dei passi**
stanno in una tendina, piu' piccole e piu' chiare del testo: un verbo e il suo
oggetto — "legge tools.py", "esegue pytest -q" — mai il pensiero, mai il
risultato; del risultato entra in pagina una cosa sola, se e' andato male.
Quando il messaggio comincia ad arrivare la tendina **si richiude** da sola e
resta un riassunto contato ("7 passi · 4 strumenti · 21s"). La cronologia si
ridisegna con le stesse righe, quindi la tendina sopravvive a un ricaricamento.

**v2.31.0** — merge del fork sviluppato con modelli open-weight. Sette funzioni
nuove e sette bug portati alla luce dalla revisione.

*Le funzioni*: **ricerca online** con goccia nel composer (schema e riga di
prompt appesi solo nei turni in cui e' accesa: spenta, il modello non sa che
esiste); **interfaccia mobile** in LAN — un ponte che proxa tutto sul processo
principale, quindi uno stato solo e due schermi sempre allineati — protetta da
una chiave stampata all'avvio; **vault Obsidian** e modalita' `wiki_manager`
sul pattern LLM Wiki di Karpathy, con `vault_search` che interroga una wiki
senza cambiare workspace; **`ignore_red`** per chiudere un punto del piano
quando la verifica rossa non riguarda il codice — con la nota obbligatoria e
il conto dei rossi archiviati; **resilienza di rete** in chat; **bus globale**
`/api/events` che tiene sincronizzate le interfacce senza polling;
**`repetition_penalty`** fra i parametri di generazione.

*I bug*, tutti silenziosi, tutti trovati leggendo il codice del fork:
`repetition_penalty` viaggiava con il nome sbagliato e Ollama lo scartava
senza dire niente (sul filo nativo l'opzione e' `repeat_penalty`);
`web_search` non era in `TOOL_NAMES`, quindi le chiamate scritte come testo
— cioe' quelle dei modelli senza tool call native — venivano buttate dal
parser; `WorkspaceError` non accettava `hint` e il ramo "nessun risultato"
della ricerca moriva di `TypeError`; `/api/answer` perdeva la goccia
riprendendo un turno sospeso da una domanda; la ricarica di fine turno usava
la POST `/open`, che ferma le anteprime, e le faceva sparire ad ogni turno
(dal telefono, ogni cinque secondi); i marchi della sandbox finivano nella
home vera anche durante i test; il ponte mobile ascoltava su `0.0.0.0` senza
autenticazione, davanti a un agente che esegue comandi.

Piu': fine-riga normalizzati a LF con `.gitattributes` (il fork era tutto
CRLF: 2.700 righe di modifiche vere ne mostravano 30.500), versione unica fra
`core/config.py` e `pyproject.toml` con un test che impedisce di separarle di
nuovo, e suite portabile su Windows.

**v2.27.1** — sei correzioni uscite da una sessione di prova lunga con
`Qwen3.8:27b` a 98k di finestra (numeri e ragionamento in
[`ANALISI_2026-08-19.md`](ANALISI_2026-08-19.md)).
*Il pensiero ha un tetto in token, non solo una quota*: la soglia del watchdog
era `max_tokens x 0,55`, che su 16k valeva 4.500 token e su 98k ne valeva
18.000 senza che nessuno l'avesse deciso — sulla sessione misurata,
243.000 token di `<think>` contro 3.300 di risposte. Ora vince il piu' stretto
fra la quota e un tetto fisso, e `think_for_step` scende di due scalini dal
terzo passo di un punto aperto.
*Il piano non si cancella piu'*: `action='set'` riscrive solo la parte aperta,
i punti chiusi restano coi loro numeri (23 chiusi lungo la sessione, 6
sopravvissuti nel file); il tetto di 12 vale sugli aperti, e nel blocco di
contesto entrano solo gli ultimi quattro chiusi piu' un conteggio.
*Il guard-rail distruttivo guarda il bersaglio e non il verbo*: dentro la
sandbox si cancella liberamente sotto `/work` (tre rifiuti di fila su un
`rm -r __pycache__` che l'utente aveva chiesto a parole), si rifiuta cio' che
punta sopra o la radice stessa; senza sandbox la severita' di prima resta.
*Una HTTP caduta non butta il turno*: timeout, connessioni cadute e 5xx valgono
due riprese automatiche, gli errori che si ripresenterebbero identici no.
*Il consumo del contesto e l'elenco delle chat si aggiornano all'invio*, non a
fine turno, e la risposta a un `ask_user_question` compare al click invece che
a ragionamento finito.
*Via il traceback dal log*: `Server-Timing` non passa piu' da
`BaseHTTPMiddleware`, che su una disconnessione del browser faceva stampare a
uvicorn `Response content shorter than Content-Length`.
*La pagina e i suoi asset si rivalidano*: `index.html`, `app.js` e `style.css`
uscivano senza `Cache-Control`, e in quel caso il browser applica la freschezza
euristica — tiene il file per circa il 10% del tempo passato dall'ultima
modifica **senza chiedere niente al server**. Si modificava `app.js`, si
riavviava `run.py`, si ricaricava, e girava ancora il codice di prima: il
sintomo era una correzione che "non funziona", con la causa fuori dal codice
che si stava guardando.

**v2.27** — l'anteprima mostra anche quello che non parla HTTP: `preview
action='gui'` mette uno schermo attorno a un programma con una finestra
(tkinter, pygame, Qt) e lo fa vedere nel pannello, dove ci si clicca dentro;
`preview action='terminal'` apre una shell della sandbox. L'iframe delle
applicazioni riceve `allow-same-origin` -- serve, e su un'origine diversa
dall'harness non concede niente -- mentre quello dei file resta com'era.

**v2.26.2** — le gocce dei file dicono *cosa*, non *dove*: via il percorso
scritto accanto all'iconcina, che era li' per distinguere due file omonimi in
`src/` e `tests/` -- un caso raro pagato ad ogni riga, che su un refactoring da
quindici file faceva dell'elenco una colonna di percorsi. L'iconcina resta la
scorciatoia per aprire la cartella; il percorso intero e' il titolo della
goccia.

**v2.26.1** — lo stato dei punti del piano si legge senza leggerlo: barra di
avanzamento a due segmenti in cima alla scheda (i saltati in ambra, che
riempiono il piano ma non sono lavoro fatto), un velo di colore sulla riga
secondo lo stato e il testo dei punti chiusi barrato in grigio chiaro. Prima
"fatto" e "da fare" erano due grigi diversi, e a colpo d'occhio due grigi sono
lo stesso grigio.

**v2.26** — ricerca nelle conversazioni, in cima all'elenco: cerca nel titolo,
in quello che e' stato detto, nei comandi lanciati e nei nomi degli allegati,
e ogni riga dice dove ha trovato la parola con l'estratto attorno. La tendina
del percorso cade sotto la fine del percorso invece che a mezzo schermo di
distanza, e le due gocce della barra tornano su due righe (affiancate, il
bordo di una toccava la freccia dell'altra) senza piu' troncarsi da sole.
L'icona della scheda e' il robottino ritagliato, senza il riquadro nero.


**v2.25.1** — tre bugie messe a posto. Il banco di prova `.analisi/` si svuota
davvero all'inizio di ogni turno e non solo davanti a una richiesta di analisi
(il prompt lo prometteva; su una richiesta di costruzione era falso, e un
modello che ci ha creduto ha lasciato i suoi scarti nella radice del
workspace). Il file di una conversazione registra il modello e la cartella con
cui e' stata fatta, che prima erano un campo vuoto e la cwd del processo. Le
impostazioni si riallineano da sole in tutti i posti che le mostrano, invece
che nei quattro punti in cui qualcuno si era ricordato di riscriverle a mano.
E la suite non gira piu' sulle preferenze vere della macchina: le leggeva
(quindi l'esito dipendeva da come avevi lasciato l'interfaccia) e le
riscriveva.

**v2.25** — la colonna di destra torna a dire una cosa sola per riquadro. Gli
allegati traslocano nel composer e si agganciano al messaggio con cui partono;
la cartella di lavoro si cambia dal percorso in alto a sinistra (con le ultime
usate) e dalle impostazioni, sotto al modello; contesto e ultima esecuzione
diventano una scheda sola; i file toccati sono le stesse gocce della chat e
aprono l'anteprima, che ora si apre da sola col file dentro invece di
accendere una scheda da premere.

**v2.16** — pulizia: via i residui della migrazione da Streamlit
(`init_state`, `gen_params_from_state`, costanti di rendering, tre funzioni di
`textutils` mai chiamate), la sonda per il tool calling e il confronto fra
modelli installati (servivano a diagnosticare qwen2.5-coder, che non emetteva
tool call native), la statistica "Risparmiati" e `EFFICIENCY.md`, che misurava
un assetto — 7B a 16k — che non esiste piu'.

**v2.15** — la schermata iniziale verifica invece di dichiarare: modello,
Docker e immagine del progetto, con il pulsante che risolve sulla riga del
problema. Docker si accende da solo all'avvio, l'immagine si costruisce quando
scegli una cartella.

**v2.14** — pannello anteprime: documenti del workspace e applicazioni che
l'agente avvia dentro la sandbox, su porte pubblicate solo su 127.0.0.1.

**v2.13** — piano di lavoro scritto dall'agente e visibile nel pannello, un
punto aperto per volta; watchdog che interrompe i ragionamenti che non portano
ad azioni; riconoscimento delle generazioni troncate da `num_predict`.

**v2.12** — budget di troncamento derivati da `num_ctx` invece che costanti
tarate su 16k; VRAM letta dalla macchina che esegue davvero il modello, anche
quando Ollama gira su un'altra macchina della rete.

**v2.10** — il nome del modello e la finestra di contesto in alto si
aggiornano appena li cambi, non dopo il primo turno. Logo nuovo in due
versioni: nera (default, anche nella scheda del browser) e beige/arancione per
il tema chiaro, con un bordo che lo stacca dallo sfondo in entrambi i temi.

**v2.9.1** — due bug trovati leggendo una sessione vera: `cd /work && pytest`
falliva con exit 127 perche' il comando finiva a `timeout` come argomenti nudi
invece che dentro una shell, e un comando inesistente restava "verifica rossa"
per sempre facendo scattare solleciti a lavoro finito.

**v2.9** — dalle prove con qwen3.5: con un test rosso i file di test non si
modificano piu' (il modello li riscriveva in tautologie per arrivare al verde),
e `ask_user_question` torna descritto per esteso, perche' accorciarlo lo aveva
di fatto tolto dal repertorio. Costo fisso da 2.214 a 2.703 token: speso
apposta.

**v2.8** — chiuso il piano di efficienza (`EFFICIENCY.md`): schemi dei tool
snelli per i modelli che ragionano, cache delle riletture, preload del modello
all'avvio. Il costo fisso di ogni richiesta scende da 3.757 a 2.214 token
(-41%): su una finestra da 8k sono +35% di spazio per la conversazione.

**v2.7** — l'harness si adatta al modello invece di trattarli tutti come
`qwen2.5-coder`. Profili di generazione per famiglia, `top_k` e
`presence_penalty` finalmente inviati, livelli di pensiero
(`low/medium/high/max`), system prompt snello per i modelli che ragionano
(612 token invece di 1538) e immagini allegate passate ai modelli con la
vision.

**v2.6** — i comandi girano in un container Docker che monta **solo** la
cartella di lavoro. Prima `run_command` usava `shell=True` con la sola working
directory come confine, e una working directory non e' un recinto: `cd ..` e
l'agente era fuori. Se Docker non risponde i comandi non partono, non
ripiegano sull'host. Aggiunto anche l'header `Server-Timing` su tutte le rotte
`/api/`, per capire dal browser se una lentezza e' del server o del rendering.

**v2.5** — tasto stop accanto a invia: il turno si ferma al primo punto
sicuro, senza troncare un tool a meta' ne' lasciare tool_call scoperte in
cronologia. Allegati alla conversazione: i file finiscono in `allegati/` nel
workspace, compaiono nel pannello di destra e l'agente ne vede l'elenco nel
contesto, leggendoli con `read_file` solo se gli servono.

**v2.4.2** — aprire una conversazione non parla piu' con Ollama. Il backend,
le capability del modello e l'albero del workspace sono in cache; l'indice
delle chat si rilegge solo se il file e' cambiato; la sidebar non fa piu' una
seconda GET dopo ogni click. Da 2 round-trip per click a zero: con Ollama
occupato si passa da ~2s a ~3ms. L'avvio fa 1 richiesta invece di 6.

**v2.4.1** — il campo "API key" non e' piu' un `input type="password"`: Chrome
lo scambiava per un login, ci autocompilava password salvate e mostrava
l'avviso "credenziale compromessa". Ora e' un campo di testo mascherato via
CSS, e il default non e' piu' la stringa `ollama` ma vuoto.

**v2.4** — ciclo di self-correction presidiato dall'harness: verifiche rosse
tracciate, riparazione sollecitata, riepilogo onesto sul rosso residuo.

**v2.3** — l'agente parte sempre da cosa c'e' nel workspace: albero a 3
livelli nel system prompt e rifiuto delle sovrascritture alla cieca. Canale
thinking rilevato dal modello.

**v2.2** — il turno gira in background legato alla conversazione: cambiare
chat non lo interrompe e tornando indietro si rivedono pensiero e tool gia'
eseguiti, con pallino pulsante nella sidebar. Blocchi del turno in ordine
cronologico. Selettore cartella nativo del sistema operativo. Scartati i
`<tool_response>` inventati dal modello.

**v2.1** — interfaccia FastAPI + SSE al posto di Streamlit; tool
`ask_user_question` con sospensione del turno; riepilogo finale obbligatorio e
tono propositivo; recupero delle tool call stampate come testo (con codice e
graffe nel `content`); lista conversazioni piatta con stato di attesa.

**v2.0** — refactoring da monolite a moduli; correzione dello streaming del
pensiero; transport Ollama nativo (`options` finalmente applicate);
compattazione del contesto e troncamento testa+coda dei log.
