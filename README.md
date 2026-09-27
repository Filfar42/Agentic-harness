# Local Agent Harness

Harness agentico per modelli locali (Ollama / vLLM): workspace su disco, tool
di lettura-scrittura-esecuzione, memoria a lungo termine, interfaccia web.

Audit e refactoring del 6 settembre 2026: relazione in quattro fasi in
`docs/audit/AUDIT.md`, verifiche in `docs/audit/VALIDAZIONE.md`, prompt completo
in `core/system_prompt.py`. Questa revisione rende rigorosi JSON e schemi dei
tool, limita i retry e il buffering, protegge le API locali e rende visibili
i fallimenti di persistenza. Il deployment previsto resta un singolo processo
per archivio e utente fidato; i rischi residui sono elencati nella relazione.
Per riprodurre l'ambiente verificato usa `uv sync --locked --extra dev`.

La revisione **2.37.0** interviene sul loop e sulla conservazione del contesto:
checkpoint incrementali con stato aperto, recupero nel corpo degli archivi,
risultati JSON integri, controllo del ragionamento per punto del piano e
telemetria di tutte le chiamate. Dettagli e limiti delle verifiche in
[Implementazione loop e contesto](docs/research/IMPLEMENTAZIONE_LOOP_CONTESTO_2026-09-14.md).

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
  context.py           riduzione strutturata dei risultati e riferimenti recuperabili
  inference.py         chiamate ausiliarie cancellabili, senza pubblicare output parziali
  telemetry.py         tempi e token di ogni chiamata, separati per scopo
  profiles.py          parametri consigliati per famiglia di modello
  plan.py              piano di lavoro della conversazione e sue regole
  sandbox.py           esecuzione dei comandi in container Docker
  settings.py          preferenze persistenti fra un avvio e l'altro
  vault.py             vault LLM Wiki: struttura, schema, prompt del manutentore
  vault_search.py      sotto-turno che interroga una wiki senza cambiare workspace
tests/                 suite di regressione, nessun modello reale richiesto
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

### Una pagina e' una cartella, non un file

Fino alla v2.34 l'anteprima di una pagina era il **file** e basta, servito da
`/api/preview/file?path=...`. Un `<link href="style.css">` dentro quella pagina
si risolve pero' in `/api/preview/style.css`, che non esiste: ogni sito scritto
dall'agente si vedeva senza foglio di stile e senza script. Non "un po'
diverso" -- un'altra cosa, e senza nessun errore da nessuna parte.

Da qui in avanti si serve la **cartella**, su un **secondo server** minuscolo
(`server/previewhost.py`) che sta su una porta sua (8124 di serie, spostabile,
0 = spento). Le due cose vanno insieme e non e' un caso: sulla porta
dell'harness la cartella non si poteva servire senza dare alla pagina scritta
dal modello la nostra stessa origine, cioe' le nostre API. Su un'origine
diversa il permesso `allow-same-origin` e' innocuo, e la pagina ottiene quello
che le serve per essere viva: `localStorage`, moduli ES, `fetch`.

- **Quale cartella.** Dal file si sale finche' si incontra un confine di
  progetto (`package.json`, `pyproject.toml`, `.git`, `requirements.txt`); il
  piu' vicino vince, cosi' in un monorepo la radice e' il sito e non il
  repository. Senza confini, la cartella piu' alta che contiene un
  `index.html`; senza neanche quello, la cartella del file. Mai il workspace
  intero se non e' li' che sta la pagina: quello che non serve non si espone.
- **Il backend, se ce n'e' uno.** Prima di mostrare, l'harness guarda la
  cartella: `app.py`/`main.py` che dichiarano Flask o FastAPI, `manage.py` di
  Django, `package.json` con Vite o con uno script `dev`/`start`. Se ne
  riconosce uno lo avvia nella sandbox e mostra **quello**, all'indirizzo suo:
  un backend decide da se' le proprie URL, il percorso del file sul disco non
  e' l'indirizzo della pagina. Se non risponde, si ripiega sulla pagina statica
  e il log finisce nel risultato del tool -- di solito mancano le dipendenze, e
  il modello puo' installarle e riprovare. Si spegne da Impostazioni.
- **Si ricarica da sola.** Ogni `write_file`/`edit_file` dentro la cartella
  mostrata ricarica il pannello, con una pausa che assorbe le raffiche. Il
  segnale e' lo stesso da cui nascono le gocce dei file: nessun evento nuovo,
  nessun watcher sul disco. Terminale e schermo sono esclusi -- sono
  dell'utente, e ricaricarli gli butterebbe via quello che ci sta facendo.

