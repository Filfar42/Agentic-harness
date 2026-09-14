"""System prompt e costruzione dell'environment header.

DIAGNOSI DEL PROMPT PRECEDENTE
------------------------------
Il vecchio system prompt conteneva questa direttiva:

    "CHAIN OF THOUGHT: Prima di eseguire qualsiasi operazione o dare una
     risposta, racchiudi il tuo ragionamento dentro i tag <think>..."

Su modelli instruct con function calling (Qwen2.5-coder, Llama 3.x, Mistral)
questa istruzione e' controproducente: obbliga il modello ad **aprire il canale
testuale** prima di poter agire. Una volta iniziato a generare testo, la
probabilita' di emettere il token speciale di tool call crolla, perche' il
template di chat mette i due canali in competizione. Il risultato osservato --
"i modelli non invocano i tool a meno che non vengano sollecitati" -- e' la
conseguenza diretta.

CORREZIONI APPLICATE
--------------------
1. Il ragionamento diventa **facoltativo e breve**, e viene esplicitamente
   dichiarato subordinato all'azione. Sui modelli con thinking nativo si usa
   invece il canale ``thinking`` di Ollama (parametro ``think=true``), che non
   compete con le tool call.
2. Si aggiunge una **tabella di trigger** esplicita: frase dell'utente ->
   tool da chiamare. E' la singola modifica con l'effetto maggiore sui modelli
   piccoli, che ragionano molto meglio per pattern-matching che per principi.
3. Si aggiunge un **environment header** con cwd, OS e albero del workspace.
   Senza contesto ambientale il modello non ha alcun motivo di chiamare
   ``list_files``: crede di sapere gia' cosa c'e'.
4. Si vieta esplicitamente di stampare JSON di tool call come testo, che era
   il comportamento che il vecchio parser regex di fallback cercava di salvare.
"""

from __future__ import annotations

from .tools import platform_summary, workspace_snapshot

from .system_prompt import SYSTEM_PROMPT, SYSTEM_PROMPT_LEAN


def pick_system_prompt(*, thinking: bool) -> str:
    """Prompt esteso o snello, secondo quello che il modello sa fare."""
    return SYSTEM_PROMPT_LEAN if thinking else SYSTEM_PROMPT


def is_stock_prompt(text: str) -> bool:
    """True se l'harness puo' scegliere il prompt da solo.

    Due casi: il testo e' **vuoto** -- cioe' nessuno ha chiesto niente di
    particolare -- oppure e' esattamente uno dei nostri.

    Il vuoto vale come "scegli tu" ed e' il gesto di ripristino: svuotare il
    campo nelle impostazioni rimette la scelta automatica. Serve, perche' qui
    c'e' una trappola silenziosa: il testo salvato nelle preferenze **vince
    sempre**, anche quando e' una copia di un nostro prompt di sei versioni
    fa. Quando il testo di riferimento cambia, quella copia smette di
    combaciare e diventa "personalizzato" senza che nessuno l'abbia
    personalizzato: da quel momento l'harness non sceglie piu' fra prompt
    esteso e snello, e ogni modifica fatta qui in ``prompts.py`` non arriva
    piu' al modello. Se ne accorge solo chi va a rileggere il campo.
    """
    testo = (text or "").strip()
    if not testo:
        return True
    return testo in {SYSTEM_PROMPT.strip(), SYSTEM_PROMPT_LEAN.strip()}


# Clausola sul ragionamento, aggiunta SOLO quando il canale thinking nativo e'
# spento e serve la tendina del pensiero nella UI.
#
# Sta a parte perche' e' la parte del prompt che compete con il tool calling: su
# un modello instruct, ordinare di aprire il canale testuale prima di ogni
# azione abbassa la probabilita' che venga emesso il token di tool call. Qui il
# ragionamento e' dichiarato facoltativo e, soprattutto, superfluo proprio nel
# momento in cui serve una chiamata.
THINK_CLAUSE = """\

# Ragionamento (facoltativo)
Se una richiesta richiede di pianificare, puoi racchiudere qualche riga di \
ragionamento in <think>...</think> prima di rispondere. Quando invece la mossa \
successiva e' evidente, salta il ragionamento e chiama direttamente il tool: \
davanti a una chiamata a tool il pensiero non serve.\
"""


