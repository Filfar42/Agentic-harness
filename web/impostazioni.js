/* Impostazioni: la finestra con la barra laterale.
 *
 * Il menu erano cinquanta campi scritti a mano in index.html, cinque schede e
 * cinquanta righe di bindField in app.js. Una scheda ("Efficienza") era
 * diventata il posto dove finiva tutto quello che non aveva un posto -- piano,
 * compattazione, Docker, porte, deleghe -- e le voci che valgono solo per un
 * server (keep_alive per Ollama, lo slot di servizio per llama.cpp) si
 * vedevano sempre, qualunque server ci fosse dall'altra parte.
 *
 * Ora il menu e' costruito da uno schema: una voce = una chiave di DEFAULTS,
 * con etichetta, una riga di spiegazione, il tipo di controllo e le
 * condizioni in cui ha senso mostrarla. Ricerca, segno "modificato",
 * ripristino ai valori di serie e voci che compaiono solo con un certo server
 * leggono tutti lo stesso schema: aggiungere un'impostazione vuol dire
 * aggiungere una riga qui (o in IMP_FUORI_MENU, dicendo perche' non c'e'), e
 * c'e' un test che se ne accorge se la si dimentica.
 *
 * Il salvataggio resta quello di sempre: ogni controllo passa da bindField
 * (app.js), che lo registra in BOUND_FIELDS, e il riallineamento resta uno
 * solo -- syncSettingsWidgets, chiamato da renderHeader -- che in coda
 * chiama impRiallinea per i pezzi disegnati sopra i campi.
 *
 * Questo file si carica PRIMA di app.js: usa le sue funzioni ($, el, esc, api,
 * toast, bindField, saveSettings, state...) solo dentro le funzioni, cioe'
 * quando app.js le ha gia' definite.
 */

// ---------------------------------------------------------------------------
// Icone
// ---------------------------------------------------------------------------

const IMP_TRATTI = {
  server: '<rect x="3.5" y="4" width="17" height="7" rx="2"/><rect x="3.5" y="13" width="17" height="7" rx="2"/><path d="M7.5 7.5h.01M7.5 16.5h.01"/>',
  generazione: '<path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"/><circle cx="15" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/>',
  comportamento: '<path d="M10 6.5h10M10 12h10M10 17.5h10"/><path d="m3.5 6.5 1.6 1.6 2.6-3.1M3.5 12l1.6 1.6 2.6-3.1"/><circle cx="5.3" cy="17.5" r="1.3"/>',
  contesto: '<path d="m12 3.5 8.5 4.5-8.5 4.5L3.5 8z"/><path d="m3.5 12.2 8.5 4.5 8.5-4.5"/><path d="m3.5 16.2 8.5 4.3 8.5-4.3"/>',
  istruzioni: '<path d="M6.5 3.5h7.5l4.5 4.5v12.5h-12z"/><path d="M14 3.5V8h4.5M9.5 12.5h6M9.5 16h6"/>',
  memorie: '<path d="M7 3.5h10a1 1 0 0 1 1 1v16l-6-4-6 4v-16a1 1 0 0 1 1-1z"/>',
  sandbox: '<path d="m12 3 8.5 4.5v9L12 21l-8.5-4.5v-9z"/><path d="M3.5 7.5 12 12l8.5-4.5M12 12v9"/>',
  anteprime: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/>',
  aspetto: '<circle cx="12" cy="12" r="8.5"/><path d="M12 3.5v17a8.5 8.5 0 0 0 0-17z" fill="currentColor" stroke="none"/>',
  info: '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.5M12 7.8h.01"/>',
  cerca: '<circle cx="10.5" cy="10.5" r="6"/><path d="m15 15 5 5"/>',
  chiudi: '<path d="M6 6l12 12M18 6 6 18"/>',
  ripristina: '<path d="M4 12a8 8 0 1 0 2.4-5.7"/><path d="M4 4.5v4h4"/>',
  occhio: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/>',
  nascondi: '<path d="M3 3l18 18"/><path d="M10.6 5.6A10 10 0 0 1 12 5.5c6 0 9.5 6.5 9.5 6.5a17 17 0 0 1-3.1 3.9M6.3 6.4C3.9 8.1 2.5 12 2.5 12S6 18.5 12 18.5a9.6 9.6 0 0 0 4.2-.9"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>',
  copia: '<rect x="8.5" y="8.5" width="11" height="11" rx="2"/><path d="M15.5 8.5v-2a2 2 0 0 0-2-2h-7a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h2"/>',
  scarica: '<path d="M12 4v11M7.5 10.5 12 15l4.5-4.5M5 19.5h14"/>',
  carica: '<path d="M12 15.5v-11M7.5 9 12 4.5 16.5 9M5 19.5h14"/>',
  aggiorna: '<path d="M20 12a8 8 0 1 1-2.4-5.7"/><path d="M20 4.5v4h-4"/>',
  spunta: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  avviso: '<path d="M12 4 21 19.5H3z"/><path d="M12 10v4.5M12 17h.01"/>',
  cestino: '<path d="M4.5 7h15M10 11v6M14 11v6M6.5 7l.8 12a1.5 1.5 0 0 0 1.5 1.5h6.4a1.5 1.5 0 0 0 1.5-1.5l.8-12M9.5 7V4.5h5V7"/>',
  freccia: '<path d="m9 6 6 6-6 6"/>',
};

function impIcona(nome, classe = '') {
  return `<svg class="imp-icona ${classe}" viewBox="0 0 24 24" fill="none" stroke="currentColor" `
    + 'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" '
    + `focusable="false">${IMP_TRATTI[nome] || ''}</svg>`;
}

// ---------------------------------------------------------------------------
// Schema
// ---------------------------------------------------------------------------
//
// Ogni voce:
//   k          chiave di DEFAULTS
//   tipo       interruttore | segmenti | scelta | testo | segreto | numero |
//              cursore | modello
//   etichetta  cosa si sceglie, in parole
//   descr      una riga su cosa cambia per chi usa l'harness (testo o
//              funzione del contesto). Ogni frase deve essere vera: il
//              progetto ha gia' pagato piu' volte le spiegazioni che
//              raccontavano un comportamento che il codice non aveva
//   solo       server per cui la voce vale (ollama | llamacpp | openai):
//              con un server diverso la voce non si mostra
//   quando     funzione del contesto: senza, la voce non si mostra
//   dipende    interruttore che deve essere acceso perche' la voce conti:
//              spento, la voce resta visibile ma disattivata
//   badge      sperimentale | container
//   ripristino false = niente "valore di serie" da proporre (indirizzi,
//              chiavi, nomi di modello: il default di fabbrica li romperebbe)
//   cerca      parole in piu' per la ricerca (nomi tecnici, sinonimi)

const IMP_SERVER = { ollama: 'Ollama', llamacpp: 'llama.cpp', openai: 'OpenAI-compatibile' };

const IMP_BADGE = {
  sperimentale: {
    testo: 'Sperimentale', classe: 'warn',
    titolo: 'Non ancora misurata sulle tue conversazioni: provala prima di lasciarla accesa.',
  },
  container: {
    testo: 'Ricrea il container', classe: '',
    titolo: 'Il container della sandbox si ricrea: il prossimo comando parte in uno nuovo.',
  },
};

const IMP_FINESTRE = [2048, 4096, 8192, 16384, 24576, 32768, 49152, 65536, 98304, 131072];

// Quanto occupano prompt di sistema e schemi dei tool prima che l'utente abbia
// scritto una parola: 1.604 + 3.702 token (snello) e 1.863 + 4.943 (esteso),
// stimati a 3,6 caratteri per token sul testo del 26/09/2026.
const IMP_PESO_FISSO = 'Prompt di sistema e schemi dei tool ne occupano già 5-7 mila.';

