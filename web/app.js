/* Local Agent Harness — client.
 *
 * Nessun framework e nessuna CDN: l'harness gira in locale, spesso su una
 * macchina senza rete, e una dipendenza remota lo romperebbe proprio nel caso
 * d'uso per cui esiste.
 *
 * Il principio che risolve il problema dell'interfaccia precedente: nessun
 * ridisegno globale. Ogni frame SSE tocca un solo nodo del DOM, gia' creato
 * quando il turno e' iniziato. Non c'e' nessuna riesecuzione dello script,
 * nessun rimontaggio della pagina, nessuna perdita di scroll o di focus.
 */

'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  settings: {},
  sessions: [],
  sessionId: null,
  memories: [],
  // I progetti registrati, con identita' e memoria (web/progetti.js).
  progetti: [],
  // Il progetto corrente: quello della chat aperta, o quello di cui si sta
  // guardando la schermata. ``progettoHome`` dice quale delle due: dentro un
  // progetto si puo' benissimo stare in una chat.
  progetto: null,
  progettoHome: false,
  // Tutto quello che serve alla schermata (chat, archiviate, "Riprendi da
  // qui", libreria): arriva in una chiamata sola da /api/progetti/home.
  progettoDati: null,
  // Albero dei progetti nella colonna di sinistra. ``progettiAperti`` sono i
  // percorsi espansi, ``progettoChat`` le chat di ciascuno, chieste quando
  // serve e non tutte all'avvio.
  progettiAperti: new Set(),
  progettoChat: {},
  // Stato della schermata che deve sopravvivere a un ridisegno: filtro della
  // memoria, voce in modifica (id, o 'nuova'), istruzioni in modifica,
  // ricerca fra le chat, archiviate aperte.
  memoriaFiltro: '',
  memoriaModulo: null,
  istruzioniInModifica: false,
  istruzioniTutte: false,
  libreriaTutta: false,
  progettoCerca: '',
  progettoArchiviateAperte: false,
  // Voci della memoria appena cambiate: si evidenziano per qualche secondo.
  memoriaNuove: new Set(),
  // Le conversazioni libere archiviate: il conto per l'etichetta, l'elenco a
  // richiesta.
  archiviateLibere: { conto: 0, aperte: false, elenco: [] },
  backend: {},
  stats: {},
  attachments: [],
  busy: false,
  // Livello di pensiero scelto nella goccia del composer: resta selezionato
  // fra i messaggi (e' un'impostazione del pannello, non un'opzione monouso)
  // e viaggia solo sull'invio, come ``web_search``.
  thinkLevel: 'auto',
  pending: null,
  autoScroll: true,
  // Conversazioni con un turno in corso sul server. Il lavoro non e' legato
  // alla finestra: si puo' cambiare chat e tornare indietro.
  running: new Set(),
  attachToken: 0,
  attachAbort: null,
  // Ricerca nelle conversazioni. ``q`` vuoto = elenco normale; ``token`` scarta
  // le risposte di una ricerca precedente, che su una digitazione veloce
  // possono arrivare dopo quelle di una piu' recente.
  search: { q: '', results: [], token: 0 },
  // Cronologia caricata **finora**. Una chat agentica lunga sono migliaia di
  // messaggi: il server ne manda la coda e il resto arriva risalendo.
  // ``msgs`` e' quello che si ha in mano, ``offset`` da che punto della
  // cronologia vera comincia, ``total`` quanti ce ne sono in tutto.
  history: { msgs: [], offset: 0, total: 0, loading: false },
  preview: null,
  // C'e' qualcosa **dentro** il pannello di anteprima. Non e' la stessa cosa
  // di "il pannello si vede": sulla schermata di un progetto il
  // pannello sparisce, ma quello che c'era dentro resta li' e torna quando si
  // rientra in una chat. Vedi ``sincronizzaAnteprima``.
  previewAperta: false,
  // Il bus globale (EventSource su /api/events). Serve a sapere se la pagina
  // e' collegata: quando non lo e', quello che succede sul server non arriva.
  bus: null,
  // Dov'era il thread all'ultimo evento di scroll. Serve a distinguere una
  // risalita vera (l'utente cerca indietro) da un passaggio vicino alla cima
  // mentre si scende: solo la prima chiede la cronologia.
  ultimoScrollTop: 0,
  // Cartella servita dal pannello, relativa al workspace ('' = il workspace
  // intero). null = si sta guardando un file solo, e solo quel file lo
  // riguarda. Decide quali salvataggi fanno scattare la ricarica viva.
  previewRoot: null,
  readiness: null,
  prepWasRunning: false,
};

// ---------------------------------------------------------------------------
// Utilita'
// ---------------------------------------------------------------------------

function esc(text) {
  return String(text ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function el(tag, className, html) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (html !== undefined) node.innerHTML = html;
  return node;
}

/**
 * Lo stato delle verifiche a fine turno, accanto al piano e non dentro.
 *
 * Il piano puo' essere tutto chiuso e la suite rossa: da quando chiudere un
 * punto non cancella piu' una verifica, sono due fatti che possono
 * contraddirsi, ed e' giusto che si vedano tutti e due.
 */
function renderQualita(qualita) {
  const rosse = Array.isArray(qualita.pendenti) ? qualita.pendenti : [];
  const box = el('div', 'notice');
  const titolo = rosse.length === 1
    ? 'Una verifica e\' rimasta rossa:'
    : `${rosse.length} verifiche sono rimaste rosse:`;
  box.appendChild(el('div', '', `<strong>${esc(titolo)}</strong>`));
  const lista = el('ul', 'qualita-elenco');
  rosse.forEach((v) => {
    const ambito = v.ambito === 'suite' ? ' — suite intera'
      : v.ambito === 'parziale' ? ' — selezione' : '';
    const codice = Number.isInteger(v.returncode) ? ` (exit ${v.returncode})` : '';
    lista.appendChild(el('li', '', `<code>${esc(String(v.comando || v.identita || ''))}</code>${esc(ambito + codice)}`));
  });
  box.appendChild(lista);
  const giustificate = Array.isArray(qualita.giustificate) ? qualita.giustificate : [];
  if (giustificate.length) {
    const righe = giustificate
      .map((v) => `${esc(String(v.comando || ''))}: ${esc(String(v.motivo || 'senza motivo scritto'))}`)
      .join('<br>');
    box.appendChild(el('div', '', `<strong>Archiviate con motivo:</strong><br>${righe}`));
  }
  return box;
}

function toast(message) {
  const node = $('#toast');
  node.textContent = message;
  node.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => node.classList.remove('show'), 2600);
}