def build_system_prompt(base: str, *, native_think: bool) -> str:
    """Aggiunge la clausola sul pensiero solo se serve davvero.

    Con il canale ``thinking`` nativo attivo (qwen3, deepseek-r1) i tag
    ``<think>`` nel testo sono ridondanti: il modello ha gia' un canale
    separato, e chiederglieli due volte peggiora e basta.
    """
    if native_think or "<think>" in base:
        return base
    return base + THINK_CLAUSE


WEB_SEARCH_CLAUSE = """\

# Ricerca online (attiva per questo turno)
Hai a disposizione anche il tool ``web_search``: interroga un motore di \
ricerca e restituisce titolo, URL e snippet dei risultati. Usalo quando la \
risposta dipende da fatti attuali o esterni al workspace (notizie, versioni \
recenti, documentazione non presente nel progetto), con query brevi e \
specifiche; se citi un risultato indica l'URL. Non serve per cio' che sai \
gia' o che sta nel workspace: li' usi list_files/read_file come sempre.\
"""


def append_web_search_clause(base: str, *, enabled: bool) -> str:
    """Aggiunge la descrizione di web_search solo quando la modalita' e' attiva.

    Il vincolo dell'utente: con la ricerca online spenta il modello non deve
    neppure sapere che il tool esiste -- nessun schema tra i tools, nessuna
    riga nel prompt. Cosi' non ci prova una chiamata fantasma e non si pagano
    token per un tool indisponibile.
    """
    return base if not enabled else base + WEB_SEARCH_CLAUSE


def build_env_header(
    workspace: str,
    *,
    tool_names: list[str] | None = None,
    sandbox: str = "host",
    preview_ports: tuple[int, int] | None = None,
) -> str:
    """Contesto ambientale iniettato dopo il system prompt.

    Nota sul caching: questa stringa e' **deterministica** a parita' di
    workspace. Non contiene timestamp ne' contatori, cosi' il prefisso del
    prompt resta byte-identico fra i passi agentici e il KV cache di Ollama
    viene riutilizzato invece di essere ricalcolato ad ogni passo.
    """
    tools_line = ", ".join(tool_names or [])
    # L'intervallo delle porte va detto qui e non nella descrizione del tool:
    # dipende dalle impostazioni di questa installazione, e un modello che
    # deve indovinare la porta la sbaglia. Resta deterministico -- cambia solo
    # quando l'utente cambia impostazione -- quindi il KV cache regge.
    ports_line = (
        f"porte_per_anteprime: {preview_ports[0]}-{preview_ports[1]} "
        "(pubblicate su 127.0.0.1; un'applicazione deve ascoltare su 0.0.0.0 "
        "e su una di queste porte per essere visibile)\n"
        if preview_ports
        else ""
    )
    shell_line = (
        "shell: container Linux isolato, il workspace e' montato su /work "
        "(usa percorsi relativi; fuori da /work non c'e' niente)\n"
        if sandbox == "docker"
        else f"shell: diretta sulla macchina, working directory {workspace}\n"
    )
    return (
        "<environment>\n"
        f"working_directory: {workspace}\n"
        f"platform: {platform_summary()}\n"
        f"{shell_line}"
        f"{ports_line}"
        f"tool_disponibili: {tools_line}\n"
        "\ncontenuto_del_workspace (letto dal disco adesso):\n"
        f"{workspace_snapshot(workspace)}\n"
        "</environment>\n\n"
        "Questo e' lo stato reale del disco all'inizio del turno: e' gia' la "
        "risposta a \"cosa c'e' nel progetto\", quindi non serve list_files per "
        "saperlo. Usa list_files solo per scendere oltre i livelli mostrati, o "
        "per rileggere una cartella dopo averci scritto dentro. Conosci i nomi "
        "dei file, non il loro contenuto: quello richiede read_file."
    )


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def build_attachments_block(entries: list[dict] | None) -> str:
    """Elenco degli allegati della conversazione: nomi, non contenuti.

    Mettere qui il testo dei file sarebbe comodo per il modello e disastroso
    per la finestra di contesto: un CSV da poche centinaia di KB la satura da
    solo. L'agente vede che i file esistono e dove sono; se gli servono, li
    legge con ``read_file``, pagando i token solo quando c'e' un motivo.
    """
    if not entries:
        return ""
    lines = "\n".join(
        f"  - {e['path']} ({_human_size(int(e.get('size') or 0))})" for e in entries
    )
    return (
        "<allegati>\n"
        "File che l'utente ha allegato a questa conversazione:\n"
        f"{lines}\n"
        "</allegati>\n\n"
        "Sono gia' sul disco, dentro il workspace. Ne conosci nome e "
        "dimensione, non il contenuto: per leggerli usa read_file sul percorso "
        "indicato. Se la richiesta dell'utente riguarda uno di questi file, "
        "aprilo prima di rispondere invece di chiedere cosa contiene."
    )