const IMP_SEZIONI = [
  {
    id: 'server', gruppoNav: 'Modello', icona: 'server', titolo: 'Modello e server',
    descr: 'Dove gira il modello e come l\'harness ci parla.',
    blocchi: [
      { speciale: 'stato-server', cerca: 'stato online offline verifica connessione' },
      {
        titolo: 'Connessione',
        campi: [
          {
            k: 'api_base', tipo: 'testo', etichetta: 'Indirizzo del server', largo: true,
            ripristino: false, segnaposto: 'http://localhost:11434',
            descr: 'Ollama di solito risponde su <code>http://localhost:11434</code>, llama-server su '
              + '<code>http://host:8080</code>, OpenRouter su <code>https://openrouter.ai/api/v1</code>.',
            cerca: 'api_base endpoint url host indirizzo porta',
          },
          {
            k: 'transport', tipo: 'segmenti', etichetta: 'Tipo di server',
            opzioni: [['auto', 'Automatico'], ['ollama', 'Ollama'], ['llamacpp', 'llama.cpp'], ['openai', 'OpenAI-compatibile']],
            descr: (c) => 'Automatico prova Ollama, poi llama.cpp, poi il dialetto OpenAI. '
              + 'Sceglilo a mano per OpenRouter, vLLM o LM Studio.'
              + (c.s.transport === 'auto' && c.rilevato ? ` Adesso: <b>${esc(IMP_SERVER[c.rilevato])}</b>.` : ''),
            cerca: 'transport backend ollama llama.cpp llamacpp openai openrouter vllm lm studio dialetto',
          },
          {
            k: 'model_name', tipo: 'modello', etichetta: 'Modello', largo: true, ripristino: false,
            segnaposto: 'qwen3.8:27b oppure qwen/qwen3-coder',
            descr: 'Scegli fra quelli che il server dichiara o scrivi un nome: su OpenRouter l\'elenco '
              + 'può non contenerlo. Si cambia anche dal nome in cima alla chat.',
            cerca: 'model_name modello qwen nome',
          },
          {
            k: 'api_key', tipo: 'segreto', etichetta: 'Chiave API', largo: true, ripristino: false,
            quando: (c) => c.server !== 'ollama' || !!c.s.api_key,
            descr: 'Per OpenRouter e OpenAI, o per un llama-server avviato con <code>--api-key</code>. '
              + 'Con un server locale di solito resta vuota.',
            cerca: 'api_key chiave token openrouter autorizzazione bearer segreto',
          },
        ],
      },
      {
        titolo: 'Avanzate', avanzate: true,
        campi: [
          {
            k: 'timeout_seconds', tipo: 'numero', etichetta: 'Attesa di un server muto', unita: 's',
            min: 10, max: 1800, passo: 10,
            descr: 'Secondi di silenzio tollerati, caricamento del prompt compreso. Non limita quanto '
              + 'dura una risposta.',
            cerca: 'timeout inattivita silenzio secondi',
          },
          {
            k: 'slot_servizio', tipo: 'numero', etichetta: 'Slot di servizio', solo: ['llamacpp'],
            min: -1, max: 15, passo: 1,
            descr: 'Con llama-server avviato con <code>-np 2</code> o più: lo slot per riassunti, estratti '
              + 'e deleghe, così non spostano la cache della conversazione. −1 = spento.',
            cerca: 'slot np parallel cache kv servizio',
          },
          {
            k: 'gpu_total_vram_mb', tipo: 'numero', etichetta: 'VRAM della scheda remota', unita: 'MB',
            solo: ['ollama'], min: 0, max: 262144, passo: 1024,
            descr: 'Solo con Ollama su un\'altra macchina: da qui la scheda non si vede, e il profilo '
              + 'consigliato ne ha bisogno per proporre un contesto. 0 = Ollama gira qui.',
            cerca: 'vram gpu memoria scheda nvidia remota',
          },
        ],
      },
    ],
  },
  {
    id: 'generazione', gruppoNav: 'Modello', icona: 'generazione', titolo: 'Generazione',
    descr: 'Quanto contesto, quanto ragionamento, quanti passi per turno.',
    alMostrare: () => refreshProfile(),
    blocchi: [
      { speciale: 'profilo', cerca: 'profilo consigliato applica valori consigliati' },
      {
        titolo: 'Limiti',
        campi: [
          {
            k: 'num_ctx', tipo: 'scelta', numerico: true, etichetta: 'Finestra di contesto',
            opzioni: IMP_FINESTRE.map((n) => [String(n), `${n / 1024}k · ${n.toLocaleString('it-IT')} token`]),
            descr: (c) => {
              const fissata = state.profilo && state.profilo.server_num_ctx;
              if (c.server === 'llamacpp') {
                return 'Con llama.cpp vale il minore fra questo valore e il <code>-c</code> del server'
                  + (fissata ? ` (${Number(fissata).toLocaleString('it-IT')})` : '')
                  + ': alzarlo oltre non allarga niente. ' + IMP_PESO_FISSO;
              }
              if (c.server === 'openai') {
                return 'La finestra che l\'harness assume per tagliare i risultati e decidere quando '
                  + 'riassumere: mettila uguale a quella del modello. ' + IMP_PESO_FISSO;
              }
              if (c.server === 'ollama') {
                return 'Quanti token Ollama tiene in memoria per la conversazione: più è grande, più '
                  + 'VRAM serve. ' + IMP_PESO_FISSO;
              }
              return 'La finestra su cui l\'harness taglia i risultati e decide quando riassumere. '
                + IMP_PESO_FISSO;
            },
            cerca: 'num_ctx contesto finestra context token n_ctx',
          },
          {
            k: 'max_tokens', tipo: 'cursore', etichetta: 'Lunghezza massima di una risposta',
            min: 256, max: 16384, passo: 256, unita: 'token',
            descr: 'Token che il modello può generare in un passo. Se la finestra è quasi piena, '
              + 'l\'harness la riduce da solo.',
            cerca: 'max_tokens num_predict output generazione',
          },
          {
            k: 'max_agent_loops', tipo: 'numero', etichetta: 'Passi per turno', min: 1, max: 500, passo: 1,
            descr: 'Quante volte il modello può agire in un turno prima che l\'harness lo chiuda con un '
              + 'riepilogo.',
            cerca: 'max_agent_loops passi step loop iterazioni tetto',
          },
        ],
      },
      {
        titolo: 'Ragionamento',
        campi: [
          {
            k: 'native_think', tipo: 'segmenti', etichetta: 'Ragionamento', sotto: true,
            opzioni: [['auto', 'Automatico'], ['no', 'Spento'], ['si', 'Acceso'], ['low', 'Basso'],
              ['medium', 'Medio'], ['high', 'Alto'], ['max', 'Massimo']],
            descr: 'Automatico lo accende, a livello alto, solo se il modello dichiara di saper '
              + 'ragionare. I livelli contano sui modelli che li capiscono. Per un messaggio solo c\'è '
              + 'la goccia del pensiero sotto la chat.',
            cerca: 'native_think thinking pensiero ragionamento reasoning livello effort',
          },
          {
            k: 'think_watchdog', tipo: 'interruttore', etichetta: 'Regia del pensiero',
            // Il budget per tipo di punto conta solo dove il pensiero si puo'
            // chiudere continuando (Ollama): dove l'unico taglio lo butta, la
            // soglia non scende mai sotto il 55% storico (``WATCHDOG_RATIO``).
            descr: 'Tiene il ragionamento proporzionato al lavoro: poco per eseguire, di più per '
              + 'progettare o cercare un errore. Con Ollama, superato il budget, il pensiero si chiude e '
              + 'il modello passa all\'azione; con gli altri server si interrompe solo quello che supera '
              + 'anche il 55% della lunghezza massima di una risposta, e il passo si rifà.',
            cerca: 'think_watchdog watchdog budget overthinking pensiero lungo interrompi',
          },
        ],
      },
      {
        titolo: 'Campionamento', avanzate: true,
        nota: 'Il profilo consigliato, qui sopra, li imposta per il modello in uso.',
        campi: [
          {
            k: 'temperature', tipo: 'cursore', etichetta: 'Temperatura', min: 0, max: 1.5, passo: 0.05,
            descr: 'Più è bassa, più le risposte sono prevedibili. Sopra 0,4 i modelli piccoli '
              + 'cominciano a inventare nomi di parametri nei tool.',
            cerca: 'temperature temperatura casualita',
          },
          {
            k: 'top_p', tipo: 'cursore', etichetta: 'top_p', min: 0.1, max: 1, passo: 0.05,
            descr: 'Il modello sceglie solo fra i token più probabili che insieme arrivano a questa '
              + 'probabilità.',
            cerca: 'top_p nucleus',
          },
          {
            k: 'top_k', tipo: 'cursore', etichetta: 'top_k', min: 1, max: 100, passo: 1,
            solo: ['ollama', 'llamacpp'],
            descr: 'Il modello sceglie solo fra i k token più probabili. Qwen 3 raccomanda 20.',
            cerca: 'top_k',
          },
          {
            k: 'presence_penalty', tipo: 'cursore', etichetta: 'Penalità di presenza', min: 0, max: 2,
            passo: 0.1, solo: ['ollama', 'llamacpp'],
            descr: 'Aiuta i modelli che ragionano a non girare in tondo. Sul codice tienila a 0: '
              + 'penalizza gli identificatori ripetuti, che devono restare uguali.',
            cerca: 'presence_penalty penalita presenza',
          },
          {
            k: 'repetition_penalty', tipo: 'cursore', etichetta: 'Penalità di ripetizione', min: 1, max: 2,
            passo: 0.05, solo: ['ollama', 'llamacpp'],
            descr: '1 = spenta. Sopra 1,05 rischia di spezzare le parole comuni a metà frase.',
            cerca: 'repetition_penalty repeat_penalty ripetizione',
          },
        ],
      },
      {
        titolo: 'Ollama', solo: ['ollama'],
        campi: [
          {
            k: 'keep_alive', tipo: 'scelta', etichetta: 'Tieni il modello caricato',
            opzioni: [['0', 'No, scaricalo subito'], ['5m', '5 minuti'], ['30m', '30 minuti'],
              ['2h', '2 ore'], ['-1', 'Sempre']],
            descr: 'Evita i 4-15 secondi di ricaricamento in VRAM fra un passo e l\'altro.',
            cerca: 'keep_alive vram caricato memoria',
          },
          {
            k: 'num_gpu', tipo: 'numero', etichetta: 'Layer su GPU', min: 0, max: 999, passo: 1,
            descr: '999 = tutti. Abbassalo solo se la VRAM non basta.',
            cerca: 'num_gpu layer gpu offload',
          },
        ],
      },
    ],
  },
  {
    id: 'comportamento', gruppoNav: 'Agente', icona: 'comportamento', titolo: 'Comportamento',
    descr: 'Le reti di sicurezza del ciclo: piano, avanzamento, esplorazioni.',
    blocchi: [
      {
        titolo: 'Piano',
        campi: [
          {
            k: 'require_plan', tipo: 'interruttore', etichetta: 'Chiedi un piano sui compiti con più obiettivi',
            descr: 'Se una richiesta lunga elenca più passi, più azioni o più file e il modello parte '
              + 'senza piano, l\'harness glielo chiede una volta.',
            cerca: 'require_plan piano obiettivi',
          },
          {
            k: 'plan_gate', tipo: 'interruttore', etichetta: 'Fammi leggere i piani lunghi prima di partire',
            descr: 'Da 6 punti in su il turno si ferma e ti mostra il piano: procedi o correggi. Sui '
              + 'compiti corti non si fa vedere.',
            cerca: 'plan_gate cancello piano lungo conferma',
          },
        ],
      },
      {
        titolo: 'Avanzamento',
        campi: [
          {
            k: 'monitor_avanzamento', tipo: 'interruttore', etichetta: 'Ferma i turni che girano a vuoto',
            descr: 'Conta i passi senza effetti: nessun file nuovo o cambiato, nessun test che passa, '
              + 'nessun punto del piano chiuso. A 6 lo dice al modello e gli lascia chiudere spiegando '
              + 'cosa blocca; a 10 chiude il turno con un riepilogo.',
            cerca: 'monitor_avanzamento stallo loop vuoto progresso',
          },
        ],
      },
      {
        titolo: 'Esplorazioni delegate',
        campi: [
          {
            k: 'spec_delega', tipo: 'interruttore', etichetta: 'Impara da come vanno le esplorazioni',
            descr: 'L\'esploratore vede quanti passi gli restano, e l\'esito di ogni esplorazione resta '
              + 'in <code>.memoria/.deleghe.json</code>. Da lì l\'harness ricava un avviso, quando qui le '
              + 'esplorazioni sforano spesso, e un esempio di domanda ben posta.',
            cerca: 'spec_delega delega esploratore figlio sotto-agente',
          },
        ],
      },
    ],
  },
  {
    id: 'contesto', gruppoNav: 'Agente', icona: 'contesto', titolo: 'Contesto e memoria',
    descr: 'Cosa resta davanti al modello a ogni passo e cosa finisce su disco.',
    blocchi: [
      {
        titolo: 'Riassunto automatico',
        campi: [
          {
            k: 'compact_history', tipo: 'interruttore', etichetta: 'Riassumi quando il contesto si riempie',
            descr: 'La parte vecchia della conversazione viene riassunta da una chiamata dedicata e tolta '
              + 'alla vista del modello; in chat resta tutto. Costa una generazione e un ricalcolo della '
              + 'cache.',
            cerca: 'compact_history compattazione riassunto cronologia',
          },
          {
            k: 'compact_threshold', tipo: 'cursore', etichetta: 'Soglia', formato: 'percento',
            min: 0.4, max: 0.95, passo: 0.05, dipende: 'compact_history',
            descr: 'Quanto si riempie la finestra prima di riassumere. A 75% resta un quarto libero per '
              + 'il passo successivo.',
            cerca: 'compact_threshold soglia percentuale',
          },
          {
            k: 'compact_max_tokens', tipo: 'numero', etichetta: 'Tetto in token', unita: 'token',
            min: 0, max: 200000, passo: 4096, dipende: 'compact_history',
            descr: 'Con le finestre grandi la percentuale da sola scatterebbe tardi: con la soglia di '
              + 'serie si riassume al più tardi qui. 0 = nessun tetto.',
            cerca: 'compact_max_tokens tetto token limite',
          },
          {
            k: 'compattazione_selettiva', tipo: 'segmenti', etichetta: 'Prima del riassunto, prova a sfoltire',
            opzioni: [['spenta', 'Spenta'], ['laya', 'Con Laya']], badge: ['sperimentale'],
            dipende: 'compact_history',
            descr: 'Un <code>laya-serve</code> in locale decide chiamata per chiamata quali risultati '
              + 'vecchi togliere: il testo resta parola per parola e, se non basta, si riassume come '
              + 'sempre. Misurala prima con <code>scripts/valuta_laya.py</code>.',
            cerca: 'compattazione_selettiva laya jev selezione sfoltire',
          },
          {
            k: 'laya_url', tipo: 'testo', etichetta: 'Indirizzo di laya-serve', largo: true,
            quando: (c) => c.s.compattazione_selettiva === 'laya',
            nascostaPerche: 'Compare con la compattazione selettiva su Laya.',
            dipende: 'compact_history',
            descr: 'Parla il protocollo di Jev (<code>/v1/systemone</code>).',
            cerca: 'laya_url laya indirizzo systemone',
          },
          {
            k: 'laya_modello', tipo: 'scelta', etichetta: 'Checkpoint di Laya',
            opzioni: [['multilingual', 'multilingual'], ['english', 'english'], ['typed-decisions', 'typed-decisions']],
            quando: (c) => c.s.compattazione_selettiva === 'laya',
            nascostaPerche: 'Compare con la compattazione selettiva su Laya.',
            dipende: 'compact_history',
            descr: '<code>multilingual</code> per le conversazioni in italiano.',
            cerca: 'laya_modello checkpoint',
          },
        ],
      },
      {
        titolo: 'Memoria del progetto',
        campi: [
          {
            k: 'memoria_progetto', tipo: 'interruttore',
            etichetta: 'Aggiorna la memoria del progetto a fine turno',
            descr: 'Nei progetti, dopo un turno che ha scritto file, lanciato comandi o chiuso punti del '
              + 'piano, l\'harness chiede al modello cosa deve restare per le prossime chat: decisioni, '
              + 'convenzioni, strade scartate, lavori aperti. Un passo in più, senza pensiero, che riusa '
              + 'la cache. Le voci che scrivi o correggi tu non le tocca.',
            cerca: 'memoria_progetto memoria progetto continuita chat automatica',
          },
        ],
      },
      {
        titolo: 'Memoria su disco',
        campi: [
          {
            k: 'libreria_concetti', tipo: 'interruttore', etichetta: 'Archivia i tratti riassunti in .memoria/',
            descr: 'Ogni riassunto resta su disco com\'è: in contesto ne rimane una riga di indice, e non '
              + 'viene riassunto una seconda volta. Il modello lo rilegge quando serve.',
            cerca: 'libreria_concetti libreria memoria archivio',
          },
          {
            k: 'estratto_pensiero', tipo: 'interruttore', dipende: 'libreria_concetti',
            etichetta: 'Distilla il ragionamento quando si chiude un punto del piano',
            descr: 'Una chiamata per punto chiuso tiene solo quello che il ragionamento ha scoperto o '
              + 'scartato, e lo archivia in <code>.memoria/</code>.',
            cerca: 'estratto_pensiero estratto distilla scoperto scartato',
          },
          {
            k: 'deposito_risultati', tipo: 'interruttore', etichetta: 'Conserva i risultati troppo lunghi in .deposito/',
            descr: 'L\'uscita completa di comandi e ricerche finisce su disco prima del taglio: il modello '
              + 'ne rilegge la coda invece di rilanciare il comando.',
            cerca: 'deposito_risultati deposito troncamento output',
          },
          {
            k: 'deposito_max_mb', tipo: 'numero', etichetta: 'Spazio del deposito', unita: 'MB',
            min: 0, max: 4096, passo: 16, dipende: 'deposito_risultati',
            descr: 'Oltre, escono i file più vecchi. 0 = nessun limite.',
            cerca: 'deposito_max_mb spazio tetto megabyte',
          },
        ],
      },
      {
        titolo: 'Avanzate', avanzate: true,
        campi: [
          {
            k: 'compact_old_tool_results', tipo: 'interruttore', etichetta: 'Accorcia i risultati vecchi dei tool',
            descr: 'Tiene interi solo i risultati più recenti (quanti dipende dalla finestra) e riduce gli '
              + 'altri a un sommario. Spento, ogni risultato resta intero fino al riassunto.',
            cerca: 'compact_old_tool_results risultati vecchi sommario',
          },
          {
            k: 'strip_think_from_context', tipo: 'interruttore',
            etichetta: 'Non rimandare al modello il ragionamento passato',
            descr: 'Il ragionamento resta leggibile in chat ma non torna nel contesto dei passi '
              + 'successivi. Spento, costa molto contesto.',
            cerca: 'strip_think_from_context think pensiero contesto',
          },
        ],
      },
    ],
  },
  {
    id: 'istruzioni', gruppoNav: 'Agente', icona: 'istruzioni', titolo: 'Istruzioni',
    descr: 'Il prompt di sistema che il modello riceve a ogni passo.',
    alMostrare: () => impCaricaPrompt(),
    blocchi: [{ speciale: 'prompt', cerca: 'system prompt sistema istruzioni personalizzato di serie esteso snello' }],
  },
  {
    id: 'memorie', gruppoNav: 'Agente', icona: 'memorie', titolo: 'Memorie',
    descr: 'Fatti da ricordare fra una conversazione e l\'altra.',
    alMostrare: () => impCaricaMemorie(),
    blocchi: [{ speciale: 'memorie', cerca: 'memorie memoria ricorda fatti manage_memory' }],
  },
  {
    id: 'sandbox', gruppoNav: 'Ambiente', icona: 'sandbox', titolo: 'Sandbox',
    descr: 'Dove girano i comandi dell\'agente e cosa possono toccare.',
    alMostrare: () => refreshSandbox(),
    blocchi: [
      { speciale: 'stato-sandbox', cerca: 'stato docker container sandbox' },
      {
        titolo: 'Esecuzione',
        campi: [
          {
            k: 'sandbox', tipo: 'segmenti', etichetta: 'Dove girano i comandi',
            opzioni: [['docker', 'In un container Docker'], ['host', 'Sulla macchina']],
            descr: (c) => (c.s.sandbox === 'host'
              ? 'Nessun recinto: l\'agente vede tutto il disco, e un <code>cd ..</code> lo porta fuori '
                + 'dalla cartella di lavoro.'
              : 'Il container monta solo la cartella di lavoro: il resto del disco non si vede.'),
            cerca: 'sandbox docker host recinto isolamento',
          },
          {
            k: 'sandbox_network', tipo: 'interruttore', etichetta: 'Rete nel container', badge: ['container'],
            quando: (c) => c.docker, nascostaPerche: 'Vale solo con i comandi in Docker.',
            descr: 'Spenta, l\'agente non può scaricare pacchetti né mandare dati fuori.',
            cerca: 'sandbox_network rete internet network',
          },
          {
            k: 'confirm_commands', tipo: 'interruttore', etichetta: 'Consenti comandi distruttivi', pericolo: true,
            descr: 'Spento, un filtro blocca <code>rm -rf</code>, <code>format</code> e simili prima che '
              + 'partano.',
            cerca: 'confirm_commands distruttivi pericolosi rm format guard-rail',
          },
        ],
      },
      {
        titolo: 'Automatismi', quando: (c) => c.docker,
        campi: [
          {
            k: 'docker_autostart', tipo: 'interruttore', etichetta: 'Avvia Docker all\'apertura dell\'harness',
            descr: 'Docker Desktop ci mette 20-60 secondi: così è pronto prima del primo messaggio. Se è '
              + 'già acceso non fa niente.',
            cerca: 'docker_autostart avvio docker desktop',
          },
          {
            k: 'image_autobuild', tipo: 'interruttore',
            etichetta: 'Costruisci l\'immagine del progetto quando scegli una cartella',
            descr: 'Solo se manca e se non hai scelto un\'immagine tua. Parte in background e viene '
              + 'selezionata quando è pronta.',
            cerca: 'image_autobuild immagine build costruisci',
          },
        ],
      },
      {
        titolo: 'Avanzate', avanzate: true, quando: (c) => c.docker,
        campi: [
          {
            k: 'docker_image', tipo: 'testo', etichetta: 'Immagine del container', largo: true, mono: true,
            badge: ['container'], ripristino: false,
            descr: 'Di solito quella del progetto, costruita da sola. Deve contenere quello che l\'agente '
              + 'usa: python, git, pytest.',
            cerca: 'docker_image immagine container',
          },
        ],
      },
    ],
  },
  {
    id: 'anteprime', gruppoNav: 'Ambiente', icona: 'anteprime', titolo: 'Anteprime',
    descr: 'Il pannello che mostra file, pagine e applicazioni scritti dall\'agente.',
    alMostrare: () => refreshSandbox(),
    blocchi: [
      {
        titolo: 'Pannello',
        campi: [
          {
            k: 'preview_enabled', tipo: 'interruttore', etichetta: 'Apri l\'anteprima da sola sui file visuali',
            descr: 'Quando l\'agente scrive un <code>.md</code>, un <code>.html</code>, un <code>.svg</code>, '
              + 'un\'immagine o un PDF. Sui file di codice no: sarebbero dieci anteprime per un refactoring.',
            cerca: 'preview_enabled anteprima automatica',
          },
          {
            k: 'preview_autostart_backend', tipo: 'interruttore', etichetta: 'Avvia il backend del progetto',
            descr: 'Se la cartella della pagina dichiara un backend (Flask, FastAPI, Django, Vite, '
              + '<code>package.json</code>) l\'harness lo avvia prima di mostrarla. Se non risponde, la '
              + 'pagina si vede lo stesso come file statico.',
            cerca: 'preview_autostart_backend backend flask fastapi vite',
          },
        ],
      },
      {
        titolo: 'Applicazioni nel container', quando: (c) => c.docker,
        campi: [
          {
            k: 'preview_ports_enabled', tipo: 'interruttore', etichetta: 'Pubblica porte per le applicazioni',
            badge: ['container'],
            descr: () => {
              const porte = state.sandbox && state.sandbox.preview_ports;
              const quali = porte ? `le porte ${porte[0]}-${porte[1]}` : 'un intervallo di porte';
              return `Il container espone ${quali} su 127.0.0.1, così un server avviato dall'agente si `
                + 'vede nel browser. Solo questa macchina, solo quelle porte.';
            },
            cerca: 'preview_ports_enabled porte pubblica applicazioni',
          },
        ],
      },
      {
        titolo: 'Avanzate', avanzate: true,
        campi: [
          {
            k: 'preview_port_base', tipo: 'numero', etichetta: 'Prima porta', min: 1024, max: 65500, passo: 1,
            badge: ['container'], dipende: 'preview_ports_enabled',
            quando: (c) => c.docker, nascostaPerche: 'Vale solo con i comandi in Docker.',
            cerca: 'preview_port_base porta base',
          },
          {
            k: 'preview_port_count', tipo: 'numero', etichetta: 'Quante porte', min: 1, max: 20, passo: 1,
            badge: ['container'], dipende: 'preview_ports_enabled',
            quando: (c) => c.docker, nascostaPerche: 'Vale solo con i comandi in Docker.',
            cerca: 'preview_port_count numero porte',
          },
          {
            k: 'preview_host_port', tipo: 'numero', etichetta: 'Porta del server delle anteprime',
            min: 0, max: 65500, passo: 1,
            descr: 'Le pagine si servono da un\'origine diversa da quella dell\'harness: possono usare '
              + '<code>localStorage</code>, moduli ES e <code>fetch</code> senza poter leggere le sue '
              + 'rotte. Si accende alla prima anteprima. 0 = spento.',
            cerca: 'preview_host_port porta server anteprime origine',
          },
        ],
      },
    ],
  },
  {
    id: 'aspetto', gruppoNav: 'Applicazione', icona: 'aspetto', titolo: 'Aspetto',
    descr: 'Tema e colonne dell\'interfaccia.',
    blocchi: [
      {
        titolo: 'Interfaccia',
        campi: [
          {
            k: 'theme_mode', tipo: 'segmenti', etichetta: 'Tema', opzioni: [['light', 'Chiaro'], ['dark', 'Scuro']],
            descr: 'Si cambia anche dal pulsante in fondo alla barra laterale.',
            alCambio: (valore) => applyTheme(valore),
            cerca: 'theme_mode tema chiaro scuro dark light',
          },
          {
            k: 'show_left_sidebar', tipo: 'interruttore', etichetta: 'Barra laterale',
            descr: 'Conversazioni e progetti. Si nasconde anche con <b>☰</b> in alto a sinistra.',
            alCambio: () => applicaColonne(),
            cerca: 'show_left_sidebar barra laterale sinistra colonna',
          },
          {
            k: 'show_right_panel', tipo: 'interruttore', etichetta: 'Pannello destro',
            descr: 'Piano, note, consumo e file toccati. Si nasconde anche con <b>☰</b> in alto a destra.',
            alCambio: () => applicaColonne(),
            cerca: 'show_right_panel pannello destro colonna',
          },
        ],
      },
    ],
  },
  {
    id: 'info', gruppoNav: 'Applicazione', icona: 'info', titolo: 'Informazioni',
    descr: 'Versione, stato dell\'installazione, backup e scorciatoie.',
    alMostrare: () => impCaricaDiagnostica(),
    blocchi: [{ speciale: 'info', cerca: 'versione diagnostica esporta importa backup trasferisci scorciatoie tastiera percorsi' }],
  },
];