function relTime(iso) {
  if (!iso) return '';
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '';
  const mins = Math.round((Date.now() - then) / 60000);
  if (mins < 1) return 'ora';
  if (mins < 60) return mins + 'm';
  const hours = Math.round(mins / 60);
  if (hours < 24) return hours + 'h';
  const days = Math.round(hours / 24);
  return days < 7 ? days + 'g' : new Date(then).toLocaleDateString('it-IT', { day: 'numeric', month: 'short' });
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch { /* body non JSON */ }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

// ---------------------------------------------------------------------------
// Markdown minimale
// ---------------------------------------------------------------------------
//
// Copre quello che un assistente di codice produce davvero: blocchi di codice,
// codice inline, grassetto/corsivo, titoli, elenchi, link. L'HTML viene
// sempre escapato prima, quindi il markup del modello non puo' iniettare nulla.

// Il segnaposto usa un carattere di CONTROLLO (\u0000) e non `%%BLOCK<n>%%`.
// Quello era testo semplice, e un documento che lo conteneva davvero se lo
// vedeva sostituire col primo blocco di codice — mentre `%%BLOCK99%%` senza un
// blocco 99 stampava la stringa `undefined`. Un NUL non può comparire in un
// messaggio scritto a mano né in un file di testo, e sopravvive a `esc()`.
const SEGNA = '\u0000';
const RE_SEGNA = /\u0000(\d+)\u0000/g;

function markdown(source) {
  const blocks = [];
  // Il NUL viene tolto dall'ingresso prima di tutto: se un giorno ne arrivasse
  // uno davvero (un file binario incollato), non deve poter fingersi un
  // segnaposto nostro.
  let text = String(source ?? '').replace(/\u0000/g, '').replace(/\r\n/g, '\n');

  // 1. blocchi recintati, messi da parte per non essere toccati dal resto
  text = text.replace(/```([\w+-]*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    blocks.push(`<pre><code data-lang="${esc(lang)}">${esc(code.replace(/\n$/, ''))}</code></pre>`);
    return `${SEGNA}${blocks.length - 1}${SEGNA}`;
  });

  text = esc(text);

  // 2. codice inline, anch'esso protetto dalle regole successive
  text = text.replace(/`([^`\n]+)`/g, (_, code) => {
    blocks.push(`<code>${code}</code>`);
    return `${SEGNA}${blocks.length - 1}${SEGNA}`;
  });

  text = text
    // Il livello si conserva: `##` non è `######`, e l'agente scrive report
    // con una gerarchia vera. Il CSS li rende simili, ma appiattirli qui
    // buttava l'informazione **prima** che il CSS potesse decidere.
    .replace(/^\s*(#{1,6})\s+(.+)$/gm, (_, h, testo) => `<h${h.length}>${testo}</h${h.length}>`)
    .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[\s(])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');

  // 3. paragrafi ed elenchi
  const out = [];
  let list = null;
  for (const rawLine of text.split('\n')) {
    const line = rawLine.trimEnd();
    const bullet = line.match(/^\s*[-*+]\s+(.*)$/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);

    if (bullet || numbered) {
      const wanted = bullet ? 'ul' : 'ol';
      if (list !== wanted) {
        if (list) out.push(`</${list}>`);
        out.push(`<${wanted}>`);
        list = wanted;
      }
      out.push(`<li>${(bullet || numbered)[1]}</li>`);
      continue;
    }
    if (list) { out.push(`</${list}>`); list = null; }

    if (!line.trim()) continue;
    if (/^<h[1-6]>/.test(line) || /^\u0000\d+\u0000$/.test(line.trim())) out.push(line);
    else out.push(`<p>${line}</p>`);
  }
  if (list) out.push(`</${list}>`);

  // `?? ''` invece di `undefined` stampato nella pagina: un indice fuori
  // elenco non può più succedere con questo segnaposto, ma se succedesse è
  // meglio un buco che la parola 'undefined' in mezzo a una risposta.
  return out.join('\n').replace(RE_SEGNA, (_, i) => blocks[Number(i)] ?? '');
}

function prettyJson(raw) {
  try { return JSON.stringify(JSON.parse(raw), null, 2); } catch { return String(raw ?? ''); }
}

/** Il testo in arrivo e', molto probabilmente, roba di servizio del modello.
 *
 * Due casi osservati con qwen2.5-coder, entrambi da NON riversare in chat:
 *   - una tool call stampata come JSON invece che via function calling;
 *   - un involucro <tool_response> scritto dal modello stesso, cioe' un
 *     risultato di tool inventato di sana pianta.
 * Il controllo e' volutamente grezzo perche' gira ad ogni refresh dello
 * stream: la prosa non comincia mai con una graffa o con un tag <tool_.
 */
function isModelPlumbing(text) {
  let stripped = String(text ?? '').trimStart();
  if (!stripped) return false;
  if (stripped.startsWith('```')) {
    const newline = stripped.indexOf('\n');
    stripped = newline !== -1 ? stripped.slice(newline + 1).trimStart() : '';
  }
  if (/^<\/?tool_/i.test(stripped)) return true;
  return stripped.startsWith('{') && stripped.includes('"');
}

const PLUMBING_PLACEHOLDER =
  '<div class="plumbing">Il modello sta emettendo una chiamata a tool nel canale ' +
  'testuale…<span class="caret"></span></div>';

// ---------------------------------------------------------------------------
// Icone
// ---------------------------------------------------------------------------

const CHEV = '<svg class="chev" width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M3.5 1.5L7 5l-3.5 3.5"/></svg>';

const FOLDER_ICON =
  '<svg viewBox="0 0 14 14" width="11" height="11" fill="none" stroke="currentColor" ' +
  'stroke-width="1.3" stroke-linejoin="round"><path d="M1.5 3.5h3.6l1.2 1.4h6.2v6.6a1 1 0 0 1-1 1H2.5a1 1 0 0 1-1-1z"/></svg>';

// Che faccia ha un file, guardando l'estensione. Volutamente grossolana: serve
// a far distinguere a colpo d'occhio un foglio di stile da uno script, non a
// classificare con precisione.
const FILE_ICON = [
  [/\.(py|pyw)$/i, '🐍'],
  [/\.(js|mjs|cjs|ts|tsx|jsx)$/i, '⚡'],
  [/\.(html?|htm)$/i, '🌐'],
  [/\.(css|scss|less)$/i, '🎨'],
  [/\.(json|ya?ml|toml|ini|cfg|env)$/i, '⚙'],
  [/\.(md|markdown|rst|txt)$/i, '📄'],
  [/\.(png|jpe?g|gif|webp|svg|ico)$/i, '🖼'],
  [/\.(sh|bat|ps1|cmd)$/i, '❯'],
  [/\.(csv|tsv|xlsx?)$/i, '▦'],
];

function fileIcon(path) {
  for (const [re, icon] of FILE_ICON) if (re.test(path)) return icon;
  return '📎';
}

// Come si chiama, per chi legge, l'azione dichiarata dal tool di scrittura.
const FILE_ACTION = { creato: 'creato', sovrascritto: 'riscritto', modificato: 'modificato' };

/** Il file toccato da una chiamata a tool, se ne ha toccato uno.
 *
 * La lista dei file di fine turno NON la scrive il modello: l'harness ha gia'
 * visto passare ogni write_file ed edit_file, e chiedergliela costerebbe token
 * per una risposta che puo' dimenticare o -- peggio -- inventare. Sta qui e non
 * nel backend perche' questa stessa funzione serve in due momenti, in diretta
 * sull'evento tool_end e alla riapertura di una sessione salvata: una copia
 * sola non puo' divergere dall'altra.
 */
function fileTocca(name, args, content, ok) {
  if (!ok || (name !== 'write_file' && name !== 'edit_file')) return null;
  let payload;
  try { payload = JSON.parse(content); } catch { return null; }
  if (!payload || payload.status !== 'ok') return null;
  const path = String(payload.filepath || (args || {}).filepath || '');
  if (!path) return null;
  return { path, action: FILE_ACTION[String(payload.action)] || 'modificato' };
}

function fileChips(files) {
  const box = el('div', 'file-chips');
  files.forEach((file) => {
    const chip = el('div', 'chip');
    chip.title = `${file.path} — ${file.action}`;
    const nome = file.path.split('/').pop();
    const apri = el('button', 'chip-name');
    apri.innerHTML = `<span class="chip-icon">${fileIcon(file.path)}</span>` +
      `<span class="chip-label">${esc(nome)}</span>`;
    apri.onclick = () => openPreviewFile(file.path);

    // Solo l'iconcina, senza il percorso scritto accanto. La cartella era
    // stampata su ogni goccia per distinguere due file omonimi in src/ e
    // tests/: un caso raro pagato ad ogni riga, che su un refactoring da
    // quindici file trasformava l'elenco in una colonna di percorsi. Dove sta
    // il file resta a portata di puntatore -- il percorso intero e' il titolo
    // della goccia -- e l'iconcina resta la scorciatoia per aprirne la
    // cartella.
    const dir = file.path.slice(0, -nome.length).replace(/\/$/, '');
    const cartella = el('button', 'chip-dir');
    cartella.innerHTML = FOLDER_ICON;
    cartella.title = dir ? `Apri ${dir}` : 'Apri la cartella del workspace';
    cartella.onclick = () => {
      api('/api/workspace/open', {
        method: 'POST',
        body: JSON.stringify({ path: file.path }),
      }).catch((e) => toast(e.message));
    };

    chip.append(apri, cartella);
    box.appendChild(chip);
  });
  return box;
}

// ---------------------------------------------------------------------------
// Rendering del thread
// ---------------------------------------------------------------------------

const thread = () => $('#thread');

/** Porta il thread in fondo.
 *
 * `behavior: 'instant'` e non l'assegnazione a `scrollTop`, e non e' un
 * dettaglio: il `#scroller` ha `scroll-behavior: smooth` nel CSS, quindi
 * `scrollTop = scrollHeight` non salta, **anima**. Aprendo una chat lunga
 * l'animazione parte da zero e per i suoi primi fotogrammi il thread e' in
 * cima -- dove il gestore dello scroll chiede il blocco di messaggi
 * precedenti, che ridisegna tutto e ripristina la posizione di *quel*
 * momento, cioe' l'inizio. Il risultato che si vedeva: si entra in una
 * conversazione e ci si ritrova al primo messaggio, con una pagina di
 * cronologia caricata che nessuno aveva chiesto.
 *
 * Il secondo colpo, al fotogramma dopo, e' per il contenuto che si assesta
 * dopo il primo disegno (font, blocchi di codice, gocce che vanno a capo): il
 * documento cresce sotto di noi e il fondo di un attimo prima non e' piu' il
 * fondo.
 */
function scrollDown(force = false) {
  if (!force && !state.autoScroll) return;
  const scroller = $('#scroller');
  const infondo = () => {
    scroller.scrollTo({ top: scroller.scrollHeight, behavior: 'instant' });
    // Chi sposta la pagina da solo dichiara dove l'ha messa: cosi' l'evento di
    // scroll che ne segue non viene scambiato per una risalita dell'utente.
    state.ultimoScrollTop = scroller.scrollTop;
  };
  infondo();
  if (force) requestAnimationFrame(infondo);
}

function addUser(text, attachments) {
  $('.welcome')?.remove();
  const node = el('div', 'msg');
  // Le gocce stanno SOPRA la bolla, dal lato di chi scrive: sono arrivate
  // prima del testo -- si allega e poi si scrive -- e leggerle prima e' anche
  // l'ordine in cui il messaggio va capito.
  if (attachments && attachments.length) node.appendChild(attachChips(attachments));
  node.appendChild(el('div', 'bubble-user', esc(text)));
  thread().appendChild(node);
  scrollDown(true);
}

/** Il turno dell'assistente: blocchi di lavoro, risposte, e quello che il
 * turno lascia (gocce dei file, avvisi, domande).
 *
 * Com'era: una tendina per ogni pensiero e una per ogni tool, tutte uguali,
 * ognuna una scatola con bordo e fondo. Un turno da sette tool erano undici
 * scatole prima della risposta, e i pensieri restavano aperti fino a fine
 * turno col testo intero.
 *
 * Com'e': i passi consecutivi (pensieri e tool) stanno in un **blocco di
 * lavoro** (``Passi.gruppo``, web/passi.js). Vivo, dice "Sta lavorando · passo
 * 6 · 31 s" e mostra gli ultimi passi, col pensiero in una finestra di quattro
 * righe; chiuso, e' una riga sola -- "Ha lavorato 41 s · 2 letture · 1
 * modifica · 2 comandi" -- che si apre sulla traccia. Un testo del modello
 * **spezza** il blocco: quello che il modello dice resta in chat, fra un
 * blocco e l'altro, e non finisce mai nascosto dentro una tendina.
 *
 * Il tempo e' quello del server: ``t`` sugli eventi dal vivo (server/main.py,
 * ``sse``), ``ts`` sui messaggi salvati. Con l'orologio del browser le durate
 * sarebbero sbagliate nell'arretrato di un riattacco, che arriva tutto in un
 * istante.
 */
function makeTurn() {
  const wrap = el('div', 'msg assistant');
  thread().appendChild(wrap);

  const turn = {
    wrap,
    statusNode: null,
    _answer: null,
    // Il testo accumulato della risposta: gli incrementi portano solo il
    // pezzo nuovo, e il markdown non si rende a pezzi.
    _answerText: '',
    // File creati o modificati nel turno, in ordine di prima comparsa. Una Map
    // e non un array: lo stesso file scritto e poi ritoccato tre volte deve
    // comparire una volta sola, e con la PRIMA azione -- "creato" e' cio' che
    // interessa a chi legge, "modificato" lo nasconderebbe.
    _files: new Map(),
    _filesNode: null,

    /** Lo stato ("Passo 2/12") resta sempre l'ultimo figlio del turno. */
    _bumpStatus() {
      if (this.statusNode) this.wrap.appendChild(this.statusNode);
    },

    // Pensieri e tool: li smista la regia dei blocchi (web/passi.js).
    ora(t) { this._lavoro.ora(t); },
    segnaPasso(n) { this._lavoro.segnaPasso(n); },
    setThinking(text) { this._lavoro.setThinking(text); },
    appendThinking(chunk) { this._lavoro.appendThinking(chunk); },
    pensieroSalvato(text, inizio, fine) { this._lavoro.pensieroSalvato(text, inizio, fine); },
    avviaTool(event) {
      this._lavoro.avviaTool({ nome: event.name, args: event.args, id: event.call_id });
    },
    concludiTool(dati, tempi) { this._lavoro.concludiTool(dati, tempi); },

    _ensureAnswer() {
      if (!this._answer) {
        this._answer = el('div', 'answer');
        this.wrap.appendChild(this._answer);
        this._bumpStatus();
      }
      return this._answer;
    },

    appendAnswer(chunk) {
      // La risposta e' markdown, e il markdown non si rende a pezzi: il testo
      // si accumula qui e si ridisegna. Il risparmio non e' nel rendering, e'
      // nei frame -- dieci al secondo invece di uno per token.
      this.setAnswer((this._answerText || '') + chunk);
    },

    setAnswer(text, { final = false } = {}) {
      this._answerText = String(text ?? '');
      // Un passo che produce solo tool call non deve creare un nodo risposta
      // vuoto: resterebbe piantato SOPRA il blocco di lavoro e spingerebbe la
      // risposta vera fuori dall'ordine cronologico.
      if (!String(text ?? '').trim() && !this._answer) return;
      // Una tool call stampata come testo non e' una risposta: non chiude il
      // blocco (la chiamata la recupera l'harness), lo dice l'intestazione.
      if (!final && !this._answer && isModelPlumbing(text)) {
        this._lavoro.nota('scrive una chiamata a tool come testo');
        return;
      }
      // Il modello parla: il blocco di lavoro si chiude qui. Quello che dice
      // resta in chat, fra un blocco e l'altro -- mai dentro una tendina.
      this._lavoro.chiudi();
      const node = this._ensureAnswer();
      node.innerHTML = (!final && isModelPlumbing(text))
        ? PLUMBING_PLACEHOLDER
        : (text ? markdown(text) : '');
      scrollDown();
    },

    /** Aggiunge in coda qualcosa che non e' un passo (avviso, domanda, gocce,
     *  errore): chiude il blocco di lavoro e apre un segmento nuovo.
     *
     *  E' il punto che tiene il turno in ordine cronologico: dopo, il pensiero
     *  e la risposta del passo successivo devono finire SOTTO, non riscrivere
     *  quelli di prima piu' in alto.
     */
    append(node) {
      this._lavoro.chiudi();
      this._lavoro.dimenticaPasso();
      this.wrap.appendChild(node);
      this._answer = null;
      // Il segmento nuovo parte da testo vuoto: gli incrementi del passo
      // successivo non devono accodarsi a quello di prima.
      this._answerText = '';
      this._bumpStatus();
      scrollDown();
    },

    addTool(node) { this.append(node); },

    /** Registra il file toccato da una chiamata, se ne ha toccato uno. */
    noteFile(name, args, content, ok) {
      const file = fileTocca(name, args, content, ok);
      if (file && !this._files.has(file.path)) this._files.set(file.path, file);
    },

    /** Segna che questa risposta l'ha chiesta l'harness, non il modello.
     *
     *  Succede quando il turno finisce i passi senza aver detto niente: senza
     *  questa riga la chiusura sembrerebbe una scelta del modello, e invece
     *  il lavoro si è fermato a metà. Chi legge deve poter distinguere "ho
     *  finito" da "mi hanno interrotto e ho raccontato dov'ero". */
    markForzato() {
      if (this._forzato) return;
      this._forzato = el('div', 'turn-forzato',
        'Passi del turno esauriti: questo è il resoconto di dove si è fermato.');
      this.append(this._forzato);
    },

    /** Mostra le gocce dei file a fine ciclo. Idempotente. */
    showFiles() {
      if (!this._files.size || this._filesNode) return;
      this._filesNode = fileChips([...this._files.values()]);
      this.append(this._filesNode);
    },

    finish() {
      this._lavoro.chiudi();
    },

    isEmpty() {
      // Un turno con il solo riquadro d'errore NON e' vuoto: e' il turno che
      // e' fallito, e il motivo e' proprio quello che l'utente deve leggere.
      // Contarlo come vuoto lo faceva rimuovere a fine stream -- l'errore
      // "che lampeggia e sparisce".
      return !this.wrap.querySelector('.ps-gruppo, .question, .error-box')
        && !(this._answer && this._answer.textContent.trim());
    },
  };

  turn._lavoro = Passi.lavoro({
    // Un blocco nuovo apre un segmento nuovo: la prossima risposta va sotto
    // di lui, non a riscrivere quella di prima piu' in alto.
    inserisci(nodo) {
      turn.wrap.appendChild(nodo);
      turn._answer = null;
      turn._answerText = '';
      turn._bumpStatus();
    },
    // Solo i turni dal vivo hanno la riga di stato, e solo loro contano i
    // secondi nell'intestazione del blocco.
    vivo: () => Boolean(turn.statusNode),
    dopo() {
      turn._bumpStatus();
      scrollDown();
    },
  });
  return turn;
}

function renderQuestion(question, answered) {
  const box = el('div', 'question');
  box.innerHTML =
    '<div class="q-label">L\'agente chiede</div>' +
    `<div class="q-text">${esc(question.question)}</div>`;

  if (answered) {
    box.appendChild(el('div', 'q-answered', `Risposta: <b>${esc(answered)}</b>`));
    return box;
  }

  // Solo il riquadro ancora in attesa e' ``live``: e' cosi' che ``submitAnswer``
  // trova quello giusto invece del primo della pagina.
  box.classList.add('live');

  const options = el('div', 'q-options');
  const multi = !!question.allow_multiple;
  const picked = new Set();

  (question.options || []).forEach((option) => {
    const button = el('button', 'q-option');
    button.innerHTML = `<span class="lbl">${esc(option.label)}</span>` +
      (option.description ? `<span class="desc">${esc(option.description)}</span>` : '');
    button.onclick = () => {
      if (!multi) {
        // Prima il segno, poi la chiamata: la scelta a scelta singola chiude
        // il riquadro, e fra il click e la fine della POST c'e' un giro di
        // rete in cui i pulsanti resterebbero cliccabili.
        button.classList.add('selected');
        submitAnswer(option.label);
        return;
      }
      if (picked.has(option.label)) { picked.delete(option.label); button.classList.remove('selected'); }
      else { picked.add(option.label); button.classList.add('selected'); }
      confirm.disabled = picked.size === 0;
    };
    options.appendChild(button);
  });
  box.appendChild(options);

  const free = el('div', 'q-free');
  const input = el('input');
  input.type = 'text';
  input.placeholder = multi ? 'Oppure scrivi la tua risposta…' : 'Altro: scrivi la tua risposta…';
  input.onkeydown = (event) => {
    if (event.key === 'Enter' && input.value.trim()) submitAnswer(input.value.trim());
  };
  const confirm = el('button', 'btn primary', multi ? 'Conferma' : 'Invia');
  confirm.disabled = multi;
  confirm.onclick = () => {
    if (multi && picked.size) return submitAnswer([...picked]);
    if (input.value.trim()) submitAnswer(input.value.trim());
  };
  free.append(input, confirm);
  box.appendChild(free);

  setTimeout(() => input.focus(), 30);
  return box;
}

// ---------------------------------------------------------------------------
// Prontezza dell'ambiente
// ---------------------------------------------------------------------------
//
// "Workspace pronto" era una speranza, non una verifica: la scritta compariva
// appena la pagina si apriva, con Ollama magari spento e Docker mai avviato. A
// scoprirlo era il primo messaggio dell'utente, sotto forma di errore dentro
// una tendina a metà turno.

const CHECK_ICON = { ok: '\u25cf', warn: '\u25d0', error: '\u25cf' };
const CHECK_ACTIONS = {
  docker: { label: 'Avvia Docker', run: () => runPrep('/api/prep/docker', 'Avvio Docker…') },
  image: { label: 'Costruisci l\'immagine', run: () => runPrep('/api/prep/image', 'Costruisco l\'immagine…') },
  // La sezione giusta, non la prima: dal server spento si arriva a
  // "Modello e server", dai comandi senza recinto a "Sandbox".
  settings: { label: 'Apri le impostazioni', run: (check) => impApri(check === 'docker' ? 'sandbox' : 'server') },
  workspace: { label: 'Scegli la cartella', run: () => browseWorkspace() },
};

function welcomeHtml() {
  const info = state.readiness;
  if (!info) {
    return '<div class="welcome"><h3>Controllo l\'ambiente…</h3>'
      + '<p>Verifico il server dei modelli, Docker e l\'immagine del progetto.</p></div>';
  }

  const rotti = info.checks.filter((c) => c.state === 'error');
  const avvisi = info.checks.filter((c) => c.state === 'warn');

  if (!rotti.length && !avvisi.length) {
    return '<div class="welcome"><h3>Workspace pronto</h3>'
      + '<p>Chiedi di ispezionare il progetto, scrivere codice o eseguire i test.<br>'
      + 'L\'agente lavora direttamente sui file della cartella.</p></div>';
  }

  const titolo = rotti.length ? 'Manca qualcosa' : 'Pronto, con qualche riserva';
  const righe = [...rotti, ...avvisi].map((c) => {
    const azione = CHECK_ACTIONS[c.action];
    return `<div class="check ${c.state}">`
      + `<span class="check-dot">${CHECK_ICON[c.state]}</span>`
      + `<span class="check-text"><b>${esc(c.label)}</b>${esc(c.detail) ? ' — ' + esc(c.detail) : ''}</span>`
      + (azione ? `<button class="mini-btn check-btn" data-action="${esc(c.action)}" data-check="${esc(c.id)}">${esc(azione.label)}</button>` : '')
      + '</div>';
  }).join('');

  return `<div class="welcome"><h3>${titolo}</h3><div class="checks">${righe}</div>`
    + `<p class="check-foot">${rotti.length
        ? 'Puoi scrivere lo stesso, ma il primo turno fallirà su questo.'
        : 'Si può lavorare: quello che manca peggiora il risultato, non lo impedisce.'}</p></div>`;
}

function bindWelcome() {
  $$('.check-btn').forEach((btn) => {
    btn.onclick = () => CHECK_ACTIONS[btn.dataset.action]?.run(btn.dataset.check);
  });
}

async function runPrep(path, messaggio) {
  try {
    await api(path, { method: 'POST' });
    toast(messaggio);
    refreshReadiness();
  } catch (error) { toast(error.message); }
}

async function refreshReadiness() {
  try {
    const info = await api('/api/readiness');
    state.readiness = info;
    const welcome = $('.welcome');
    if (welcome) {
      welcome.outerHTML = welcomeHtml();
      bindWelcome();
    }
    // Si continua a chiedere solo finché c'è un lavoro in corso: un polling
    // perenne su una pagina aperta tutto il giorno è traffico per niente.
    const lavorando = Object.values(info.jobs || {}).some((j) => j.state === 'running');
    clearTimeout(refreshReadiness._t);
    if (lavorando) {
      refreshReadiness._t = setTimeout(refreshReadiness, 2500);
    } else if (state.prepWasRunning) {
      const rotti = info.checks.filter((c) => c.state === 'error');
      toast(rotti.length ? `Ancora da sistemare: ${rotti[0].label}` : 'Ambiente pronto.');
    }
    state.prepWasRunning = lavorando;
  } catch { /* non critico: la schermata resta com'è */ }
}

/** Ridisegna un'intera conversazione caricata da disco. */
function renderHistory(messages, opzioni = {}) {
  const root = thread();
  root.innerHTML = '';
  state.pending = null;

  // La sentinella della risalita. E' un nodo vero e non solo un ascoltatore
  // sullo scroll perche' deve dire anche una cosa: che sopra c'e' dell'altro.
  // Senza, una chat aperta a meta' sembra una chat che comincia li'.
  if (state.history.offset > 0) root.appendChild(sentinellaPrecedenti());

  if (!messages.length) {
    root.innerHTML = welcomeHtml();
    bindWelcome();
    // La prontezza vera arriva dal server: finché non risponde si mostra
    // l'ultima conosciuta, che al primo avvio è "sto controllando".
    refreshReadiness();
    return;
  }

  // Un solo turno per blocco assistant+tool consecutivi, e i nodi vengono
  // aggiunti nell'ordine in cui i messaggi sono stati salvati: e' lo stesso
  // ordine cronologico che si vede durante lo streaming.
  let turn = null;
  const currentTurn = () => (turn ??= makeTurn());
  // Le gocce si disegnano quando il turno si chiude, cioe' alla richiesta
  // successiva dell'utente o alla fine della cronologia: e' lo stesso confine
  // che in diretta segna l'evento done.
  const closeTurn = () => { turn?.finish(); turn?.showFiles(); turn = null; };
  // L'ora del messaggio precedente (``ts``, orologio del server): e' da li'
  // che il modello ha cominciato a lavorare sul passo dopo. Le durate dei
  // blocchi e dei pensieri si ricavano cosi'; dove manca -- sessioni salvate
  // prima che il messaggio dell'utente la portasse -- si tace.
  let prima = null;

  messages.forEach((msg) => {
    const ts = Number.isFinite(msg.ts) ? msg.ts : null;
    const inizio = prima;
    if (ts !== null) prima = ts;
    if (msg.hidden) return;

    if (msg.role === 'user') { closeTurn(); addUser(msg.content, msg.attachments); return; }

    if (msg.role === 'assistant') {
      const [reasoning, answer] = splitThink(msg.content || '');
      // Un assistant che porta solo tool_calls non chiude il turno: i suoi
      // risultati arrivano subito dopo e vanno nello stesso blocco.
      if (!reasoning && !answer) { currentTurn(); return; }
      const t = currentTurn();
      if (reasoning) t.pensieroSalvato(reasoning, inizio, ts);
      t.ora(ts);
      if (answer) t.setAnswer(answer, { final: true });
      // Il riepilogo chiesto dall'harness a passi esauriti non è una risposta
      // come le altre: senza dirlo sembrerebbe che il modello si sia fermato
      // da solo e abbia tirato le somme, quando invece il turno è finito
      // perché i passi erano finiti. La differenza cambia la mossa dopo.
      if (msg.forzato && answer) t.markForzato();
      return;
    }

    if (msg.role === 'tool') {
      if (msg.name === 'ask_user_question') {
        let given = '';
        try { given = JSON.parse(msg.content).user_answer || ''; } catch { /* formato vecchio */ }
        currentTurn().append(
          renderQuestion({ question: (msg.args || {}).question || '' }, given || '—'),
        );
      } else {
        const t = currentTurn();
        t.concludiTool({
          nome: msg.name, args: msg.args, risultato: msg.content,
          ok: msg.ok !== false, durata: msg.duration_s, id: msg.tool_call_id,
        }, { inizio, fine: ts });
        t.noteFile(msg.name, msg.args, msg.content, msg.ok !== false);
      }
      return;
    }

    if (msg.role === 'summary') {
      // Il cartello viene ridisegnato dalla cronologia, non dall'evento: una
      // sessione riaperta domani deve mostrare lo stesso confine di oggi.
      closeTurn();
      thread().appendChild(compactedNode({
        messages: msg.replaced || 0,
        // Il riassunto è il messaggio stesso, senza il tag che serve al
        // modello per riconoscerlo e a chi legge per niente.
        summary: String(msg.content || '')
          .replace(/<\/?cronologia_compattata>/g, '').trim(),
      }));
      return;
    }

    if (msg.role === 'selezione') {
      // Compattazione selettiva: nessun riassunto, alcune chiamate vecchie
      // tolte o accorciate nella vista del modello. Stesso cartello del
      // riassunto, con la riga che dice cosa e' stato deciso.
      closeTurn();
      thread().appendChild(compactedNode({
        messages: msg.candidati || 0,
        summary: String(msg.descrizione || ''),
      }));
      return;
    }

    if (msg.role === 'pending_question') {
      state.pending = msg;
      currentTurn().append(renderQuestion(msg, null));
      return;
    }

    if (msg.role === 'memoria') {
      // La memoria del progetto aggiornata a fine turno (core/memoria_progetto.py).
      // Dopo le gocce dei file, come in diretta: prima il lavoro, poi cosa ne resta.
      const t = currentTurn();
      t.showFiles();
      t.append(nodoMemoria(msg.esito, ''));
      return;
    }

    if (msg.role === 'error') {
      // L'errore che ha chiuso il turno, salvato dal server (``registra_errore``):
      // la rilettura dal disco a fine turno lo ridisegna invece di cancellarlo.
      currentTurn().append(el('div', 'error-box', esc(msg.content || '')));
    }
  });

  // Un turno lasciato aperto in coda alla cronologia e' comunque concluso --
  // a meno che la sessione sia ferma su una domanda: li' il ciclo riprendera'
  // appena l'utente risponde, e le gocce arriveranno alla fine vera.
  if (!state.pending) closeTurn();
  else turn?.finish();
  // Risalendo NON si scende: si e' appena aggiunto testo sopra la finestra, e
  // portare in fondo chi stava leggendo indietro e' esattamente il gesto che
  // la risalita serve a evitare. Il ripristino della posizione lo fa chi
  // chiama, che e' l'unico a conoscere l'altezza di prima.
  if (!opzioni.keepScroll) scrollDown(true);
}

/** Riga cliccabile in cima al thread: "ci sono altri N messaggi sopra".
 *
 *  Cliccarla e' la stessa cosa che scorrere fino in cima -- la risalita parte
 *  da sola quando ci si arriva -- ma esiste per chi legge, non per chi
 *  scorre: dice quanti sono e che quindi la conversazione non comincia qui.
 */
function sentinellaPrecedenti() {
  const { offset, loading } = state.history;
  const node = el('button', 'load-older' + (loading ? ' loading' : ''));
  node.textContent = loading
    ? 'Carico i messaggi precedenti…'
    : `Carica i ${Math.min(offset, MESSAGGI_PER_PAGINA)} messaggi precedenti (${offset} sopra)`;
  node.disabled = loading;
  node.onclick = () => caricaPrecedenti();
  return node;
}

const MESSAGGI_PER_PAGINA = 15;

/** Chiede al server il blocco che sta prima di quello gia' disegnato.
 *
 *  Ridisegna tutto invece di inserire i nodi nuovi in testa: il disegno di un
 *  turno e' sequenziale -- un assistant e i tool che seguono finiscono nello
 *  stesso blocco -- e inserire a meta' vorrebbe dire una seconda funzione di
 *  disegno che quella regola la deve conoscere di nuovo. Due posti in cui
 *  ricordarsela, e prima o poi in uno dei due manchera'. Il costo e' ridisegnare
 *  quello che c'e' gia', che e' lo stesso lavoro che si fa aprendo la chat.
 *
 *  Non risale durante un turno in corso: il thread contiene nodi vivi che lo
 *  stream sta ancora scrivendo, e ``state.history.msgs`` non li ha -- un
 *  ridisegno li cancellerebbe a meta' frase.
 */
async function caricaPrecedenti() {
  const h = state.history;
  if (h.loading || h.offset <= 0) return;
  if (state.running.has(state.sessionId)) return;
  const id = state.sessionId;
  h.loading = true;
  const scroller = $('#scroller');
  const sentinella = $('.load-older');
  if (sentinella) { sentinella.disabled = true; sentinella.textContent = 'Carico i messaggi precedenti…'; }
  try {
    const data = await api(
      `/api/sessions/${id}/messages?before=${h.offset}&limit=${MESSAGGI_PER_PAGINA}`,
    );
    // Nel frattempo si puo' aver cambiato conversazione: la risposta che
    // arriva dopo appartiene a un'altra chat e non va disegnata qui.
    if (id !== state.sessionId) return;
    const primaAltezza = scroller.scrollHeight;
    const primaCima = scroller.scrollTop;
    h.msgs = (data.messages || []).concat(h.msgs);
    h.offset = data.messages_offset || 0;
    h.total = data.messages_total || h.msgs.length;
    h.loading = false;
    renderHistory(h.msgs, { keepScroll: true });
    // Si e' aggiunto contenuto sopra: per restare sulla stessa riga bisogna
    // scendere di quanto e' cresciuto il documento.
    scroller.scrollTop = primaCima + (scroller.scrollHeight - primaAltezza);
    state.ultimoScrollTop = scroller.scrollTop;
  } catch (error) {
    toast(error.message);
  } finally {
    h.loading = false;
  }
}

function splitThink(text) {
  const parts = [];
  let rest = String(text || '');
  const re = /<think>([\s\S]*?)(?:<\/think>|$)/g;
  let match;
  while ((match = re.exec(rest))) parts.push(match[1]);
  const answer = rest.replace(/<think>[\s\S]*?(?:<\/think>|$)/g, '').trim();
  return [parts.join('').trim(), answer];
}

// ---------------------------------------------------------------------------
// Streaming del turno
// ---------------------------------------------------------------------------

/** Segna una conversazione come "in esecuzione".
 *
 * L'input si blocca solo se il turno e' di *questa* conversazione: si puo'
 * benissimo scrivere in un'altra chat mentre l'agente lavora qui.
 */
function setRunning(sessionId, running) {
  if (running) state.running.add(sessionId);
  else state.running.delete(sessionId);
  const busyHere = state.running.has(state.sessionId);
  const activityChanged = busyHere !== state.busy;
  state.busy = busyHere;
  // Decorative feedback follows actual activity in the visible conversation.
  const companion = typeof window !== 'undefined' && window.HarnessCompanion;
  if (companion) {
    if (busyHere && activityChanged) companion.setState('working');
    else if (!busyHere && companion.getState() === 'working') companion.setState('idle');
  }

  // Mentre l'agente lavora il tasto non si spegne: diventa stop. Un tasto
  // disabilitato lascia l'utente senza via d'uscita se il modello parte per
  // la tangente, ed e' proprio il momento in cui serve poterlo fermare.
  const send = $('#send');
  send.disabled = false;
  send.classList.toggle('stopping', busyHere);
  send.title = busyHere ? 'Ferma l\'agente' : 'Invia (Invio)';
  send.setAttribute('aria-label', busyHere ? 'Ferma l\'agente' : 'Invia');
  $('#icon-send').style.display = busyHere ? 'none' : '';
  $('#icon-stop').style.display = busyHere ? '' : 'none';
  $('#composer-hint').textContent = busyHere
    ? 'L\'agente sta lavorando · premi il quadrato per fermarlo'
    : 'Invio per inviare · Maiusc+Invio per andare a capo';

  // Le × degli allegati spariscono finche' l'agente lavora: il file e' montato
  // nel workspace e cancellarlo sotto ai piedi di un read_file in corso
  // trasformerebbe un gesto di pulizia in un errore da indagare.
  document.body.classList.toggle('turn-live', busyHere);

  $('#composer textarea').disabled = busyHere;
  $('#composer textarea').placeholder = busyHere
    ? 'L\'agente sta lavorando in questa chat…'
    : "Chiedi all'agente di lavorare sul workspace…";
  const btnContinue = $('#btn-quick-continue');
  if (btnContinue) btnContinue.disabled = busyHere;
  renderSessions();
}

async function stopTurn() {
  const sessionId = state.sessionId;
  $('#send').disabled = true;          // evita il doppio clic sullo stesso stop
  try {
    await api(`/api/stop/${encodeURIComponent(sessionId)}`, { method: 'POST' });
    toast('Interruzione richiesta: l\'agente si ferma al primo punto sicuro.');
  } catch (error) {
    toast(error.message);
    $('#send').disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Riattacco
// ---------------------------------------------------------------------------
//
// Il turno vive sul server, in un thread suo, e il suo buffer di eventi e'
// completo: riattaccarsi non perde niente. Il client pero' non ci provava --
// a stream spezzato scriveva una casella rossa e restava sordo fino alla fine
// del turno. Bastava che il computer dell'harness andasse in sospensione:
// "network error", poi l'agente sembrava congelato, e per rivederlo lavorare
// bisognava chiudere e riaprire la conversazione. Il lavoro non si era mai
// fermato: era la pagina che non guardava piu'.
//
// Le attese crescono per non martellare un server davvero morto, ma non si
// arrendono mai: una sospensione di otto ore deve recuperarsi da sola.
const RIATTACCO_ATTESE = [500, 1000, 2000, 4000, 8000];
const RIATTACCO_ATTESA_MAX = 10000;

function attesaRiattacco(tentativo) {
  return RIATTACCO_ATTESE[tentativo] ?? RIATTACCO_ATTESA_MAX;
}

/** Aspetta, ma si sveglia subito se il computer torna.
 *
 * E' la meta' che rende sopportabili le attese lunghe: al risveglio dalla
 * sospensione, o al ritorno della rete, non si aspettano i dieci secondi del
 * turno di guardia -- si riprova nello stesso istante in cui c'e' di nuovo
 * qualcuno dall'altra parte.
 */
function aspettaOSvegliati(ms, signal) {
  return new Promise((resolve) => {
    let chiuso = false;
    const basta = () => {
      if (chiuso) return;
      chiuso = true;
      clearTimeout(timer);
      window.removeEventListener('online', basta);
      document.removeEventListener('visibilitychange', seVisibile);
      signal?.removeEventListener('abort', basta);
      resolve();
    };
    const seVisibile = () => { if (!document.hidden) basta(); };
    const timer = setTimeout(basta, ms);
    window.addEventListener('online', basta);
    document.addEventListener('visibilitychange', seVisibile);
    signal?.addEventListener('abort', basta);
  });
}

/** Un turno vuoto pronto a ricevere, con la sua riga di stato in fondo. */
function nuovoTurnoDiStream() {
  const turn = makeTurn();
  const status = el('div', 'turn-status', '<span class="spinner"></span><span>Avvio\u2026</span>');
  // aria-live sulla riga di stato, come il mobile ce l'ha sulla striscia di
  // attività (web_mobile/index.html:54). È la stessa domanda — "sta ancora
  // lavorando o si è piantato?" — e senza questo il desktop non annunciava
  // niente: né i passi né la risposta in arrivo. Sta qui e non sul thread
  // perché il thread è tutta la conversazione: con `aria-live` lì sopra, uno
  // screen reader rileggerebbe la risposta ad ogni incremento dello stream.
  status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');
  turn.wrap.appendChild(status);
  turn.statusNode = status;
  return turn;
}

function statoTurno(turn, testo) {
  if (turn.statusNode) turn.statusNode.lastElementChild.textContent = testo;
}

/** Cosa si legge mentre si riprova.
 *
 * Dopo qualche tentativo la frase cambia: una connessione che non torna non e'
 * piu' un singhiozzo, e chi guarda deve poter distinguere "un attimo" da "il
 * server e' spento". In tutti e due i casi si continua a provare.
 */
function messaggioRiattacco(tentativi) {
  return tentativi > 3
    ? 'Connessione persa \u2014 il server non risponde, mi riattacco appena torna\u2026'
    : 'Connessione persa \u2014 mi riattacco\u2026';
}

/** Si attacca allo stream di una conversazione e disegna quello che arriva.
 *
 * Puo' essere chiamata sia dopo aver avviato un turno, sia quando si torna su
 * una conversazione dove il turno e' ancora in corso: il server rimanda prima
 * tutto l'arretrato, quindi si rivedono pensiero e tool gia' eseguiti.
 *
 * ``state.attach`` fa da token: se nel frattempo l'utente cambia chat, il
 * ciclo si accorge di non essere piu' quello buono e smette di scrivere nel
 * DOM, senza interrompere il lavoro sul server.
 *
 * E' un **ciclo**, non una connessione sola: una caduta e' un riattacco, non
 * una fine. Ad ogni ricollegamento l'arretrato ridisegna il turno da capo,
 * quindi il nodo vecchio se ne va e se ne fa uno nuovo -- senza, ogni
 * riconnessione raddoppierebbe pensiero e tool gia' mostrati.
 */
async function attachStream(sessionId) {
  const token = ++state.attachToken;
  const controller = new AbortController();
  state.attachAbort?.abort();
  state.attachAbort = controller;

  setRunning(sessionId, true);
  let turn = nuovoTurnoDiStream();
  scrollDown(true);

  const stillMine = () => token === state.attachToken
    && sessionId === state.sessionId
    && !controller.signal.aborted;

  let tentativi = 0;
  let caduto = false;        // c'e' stata almeno una interruzione
  let sawIdle = false;
  let mio = true;

  while (true) {
    let response;
    try {
      response = await fetch(`/api/stream/${encodeURIComponent(sessionId)}`, {
        signal: controller.signal,
      });
      if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);
    } catch (error) {
      if (!stillMine() || error.name === 'AbortError') { mio = false; break; }
      caduto = true;
      statoTurno(turn, messaggioRiattacco(tentativi));
      await aspettaOSvegliati(attesaRiattacco(tentativi++), controller.signal);
      continue;
    }
    if (!stillMine()) { mio = false; break; }

    // Da qui arriva l'arretrato completo: il turno si ridisegna da zero.
    const vecchio = turn;
    turn = nuovoTurnoDiStream();
    vecchio.wrap.remove();
    scrollDown(true);

    const esito = await leggiLoStream(response, turn, stillMine);
    sawIdle = sawIdle || esito.idle;
    if (esito.tipo === 'estraneo') { mio = false; break; }
    if (esito.tipo === 'fine') break;
    // Caduto. Se questa connessione aveva funzionato, il conto riparte da
    // zero: la prossima sospensione non deve ereditare l'attesa lunga di
    // quella di prima.
    caduto = true;
    if (esito.frames) tentativi = 0;
    statoTurno(turn, messaggioRiattacco(tentativi));
    await aspettaOSvegliati(attesaRiattacco(tentativi++), controller.signal);
  }

  if (!mio) {
    // Un altro attach ha preso il posto di questo: il controller lo azzera lui.
    return;
  }
  turn.statusNode?.remove();
  turn.finish();
  if (turn.isEmpty() || sawIdle) turn.wrap.remove();
  // Lo stream e' finito: da adesso questa pagina **non** e' piu' la fonte
  // viva, e chi arriva dal bus globale puo' ridisegnare. Senza questa riga il
  // controller restava li', mai abortito, e ``attaccatoAUnoStream()`` diceva
  // "sto disegnando io" per sempre: dopo il primo turno, un messaggio scritto
  // dal telefono non compariva piu' sul desktop fino a un ricaricamento.
  if (state.attachAbort === controller) state.attachAbort = null;
  setRunning(sessionId, false);
  refreshSessions();
  // C'e' stata un'interruzione, o il server non aveva piu' niente da
  // raccontare: quello che e' successo mentre non guardavamo non lo ripete
  // nessuno. Si rilegge la conversazione dal disco, che e' la sola fonte
  // completa -- ed e' esattamente il gesto che l'utente faceva a mano,
  // chiudendo e riaprendo la chat.
  if (caduto || sawIdle) riallinea(sessionId);
}

/** Legge una connessione fino in fondo. Non decide niente: dice com'e' finita.
 *
 * ``{tipo: 'fine'}`` lo stream si e' chiuso da solo (turno finito, o niente da
 * seguire); ``'caduto'`` la connessione si e' spezzata a meta'; ``'estraneo'``
 * nel frattempo questa pagina e' andata da un'altra parte. ``frames`` dice se
 * qualcosa era arrivato, ``idle`` se il server non aveva nessun turno.
 */
async function leggiLoStream(response, turn, stillMine) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const setStatus = (testo) => statoTurno(turn, testo);
  let buffer = '';
  let idle = false;
  let frames = 0;
  let completed = false;

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      if (buffer.length > 2 * 1024 * 1024) throw new Error('Frame SSE troppo grande');
      const pezzi = buffer.split('\n\n');
      buffer = pezzi.pop();

      for (const frame of pezzi) {
        const line = frame.split('\n').find((l) => l.startsWith('data: '));
        if (!line) continue;                       // commento di keepalive
        let event;
        event = JSON.parse(line.slice(6));
        if (event.type === 'idle') { idle = true; continue; }
        if (event.type === 'done') completed = true;
        if (!stillMine()) return { tipo: 'estraneo', idle, frames };
        frames += 1;
        handleEvent(event, turn, turn.statusNode, setStatus);
      }
    }
  } catch (error) {
    if (!stillMine() || error.name === 'AbortError') {
      return { tipo: 'estraneo', idle, frames };
    }
    // Una lettura che si spezza a meta' non e' la fine del turno: il turno sta
    // sul server e continua. Questa e' solo la nostra finestra che si e'
    // chiusa, e se ne apre un'altra.
    return { tipo: 'caduto', idle, frames };
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
  if (!stillMine()) return { tipo: 'estraneo', idle, frames };
  return { tipo: completed || idle ? 'fine' : 'caduto', idle, frames };
}

/** Rilegge dal disco la conversazione aperta e la rimette in pari.
 *
 * Gli eventi persi mentre la pagina era scollegata non li ripete nessuno: il
 * bus globale non ha arretrato, e un turno finito durante una sospensione non
 * lascia altra traccia che i messaggi salvati. Questa e' la fonte completa.
 *
 * **GET e non la POST /open**: aprire una conversazione e' un gesto (sposta il
 * workspace, ferma le anteprime della chat da cui si veniva). Usarla come
 * "ricarica" faceva sparire l'anteprima che l'utente stava guardando.
 */
let riallineamentoInCorso = null;

async function riallinea(sessionId) {
  if (!sessionId || sessionId !== state.sessionId) return;
  // Le sveglie possono arrivare a raffica (visibilitychange + online + il
  // riattacco che finisce): una lettura per volta basta.
  if (riallineamentoInCorso) return riallineamentoInCorso;
  riallineamentoInCorso = (async () => {
    try {
      const payload = await api(`/api/sessions/${encodeURIComponent(sessionId)}`);
      // Nel frattempo si puo' essere cambiata chat, o essere arrivata una
      // fonte viva: quella e' piu' fresca del disco e non va calpestata.
      if (sessionId !== state.sessionId || attaccatoAUnoStream()) return;
      showSession(payload);
    } catch { /* si riprovera' alla prossima sveglia */ }
    finally { riallineamentoInCorso = null; }
  })();
  return riallineamentoInCorso;
}

/** Il computer si e' risvegliato, la rete e' tornata, la scheda e' di nuovo
 *  davanti: se non c'e' una fonte viva, si rilegge la conversazione.
 *
 *  Senza, dopo una sospensione la chat restava ferma a com'era: il turno era
 *  finito, i suoi eventi erano passati mentre nessuno ascoltava, e l'unico
 *  modo di rivederli era chiudere e riaprire. */
function bindSveglie() {
  const sveglia = () => {
    if (document.hidden) return;
    // Il bus globale fa da sentinella: finche' e' **aperto**, gli eventi
    // arrivano e la pagina e' gia' in pari. Rileggere ad ogni ritorno sulla
    // scheda sarebbe lavoro inutile con un effetto collaterale sgradevole --
    // il thread si ridisegna e chi stava leggendo indietro si ritrova in
    // fondo. Si rilegge solo quando la sentinella non c'e'.
    if (state.bus && state.bus.readyState === EventSource.OPEN) return;
    refreshSessions();
    if (attaccatoAUnoStream()) return;
    riallinea(state.sessionId);
  };
  window.addEventListener('online', sveglia);
  document.addEventListener('visibilitychange', sveglia);
  // Ritorno dalla cache di navigazione (indietro del browser, scheda
  // ripristinata): la pagina e' quella di prima, le connessioni no.
  window.addEventListener('pageshow', (event) => { if (event.persisted) sveglia(); });
}

/** Questa pagina sta ricevendo un turno dal vivo?
 *
 *  E' la domanda che decide se un evento del bus globale puo' ridisegnare la
 *  conversazione. Un controller esistente e non abortito significa che lo
 *  stream e' aperto **adesso**; ``attachStream`` lo azzera quando finisce. */
function attaccatoAUnoStream() {
  return Boolean(state.attachAbort) && !state.attachAbort.signal.aborted;
}

function handleEvent(event, turn, status, setStatus) {
  // L'ora del server viaggia su ogni frame (server/main.py, ``sse``): le
  // durate dei blocchi di lavoro si misurano con quella.
  if (Number.isFinite(event.t)) turn.ora?.(event.t);
  switch (event.type) {
    case 'start':
      setStatus(event.resume ? 'Riprendo…' : 'Il modello sta pensando…');
      if (typeof window !== 'undefined') window.Cruscotto?.inizioTurno();
      break;
    case 'step':
      setStatus(`Passo ${event.step}/${event.total}`);
      turn.segnaPasso?.(event.step, event.total);
      if (typeof window !== 'undefined') window.Cruscotto?.passo(event.step, event.total);
      break;
    case 'metriche':
      // Il cruscotto della colonna di destra: velocita', fase, prefill,
      // draft. Quattro al secondo durante la generazione, uno definitivo a
      // fine passo. Non tocca il thread.
      if (typeof window !== 'undefined') window.Cruscotto?.metriche(event);
      break;
    case 'note':
      // Avvisi dell'harness, non del modello: immagini entrate in contesto,
      // allegati esclusi. Vanno visti, ma non confusi con una risposta.
      turn.append(el('div', 'notice', esc(event.message)));
      break;
    case 'reasoning':
      if (event.append) turn.appendThinking(event.append);
      else turn.setThinking(event.text || '');
      break;
    case 'content':
      if (event.append) turn.appendAnswer(event.append);
      else turn.setAnswer(event.text || '');
      break;
    case 'assistant':
      turn.setAnswer(event.content || '', { final: true });
      if (event.recovered) {
        turn.append(el('div', 'notice',
          'Il modello ha stampato la chiamata come testo invece di usare il function ' +
          'calling nativo: l\'harness l\'ha recuperata ed eseguita.'));
      }
      break;
    case 'tool_start':
      setStatus(`Eseguo ${event.name}…`);
      turn.avviaTool?.(event);
      if (typeof window !== 'undefined') window.Cruscotto?.toolInizio(event.name);
      break;
    case 'tool_end': {
      turn.concludiTool({
        nome: event.name, args: event.args, risultato: event.result,
        ok: event.ok, durata: event.duration_s, id: event.call_id,
      });
      turn.noteFile(event.name, event.args, event.result, event.ok);
      forseRicarica(event.name, event.args, event.result, event.ok);
      if (typeof window !== 'undefined') window.Cruscotto?.toolFine(event.name, event.duration_s);
      // La scheda dei file toccati si aggiorna adesso, non a fine turno: la
      // stessa regola delle gocce (``fileTocca``), un file per volta.
      const toccato = fileTocca(event.name, event.args, event.result, event.ok);
      if (toccato) aggiungiToccato(toccato.path);
      break;
    }
    case 'plan':
      renderPlan(event.steps);
      break;
    case 'notes':
      renderNotes(event.notes);
      break;
    case 'memoria':
      // L'harness aggiorna la memoria del progetto: un passo in piu' a fine
      // turno, in coda alla conversazione. Lo disegna web/progetti.js.
      eventoMemoria(event, turn, setStatus);
      break;
    case 'compacted':
      turn.append(compactedNode(event));
      if (typeof window !== 'undefined') window.Cruscotto?.compattato();
      break;
    case 'preview':
      renderPreview(event.payload);
      break;
    case 'question':
      state.pending = event;
      turn.addTool(renderQuestion(event, null));
      scrollDown(true);
      break;
    case 'error':
      if (event.message && event.message.includes('riprendo da dove eravamo')) {
        turn.append(el('div', 'notice', esc(event.message)));
        setStatus('Riconnessione al modello in corso…');
      } else {
        turn.append(el('div', 'error-box', esc(event.message)));
        turn.companionFailed = true;
        if (typeof window !== 'undefined') window.HarnessCompanion?.setState('error');
      }
      break;
    case 'done': {
      // `reason` dice come si e' fermato il ciclo, non se il lavoro e'
      // riuscito: "completed" vale anche per un turno che ha esaurito i
      // solleciti con due verifiche rosse aperte. `qualita` e' l'altra meta',
      // e senza mostrarla qui la separazione fra piano e verifiche si
      // ridurrebbe a nascondere i rossi meglio di prima.
      const qualita = event.qualita && typeof event.qualita === 'object' ? event.qualita : null;
      const rosse = qualita && Array.isArray(qualita.pendenti) ? qualita.pendenti : [];
      // A stop, question or step limit must never look like a successful delivery.
      // Nemmeno una consegna con verifiche rosse: non e' un errore del turno,
      // quindi non e' 'error', ma non e' un successo da festeggiare.
      if (typeof window !== 'undefined' && !turn.companionSettled) {
        turn.companionSettled = true;
        window.HarnessCompanion?.setState(turn.companionFailed || event.reason === 'error' ? 'error'
          : rosse.length ? 'idle'
          : event.reason === 'completed' && event.steps > 1 ? 'success' : 'idle');
      }
      // Le gocce chiudono il ciclo: su una pausa per domanda il ciclo non e'
      // finito, e mostrarle li' direbbe "ho consegnato" quando invece sta
      // ancora aspettando una risposta.
      if (event.reason !== 'awaiting_user') turn.showFiles();
      if (event.reason === 'max_steps') {
        turn.append(el('div', 'notice',
          `Limite di ${event.steps} passi raggiunto. Scrivi "continua" o alza il limite nelle impostazioni.`));
        const tb = $('#composer-toolbar');
        if (tb) tb.hidden = false;
      }
      // Il monitor di avanzamento (core/ciclo/avanzamento.py) ha chiuso il
      // turno: troppi passi di fila senza nessun effetto nuovo. Non e' il
      // tetto dei passi, e va detto diversamente: alzare il limite qui non
      // servirebbe a niente.
      if (event.reason === 'stallo') {
        turn.append(el('div', 'notice',
          `Turno fermato dopo ${event.steps} passi: gli ultimi non producevano più niente di nuovo. ` +
          'Scrivi "continua" per riprendere (le verifiche rimaste rosse restano aperte) o indica un\'altra strada.'));
      }
      if (rosse.length) turn.append(renderQualita(qualita));
      // Il riassunto del turno (media, token, correzioni) lo disegna il
      // cruscotto, che ha gia' le righe di ogni passo.
      if (typeof window !== 'undefined') window.Cruscotto?.fine(event);
      break;
    }
    case 'state':
      applyStats(event);
      break;
    default:
      break;
  }
}

async function send() {
  if (state.busy) return stopTurn();   // col turno in corso il tasto e' uno stop
  const box = $('#composer textarea');
  const text = box.value.trim();
  if (!text) return;
  const tb = $('#composer-toolbar');
  if (tb) tb.hidden = true;
  if (state.pending) { toast('Rispondi prima alla domanda dell\'agente.'); return; }
  const sessionId = state.sessionId;
  // Gli allegati in attesa partono con questo messaggio e restano suoi: la
  // striscia si svuota subito, cosi' il prossimo invio non se li riporta
  // dietro. Se la chiamata fallisce li rimette applyStats, che li ritrova
  // ancora non agganciati.
  const allegati = state.attachments.slice();
  // Modalita' ricerca online: lo stato della goccia vale per questo invio e
  // RESTA com'e' per i prossimi: e' un interruttore, non un'opzione
  // monouso. Spenderla sotto le mani dell'utente l'avrebbe costretto a
  // riaccenderla a ogni messaggio, e un modello bloccato dalla guardia
  // "modalita' non attiva" e' quello che sembra un bug.
  const gocciaWeb = $('#toggle-web-search');
  const webSearch = !!(gocciaWeb && gocciaWeb.classList.contains('on'));
  box.value = '';
  box.style.height = 'auto';
  addUser(text, allegati);
  renderAttachments([]);
  // Tutto quello che il pannello puo' sapere **adesso**, lo sa adesso. Prima
  // questi due dipendevano dalla risposta del server, e su un modello locale
  // fra l'invio e il primo byte passano i secondi del caricamento in VRAM: la
  // conversazione appena creata non compariva nell'elenco a sinistra e il
  // riquadro dell'ultima esecuzione mostrava ancora i numeri di quella prima.
  // Nessuno dei due e' un'informazione che deve arrivare dalla rete.
  resetUsage();
  aggiungiAllElenco(sessionId, text);
  try {
    const data = await api('/api/chat', {
      method: 'POST',
      body: JSON.stringify({
        session_id: sessionId,
        prompt: text,
        attachments: allegati.map((a) => a.name),
        web_search: webSearch,
        // "auto" non viaggia: e' l'assenza di override, il server fa come
        // da impostazioni. Low/Medium/High sono il livello di QUESTO turno.
        think_level: state.thinkLevel === 'auto' ? undefined : state.thinkLevel,
      }),
    });
    // Il consumo del contesto e l'elenco delle conversazioni si aggiornano
    // qui, non a fine turno: il messaggio e' partito, il conto e' gia'
    // cambiato, e su un turno da minuti aspettare l'evento ``state`` finale
    // vuol dire leggere per tutto quel tempo un numero che non e' piu' vero.
    if (data.stats) applyStats(data.stats);
  } catch (error) { toast(error.message); return; }
  attachStream(sessionId);
}

/** Segna il riquadro come risposto: via i comandi, dentro la risposta scelta.
 *
 *  Separata da ``submitAnswer`` perche' deve avvenire **prima** di qualunque
 *  attesa: il gesto dell'utente e' gia' compiuto, e l'interfaccia non ha
 *  nessun motivo di aspettare il server per dirlo.
 */
function markAnswered(box, answer) {
  if (!box) return;
  const shown = Array.isArray(answer) ? answer.join(', ') : answer;
  box.classList.remove('live');
  box.querySelector('.q-options')?.remove();
  box.querySelector('.q-free')?.remove();
  box.appendChild(el('div', 'q-answered', `Risposta: <b>${esc(shown)}</b>`));
}

async function submitAnswer(answer) {
  if (state.busy) return;
  const sessionId = state.sessionId;
  // ``.question.live`` e non ``.question``: querySelector torna il PRIMO nodo
  // del documento, che in una conversazione con piu' domande e' una vecchia
  // gia' risposta. La risposta finiva incollata sotto quella, e il riquadro
  // vivo restava con i suoi pulsanti fino al ridisegno della cronologia --
  // cioe' a ragionamento finito. Solo il riquadro in attesa porta ``live``.
  markAnswered($('.question.live'), answer);
  state.pending = null;
  try {
    const data = await api('/api/answer', {
      method: 'POST',
      body: JSON.stringify({ session_id: sessionId, answer }),
    });
    if (data.stats) applyStats(data.stats);
  } catch (error) { toast(error.message); return; }
  attachStream(sessionId);
}

// ---------------------------------------------------------------------------
// Pannelli
// ---------------------------------------------------------------------------

/** I file toccati della conversazione, nella scheda in fondo alla colonna.
 *
 *  Le stesse gocce di fine turno, impilate: il nome apre l'anteprima e
 *  l'iconcina la cartella. Riusare ``fileChips`` e non una seconda lista di
 *  righe e' cio' che tiene un gesto solo per lo stesso oggetto -- e cio' che
 *  evita di doverlo correggere in due posti.
 */
function renderToccati(files) {
  state.toccati = files.slice();
  $('#files-title').textContent = `File toccati (${files.length})`;
  const box = $('#files');
  box.innerHTML = '';
  if (!files.length) {
    box.innerHTML = '<div class="empty">Nessun file toccato.</div>';
    return;
  }
  const chips = fileChips(files.slice(0, 40).map((path) => ({ path, action: 'toccato' })));
  chips.classList.add('stacked');
  box.appendChild(chips);
}

/** Un file appena scritto dal turno in corso: entra subito nella scheda.
 *  Prima la scheda si aggiornava solo con le stats di fine turno, e durante
 *  un turno da dieci minuti diceva "nessun file toccato" mentre l'agente ne
 *  riscriveva sei. Le stats di fine turno restano la fonte: la rimettono in
 *  ordine e ci aggiungono i file toccati da altre strade. */
function aggiungiToccato(path) {
  const attuali = state.toccati || [];
  if (attuali.includes(path)) return;
  renderToccati([...attuali, path].sort());
}

function applyStats(stats) {
  state.stats = stats;
  // Contesto, timeline dell'ultimo turno e punti velocita'/contesto: li
  // disegna il cruscotto (web/cruscotto.js), che a turno vivo li tiene
  // aggiornati dagli eventi e qui prende solo lo storico.
  if (typeof window !== 'undefined') window.Cruscotto?.stats(stats);

  renderAttachments(stats.attachments || []);
  renderToccati(stats.touched_files || []);

  // La lista arriva gia' dentro le stats: chi apre una chat non deve fare
  // una seconda GET /api/sessions per riavere le stesse righe.
  if (stats.sessions) {
    state.sessions = stats.sessions;
    stats.sessions.forEach((item) => {
      if (item.running) state.running.add(item.id);
      else state.running.delete(item.id);
    });
    renderSessions();
  }
  if (stats.session_id) state.sessionId = stats.session_id;
}

// ---------------------------------------------------------------------------
// Allegati
// ---------------------------------------------------------------------------

function humanSize(n) {
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return Math.round(n / 1024) + ' KB';
  return (n / (1024 * 1024)).toFixed(1) + ' MB';
}

/** Una goccia di allegato: icona, nome, peso, e la × per toglierlo.
 *
 * La stessa forma in due posti — la striscia sopra la casella di scrittura e
 * il messaggio a cui l'allegato e' agganciato — perche' e' lo stesso oggetto in
 * due momenti della sua vita, prima e dopo l'invio. Due disegni diversi
 * avrebbero fatto sembrare che fossero due cose.
 */
function attachChip(item) {
  const chip = el('div', 'att-chip');
  chip.title = item.path || item.name;
  // Un'immagine entra davvero nel contesto solo se il modello ha la vision:
  // segnalarlo evita di aspettarsi che l'agente "guardi" uno screenshot che
  // per lui e' solo un nome di file.
  const visto = item.image && state.stats.vision;
  chip.innerHTML =
    `<span class="att-icon">${fileIcon(item.name || '')}</span>` +
    `<span class="att-name">${esc(item.name)}</span>` +
    (visto ? '<span class="att-badge" title="Passata al modello">occhio</span>' : '') +
    `<span class="att-size">${humanSize(item.size || 0)}</span>` +
    '<button class="att-del" title="Rimuovi l\'allegato">×</button>';
  chip.querySelector('.att-del').onclick = () => removeAttachment(item.name, chip);
  return chip;
}

/** Le gocce degli allegati partiti insieme a un messaggio. */
function attachChips(list) {
  const box = el('div', 'att-chips');
  (list || []).forEach((item) => box.appendChild(attachChip(item)));
  return box;
}

/** La striscia sopra la casella: solo gli allegati non ancora inviati.
 *
 * Quelli gia' partiti stanno disegnati sotto il loro messaggio e non tornano
 * qui: la striscia dice "parte col prossimo invio", non "esiste".
 */
function renderAttachments(list) {
  state.attachments = list;
  const strip = $('#attach-strip');
  strip.innerHTML = '';
  strip.hidden = !list.length;
  list.forEach((item) => strip.appendChild(attachChip(item)));
}

async function uploadAttachments(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;
  const form = new FormData();
  files.forEach((file) => form.append('files', file, file.name));
  try {
    // Niente Content-Type a mano: il browser deve poter mettere il boundary.
    const response = await fetch(`/api/attachments/${encodeURIComponent(state.sessionId)}`, {
      method: 'POST',
      body: form,
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Allegato rifiutato.');
    if (data.stats) applyStats(data.stats);
    const names = (data.added || []).map((a) => a.name).join(', ');
    if (names) toast(`Allegato in allegati/: ${names}`);
  } catch (error) { toast(error.message); }
}

/** Toglie un allegato. ``chip`` e' la goccia da far sparire dal DOM.
 *
 * Le gocce nella striscia le ridisegna ``applyStats``; quelle agganciate a un
 * messaggio no -- la cronologia non si ridisegna ad ogni evento, e senza
 * toglierla a mano resterebbe una goccia che punta a un file cancellato.
 */
async function removeAttachment(name, chip) {
  // Il server rifiuta comunque a turno in corso: il file e' montato nel
  // workspace e l'agente potrebbe averlo aperto un istante fa. Qui si dice
  // perche', invece di far arrivare un 409 senza contesto.
  if (state.busy) { toast('L\'agente sta lavorando: rimuovilo a turno finito.'); return; }
  try {
    const data = await api(
      `/api/attachments/${encodeURIComponent(state.sessionId)}?name=${encodeURIComponent(name)}`,
      { method: 'DELETE' },
    );
    if (chip && !chip.closest('#attach-strip')) {
      const box = chip.parentElement;
      chip.remove();
      if (box && !box.children.length) box.remove();
    }
    if (data.stats) applyStats(data.stats);
  } catch (error) { toast(error.message); }
}

// ---------------------------------------------------------------------------
// Sandbox
// ---------------------------------------------------------------------------

/** La goccia di stato dell'endpoint: etichetta e colore scritti insieme.
 *
 *  Erano due righe separate, e una sola delle due veniva rifatta quando si
 *  ricaricava l'elenco dei modelli: l'etichetta passava a "online" e la classe
 *  restava quella di prima. Il risultato era la goccia che dice una cosa e ne
 *  colora un'altra -- il caso peggiore, perche' non sembra un errore. Qui il
 *  colore e' una funzione dell'etichetta, e divergere non e' piu' possibile.
 *
 *  ``online`` a ``null`` vuol dire "non lo so ancora": lo stato neutro esiste
 *  perche' prima dell'avvio la goccia era una ``.pill`` senza modificatore, e
 *  il grigio caldo della palette a quella dimensione si legge come un ambra
 *  spento -- cioe' come un avviso che nessuno ha inteso dare.
 */
function renderStatusPill(online, detail) {
  const pill = $('#pill-status');
  if (!pill) return;
  const stato = online == null ? 'wait' : (online ? 'ok' : 'err');
  const label = online == null ? 'controllo…' : (online ? 'online' : 'offline');
  pill.className = 'pill ' + stato;
  pill.innerHTML = `<span class="dot"></span>${label}`;
  const base = online == null
    ? 'Sto interrogando l\'endpoint dei modelli.'
    : (online ? 'Endpoint dei modelli raggiungibile.' : 'Endpoint dei modelli non raggiungibile.');
  // Il dettaglio del server nel titolo: quando la goccia e' rossa, "quale
  // rotta ha risposto cosa" e' l'unica riga che distingue un server spento
  // da un indirizzo sbagliato o da una chiave che manca. Prima quel testo
  // arrivava solo dentro un toast, cioe' spariva dopo tre secondi.
  pill.title = detail ? base + '\n' + detail : base;
  if (typeof impDisegnaStati === 'function') impDisegnaStati();
}

/** Chiede al server del modello chi e' e cosa ha, a pagina gia' disegnata.
 *
 * Era dentro ``/api/bootstrap``: tre viaggi di rete prima di rispondere, cioe'
 * fino a una decina di secondi di **pagina bianca** con il modello su una
 * macchina spenta -- mentre tutto quello che serviva a disegnare l'interfaccia
 * era gia' sul disco dell'harness. Adesso la pagina c'e' subito e questa
 * riempie i pezzi che mancano: la goccia, la versione del backend, la tendina
 * dei modelli, e l'avviso sui tool non-streaming.
 */
async function sondaBackend() {
  let info;
  try {
    info = await api('/api/backend');
  } catch {
    renderStatusPill(false, 'Nessuna risposta dall\'harness.');
    return;
  }
  state.backend = info;
  renderStatusPill(info.online, info.detail);
  $('#pill-backend').textContent = info.name + (info.version ? ' ' + info.version : '');
  fillModels(info.models);
  if (info.streams_tools === false) {
    $('#pill-stream').style.display = '';
    $('#pill-stream').textContent = 'tool non-streaming';
  }
  // Il modello configurato poteva non esserci piu' e il server ne ha scelto un
  // altro: senza questa riga la tendina in alto continuerebbe a mostrare
  // quello sparito.
  if (info.model_name && info.model_name !== state.settings.model_name) {
    state.settings.model_name = info.model_name;
    renderHeader();
  }
}

/** Ricontrolla l'endpoint da solo, ogni ``INTERVALLO_PING`` ms.
 *
 *  La goccia si calcolava al bootstrap e poi solo cambiando indirizzo o
 *  transport: chi accende llama-server (o Ollama) *dopo* aver aperto la UI
 *  restava con un "offline" che non aveva piu' modo di cambiare idea. Il
 *  contrario e' altrettanto vero -- un server caduto restava verde.
 *
 *  Si ferma quando la scheda non e' visibile: interrogare un endpoint ogni
 *  venti secondi per una finestra che nessuno guarda tiene sveglio il
 *  modello e non serve a nessuno. Al ritorno si controlla subito, perche'
 *  quello e' esattamente il momento in cui il dato sullo schermo e' vecchio.
 */
const INTERVALLO_PING = 20000;

async function ricontrollaEndpoint() {
  try {
    const data = await api('/api/ping');
    if (state.backend) { state.backend.online = data.online; state.backend.detail = data.detail; }
    renderStatusPill(data.online, data.detail);
  } catch { /* la rete e' andata via: la goccia resta com'era */ }
}

function avviaPollingEndpoint() {
  let timer = null;
  const parti = () => {
    if (timer) return;
    timer = setInterval(ricontrollaEndpoint, INTERVALLO_PING);
  };
  const ferma = () => { clearInterval(timer); timer = null; };
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) ferma();
    else { ricontrollaEndpoint(); parti(); }
  });
  if (!document.hidden) parti();
}

function renderSandbox(info) {
  state.sandbox = info;
  const pill = $('#pill-sandbox');
  if (info.mode !== 'docker') {
    pill.textContent = 'nessun recinto';
    pill.className = 'pill warn';
    pill.title = 'I comandi girano direttamente sulla macchina: l\'agente vede tutto il disco.';
  } else if (!info.available) {
    pill.textContent = 'docker assente';
    pill.className = 'pill err';
    pill.title = info.detail + ' — i comandi non partiranno.';
  } else {
    pill.textContent = info.running ? 'sandbox attiva' : 'sandbox pronta';
    pill.className = 'pill ok';
    pill.title = `${info.detail} · workspace montato su ${info.workdir}`;
  }
  // Il racconto per esteso -- e le porte che il server pubblicherebbe davvero
  // -- sta nel menu, sezioni Sandbox e Anteprime (``impTestoSandbox``).
  if (typeof impDisegnaStati === 'function') impDisegnaStati();
}

async function refreshProfile() {
  try {
    // Il profilo consigliato vive nella sezione Generazione del menu, che lo
    // disegna con le differenze rispetto ai valori di adesso.
    state.profilo = await api('/api/profile');
    if (typeof impDisegnaStati === 'function') impDisegnaStati();
    if (typeof impRiallinea === 'function') impRiallinea();
  } catch { /* non critico */ }
}

async function refreshSandbox() {
  try { renderSandbox(await api('/api/sandbox')); } catch { /* non critico */ }
}

// Il piano di lavoro nel pannello destro. Non e' decorazione: e' l'unico posto
// in cui si vede *cosa* l'agente crede di stare facendo mentre lo fa. Prima,
// con una richiesta in cinque punti, l'unico segnale era una tendina del
// pensiero che si apriva e si chiudeva.
// Forme diverse fra loro, non varianti dello stesso segno: cerchio vuoto,
// cerchio pieno, spunta, barrato. Servono a distinguere lo stato senza
// affidarsi al colore.
const PLAN_ICONS = { todo: '\u25cb', doing: '\u25cf', done: '\u2713', skipped: '\u2298' };
const PLAN_TITLES = {
  todo: 'da fare', doing: 'in corso', done: 'fatto', skipped: 'saltato',
};

/** Accende la sfumatura di coda finché c'è contenuto sotto la piega.
 *
 *  Si aggancia una volta sola per elemento: rifare l'addEventListener ad ogni
 *  ridisegno del piano — che avviene ad ogni chiamata a manage_plan —
 *  accumulerebbe ascoltatori per tutta la durata della sessione.
 */
function segnalaScorrimento(root) {
  const aggiorna = () => {
    const resta = root.scrollHeight - root.clientHeight - root.scrollTop;
    root.classList.toggle('scorre', resta > 4);
  };
  if (!root.dataset.scrollBound) {
    root.addEventListener('scroll', aggiorna, { passive: true });
    root.dataset.scrollBound = '1';
  }
  aggiorna();
}

function renderPlan(steps) {
  const root = $('#plan');
  const card = $('#plan-card');
  if (!steps || !steps.length) {
    card.style.display = 'none';
    root.innerHTML = '';
    return;
  }
  card.style.display = '';
  const fatti = steps.filter((s) => s.status === 'done').length;
  const saltati = steps.filter((s) => s.status === 'skipped').length;
  $('#plan-count').textContent = `${fatti}/${steps.length}`;
  // I saltati riempiono la barra ma in ambra: il piano è più corto di prima,
  // e sommarli ai fatti direbbe che è stato fatto qualcosa che non è stato
  // fatto -- tenerli fuori del tutto direbbe che manca ancora da fare.
  const quota = (n) => (n / steps.length * 100).toFixed(1) + '%';
  $('#plan-bar-done').style.width = quota(fatti);
  $('#plan-bar-skipped').style.width = quota(saltati);
  $('#plan-bar').title =
    `${fatti} fatti · ${saltati} saltati · ${steps.length - fatti - saltati} da fare`;
  root.innerHTML = '';
  steps.forEach((step) => {
    const row = el('div', 'plan-step ' + step.status);
    // Il numero è l'id del punto, lo stesso che il modello passa a
    // manage_plan: leggere il pannello e leggere la chiamata devono dare la
    // stessa risposta. Il glifo di stato resta come titolo del numero, così
    // "fatto" e "saltato" si distinguono anche senza il colore.
    const stato = PLAN_TITLES[step.status] || '';
    row.innerHTML =
      `<span class="plan-num">${esc(step.id)}</span>` +
      `<span class="plan-mark" title="${stato}" aria-label="${stato}">` +
      `${PLAN_ICONS[step.status] || PLAN_ICONS.todo}</span>` +
      // Il tipo del punto decide quanto ragionera' l'agente su quel punto:
      // si vede accanto al testo, non in un tooltip, perche' e' la cosa da
      // controllare quando un punto "esegui" si prende diecimila token.
      `<span class="plan-text">` +
      (step.tipo ? `<span class="plan-tipo plan-tipo-${esc(step.tipo)}">${esc(step.tipo)}</span>` : '') +
      `${esc(step.text)}</span>` +
      (step.note ? `<span class="plan-note">${esc(step.note)}</span>` : '') +
      (step.status === 'doing' && Array.isArray(step.ipotesi)
        ? step.ipotesi.map((h) => `<span class="plan-note plan-ipotesi">${esc(h)}</span>`).join('')
        : '');
    root.appendChild(row);
  });
  segnalaScorrimento(root);
}

function renderNotes(notes) {
  const root = $('#notes');
  const card = $('#notes-card');
  if (!notes || !notes.length) {
    card.style.display = 'none';
    root.innerHTML = '';
    return;
  }
  card.style.display = '';
  $('#notes-count').textContent = String(notes.length);
  root.innerHTML = '';
  notes.forEach((nota) => {
    const row = el('div', 'note-row');
    row.innerHTML = `<span class="note-dot"></span><span>${esc(nota.text)}</span>`;
    root.appendChild(row);
  });
  segnalaScorrimento(root);
}

/** Il cartello che segna dove la cronologia è uscita dalla vista del modello.
 *
 * Sopra questa riga i messaggi ci sono ancora e si leggono: è il modello che
 * non li ha più, non l'utente. Dirlo esplicitamente evita l'unica lettura
 * sbagliata possibile, cioè che l'harness abbia buttato via del lavoro.
 */
function compactedNode(event) {
  const box = el('div', 'compacted');
  const risparmio = Math.max(0, (event.tokens_before || 0) - (event.tokens_after || 0));
  // I token si mostrano solo quando li conosciamo: alla riapertura di una
  // sessione salvata c'è il confine ma non la misura, e stampare "0 → 0" farebbe
  // sembrare rotto qualcosa che ha funzionato.
  const misura = event.tokens_before
    ? ` · ${fmtTok(event.tokens_before)} → ${fmtTok(event.tokens_after)} tok` +
      (risparmio ? ` (−${fmtTok(risparmio)})` : '')
    : '';
  const testa = el('div', 'compacted-head');
  testa.innerHTML =
    '<span class="compacted-line"></span>' +
    '<span class="compacted-label">Cronologia compattata' +
    (event.messages ? ` · ${event.messages} messaggi` : '') + misura +
    '</span><span class="compacted-line"></span>';
  box.appendChild(testa);

  if (event.summary) {
    const drawer = el('details', 'drawer compacted-drawer');
    drawer.innerHTML =
      `<summary>${CHEV}<span class="grow">Cosa ne è rimasto per il modello</span></summary>` +
      `<div class="drawer-body"><div class="markdown">${markdown(event.summary)}</div></div>`;
    box.appendChild(drawer);
  }
  const nota = el('div', 'compacted-note',
    'I messaggi qui sopra restano leggibili: è il modello che non li ha più in contesto.');
  box.appendChild(nota);
  return box;
}

function fmtTok(n) {
  const v = Number(n) || 0;
  return v >= 1000 ? `${(v / 1000).toFixed(1)}k` : String(v);
}

// ---------------------------------------------------------------------------
// Anteprima
// ---------------------------------------------------------------------------
//
// Due sorgenti, una sola scheda: un file del workspace (servito da
// /api/preview/file) oppure un'applicazione che l'agente ha avviato dentro la
// sandbox su una porta pubblicata. Per chi guarda sono la stessa cosa -- "fammi
// vedere" -- quindi hanno lo stesso posto e lo stesso gesto per aprirle.

const IMG_EXT = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'avif'];
const FRAME_EXT = ['html', 'htm', 'svg', 'pdf'];

function previewExt(path) {
  const dot = String(path || '').lastIndexOf('.');
  return dot === -1 ? '' : String(path).slice(dot + 1).toLowerCase();
}

function previewUrl(payload) {
  if (!payload) return '';
  if (payload.kind === 'app') {
    return `http://127.0.0.1:${payload.port}${payload.url_path || '/'}`;
  }
  return `/api/preview/file?path=${encodeURIComponent(payload.path)}`;
}

/** L'anteprima dichiarata dal modello (o dedotta da un write_file visuale).
 *
 * Si apre da sola, col file dentro. Quando l'anteprima era un overlay sopra la
 * chat questo sarebbe stato uno strappo \u2014 si stava leggendo, e la pagina
 * spariva \u2014 e infatti allora c'era una scheda da premere nella colonna di
 * destra. Da quando il pannello e' una colonna affiancata non copre niente:
 * la chat si stringe, e chiedere un clic in piu' per vedere una cosa che il
 * modello ha appena detto di voler mostrare era solo un passaggio a vuoto.
 */
function renderPreview(payload, { apri = true } = {}) {
  state.preview = payload || null;
  if (!payload) { closePreview(); return; }
  if (!apri) {
    // Entrare in una conversazione non e' chiedere di vedere qualcosa.
    // L'anteprima memorizzata nella chat e' il ricordo di cosa l'agente
    // mostrava l'ultima volta: riaprirla ad ogni ingresso significava
    // ritrovarsi mezzo schermo occupato da una pagina di ieri, per poi
    // chiuderla a mano ogni volta. Il pannello si apre quando l'agente mostra
    // qualcosa **adesso**, che e' l'unico momento in cui lo si e' chiesto.
    state.previewShown = null;
    closePreview();
    return;
  }
  // Cambio chat con anteprima identica a quella gia' mostrata: niente
  // riapertura dell'iframe (flash bianco + refetch del file). Se nel frattempo
  // da una goccia era stato aperto un altro file, il confronto qui sotto
  // fallisce e l'anteprima della conversazione torna giustamente in primo piano.
  if (JSON.stringify(state.previewShown || null) === JSON.stringify(payload)) return;
  openPreview(payload);
}

/** Mostra un file del workspace nel pannello, senza passare dall'agente.
 *
 * E' cio' che sta dietro al nome di una goccia: il file lo ha appena scritto
 * lui, guardarlo deve costare un clic e non una richiesta.
 */
function openPreviewFile(path) {
  openPreview({ kind: 'text', path, title: path.split('/').pop() });
}

/** Titolo, sottotitolo e link esterno del pannello.
 *
 * Sta in una funzione sua perche' l'indirizzo di una pagina si conosce solo
 * dopo aver chiesto al server: prima si disegna quello che si sa gia', poi si
 * corregge il resto. Scriverlo due volte a mano era il modo per ritrovarsi
 * l'iconcina "apri in una scheda" puntata al vecchio indirizzo.
 */
function aggiornaIntestazione(payload, url) {
  $('#ov-sub').textContent = payload.kind === 'app' ? url : (payload.path || '');
  $('#ov-external').href = url || '#';
}

/** Chiede al server di apparecchiare la cartella e torna l'indirizzo vivo.
 *
 * Null quando il server delle anteprime non c'e' (porta spenta, o tutte
 * occupate): chi chiama ripiega sull'iframe a origine opaca di prima, che
 * mostra la pagina senza storage ne' moduli. Peggio del nuovo, meglio di niente.
 */
async function previewHost(path) {
  try {
    const risposta = await api('/api/preview/host', {
      method: 'POST',
      body: JSON.stringify({ path }),
    });
    return risposta && risposta.url ? risposta : null;
  } catch {
    return null;
  }
}

async function openPreview(payload) {
  if (!payload) payload = state.preview;
  if (!payload) return;
  // Cosa c'e' davvero nel pannello adesso: puo' essere l'anteprima corrente
  // dell'agente oppure un file aperto da una goccia, e "ricarica" deve
  // ricaricare quello che si sta guardando, non l'altro.
  state.previewShown = payload;
  const body = $('#ov-body');
  let url = previewUrl(payload);
  // Cosa deve far scattare la ricarica viva. Per un'applicazione o una pagina
  // e' tutta la cartella servita (un foglio di stile riscritto cambia cio' che
  // si vede); per un .md o un'immagine solo quel file, senno' ogni salvataggio
  // del turno rileggerebbe un documento che non e' cambiato.
  state.previewRoot = payload.kind === 'app' ? (payload.root || '') : null;
  $('#ov-title').textContent = payload.title || payload.path || 'anteprima';
  $('#ov-icon').textContent = payload.kind === 'app'
    ? (payload.mode === 'terminal' ? '\u2b1a' : payload.mode === 'gui' ? '\u25a3' : '\u25b6')
    : '\u25f1';
  state.previewAperta = true;
  // ...ma non per forza visibile adesso: se l'agente apre un'anteprima mentre
  // si sta guardando la schermata di un progetto, il pannello resta
  // fermo e comparira' rientrando in una chat.
  sincronizzaAnteprima();
  aggiornaIntestazione(payload, url);
  body.innerHTML = '<div class="empty" style="padding:16px">Carico\u2026</div>';

  const ext = previewExt(payload.path || '');

  // Applicazioni e documenti che il browser sa gia' rendere vanno in un
  // iframe.
  //
  // Il sandbox di un'anteprima di FILE e' senza allow-same-origin di
  // proposito: quell'HTML lo ha scritto il modello e viene servito da
  // /api/preview/file, cioe' dalla stessa origine delle API dell'harness.
  // Li' allow-scripts e allow-same-origin insieme annullerebbero il sandbox --
  // la pagina potrebbe chiamare le nostre rotte e leggerne le risposte -- e
  // non vanno mai messi nella stessa lista.
  //
  // Un'applicazione e' un'altra cosa: sta su 127.0.0.1:82xx, che e' un'origine
  // **diversa** da quella dell'harness, quindi allow-same-origin non le da'
  // accesso ne' al nostro DOM ne' alle nostre risposte. Le serve per esistere:
  // con l'origine opaca il browser applica il CORS ai moduli ES e alle fetch,
  // e sia noVNC (`core/rfb.js`) sia ttyd (`/token`) vengono rifiutati da
  // 'origin: null'. Misurato in Chromium, non dedotto.
  if (payload.kind === 'app' || FRAME_EXT.includes(ext)) {
    // Una pagina passa dal server delle anteprime: e' li' che la sua cartella
    // e' montata su '/', quindi lo stile, gli script e le immagini si trovano
    // -- e l'origine e' diversa dalla nostra, quindi allow-same-origin non le
    // apre le API dell'harness.
    let stessaOrigine = payload.kind === 'app';
    if (payload.kind !== 'app') {
      const host = await previewHost(payload.path);
      if (host) {
        url = host.url;
        state.previewRoot = host.root || '';
        stessaOrigine = true;
      }
    }
    aggiornaIntestazione(payload, url);
    body.innerHTML = '';
    const frame = el('iframe', 'preview-frame');
    const permessi = ['allow-scripts', 'allow-forms', 'allow-popups', 'allow-modals'];
    if (stessaOrigine) permessi.push('allow-same-origin');
    frame.setAttribute('sandbox', permessi.join(' '));
    frame.src = url;
    body.appendChild(frame);
    return;
  }

  // Da qui in giù si costruisce markup attorno a file **scritti dal modello**
  // (o depositati dall'utente in `raw/`). Il percorso passa da `esc`, il
  // markdown da `markdown()`, e nessuno dei due lascia entrare HTML altrui.
  // Ma la difesa vera non è qui: è che tutto questo lo serve un'ALTRA origine
  // (server/previewhost.py, porta sua, niente cookie, niente CORS) e che
  // l'iframe qui sopra è `sandbox`ato senza `allow-same-origin` quando la
  // pagina non è nostra. Chi tocca questo blocco tenga presente che l'esca è
  // già passata due volte da un confine prima di arrivarci.
  if (IMG_EXT.includes(ext)) {
    body.innerHTML = `<div class="preview-pad"><img class="preview-img" src="${esc(url)}" alt=""></div>`;
    return;
  }

  // Markdown e codice li rende il client, con lo stesso markdown della chat:
  // cosi' un report dell'agente si legge come si legge una sua risposta.
  try {
    const response = await fetch(url);
    if (!response.ok) throw new Error('Non riesco a leggere il file.');
    const text = await response.text();
    if (ext === 'md' || ext === 'markdown') {
      body.innerHTML = `<div class="preview-pad markdown">${markdown(text)}</div>`;
    } else {
      body.innerHTML = `<div class="preview-pad"><pre><code>${esc(text)}</code></pre></div>`;
    }
  } catch (error) {
    body.innerHTML = `<div class="error-box" style="margin:16px">${esc(error.message)}</div>`;
  }
}

// ---------------------------------------------------------------------------
// Ricarica viva
// ---------------------------------------------------------------------------
//
// L'agente scrive, l'anteprima si aggiorna. Senza, il pannello mostra la
// versione di due passi fa e si guarda una correzione che "non ha funzionato"
// solo perche' nessuno ha premuto ricarica.
//
// Il segnale ce l'abbiamo gia': i tool_end di write_file/edit_file sono gli
// stessi da cui nascono le gocce dei file. Nessun evento nuovo dal server,
// nessun watcher sul disco -- l'informazione era gia' qui.

let ricaricaVivaTimer = null;

/** Il salvataggio appena arrivato riguarda cio' che si sta guardando? */
function toccaLAnteprima(path) {
  const mostrata = state.previewShown;
  if (!mostrata || !state.previewAperta || !path) return false;
  // Il terminale e lo schermo sono dell'utente: ricaricarli gli butterebbe via
  // quello che ci sta facendo a meta'.
  if (mostrata.mode === 'terminal' || mostrata.mode === 'gui') return false;
  const radice = state.previewRoot;
  if (radice === null || radice === undefined) return path === mostrata.path;
  return radice === '' || path === radice || path.startsWith(radice + '/');
}

/** Rimette in pagina quello che c'e' gia', con il contenuto nuovo.
 *
 * Rassegnare `src` invece di rifare `openPreview` evita il lampo bianco e la
 * seconda richiesta al server delle anteprime: la cartella e' gia' quella
 * giusta, e' cambiato solo cio' che c'e' dentro.
 */
function ricaricaAnteprima() {
  const frame = $('#ov-body iframe');
  if (frame) {
    frame.src = frame.src;
    return;
  }
  openPreview(state.previewShown);
}

function forseRicarica(name, args, content, ok) {
  const file = fileTocca(name, args, content, ok);
  if (!file || !toccaLAnteprima(file.path)) return;
  // Un turno che riscrive sei file di fila e' una ricarica sola: si aspetta
  // che la raffica finisca, senno' si guarda una pagina a meta'.
  clearTimeout(ricaricaVivaTimer);
  ricaricaVivaTimer = setTimeout(ricaricaAnteprima, 400);
}

// Trascinamento del divisorio verticale. Il bordo destro del pannello è fisso
// (è il bordo della finestra), quindi la larghezza nuova è semplicemente la
// distanza fra il puntatore e quel bordo: nessun offset da ricordare, nessuna
// deriva se il mouse esce e rientra.
const PREVIEW_W_KEY = 'ah-preview-w';

/** Rende trascinabile un divisorio verticale.
 *
 *  Le tre maniglie dell'app — anteprima, sidebar, pannello — differiscono solo
 *  per come si ricava la larghezza dalla posizione del puntatore e per dove
 *  finisce il numero. Tutto il resto è identico: la cattura del puntatore
 *  (senza, trascinando sopra l'iframe dell'anteprima gli eventi li mangia
 *  quello e la maniglia si stacca dal cursore), la classe sul body che
 *  impedisce al cursore di tornare freccia e al testo di evidenziarsi, e il
 *  doppio clic come uscita di sicurezza da una colonna ridotta a fessura.
 */
function bindGrip(grip, { larghezza, applica, ripristina, classe = 'resizing' }) {
  if (!grip) return;
  grip.addEventListener('pointerdown', (event) => {
    event.preventDefault();
    grip.setPointerCapture(event.pointerId);
    document.body.classList.add(classe);
    const muovi = (e) => applica(larghezza(e));
    const finisci = () => {
      grip.removeEventListener('pointermove', muovi);
      document.body.classList.remove(classe);
      grip.releasePointerCapture?.(event.pointerId);
    };
    grip.addEventListener('pointermove', muovi);
    grip.addEventListener('pointerup', finisci, { once: true });
    grip.addEventListener('pointercancel', finisci, { once: true });
  });
  if (ripristina) grip.addEventListener('dblclick', ripristina);
}

// Larghezze delle due colonne laterali. I minimi non sono estetici: sotto
// quella soglia il titolo di una conversazione e le righe del piano non stanno
// più su una riga e la colonna smette di servire a qualcosa.
const COLONNE = {
  sidebar: { key: 'ah-sidebar-w', varName: '--sidebar-w', min: 188, max: 460, def: 252 },
  panel: { key: 'ah-panel-w', varName: '--panel-w', min: 224, max: 520, def: 288 },
};

function setColumnWidth(nome, px) {
  const c = COLONNE[nome];
  // Il tetto tiene conto della finestra: su uno schermo stretto due colonne al
  // loro massimo lascerebbero alla chat meno spazio di quello che occupano.
  const massimo = Math.min(c.max, Math.max(c.min, window.innerWidth * 0.32));
  const larghezza = Math.round(Math.min(Math.max(px, c.min), massimo));
  document.documentElement.style.setProperty(c.varName, larghezza + 'px');
  try { localStorage.setItem(c.key, String(larghezza)); } catch { /* modalita' privata */ }
}

// Altezza della sezione Progetti nella colonna di sinistra.
//
// Il minimo e' l'etichetta piu' il pulsante piu' una riga: sotto quella soglia
// la sezione non mostra piu' nessun progetto e la maniglia diventa un modo di
// farla sparire per sbaglio. Il massimo lascia in vita l'elenco di sopra per
// lo stesso motivo -- una maniglia che puo' annullare uno dei due lati non e'
// un divisorio, e' un interruttore travestito.
const PROGETTI_H_KEY = 'ah-progetti-h';
const PROGETTI_H_MIN = 92;
const CONVERSAZIONI_H_MIN = 120;

function setProgettiHeight(px) {
  const sec = $('#progetti-sec');
  const sidebar = $('#sidebar');
  if (!sec || !sidebar) return;
  // Lo spazio contendibile e' quello che resta alla sezione **adesso**: il
  // resto della colonna (marchio, nuova chat, ricerca, footer) non e'
  // ridimensionabile e non deve entrare nel conto.
  const disponibile = sec.getBoundingClientRect().bottom - $('#sessions').getBoundingClientRect().top;
  const massimo = Math.max(PROGETTI_H_MIN, disponibile - CONVERSAZIONI_H_MIN);
  const altezza = Math.round(Math.min(Math.max(px, PROGETTI_H_MIN), massimo));
  document.documentElement.style.setProperty('--progetti-h', altezza + 'px');
  try { localStorage.setItem(PROGETTI_H_KEY, String(altezza)); } catch { /* modalita' privata */ }
}

function bindProgettiResize() {
  const sec = $('#progetti-sec');
  bindGrip($('#progetti-grip'), {
    classe: 'resizing-rows',
    // Il bordo basso della sezione e' fisso (ci sta sotto il footer): l'altezza
    // e' la distanza fra quello e il puntatore.
    larghezza: (e) => sec.getBoundingClientRect().bottom - e.clientY,
    applica: setProgettiHeight,
    // Meta' e meta', calcolata sullo spazio contendibile di adesso: un numero
    // fisso su una finestra bassa vorrebbe dire l'elenco di sopra schiacciato.
    ripristina: () => setProgettiHeight(
      (sec.getBoundingClientRect().bottom - $('#sessions').getBoundingClientRect().top) / 2,
    ),
  });
  let salvata = 0;
  try { salvata = Number(localStorage.getItem(PROGETTI_H_KEY) || 0); } catch { /* privata */ }
  if (salvata > 0) setProgettiHeight(salvata);
}

function bindColumnResize() {
  bindGrip($('#sidebar-grip'), {
    // Il bordo sinistro della sidebar è quello della finestra: la larghezza è
    // semplicemente dove sta il puntatore, nessun offset da ricordare.
    larghezza: (e) => e.clientX,
    applica: (px) => setColumnWidth('sidebar', px),
    ripristina: () => setColumnWidth('sidebar', COLONNE.sidebar.def),
  });
  bindGrip($('#panel-grip'), {
    larghezza: (e) => window.innerWidth - e.clientX,
    applica: (px) => setColumnWidth('panel', px),
    ripristina: () => setColumnWidth('panel', COLONNE.panel.def),
  });
  Object.keys(COLONNE).forEach((nome) => {
    let salvata = 0;
    try { salvata = Number(localStorage.getItem(COLONNE[nome].key) || 0); } catch { /* privata */ }
    if (salvata > 0) setColumnWidth(nome, salvata);
  });
}

function setPreviewWidth(px) {
  const split = $('#main-split');
  const minimo = 260;
  const massimo = Math.max(minimo, split.clientWidth * 0.78);
  const larghezza = Math.round(Math.min(Math.max(px, minimo), massimo));
  $('#preview-pane').style.width = larghezza + 'px';
  try { localStorage.setItem(PREVIEW_W_KEY, String(larghezza)); } catch { /* modalita' privata */ }
}

function bindPreviewResize() {
  const pane = $('#preview-pane');
  bindGrip($('#preview-grip'), {
    larghezza: (e) => pane.getBoundingClientRect().right - e.clientX,
    applica: setPreviewWidth,
    ripristina: () => setPreviewWidth($('#main-split').clientWidth / 2),
  });

  let salvata = 0;
  try { salvata = Number(localStorage.getItem(PREVIEW_W_KEY) || 0); } catch { /* privata */ }
  if (salvata > 0) setPreviewWidth(salvata);
}

/** Decide se il pannello di anteprima si vede, adesso.
 *
 *  Due condizioni, e servono entrambe: che ci sia qualcosa dentro, e che si
 *  stia guardando una conversazione. La schermata di un progetto non e'
 *  una conversazione -- e' il posto in cui si sceglie quale cominciare -- e
 *  un'anteprima aperta li' e' l'anteprima di **un'altra** chat: la stessa
 *  ragione per cui li' spariscono il piano, le note e i numeri dell'ultima
 *  esecuzione.
 *
 *  Nasconde e basta: non svuota. Un'applicazione avviata dall'agente vive
 *  dentro quell'iframe, e passare dalla schermata del progetto non deve
 *  fermarla ne' farle ricaricare la pagina al ritorno.
 */
function sincronizzaAnteprima() {
  const pane = $('#preview-pane');
  if (!pane) return;
  pane.hidden = !(state.previewAperta && !state.progettoHome);
}

function closePreview() {
  const pane = $('#preview-pane');
  // Il controllo e' su ``previewAperta`` e non su ``pane.hidden``: sulla
  // schermata di un progetto il pannello e' gia' nascosto, e uscire di qui senza far
  // niente lascerebbe l'iframe vivo -- cioe' l'applicazione in esecuzione e
  // la connessione aperta -- per una chat che nel frattempo e' stata chiusa.
  if (!pane || !state.previewAperta) return;
  state.previewAperta = false;
  state.previewRoot = null;
  // Una ricarica gia' in coda troverebbe il pannello vuoto e lo riaprirebbe.
  clearTimeout(ricaricaVivaTimer);
  sincronizzaAnteprima();
  // Si svuota davvero: un iframe lasciato nel DOM continua a far girare
  // l'applicazione e a tenere aperta la connessione a pannello chiuso.
  $('#ov-body').innerHTML = '';
}

/** Il messaggio e' partito: i numeri del cruscotto sono del turno di prima.
 *
 *  Fra l'invio e il primo evento del modello possono passare i secondi del
 *  caricamento in VRAM, e i numeri vecchi -- senza etichetta di provenienza --
 *  letti in quel momento non sembrano vecchi, sembrano sbagliati. Il
 *  cruscotto lo dice ("invio…") finche' il turno non parte davvero.
 */
function resetUsage() {
  if (typeof window !== 'undefined') window.Cruscotto?.preparaTurno();
}

/** Evidenzia i termini cercati dentro un estratto gia' da scappare.
 *
 * Si marca **dopo** ``esc``, mai prima: costruire l'HTML e poi passarlo
 * all'escape cancellerebbe i tag, e costruirlo attorno al testo grezzo
 * lascerebbe passare quello che c'era scritto nella conversazione.
 */
function marca(testo, query) {
  let html = esc(testo);
  const termini = String(query || '').split(/\s+/).filter(Boolean)
    .map((t) => esc(t).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
    .sort((a, b) => b.length - a.length);      // prima i piu' lunghi
  if (!termini.length) return html;
  html = html.replace(new RegExp(`(${termini.join('|')})`, 'gi'), '<mark>$1</mark>');
  return html;
}

// Etichetta di dove la parola cercata e' stata trovata. Sono le quattro
// risposte alla domanda "perche' questa conversazione e' in elenco?".
const DOVE_TROVATO = {
  titolo: 'titolo', allegato: 'allegato', testo: 'testo', comando: 'comando',
};

function renderSessions() {
  const root = $('#sessions');
  root.innerHTML = '';
  // Questo elenco e' **sempre** quello delle conversazioni libere: le chat di
  // un progetto stanno annidate sotto di lui, nella sezione qui sotto.
  //
  // Una lista sola, due sorgenti: con la ricerca attiva si disegnano i
  // risultati, altrimenti le conversazioni. Le righe le fa ``rigaSessione``
  // (web/progetti.js), la stessa dei rami dei progetti: il pallino della chat
  // in esecuzione e il menu stanno in un posto solo.
  const cercando = !!state.search.q;
  const elenco = cercando ? state.search.results : state.sessions;
  if (cercando && !elenco.length) {
    root.innerHTML = `<div class="search-empty">Nessuna conversazione per «${esc(state.search.q)}».</div>`;
  } else if (!cercando && !elenco.length) {
    root.innerHTML = '<div class="search-empty">Nessuna conversazione libera.</div>';
  }
  elenco.forEach((item) => {
    // L'estratto e' l'unica cosa che spiega perche' una riga trovata "nel
    // testo" e' li': senza, e' un titolo qualunque in mezzo agli altri.
    root.appendChild(rigaSessione(item, {
      snippet: item.snippet ? marca(item.snippet, state.search.q) : '',
      dove: item.match_in ? (DOVE_TROVATO[item.match_in] || item.match_in) : '',
    }));
  });
  if (!cercando) renderArchiviateLibere(root);
}

/** Mette in cima all'elenco la conversazione che ha appena ricevuto il primo
 *  messaggio, se non c'era.
 *
 *  Una conversazione nuova non esiste su disco finche' non le si scrive
 *  qualcosa, quindi ``session_list()`` non puo' includerla prima. Ma il titolo
 *  di una chat **e' la prima riga che ci ha scritto l'utente**: il client lo
 *  conosce nel momento in cui preme invio, e chiederlo al server sarebbe
 *  chiedere indietro una cosa che ha appena mandato lui. La riga vera arriva
 *  con le statistiche subito dopo e prende il posto di questa.
 */
function aggiungiAllElenco(sessionId, testo) {
  if (state.search.q) return;                // con la ricerca attiva non c'entra
  const titolo = String(testo || '').trim().split('\n')[0].slice(0, 60) || 'Nuova conversazione';
  const riga = {
    id: sessionId,
    title: titolo,
    n_messages: 1,
    updated_at: new Date().toISOString().slice(0, 19),
    running: true,
  };
  // Dentro un progetto la chat nuova appartiene al suo ramo, non alle libere:
  // metterla in cima all'elenco generale la farebbe comparire in un posto in
  // cui, un secondo dopo, il server non la manderebbe piu'.
  if (state.progetto) {
    const chat = state.progettoChat[state.progetto.path];
    if (!chat || chat.some((c) => c.id === sessionId)) return;
    chat.unshift(riga);
    renderProgetti();
    return;
  }
  if (state.sessions.some((s) => s.id === sessionId)) return;
  state.sessions.unshift(riga);
  renderSessions();
}

function markActive(id) {
  // Sposta l'evidenziazione senza aspettare il server: il click deve
  // sembrare istantaneo anche se la risposta arriva qualche decina di ms dopo.
  $$('#sessions .session, #progetti .session').forEach((row) => {
    row.classList.toggle('active', row.dataset.id === id);
  });
}

/** Cerca nelle conversazioni. Il testo lo ha solo il server: vedi la rotta. */
async function cercaConversazioni(query) {
  const q = String(query || '').trim();
  state.search.q = q;
  if (!q) {
    state.search.results = [];
    renderSessions();
    return;
  }
  const token = ++state.search.token;
  try {
    const data = await api(`/api/sessions/search?q=${encodeURIComponent(q)}`);
    if (token !== state.search.token) return;      // e' arrivata una risposta vecchia
    state.search.results = data.sessions;
    data.sessions.forEach((item) => {
      if (item.running) state.running.add(item.id);
      else state.running.delete(item.id);
    });
    renderSessions();
  } catch { /* la ricerca non e' critica: resta l'ultimo esito */ }
}

async function refreshSessions() {
  // Con una ricerca attiva si aggiorna quella: rileggere l'elenco completo
  // lascerebbe in vista una conversazione appena cancellata, o senza il
  // pallino di quella che ha appena iniziato a lavorare.
  if (state.search.q) { cercaConversazioni(state.search.q); return; }
  try {
    const data = await api('/api/sessions');
    state.sessions = data.sessions;
    state.archiviateLibere.conto = data.archiviate || 0;
    if (!state.archiviateLibere.conto) state.archiviateLibere.aperte = false;
    data.sessions.forEach((item) => {
      if (item.running) state.running.add(item.id);
      else state.running.delete(item.id);
    });
    renderSessions();
  } catch { /* la lista non e' critica */ }
}

/** Bus globale del server (SSE /api/events): le notizie larghe che non
 *  appartengono al turno di questa pagina. E' il pezzo che tiene desktop e
 *  telefono sincronizzati: un messaggio scritto dall'altro schermo, una chat
 *  creata o cancellata, una domanda dell'agente arrivano qui e la pagina si
 *  ridisegna da sola, senza refresh manuale. EventSource si ricollega da
 *  solo se la connessione cade. */
function bindGlobalEvents() {
  const bus = new EventSource('/api/events');
  // Tenuto a portata di mano: il suo ``readyState`` e' il modo piu' onesto di
  // rispondere alla domanda "questa pagina e' collegata?".
  state.bus = bus;
  // EventSource si ricollega da solo, ma il bus **non ha arretrato**: quello
  // che e' passato mentre la connessione era giu' -- una sospensione, la rete
  // che cade -- non lo ripete nessuno. Ogni riapertura che non sia la prima
  // e' quindi il momento di rileggere la conversazione dal disco.
  let primaApertura = true;
  bus.onopen = () => {
    if (primaApertura) { primaApertura = false; return; }
    refreshSessions();
    if (!attaccatoAUnoStream()) riallinea(state.sessionId);
  };
  bus.onmessage = (msg) => {
    let event;
    try { event = JSON.parse(msg.data); } catch { return; }
    const suaChat = event.session_id && event.session_id === state.sessionId;

    if (event.type === 'turn' || event.type === 'question') {
      // Fine turno o domanda: la chat aperta si ricarica dal disco, dove il
      // messaggio finale (o il pending_question) e' gia' stato salvato.
      // Una sola funzione per "rimettiti in pari": la stessa che usano il
      // riattacco e le sveglie. Legge con la GET e non con la POST /open --
      // aprire una conversazione ferma le anteprime della chat da cui si
      // veniva, e usarla come ricarica le faceva sparire ad ogni fine turno.
      if (suaChat) riallinea(state.sessionId);
      refreshSessions();
      // Le chat dei progetti stanno nei rami: anche li' il pallino si spegne.
      if (event.type === 'turn') refreshProgetti();
    } else if (event.type === 'sessions') {
      // Creazione, cancellazione o nuovo messaggio dall'altro lato: basta
      // l'elenco. Se e' la chat che sto guardando, il 'turn' che segue si
      // occupa di ricaricarne il contenuto.
      refreshSessions();
      refreshProgetti();
    } else if (event.type === 'progetto') {
      // Un progetto e' cambiato: la memoria scritta a fine turno, una voce
      // corretta dall'altro schermo, il nome.
      progettoCambiato(event);
    }
  };
}

/** Mostra una conversazione. Non ferma nulla: il turno eventualmente in
 *  corso continua sul server, e se e' proprio quello della chat che si apre
 *  ci si riattacca allo stream, ricostruendo pensiero e tool gia' eseguiti. */
/** Mostra una conversazione.
 *
 * ``entrando`` distingue i due gesti che passano di qui: **entrare** in una
 * chat (dalla colonna, una nuova, l'avvio della pagina) e **rileggerla**
 * mentre ci si e' gia' dentro (fine turno, riallineamento dopo una caduta).
 * Sembrano la stessa cosa e non lo sono: entrando l'anteprima va chiusa,
 * rileggendo va lasciata dov'e' -- chiuderla a fine turno vorrebbe dire far
 * sparire da sola la pagina che l'agente ha appena finito di costruire.
 */
async function showSession(payload, { entrando = false } = {}) {
  const conversationChanged = state.sessionId !== payload.session_id;
  state.sessionId = payload.session_id;
  // Il workspace segue la conversazione: aprendo una chat di ieri il server
  // ci rimette sulla cartella su cui era stata fatta, e qui se ne prende
  // atto -- percorso in testa, scheda della sandbox, cartelle recenti.
  // Solo se e' davvero cambiato: showSession gira anche ad ogni evento del
  // bus, e un avviso per ogni fine turno sarebbe rumore.
  if (payload.workspace_dir && payload.workspace_dir !== state.settings.workspace_dir) {
    // "Ora si lavora in" e non "workspace della chat": la cartella e' una per
    // processo, e questo messaggio dice dov'e' finito l'harness -- vale anche
    // quando a spostarlo e' stato l'altro schermo.
    dopoIlCambio(payload, 'Ora si lavora in: ' + nomeCartella(payload.workspace_dir));
  }
  // I numeri del cruscotto sono di **quella** conversazione: cambiando chat
  // vanno via subito, prima ancora di disegnare la nuova. Rileggere la stessa
  // chat (fine turno, riallineamento) invece non li tocca: la traccia dal
  // vivo del turno appena finito esiste solo in questa pagina.
  if (conversationChanged && typeof window !== 'undefined') window.Cruscotto?.reset();
  // Il progetto della chat: la goccia in alto e la memoria a destra.
  allineaProgettoDellaChat(payload);
  // Il piano arriva dal payload, non dagli eventi: riaprendo una chat vecchia
  // il pannello si ripopola anche se il turno che l'ha scritto e' finito ieri.
  renderPlan(payload.plan);
  renderNotes(payload.notes);
  // Chiusa entrando, tranne dove l'agente sta lavorando **adesso**: li' il
  // pannello e' parte di cio' che si sta guardando accadere, e lo stream che
  // sta per riattaccarsi lo riaprirebbe comunque un istante dopo.
  renderPreview(payload.preview, { apri: !entrando || Boolean(payload.running) });
  state.attachAbort?.abort();
  state.attachToken += 1;                 // invalida un eventuale attach vecchio
  state.history = {
    msgs: payload.messages || [],
    offset: payload.messages_offset || 0,
    total: payload.messages_total ?? (payload.messages || []).length,
    loading: false,
  };
  renderHistory(state.history.msgs);
  if (payload.stats) applyStats(payload.stats);
  if (payload.running) state.running.add(payload.session_id);
  else state.running.delete(payload.session_id);
  setRunning(payload.session_id, payload.running);
  if (conversationChanged && typeof window !== 'undefined') {
    window.HarnessCompanion?.setState(payload.running ? 'working' : 'idle');
  }
  if (payload.running) attachStream(payload.session_id);
}

async function openSession(id) {
  // Aprire una conversazione e' il gesto con cui si esce dalla schermata del
  // progetto: si resta nel progetto, ma davanti c'e' una chat.
  const veniva_dalla_home = state.progettoHome;
  if (state.progettoHome) mostraProgettoHome(false);
  if (id === state.sessionId) {
    // Stessa chat di prima: non si riapre (aprire e' un gesto che sposta il
    // workspace e ferma le anteprime), ma il pannello si rilegge: se nel
    // frattempo il turno era andato avanti, il piano mostrato era quello di
    // quando si era usciti.
    if (veniva_dalla_home) {
      try {
        const payload = await api(`/api/sessions/${encodeURIComponent(id)}`);
        renderPlan(payload.plan);
        renderNotes(payload.notes);
        renderPreview(payload.preview);
        if (payload.stats) applyStats(payload.stats);
      } catch { /* il pannello resta com'era */ }
    }
    return;
  }
  markActive(id);          // feedback immediato: la riga si accende subito
  try {
    // showSession -> applyStats aggiorna gia' la sidebar: nessuna seconda GET.
    await showSession(await api(`/api/sessions/${id}/open`, { method: 'POST' }), {
      entrando: true,
    });
  } catch (error) {
    markActive(state.sessionId);
    toast(error.message);
  }
}

async function newSession() {
  try {
    if (state.progettoHome) mostraProgettoHome(false);
    await showSession(await api('/api/sessions', { method: 'POST' }), { entrando: true });
  } catch (error) { toast(error.message); }
}

/** "Nuova chat" in cima alla colonna: una conversazione **libera**.
 *
 *  Le chat di un progetto nascono dalla sua schermata (o dal menu del
 *  progetto). Da dentro un progetto, quindi, questo pulsante porta fuori:
 *  nell'ultima cartella libera usata. Senza una cartella libera recente la
 *  chat nasce dove si e', e lo si dice.
 */
async function nuovaChatLibera() {
  if (state.progetto) {
    const libera = (state.settings.recent_workspaces || []).find((p) => !prDelPercorso(p));
    if (libera) {
      await useWorkspace(libera);
    } else {
      toast(`Nessuna cartella libera recente: la chat nasce nel progetto «${state.progetto.nome}»`);
    }
  }
  await newSession();
}

async function deleteSession(id) {
  try {
    const payload = await api(`/api/sessions/${id}`, { method: 'DELETE' });
    if (id === state.sessionId) await showSession(payload, { entrando: true });
    refreshSessions();
  } catch (error) { toast(error.message); }
}

// ---------------------------------------------------------------------------
// Impostazioni
// ---------------------------------------------------------------------------

/** Ridipinge l'intestazione da ``state.settings``.
 *
 * Prima questi tre valori venivano scritti una volta sola all'avvio: cambiare
 * modello nelle impostazioni aggiornava lo stato ma non quello che si legge in
 * alto, e il nome giusto compariva solo dopo il primo turno. Sapere quale
 * modello sta per rispondere deve costare un'occhiata, non una prova.
 */
function renderHeader() {
  const s = state.settings;
  // L'intestazione e il pannello impostazioni sono due viste della stessa cosa.
  // Ogni percorso che cambia un'impostazione passa gia' di qui -- e' l'unico
  // motivo per cui la barra in alto non resta mai indietro: agganciando qui il
  // riallineamento dei campi, quella garanzia vale per tutti e due i posti
  // insieme, invece che per uno solo con l'altro da ricordarsi a mano.
  syncSettingsWidgets();
  $('#top-title').textContent = s.model_name || '—';
  $('#top-sub').textContent = s.workspace_dir || '';
  $('#ws-chip').title = s.workspace_dir
    ? `${s.workspace_dir} — premi per cambiare cartella`
    : 'Cambia cartella di lavoro';
  $('#pill-ctx').textContent = 'ctx ' + Math.round((s.num_ctx || 0) / 1024) + 'k';
}

async function saveSettings(patch) {
  const prima = {};
  Object.keys(patch).forEach((k) => { prima[k] = state.settings[k]; });
  Object.assign(state.settings, patch);
  // Ottimistico: l'intestazione segue il campo appena toccato, senza aspettare
  // il giro sul server.
  renderHeader();
  try {
    const data = await api('/api/settings', {
      method: 'POST',
      body: JSON.stringify({ values: patch }),
    });
    state.settings = data.settings;
    if (data.stats) applyStats(data.stats);
  } catch (error) {
    // Il valore ottimistico non e' mai arrivato sul server: tenerlo vorrebbe
    // dire mostrare una scelta che al prossimo avvio non c'e'. Si torna a
    // quello di prima, ma solo dove nessun salvataggio piu' recente l'ha gia'
    // cambiato di nuovo.
    Object.keys(prima).forEach((k) => {
      if (state.settings[k] === patch[k]) state.settings[k] = prima[k];
    });
    throw error;
  } finally {
    renderHeader();
  }
}

// Ogni campo legato a un'impostazione, registrato da bindField.
//
// Serve a rispondere una volta sola alla domanda "chi mostra questo valore?".
// Prima la risposta era sparsa: la goccia in alto la riallineava renderHeader,
// la tendina del modello una riga dentro pickModel, sei campi del profilo una
// mappa scritta a mano dentro il pulsante "Applica i consigliati", l'immagine
// del container un'altra riga dentro il pulsante della build. Quattro copie
// della stessa idea, e ognuna copriva i campi che si ricordava chi l'ha
// scritta: cambiando modello dalla goccia in alto la tendina delle
// impostazioni restava indietro, perche' nessuno l'aveva messa in elenco.
const BOUND_FIELDS = [];

/** Riporta un campo al valore che ha nelle impostazioni. */
function applyField({ id, key }) {
  const node = $(id);
  if (!node) return;
  const value = state.settings[key];
  if (node.type === 'checkbox') {
    node.checked = !!value;
  } else {
    // Un <select> puo' non avere ancora l'opzione -- l'elenco dei modelli
    // arriva dall'endpoint e puo' essere piu' vecchio della scelta. Senza
    // questa riga l'assegnazione fallirebbe in silenzio, che e' esattamente
    // il modo in cui un widget resta indietro senza che nessuno se ne accorga.
    if (node.tagName === 'SELECT' && value != null
        && !Array.from(node.options).some((o) => o.value === String(value))) {
      const opt = el('option');
      opt.value = String(value);
      opt.textContent = String(value);
      node.appendChild(opt);
    }
    const testo = value == null ? '' : String(value);
    if (node.value !== testo) node.value = testo;
  }
  // Riallineato al valore salvato: se era segnato come non valido, non lo e'
  // piu'.
  node.removeAttribute('aria-invalid');
}

/** Riallinea tutti i campi delle impostazioni a ``state.settings``.
 *
 * Salta quello che ha il fuoco: su un campo di testo il salvataggio parte ad
 * ogni tasto, e riscrivergli dentro il valore mentre l'utente digita gli
 * sposterebbe il cursore in fondo alla riga.
 */
function syncSettingsWidgets() {
  const attivo = document.activeElement;
  BOUND_FIELDS.forEach((campo) => {
    if ($(campo.id) !== attivo) applyField(campo);
  });
  // I controlli disegnati sopra i campi (segmenti, cursori, stati) e le voci
  // che compaiono solo con certe scelte seguono lo stesso giro: e' il motivo
  // per cui il menu non resta indietro quando si cambia qualcosa da fuori.
  if (typeof impRiallinea === 'function') impRiallinea();
}

/**
 * Lega un campo a un'impostazione: valore iniziale, salvataggio, riallineamento.
 *
 * ``transform`` converte il valore grezzo del controllo. Se restituisce
 * ``undefined`` il valore non e' valido -- un numero fuori dai limiti, un campo
 * vuoto -- e non si salva: il campo resta segnato con ``aria-invalid`` finche'
 * non torna valido.
 *
 * ``ritardo`` (ms) e' per i campi che si scrivono: si salva quando si smette
 * di scrivere, e subito al ``change`` (uscita dal campo, Invio). Salvare a ogni
 * tasto voleva dire, sull'indirizzo del server, ricostruire il backend e
 * rifare i controlli di prontezza per ogni cifra di un IP.
 */
function bindField(id, key, transform = (v) => v, { ritardo = 0 } = {}) {
  const node = $(id);
  if (!node) return;
  BOUND_FIELDS.push({ id, key });
  applyField({ id, key });
  let attesa = null;
  const salva = () => {
    clearTimeout(attesa);
    attesa = null;
    const raw = node.type === 'checkbox' ? node.checked : node.value;
    const out = transform(raw);
    if (out === undefined) {
      node.setAttribute('aria-invalid', 'true');
      return;
    }
    node.removeAttribute('aria-invalid');
    if (state.settings[key] === out) return;
    const salvataggio = saveSettings({ [key]: out });
    // Chi mostra lo stato del salvataggio (la spia del menu) lo sa da qui.
    document.dispatchEvent(new CustomEvent('impostazioni:salvataggio', {
      detail: { key, salvataggio },
    }));
    salvataggio.catch((e) => toast(e.message));
  };
  const evento = node.tagName === 'SELECT' || node.type === 'checkbox' ? 'change' : 'input';
  node.addEventListener(evento, () => {
    if (!ritardo) { salva(); return; }
    clearTimeout(attesa);
    attesa = setTimeout(salva, ritardo);
  });
  if (ritardo) node.addEventListener('change', salva);
}

function fillModels(models) {
  const datalist = $('#s-model-list');
  if (datalist) {
    datalist.innerHTML = '';
    (models || []).forEach((name) => {
      const option = el('option');
      option.value = name;
      datalist.appendChild(option);
    });
  }
  const input = $('#s-model-name');
  if (input && state.settings.model_name && input.value !== state.settings.model_name && document.activeElement !== input) {
    input.value = state.settings.model_name;
  }
  const list = models && models.length ? [...models] : (state.settings.model_name ? [state.settings.model_name] : []);
  if (state.settings.model_name && !list.includes(state.settings.model_name)) list.unshift(state.settings.model_name);
  state.models = list;
  if (!$('#model-menu').hidden) renderModelMenu();
}

// ---------------------------------------------------------------------------
// Selettore del modello nella barra in alto
// ---------------------------------------------------------------------------
//
// Il modello era un'etichetta e cambiarlo voleva dire aprire le impostazioni,
// scorrere fino alla tendina e chiudere: quattro gesti per una cosa che si fa
// più volte in una sessione di prove. Qui è la stessa tendina, agganciata al
// posto dove il modello si legge già.

function renderModelMenu() {
  const menu = $('#model-menu');
  const elenco = state.models || [];
  menu.innerHTML = '';
  if (!elenco.length) {
    menu.innerHTML = '<div class="empty">Nessun modello sull\'endpoint. '
      + 'Controlla l\'indirizzo in Impostazioni.</div>';
    return;
  }
  elenco.forEach((nome) => {
    const scelto = nome === state.settings.model_name;
    const opt = el('button', 'model-opt' + (scelto ? ' on' : ''));
    opt.type = 'button';
    opt.setAttribute('role', 'option');
    opt.setAttribute('aria-selected', String(scelto));
    opt.innerHTML = `<span class="model-tick">${scelto ? '✓' : ''}</span>`
      + `<span class="model-name">${esc(nome)}</span>`;
    opt.onclick = () => pickModel(nome);
    menu.appendChild(opt);
  });
}

function toggleModelMenu(aprire) {
  const menu = $('#model-menu');
  const chip = $('#model-chip');
  const apri = aprire ?? menu.hidden;
  if (apri) renderModelMenu();
  menu.hidden = !apri;
  chip.setAttribute('aria-expanded', String(apri));
}

async function pickModel(nome) {
  toggleModelMenu(false);
  const precedente = state.settings.model_name;
  if (nome === precedente) return;
  try {
    await saveSettings({ model_name: nome });
    refreshProfile();
    // Il caricamento in VRAM lo paghiamo adesso, mentre l'utente sta ancora
    // guardando la tendina, invece che sul primo messaggio.
    api('/api/preload', { method: 'POST' }).catch(() => {});
    toast(`Modello: ${nome}`);
  } catch (error) {
    // saveSettings aggiorna lo stato in modo ottimistico e non lo riporta
    // indietro da sé: senza questo, la barra continuerebbe a mostrare un
    // modello che il server non ha accettato.
    state.settings.model_name = precedente;
    renderHeader();
    toast(error.message);
  }
}

function bindModelChip() {
  $('#model-chip').onclick = (event) => {
    event.stopPropagation();
    toggleWsMenu(false);          // due tendine aperte insieme si coprirebbero
    toggleModelMenu();
  };
  // Chiusura su clic fuori e su Esc: una tendina che resta aperta mentre si
  // lavora altrove copre il thread.
  document.addEventListener('click', (event) => {
    if (!$('#model-menu').hidden && !$('#model-menu').contains(event.target)) {
      toggleModelMenu(false);
    }
    if (!$('#ws-menu').hidden && !$('#ws-menu').contains(event.target)) {
      toggleWsMenu(false);
    }
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') { toggleModelMenu(false); toggleWsMenu(false); }
  });
}

// ---------------------------------------------------------------------------
// Cartella di lavoro
// ---------------------------------------------------------------------------
//
// Il percorso in alto a sinistra e' anche il comando per cambiarlo. La tendina
// elenca le ultime cartelle usate -- chi alterna due progetti torna indietro
// con un clic invece che con un giro nel selettore di sistema -- e in fondo
// tiene le due azioni che restano: scegliere una cartella nuova (dialogo del
// sistema operativo, non un elenco ridisegnato in pagina) e aprire questa.

function nomeCartella(path) {
  const parti = String(path || '').split(/[\\/]/).filter(Boolean);
  return parti[parti.length - 1] || String(path || '');
}

function renderWsMenu() {
  const menu = $('#ws-menu');
  menu.innerHTML = '';
  const corrente = state.settings.workspace_dir || '';
  const recenti = (state.settings.recent_workspaces || []).filter((p) => p && p !== corrente);

  if (recenti.length) {
    menu.appendChild(el('div', 'menu-label', 'Cartelle recenti'));
    recenti.slice(0, 5).forEach((path) => {
      const voce = el('button', 'model-opt ws-opt');
      voce.title = path;
      voce.innerHTML = `<span class="ws-opt-name">${esc(nomeCartella(path))}</span>`
        + `<span class="ws-opt-path">${esc(path)}</span>`;
      voce.onclick = () => useWorkspace(path);
      menu.appendChild(voce);
    });
    menu.appendChild(el('div', 'menu-sep'));
  }

  const sfoglia = el('button', 'model-opt', 'Scegli un\'altra cartella…');
  sfoglia.onclick = () => { toggleWsMenu(false); browseWorkspace(); };
  const apri = el('button', 'model-opt', 'Apri in Esplora risorse');
  apri.onclick = () => {
    toggleWsMenu(false);
    api('/api/workspace/open', { method: 'POST' }).catch((e) => toast(e.message));
  };
  menu.append(sfoglia, apri);

  // I rimedi dell'ambiente stanno accanto alla cosa da rimediare. Container e
  // immagine si preparano da soli quando si cambia cartella: questi due sono
  // per il caso in cui qualcosa è andato storto — un Dockerfile modificato a
  // mano, un container morto — e allora si rifà. Sono di *questa cartella*,
  // non dell'applicazione: è per questo che vivono qui e non fra le
  // impostazioni, dove sembravano due manopole da usare invece che due rimedi.
  menu.appendChild(el('div', 'menu-sep'));
  menu.appendChild(el('div', 'menu-label', 'Impostazioni'));
  const container = el('button', 'model-opt', 'Crea il container');
  container.title = 'Ricrea il container di questa cartella, se manca o è rotto';
  container.onclick = () => {
    toggleWsMenu(false);
    runPrep('/api/prep/container', 'Preparo il container…');
  };
  const immagine = el('button', 'model-opt', 'Crea l\'immagine');
  immagine.title = 'Ricostruisce l\'immagine di questa cartella dal suo Dockerfile';
  immagine.onclick = () => {
    toggleWsMenu(false);
    runPrep('/api/prep/image', 'Costruisco l\'immagine…');
  };
  menu.append(container, immagine);
}

function toggleWsMenu(aprire) {
  const menu = $('#ws-menu');
  if (!menu) return;
  const apri = aprire ?? menu.hidden;
  if (apri) renderWsMenu();
  menu.hidden = !apri;
  $('#ws-chip').setAttribute('aria-expanded', String(apri));
}

/** Applica la cartella scelta e riallinea tutto quello che ne dipende. */
function dopoIlCambio(data, messaggio) {
  state.settings.workspace_dir = data.workspace_dir;
  if (data.recent_workspaces) state.settings.recent_workspaces = data.recent_workspaces;
  // Cambiare cartella verso un posto che non e' il progetto aperto vuol dire
  // esserne usciti: la memoria a destra non deve restare a descrivere un
  // progetto in cui non si sta piu'. Chi apre un progetto lo sa gia'.
  if (!data.progetto) {
    const suo = prDelPercorso(data.workspace_dir);
    if (state.progetto && (!suo || prChiave(suo.path) !== prChiave(state.progetto.path))) {
      if (state.progettoHome) mostraProgettoHome(false);
    }
    state.progetto = suo;
    renderMemoriaPannello();
    renderProgettoChip();
  }
  renderHeader();
  refreshSandbox();          // workspace nuovo = container nuovo
  if (data.stats) applyStats(data.stats);
  // Il server può aver già avviato la build dell'immagine per la cartella
  // appena scelta: la prontezza lo racconta, e il polling si ferma da solo.
  refreshReadiness();
  const costruendo = data.jobs && data.jobs.image.state === 'running';
  toast(costruendo ? `${messaggio} — costruisco l'immagine…` : messaggio);
}

async function useWorkspace(path) {
  toggleWsMenu(false);
  if (!path || path === state.settings.workspace_dir) return;
  try {
    const data = await api('/api/workspace', {
      method: 'POST', body: JSON.stringify({ path }),
    });
    dopoIlCambio(data, 'Workspace: ' + data.workspace_dir);
  } catch (error) { toast(error.message); }
}

// Il selettore e' quello del sistema operativo, aperto dal server: e' la
// stessa macchina, ed e' piu' rapido e piu' familiare di qualunque elenco
// ridisegnato dentro la pagina.
async function browseWorkspace() {
  // Nessun bottone da spegnere e riaccendere: il gesto parte dalla tendina
  // della cartella (che si chiude subito) o dalla schermata di prontezza. Il
  // dialogo di sistema e' modale e blocca gia' lui, quindi non serve altro.
  if (browseWorkspace._aperto) return;
  browseWorkspace._aperto = true;
  try {
    const data = await api('/api/workspace/pick', { method: 'POST' });
    if (!data.cancelled) dopoIlCambio(data, 'Workspace: ' + data.workspace_dir);
  } catch (error) { toast(error.message); }
  browseWorkspace._aperto = false;
}

// ---------------------------------------------------------------------------
// Tema e layout
// ---------------------------------------------------------------------------

function applyTheme(mode) {
  document.documentElement.dataset.theme = mode;
  state.settings.theme_mode = mode;
  $('#theme-toggle').textContent = mode === 'dark' ? '☾' : '☀';
  try { localStorage.setItem('ah-theme', mode); } catch { /* modalita' privata */ }
}

/** Colonne laterali visibili o no, dalle impostazioni.
 *
 * Una funzione sola per i tre posti che le cambiano -- i due ☰ in alto, il
 * menu Aspetto, un'importazione -- e per l'avvio. Il titolo dei pulsanti dice
 * cosa succede al prossimo clic, non come si chiama la cosa: "Pannello
 * destro" su un pulsante che lo nasconde non aiuta nessuno.
 */
function applicaColonne() {
  const app = $('#app');
  const senzaBarra = state.settings.show_left_sidebar === false;
  const senzaPannello = state.settings.show_right_panel === false;
  app.classList.toggle('no-sidebar', senzaBarra);
  app.classList.toggle('no-panel', senzaPannello);
  $('#toggle-sidebar').title = senzaBarra ? 'Mostra la barra laterale' : 'Nascondi la barra laterale';
  $('#toggle-panel').title = senzaPannello ? 'Mostra il pannello destro' : 'Nascondi il pannello destro';
}

// ---------------------------------------------------------------------------
// Avvio
// ---------------------------------------------------------------------------

async function boot() {
  const data = await api('/api/bootstrap');
  state.settings = data.settings;
  state.memories = data.memories;
  state.backend = data.backend;
  state.app = data.app;

  // Il server e' la fonte di verita'; localStorage e' solo una scorciatoia per
  // non far lampeggiare il tema sbagliato prima che /api/bootstrap risponda.
  let theme = state.settings.theme_mode;
  try { theme = localStorage.getItem('ah-theme') || theme; } catch { /* ignora */ }
  applyTheme(theme);

  applicaColonne();

  $('#brand-name').textContent = data.app.name;
  $('#brand-sub').textContent = 'v' + data.app.version;

  const backend = data.backend;
  // ``online: null`` = non ancora chiesto: la goccia dice "controllo…". Lo
  // stato vero arriva da ``sondaBackend()``, che parte piu' sotto senza
  // bloccare niente.
  renderStatusPill(backend.online, backend.detail);
  $('#pill-backend').textContent = backend.name;
  renderHeader();

  await showSession(data.session, { entrando: true });
  window.HarnessCompanion?.init({
    mounts: ['#sidebar .logo', '#composer-companion'],
    state: data.session.running ? 'working' : 'welcome',
  });
  bindGlobalEvents();
  bindSveglie();
  refreshSandbox();
  bindProgettiUI();
  // Ricaricando la pagina dentro un progetto si torna nella chat di prima, non
  // sulla sua schermata: si stava lavorando, non scegliendo dove. Il ramo del
  // progetto parte aperto (lo apre ``allineaProgettoDellaChat``), e la
  // memoria a destra sa dove si e'.
  await refreshProgetti();
  allineaProgettoDellaChat(data.session);
  if (state.progetto) caricaChatProgetto(state.progetto.path);
  refreshSessions();
  // Scalda il modello mentre l'utente legge la pagina: i 4-15 s di
  // caricamento in VRAM li paghiamo adesso invece che sul primo messaggio.
  // Volutamente senza await: se Ollama e' spento non deve bloccare l'avvio.
  api('/api/preload', { method: 'POST' }).catch(() => {});
  // La goccia si ricontrolla da sola da qui in avanti.
  avviaPollingEndpoint();
  // Anche questa senza await: e' l'unica cosa dell'avvio che dipende da
  // un'altra macchina, ed e' esattamente quella che non deve tenere ferma la
  // pagina. Vedi ``/api/bootstrap``.
  sondaBackend();

  // Il menu delle impostazioni si costruisce dal suo schema (impostazioni.js)
  // e lega ogni campo con bindField: una volta, adesso che le impostazioni
  // ci sono.
  impMonta();
  bindPreviewResize();
  bindColumnResize();
  bindProgettiResize();
  bindModelChip();
  $('#ov-close').onclick = closePreview;
  $('#ov-reload').onclick = () => openPreview(state.previewShown);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closePreview();
  });
  refreshProfile();
}

function wireUi() {
  $('#new-chat').onclick = nuovaChatLibera;
  // Il tema e i due pannelli vivevano solo nel browser: adesso che le
  // impostazioni stanno su disco, seguono l'utente anche da un'altra finestra.
  // localStorage resta per il tema, perche' evita il lampo di tema sbagliato
  // fra il caricamento della pagina e la risposta di /api/bootstrap.
  $('#theme-toggle').onclick = () => {
    const mode = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    applyTheme(mode);
    saveSettings({ theme_mode: mode }).catch(() => { /* il tema e' gia' applicato */ });
  };
  $('#toggle-sidebar').onclick = () => {
    saveSettings({ show_left_sidebar: state.settings.show_left_sidebar === false }).catch(() => {});
    applicaColonne();
  };
  $('#toggle-panel').onclick = () => {
    saveSettings({ show_right_panel: state.settings.show_right_panel === false }).catch(() => {});
    applicaColonne();
  };
  applicaColonne();
  // Apertura, chiusura e scorciatoie del menu delle impostazioni: la finestra
  // si costruisce dopo, al boot, quando le impostazioni sono arrivate.
  impCollega();

  $('#send').onclick = send;
  // Goccia "Ricerca online": toggle puro lato client. Lo stato vero vive nel
  // flag che send() manda col messaggio: qui si accende e basta, cosi'
  // l'utente puo' cambiar idea finche' non preme invio.
  const gocciaWeb = $('#toggle-web-search');
  if (gocciaWeb) {
    gocciaWeb.onclick = () => {
      const on = gocciaWeb.classList.toggle('on');
      gocciaWeb.setAttribute('aria-pressed', on ? 'true' : 'false');
    };
  }
  // Goccia "Pensiero": tendina Low/Medium/High/Auto. Lo stato vive in
  // state.thinkLevel e viaggia solo sull'invio, come web_search; "auto" non
  // parte nemmeno sulla rete perche' non e' un override, e' l'assenza di uno.
  const LIVELLI_THINK = [
    { v: 'low', nome: 'Low', desc: 'ragionamento breve' },
    { v: 'medium', nome: 'Medium', desc: 'ragionamento medio' },
    { v: 'high', nome: 'High', desc: 'ragionamento esteso' },
    { v: 'auto', nome: 'Auto', desc: 'come da impostazioni' },
  ];
  const gocciaThink = $('#toggle-think');
  const thinkMenu = $('#think-menu');
  const thinkLabel = $('#think-label');
  function toggleThinkMenu(aprire) {
    const visibile = typeof aprire === 'boolean' ? aprire : thinkMenu.hidden;
    thinkMenu.hidden = !visibile;
    gocciaThink.setAttribute('aria-expanded', visibile ? 'true' : 'false');
  }
  if (gocciaThink && thinkMenu && thinkLabel) {
    thinkMenu.innerHTML = LIVELLI_THINK.map((livello) =>
      `<button class="model-opt" role="option" type="button" data-think="${livello.v}">` +
      `<span class="model-tick"></span><span class="model-name">${livello.nome}</span>` +
      `<span class="menu-desc">${livello.desc}</span></button>`
    ).join('');
    const segnaSelezionato = () => {
      $$('#think-menu .model-opt').forEach((b) =>
        b.classList.toggle('on', b.dataset.think === state.thinkLevel));
    };
    segnaSelezionato();
    thinkLabel.textContent =
      (LIVELLI_THINK.find((l) => l.v === state.thinkLevel) || LIVELLI_THINK[3]).nome;
    gocciaThink.onclick = (event) => {
      event.stopPropagation();
      // Due tendine aperte insieme si coprirebbero: chiudi le altre due.
      const menuModelli = $('#model-menu');
      const menuWs = $('#ws-menu');
      if (menuModelli && !menuModelli.hidden) toggleModelMenu(false);
      if (menuWs && !menuWs.hidden) toggleWsMenu(false);
      segnaSelezionato();
      toggleThinkMenu();
    };
    thinkMenu.onclick = (event) => {
      const scelta = event.target.closest('[data-think]');
      if (!scelta) return;
      state.thinkLevel = scelta.dataset.think;
      thinkLabel.textContent = scelta.querySelector('.model-name').textContent;
      segnaSelezionato();
      toggleThinkMenu(false);
    };
    document.addEventListener('click', (event) => {
      if (!thinkMenu.hidden && !thinkMenu.contains(event.target) &&
          event.target !== gocciaThink) toggleThinkMenu(false);
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') toggleThinkMenu(false);
    });
  }
  const quickContinue = $('#btn-quick-continue');
  if (quickContinue) {
    quickContinue.onclick = () => {
      if (state.busy) return;
      const box = $('#composer textarea');
      box.value = 'continua';
      send();
    };
  }

  // --- allegati ---
  const picker = $('#attach-input');
  picker.onchange = () => { uploadAttachments(picker.files); picker.value = ''; };
  $('#attach').onclick = () => picker.click();

  const composer = $('#composer');
  ['dragenter', 'dragover'].forEach((type) =>
    composer.addEventListener(type, (event) => {
      event.preventDefault();
      composer.classList.add('dragging');
    }),
  );
  ['dragleave', 'drop'].forEach((type) =>
    composer.addEventListener(type, () => composer.classList.remove('dragging')),
  );
  composer.addEventListener('drop', (event) => {
    event.preventDefault();
    uploadAttachments(event.dataTransfer?.files);
  });

  const box = $('#composer textarea');
  box.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); send(); }
  });
  box.addEventListener('input', () => {
    box.style.height = 'auto';
    box.style.height = Math.min(box.scrollHeight, 200) + 'px';
  });

  // L'auto-scroll si disattiva se l'utente risale a leggere: senza questo,
  // lo streaming strapperebbe via la pagina mentre si guarda indietro.
  const scroller = $('#scroller');
  scroller.addEventListener('scroll', () => {
    const distance = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
    state.autoScroll = distance < 90;
    // Risalita: si chiede il blocco precedente **prima** di toccare il bordo,
    // cosi' chi scorre veloce non trova il muro. 220px sono circa due righe
    // di thread: abbastanza per far arrivare la risposta, non tanto da
    // caricare tre blocchi in un colpo di rotella.
    //
    // "Risalita" alla lettera: deve esserci un movimento verso l'alto. Vicino
    // alla cima ci si passa anche scendendo -- ed e' quello che fa ogni
    // ancoraggio al fondo di una chat appena aperta: senza questa condizione
    // il gesto di *entrare* in una conversazione ne chiedeva la cronologia,
    // e il ripristino della posizione la inchiodava al primo messaggio.
    const sale = scroller.scrollTop < state.ultimoScrollTop;
    state.ultimoScrollTop = scroller.scrollTop;
    if (sale && scroller.scrollTop < 220) caricaPrecedenti();
  });

  // --- ricerca nelle conversazioni ---
  // Con un ritardo: a 40 conversazioni ogni tasto premuto sarebbe una lettura
  // dell'indice, e l'utente sta ancora scrivendo la parola.
  const cerca = $('#session-search');
  let attesa = null;
  const aggiorna = () => {
    $('#search-clear').hidden = !cerca.value;
    clearTimeout(attesa);
    attesa = setTimeout(() => cercaConversazioni(cerca.value), 180);
  };
  cerca.addEventListener('input', aggiorna);
  cerca.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') { event.stopPropagation(); cerca.value = ''; aggiorna(); }
  });
  $('#search-clear').onclick = () => { cerca.value = ''; aggiorna(); cerca.focus(); };

  $('#ws-chip').onclick = (event) => {
    event.stopPropagation();
    toggleModelMenu(false);
    toggleWsMenu();
  };
}