# Iniettato una sola volta quando il modello produce testo che descrive
# un'azione senza chiamare alcun tool. Volutamente corto e imperativo.
TOOL_NUDGE = (
    "Hai risposto a parole, ma la richiesta riguarda il workspace e il testo "
    "non tocca il disco. Esegui adesso l'azione chiamando il tool appropriato "
    "(list_files, read_file, search_files, write_file, edit_file, run_command). "
    "Solo la chiamata, senza altro testo. Se davvero non serve nessun tool, "
    "dillo in una riga e spiega perche'."
)

# Iniettato quando il modello ha fatto la cosa giusta -- fermarsi e chiedere --
# ma l'ha fatta a parole. Il testo libero non arriva all'utente come domanda:
# resta nel flusso e il turno si chiude senza risposta. Il tool si', e mette
# davanti delle opzioni su cui l'utente puo' solo cliccare.
ASK_NUDGE = (
    "Hai fatto bene a fermarti: la richiesta non basta a decidere. Ma una "
    "domanda scritta a parole non arriva all'utente come domanda. Rifalla "
    "adesso chiamando ask_user_question, con le opzioni concrete fra cui vuoi "
    "che scelga. Solo la chiamata, senza altro testo."
)

# Iniettato quando il turno sta per chiudersi con una verifica verde che pero'
# non tocca il codice appena scritto. Il caso reale: funzione nuova, zero test,
# `pytest` su un altro file, "10 passed", "lavoro completato" -- e la funzione
# sul disco sbagliata su tutti i casi di riferimento.
COVERAGE_NUDGE = (
    "Fermo: hai aggiunto {symbols}, ma nessun file di test nomina questi "
    "simboli. La verifica che hai eseguito e' verde perche' misura altro "
    "codice, non quello che hai appena scritto: come prova che funzioni non "
    "vale niente. Scrivi adesso i test che lo esercitano davvero -- almeno un "
    "caso con un valore atteso esplicito, non solo un controllo di lunghezza o "
    "di tipo -- ed eseguili. Se sei convinto che non servano, dillo e spiega "
    "perche' invece di chiudere in silenzio."
)

# Iniettato quando il turno ha usato dei tool ma si e' chiuso senza una parola.
# Senza questo, con i modelli piccoli l'utente si ritrova davanti a delle
# tendine chiuse e nessuna spiegazione.
SUMMARY_NUDGE = (
    "Hai usato dei tool ma questo passo non ha prodotto niente: ne' una "
    "chiamata ne' una parola.\n"
    "**Se la richiesta non e' ancora soddisfatta per intero, non chiudere: "
    "riprendi da dove eri e fai la prossima azione.** Rileggi cosa ti era stato "
    "chiesto -- spesso una richiesta ha due meta' e ci si ferma dopo la prima.\n"
    "Se invece hai davvero finito, chiudi con un messaggio breve in questo "
    "formato, senza altre chiamate a tool:\n"
    "Fatto: i punti che hai chiuso, con i file toccati.\n"
    "Verifica: come hai verificato i risultati, il comando eseguito e il suo esito reale. Se e' rosso dillo.\n"
    "Poi: i punti rimasti aperti, e quale affronteresti per primo.\n"
)