// Le chiavi di DEFAULTS che non hanno una voce nel menu, e perche'. Non e' un
// elenco di dimenticanze: il test ``test_ogni_impostazione_ha_un_posto``
// pretende che ogni chiave stia o nello schema o qui, cosi' una chiave nuova
// non puo' entrare in DEFAULTS senza che qualcuno decida dove va.
const IMP_FUORI_MENU = {
  stream_tools: 'Serve solo con Ollama precedente alla 0.8.0, e il valore «auto» legge la versione da solo.',
  auto_env_header: 'Toglie al modello l\'albero della cartella di lavoro: spegnerla peggiora sempre l\'agente.',
  expand_thoughts: 'Nessuna parte del codice la legge.',
  workspace_dir: 'Si sceglie dal percorso in cima alla chat.',
  recent_workspaces: 'Si usano dal percorso in cima alla chat.',
  progetti: 'Si gestiscono dalla colonna di sinistra e dalla schermata di ogni progetto.',
  mobile_token: 'La crea l\'avvio con --mobile.',
  agent_running: 'Stato del turno, non una preferenza.',
  pending_prompt: 'Stato del turno, non una preferenza.',
  last_usage: 'Stato del turno, non una preferenza.',
};

// Dopo il salvataggio di una chiave, cosa va ricontrollato. Stava in dieci
// ``addEventListener('change', () => setTimeout(..., 120))`` sparsi nel boot:
// qui partono quando il server ha risposto, non dopo un'attesa indovinata.
const IMP_DOPO = {
  api_base: () => impRicontrollaServer(),
  transport: () => impRicontrollaServer(),
  api_key: () => impRicontrollaServer(),
  timeout_seconds: () => impRicontrollaServer(),
  model_name: () => { refreshProfile(); refreshReadiness(); },
  native_think: () => { refreshProfile(); if (Imp.sezione === 'istruzioni') impCaricaPrompt(); },
  sandbox: () => { refreshSandbox(); refreshReadiness(); },
  docker_image: () => { refreshSandbox(); refreshReadiness(); },
  sandbox_network: () => { refreshSandbox(); refreshReadiness(); },
  preview_ports_enabled: () => refreshSandbox(),
  preview_port_base: () => refreshSandbox(),
  preview_port_count: () => refreshSandbox(),
  system_prompt: () => impCaricaPrompt(),
};

// Tutte le voci, in ordine, con la sezione e il blocco di appartenenza.
const IMP_CAMPI = IMP_SEZIONI.flatMap((sez) => sez.blocchi.flatMap(
  (blocco) => (blocco.campi || []).map((campo) => ({ ...campo, sezione: sez.id, blocco })),
));

// ---------------------------------------------------------------------------
// Stato della finestra
// ---------------------------------------------------------------------------

const Imp = {
  montata: false,
  sezione: 'server',
  defaults: null,
  ricerca: [],
  apertaDa: null,
  prompt: null,
  modoPrompt: null,
  diagnostica: null,
  importazione: null,
  // Chiavi con un salvataggio in volo: la spia in alto dice "Salvataggio…"
  // finche' non tornano tutte.
  inVolo: new Set(),
  // Ridisegni dei controlli disegnati sopra un campo (segmenti, cursori):
  // riportano la vista al valore del campo dopo un riallineamento.
  ridisegni: [],
};

const impId = (k) => 's-' + String(k).replace(/_/g, '-');

function impContesto() {
  const s = state.settings || {};
  const transport = String(s.transport || 'auto');
  const nome = state.backend && state.backend.name;
  const rilevato = IMP_SERVER[nome] ? nome : null;
  return {
    s,
    rilevato,
    // null = non si sa ancora (automatico, sonda non tornata): si mostra tutto.
    server: transport === 'auto' ? rilevato : transport,
    docker: s.sandbox === 'docker',
  };
}

/** La voce (o il blocco) ha senso con il server e le scelte di adesso? */
function impApplicabile(voce, c) {
  if (voce.solo && c.server && !voce.solo.includes(c.server)) return false;
  if (voce.quando && !voce.quando(c)) return false;
  return true;
}

function impTesto(valore, c) {
  return typeof valore === 'function' ? valore(c) : (valore || '');
}

function impNumeroIt(n, cifre = 2) {
  return Number(n).toLocaleString('it-IT', { maximumFractionDigits: cifre });
}

/** Un valore come lo si legge in una frase: "75%", "180 s", "Chiaro", "Sì". */
function impFormatta(campo, valore) {
  if (valore === undefined || valore === null) return '—';
  if (campo && campo.formato === 'percento') return Math.round(Number(valore) * 100) + '%';
  if (campo && campo.tipo === 'interruttore') return valore ? 'Attiva' : 'Spenta';
  if (campo && campo.opzioni) {
    const opzione = campo.opzioni.find(([v]) => String(v) === String(valore));
    if (opzione) return opzione[1];
  }
  if (typeof valore === 'boolean') return valore ? 'Sì' : 'No';
  if (typeof valore === 'number') {
    return impNumeroIt(valore) + (campo && campo.unita ? ' ' + campo.unita : '');
  }
  const testo = String(valore);
  if (!testo) return '(vuoto)';
  return testo.length > 64 ? testo.slice(0, 61) + '…' : testo;
}

/** Riscrive il contenuto di un nodo solo se e' cambiato.
 *
 * Gli stati e il profilo si ridisegnano a ogni riallineamento, cioe' anche per
 * il controllo della goccia ogni venti secondi: riscriverli sempre toglieva il
 * fuoco a chi stava su "Verifica" o su "Applica" con la tastiera, e la
 * selezione a chi stava copiando un messaggio d'errore. Restituisce true se
 * ha riscritto (e i gestori vanno rilegati).
 */
function impAggiornaHtml(nodo, html) {
  if (nodo._html === html) return false;
  nodo.innerHTML = html;
  nodo._html = html;
  return true;
}

function impCampo(k) {
  return IMP_CAMPI.find((campo) => campo.k === k) || null;
}

function impEtichetta(k) {
  const campo = impCampo(k);
  if (campo) return campo.etichetta;
  return k === 'system_prompt' ? 'Prompt di sistema' : k;
}

// ---------------------------------------------------------------------------
// Costruzione
// ---------------------------------------------------------------------------

/** Costruisce il menu dallo schema e lega i campi. Una volta sola, al boot. */
function impMonta() {
  if (Imp.montata) return;
  Imp.montata = true;
  const schede = $('#imp-schede');
  const corpo = $('#imp-corpo');
  let gruppo = null;
  IMP_SEZIONI.forEach((sez) => {
    if (sez.gruppoNav !== gruppo) {
      gruppo = sez.gruppoNav;
      schede.appendChild(el('div', 'imp-nav-gruppo', esc(gruppo)));
    }
    const scheda = el('button', 'imp-scheda');
    scheda.type = 'button';
    scheda.id = 'imp-scheda-' + sez.id;
    scheda.dataset.sezione = sez.id;
    scheda.setAttribute('role', 'tab');
    scheda.setAttribute('aria-selected', 'false');
    scheda.setAttribute('aria-controls', 'imp-pannello-' + sez.id);
    scheda.tabIndex = -1;
    scheda.innerHTML = impIcona(sez.icona)
      + `<span class="imp-scheda-nome">${esc(sez.titolo)}</span>`
      + `<span class="imp-scheda-segno" id="imp-segno-${sez.id}"></span>`;
    scheda.onclick = () => impSeleziona(sez.id, { svuotaRicerca: true });
    schede.appendChild(scheda);

    const pannello = el('div', 'imp-pannello');
    pannello.id = 'imp-pannello-' + sez.id;
    pannello.setAttribute('role', 'tabpanel');
    pannello.setAttribute('aria-labelledby', 'imp-scheda-' + sez.id);
    pannello.hidden = true;
    // Il titolo della sezione dentro il pannello si vede solo cercando: e'
    // l'unico momento in cui piu' sezioni stanno sulla stessa pagina.
    pannello.appendChild(el('div', 'imp-pannello-titolo',
      `${impIcona(sez.icona)}<span>${esc(sez.titolo)}</span>`));
    sez.blocchi.forEach((blocco) => pannello.appendChild(impBlocco(sez, blocco)));
    const piede = el('div', 'imp-pannello-piede');
    piede.id = 'imp-piede-' + sez.id;
    pannello.appendChild(piede);
    corpo.appendChild(pannello);
  });
  const vuoto = el('div', 'imp-vuoto');
  vuoto.id = 'imp-vuoto';
  vuoto.hidden = true;
  corpo.appendChild(vuoto);

  // I campi si legano quando sono nel documento: bindField li cerca con $().
  IMP_CAMPI.forEach((campo) => impLega(campo));
  impLegaPrompt();

  let ricordata = null;
  try { ricordata = localStorage.getItem('ah-imp-sezione'); } catch { /* storage non disponibile */ }
  impSeleziona(IMP_SEZIONI.some((s) => s.id === ricordata) ? ricordata : 'server');
  const app = state.app || {};
  $('#imp-nav-piede').innerHTML = `<button type="button" class="imp-versione" id="imp-versione" `
    + `title="${esc(app.name || 'Local Agent Harness')}: informazioni e diagnostica">`
    + `${impIcona('info')}<span>Versione ${esc(app.version || '')}</span></button>`
    + `<span class="imp-kbd" title="Apre e chiude le impostazioni">${impTastoMod()} ,</span>`;
  $('#imp-versione').onclick = () => impSeleziona('info', { svuotaRicerca: true });
  impCaricaMeta();
  impRiallinea();
}