Al modello questo si dice nel blocco `<anteprima>`: che il pannello e' gia'
aperto **e** che si ricarica da solo. Senza, richiama `preview` dopo ogni
correzione -- un passo intero per non fare niente.

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

**L'iframe ha `allow-same-origin` solo dove l'origine e' un'altra.** Con
l'origine opaca il browser applica il CORS ai moduli ES e alle `fetch`, e sia
noVNC (`core/rfb.js`) sia ttyd (`/token`) vengono rifiutati da `origin: null`;
in piu' `localStorage` non torna vuoto, **solleva**. Un'applicazione sta su
`127.0.0.1:82xx` e una pagina sul server delle anteprime (vedi sotto): sono
origini **diverse** da quella dell'harness, quindi il permesso non da' accesso
ne' al DOM ne' alle risposte delle API. Cio' che arriva da `/api/preview/file`
-- markdown, immagini, codice -- viene invece dalla nostra stessa origine, e li'
`allow-scripts` e `allow-same-origin` insieme annullerebbero il sandbox: non
vanno mai nella stessa lista.

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

Il menu si apre dall'ingranaggio in fondo alla barra laterale o con
**Ctrl+,** (⌘, su macOS). Ha una ricerca (`/`), segna le voci cambiate
rispetto ai valori di serie e le rimette com'erano, e mostra le voci che
valgono per un solo server solo quando si usa quel server. Le poche chiavi che
non ha (`stream_tools`, `auto_env_header`, ...) si cambiano a mano nel file
delle impostazioni, con l'harness spento: il percorso è in Informazioni.

- **Transport `ollama`** — usa `POST /api/chat`. E' l'unico endpoint dove
  `num_ctx`, `num_gpu` e `keep_alive` hanno effetto: su `/v1/chat/completions`
  Ollama li ignora senza segnalarlo.
- **`num_ctx`** — system prompt e schemi dei tool occupano gia' 5-7 mila token
  (snello 1.604 + 3.702, esteso 1.863 + 4.943): sotto 16k resta poco per il
  lavoro. Con llama.cpp vale il minore fra questo valore e il `-c` del server.
- **`keep_alive`** — tenere il modello in VRAM elimina 4-15 s di ricarica ad
  ogni passo agentico. E' l'impostazione con l'effetto percepito maggiore.
- **Canale thinking nativo** — su `auto` si accende da solo se il modello
  dichiara la capability `thinking` (qwen3, deepseek-r1), e in quel caso la
  clausola `<think>` sparisce dal prompt: chiedere il ragionamento due volte
  peggiora e basta. Su un instruct normale resta spento.

## Test