# Iniettato quando il turno starebbe per chiudersi con una verifica rossa.
# E' il pezzo che trasforma il ciclo di self-correction da buona intenzione a
# comportamento: un modello piccolo, davanti a un test fallito, tende a
# riassumere e chiudere come se avesse finito.
VERIFY_NUDGE = (
    "Il comando `{command}` e' fallito (exit code {code}) e non l'hai ancora "
    "sistemato: il lavoro non e' finito.\n"
    "Rileggi lo stderr del risultato qui sopra, individua file e riga e correggi "
    "con edit_file (se non ricordi il contenuto esatto, prima read_file). "
    "Rilancia `{command}` **dopo** aver cambiato qualcosa: rieseguirlo identico "
    "senza una modifica in mezzo ti ridara' lo stesso rosso, e ti costa un passo.\n"
    "Se questo rosso e' il risultato che ti aspettavi -- un test scritto apposta "
    "per riprodurre un bug prima di correggerlo -- non ti blocca: il piano puo' "
    "andare avanti lo stesso, e la verifica resta segnata finche' non e' verde.\n"
    "Fallo adesso, con una chiamata a tool: non rispondere a parole."
)

# Iniettato quando le ultime chiamate non hanno cambiato niente: stesso errore
# o stesso rifiuto, nessuna modifica in mezzo.
#
# E' il buco che restava fra gli altri due solleciti. ``LOOP_NUDGE`` guarda un
# comando che fallisce e ``RIPETIZIONE_NUDGE`` una chiamata che riesce e viene
# rifatta; il caso osservato -- ``manage_plan`` rifiutato, rilanciato, rifiutato
# di nuovo -- non era ne' l'uno ne' l'altro, perche' le chiamate **fallite** non
# entravano affatto nel conteggio delle ripetizioni.
STALLO_NUDGE = (
    "Hai chiamato `{tool}` {quante} volte di fila con lo stesso esito e senza "
    "cambiare niente in mezzo: ripetere non lo sbloccera'.\n"
    "Scegline una: (1) leggi con read_file il file che l'errore indica e cambia "
    "il codice; (2) isola il problema con un comando piu' piccolo -- un singolo "
    "test, un import; (3) se la decisione spetta all'utente, chiedi con "
    "ask_user_question. Se non si applica nessuna delle tre, chiudi il turno "
    "dicendo cosa resta rosso e perche'."
)

# Iniettato quando lo stesso comando fallisce piu' volte allo stesso modo:
# ripeterlo non lo aggiustera', e senza questo l'agente cicla fino a esaurire
# i passi disponibili.
LOOP_NUDGE = (
    "Hai eseguito `{command}` {count} volte e continua a fallire nello stesso "
    "modo: ripeterlo non lo sistemera'.\n"
    "Cambia approccio: rileggi con read_file il file che l'errore indica per "
    "vedere com'e' davvero adesso, oppure isola il problema con un comando piu' "
    "piccolo (un singolo test, un import). Se il problema e' una scelta che "
    "spetta all'utente -- una dipendenza da installare, un requisito ambiguo -- "
    "chiedi con ask_user_question invece di insistere."
)

# Iniettato quando la stessa chiamata, con gli stessi argomenti, torna per la
# terza volta nello stesso turno.
#
# E' il modo di guasto agentico piu' comune e l'unico che l'harness non
# copriva: ``VerificationTracker`` guarda i comandi che *falliscono*,
# ``esplorazioni_di_fila`` conta le esplorazioni ma non si accorge che sono la
# stessa. Una lettura che riesce e viene rifatta costa un passo e una seconda
# copia dello stesso file in contesto, e nessuno la vedeva.
RIPETIZIONE_NUDGE = (
    "Hai gia' chiamato `{tool}` con questi stessi argomenti {quante} volte in "
    "questo turno, e il risultato e' stato lo stesso: e' ancora qui sopra nella "
    "conversazione, scorri indietro e usalo.\n"
    "Se ti serve qualcosa che quel risultato non conteneva, cambia la chiamata "
    "-- un altro file, un'altra porzione, un altro strumento -- oppure di' cosa "
    "manca invece di richiedere la stessa cosa."
)