function impBlocco(sez, blocco) {
  if (blocco.speciale) {
    const nodo = el('div', 'imp-speciale imp-blocco');
    nodo.id = `imp-${blocco.speciale}`;
    nodo.dataset.cerca = `${sez.titolo} ${blocco.cerca || ''}`;
    return nodo;
  }
  const campi = (blocco.campi || []).map((campo) => ({ ...campo, sezione: sez.id, blocco }));
  if (blocco.avanzate) {
    const dettagli = el('details', 'imp-gruppo imp-avanzate imp-blocco');
    const titolo = el('summary', 'imp-avanzate-titolo',
      `${impIcona('freccia', 'imp-avanzate-freccia')}<span>${esc(blocco.titolo)}</span>`
      + `<span class="imp-avanzate-conta"></span>`);
    dettagli.appendChild(titolo);
    if (blocco.nota) dettagli.appendChild(el('p', 'imp-gruppo-nota', esc(blocco.nota)));
    dettagli.appendChild(impCarta(campi));
    dettagli._blocco = blocco;
    return dettagli;
  }
  const gruppo = el('section', 'imp-gruppo imp-blocco');
  gruppo.appendChild(el('h4', 'imp-gruppo-titolo', esc(blocco.titolo)));
  if (blocco.nota) gruppo.appendChild(el('p', 'imp-gruppo-nota', esc(blocco.nota)));
  gruppo.appendChild(impCarta(campi));
  // Il blocco resta attaccato al nodo: il riallineamento ci legge ``solo`` e
  // ``quando`` per nascondere il gruppo intero (il gruppo "Ollama").
  gruppo._blocco = blocco;
  return gruppo;
}

function impCarta(campi) {
  const carta = el('div', 'imp-carta');
  campi.forEach((campo) => carta.appendChild(impRiga(campo)));
  return carta;
}

function impRiga(campo) {
  const id = impId(campo.k);
  const riga = el('div', 'imp-riga');
  riga.dataset.chiave = campo.k;
  riga._campo = campo;
  if (campo.sotto) riga.classList.add('sotto');
  if (campo.pericolo) riga.classList.add('pericolo');

  const testo = el('div', 'imp-riga-testo');
  const intestazione = el('div', 'imp-etichetta-riga');
  // Il pallino "modificato" sta fuori dall'etichetta: e' un segno, non una
  // parola da leggere, e lo screen reader ha gia' il titolo del ripristino.
  intestazione.appendChild(el('span', 'imp-modificato'));
  const etichetta = el('label', 'imp-etichetta');
  etichetta.id = id + '-etichetta';
  if (campo.tipo !== 'segmenti') etichetta.htmlFor = id;
  etichetta.innerHTML = esc(campo.etichetta);
  etichetta.dataset.testo = campo.etichetta;
  intestazione.appendChild(etichetta);
  if (campo.solo) {
    const nomi = campo.solo.map((s) => IMP_SERVER[s]).join(' e ');
    intestazione.appendChild(el('span', 'imp-badge', `Solo ${esc(nomi)}`));
  }
  (campo.badge || []).forEach((nome) => {
    const b = IMP_BADGE[nome];
    const badge = el('span', 'imp-badge' + (b.classe ? ' ' + b.classe : ''), esc(b.testo));
    badge.title = b.titolo;
    intestazione.appendChild(badge);
  });
  if (campo.ripristino !== false) {
    const ripristina = el('button', 'imp-ripristina', impIcona('ripristina'));
    ripristina.type = 'button';
    ripristina.onclick = () => impRipristina([campo.k]);
    intestazione.appendChild(ripristina);
  }
  testo.appendChild(intestazione);
  const descr = el('div', 'imp-descr');
  descr.id = id + '-descr';
  testo.appendChild(descr);
  const nota = el('div', 'imp-riga-nota');
  nota.hidden = true;
  testo.appendChild(nota);

  const controllo = el('div', 'imp-controllo');
  controllo.appendChild(impControllo(campo, id));
  riga.append(testo, controllo);
  return riga;
}

function impControllo(campo, id) {
  const descritto = id + '-descr';
  switch (campo.tipo) {
    case 'interruttore': {
      const input = el('input', 'imp-interruttore');
      input.type = 'checkbox';
      input.id = id;
      input.setAttribute('role', 'switch');
      input.setAttribute('aria-describedby', descritto);
      return input;
    }
    case 'segmenti':
      return impSegmenti(campo, id);
    case 'scelta': {
      const involucro = el('span', 'imp-scelta');
      const select = el('select', 'imp-select');
      select.id = id;
      select.setAttribute('aria-describedby', descritto);
      campo.opzioni.forEach(([valore, testo]) => {
        const opzione = el('option');
        opzione.value = valore;
        opzione.textContent = testo;
        select.appendChild(opzione);
      });
      involucro.appendChild(select);
      return involucro;
    }
    case 'numero': {
      const involucro = el('span', 'imp-numero-box');
      const input = el('input', 'imp-input imp-numero');
      input.type = 'number';
      input.id = id;
      input.inputMode = 'numeric';
      if (campo.min != null) input.min = campo.min;
      if (campo.max != null) input.max = campo.max;
      input.step = campo.passo || 1;
      input.title = `Fra ${impNumeroIt(campo.min)} e ${impNumeroIt(campo.max)}`;
      input.setAttribute('aria-describedby', descritto);
      involucro.appendChild(input);
      if (campo.unita) involucro.appendChild(el('span', 'imp-unita', esc(campo.unita)));
      return involucro;
    }
    case 'cursore': {
      const involucro = el('span', 'imp-cursore');
      const input = el('input', 'imp-range');
      input.type = 'range';
      input.id = id;
      input.min = campo.min;
      input.max = campo.max;
      input.step = campo.passo;
      input.setAttribute('aria-describedby', descritto);
      const eco = el('output', 'imp-eco');
      eco.id = id + '-eco';
      eco.htmlFor = id;
      const mostra = (valore) => {
        const testo = impFormatta(campo, Number(valore));
        eco.textContent = testo;
        input.setAttribute('aria-valuetext', testo);
      };
      // Il numero accanto al cursore segue il trascinamento: il campo ha il
      // fuoco, e il riallineamento generale lo salta apposta.
      input.addEventListener('input', () => mostra(input.value));
      // Da fermo si mostra il valore salvato, non quello del cursore: un valore
      // importato o scritto a mano puo' non stare sulla griglia del passo, e il
      // cursore lo arrotonda senza dirlo (0,33 diventerebbe 0,35).
      Imp.ridisegni.push(() => {
        if (document.activeElement !== input) mostra(state.settings[campo.k] ?? input.value);
      });
      involucro.append(input, eco);
      return involucro;
    }
    case 'modello': {
      const involucro = el('span', 'imp-testo-box');
      const input = el('input', 'imp-input imp-largo');
      input.type = 'text';
      input.id = id;
      input.setAttribute('list', 's-model-list');
      input.autocomplete = 'off';
      input.spellcheck = false;
      input.placeholder = campo.segnaposto || '';
      input.setAttribute('aria-describedby', descritto);
      const elenco = el('datalist');
      elenco.id = 's-model-list';
      involucro.append(input, elenco);
      return involucro;
    }
    case 'segreto': {
      const involucro = el('span', 'imp-testo-box imp-segreto');
      // NON type="password": il gestore password di Chrome tratterebbe il
      // campo come una credenziale -- offrirebbe di salvarlo, lo
      // autocompilerebbe, e mostrerebbe l'avviso "password compromessa".
      // Si maschera in CSS (``masked``): stesso aspetto, nessun gestore.
      const input = el('input', 'imp-input imp-largo imp-mono masked');
      input.type = 'text';
      input.id = id;
      input.autocomplete = 'off';
      input.spellcheck = false;
      input.setAttribute('autocapitalize', 'off');
      input.setAttribute('data-lpignore', 'true');
      input.setAttribute('data-1p-ignore', '');
      input.setAttribute('data-bwignore', '');
      input.setAttribute('data-form-type', 'other');
      input.setAttribute('aria-describedby', descritto);
      // `-webkit-text-security` in Firefox non esiste: li' il campo
      // *sembrava* mascherato e mostrava la chiave in chiaro, che e' peggio di
      // un campo dichiaratamente visibile. Dove la proprieta' manca si torna a
      // type="password", che maschera davvero: l'unico motivo per evitarlo e'
      // il gestore password di Chrome, e in Chrome questo ramo non gira.
      const senzaMaschera = !(window.CSS && CSS.supports && CSS.supports('-webkit-text-security', 'disc'));
      if (senzaMaschera) input.type = 'password';
      const occhio = el('button', 'imp-occhio', impIcona('occhio'));
      occhio.type = 'button';
      occhio.title = 'Mostra la chiave';
      occhio.setAttribute('aria-label', 'Mostra la chiave');
      occhio.setAttribute('aria-pressed', 'false');
      occhio.onclick = () => {
        const visibile = occhio.getAttribute('aria-pressed') !== 'true';
        occhio.setAttribute('aria-pressed', String(visibile));
        occhio.title = visibile ? 'Nascondi la chiave' : 'Mostra la chiave';
        occhio.setAttribute('aria-label', occhio.title);
        occhio.innerHTML = impIcona(visibile ? 'nascondi' : 'occhio');
        input.classList.toggle('masked', !visibile);
        if (senzaMaschera) input.type = visibile ? 'text' : 'password';
      };
      involucro.append(input, occhio);
      return involucro;
    }
    default: {
      const involucro = el('span', 'imp-testo-box');
      const input = el('input', 'imp-input' + (campo.largo ? ' imp-largo' : '') + (campo.mono ? ' imp-mono' : ''));
      input.type = 'text';
      input.id = id;
      input.autocomplete = 'off';
      input.spellcheck = false;
      input.placeholder = campo.segnaposto || '';
      input.setAttribute('aria-describedby', descritto);
      involucro.appendChild(input);
      return involucro;
    }
  }
}

/** Controllo a segmenti: bottoni visibili sopra una <select> nascosta.
 *
 * La <select> e' il campo vero, quello che bindField lega e che applyField
 * riallinea (aggiungendo l'opzione se il valore salvato non e' fra quelle
 * dello schema, come "regole" per la compattazione selettiva). I bottoni si
 * ridisegnano da lei, quindi non possono mostrare un valore diverso.
 */
function impSegmenti(campo, id) {
  const gruppo = el('div', 'imp-segmenti');
  gruppo.setAttribute('role', 'radiogroup');
  gruppo.setAttribute('aria-labelledby', id + '-etichetta');
  gruppo.setAttribute('aria-describedby', id + '-descr');
  const select = el('select', 'imp-nascosto');
  select.id = id;
  select.hidden = true;
  select.tabIndex = -1;
  select.setAttribute('aria-hidden', 'true');
  campo.opzioni.forEach(([valore, testo]) => {
    const opzione = el('option');
    opzione.value = valore;
    opzione.textContent = testo;
    select.appendChild(opzione);
  });
  gruppo.appendChild(select);
  const scegli = (valore) => {
    if (select.value === valore || select.disabled) return;
    select.value = valore;
    select.dispatchEvent(new Event('change'));
    disegna();
  };
  // I bottoni si aggiornano sul posto e si ricreano solo se cambiano le
  // opzioni (applyField ne aggiunge una per un valore fuori elenco). Ricrearli
  // a ogni riallineamento toglieva il fuoco a chi li usa con la tastiera: il
  // salvataggio, tornando dal server, riallinea tutto.
  const disegna = () => {
    const opzioni = Array.from(select.options);
    let bottoni = $$('.imp-segmento', gruppo);
    const uguali = bottoni.length === opzioni.length
      && bottoni.every((b, i) => b.dataset.valore === opzioni[i].value);
    if (!uguali) {
      bottoni.forEach((b) => b.remove());
      bottoni = opzioni.map((opzione) => {
        const bottone = el('button', 'imp-segmento', esc(opzione.textContent));
        bottone.type = 'button';
        bottone.dataset.valore = opzione.value;
        bottone.setAttribute('role', 'radio');
        bottone.onclick = () => scegli(opzione.value);
        gruppo.appendChild(bottone);
        return bottone;
      });
    }
    bottoni.forEach((bottone) => {
      const scelto = bottone.dataset.valore === select.value;
      bottone.setAttribute('aria-checked', String(scelto));
      bottone.tabIndex = scelto ? 0 : -1;
      bottone.disabled = select.disabled;
    });
  };
  // Frecce come in un gruppo di radio: spostano la scelta, non solo il fuoco.
  gruppo.addEventListener('keydown', (event) => {
    const passi = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };
    if (!(event.key in passi)) return;
    event.preventDefault();
    const valori = Array.from(select.options).map((o) => o.value);
    const i = valori.indexOf(select.value);
    const prossimo = valori[(i + passi[event.key] + valori.length) % valori.length];
    scegli(prossimo);
    $(`.imp-segmento[data-valore="${CSS.escape(prossimo)}"]`, gruppo)?.focus();
  });
  Imp.ridisegni.push(disegna);
  return gruppo;
}

/** Il valore grezzo di un campo numerico, validato. ``undefined`` = non salvare. */
function impNumero(campo, grezzo) {
  const testo = String(grezzo ?? '').trim().replace(',', '.');
  if (testo === '') return undefined;
  const numero = Number(testo);
  if (!Number.isFinite(numero)) return undefined;
  if (campo.min != null && numero < campo.min) return undefined;
  if (campo.max != null && numero > campo.max) return undefined;
  // Il server vuole il tipo del default: un intero per num_ctx, anche se
  // l'utente ha scritto "4096.0". Il passo decide quale dei due e' la voce.
  return Number.isInteger(campo.passo ?? 1) ? Math.round(numero) : numero;
}

function impTrasforma(campo) {
  if (campo.tipo === 'numero' || campo.tipo === 'cursore') return (grezzo) => impNumero(campo, grezzo);
  if (campo.numerico) return Number;
  return (valore) => valore;
}

// Si salva quando si smette di scrivere. Sull'indirizzo del server ogni
// salvataggio ricostruisce il backend e rifa' i controlli di prontezza: a ogni
// tasto erano una raffica di sonde verso host che non esistevano ancora.
const IMP_RITARDO = { testo: 700, modello: 700, segreto: 700, numero: 600, cursore: 250 };