wireUi();
boot().catch((error) => {
  // Un pannello SOPRA la pagina, non al posto della pagina.
  // `document.body.innerHTML = ...` distruggeva tutto il DOM: dopo, nessun
  // ascoltatore era più agganciato a niente e l'unico modo di riprovare era
  // F5. E il caso tipico è il server che parte due secondi dopo il browser,
  // cioè quello in cui basterebbe riprovare.
  const pannello = document.createElement('div');
  pannello.id = 'avvio-fallito';
  pannello.style.cssText =
    'position:fixed;inset:0;z-index:9999;display:flex;align-items:center;' +
    'justify-content:center;background:rgba(16,20,24,.94);' +
    'font-family:system-ui;text-align:center;padding:40px';
  pannello.innerHTML =
    `<div>
       <h2 style="margin:0 0 8px">Impossibile contattare il server</h2>
       <p style="color:#888;margin:0 0 4px">${esc(error.message)}</p>
       <p style="color:#888;margin:0 0 18px">Avvia l'harness con <code>python run.py</code>.</p>
       <button id="riprova-avvio" style="padding:8px 18px;border-radius:9px;cursor:pointer">Riprova</button>
     </div>`;
  document.body.appendChild(pannello);
  $('#riprova-avvio').onclick = () => {
    pannello.remove();
    boot().catch(() => document.body.appendChild(pannello));
  };
});