# Variante del riepilogo per un turno che si chiude comunque in rosso:
# il riepilogo deve dire la verita', non arrotondare.
FAILED_SUMMARY_NUDGE = (
    "Chiudi adesso con un messaggio breve, e sii esplicito sul fatto che la "
    "verifica NON passa. Queste sono rimaste rosse:\n"
    "{verifiche}\n"
    "Fatto: i punti che hai chiuso, con i file toccati.\n"
    "Verifica: come li hai verificati -- comando ed esito reale. Se e' rosso dillo.\n"
    "Poi: i punti rimasti aperti, e quale affronteresti per primo.\n"
    "Solo testo, nessuna nuova chiamata a tool."
)

# Iniettato quando il modello stampa una tool call come testo JSON.
JSON_LEAK_NUDGE = (
    "Hai stampato una chiamata a tool come testo JSON: cosi' non viene eseguita. "
    "Riemetti la stessa chiamata usando il meccanismo nativo di function calling."
)

# Iniettato quando la generazione e' stata tagliata dal tetto di num_predict.
#
# E' il caso che l'harness non sapeva riconoscere, e che sui modelli di
# ragionamento e' il piu' frequente di tutti: il modello pianifica l'intero
# compito nel canale di pensiero, finisce i token e chiude il turno senza aver
# emesso nulla. L'harness lo scambiava per "ha risposto a parole" e gli
# rispondeva "esegui l'azione", che davanti a una risposta *vuota* non vuol
# dire niente. Qui invece gli si dice la cosa vera: non hai finito di pensare,
# hai finito i token.
TRUNCATED_NUDGE = (
    "Il tuo ragionamento e' stato **troncato**: hai esaurito i token generabili "
    "in un passo ({tokens}) prima di arrivare a un'azione, quindi non e' "
    "arrivato niente ne' a me ne' all'utente.\n"
    "Non e' un problema di quanto sei bravo a pianificare: e' che stai "
    "pianificando troppo in una volta. Il turno ha piu' passi, e quello che "
    "decidi adesso lo ritrovi scritto al passo dopo.\n"
    "Adesso: niente ragionamento lungo. Decidi la **prossima singola azione** "
    "ed emetti la tool call, oppure -- se il compito ha piu' punti -- scrivi "
    "prima il piano con manage_plan e fermati li'."
)

# Iniettato quando il watchdog ha chiuso lo stream a meta' ragionamento.
#
# Differenza dal precedente: qui il taglio l'abbiamo fatto noi, prima che lo
# facesse num_predict, quindi il modello ha ancora budget per agire. Il tono
# cambia di conseguenza -- non "e' andata male", ma "ti ho fermato apposta".
THINK_WATCHDOG_NUDGE = (
    "Ti ho interrotto: eri a {tokens} token di ragionamento senza aver ancora "
    "chiamato un tool. A quel ritmo il budget finisce prima dell'azione e il "
    "turno si chiude a vuoto -- e' gia' successo.\n"
    "Il ragionamento serve a scegliere la mossa successiva, non a risolvere "
    "tutto il compito in testa. Quello che stavi pianificando non va perso se "
    "lo scrivi: usa manage_plan.\n"
    "Adesso fai una cosa sola, subito: il piano con manage_plan se il compito "
    "ha piu' punti, altrimenti la prima tool call concreta. Poche righe di "
    "pensiero, non di piu'."
)

# Iniettato quando la richiesta ha piu' azioni e il modello e' partito a
# lavorare senza un piano.
PLAN_NUDGE = (
    "Questa richiesta contiene piu' azioni distinte e non hai un piano.\n"
    "Scrivilo adesso con manage_plan action='set': 3-8 punti, uno per "
    "obiettivo verificabile, nell'ordine in cui li affronterai. Non e' "
    "burocrazia: e' l'unico posto dove lo stato del lavoro sopravvive: il tuo "
    "ragionamento viene scartato dal contesto ad ogni turno, il piano no. "
    "Serve anche all'utente, che lo vede aggiornarsi mentre lavori.\n"
    "Solo la chiamata a manage_plan, senza altro testo: il primo punto si apre "
    "da solo. Da li' in poi ogni action='complete' apre il successivo, quindi "
    "chiudi ogni punto NELLO STESSO PASSO dell'ultima azione che lo conclude "
    "-- una chiamata in piu' in quel passo, non un passo in piu'."
)