function impLega(campo) {
  const id = impId(campo.k);
  const nodo = $('#' + id);
  if (!nodo) return;
  if (campo.tipo === 'numero') {
    // Uscendo dal campo: un numero fuori dai limiti si porta al limite, un
    // campo vuoto torna al valore salvato. Registrato PRIMA di bindField, cosi'
    // il salvataggio del ``change`` legge il valore gia' corretto.
    nodo.addEventListener('change', () => {
      const testo = String(nodo.value).trim().replace(',', '.');
      const numero = Number(testo);
      if (testo === '' || !Number.isFinite(numero)) {
        nodo.value = String(state.settings[campo.k] ?? '');
        return;
      }
      const limitato = Math.min(campo.max ?? Infinity, Math.max(campo.min ?? -Infinity, numero));
      nodo.value = String(Number.isInteger(campo.passo ?? 1) ? Math.round(limitato) : limitato);
    });
  }
  bindField('#' + id, campo.k, impTrasforma(campo), { ritardo: IMP_RITARDO[campo.tipo] || 0 });
  if (campo.alCambio) {
    // Dopo bindField: il suo gestore salva per primo, poi si applica.
    nodo.addEventListener('change', () => {
      campo.alCambio(nodo.type === 'checkbox' ? nodo.checked : nodo.value);
    });
  }
}

// ---------------------------------------------------------------------------
// Riallineamento
// ---------------------------------------------------------------------------

/** Riporta la finestra allo stato delle impostazioni.
 *
 * La chiama syncSettingsWidgets, cioe' ogni percorso che cambia
 * un'impostazione: la goccia del modello, il tema dal pulsante in basso,
 * "Applica i consigliati", un'importazione. Costa poco (una cinquantina di
 * righe) ed e' l'unico posto che decide cosa si vede.
 */
function impRiallinea() {
  if (!Imp.montata) return;
  const c = impContesto();
  Imp.ridisegni.forEach((ridisegna) => ridisegna());
  $$('#imp-corpo .imp-riga').forEach((riga) => impRiallineaRiga(riga, c));
  $$('#imp-corpo .imp-blocco').forEach((blocco) => {
    const b = blocco._blocco;
    blocco.classList.toggle('imp-inapplicabile', !!(b && !impApplicabile(b, c)));
  });
  $$('#imp-corpo details.imp-avanzate').forEach((dettagli) => {
    const righe = $$('.imp-riga', dettagli).filter((r) => !r.classList.contains('imp-inapplicabile'));
    const cambiate = righe.filter((r) => r.classList.contains('modificata')).length;
    $('.imp-avanzate-conta', dettagli).textContent = `${righe.length} ${righe.length === 1 ? 'voce' : 'voci'}`
      + (cambiate ? ` · ${cambiate} ${cambiate === 1 ? 'cambiata' : 'cambiate'}` : '');
  });
  IMP_SEZIONI.forEach((sez) => impPiedeSezione(sez));
  impDisegnaStati();
  if (Imp.ricerca.length) impApplicaRicerca();
}

/** Perche' una voce non conta con le scelte di adesso, o '' se conta. */
function impRagioneInapplicabile(campo, c) {
  for (const voce of [campo, campo.blocco]) {
    if (!voce) continue;
    if (voce.solo && c.server && !voce.solo.includes(c.server)) {
      return `Il server in uso è ${IMP_SERVER[c.server]}: questa voce non gli arriva.`;
    }
    if (voce.quando && !voce.quando(c)) return voce.nascostaPerche || 'Non conta con le scelte di adesso.';
  }
  return '';
}

function impRiallineaRiga(riga, c) {
  const campo = riga._campo;
  const nodo = $('#' + impId(campo.k));
  const ragione = impRagioneInapplicabile(campo, c);
  const applicabile = !ragione;
  // Anche il blocco intero puo' non contare (il gruppo "Ollama" con un altro
  // server): la riga resta del blocco, ma cercando dice perche'.
  riga.classList.toggle('imp-inapplicabile', !impApplicabile(campo, c));

  impAggiornaHtml($('.imp-descr', riga), impTesto(campo.descr, c));

  // Voci che dipendono da un interruttore spento: restano dove sono, ma
  // disattivate e con la ragione scritta sotto.
  const padre = campo.dipende ? impCampo(campo.dipende) : null;
  const bloccata = !!(padre && !state.settings[campo.dipende]);
  riga.classList.toggle('disattivata', bloccata);
  if (nodo) nodo.disabled = bloccata;
  $$('.imp-segmento', riga).forEach((b) => { b.disabled = bloccata; });
  const nota = $('.imp-riga-nota', riga);
  let testoNota = '';
  if (!applicabile) testoNota = ragione;
  else if (bloccata) testoNota = `Conta solo con «${padre.etichetta}» attiva.`;
  nota.textContent = testoNota;
  nota.hidden = !testoNota;

  const defaults = Imp.defaults;
  const ripristina = $('.imp-ripristina', riga);
  const modificata = !!(defaults && campo.ripristino !== false && campo.k in defaults
    && !impUguali(state.settings[campo.k], defaults[campo.k]));
  riga.classList.toggle('modificata', modificata);
  if (ripristina) {
    const titolo = defaults && campo.k in defaults
      ? `Torna al valore di serie: ${impFormatta(campo, defaults[campo.k])}`
      : 'Torna al valore di serie';
    ripristina.title = titolo;
    ripristina.setAttribute('aria-label', `${campo.etichetta}: ${titolo.toLowerCase()}`);
    ripristina.hidden = !modificata;
  }
}

function impUguali(a, b) {
  if (typeof a === 'number' || typeof b === 'number') return Number(a) === Number(b);
  return JSON.stringify(a) === JSON.stringify(b);
}

/** Le voci cambiate di una sezione che si possono rimettere come di serie. */
function impCambiate(sezioneId) {
  if (!Imp.defaults) return [];
  const c = impContesto();
  return IMP_CAMPI.filter((campo) => campo.sezione === sezioneId
    && campo.ripristino !== false
    && campo.k in Imp.defaults
    && impApplicabile(campo, c)
    && (!campo.blocco || impApplicabile(campo.blocco, c))
    && !impUguali(state.settings[campo.k], Imp.defaults[campo.k]));
}

function impPiedeSezione(sez) {
  const piede = $('#imp-piede-' + sez.id);
  if (!piede) return;
  const cambiate = impCambiate(sez.id);
  if (!cambiate.length) {
    impAggiornaHtml(piede, '');
    piede.hidden = true;
    return;
  }
  piede.hidden = false;
  if (piede.dataset.conferma === '1') return;
  const html = `<button type="button" class="imp-link">${impIcona('ripristina')}<span>Ripristina i valori `
    + `di serie di questa sezione (${cambiate.length})</span></button>`;
  if (impAggiornaHtml(piede, html)) {
    $('.imp-link', piede).onclick = () => impChiediRipristinoSezione(sez, piede);
  }
}

function impChiediRipristinoSezione(sez, piede) {
  const cambiate = impCambiate(sez.id);
  if (!cambiate.length) return;
  piede.dataset.conferma = '1';
  piede.innerHTML = '';
  piede._html = null;
  const elenco = cambiate.map((campo) => `${campo.etichetta} → ${impFormatta(campo, Imp.defaults[campo.k])}`);
  const testo = el('div', 'imp-conferma-testo',
    `<b>Torneranno come di serie:</b> ${esc(elenco.join(' · '))}`);
  const conferma = el('button', 'btn primary', 'Ripristina');
  conferma.type = 'button';
  const annulla = el('button', 'btn', 'Annulla');
  annulla.type = 'button';
  const chiudi = () => { delete piede.dataset.conferma; impPiedeSezione(sez); };
  annulla.onclick = chiudi;
  conferma.onclick = async () => {
    delete piede.dataset.conferma;
    await impRipristina(cambiate.map((campo) => campo.k));
    impPiedeSezione(sez);
  };
  const bottoni = el('div', 'imp-conferma-bottoni');
  bottoni.append(annulla, conferma);
  piede.append(testo, bottoni);
  conferma.focus();
}

async function impRipristina(chiavi) {
  if (!Imp.defaults) return;
  const patch = {};
  chiavi.forEach((k) => { if (k in Imp.defaults) patch[k] = Imp.defaults[k]; });
  if (!Object.keys(patch).length) return;
  const salvataggio = saveSettings(patch);
  impSegnaSalvataggio(Object.keys(patch), salvataggio);
  try {
    await salvataggio;
    if ('theme_mode' in patch) applyTheme(state.settings.theme_mode);
    if ('show_left_sidebar' in patch || 'show_right_panel' in patch) applicaColonne();
    toast(chiavi.length === 1
      ? `${impEtichetta(chiavi[0])}: di nuovo come di serie.`
      : `${chiavi.length} impostazioni di nuovo come di serie.`);
  } catch (error) {
    toast(error.message);
  }
}

// ---------------------------------------------------------------------------
// Salvataggio: la spia in alto
// ---------------------------------------------------------------------------

/** Segue un salvataggio: spia "Salvataggio…/Salvato", poi i ricontrolli. */
function impSegnaSalvataggio(chiavi, salvataggio) {
  chiavi.forEach((k) => Imp.inVolo.add(k));
  impSpia('in-corso');
  salvataggio.then(() => {
    chiavi.forEach((k) => Imp.inVolo.delete(k));
    if (!Imp.inVolo.size) impSpia('ok');
    chiavi.forEach((k) => { if (IMP_DOPO[k]) IMP_DOPO[k](); });
  }, (error) => {
    chiavi.forEach((k) => Imp.inVolo.delete(k));
    impSpia('errore', error && error.message);
  });
}

function impSpia(stato, dettaglio = '') {
  const spia = $('#imp-salvataggio');
  if (!spia) return;
  clearTimeout(impSpia._t);
  spia.className = 'imp-salvataggio ' + stato;
  spia.title = dettaglio;
  if (stato === 'in-corso') spia.innerHTML = '<span class="imp-rotella"></span>Salvataggio…';
  else if (stato === 'ok') spia.innerHTML = `${impIcona('spunta')}Salvato`;
  else if (stato === 'errore') spia.innerHTML = `${impIcona('avviso')}Non salvato`;
  else spia.innerHTML = '';
  if (stato === 'ok') impSpia._t = setTimeout(() => impSpia(''), 1800);
}

// bindField (app.js) annuncia ogni salvataggio che parte da un campo legato.
document.addEventListener('impostazioni:salvataggio', (event) => {
  const { key, salvataggio } = event.detail || {};
  if (key && salvataggio) impSegnaSalvataggio([key], salvataggio);
});

// ---------------------------------------------------------------------------
// Apertura, sezioni, tastiera
// ---------------------------------------------------------------------------

const impTastoMod = () => (/Mac|iPhone|iPad/.test(navigator.platform || '') ? '⌘' : 'Ctrl');

function impAperta() {
  const velo = $('#impostazioni');
  return !!velo && !velo.hidden;
}

/** Apre le impostazioni, su una sezione se detto. */
function impApri(sezione) {
  if (!Imp.montata) {
    if (!state.settings || !Object.keys(state.settings).length) return;
    impMonta();
  }
  const velo = $('#impostazioni');
  if (!impAperta()) {
    Imp.apertaDa = document.activeElement;
    velo.hidden = false;
    document.body.classList.add('imp-aperte');
  }
  if (sezione) impSeleziona(sezione, { svuotaRicerca: true });
  else {
    const corrente = IMP_SEZIONI.find((s) => s.id === Imp.sezione);
    if (corrente && corrente.alMostrare) corrente.alMostrare();
  }
  impRiallinea();
  // Il fuoco va sulla ricerca: e' il modo piu' corto per arrivare a una voce,
  // e da li' Tab porta alle sezioni.
  requestAnimationFrame(() => $('#imp-cerca')?.focus());
}

function impChiudi() {
  if (!impAperta()) return;
  $('#impostazioni').hidden = true;
  document.body.classList.remove('imp-aperte');
  if ($('#imp-cerca').value) {
    $('#imp-cerca').value = '';
    impCerca('');
  }
  Imp.importazione = null;
  const prima = Imp.apertaDa;
  Imp.apertaDa = null;
  if (prima && typeof prima.focus === 'function' && document.contains(prima)) prima.focus();
}

function impSeleziona(id, { svuotaRicerca = false, fuoco = false } = {}) {
  const sez = IMP_SEZIONI.find((s) => s.id === id);
  if (!sez) return;
  if (svuotaRicerca && $('#imp-cerca') && $('#imp-cerca').value) {
    $('#imp-cerca').value = '';
    impCerca('');
  }
  Imp.sezione = id;
  try { localStorage.setItem('ah-imp-sezione', id); } catch { /* storage non disponibile */ }
  $$('.imp-scheda').forEach((scheda) => {
    const scelta = scheda.dataset.sezione === id;
    scheda.setAttribute('aria-selected', String(scelta));
    scheda.tabIndex = scelta ? 0 : -1;
    if (scelta && fuoco) scheda.focus();
  });
  if (!Imp.ricerca.length) {
    $$('.imp-pannello').forEach((p) => { p.hidden = p.id !== 'imp-pannello-' + id; });
    $('#imp-sezione-titolo').textContent = sez.titolo;
    $('#imp-sezione-descr').textContent = sez.descr;
    $('#imp-corpo').scrollTop = 0;
  }
  if (sez.alMostrare && impAperta()) sez.alMostrare();
}

/** Tastiera dentro la finestra: Esc, "/", frecce nella barra, Tab che resta dentro. */
function impTastiera(event) {
  if (event.key === 'Escape') {
    // Esc non deve arrivare al documento: la' chiuderebbe anche l'anteprima
    // e le tendine che stanno sotto la finestra.
    event.stopPropagation();
    event.preventDefault();
    const cerca = $('#imp-cerca');
    if (cerca.value) {
      cerca.value = '';
      impCerca('');
      cerca.focus();
    } else {
      impChiudi();
    }
    return;
  }
  const scrivendo = event.target.closest('input, textarea, select, [contenteditable="true"]');
  if (event.key === '/' && !scrivendo) {
    event.preventDefault();
    $('#imp-cerca').focus();
    $('#imp-cerca').select();
    return;
  }
  if (event.target.classList.contains('imp-scheda')) {
    const schede = $$('.imp-scheda');
    const i = schede.indexOf(event.target);
    let prossima = null;
    if (event.key === 'ArrowDown') prossima = schede[(i + 1) % schede.length];
    else if (event.key === 'ArrowUp') prossima = schede[(i - 1 + schede.length) % schede.length];
    else if (event.key === 'Home') prossima = schede[0];
    else if (event.key === 'End') prossima = schede[schede.length - 1];
    if (prossima) {
      event.preventDefault();
      impSeleziona(prossima.dataset.sezione, { svuotaRicerca: true, fuoco: true });
    }
    return;
  }
  if (event.key === 'Tab') {
    // Il fuoco resta nella finestra: dietro c'e' una pagina che non si vede.
    const raggiungibili = $$('button, [href], input, select, textarea, summary, [tabindex]:not([tabindex="-1"])',
      $('.imp-finestra')).filter((n) => !n.disabled && n.tabIndex !== -1 && n.offsetParent !== null);
    if (!raggiungibili.length) return;
    const primo = raggiungibili[0];
    const ultimo = raggiungibili[raggiungibili.length - 1];
    if (event.shiftKey && document.activeElement === primo) { event.preventDefault(); ultimo.focus(); }
    else if (!event.shiftKey && document.activeElement === ultimo) { event.preventDefault(); primo.focus(); }
  }
}