```bash
uv run pytest tests -q              # 915 verdi
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

**I vault diventano Progetti** (26-27/09/2026, ramo `progetti-2026-09-26`
sopra `barra-destra-2026-09-26`, versione non ancora numerata) — il progetto
e' il pezzo principale, la wiki una sua opzione.

Un progetto e' una cartella di lavoro, le chat che ci lavorano dentro e una
memoria sua. La memoria c'era gia', ma **nessuno la scriveva**: su 81 sessioni
e 2.890 chiamate a tool, zero `manage_notes` con `ambito='vault'`. Ogni chat
ripartiva da capo. Ora la scrive l'harness:

- **Quando.** A fine turno, in un progetto, se il turno ha lavorato (ha scritto
  file, lanciato comandi o chiuso punti del piano). Un turno di sole domande non
  la tocca.
- **Come.** Un passo in piu' in coda alla stessa conversazione: stesso system
  prompt, stesse intestazioni, stessi schemi dei tool, quindi la cache del
  prefisso si riusa. Niente pensiero, temperatura 0,1, al piu' 700 token. Con
  due slot llama.cpp va nello slot della conversazione. Il modello risponde con
  operazioni JSON (`aggiungi`, `modifica`, `togli`, al piu' 6 per giro).
- **Cosa.** Voci con un tipo (decisione, convenzione, fatto, scartato,
  aperto), la chat da cui vengono e chi le ha scritte. Quelle scritte o
  corrette a mano sono dell'utente e l'harness non le tocca.
- **Dove si vede.** Nella chat, una riga "Memoria del progetto aggiornata +3"
  apribile; nella colonna di destra, la scheda della memoria. Nel prompt la
  memoria sta nel blocco di coda, prima delle note della chat e del piano:
  una voce nuova non invalida il prefisso.
- Si spegne da **Impostazioni › Contesto e memoria** (`memoria_progetto`).

**La schermata del progetto** e' un cruscotto: testata e composer (una chat
nuova parte gia' nel progetto), poi le schede **Riprendi da qui** (l'ultima
chat: piano, checkpoint, ultima risposta), **Memoria** (filtri per tipo,
provenienza, aggiunta e correzione in riga), **Conversazioni**, **Istruzioni**
e **Libreria** (`.memoria/`, con lettore). Sotto gli 860 px diventa una
colonna sola. La creazione e' guidata (cartella esistente o nuova, nome,
istruzioni, wiki facoltativa) e le impostazioni del progetto hanno una
finestra loro.

**Chat e progetti si gestiscono**: rinomina in riga, archivio (le archiviate
stanno in fondo all'elenco, e si riaprono), spostamento dentro e fuori da un
progetto, con conferma, e rimozione del progetto dall'elenco senza toccare la
cartella. Rinominare e archiviare non cambiano `updated_at`: l'ordine
dell'elenco resta quello del lavoro.

**Telefono**: le chat raggruppate per progetto, il nome del progetto sulla
testata della chat e la memoria in sola lettura.

Rinomina ovunque, con migrazione: `core/progetto.py`, rotte `/api/progetti*`,
file `.progetto.json` (il `.vault.json` si converte al primo accesso, e le
note vecchie tengono un id stabile), impostazione `progetti` (`vaults` si
rinomina alla lettura), `manage_notes ambito='progetto'`, `wiki_search`
offerto solo se il progetto ha la wiki. Prove: `test_progetto.py`,
`test_memoria_progetto.py`, `test_progetti_web.py`; 1850 passano.

**Colonna di destra: il cruscotto** (26/09/2026, ramo `barra-destra-2026-09-26`
sopra `impostazioni-2026-09-26`, versione non ancora numerata) — rifatta da
capo.

La colonna aveva cinque righe di numeri ("Inviati al modello", "Prompt",
"Generati", "Velocita'", "Totale") che si aggiornavano all'invio e a fine
turno, cioe' quando non servivano piu', e la velocita' era una media su tutto
il turno. Ora ci sono quattro schede dal vivo, poi piano, note e file toccati.

- **Velocita'.** Token al secondo **adesso** (ultimi 1,5 s di generazione),
  aggiornati quattro volte al secondo, con la fase (attesa, prefill, pensa,
  risponde, scrive la chiamata, esegue un tool), la traccia del turno colorata
  per fase, il tempo al primo token, la velocita' del prefill e i token
  generati.
- **Turno.** Una riga per passo: attesa, pensiero, risposta, argomenti delle
  chiamate ed esecuzione dei tool, in scala fra i passi; sopra, dove e' andato
  il tempo di tutto il turno in percentuale. I passi dopo una compattazione
  hanno un segno. Al passaggio del puntatore: tempi, tok/s, prompt, tool.
- **Contesto.** Il prompt del passo in corso sulla finestra vera, con la parte
  ripresa dalla cache (piena) e quella ricalcolata (tratteggiata); sotto, il
  draft MTP: quota di token accettati dal vivo e una barretta per passo.
- **Velocita' e contesto.** Tok/s contro token di prompt, passo per passo e
  turno per turno (grigi i turni vecchi, colorati l'ultimo, pulsa il passo in
  corso), con la tendenza scritta: "−2,2 tok/s ogni 10k token di prompt".
- **Telefono.** Una goccia con i tok/s in alto (toccandola: contesto, cache,
  MTP), una riga sottile per il contesto sotto la barra, e la rotaia del piano
  sul bordo destro: un pallino per punto, l'elenco toccandola.

Da dove vengono i numeri (`core/cruscotto.py`):

- `Tachimetro` guarda lo stream di un passo e produce l'evento `metriche`
  (SSE), al piu' quattro al secondo e uno **definitivo** a fine passo. Con
  llama.cpp usa i `timings` che il server manda a ogni token
  (`timings_per_token`, gia' acceso) e il progresso del prefill
  (`return_progress`, nuovo: un server che non lo conosce lo ignora); con
  Ollama conta i pezzi dello stream (un token l'uno) e a fine passo prende
  `eval_count`/`eval_duration`. `fonte` dice quale dei due.
- I transport emettono `StreamEvent("battito")`: timings, progresso del
  prefill e caratteri degli **argomenti delle tool call** mentre arrivano (un
  `write_file` lungo prima era silenzio). Un battito non conta come output: dopo
  un battito il transport puo' ancora riprovare.
- Le metriche dal vivo sono frame **volatili** del runner: dell'arretrato si
  tiene solo l'ultima, al posto in cui era stata emessa. Senza, un turno da
  un'ora avrebbe aggiunto quindicimila frame e fatto scattare il limite degli
  eventi. Le definitive restano tutte.
- `Registro` fa le righe della timeline dagli stessi eventi; il server le
  salva con la telemetria del turno, e `stats.cruscotto` (`storico`) le
  restituisce a chi riapre la chat, insieme ai punti velocita'/contesto. Per i
  turni registrati prima, le righe si ricostruiscono dalle chiamate della
  telemetria (senza la ripartizione pensiero/risposta).
- I file toccati si aggiornano a ogni `write_file`/`edit_file`, non a fine turno.

Altro:

- L'impronta anti-cache degli asset copre anche `impostazioni.*` e
  `cruscotto.*`.
- `scripts/finto_llama_server.py`: un llama-server finto (timings, prefill,
  draft MTP, velocita' che cala col contesto) per vedere il cruscotto senza GPU.

**Menu delle impostazioni** (26/09/2026, ramo `impostazioni-2026-09-26`,
versione non ancora numerata) — rifatto da capo.

Erano cinque schede scritte a mano; una ("Efficienza") raccoglieva 25 voci che
non c'entravano fra loro, e "Connessione" conteneva mezza sandbox. Ora e' una
finestra con una barra laterale e dieci sezioni (Modello e server,
Generazione, Comportamento, Contesto e memoria, Istruzioni, Memorie, Sandbox,
Anteprime, Aspetto, Informazioni), costruita da uno schema in
`web/impostazioni.js`: una voce per chiave di `DEFAULTS`, con etichetta, una
riga su cosa cambia per chi usa l'harness, tipo di controllo e condizioni.
`test_ogni_impostazione_ha_un_posto` pretende che ogni chiave stia nello schema
o in `IMP_FUORI_MENU` con il motivo.

- **Voci per server.** `keep_alive` e `num_gpu` si vedono solo con Ollama, lo
  slot di servizio solo con llama.cpp, `top_k` e le penalita' non con un server
  OpenAI-compatibile (che non le riceve). Cercando si vedono lo stesso,
  sbiadite, con il perche'.
- **Essenziali e Avanzate.** Ogni sezione mostra le voci che si cambiano; il
  resto sta in "Avanzate", chiuso. Fuori dal menu: `stream_tools` (serve solo
  con Ollama < 0.8), `auto_env_header` (spento peggiora sempre l'agente),
  `expand_thoughts` (nessuno lo legge).
- **Ricerca, pallino, ripristino.** La ricerca guarda l'inizio delle parole, non
  i pezzi ("porta" non trova piu' "comportamento"), ignora gli accenti e la
  vocale finale (porta/porte). Le voci diverse dal valore di serie hanno un
  pallino e il ripristino, anche per sezione intera, con l'elenco di cosa
  cambia prima di confermare. I valori di serie li da' `GET /api/settings/meta`.
- **Salvataggio immediato, ma non a ogni tasto.** I campi di testo e numerici
  salvano quando si smette di scrivere (`bindField(..., { ritardo })`) e subito
  uscendo dal campo: prima ogni cifra dell'indirizzo del server ricostruiva il
  backend. Un numero fuori dai limiti non si salva e, uscendo, si porta al
  limite. Un salvataggio rifiutato dal server non resta piu' nello stato.
  Spia "Salvato" in alto.
- **Stati dentro le sezioni.** Server del modello (raggiungibile o no, con il
  messaggio del server e "Verifica") e sandbox (Docker, immagine del progetto)
  in testa alle loro sezioni, con un segno nella barra quando c'e' un problema.
  "Apri le impostazioni" dalla schermata di prontezza va alla sezione giusta.
- **Profilo consigliato con le differenze.** Dice cosa cambierebbe prima di
  applicarlo. Il suggerimento sulla VRAM remota compare solo con Ollama: con
  OpenRouter parlava di "Ollama su openrouter.ai".
- **Istruzioni: di serie o tuo.** Il campo del prompt conteneva il prompt
  esteso anche per chi riceveva quello snello. `GET /api/settings/prompt` dice
  quale prompt e' in uso e restituisce quello effettivo, memorie comprese;
  "Personalizzato" parte dal testo di serie in uso, e tornare indietro chiede
  conferma.
- **Esporta / importa.** `GET /api/settings/export` scrive un JSON
  (`astra-impostazioni`) senza chiave API, chiave del telefono, cartelle,
  vault e immagine Docker; `POST /api/settings/import` accetta quel file o un
  `agent_settings.json`, con `prova: true` mostra le differenze prima di
  applicare, e scarta con il motivo le chiavi sconosciute o di tipo sbagliato.
  Menu e importazione passano da `_applica_impostazioni`, la stessa porta.
- **Informazioni e diagnostica.** Versione, Python, sistema, dove stanno
  impostazioni, conversazioni e memorie, e "Copia diagnostica" (le impostazioni
  diverse dal default, con la chiave API coperta). `GET /api/diagnostics` non
  interroga la rete.
- **Tastiera.** Ctrl+, apre e chiude, `/` cerca, frecce fra le sezioni, Invio
  dalla ricerca porta alla prima voce, Esc svuota la ricerca e poi chiude senza
  chiudere anche l'anteprima sotto; il fuoco resta nella finestra.
- **Corretto:** "Passi per turno" arrivava a 40 mentre il valore in uso era 100,
  quindi il campo nasceva non valido.

**v2.38.1** — i template severi: il GGUF ufficiale di Qwen3.8 non rifiuta piu'
ogni messaggio.

Con `ISTA-DASLab/Qwen3.8-27B-GSQ-RCO-IQ3_S-mtp` su llama-server ogni turno
moriva al primo passo con due eccezioni del chat template: `Unexpected
reasoning effort high. Supported types are xhigh (default), medium, and low.`
e `System message must be at the beginning.` Il template unsloth usato prima
non controllava ne' l'una ne' l'altra cosa.

**Il livello di pensiero si traduce su quelli che il template ammette.** I
valori non stanno scritti nel codice: si leggono dal `chat_template` di
`/props` (la frase dell'eccezione o la tupla del controllo) e, se il template
non li lascia leggere, dal messaggio del 500, che si impara una volta e si
riprova una volta sola (cache per endpoint e modello, svuotata da
`forget_model_info`). Scala `low < medium < high < xhigh` (`max` sopra
tutti); a parita' di distanza vince il piu' alto, quindi `high` -> `xhigh`: il
pensiero di default non si abbassa. Vale anche su Ollama e sul generico
compatibile (vLLM, OpenRouter): se il livello e' gia' ammesso il payload e'
identico a prima. La traccia `think` del messaggio ha il campo `inviato` e,
quando differisce da `usato`, `traduzione_livello` (`high -> xhigh`).

**Un solo system, in testa, al confine di serializzazione.** Gli unici system
dopo il primo erano in testa: l'environment (sempre) e la nota di
`trim_to_window` (quando taglia); i solleciti erano gia' messaggi `user`.
`messaggi_per_il_filo` fonde i system contigui in testa nel primo -- quello che
`to_ollama_messages` faceva gia' per Ollama -- e trasforma un eventuale system
piu' avanti in un `user` "[Nota dell'harness] ...", spostato dopo l'ultimo
`tool` se cadrebbe dentro un gruppo `tool_calls`. Tutti i transport. La
cronologia salvata non cambia.

**L'errore resta in chat.** Il turno con il solo riquadro d'errore contava
come vuoto e veniva rimosso a fine stream, e la rilettura dal disco di fine
turno non lo ritrovava. Ora l'errore che chiude il turno si salva come record
`error` (mai inviato al modello) e si ridisegna, anche sul telefono. Il
messaggio mostrato e' quello del server senza l'involucro JSON, e un 500 del
template non si riprova tre volte col backoff: e' deterministico.

**v2.37.0** — checkpoint, recupero e misure del loop.

La compattazione elabora i nuovi eventi e riporta esplicitamente attivita'
aperte e vincoli. Le richieste e i chiarimenti umani restano testuali. I
risultati dei tool conservano JSON valido, esiti, hash e percorsi del deposito;
una scrittura fallita mantiene gli argomenti originali. La libreria cerca
anche dentro gli archivi e restituisce frammenti pertinenti entro un budget.

Il ragionamento si ripristina a un nuovo punto del piano o dopo un errore.
Il controllo della finestra conta anche gli schemi dei tool; compattazione,
estrazione e riepilogo rispettano lo stop e scartano le risposte incomplete.
`think=false` e `repeat_penalty=1.0` vengono trasmessi esplicitamente quando
supportati, con diagnostica che distingue richiesta e supporto conosciuto.

Ogni turno conserva tempi, token riportati e stime per le chiamate principali
e ausiliarie. I dettagli stanno in `<id>.telemetry.json`, aggiornato quando
cambia lo snapshot, entro 50 turni e 2 MiB; il salvataggio dei tool non
riscrive le metriche. La lettura resta compatibile con sessioni precedenti.
Le prove sono deterministiche: non quantificano guadagni di qualita' o
velocita' su un modello reale.

**v2.36.1** — le ultime due attese, tolte alla radice.

**La pagina non aspetta piu' la rete.** `/api/bootstrap` faceva tre viaggi
verso il server del modello (`status`, `version`, `streams_tool_calls`) *prima*
di rispondere: con il modello su un'altra macchina spenta erano fino a una
decina di secondi di **pagina bianca** — non un'interfaccia lenta, proprio
nessuna interfaccia, mentre impostazioni, conversazione e memorie erano gia' su
questo disco. Adesso il bootstrap risponde con `online: null`, la goccia dice
"controllo…", e la sonda va per conto suo su `/api/backend` a pagina gia' viva.

**I messaggi si scrivono in coda.** Una conversazione ora sono **due file**:
`<id>.json` con i metadati (titolo, piano, note, allegati, e i due numeri che
servono alla sidebar) e `<id>.jsonl` con i messaggi, uno per riga, scritti in
append. Prima la cronologia stava dentro il JSON dei metadati e salvare voleva
dire riserializzare e riscrivere tutto: su una chat da 2,5 MB sono **23 ms** di
GIL — cioe' 23 ms in cui il server non risponde a nessuno — pagati ad **ogni
tool finito**, ed erano proprio le conversazioni lunghe a pagarli piu' spesso.
Adesso un messaggio nuovo costa la sua riga: **0,5 ms**.

Il pericolo dell'append e' che i messaggi vengono anche modificati sul posto —
la traccia del pensiero si attacca all'assistente del passo precedente, la
cancellazione di un allegato ripulisce i messaggi che lo nominavano, la
compattazione sostituisce un tratto di cronologia. Due difese, e insieme
coprono tutto: **l'impronta** dell'ultima riga scritta (che vede accorciamenti,
compattazioni e modifiche all'ultimo messaggio, e allora riscrive) e **la
riscrittura di fine turno**, l'unico momento in cui la cronologia e' ferma e
completa — una volta per turno invece di una per tool.

Tre effetti collaterali graditi: la sidebar non apre piu' la cronologia di
nessuno (`n_messages` sta nei metadati), non c'e' nessuna migrazione da
lanciare (la coda nasce al primo salvataggio, e una conversazione mai piu'
aperta resta com'e'), e un processo che muore durante la scrittura ora costa
**l'ultimo messaggio** invece di tutta la chat — con un file unico restava un
JSON illeggibile.

*Nota per gli script di analisi in `claude output/`: leggono
`chat_sessions/*.json` aspettandosi la chiave `messages`, che per le
conversazioni convertite non c'e' piu'. I messaggi vanno letti dal `.jsonl`
accanto.*

**v2.36.0** — l'interfaccia smette di essere lenta. Quattro cause, misurate.

**1. Il pensiero viaggiava al quadrato.** Ad ogni token generato si spediva
*tutto* il testo accumulato fino a li'. Un passo da 20.000 token di
ragionamento sono 20.000 eventi, il primo da pochi byte e l'ultimo da 80 kB:
**~1,8 GB** di JSON per un solo passo, serializzati, spediti, tenuti in RAM nel
buffer del turno (quello che serve a chi si riattacca) e ridisegnati dal
browser una volta per token. Era questo a far comparire il popup di Chrome "la
pagina non risponde". Ora l'evento porta `append` — i soli caratteri nuovi — e
non piu' di dieci volte al secondo: **0,2 MB in 800 eventi**, quattro ordini di
grandezza. Il testo completo continua ad arrivare una volta per passo, ed e'
voluto: un abbonato lento puo' vedersi scartare dei frame, e quello lo rimette
in pari.

**2. Il modello irraggiungibile si pagava ad ogni richiesta.** `session_stats`
chiede le capability del modello, e i fallimenti non venivano memorizzati ("al
prossimo giro riprova"). Con il modello su un'altra macchina spenta, ogni
lettura di sessione pagava i **sei secondi** di timeout per intero: aprire una
chat, finire un turno, riallinearsi. Ora un fallimento si ricorda per trenta
secondi — e la sonda a mano lo dimentica, perche' chi la lancia ha appena
acceso qualcosa.

**3. Il conto del contesto si rifaceva sempre.** Ricostruire i messaggi per
l'API e stimarli costa quanto tutta la cronologia, e `session_stats` viene
chiamata ad ogni apertura, ad ogni fine turno, ad ogni invio, ad ogni
riallineamento. Ora si ricalcola solo quando cambia uno dei suoi ingredienti.
Le due cose insieme: `GET /api/sessions/<chat da 2,5 MB>` da **155 ms a 7,6
ms**, senza contare i sei secondi del punto 2.

**4. Meno lavoro per il browser.** La coda della cronologia passa da 40
messaggi a **15** (in una chat agentica sono in maggioranza risultati di tool,
e ogni risultato e' una tendina). E le tendine dei tool costruiscono argomenti
e risultato **al primo clic**, non da chiuse: un `write_file` da 34 kB non
entra piu' nel DOM per non essere guardato.

Nella stessa revisione: **l'anteprima non si spalanca piu' da sola entrando in
una chat.** L'anteprima memorizzata e' il ricordo di cosa l'agente mostrava
l'ultima volta; riaprirla ad ogni ingresso significava mezzo schermo occupato
da una pagina di ieri, da chiudere a mano ogni volta. Ora entrare in una
conversazione la lascia chiusa — la riapre solo un turno **vivo**, o l'agente
che mostra qualcosa adesso. Rileggere una chat in cui si e' gia' dentro (fine
turno, riallineamento) invece non la tocca: chiuderla li' vorrebbe dire far
sparire da sola la pagina appena costruita.

E fuori dalla richiesta: la spazzata dei container rimasti da un riavvio
precedente ora gira in un thread. Serviva quasi sempre a scoprire che era gia'
tutto pulito, e su Windows ogni `docker` e' mezzo secondo buono pagato dentro
la prima apertura di chat.

**v2.35.0** — l'anteprima di una pagina e' la pagina, non il suo file.

Tre difetti sotto una frase sola dell'utente ("un html senza il css e senza il
backend avviato non funziona come dovrebbe"). Il primo: si serviva **un file**,
quindi i percorsi relativi del sito puntavano dentro `/api/preview/`, e lo
stile non arrivava. Il secondo: l'iframe dei file aveva l'origine opaca --
scelta giusta finche' il file arrivava dalla nostra porta -- e con quella
`localStorage` solleva, i moduli ES vengono rifiutati e ogni `fetch` e'
cross-origin. Il terzo: una pagina che parla con un backend spento non e'
un'anteprima, e' un'anteprima che mente.

La cura e' una sola cosa fatta in tre punti: un secondo server sulla sua porta
che monta la cartella del progetto su `/` (quindi origine diversa, quindi
`allow-same-origin` senza aprire le API dell'harness), il riconoscimento del
backend con avvio automatico e ripiego sullo statico se non risponde, e la
ricarica viva ad ogni salvataggio dentro quella cartella.

Nella stessa revisione: **entrando in una chat si atterra sull'ultimo
messaggio**. Ci si ritrovava al primo, ed erano due cose che da sole non si
vedevano. `#scroller` ha `scroll-behavior: smooth`, quindi `scrollTop =
scrollHeight` non salta ma **anima**; per i primi fotogrammi il thread e'
ancora in cima; e in cima il gestore dello scroll chiede il blocco di messaggi
precedenti, che ridisegna tutto e ripristina la posizione di *quel* momento --
l'inizio, con in piu' una pagina di cronologia che nessuno aveva chiesto. Ora
l'ancoraggio salta (`behavior: 'instant'`, piu' un secondo colpo al fotogramma
dopo per il contenuto che si assesta) e la cronologia si chiede solo quando il
movimento e' davvero **verso l'alto**.

E ancora: **una sospensione del computer non interrompe piu' niente.** Il turno
vive gia' in un thread suo, con un buffer di eventi completo — riattaccarsi non
perde nulla, ed e' per questo che chiudere e riaprire la conversazione la
rimetteva in pari. Il client pero' non ci provava: a stream spezzato scriveva
una casella rossa e restava sordo fino alla fine del turno. Ora `attachStream`
e' un **ciclo di connessioni**: una caduta e' un riattacco (0,5s, 1, 2, 4, 8,
poi ogni 10s, e subito al risveglio del computer o al ritorno della rete), e ad
ogni ricollegamento l'arretrato ridisegna il turno da capo — il nodo vecchio se
ne va, senno' pensiero e tool si sdoppierebbero. Al posto della casella rossa
c'e' la riga di stato che dice cosa sta succedendo.

La seconda meta' e' che il bus globale **non ha arretrato**: quello che passa
mentre la connessione e' giu' non lo ripete nessuno, e un turno finito durante
una sospensione non lascia altra traccia che i messaggi salvati. La cura e' una
funzione sola, `riallinea()`, che rilegge la conversazione dal disco (GET, mai
la POST `/open`, che e' un gesto e ferma le anteprime) e ha tre chiamanti: la
fine di uno stream interrotto, la riapertura del bus, e le sveglie
(`online`, `visibilitychange`, `pageshow`). Le sveglie non fanno niente finche'
il bus e' aperto: rileggere ad ogni ritorno sulla scheda ridisegnerebbe il
thread e butterebbe in fondo chi stava leggendo indietro.

**v2.34.1** — un Ctrl+C riuscito non stampa piu' un traceback.

Coda della v2.33.0: per agganciare `handle_exit` il `Server` viene costruito a
mano, e cosi' si perdono due cose che `uvicorn.run()` faceva da solo. La prima
si vedeva: a spegnimento avvenuto uvicorn **ri-solleva** il segnale che aveva
intercettato -- per lasciar succedere quello che sarebbe successo senza il suo
gestore -- e senza nessuno che lo assorba, un Ctrl+C andato a buon fine
chiudeva con sette righe di `KeyboardInterrupt`. La seconda no: con l'avvio
fallito (porta occupata, app che non importa) il processo usciva con **zero**,
cioe' diceva "tutto bene" a chi lo lancia da uno script.

**v2.34.0** — il workspace e' della conversazione, non dell'applicazione.

Ogni chat ricorda su quale cartella e' stata fatta (il campo era salvato da
sempre) e **riaprendola l'harness ci torna**: la cronologia parla di *quei*
file, e ritrovarsi puntati su un altro progetto e' il modo piu' rapido di far
scrivere l'agente nel posto sbagliato. Lo spostamento passa da un solo punto
(`applica_workspace`), avviene solo a turni fermi -- la cartella e' una per
processo: una sandbox, un albero nell'`<environment>`, un intervallo di porte
-- e una cartella sparita nel frattempo si ignora invece di impedire
l'apertura. Il percorso viaggia nel payload, quindi intestazione, scheda della
sandbox e cartelle recenti si aggiornano senza una seconda chiamata.

Rivisto il paragrafo sull'efficienza nei due system prompt: *i passi sono un
tetto, non un budget*; le richieste indipendenti vanno **nello stesso passo**
(le tool call di un passo vengono eseguite tutte prima che il modello torni a
decidere); e un ragionamento che si ripete e' un'azione non fatta.

E una trappola tolta di mezzo: il testo salvato in **Impostazioni → System
prompt** vince sempre su quello del codice. Quando il nostro prompt cambia,
una copia della versione precedente rimasta li' dentro smette di combaciare e
passa per "personalizzata": da quel momento l'harness non sceglie piu' fra
prompt esteso e snello, e nessuna modifica a `core/prompts.py` arriva piu' al
modello. Ora il campo vuoto vale "scegli tu", c'e' un bottone **Ripristina**, e
la spiegazione sta sotto al campo.

**v2.33.0** — Ctrl+C, sincronizzazione in diretta, app installabile.

**Ctrl+C non spegneva piu' niente.** Le rotte SSE (`/api/events`,
`/api/stream/{id}`) sono risposte HTTP che per mestiere non finiscono;
uvicorn, ricevuto il segnale, smette di accettare connessioni e poi *aspetta
che le risposte in corso finiscano*. Bastava una scheda del browser aperta --
cioe' sempre -- e l'unico modo di uscire era chiudere la finestra del
terminale. Ora il gestore del segnale avvisa gli stream **prima** che
l'attesa cominci (lo spegnimento della lifespan viene dopo, troppo tardi),
e resta un tetto di 5 secondi come rete di sicurezza. C'e' un test che avvia
un server vero, gli attacca un finto browser al bus e gli manda un SIGINT.

**La sincronizzazione fra i due schermi si bloccava dopo il primo turno.** Il
bus globale ridisegna la conversazione solo se la pagina non sta gia'
ricevendo un turno dal vivo, ma il controller dell'attacco non veniva mai
azzerato a fine stream: da li' in poi il desktop rispondeva "sto disegnando
io" per sempre. Dal telefono era peggio: non ascoltava il bus affatto, e
appena `running` diventava vero il polling smetteva di rileggere la
conversazione mentre nessuno stream era attaccato. Si vedeva soprattutto con
`ask_user_question`, dove il turno si chiude subito e la novita' e' tutta
nella domanda in sospeso.

**Sul telefono la domanda spariva dopo la risposta.** Ora lo scambio resta in
chiaro come sul desktop: la domanda con la sua cornice, la scelta nella bolla
di chi l'ha data.

**L'interfaccia mobile si installa.** La chiave era nuova ad ogni avvio,
quindi l'icona sulla schermata home apriva ogni giorno un link scaduto: ora
si genera una volta e vive nelle preferenze, e il manifest -- servito dal
processo, non dal disco -- se la porta dentro `start_url`. Icone rifatte: il
robottino in terracotta, piu' piccolo e lontano dai bordi, dentro la zona
sicura del ritaglio a cerchio di Android.

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