# Il turno ha esaurito i passi senza scrivere una parola. Non e' un sollecito:
# e' il prompt di sistema di **una chiamata sola e senza tool**, perche' un
# sollecito avrebbe bisogno di un passo successivo e non ce n'e' piu' uno.
#
# Perche' serve: `SUMMARY_NUDGE` copre il turno che si chiude da solo, e ha in
# guardia `step < max_steps` — cioe' non copre il turno esaurito, che e'
# esattamente quello lungo e faticoso in cui l'utente ha piu' bisogno di sapere
# cos'e' successo. Misurato il 23/08/2026: la sessione 20260820_112642 ha
# prodotto 21 passi, 46 risultati di tool, 124.877 caratteri di ragionamento e
# **zero caratteri di risposta**, mai. L'utente ha visto delle tendine aprirsi
# e chiudersi, e nient'altro.
PROMPT_RIEPILOGO_FINALE = """\
I passi a disposizione per questo turno sono finiti. Non puoi piu' chiamare \
tool: quello che hai davanti e' tutto quello che avrai.

Scrivi ORA all'utente, in italiano, cosa e' successo. E' l'unica cosa che \
vedra' di questo turno: senza, resta davanti a delle tendine chiuse.

Tre cose, brevi, in questo ordine:
1. **Cosa hai fatto davvero**, con i file toccati. Solo azioni riuscite: se un \
comando e' fallito dillo con l'errore, non contarlo fra le cose fatte.
2. **Dove ti sei fermato** e perche' -- i passi finiti non sono una scusa, \
sono un fatto: di' a che punto era il lavoro.
3. **La prossima mossa**, una riga: cosa dovresti fare al turno dopo.

Niente preamboli, niente scuse, niente elenchi di intenzioni. Non promettere \
di fare qualcosa adesso: adesso e' finita.\
"""

# Iniettato quando il modello sta esplorando a mano da parecchi passi senza
# aver mai delegato. Misurato il 23/08/2026: l'esplorazione fatta dal padre --
# read_file, search_files, list_files -- vale il 46% dei token di risultato che
# restano in contesto per sempre, mentre `esplora` compare in 4 sessioni su 24.
# Il tool c'e' e funziona: quello che manca e' che qualcuno lo nomini nel
# momento in cui servirebbe.
DELEGA_NUDGE = (
    "Sono {quante} letture di fila senza scrivere niente: stai esplorando. "
    "Ogni file che apri resta nel tuo contesto per tutto il resto del lavoro, "
    "anche quando ti e' servito una volta sola.\n"
    "Se quello che ti manca e' ancora una domanda -- 'dove sta X', 'chi usa "
    "Y', 'come si lancia Z' -- passala a `esplora`: legge lui, e a te torna "
    "solo la risposta. Se invece hai gia' trovato quello che cercavi, vai "
    "avanti e ignora questo messaggio."
)

# Coda al sollecito quando in questo workspace una delega ha gia' funzionato.
# Un esempio vero vale piu' di tre righe di istruzioni sulla forma giusta, e
# costa venti token nel punto in cui il modello sta gia' leggendo il sollecito:
# fuori di qui sarebbe un elenco in coda al contesto pagato a ogni passo per
# essere letto una volta ogni tanto. Il testo si ricava dai fatti al momento
# (vedi ``core/spec_delega.py``): se l'evidenza sparisce, la frase sparisce.
DELEGA_ESEMPIO = (
    "\nIn questo workspace ha funzionato una domanda cosi': «{domanda}»."
)

# Variante del riepilogo quando c'e' un piano: il riassunto non si fa a
# memoria, si legge da quello che e' stato dichiarato punto per punto.
PLAN_SUMMARY_NUDGE = (
    "Chiudi adesso il turno con un messaggio breve, costruito **sul piano** "
    "({summary}):\n"
    "Fatto: i punti che hai chiuso, con i file toccati.\n"
    "Verifica: come li hai verificati -- comando ed esito reale. Se e' rosso dillo.\n"
    "Poi: i punti rimasti aperti, e quale affronteresti per primo.\n"
    "Non dichiarare fatto un punto che nel piano non risulta chiuso. "
    "Solo testo, nessuna nuova chiamata a tool."
)