/** Collega bottoni e scorciatoie. Gira prima del boot: la finestra si monta dopo. */
function impCollega() {
  $('#open-settings').onclick = () => impApri();
  $('#imp-chiudi').onclick = () => impChiudi();
  const velo = $('#impostazioni');
  // Un clic sul velo chiude, un trascinamento che *finisce* sul velo no: chi
  // seleziona il testo di un campo e rilascia fuori non vuole perdere tutto.
  let premutoSulVelo = false;
  velo.addEventListener('mousedown', (event) => { premutoSulVelo = event.target === velo; });
  velo.addEventListener('click', (event) => {
    if (premutoSulVelo && event.target === velo) impChiudi();
    premutoSulVelo = false;
  });
  velo.addEventListener('keydown', impTastiera);
  const cerca = $('#imp-cerca');
  cerca.addEventListener('input', () => impCerca(cerca.value));
  $('#imp-cerca-svuota').onclick = () => {
    cerca.value = '';
    impCerca('');
    cerca.focus();
  };
  cerca.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      const primo = $$('#imp-corpo .imp-riga:not(.imp-esclusa)').find((r) => r.offsetParent !== null);
      if (primo) $('input:not([hidden]), select:not([hidden]), .imp-segmento[tabindex="0"]', primo)?.focus();
    } else if (event.key === 'ArrowDown') {
      event.preventDefault();
      $('.imp-scheda[aria-selected="true"]')?.focus();
    }
  });
  document.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key === ',') {
      event.preventDefault();
      if (impAperta()) impChiudi();
      else impApri();
    }
  });
}

// ---------------------------------------------------------------------------
// Ricerca
// ---------------------------------------------------------------------------

function impNormalizza(testo) {
  return String(testo || '').toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '');
}

/** Le parole di un testo, normalizzate. Le chiavi valgono intere e a pezzi:
 *  ``num_ctx`` si trova con "num_ctx", con "num" e con "ctx". */
function impParole(testo) {
  const parole = new Set();
  impNormalizza(testo).split(/[^a-z0-9_.]+/).forEach((parola) => {
    if (!parola) return;
    parole.add(parola);
    parola.split(/[_.]+/).forEach((pezzo) => { if (pezzo) parole.add(pezzo); });
  });
  return [...parole];
}

/** La radice di un termine cercato.
 *
 * Si cerca all'inizio delle parole, non dentro: "porta" dentro
 * "comportamento" o "importa" trovava mezzo menu. E l'italiano cambia la
 * vocale finale fra singolare e plurale (porta/porte, memoria/memorie):
 * dalla quinta lettera in su l'ultima non conta.
 */
function impRadice(termine) {
  return termine.length >= 5 ? termine.slice(0, -1) : termine;
}

function impTrova(parole, termini) {
  return termini.every((t) => {
    const radice = impRadice(t);
    return parole.some((p) => p.startsWith(radice));
  });
}

function impCerca(testo) {
  Imp.ricerca = impNormalizza(testo).split(/\s+/).filter(Boolean);
  // Chiunque svuoti la ricerca (Esc, un clic su una sezione, la chiusura)
  // passa di qui: il pulsante per svuotarla segue il testo.
  const svuota = $('#imp-cerca-svuota');
  if (svuota) svuota.hidden = !testo;
  const finestra = $('.imp-finestra');
  if (!Imp.ricerca.length) {
    finestra.classList.remove('imp-in-ricerca');
    $$('#imp-corpo .imp-esclusa').forEach((n) => n.classList.remove('imp-esclusa'));
    $$('#imp-corpo .imp-etichetta').forEach((e) => { e.innerHTML = esc(e.dataset.testo); });
    $$('.imp-scheda-conta').forEach((n) => n.remove());
    $$('.imp-scheda').forEach((s) => s.classList.remove('spenta'));
    $('#imp-vuoto').hidden = true;
    impSeleziona(Imp.sezione);
    return;
  }
  finestra.classList.add('imp-in-ricerca');
  impApplicaRicerca();
}

function impApplicaRicerca() {
  const termini = Imp.ricerca;
  let totale = 0;
  IMP_SEZIONI.forEach((sez) => {
    const pannello = $('#imp-pannello-' + sez.id);
    let trovate = 0;
    // Il nome della sezione vale per tutte le sue voci: "sandbox" deve
    // mostrare la sandbox intera, non le sole righe che ripetono la parola.
    $$('.imp-blocco', pannello).forEach((blocco) => {
      if (blocco.classList.contains('imp-speciale')) {
        const ok = impTrova(impParole(blocco.dataset.cerca), termini);
        blocco.classList.toggle('imp-esclusa', !ok);
        if (ok) trovate += 1;
        return;
      }
      let nelBlocco = 0;
      $$('.imp-riga', blocco).forEach((riga) => {
        const campo = riga._campo;
        const parole = impParole([sez.titolo, campo.blocco.titolo, campo.etichetta,
          riga.querySelector('.imp-descr').textContent, campo.k, campo.cerca || ''].join(' '));
        const ok = impTrova(parole, termini);
        riga.classList.toggle('imp-esclusa', !ok);
        const etichetta = $('.imp-etichetta', riga);
        etichetta.innerHTML = ok ? impEvidenzia(etichetta.dataset.testo, termini) : esc(etichetta.dataset.testo);
        if (ok) nelBlocco += 1;
      });
      blocco.classList.toggle('imp-esclusa', !nelBlocco);
      if (nelBlocco && blocco.tagName === 'DETAILS') blocco.open = true;
      trovate += nelBlocco;
    });
    pannello.hidden = !trovate;
    // Il separatore fra sezioni va sopra tutte tranne la prima che si vede:
    // il selettore "+" del CSS conterebbe anche i pannelli nascosti.
    pannello.classList.toggle('imp-primo', !!trovate && !totale);
    totale += trovate;
    const scheda = $('#imp-scheda-' + sez.id);
    scheda.classList.toggle('spenta', !trovate);
    $('.imp-scheda-conta', scheda)?.remove();
    if (trovate) scheda.appendChild(el('span', 'imp-scheda-conta', String(trovate)));
  });
  const grezzo = $('#imp-cerca').value.trim();
  $('#imp-sezione-titolo').textContent = totale
    ? `${totale} ${totale === 1 ? 'risultato' : 'risultati'}`
    : 'Nessun risultato';
  $('#imp-sezione-descr').textContent = `per «${grezzo}»`;
  const vuoto = $('#imp-vuoto');
  vuoto.hidden = !!totale;
  if (!totale) {
    vuoto.innerHTML = `${impIcona('cerca')}<div><b>Nessuna impostazione per «${esc(grezzo)}»</b></div>`
      + '<div>Prova con un nome tecnico (<code>num_ctx</code>, <code>top_k</code>) o con una parola '
      + 'più corta.</div>';
  }
}

function impEvidenzia(testo, termini) {
  const normale = impNormalizza(testo);
  // Normalizzare toglie gli accenti senza cambiare la lunghezza delle lettere
  // italiane (una lettera, un segno che sparisce): gli indici restano validi.
  if (normale.length !== testo.length) return esc(testo);
  const segni = new Array(testo.length).fill(false);
  // Si evidenzia la parola intera che comincia con la radice cercata: e'
  // quella che la ricerca ha trovato.
  const parola = /[a-z0-9]+/g;
  let trovata = parola.exec(normale);
  while (trovata) {
    const [intera] = trovata;
    if (termini.some((t) => intera.startsWith(impRadice(t)))) {
      for (let i = trovata.index; i < trovata.index + intera.length; i += 1) segni[i] = true;
    }
    trovata = parola.exec(normale);
  }
  let html = '';
  let aperto = false;
  for (let i = 0; i < testo.length; i += 1) {
    if (segni[i] && !aperto) { html += '<mark>'; aperto = true; }
    if (!segni[i] && aperto) { html += '</mark>'; aperto = false; }
    html += esc(testo[i]);
  }
  return html + (aperto ? '</mark>' : '');
}

// ---------------------------------------------------------------------------
// Dati dal server
// ---------------------------------------------------------------------------

async function impCaricaMeta() {
  try {
    const meta = await api('/api/settings/meta');
    Imp.defaults = meta.defaults || {};
    Imp.fuoriDalloScambio = meta.fuori_dallo_scambio || {};
    impRiallinea();
  } catch { /* senza valori di serie mancano solo il pallino e il ripristino */ }
}

async function impRicontrollaServer() {
  impDisegnaStatoServer(true);
  await sondaBackend();
  refreshProfile();
  refreshReadiness();
}

// ---------------------------------------------------------------------------
// Stati: server, sandbox, profilo, segni nella barra
// ---------------------------------------------------------------------------

function impDisegnaStati() {
  if (!Imp.montata) return;
  impDisegnaStatoServer();
  impDisegnaStatoSandbox();
  impDisegnaProfilo();
  const b = state.backend || {};
  const segnoServer = $('#imp-segno-server');
  segnoServer.className = 'imp-scheda-segno' + (b.online === false ? ' err' : '');
  segnoServer.title = b.online === false ? 'Il server del modello non risponde' : '';
  const sb = state.sandbox;
  const segnoSandbox = $('#imp-segno-sandbox');
  const problema = sb && (sb.mode !== 'docker' || !sb.available);
  segnoSandbox.className = 'imp-scheda-segno' + (problema ? ' warn' : '');
  segnoSandbox.title = problema ? (sb.mode !== 'docker' ? 'Comandi senza recinto' : 'Docker non raggiungibile') : '';
  const segnoMemorie = $('#imp-segno-memorie');
  const quante = (state.memories || []).length;
  segnoMemorie.className = 'imp-scheda-segno' + (quante ? ' numero' : '');
  segnoMemorie.textContent = quante ? String(quante) : '';
}

function impDisegnaStatoServer(controllando = false) {
  const nodo = $('#imp-stato-server');
  if (!nodo) return;
  const b = state.backend || {};
  const online = controllando ? null : b.online;
  const stato = online == null ? 'wait' : (online ? 'ok' : 'err');
  const titolo = online == null ? 'Controllo il server…' : (online ? 'Server raggiungibile' : 'Server non raggiungibile');
  const nome = IMP_SERVER[b.name] || b.name || '';
  const pezzi = [];
  if (nome && online != null) pezzi.push(esc(nome) + (b.version ? ' ' + esc(b.version) : ''));
  // L'indirizzo come l'ha scritto l'utente: quello normalizzato perde il
  // ``/v1`` e non si riconosce piu'.
  const indirizzo = (state.settings && state.settings.api_base) || b.url;
  if (indirizzo) pezzi.push(`<span class="imp-mono">${esc(indirizzo)}</span>`);
  if (online && Array.isArray(b.models)) {
    pezzi.push(`${b.models.length} ${b.models.length === 1 ? 'modello' : 'modelli'}`);
  }
  const dettaglio = online === false && b.detail
    ? `<div class="imp-stato-errore imp-mono">${esc(b.detail)}</div>` : '';
  nodo.className = 'imp-speciale imp-blocco imp-stato ' + stato
    + (nodo.classList.contains('imp-esclusa') ? ' imp-esclusa' : '');
  impAggiornaHtml(nodo, `<span class="imp-stato-punto" aria-hidden="true"></span>`
    + `<div class="imp-stato-testo"><div class="imp-stato-titolo">${titolo}</div>`
    + `<div class="imp-stato-dett">${pezzi.join('<span class="imp-sep">·</span>')}</div>${dettaglio}</div>`
    + `<button type="button" class="btn imp-stato-azione">${impIcona('aggiorna')}Verifica</button>`);
  const bottone = $('.imp-stato-azione', nodo);
  bottone.disabled = online == null;
  bottone.onclick = () => impRicontrollaServer();
}

/** Cosa dire della sandbox: una frase per ogni stato che chiede un gesto diverso. */
function impTestoSandbox(info) {
  if (!info) return { stato: 'wait', titolo: 'Controllo la sandbox…', dettaglio: '' };
  if (info.mode !== 'docker') {
    return {
      stato: 'warn', titolo: 'Nessun recinto',
      dettaglio: 'I comandi girano sulla tua macchina e la cartella di lavoro non li contiene: '
        + 'con <code>cd ..</code> l\'agente è fuori.',
    };
  }
  if (!info.available) {
    return {
      stato: 'err', titolo: 'Docker non raggiungibile',
      dettaglio: `${esc(info.detail || '')}. Finché non riparte, <code>run_command</code> rifiuta di `
        + 'eseguire: non ripiega sulla macchina.',
    };
  }
  const suaImmagine = state.settings.docker_image === info.project_image;
  // L'immagine di serie ha solo Python: senza quella del progetto, il primo
  // `pytest -q` dell'agente risponde "not found" e sembra un bug nostro.
  const immagine = suaImmagine
    ? `Immagine del progetto · cartella montata su <code>${esc(info.workdir || '/work')}</code>`
    : `L'immagine <code>${esc(state.settings.docker_image || '')}</code> non è quella del progetto: dentro `
      + 'mancano pytest, ruff e git. Si costruisce dal percorso in cima alla chat, con «Crea l\'immagine».';
  return {
    stato: suaImmagine ? 'ok' : 'warn',
    titolo: info.running ? 'Sandbox attiva' : 'Sandbox pronta',
    dettaglio: immagine,
  };
}

function impDisegnaStatoSandbox() {
  const nodo = $('#imp-stato-sandbox');
  if (!nodo) return;
  const { stato, titolo, dettaglio } = impTestoSandbox(state.sandbox);
  nodo.className = 'imp-speciale imp-blocco imp-stato ' + stato
    + (nodo.classList.contains('imp-esclusa') ? ' imp-esclusa' : '');
  impAggiornaHtml(nodo, `<span class="imp-stato-punto" aria-hidden="true"></span>`
    + `<div class="imp-stato-testo"><div class="imp-stato-titolo">${esc(titolo)}</div>`
    + `<div class="imp-stato-dett">${dettaglio}</div></div>`);
}

// Le chiavi che il profilo consigliato scrive (``profiles.as_settings``).
const IMP_CHIAVI_PROFILO = ['temperature', 'top_p', 'top_k', 'presence_penalty', 'num_ctx', 'max_tokens'];

function impDisegnaProfilo() {
  const nodo = $('#imp-profilo');
  if (!nodo) return;
  const info = state.profilo;
  const esclusa = nodo.classList.contains('imp-esclusa') ? ' imp-esclusa' : '';
  if (!info) {
    nodo.className = 'imp-speciale imp-blocco imp-profilo' + esclusa;
    impAggiornaHtml(nodo, '<div class="imp-profilo-testa"><span class="imp-profilo-etichetta">Profilo consigliato</span>'
      + '<span class="imp-profilo-nome">…</span></div>');
    return;
  }
  const capacita = [info.thinking ? 'ragiona' : null, info.vision ? 'vede le immagini' : null].filter(Boolean);
  // Da dove viene il contesto proposto. Quattro casi, quattro gesti diversi:
  // lo fissa il server (llama.cpp), e' calcolato sulla VRAM misurata, Ollama
  // e' su un'altra macchina e la scheda va dichiarata, nessuna misura. Con un
  // server OpenAI-compatibile la VRAM non e' affar nostro: non si dice niente.
  const c = impContesto();
  const kv = info.kv_mb_per_token ? ` Cache: ${impNumeroIt(info.kv_mb_per_token, 3)} MB per token.` : '';
  let vram = '';
  if (info.server_num_ctx) {
    vram = `La finestra la fissa il server: ${impNumeroIt(info.server_num_ctx)} token.`;
  } else if (info.free_vram_mb) {
    vram = `VRAM libera ${impNumeroIt(info.free_vram_mb / 1024, 1)} GB: il contesto proposto è calcolato su questa.${kv}`;
  } else if (c.server === 'ollama' && info.vram && info.vram.remote) {
    vram = `Ollama gira su ${esc(info.vram.host || 'un\'altra macchina')}: scrivi la VRAM della scheda in `
      + `Modello e server › Avanzate per avere un contesto calcolato invece che confermato.${kv}`;
  } else if (c.server === 'ollama') {
    vram = `VRAM non rilevabile: il contesto proposto è quello attuale, non una misura.${kv}`;
  }
  const differenze = IMP_CHIAVI_PROFILO
    .filter((k) => k in (info.values || {}) && !impUguali(state.settings[k], info.values[k]))
    .map((k) => {
      const campo = impCampo(k);
      return `<li><span>${esc(impEtichetta(k))}</span><span class="imp-da">${esc(impFormatta(campo, state.settings[k]))}</span>`
        + `<span class="imp-freccia">→</span><span class="imp-a">${esc(impFormatta(campo, info.values[k]))}</span></li>`;
    });
  nodo.className = 'imp-speciale imp-blocco imp-profilo' + esclusa;
  const riscritto = impAggiornaHtml(nodo, `<div class="imp-profilo-testa"><span class="imp-profilo-etichetta">Profilo consigliato</span>`
    + `<span class="imp-profilo-nome">${esc(info.profile)}</span>`
    + (capacita.length ? `<span class="imp-badge acc">${esc(capacita.join(' · '))}</span>` : '')
    + '</div>'
    + `<p class="imp-profilo-nota">${esc(info.note || '')} ${vram}</p>`
    + (differenze.length
      ? `<div class="imp-profilo-diff"><div class="imp-profilo-diff-titolo">Applicarlo cambierebbe</div><ul>${differenze.join('')}</ul></div>`
      : '<p class="imp-profilo-ok">Le impostazioni coincidono già con il profilo.</p>')
    + '<div class="imp-profilo-azioni"></div>');
  if (riscritto && differenze.length) {
    const applica = el('button', 'btn primary', 'Applica i consigliati');
    applica.type = 'button';
    applica.onclick = () => impApplicaProfilo(applica);
    $('.imp-profilo-azioni', nodo).appendChild(applica);
  }
}

async function impApplicaProfilo(bottone) {
  bottone.disabled = true;
  try {
    const data = await api('/api/profile/apply', { method: 'POST' });
    state.settings = data.settings;
    // I valori sono cambiati sul server: renderHeader li rimette in tutti i
    // campi, profilo compreso. Nessuna mappa id -> chiave scritta qui.
    renderHeader();
    toast('Profilo applicato: ' + data.applied.profile);
  } catch (error) {
    toast(error.message);
  } finally {
    bottone.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Istruzioni: il prompt di sistema
// ---------------------------------------------------------------------------
//
// Il campo da solo mentiva a chi non l'aveva mai toccato: conteneva il prompt
// esteso (``setdefault`` sul server) mentre a un modello che ragiona arriva
// quello snello. Qui la domanda e' esplicita -- di serie o tuo -- e "di serie"
// mostra quello che il modello riceve davvero, memorie comprese.

const IMP_NOMI_PROMPT = {
  snello: 'snello, per i modelli che ragionano',
  esteso: 'esteso, per i modelli senza canale di pensiero',
  wiki: 'del manutentore della wiki, perché il progetto ha la wiki accesa',
  personalizzato: 'personalizzato',
};

function impLegaPrompt() {
  const nodo = $('#imp-prompt');
  if (!nodo) return;
  nodo.innerHTML = '<div class="imp-prompt-stato" id="imp-prompt-stato">Carico il prompt…</div>'
    + '<div class="imp-segmenti imp-prompt-modo" role="radiogroup" aria-label="Quale prompt usare">'
    + '<button type="button" class="imp-segmento" role="radio" data-modo="serie">Di serie</button>'
    + '<button type="button" class="imp-segmento" role="radio" data-modo="personalizzato">Personalizzato</button>'
    + '</div>'
    + '<div class="imp-prompt-serie" id="imp-prompt-serie">'
    + '<p class="imp-descr">L\'harness sceglie da solo fra il prompt esteso e quello snello, e aggiunge i '
    + 'moduli per il pensiero e la ricerca online quando servono. Qui sotto c\'è quello che il modello '
    + 'riceve adesso, memorie comprese.</p>'
    + '<pre class="imp-prompt-anteprima" id="imp-prompt-anteprima" tabindex="0"></pre></div>'
    + '<div class="imp-prompt-tuo" id="imp-prompt-tuo" hidden>'
    + '<p class="imp-descr">Il tuo testo vince sempre: l\'harness smette di scegliere fra esteso e snello, '
    + 'e le modifiche future al prompt di serie non ti arrivano più. Memorie e moduli si aggiungono lo '
    + 'stesso in coda.</p>'
    + '<textarea id="s-system-prompt" class="imp-area" spellcheck="false" '
    + 'aria-label="Prompt di sistema personalizzato"></textarea>'
    + '<div class="imp-prompt-piede"><span id="imp-prompt-conta"></span>'
    + '<button type="button" class="imp-link" id="imp-prompt-serie-btn">'
    + `${impIcona('ripristina')}<span>Torna al prompt di serie</span></button></div>`
    + '<div class="imp-conferma" id="imp-prompt-conferma" hidden></div></div>';
  bindField('#s-system-prompt', 'system_prompt', (v) => v, { ritardo: 800 });
  const area = $('#s-system-prompt');
  area.addEventListener('input', impContaPrompt);
  $$('.imp-prompt-modo .imp-segmento', nodo).forEach((bottone) => {
    bottone.onclick = () => impScegliModoPrompt(bottone.dataset.modo);
  });
  $('.imp-prompt-modo', nodo).addEventListener('keydown', (event) => {
    if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) return;
    event.preventDefault();
    const altro = Imp.modoPrompt === 'personalizzato' ? 'serie' : 'personalizzato';
    impScegliModoPrompt(altro);
    $(`.imp-prompt-modo [data-modo="${altro}"]`)?.focus();
  });
  $('#imp-prompt-serie-btn').onclick = () => impScegliModoPrompt('serie');
}

async function impCaricaPrompt() {
  try {
    Imp.prompt = await api('/api/settings/prompt');
    if (!Imp.modoPrompt || (Imp.modoPrompt === 'serie' && !Imp.prompt.di_serie)) {
      Imp.modoPrompt = Imp.prompt.di_serie ? 'serie' : 'personalizzato';
    }
    impDisegnaPrompt();
  } catch (error) {
    const stato = $('#imp-prompt-stato');
    if (stato) stato.textContent = 'Non riesco a leggere il prompt: ' + error.message;
  }
}

function impDisegnaPrompt() {
  const info = Imp.prompt;
  if (!info || !$('#imp-prompt')) return;
  const tuo = Imp.modoPrompt === 'personalizzato';
  $$('.imp-prompt-modo .imp-segmento').forEach((b) => {
    const scelto = (b.dataset.modo === 'personalizzato') === tuo;
    b.setAttribute('aria-checked', String(scelto));
    b.tabIndex = scelto ? 0 : -1;
  });
  $('#imp-prompt-serie').hidden = tuo;
  $('#imp-prompt-tuo').hidden = !tuo;
  const token = `${impNumeroIt(info.token_effettivo)} token con memorie e moduli`;
  const stato = $('#imp-prompt-stato');
  if (!info.di_serie) {
    stato.innerHTML = `<span class="imp-badge acc">Personalizzato</span><span>In uso il tuo testo · ${token}</span>`;
  } else if (tuo) {
    stato.innerHTML = '<span class="imp-badge">Di serie</span><span>Il testo qui sotto è ancora quello di '
      + 'serie: diventa tuo appena lo modifichi.</span>';
  } else {
    stato.innerHTML = `<span class="imp-badge">Di serie</span><span>In uso il prompt `
      + `${esc(IMP_NOMI_PROMPT[info.attivo] || info.attivo)} · ${token}</span>`;
  }
  $('#imp-prompt-anteprima').textContent = info.effettivo || '';
  impContaPrompt();
}

function impContaPrompt() {
  const area = $('#s-system-prompt');
  const conta = $('#imp-prompt-conta');
  if (!area || !conta) return;
  const caratteri = area.value.length;
  conta.textContent = `${impNumeroIt(caratteri)} caratteri · circa ${impNumeroIt(Math.round(caratteri / 3.6))} token`;
}

async function impScegliModoPrompt(modo) {
  const info = Imp.prompt;
  if (!info) return;
  const conferma = $('#imp-prompt-conferma');
  if (modo === 'personalizzato') {
    Imp.modoPrompt = 'personalizzato';
    // Si parte dal testo che il modello riceve adesso, non da un campo vuoto:
    // un prompt riscritto da zero perde regole che nessuno ricorda di aver
    // chiesto. Salvarlo non cambia niente -- e' ancora un testo di serie, e il
    // server lo riconosce -- ma lo mette nel campo, e il riallineamento non
    // lo svuota piu'.
    if (!String(state.settings.system_prompt || '').trim() || info.di_serie) {
      const base = info.base_attiva || info.base.snello;
      if (state.settings.system_prompt !== base) {
        const salvataggio = saveSettings({ system_prompt: base });
        impSegnaSalvataggio(['system_prompt'], salvataggio);
        try { await salvataggio; } catch (error) { toast(error.message); }
      }
    }
    impDisegnaPrompt();
    $('#s-system-prompt').focus();
    return;
  }
  // Tornare al prompt di serie butta il testo scritto a mano: si chiede prima.
  if (!info.di_serie && conferma.hidden) {
    conferma.hidden = false;
    conferma.innerHTML = '<div class="imp-conferma-testo">Il tuo testo verrà eliminato e il modello '
      + 'riceverà di nuovo il prompt di serie.</div><div class="imp-conferma-bottoni">'
      + '<button type="button" class="btn" data-no>Annulla</button>'
      + '<button type="button" class="btn primary" data-si>Torna al prompt di serie</button></div>';
    $('[data-no]', conferma).onclick = () => { conferma.hidden = true; impDisegnaPrompt(); };
    $('[data-si]', conferma).onclick = () => { conferma.hidden = true; impTornaAlPromptDiSerie(); };
    $('[data-si]', conferma).focus();
    return;
  }
  impTornaAlPromptDiSerie();
}

async function impTornaAlPromptDiSerie() {
  Imp.modoPrompt = 'serie';
  // Il vuoto e' il gesto che il server legge come "scegli tu" (is_stock_prompt).
  const salvataggio = saveSettings({ system_prompt: '' });
  impSegnaSalvataggio(['system_prompt'], salvataggio);
  try {
    await salvataggio;
    toast('Prompt di serie: lo sceglie l\'harness in base al modello.');
  } catch (error) {
    toast(error.message);
  }
  impDisegnaPrompt();
}

// ---------------------------------------------------------------------------
// Memorie
// ---------------------------------------------------------------------------

const IMP_MEMORIE_MAX = 60;       // core/memory.py: MAX_MEMORIES
const IMP_MEMORIA_CARATTERI = 400; // core/memory.py: MAX_MEMORY_CHARS

async function impCaricaMemorie() {
  try {
    const data = await api('/api/memories');
    state.memories = data.memories;
  } catch { /* si disegna quello che si ha */ }
  impDisegnaMemorie();
}

function impDisegnaMemorie() {
  const nodo = $('#imp-memorie');
  if (!nodo) return;
  if (!$('#imp-mem-nuova', nodo)) {
    nodo.innerHTML = '<p class="imp-descr imp-memorie-descr">Finiscono tutte nel prompt di sistema di ogni '
      + 'conversazione, come fatti da verificare e non come ordini. L\'agente ne aggiunge anche da solo.</p>'
      + '<form class="imp-mem-aggiungi" id="imp-mem-form">'
      + `<input type="text" class="imp-input" id="imp-mem-nuova" maxlength="${IMP_MEMORIA_CARATTERI}" `
      + 'placeholder="Es. I test si lanciano con uv run pytest -q" autocomplete="off" '
      + 'aria-label="Nuova memoria">'
      + '<button type="submit" class="btn primary">Aggiungi</button></form>'
      + '<div class="imp-mem-testa"><span id="imp-mem-conta"></span></div>'
      + '<div class="imp-carta imp-mem-lista" id="imp-mem-lista"></div>';
    $('#imp-mem-form').onsubmit = async (event) => {
      event.preventDefault();
      const input = $('#imp-mem-nuova');
      const testo = input.value.trim();
      if (!testo) return;
      try {
        const data = await api('/api/memories', { method: 'POST', body: JSON.stringify({ text: testo }) });
        state.memories = data.memories;
        if (data.ok === false) toast(data.message);
        else input.value = '';
      } catch (error) { toast(error.message); }
      impDisegnaMemorie();
      input.focus();
    };
  }
  const memorie = state.memories || [];
  $('#imp-mem-conta').textContent = `${memorie.length} di ${IMP_MEMORIE_MAX}`;
  const lista = $('#imp-mem-lista');
  lista.innerHTML = '';
  lista.hidden = !memorie.length;
  if (!memorie.length) {
    lista.hidden = false;
    lista.appendChild(el('div', 'imp-mem-vuota', 'Nessuna memoria salvata.'));
  }
  [...memorie].reverse().forEach((memoria) => {
    const riga = el('div', 'imp-mem-riga');
    const quando = memoria.created_at ? relTime(memoria.created_at) : '';
    riga.innerHTML = `<div class="imp-mem-testo">${esc(memoria.text)}</div>`
      + `<div class="imp-mem-meta imp-mono">${esc(memoria.id)}${quando ? ' · ' + esc(quando) : ''}</div>`;
    const togli = el('button', 'imp-mem-togli', impIcona('cestino'));
    togli.type = 'button';
    togli.title = 'Elimina questa memoria';
    togli.setAttribute('aria-label', 'Elimina la memoria: ' + memoria.text.slice(0, 60));
    togli.onclick = async () => {
      try {
        const data = await api(`/api/memories/${encodeURIComponent(memoria.id)}`, { method: 'DELETE' });
        state.memories = data.memories;
      } catch (error) { toast(error.message); }
      impDisegnaMemorie();
    };
    riga.appendChild(togli);
    lista.appendChild(riga);
  });
  impDisegnaStati();
}

// ---------------------------------------------------------------------------
// Informazioni, diagnostica, backup
// ---------------------------------------------------------------------------

async function impCaricaDiagnostica() {
  impDisegnaInfo();
  try {
    Imp.diagnostica = await api('/api/diagnostics');
  } catch (error) {
    Imp.diagnostica = { errore: error.message };
  }
  impDisegnaInfo();
}

function impKv(etichetta, valore, { mono = false, titolo = '' } = {}) {
  return `<div class="imp-kv"><span class="imp-kv-chiave">${esc(etichetta)}</span>`
    + `<span class="imp-kv-valore${mono ? ' imp-mono' : ''}"${titolo ? ` title="${esc(titolo)}"` : ''}>${valore}</span></div>`;
}

function impDisegnaInfo() {
  const nodo = $('#imp-info');
  if (!nodo) return;
  const d = Imp.diagnostica || {};
  const app = d.app || { nome: (state.app && state.app.name) || 'Local Agent Harness', versione: (state.app && state.app.version) || '' };
  const b = state.backend || {};
  const nomeServer = IMP_SERVER[b.name] || b.name || '—';
  const serverTesto = b.online == null ? 'controllo in corso'
    : `${esc(nomeServer)}${b.version ? ' ' + esc(b.version) : ''} · ${b.online ? 'raggiungibile' : 'non raggiungibile'}`;
  const sandbox = impTestoSandbox(state.sandbox);
  const percorsi = d.percorsi || {};
  const tasti = [
    [`${impTastoMod()} ,`, 'Apri o chiudi le impostazioni'],
    ['/', 'Cerca fra le impostazioni'],
    ['↑ ↓', 'Sezione precedente o successiva, dalla barra laterale'],
    ['Invio', 'Dalla ricerca, vai alla prima voce trovata'],
    ['Esc', 'Svuota la ricerca, poi chiude'],
  ];
  nodo.innerHTML = '<div class="imp-marchio">'
    + '<img class="imp-marchio-chiaro" src="/static/brand/mark-light.svg" alt="">'
    + '<img class="imp-marchio-scuro" src="/static/brand/mark-dark.svg" alt="">'
    + `<div><div class="imp-marchio-nome">${esc(app.nome)}</div>`
    + `<div class="imp-marchio-sub">Versione ${esc(app.versione)}`
    + (d.python ? ` · Python ${esc(d.python)}` : '') + (d.sistema ? ` · ${esc(d.sistema)}` : '')
    + '</div></div></div>'
    + '<section class="imp-gruppo"><h4 class="imp-gruppo-titolo">Stato</h4><div class="imp-carta imp-kv-lista">'
    + impKv('Server del modello', serverTesto)
    + impKv('Modello', esc(state.settings.model_name || '—'), { mono: true })
    + impKv('Sandbox', esc(sandbox.titolo))
    + impKv('Prompt di sistema', d.prompt_personalizzato == null ? '…' : (d.prompt_personalizzato ? 'personalizzato' : 'di serie'))
    + impKv('Conversazioni', d.conversazioni == null ? '…' : impNumeroIt(d.conversazioni))
    + impKv('Memorie', `${(state.memories || []).length} di ${IMP_MEMORIE_MAX}`)
    + '</div><div class="imp-azioni">'
    + `<button type="button" class="btn" id="imp-copia-diagnostica">${impIcona('copia')}Copia diagnostica</button>`
    + `<button type="button" class="btn" id="imp-riverifica">${impIcona('aggiorna')}Verifica di nuovo</button>`
    + '</div></section>'
    + '<section class="imp-gruppo"><h4 class="imp-gruppo-titolo">Dove stanno i dati</h4>'
    + '<p class="imp-gruppo-nota">Le voci che il menu non mostra si cambiano a mano nel file delle impostazioni, '
    + 'con l\'harness spento.</p><div class="imp-carta imp-kv-lista">'
    + impKv('Impostazioni', esc(percorsi.impostazioni || '…'), { mono: true, titolo: percorsi.impostazioni })
    + impKv('Conversazioni', esc(percorsi.conversazioni || '…'), { mono: true, titolo: percorsi.conversazioni })
    + impKv('Memorie', esc(percorsi.memorie || '…'), { mono: true, titolo: percorsi.memorie })
    + '</div></section>'
    + '<section class="imp-gruppo"><h4 class="imp-gruppo-titolo">Backup e trasferimento</h4>'
    + '<div class="imp-carta imp-backup"><p class="imp-descr">Salva le impostazioni in un file JSON e '
    + 'riaprile sull\'altra macchina. Restano qui la chiave API, la chiave del telefono, le cartelle, i '
    + 'progetti e l\'immagine Docker: sono segreti o percorsi di questa macchina. Si può importare anche un '
    + '<code>agent_settings.json</code> così com\'è.</p><div class="imp-azioni">'
    + `<button type="button" class="btn" id="imp-esporta">${impIcona('scarica')}Esporta…</button>`
    + `<button type="button" class="btn" id="imp-importa">${impIcona('carica')}Importa…</button>`
    + '<input type="file" id="imp-file" accept=".json,application/json" hidden></div>'
    + '<div id="imp-importazione"></div></div></section>'
    + '<section class="imp-gruppo"><h4 class="imp-gruppo-titolo">Scorciatoie da tastiera</h4>'
    + '<div class="imp-carta imp-kv-lista">'
    + tasti.map(([tasto, cosa]) => impKv(cosa, tasto.split(' ').map((t) => `<kbd class="imp-kbd">${esc(t)}</kbd>`).join(' ')))
      .join('')
    + '</div></section>';
  if (d.errore) {
    nodo.insertBefore(el('div', 'imp-avviso', `${impIcona('avviso')}<span>Diagnostica non disponibile: ${esc(d.errore)}</span>`),
      nodo.children[1]);
  }
  $('#imp-copia-diagnostica').onclick = impCopiaDiagnostica;
  $('#imp-riverifica').onclick = async () => {
    refreshSandbox();
    await impRicontrollaServer();
    impCaricaDiagnostica();
  };
  $('#imp-esporta').onclick = impEsporta;
  $('#imp-importa').onclick = () => $('#imp-file').click();
  $('#imp-file').onchange = (event) => impLeggiImportazione(event.target.files && event.target.files[0]);
  impDisegnaImportazione();
}

function impTestoDiagnostica() {
  const d = Imp.diagnostica || {};
  const s = state.settings;
  const b = state.backend || {};
  const sandbox = state.sandbox || {};
  const adesso = new Date().toLocaleString('it-IT');
  const righe = [
    `${(d.app && d.app.nome) || 'Local Agent Harness'} ${(d.app && d.app.versione) || ''} — diagnostica del ${adesso}`,
    `Sistema: ${d.sistema || '?'} · Python ${d.python || '?'}`,
    `Server del modello: ${IMP_SERVER[b.name] || b.name || '?'}${b.version ? ' ' + b.version : ''} · ${b.url || s.api_base}`
      + ` · ${b.online == null ? 'non ancora controllato' : (b.online ? 'raggiungibile' : 'non raggiungibile')}`,
  ];
  if (b.online === false && b.detail) righe.push(`  ${b.detail}`);
  righe.push(
    `Modello: ${s.model_name}`,
    `Contesto: ${s.num_ctx} token · risposta max ${s.max_tokens} · ${s.max_agent_loops} passi · pensiero ${s.native_think}`,
    `Sandbox: ${sandbox.mode || s.sandbox}${sandbox.mode === 'docker' ? ` · ${sandbox.available ? (sandbox.running ? 'attiva' : 'pronta') : 'Docker non raggiungibile'}` : ''}`
      + ` · immagine ${s.docker_image}`,
    `Prompt di sistema: ${d.prompt_personalizzato ? 'personalizzato' : 'di serie'}`,
    `Conversazioni: ${d.conversazioni ?? '?'} · Memorie: ${(state.memories || []).length}`,
  );
  const diverse = d.diverse_dal_default || {};
  const chiavi = Object.keys(diverse).sort();
  if (chiavi.length) {
    righe.push('Impostazioni diverse dal valore di serie:');
    chiavi.forEach((k) => righe.push(`  ${k} = ${JSON.stringify(diverse[k])}`));
  }
  return righe.join('\n');
}

async function impCopiaDiagnostica() {
  if (!Imp.diagnostica || Imp.diagnostica.errore) await impCaricaDiagnostica();
  const testo = impTestoDiagnostica();
  try {
    await navigator.clipboard.writeText(testo);
    toast('Diagnostica copiata: incollala dove serve.');
  } catch {
    // Senza permesso sugli appunti si ripiega sul vecchio metodo.
    const area = el('textarea');
    area.value = testo;
    area.setAttribute('readonly', '');
    area.style.cssText = 'position:fixed;opacity:0;pointer-events:none';
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand && document.execCommand('copy');
    area.remove();
    toast(ok ? 'Diagnostica copiata.' : 'Non riesco a copiare: il browser non lo permette.');
  }
}

async function impEsporta() {
  try {
    const dati = await api('/api/settings/export');
    const blob = new Blob([JSON.stringify(dati, null, 2) + '\n'], { type: 'application/json' });
    const data = new Date();
    const giorno = `${data.getFullYear()}-${String(data.getMonth() + 1).padStart(2, '0')}-${String(data.getDate()).padStart(2, '0')}`;
    const link = el('a');
    link.href = URL.createObjectURL(blob);
    link.download = `astra-impostazioni-${giorno}.json`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 4000);
    toast(`Esportate ${Object.keys(dati.impostazioni || {}).length} impostazioni.`);
  } catch (error) {
    toast('Esportazione non riuscita: ' + error.message);
  }
}