# ---------------------------------------------------------------------------
# Estratto del pensiero alla chiusura di un punto di piano
# ---------------------------------------------------------------------------
# Misurato il 23/08/2026 su quattro sessioni qwen3.8: 2.427.071 caratteri
# pensati contro 108.970 di risposte -- 22 a 1 -- e tutto buttato a fine passo
# (`strip_think_from_context`). Dentro non c'e' ripetizione (8-grammi ripetuti
# 0,5%, somiglianza fra blocchi consecutivi 0,15): il modello **delibera**, e
# quella deliberazione contiene fatti verificati che nessun altro posto
# registra. Il 10,1% e' ripensamento ('wait', 'actually') e il 5,9%
# auto-istruzioni: e' quello che va lasciato indietro.
#
# Perche' alla chiusura di un punto e non ad ogni passo: `manage_plan
# action='complete'` **pretende** gia' una nota e ne ha ottenute 23 su 24 in
# tre sessioni, mentre `manage_notes`, che la propone e basta, e' stato usato 0
# volte su 42 conversazioni. Il rito batte l'invito: l'estratto si attacca al
# rito che gia' funziona, e costa una chiamata per punto invece che per passo.
#
# In italiano, e non e' un dettaglio di gusto: 344 blocchi su 350 pensano in
# inglese mentre i punti di piano sono in italiano, e `libreria.precarico`
# ripesca incrociando **lessicalmente** le parole del punto aperto con i titoli
# delle voci. Un estratto in inglese scriverebbe una libreria che nessun
# precarico sa piu' ritrovare.
PROMPT_ESTRATTO_PENSIERO = """\
Ricevi il ragionamento grezzo di un agente di programmazione lungo un solo \
punto di lavoro, che adesso e' chiuso. Quel ragionamento sta per essere \
cancellato: e' l'unico posto in cui sono passati certi fatti.

Tieni solo cio' che sarebbe costoso riscoprire. In italiano, senza preamboli, \
in punti elenco brevi, sotto queste due voci (salta quella vuota):
SCOPERTO: fatti tecnici verificati -- errori veri con il loro testo, versioni, \
percorsi, firme di funzione, comandi che funzionano, vincoli dell'ambiente.
SCARTATO: strade provate che non hanno funzionato, e perche'. Servono a non \
rifarle.

Butta senza pieta': i ripensamenti ('aspetta', 'in realta''), le \
auto-istruzioni ('dovrei...'), i piani su cosa fare dopo, le riformulazioni \
della richiesta, e tutto cio' che e' gia' evidente dal testo del punto.

Regole: solo cose presenti nel ragionamento, mai dedotte e mai completate a \
intuito. Un'ipotesi che il ragionamento non ha verificato non e' uno SCOPERTO: \
o la ometti, o scrivi che era un'ipotesi. Se non resta niente che valga la \
pena conservare, rispondi esattamente NIENTE e nient'altro.\
"""


# ---------------------------------------------------------------------------
# Quanto costa davvero il blocco fisso
# ---------------------------------------------------------------------------


def costi_del_prefisso() -> dict[str, int]:
    """Il peso in token di prompt e schemi, misurato adesso.

    I numeri stavano nei commenti -- "il prompt snello occupa 612 token", "gli
    schemi ~1.790" -- ed erano sbagliati di 4,9x e 2,4x: erano veri quando
    qualcuno li ha misurati, e sono invecchiati in silenzio mentre i testi
    crescevano. Un numero che si ricalcola non puo' mentire, e un test puo'
    controllare che il rapporto fra i due percorsi sia ancora quello che
    giustifica la scelta automatica.

    Importa i tool qui dentro e non in testa al modulo perche' ``tools``
    importa ``prompts``: il giro si chiude solo a runtime.
    """
    import json

    from .textutils import estimate_tokens
    from .tools import TOOLS_SCHEMA, TOOLS_SCHEMA_LEAN

    def _schema(s: list) -> int:
        return estimate_tokens(json.dumps(s, ensure_ascii=False))

    return {
        "prompt_esteso": estimate_tokens(SYSTEM_PROMPT),
        "prompt_snello": estimate_tokens(SYSTEM_PROMPT_LEAN),
        "schemi_estesi": _schema(TOOLS_SCHEMA),
        "schemi_snelli": _schema(TOOLS_SCHEMA_LEAN),
    }