async function impLeggiImportazione(file) {
  $('#imp-file').value = '';
  if (!file) return;
  let dati;
  try {
    dati = JSON.parse(await file.text());
  } catch {
    toast(`${file.name} non è un file JSON valido.`);
    return;
  }
  try {
    const prova = await api('/api/settings/import', {
      method: 'POST', body: JSON.stringify({ impostazioni: dati, prova: true }),
    });
    Imp.importazione = { file: file.name, dati, prova };
  } catch (error) {
    Imp.importazione = null;
    toast(error.message);
  }
  impDisegnaImportazione();
}

function impDisegnaImportazione() {
  const nodo = $('#imp-importazione');
  if (!nodo) return;
  const imp = Imp.importazione;
  nodo.innerHTML = '';
  if (!imp) return;
  const { prova } = imp;
  const cambi = prova.diverse.map((k) => {
    const campo = impCampo(k);
    const prima = k === 'system_prompt' ? 'di serie o il tuo' : impFormatta(campo, state.settings[k]);
    const dopo = k === 'system_prompt'
      ? `testo personalizzato (${impNumeroIt(String(prova.valori[k] || '').length)} caratteri)`
      : impFormatta(campo, prova.valori[k]);
    return `<li><span>${esc(impEtichetta(k))}</span><span class="imp-da">${esc(prima)}</span>`
      + `<span class="imp-freccia">→</span><span class="imp-a">${esc(dopo)}</span></li>`;
  });
  const ignorate = prova.ignorate.map((i) => `<li><code>${esc(i.chiave)}</code>: ${esc(i.motivo)}</li>`);
  const riquadro = el('div', 'imp-importa-anteprima');
  riquadro.innerHTML = `<div class="imp-importa-titolo">${esc(imp.file)}</div>`
    + (cambi.length
      ? `<div class="imp-profilo-diff"><div class="imp-profilo-diff-titolo">Cambierebbero ${cambi.length} `
        + `${cambi.length === 1 ? 'impostazione' : 'impostazioni'}</div><ul>${cambi.join('')}</ul></div>`
      : '<p class="imp-profilo-ok">Il file non cambia niente: le impostazioni sono già queste.</p>')
    + (ignorate.length
      ? `<details class="imp-ignorate"><summary>${ignorate.length} ${ignorate.length === 1 ? 'voce lasciata' : 'voci lasciate'} `
        + `fuori</summary><ul>${ignorate.join('')}</ul></details>`
      : '');
  const bottoni = el('div', 'imp-conferma-bottoni');
  const annulla = el('button', 'btn', 'Annulla');
  annulla.type = 'button';
  annulla.onclick = () => { Imp.importazione = null; impDisegnaImportazione(); };
  bottoni.appendChild(annulla);
  if (cambi.length) {
    const applica = el('button', 'btn primary', 'Importa');
    applica.type = 'button';
    applica.onclick = () => impApplicaImportazione(applica);
    bottoni.appendChild(applica);
  }
  riquadro.appendChild(bottoni);
  nodo.appendChild(riquadro);
}

async function impApplicaImportazione(bottone) {
  const imp = Imp.importazione;
  if (!imp) return;
  bottone.disabled = true;
  try {
    const data = await api('/api/settings/import', {
      method: 'POST', body: JSON.stringify({ impostazioni: imp.dati }),
    });
    state.settings = data.settings;
    if (data.stats) applyStats(data.stats);
    applyTheme(state.settings.theme_mode);
    applicaColonne();
    renderHeader();
    Imp.importazione = null;
    impDisegnaImportazione();
    const toccate = new Set(data.diverse);
    if (['api_base', 'transport', 'api_key', 'timeout_seconds', 'model_name'].some((k) => toccate.has(k))) {
      impRicontrollaServer();
    } else {
      refreshProfile();
    }
    if (['sandbox', 'sandbox_network', 'preview_ports_enabled', 'preview_port_base', 'preview_port_count']
      .some((k) => toccate.has(k))) {
      refreshSandbox();
      refreshReadiness();
    }
    if (toccate.has('system_prompt')) { Imp.modoPrompt = null; impCaricaPrompt(); }
    toast(`Importate ${data.diverse.length} ${data.diverse.length === 1 ? 'impostazione' : 'impostazioni'}.`);
  } catch (error) {
    bottone.disabled = false;
    toast(error.message);
  }
}
