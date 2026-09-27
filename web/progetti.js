/* Progetti — la colonna, la schermata del progetto, le finestre, la memoria.
 *
 * Un progetto e' una cartella con un nome, le chat fatte li' dentro e una
 * memoria che vale per tutte (core/progetto.py). Fino al 26/09/2026 si
 * chiamava "vault" e la sua memoria non la scriveva nessuno; adesso la scrive
 * l'harness a fine turno (core/memoria_progetto.py) e qui si legge, si
 * corregge e si aggiunge.
 *
 * Caricato prima di app.js, come impostazioni.js: usa ``state``, ``api``,
 * ``$``, ``esc``, ``el``, ``toast``, ``relTime``, ``markdown`` e le funzioni
 * delle conversazioni di app.js solo quando viene chiamato, mai al caricamento.
 */

'use strict';

// ---------------------------------------------------------------------------
// Vocabolario
// ---------------------------------------------------------------------------

// I tipi della memoria, nell'ordine in cui si leggono: dal piu' stabile al
// piu' volatile. Stesso ordine di ``progetto.TIPI_MEMORIA``.
const PR_TIPI = [
  { k: 'decisione', gruppo: 'Decisioni', uno: 'Decisione' },
  { k: 'convenzione', gruppo: 'Convenzioni', uno: 'Convenzione' },
  { k: 'fatto', gruppo: 'Fatti', uno: 'Fatto' },
  { k: 'scartato', gruppo: 'Strade scartate', uno: 'Strada scartata' },
  { k: 'aperto', gruppo: 'Lavori aperti', uno: 'Lavoro aperto' },
];
const PR_MAX_VOCE = 240;
const PR_MAX_VOCI = 40;
const PR_MAX_ISTRUZIONI = 2000;

// Come si e' chiuso l'ultimo turno di una chat, detto per chi riprende.
const PR_STATI = {
  completed: ['completata', 'ok'],
  in_corso: ['in corso', 'vivo'],
  in_attesa: ['aspetta una tua risposta', 'warn'],
  max_steps: ['fermata: passi finiti', 'warn'],
  stallo: ['fermata: non avanzava', 'warn'],
  stopped: ['interrotta', ''],
  error: ['finita con un errore', 'err'],
  finestra_piena: ['fermata: contesto pieno', 'warn'],
  awaiting_user: ['aspetta una tua risposta', 'warn'],
};

const PR_ICONE = {
  piu: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M8 3v10M3 8h10"/></svg>',
  puntini: '<svg viewBox="0 0 16 16" fill="currentColor"><circle cx="3.5" cy="8" r="1.3"/><circle cx="8" cy="8" r="1.3"/><circle cx="12.5" cy="8" r="1.3"/></svg>',
  matita: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"><path d="M10.8 2.8l2.4 2.4-7.6 7.6-3 .6.6-3z"/></svg>',
  x: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M4 4l8 8M12 4l-8 8"/></svg>',
  cartella: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"><path d="M1.8 4.2h4l1.2 1.6h7.2v7A1.2 1.2 0 0 1 13 14H3a1.2 1.2 0 0 1-1.2-1.2z"/></svg>',
  ingranaggio: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3.1"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
  freccia: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M8 13V3M3.5 7.5L8 3l4.5 4.5"/></svg>',
  apri: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3.5h6.5V10M12.3 3.7 4 12"/></svg>',
  sposta: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M1.8 4.2h4l1.2 1.6h7.2v7A1.2 1.2 0 0 1 13 14H3a1.2 1.2 0 0 1-1.2-1.2z"/><path d="M6 10h5M9 8l2 2-2 2"/></svg>',
  archivio: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"><rect x="2" y="3" width="12" height="3" rx=".8"/><path d="M3 6v6.5c0 .6.4 1 1 1h8c.6 0 1-.4 1-1V6M6.5 9h3"/></svg>',
  cestino: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2.8 4.3h10.4M6.2 4.3V3h3.6v1.3M4 4.3l.6 8.6c0 .6.5 1.1 1.1 1.1h4.6c.6 0 1.1-.5 1.1-1.1l.6-8.6"/></svg>',
  memoria: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3.5h10v9H3zM5.5 6.5h5M5.5 9.5h3"/></svg>',
  uscita: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9.5 3H3.5v10h6M7 8h7M11.5 5.5 14 8l-2.5 2.5"/></svg>',
  chat: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"><path d="M2.5 3.5h11v7.5H7l-3 2.5V11H2.5z"/></svg>',
  cerca: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><circle cx="7" cy="7" r="4.3"/><path d="m10.2 10.2 3 3"/></svg>',
};
const PR_CHEV = '<svg class="chev" width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M3.5 1.5L7 5l-3.5 3.5"/></svg>';

function prTinta(nome) {
  let h = 0;
  for (const c of String(nome || '')) h = (h * 31 + c.codePointAt(0)) >>> 0;
  return h % 6;
}

function prIniziale(nome) {
  const pulito = String(nome || '').trim();
  return pulito ? [...pulito][0].toUpperCase() : '·';
}

function prAvatar(nome, classe = '') {
  return `<span class="pr-avatar ${classe}" data-tinta="${prTinta(nome)}" aria-hidden="true">${esc(prIniziale(nome))}</span>`;
}

/** Stessa cartella, scritta in due modi? Come ``session.chiave_cartella``. */
function prChiave(path) {
  let p = String(path || '').trim().replace(/[\\/]+$/, '');
  if (/^[a-zA-Z]:/.test(p) || p.includes('\\')) p = p.replace(/\//g, '\\').toLowerCase();
  return p;
}

function prDelPercorso(path) {
  const chiave = prChiave(path);
  return chiave ? state.progetti.find((p) => prChiave(p.path) === chiave) || null : null;
}

function prTipo(k) {
  return PR_TIPI.find((t) => t.k === k) || PR_TIPI[2];
}

function prAccorcia(testo, n) {
  const s = String(testo || '').replace(/\.\.\.$/, '');
  return s.length > n ? s.slice(0, n - 1).trimEnd() + '…' : s;
}

function prQuando(iso) {
  const r = relTime(iso);
  if (!r) return '';
  return /^\d+[mhg]$/.test(r) ? `${r} fa` : r;
}

// ---------------------------------------------------------------------------
// Colonna di sinistra: l'albero dei progetti
// ---------------------------------------------------------------------------

async function refreshProgetti() {
  try {
    const data = await api('/api/progetti');
    state.progetti = data.progetti || [];
  } catch { return; /* la sezione resta com'era: non e' critica come le chat */ }
  // La scheda del progetto corrente viene dall'elenco: e' li' che arriva la
  // memoria aggiornata.
  if (state.progetto) {
    state.progetto = prDelPercorso(state.progetto.path) || null;
  }
  renderProgetti();
  renderMemoriaPannello();
  renderProgettoChip();
  const vivi = new Set(state.progetti.map((p) => prChiave(p.path)));
  [...state.progettiAperti].filter((p) => vivi.has(prChiave(p))).forEach(caricaChatProgetto);
}

function renderProgetti() {
  const root = $('#progetti');
  if (!root) return;
  root.innerHTML = '';
  if (!state.progetti.length) {
    root.innerHTML = '<div class="pr-vuoto-colonna"><strong>Nessun progetto</strong>'
      + 'Un progetto tiene insieme le chat di una cartella e una memoria che passa dall\'una all\'altra.'
      + '<br><button class="pr-bottone piccolo" type="button" data-pr="nuovo">'
      + PR_ICONE.piu + 'Nuovo progetto</button></div>';
    root.querySelector('[data-pr="nuovo"]').onclick = () => finestraNuovoProgetto();
    return;
  }
  state.progetti.forEach((p) => {
    const aperto = state.progettiAperti.has(p.path);
    const attivo = state.progetto && prChiave(state.progetto.path) === prChiave(p.path)
      && state.progettoHome;
    const nodo = el('div', 'pr-nodo' + (aperto ? ' aperto' : ''));
    nodo.setAttribute('role', 'treeitem');
    nodo.setAttribute('aria-expanded', aperto ? 'true' : 'false');
    const riga = el('div', 'pr-riga' + (attivo ? ' attiva' : '') + (p.esiste === false ? ' mancante' : ''));
    riga.innerHTML =
      `<button class="pr-freccia" type="button" title="${aperto ? 'Nascondi le chat' : 'Mostra le chat'}"
               aria-label="${aperto ? 'Nascondi' : 'Mostra'} le chat di ${esc(p.nome)}">${PR_CHEV}</button>` +
      `<button class="pr-apri" type="button" title="${esc(p.esiste === false
        ? `Cartella non trovata: ${p.path}` : [p.path, p.descrizione].filter(Boolean).join('\n'))}">` +
      `${prAvatar(p.nome)}<span class="pr-nome">${esc(p.nome)}</span>` +
      `<span class="pr-conto">${p.esiste === false ? '—' : (p.chat || 0)}</span></button>` +
      `<button class="pr-piu" type="button" aria-haspopup="menu" aria-expanded="false"
               title="Azioni sul progetto" aria-label="Azioni su ${esc(p.nome)}">${PR_ICONE.puntini}</button>`;
    riga.querySelector('.pr-freccia').onclick = () => alternaProgetto(p.path);
    riga.querySelector('.pr-apri').onclick = () => {
      if (p.esiste === false) { menuProgetto(p, riga.querySelector('.pr-piu')); return; }
      state.progettiAperti.add(p.path);
      openProgetto(p.path);
    };
    riga.querySelector('.pr-piu').onclick = (e) => { e.stopPropagation(); menuProgetto(p, e.currentTarget); };
    nodo.appendChild(riga);
    if (aperto) nodo.appendChild(ramoProgetto(p));
    root.appendChild(nodo);
  });
}

/** Le chat di un progetto, annidate sotto di lui. */
function ramoProgetto(p) {
  const ramo = el('div', 'pr-ramo');
  ramo.setAttribute('role', 'group');
  const chat = state.progettoChat[p.path];
  if (p.esiste === false) {
    ramo.innerHTML = '<div class="pr-ramo-vuoto">La cartella non c\'è più: ricollegala o togli il progetto dall\'elenco.</div>';
    return ramo;
  }
  if (!chat) {
    ramo.innerHTML = '<div class="pr-ramo-vuoto">Carico le conversazioni…</div>';
    return ramo;
  }
  if (!chat.length) {
    ramo.innerHTML = '<div class="pr-ramo-vuoto">Nessuna conversazione.</div>';
    return ramo;
  }
  chat.slice(0, 12).forEach((item) => ramo.appendChild(rigaSessione(item)));
  if (chat.length > 12) {
    const tutte = el('button', 'pr-ramo-vuoto pr-link', `Tutte le ${chat.length} conversazioni`);
    tutte.type = 'button';
    tutte.onclick = () => openProgetto(p.path);
    ramo.appendChild(tutte);
  }
  return ramo;
}

async function alternaProgetto(path) {
  if (state.progettiAperti.has(path)) {
    state.progettiAperti.delete(path);
    renderProgetti();
    return;
  }
  state.progettiAperti.add(path);
  renderProgetti();                 // subito, con "Carico…": il clic risponde
  await caricaChatProgetto(path);
}

async function caricaChatProgetto(path) {
  try {
    const data = await api('/api/progetti/home?path=' + encodeURIComponent(path));
    state.progettoChat[path] = data.sessions || [];
    (data.sessions || []).forEach((item) => {
      if (item.running) state.running.add(item.id);
      else state.running.delete(item.id);
    });
    if (state.progettoHome && state.progetto && prChiave(state.progetto.path) === prChiave(path)) {
      state.progettoDati = data;
      state.progetto = data.progetto;
      renderProgettoHome();
    }
  } catch {
    state.progettoChat[path] = [];  // il ramo dice "nessuna" invece di restare a caricare
  }
  renderProgetti();
}

/** Una riga di conversazione: la stessa nella colonna, nei rami e nelle ricerche.
 *
 *  Una funzione sola perche' il pallino della chat in esecuzione, la domanda
 *  in attesa e il menu vanno in tutti gli elenchi: scritte in tre posti,
 *  prima o poi uno dei tre resterebbe indietro.
 */
function rigaSessione(item, { snippet = '', dove = '' } = {}) {
  const running = item.running || state.running.has(item.id);
  const attiva = item.id === state.sessionId && !state.progettoHome;
  const row = el('div', 'session' + (attiva ? ' active' : '') + (running ? ' running' : '')
    + (item.archiviata ? ' archiviata' : ''));
  row.dataset.id = item.id;
  row.setAttribute('role', 'button');
  row.tabIndex = 0;
  row.title = `${item.title}\n${item.n_messages} messaggi · ${(item.updated_at || '').replace('T', ' ')}`
    + (running ? '\nL\'agente sta lavorando' : '')
    + (item.pending ? '\nIn attesa di una tua risposta' : '');
  row.innerHTML =
    `<span class="session-title">${esc(item.title)}</span>` +
    (running
      ? '<span class="session-running" title="L\'agente sta lavorando"></span>'
      : item.pending ? '<span class="session-pending" title="In attesa di una risposta"></span>' : '') +
    (dove ? `<span class="session-where">${esc(dove)}</span>` : '') +
    `<span class="session-meta">${esc(running ? 'attiva' : relTime(item.updated_at))}</span>` +
    `<button class="pr-piu" type="button" aria-haspopup="menu" aria-expanded="false"
             title="Azioni sulla conversazione" aria-label="Azioni su ${esc(item.title)}">${PR_ICONE.puntini}</button>` +
    (snippet ? `<span class="session-snippet">${snippet}</span>` : '');
  row.onclick = (event) => {
    if (event.target.closest('.pr-piu, .pr-rinomina')) return;
    openSession(item.id);
  };
  row.onkeydown = (event) => {
    if (event.target !== row) return;
    if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); openSession(item.id); }
    if (event.key === 'F2') { event.preventDefault(); rinominaChat(item, row); }
  };
  row.querySelector('.pr-piu').onclick = (event) => {
    event.stopPropagation();
    menuChat(item, event.currentTarget, row);
  };
  return row;
}

// ---------------------------------------------------------------------------
// Menu contestuale
// ---------------------------------------------------------------------------

let prMenuAncora = null;

function chiudiMenu() {
  const menu = $('#pr-menu');
  if (!menu || menu.hidden) return;
  menu.hidden = true;
  menu.innerHTML = '';
  if (prMenuAncora) {
    prMenuAncora.setAttribute('aria-expanded', 'false');
    prMenuAncora.focus({ preventScroll: true });
  }
  prMenuAncora = null;
}

/** Apre il menu vicino all'ancora. ``voci``: {etichetta, icona, azione,
 *  pericolo, disabilitata} oppure '-' per un separatore, {titolo} per un'intestazione. */
function apriMenu(ancora, voci) {
  const menu = $('#pr-menu');
  if (!menu) return;
  if (!menu.hidden && prMenuAncora === ancora) { chiudiMenu(); return; }
  chiudiMenu();
  menu.innerHTML = '';
  voci.forEach((v) => {
    if (v === '-') { menu.appendChild(el('div', 'pr-menu-sep')); return; }
    if (v.titolo) { menu.appendChild(el('div', 'pr-menu-titolo', esc(v.titolo))); return; }
    const b = el('button', 'pr-menu-voce' + (v.pericolo ? ' pericolo' : ''));
    b.type = 'button';
    b.setAttribute('role', 'menuitem');
    b.innerHTML = (v.html || ((v.icona || '') + `<span>${esc(v.etichetta)}</span>`))
      + (v.spunta ? '<span class="spunta">✓</span>' : '');
    b.disabled = !!v.disabilitata;
    b.onclick = (e) => { e.stopPropagation(); chiudiMenu(); v.azione?.(); };
    menu.appendChild(b);
  });
  menu.hidden = false;
  prMenuAncora = ancora;
  ancora.setAttribute('aria-expanded', 'true');
  // Posizione: sotto l'ancora, allineato a destra; sopra se non ci sta.
  const r = ancora.getBoundingClientRect();
  const w = menu.offsetWidth;
  const h = menu.offsetHeight;
  let left = Math.min(window.innerWidth - w - 8, Math.max(8, r.right - w));
  let top = r.bottom + 4;
  if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 4);
  if (left < 8) left = 8;
  menu.style.left = left + 'px';
  menu.style.top = top + 'px';
  menu.querySelector('.pr-menu-voce:not(:disabled)')?.focus({ preventScroll: true });
}

function prMenuTastiera(event) {
  const menu = $('#pr-menu');
  if (!menu || menu.hidden) return;
  const voci = [...menu.querySelectorAll('.pr-menu-voce:not(:disabled)')];
  const i = voci.indexOf(document.activeElement);
  if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); chiudiMenu(); }
  else if (event.key === 'ArrowDown') { event.preventDefault(); voci[(i + 1) % voci.length]?.focus(); }
  else if (event.key === 'ArrowUp') { event.preventDefault(); voci[(i - 1 + voci.length) % voci.length]?.focus(); }
  else if (event.key === 'Tab') chiudiMenu();
}

/** Il menu di una conversazione: rinomina, sposta, archivia, elimina. */
function menuChat(item, ancora, riga) {
  const running = item.running || state.running.has(item.id);
  const suo = prDelPercorso(item.workspace_dir);
  const voci = [
    { etichetta: 'Rinomina', icona: PR_ICONE.matita, azione: () => rinominaChat(item, riga) },
  ];
  const destinazioni = state.progetti.filter((p) => p.esiste !== false
    && (!suo || prChiave(p.path) !== prChiave(suo.path)));
  if (destinazioni.length || suo) {
    voci.push('-', { titolo: 'Sposta in' });
    destinazioni.slice(0, 8).forEach((p) => voci.push({
      html: `${prAvatar(p.nome)}<span>${esc(p.nome)}</span>`,
      disabilitata: running,
      azione: () => spostaChat(item, p),
    }));
    if (suo) {
      voci.push({
        etichetta: 'Fuori dal progetto', icona: PR_ICONE.uscita, disabilitata: running,
        azione: () => spostaChat(item, null),
      });
    }
  }
  voci.push('-', {
    etichetta: item.archiviata ? 'Ripristina dall\'archivio' : 'Archivia',
    icona: PR_ICONE.archivio, disabilitata: running && !item.archiviata,
    azione: () => archiviaChat(item, !item.archiviata),
  }, {
    etichetta: 'Elimina…', icona: PR_ICONE.cestino, pericolo: true, disabilitata: running,
    azione: () => eliminaChat(item),
  });
  apriMenu(ancora, voci);
}

function menuProgetto(p, ancora) {
  const voci = [];
  if (p.esiste !== false) {
    voci.push(
      { etichetta: 'Apri il progetto', icona: PR_ICONE.apri, azione: () => openProgetto(p.path) },
      { etichetta: 'Nuova chat nel progetto', icona: PR_ICONE.chat, azione: () => nuovaChatInProgetto(p) },
      { etichetta: 'Impostazioni del progetto…', icona: PR_ICONE.ingranaggio,
        azione: () => finestraImpostazioniProgetto(p) },
      '-',
    );
  }
  voci.push({
    etichetta: 'Togli dall\'elenco…', icona: PR_ICONE.x, pericolo: true,
    azione: () => toglieProgetto(p),
  });
  apriMenu(ancora, voci);
}

// ---------------------------------------------------------------------------
// Azioni sulle conversazioni
// ---------------------------------------------------------------------------

/** Aggiorna gli elenchi dopo un cambio: colonna, rami, schermata. */
async function prAggiornaElenchi() {
  await refreshSessions();
  await refreshProgetti();
  if (state.progettoHome && state.progetto) await ricaricaProgetto();
}

function rinominaChat(item, riga) {
  const titolo = riga?.querySelector('.session-title, .pr-chat-titolo span');
  if (!riga || !titolo) {
    // Nessuna riga in vista (menu dalla schermata): una finestra.
    finestraRinomina(item);
    return;
  }
  const campo = el('input', 'pr-rinomina');
  campo.value = item.title;
  campo.maxLength = 120;
  campo.setAttribute('aria-label', 'Nuovo titolo');
  titolo.replaceWith(campo);
  campo.focus();
  campo.select();
  let fatto = false;
  const chiudi = async (salva) => {
    if (fatto) return;
    fatto = true;
    const valore = campo.value.trim();
    if (salva && valore !== item.title) {
      try {
        await api(`/api/sessions/${encodeURIComponent(item.id)}`, {
          method: 'PATCH', body: JSON.stringify({ titolo: valore }),
        });
      } catch (error) { toast(error.message); }
    }
    prAggiornaElenchi();
  };
  campo.onkeydown = (e) => {
    e.stopPropagation();
    if (e.key === 'Enter') { e.preventDefault(); chiudi(true); }
    if (e.key === 'Escape') { e.preventDefault(); chiudi(false); }
  };
  campo.onclick = (e) => e.stopPropagation();
  campo.onblur = () => chiudi(true);
}

function finestraRinomina(item) {
  apriFinestra(`
    <div class="pr-finestra-testa"><div class="spinta"><h2 id="pr-finestra-titolo">Rinomina la conversazione</h2>
      <p>Un titolo vuoto torna a quello ricavato dalla prima riga.</p></div>
      <button class="pr-chiudi" type="button" data-pr="chiudi" aria-label="Chiudi">${PR_ICONE.x}</button></div>
    <div class="pr-finestra-corpo"><div class="pr-campo"><label for="pr-f-titolo">Titolo</label>
      <input id="pr-f-titolo" maxlength="120" value="${esc(item.title)}"></div></div>
    <div class="pr-finestra-piede"><span class="spinta"></span>
      <button class="pr-bottone" type="button" data-pr="chiudi">Annulla</button>
      <button class="pr-bottone primario" type="button" data-pr="salva">Salva</button></div>`);
  const campo = $('#pr-f-titolo');
  const salva = async () => {
    try {
      await api(`/api/sessions/${encodeURIComponent(item.id)}`, {
        method: 'PATCH', body: JSON.stringify({ titolo: campo.value.trim() }),
      });
      chiudiFinestra();
      prAggiornaElenchi();
    } catch (error) { toast(error.message); }
  };
  $('#pr-finestra [data-pr="salva"]').onclick = salva;
  campo.onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); salva(); } };
  campo.select();
}

async function archiviaChat(item, archiviata) {
  try {
    await api(`/api/sessions/${encodeURIComponent(item.id)}`, {
      method: 'PATCH', body: JSON.stringify({ archiviata }),
    });
    toast(archiviata ? `«${item.title}» archiviata` : `«${item.title}» ripristinata`);
    if (archiviata && item.id === state.sessionId && !state.progettoHome) {
      // Si resta nella chat: archiviare non e' chiudere. La riga sparisce
      // dall'elenco, e si ritrova fra le archiviate.
    }
    if (state.archiviateLibere.aperte) await caricaArchiviateLibere();
    prAggiornaElenchi();
  } catch (error) { toast(error.message); }
}

async function spostaChat(item, destinazione) {
  const verso = destinazione ? `nel progetto «${destinazione.nome}»` : 'fuori dal progetto';
  const ok = await finestraConferma({
    titolo: `Spostare «${item.title}» ${verso}?`,
    testo: destinazione
      ? `Da qui in poi la conversazione lavora nella cartella del progetto, con la sua memoria e le sue istruzioni. I file creati finora restano dove sono.`
      : 'La conversazione torna fra quelle libere e lavora nell\'ultima cartella libera che hai usato. I file creati finora restano dove sono.',
    conferma: 'Sposta',
  });
  if (!ok) return;
  try {
    const data = await api(`/api/sessions/${encodeURIComponent(item.id)}/sposta`, {
      method: 'POST', body: JSON.stringify({ progetto: destinazione ? destinazione.path : '' }),
    });
    toast(destinazione ? `Spostata in «${destinazione.nome}»` : 'Spostata fra le conversazioni libere');
    if (item.id === state.sessionId && !state.progettoHome) await showSession(data);
    if (destinazione) state.progettiAperti.add(destinazione.path);
    prAggiornaElenchi();
  } catch (error) { toast(error.message); }
}

async function eliminaChat(item) {
  const ok = await finestraConferma({
    titolo: `Eliminare «${item.title}»?`,
    testo: 'La conversazione sparisce per sempre, con la sua cronologia. I file che ha creato restano nella cartella; la memoria del progetto non cambia. Se vuoi solo toglierla di mezzo, archiviala.',
    conferma: 'Elimina', pericolo: true,
  });
  if (!ok) return;
  await deleteSession(item.id);
  prAggiornaElenchi();
}

// Le libere archiviate, in fondo all'elenco delle conversazioni.
async function caricaArchiviateLibere() {
  try {
    const data = await api('/api/sessions?archiviate=1');
    state.archiviateLibere.elenco = data.sessions || [];
    state.archiviateLibere.conto = data.archiviate || 0;
  } catch { state.archiviateLibere.elenco = []; }
  renderSessions();
}

/** L'etichetta delle libere archiviate, in fondo all'elenco, e sotto di lei
 *  le archiviate quando e' aperta. Sta **dentro** #sessions: fuori, l'elenco
 *  che si allunga la spingeva in fondo alla colonna, lontana dalle righe che
 *  apre. */
function renderArchiviateLibere(root) {
  const n = state.archiviateLibere.conto;
  if (!root || !n || state.search.q) return;
  const aperte = state.archiviateLibere.aperte;
  const bottone = el('button', 'pr-archiviate', `${PR_CHEV}<span>Archiviate (${n})</span>`);
  bottone.type = 'button';
  bottone.id = 'sessions-archiviate';
  bottone.setAttribute('aria-expanded', aperte ? 'true' : 'false');
  bottone.onclick = async () => {
    state.archiviateLibere.aperte = !state.archiviateLibere.aperte;
    if (state.archiviateLibere.aperte) await caricaArchiviateLibere();
    else renderSessions();
  };
  root.appendChild(bottone);
  if (aperte) state.archiviateLibere.elenco.forEach((item) => root.appendChild(rigaSessione(item)));
}

// ---------------------------------------------------------------------------
// La schermata del progetto
// ---------------------------------------------------------------------------

/** Apre un progetto: diventa la cartella di lavoro, e si mostra la sua schermata. */
async function openProgetto(path) {
  chiudiMenu();
  const giaQui = state.progettoHome && state.progetto && prChiave(state.progetto.path) === prChiave(path);
  state.progetto = prDelPercorso(path) || state.progetto;
  mostraProgettoHome(true);
  if (!giaQui) {
    state.progettoDati = null;
    renderProgettoHome();          // lo scheletro, subito
  }
  try {
    const data = await api('/api/progetti/open', {
      method: 'POST', body: JSON.stringify({ path }),
    });
    state.progetto = data.progetto;
    state.progettoDati = data;
    state.progettoChat[data.progetto.path] = data.sessions || [];
    state.progettiAperti.add(data.progetto.path);
    dopoIlCambio(data, `Progetto: ${data.progetto.nome}`);
    renderProgettoHome();
    refreshProgetti();
  } catch (error) {
    state.progettoDati = { errore: error.message, path };
    renderProgettoHome();
  }
}

async function ricaricaProgetto() {
  if (!state.progetto) return;
  try {
    const data = await api('/api/progetti/home?path=' + encodeURIComponent(state.progetto.path));
    state.progetto = data.progetto;
    state.progettoDati = data;
    state.progettoChat[data.progetto.path] = data.sessions || [];
  } catch (error) {
    state.progettoDati = { errore: error.message, path: state.progetto.path };
  }
  if (state.progettoHome) renderProgettoHome();
}

function mostraProgettoHome(attiva) {
  state.progettoHome = !!attiva;
  const casa = $('#progetto-col');
  const chat = $('#chat-col');
  if (casa) casa.hidden = !attiva;
  if (chat) chat.hidden = !!attiva;
  // La colonna di destra parla di una conversazione: qui non ce n'e' una, e
  // la schermata prende tutta la larghezza (progetti.css, .pr-home-attiva).
  $('#app')?.classList.toggle('pr-home-attiva', !!attiva);
  // L'anteprima segue la chat: nascosta, non chiusa -- un'applicazione avviata
  // dall'agente vive dentro quell'iframe.
  sincronizzaAnteprima();
  renderProgettoChip();
  renderProgetti();
  if (!attiva) markActive(state.sessionId);
  else $$('#sessions .session, #progetti .session').forEach((r) => r.classList.remove('active'));
}

function prScheletro() {
  return '<div class="pr-scheletro"><span></span><span></span><span></span></div>';
}

function renderProgettoHome() {
  const col = $('#progetto-col');
  if (!col) return;
  const p = state.progetto;
  const dati = state.progettoDati;
  if (dati && dati.errore) {
    col.innerHTML = `<div class="pr-errore-pagina"><h2>Il progetto non si apre</h2>
      <p>${esc(dati.errore)}</p><button class="pr-bottone" type="button" data-pr="riprova">Riprova</button></div>`;
    col.querySelector('[data-pr="riprova"]').onclick = () => openProgetto(dati.path);
    return;
  }
  if (!p) { col.innerHTML = ''; return; }
  // Il testo che si stava scrivendo nella casella non si perde ridisegnando.
  const bozza = $('#pr-bozza')?.value || '';
  const bozzaFocus = document.activeElement?.id === 'pr-bozza';
  const nChat = dati ? (dati.sessions || []).length : (p.chat || 0);
  col.innerHTML = `
    <div class="pr-home">
      <header class="pr-testa">
        ${prAvatar(p.nome, 'grande')}
        <div class="pr-titoli">
          <h1>${esc(p.nome)}</h1>
          ${p.descrizione ? `<p class="pr-descr">${esc(p.descrizione)}</p>` : ''}
          <div class="pr-meta">
            <button class="pr-cartella" type="button" data-pr="cartella" title="Apri ${esc(p.path)} in Esplora risorse">${PR_ICONE.cartella}<span>${esc(p.path)}</span></button>
            <span>${nChat} ${nChat === 1 ? 'conversazione' : 'conversazioni'}</span>
            ${p.creato ? `<span>creato ${esc(new Date(p.creato).toLocaleDateString('it-IT', { day: 'numeric', month: 'short', year: 'numeric' }))}</span>` : ''}
            ${p.wiki ? `<span class="pr-etichetta" title="${p.pagine} pagine · ${p.fonti} fonti">wiki</span>` : ''}
          </div>
        </div>
        <div class="pr-azioni">
          <button class="pr-bottone" type="button" data-pr="impostazioni">${PR_ICONE.ingranaggio}<span>Impostazioni</span></button>
          <button class="pr-bottone solo-icona" type="button" data-pr="menu" aria-haspopup="menu" aria-expanded="false" title="Altre azioni" aria-label="Altre azioni">${PR_ICONE.puntini}</button>
        </div>
      </header>
      <div class="pr-composer">
        <textarea id="pr-bozza" rows="1" placeholder="Nuova conversazione in ${esc(p.nome)}…" aria-label="Nuova conversazione nel progetto"></textarea>
        <button class="pr-invia" type="button" data-pr="avvia" title="Avvia (Invio)" aria-label="Avvia la conversazione">${PR_ICONE.freccia}</button>
      </div>
      <div class="pr-griglia">
        <div class="pr-colonna larga">
          <section class="pr-scheda" data-scheda="riprendi" aria-labelledby="pr-h-riprendi"></section>
          <section class="pr-scheda" data-scheda="chat" aria-labelledby="pr-h-chat"></section>
        </div>
        <div class="pr-colonna">
          <section class="pr-scheda" data-scheda="memoria" aria-labelledby="pr-h-memoria"></section>
          <section class="pr-scheda" data-scheda="istruzioni" aria-labelledby="pr-h-istruzioni"></section>
          <section class="pr-scheda" data-scheda="libreria" aria-labelledby="pr-h-libreria"></section>
        </div>
      </div>
    </div>`;
  col.querySelector('[data-pr="cartella"]').onclick = () => {
    api('/api/workspace/open', { method: 'POST', body: '{}' }).catch((e) => toast(e.message));
  };
  col.querySelector('[data-pr="impostazioni"]').onclick = () => finestraImpostazioniProgetto(p);
  col.querySelector('[data-pr="menu"]').onclick = (e) => menuProgetto(p, e.currentTarget);
  const box = $('#pr-bozza');
  box.value = bozza;
  const avvia = col.querySelector('[data-pr="avvia"]');
  const aggiornaInvio = () => { avvia.disabled = !box.value.trim(); };
  aggiornaInvio();
  box.oninput = () => {
    aggiornaInvio();
    box.style.height = 'auto';
    box.style.height = Math.min(box.scrollHeight, 220) + 'px';
  };
  box.onkeydown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); nuovaChatNelProgetto(); }
  };
  avvia.onclick = nuovaChatNelProgetto;
  if (bozzaFocus) box.focus();

  prSchedaRiprendi(col.querySelector('[data-scheda="riprendi"]'));
  prSchedaMemoria(col.querySelector('[data-scheda="memoria"]'));
  prSchedaChat(col.querySelector('[data-scheda="chat"]'));
  prSchedaIstruzioni(col.querySelector('[data-scheda="istruzioni"]'));
  prSchedaLibreria(col.querySelector('[data-scheda="libreria"]'));
}

function prTesta(id, titolo, conto = '', extra = '') {
  return `<div class="pr-scheda-testa"><h2 id="${id}">${esc(titolo)}</h2>`
    + (conto !== '' ? `<span class="pr-conto-mono">${esc(String(conto))}</span>` : '')
    + '<span class="spinta"></span>' + extra + '</div>';
}

// --- Riprendi da qui ---------------------------------------------------------

function prSchedaRiprendi(nodo) {
  const dati = state.progettoDati;
  const aperti = (state.progetto?.memoria || []).filter((v) => v.tipo === 'aperto');
  if (!dati) { nodo.innerHTML = prTesta('pr-h-riprendi', 'Riprendi da qui') + prScheletro(); return; }
  const r = dati.riprendi;
  if (!r) {
    nodo.innerHTML = prTesta('pr-h-riprendi', 'Riprendi da qui')
      + '<p class="pr-vuoto"><strong>Nessuna conversazione, ancora.</strong> Scrivi qui sopra la prima '
      + 'richiesta. Alla fine dei turni in cui si lavora, l\'harness annota nella memoria le decisioni, le '
      + 'convenzioni e i lavori rimasti aperti: le chat successive partono da lì.</p>';
    return;
  }
  const s = r.session;
  const [statoTesto, statoClasse] = PR_STATI[r.stato] || [r.stato, ''];
  const piano = r.piano || { totale: 0, fatti: 0, aperti: [] };
  const riprendibile = ['max_steps', 'stallo', 'stopped', 'finestra_piena'].includes(r.stato);
  nodo.innerHTML = prTesta('pr-h-riprendi', 'Riprendi da qui')
    + `<button class="pr-riprendi-titolo" type="button" data-pr="apri"><span>${esc(s.title)}</span>`
    + `<span class="pr-stato ${statoClasse}"><i></i>${esc(statoTesto)}</span></button>`
    + `<div class="pr-quando">ultima attività ${esc(prQuando(s.updated_at))} · ${s.n_messages} messaggi</div>`
    + (r.ultima_risposta ? `<div class="pr-citazione">${esc(r.ultima_risposta)}</div>` : '')
    + (piano.totale ? `<div class="pr-sottoblocco"><div class="pr-sottoblocco-titolo">Piano
        <span class="pr-barra" role="img" aria-label="${piano.fatti} punti su ${piano.totale}"><span style="width:${Math.round(100 * piano.fatti / piano.totale)}%"></span></span>
        <span class="pr-conto-mono">${piano.fatti}/${piano.totale}</span></div>`
      + (piano.aperti.length ? `<ul class="pr-punti">${piano.aperti.map((a) =>
        `<li class="${a.status === 'doing' ? 'doing' : ''}">${esc(a.text)}</li>`).join('')}</ul>` : '')
      + '</div>' : '')
    + (aperti.length ? `<div class="pr-sottoblocco"><div class="pr-sottoblocco-titolo">Lavori aperti nel progetto</div>
        <ul class="pr-punti">${aperti.slice(0, 4).map((v) => `<li class="aperto">${esc(v.testo)}</li>`).join('')}</ul></div>` : '')
    + '<div class="pr-riprendi-azioni">'
    + `<button class="pr-bottone ${riprendibile ? '' : 'primario'} piccolo" type="button" data-pr="apri">${PR_ICONE.chat}Apri la chat</button>`
    + (riprendibile ? `<button class="pr-bottone primario piccolo" type="button" data-pr="continua">Continua da dove si era fermata</button>` : '')
    + '</div>';
  nodo.querySelectorAll('[data-pr="apri"]').forEach((b) => { b.onclick = () => openSession(s.id); });
  const continua = nodo.querySelector('[data-pr="continua"]');
  if (continua) {
    continua.onclick = async () => {
      await openSession(s.id);
      const composer = $('#composer textarea');
      if (composer && !state.busy) { composer.value = 'continua'; send(); }
    };
  }
}

// --- Memoria -----------------------------------------------------------------

function prMemoriaUltimaModifica(memoria) {
  return memoria.reduce((ultima, v) => (!ultima || (v.aggiornata || '') > (ultima.aggiornata || '') ? v : ultima), null);
}

function prSchedaMemoria(nodo) {
  const p = state.progetto;
  const memoria = p?.memoria || [];
  const dati = state.progettoDati;
  const filtro = state.memoriaFiltro || '';
  const conti = Object.fromEntries(PR_TIPI.map((t) => [t.k, memoria.filter((v) => v.tipo === t.k).length]));
  const ultima = prMemoriaUltimaModifica(memoria);
  const piena = memoria.length >= PR_MAX_VOCI;
  nodo.innerHTML = prTesta('pr-h-memoria', 'Memoria', `${memoria.length}/${PR_MAX_VOCI}`,
    `<button class="pr-bottone piccolo" type="button" data-pr="aggiungi" ${piena ? 'disabled title="La memoria è piena: togli le voci superate"' : ''}>${PR_ICONE.piu}Aggiungi</button>`);
  const corpo = el('div');
  if (dati && dati.memoria_automatica === false) {
    corpo.appendChild(el('p', 'pr-avviso',
      'La scrittura automatica è spenta: la memoria cambia solo quando la scrivi tu o il modello. '
      + 'Si accende in <b>Impostazioni › Contesto e memoria</b>.'));
  }
  if (ultima && ultima.aggiornata) {
    const da = ultima.titolo_chat ? ` da «${esc(prAccorcia(ultima.titolo_chat, 40))}»` : '';
    corpo.appendChild(el('p', 'pr-sottotitolo', `Ultima modifica${da} · ${esc(prQuando(ultima.aggiornata))}`));
  }
  if (state.memoriaModulo === 'nuova') corpo.appendChild(prModuloVoce(null));
  if (!memoria.length) {
    corpo.appendChild(el('p', 'pr-vuoto',
      '<strong>Ancora vuota.</strong> Dopo i turni in cui si lavora l\'harness ci scrive quello che deve '
      + 'valere anche nelle prossime chat: decisioni, convenzioni, strade scartate, lavori rimasti aperti. '
      + 'Puoi aggiungerne anche tu.'));
  } else {
    const usati = PR_TIPI.filter((t) => conti[t.k]);
    if (usati.length > 1) {
      const filtri = el('div', 'pr-filtri');
      filtri.setAttribute('role', 'group');
      filtri.setAttribute('aria-label', 'Filtra per tipo');
      [{ k: '', gruppo: 'Tutte' }, ...usati].forEach((t) => {
        const b = el('button', 'pr-filtro');
        b.type = 'button';
        b.setAttribute('aria-pressed', filtro === t.k ? 'true' : 'false');
        if (t.k) b.dataset.tipo = t.k;
        b.innerHTML = (t.k ? '<span class="pr-tipo-segno"></span>' : '')
          + `${esc(t.gruppo)} <b>${t.k ? conti[t.k] : memoria.length}</b>`;
        b.onclick = () => { state.memoriaFiltro = filtro === t.k ? '' : t.k; prSchedaMemoria(nodo); };
        filtri.appendChild(b);
      });
      corpo.appendChild(filtri);
    }
    PR_TIPI.filter((t) => conti[t.k] && (!filtro || filtro === t.k)).forEach((t) => {
      const gruppo = el('div', 'pr-gruppo');
      gruppo.innerHTML = `<div class="pr-gruppo-titolo" data-tipo="${t.k}"><span class="pr-tipo-segno"></span>${esc(t.gruppo)}</div>`;
      memoria.filter((v) => v.tipo === t.k).forEach((v) => gruppo.appendChild(prRigaVoce(v)));
      corpo.appendChild(gruppo);
    });
  }
  nodo.appendChild(corpo);
  const aggiungi = nodo.querySelector('[data-pr="aggiungi"]');
  aggiungi.onclick = () => {
    state.memoriaModulo = state.memoriaModulo === 'nuova' ? null : 'nuova';
    prSchedaMemoria(nodo);
    nodo.querySelector('.pr-modulo textarea')?.focus();
  };
}

function prRigaVoce(v) {
  if (state.memoriaModulo === v.id) {
    const riga = el('div', 'pr-voce');
    riga.appendChild(prModuloVoce(v));
    return riga;
  }
  const riga = el('div', 'pr-voce' + (state.memoriaNuove.has(v.id) ? ' nuova' : ''));
  riga.dataset.tipo = v.tipo;
  riga.dataset.id = v.id;
  const autore = { harness: 'automatica', modello: 'dal modello', utente: 'tua' }[v.autore] || v.autore;
  const chatEsiste = v.chat && (state.progettoDati?.sessions || []).concat(state.progettoDati?.archiviate || [])
    .some((s) => s.id === v.chat);
  // Il titolo della chat accorciato: la riga dice da dove viene la voce, il
  // titolo intero sta nel suggerimento.
  const breve = prAccorcia(v.titolo_chat, 34);
  const fonte = v.titolo_chat
    ? (chatEsiste
      ? `<button type="button" data-pr="fonte" title="Apri «${esc(v.titolo_chat)}»">da «${esc(breve)}»</button>`
      : `<span title="${esc(v.titolo_chat)} (conversazione non più presente)">da «${esc(breve)}»</span>`)
    : '';
  riga.innerHTML = '<span class="pr-tipo-segno" aria-hidden="true"></span>'
    + `<div class="pr-voce-corpo"><div class="pr-voce-testo">${esc(v.testo)}</div>`
    + `<div class="pr-voce-meta"><span class="pr-autore ${v.autore === 'utente' ? 'utente' : ''}" title="${v.autore === 'utente'
      ? 'Scritta o corretta da te: l\'harness non la tocca' : 'La può aggiornare l\'harness'}">${esc(autore)}</span>`
    + fonte + (v.aggiornata ? `<span>${esc(prQuando(v.aggiornata))}</span>` : '') + '</div></div>'
    + '<div class="pr-voce-azioni">'
    + `<button class="pr-mini" type="button" data-pr="correggi" title="Correggi" aria-label="Correggi la voce">${PR_ICONE.matita}</button>`
    + `<button class="pr-mini pericolo" type="button" data-pr="togli" title="Togli dalla memoria" aria-label="Togli la voce">${PR_ICONE.x}</button>`
    + '</div>';
  riga.querySelector('[data-pr="fonte"]')?.addEventListener('click', () => openSession(v.chat));
  riga.querySelector('[data-pr="correggi"]').onclick = () => {
    state.memoriaModulo = v.id;
    const nodo = riga.closest('[data-scheda="memoria"]');
    prSchedaMemoria(nodo);
    nodo.querySelector('.pr-modulo textarea')?.focus();
  };
  riga.querySelector('[data-pr="togli"]').onclick = () => prConfermaTogli(riga, v);
  return riga;
}

function prConfermaTogli(riga, v) {
  if (riga.querySelector('.pr-voce-conferma')) return;
  const conferma = el('div', 'pr-voce-conferma',
    '<span>Togliere questa voce?</span>'
    + '<button class="pr-bottone pericolo piccolo" type="button" data-pr="si">Togli</button>'
    + '<button class="pr-bottone piccolo" type="button" data-pr="no">Annulla</button>');
  riga.querySelector('.pr-voce-corpo').appendChild(conferma);
  conferma.querySelector('[data-pr="no"]').onclick = () => conferma.remove();
  conferma.querySelector('[data-pr="si"]').onclick = () => togliVoce(v);
  conferma.querySelector('[data-pr="si"]').focus();
}

function prModuloVoce(v) {
  const modulo = el('div', 'pr-modulo');
  const tipo = v ? v.tipo : (state.memoriaFiltro || 'decisione');
  modulo.innerHTML = `<textarea rows="2" maxlength="${PR_MAX_VOCE}" placeholder="Una frase sola, che si capisca anche fra un mese">${esc(v ? v.testo : '')}</textarea>`
    + '<div class="pr-modulo-piede">'
    + `<select aria-label="Tipo della voce">${PR_TIPI.map((t) =>
      `<option value="${t.k}" ${t.k === tipo ? 'selected' : ''}>${esc(t.uno)}</option>`).join('')}</select>`
    + `<span class="pr-contatore">0/${PR_MAX_VOCE}</span>`
    + '<button class="pr-bottone piccolo" type="button" data-pr="annulla">Annulla</button>'
    + `<button class="pr-bottone primario piccolo" type="button" data-pr="salva">${v ? 'Salva' : 'Aggiungi'}</button></div>`
    + (v && v.autore !== 'utente'
      ? '<div class="pr-campo-aiuto">Corretta da te, la voce diventa tua: l\'harness non la cambierà più.</div>' : '');
  const testo = modulo.querySelector('textarea');
  const contatore = modulo.querySelector('.pr-contatore');
  const salva = modulo.querySelector('[data-pr="salva"]');
  const aggiorna = () => {
    const n = testo.value.trim().length;
    contatore.textContent = `${testo.value.length}/${PR_MAX_VOCE}`;
    contatore.classList.toggle('oltre', testo.value.length >= PR_MAX_VOCE);
    salva.disabled = !n;
  };
  aggiorna();
  testo.oninput = aggiorna;
  const annulla = () => {
    state.memoriaModulo = null;
    prSchedaMemoria(modulo.closest('[data-scheda="memoria"]'));
  };
  const invia = async () => {
    const valore = testo.value.trim();
    if (!valore) return;
    salva.disabled = true;
    const scelto = modulo.querySelector('select').value;
    const ok = v ? await correggiVoce(v, valore, scelto) : await aggiungiVoce(valore, scelto);
    if (!ok) salva.disabled = false;
  };
  testo.onkeydown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); invia(); }
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); annulla(); }
  };
  modulo.querySelector('[data-pr="annulla"]').onclick = annulla;
  salva.onclick = invia;
  return modulo;
}

async function prCambiaMemoria(richiesta, messaggio) {
  try {
    const data = await richiesta();
    const prima = new Set((state.progetto?.memoria || []).map((x) => `${x.id}:${x.aggiornata}`));
    state.progetto = data.progetto;
    prSegnaNuove((data.progetto.memoria || []).filter((x) => !prima.has(`${x.id}:${x.aggiornata}`)).map((x) => x.id));
    state.memoriaModulo = null;
    const i = state.progetti.findIndex((x) => prChiave(x.path) === prChiave(data.progetto.path));
    if (i >= 0) state.progetti[i] = { ...state.progetti[i], ...data.progetto };
    if (state.progettoHome) {
      const nodo = $('#progetto-col [data-scheda="memoria"]');
      if (nodo) prSchedaMemoria(nodo);
      const riprendi = $('#progetto-col [data-scheda="riprendi"]');
      if (riprendi) prSchedaRiprendi(riprendi);
    }
    renderMemoriaPannello();
    if (messaggio) toast(messaggio);
    return true;
  } catch (error) {
    toast(error.message);
    return false;
  }
}

function aggiungiVoce(testo, tipo) {
  const path = state.progetto.path;
  return prCambiaMemoria(() => api('/api/progetti/memoria', {
    method: 'POST', body: JSON.stringify({ path, testo, tipo }),
  }), 'Aggiunta alla memoria del progetto');
}

function correggiVoce(v, testo, tipo) {
  const path = state.progetto.path;
  return prCambiaMemoria(() => api('/api/progetti/memoria', {
    method: 'PATCH', body: JSON.stringify({ path, id: v.id, testo, tipo }),
  }), '');
}

function togliVoce(v) {
  const path = state.progetto.path;
  return prCambiaMemoria(() => api(
    `/api/progetti/memoria?path=${encodeURIComponent(path)}&id=${encodeURIComponent(v.id)}`,
    { method: 'DELETE' },
  ), 'Tolta dalla memoria del progetto');
}

/** Segna per qualche secondo le voci appena cambiate: si vede cosa e' successo. */
function prSegnaNuove(ids) {
  ids.forEach((id) => state.memoriaNuove.add(id));
  clearTimeout(prSegnaNuove._t);
  prSegnaNuove._t = setTimeout(() => state.memoriaNuove.clear(), 4000);
}

// --- Conversazioni -----------------------------------------------------------

function prSchedaChat(nodo) {
  const dati = state.progettoDati;
  if (!dati) { nodo.innerHTML = prTesta('pr-h-chat', 'Conversazioni') + prScheletro(); return; }
  const tutte = dati.sessions || [];
  const archiviate = dati.archiviate || [];
  const q = (state.progettoCerca || '').trim().toLowerCase();
  nodo.innerHTML = prTesta('pr-h-chat', 'Conversazioni', tutte.length,
    tutte.length > 5 ? `<label class="pr-cerca">${PR_ICONE.cerca}<input type="search" id="pr-cerca"
      placeholder="Cerca per titolo" aria-label="Cerca fra le conversazioni del progetto" value="${esc(state.progettoCerca || '')}"></label>` : '');
  const elenco = el('div', 'pr-chat-elenco');
  const filtra = (s) => !q || String(s.title || '').toLowerCase().includes(q);
  const visibili = tutte.filter(filtra);
  if (!tutte.length) {
    elenco.appendChild(el('p', 'pr-vuoto', 'Le conversazioni del progetto compariranno qui.'));
  } else if (!visibili.length) {
    elenco.appendChild(el('p', 'pr-vuoto', `Nessuna conversazione per «${esc(q)}».`));
  }
  visibili.forEach((s) => elenco.appendChild(prRigaChat(s)));
  nodo.appendChild(elenco);
  if (archiviate.length) {
    const piede = el('div', 'pr-piede-scheda');
    const aperte = !!state.progettoArchiviateAperte;
    const b = el('button', 'pr-archiviate', `${PR_CHEV}<span>Archiviate (${archiviate.length})</span>`);
    b.hidden = false;
    b.type = 'button';
    b.setAttribute('aria-expanded', aperte ? 'true' : 'false');
    b.onclick = () => { state.progettoArchiviateAperte = !aperte; prSchedaChat(nodo); };
    piede.appendChild(b);
    if (aperte) {
      const lista = el('div', 'pr-chat-elenco');
      archiviate.filter(filtra).forEach((s) => lista.appendChild(prRigaChat(s)));
      piede.appendChild(lista);
    }
    nodo.appendChild(piede);
  }
  const cerca = nodo.querySelector('#pr-cerca');
  if (cerca) {
    cerca.oninput = () => {
      state.progettoCerca = cerca.value;
      const pos = cerca.selectionStart;
      prSchedaChat(nodo);
      const nuovo = nodo.querySelector('#pr-cerca');
      nuovo.focus();
      nuovo.setSelectionRange(pos, pos);
    };
  }
}

function prRigaChat(s) {
  const running = s.running || state.running.has(s.id);
  const riga = el('div', 'pr-chat' + (s.archiviata ? ' archiviata' : ''));
  riga.innerHTML = `<button class="pr-chat-apri" type="button">
      <span class="pr-chat-titolo"><span>${esc(s.title)}</span>${running
        ? '<span class="session-running" title="L\'agente sta lavorando"></span>'
        : s.pending ? '<span class="session-pending" title="In attesa di una tua risposta"></span>' : ''}</span>
      <span class="pr-chat-meta">${s.n_messages} messaggi · ${esc(running ? 'attiva adesso' : prQuando(s.updated_at))}</span>
    </button>
    <button class="pr-piu" type="button" aria-haspopup="menu" aria-expanded="false" title="Azioni sulla conversazione"
            aria-label="Azioni su ${esc(s.title)}">${PR_ICONE.puntini}</button>`;
  riga.querySelector('.pr-chat-apri').onclick = () => openSession(s.id);
  riga.querySelector('.pr-piu').onclick = (e) => {
    e.stopPropagation();
    menuChat({ ...s, workspace_dir: s.workspace_dir || state.progetto?.path }, e.currentTarget, null);
  };
  return riga;
}

// --- Istruzioni --------------------------------------------------------------

function prSchedaIstruzioni(nodo) {
  const p = state.progetto;
  const testo = (p?.istruzioni || '').trim();
  const modifica = state.istruzioniInModifica;
  nodo.innerHTML = prTesta('pr-h-istruzioni', 'Istruzioni', '',
    modifica ? '' : `<button class="pr-bottone piccolo" type="button" data-pr="modifica">${PR_ICONE.matita}${testo ? 'Modifica' : 'Scrivi'}</button>`);
  if (modifica) {
    const modulo = el('div', 'pr-modulo');
    modulo.innerHTML = `<textarea rows="7" maxlength="${PR_MAX_ISTRUZIONI}" placeholder="Come si lavora in questo progetto: lingua, stile, cartelle da non toccare, comandi per i test…">${esc(p.istruzioni || '')}</textarea>`
      + `<div class="pr-modulo-piede"><span class="pr-campo-aiuto">Arrivano al modello in ogni chat del progetto.</span>`
      + `<span class="pr-contatore"></span>`
      + '<button class="pr-bottone piccolo" type="button" data-pr="annulla">Annulla</button>'
      + '<button class="pr-bottone primario piccolo" type="button" data-pr="salva">Salva</button></div>';
    nodo.appendChild(modulo);
    const area = modulo.querySelector('textarea');
    const contatore = modulo.querySelector('.pr-contatore');
    const aggiorna = () => {
      contatore.textContent = `${area.value.length}/${PR_MAX_ISTRUZIONI}`;
      contatore.classList.toggle('oltre', area.value.length >= PR_MAX_ISTRUZIONI);
    };
    aggiorna();
    area.oninput = aggiorna;
    area.focus();
    area.setSelectionRange(area.value.length, area.value.length);
    const chiudi = () => { state.istruzioniInModifica = false; prSchedaIstruzioni(nodo); };
    modulo.querySelector('[data-pr="annulla"]').onclick = chiudi;
    area.onkeydown = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); chiudi(); }
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); modulo.querySelector('[data-pr="salva"]').click(); }
    };
    modulo.querySelector('[data-pr="salva"]').onclick = async () => {
      if (await salvaProgetto({ istruzioni: area.value })) {
        state.istruzioniInModifica = false;
        prSchedaIstruzioni(nodo);
        toast('Istruzioni salvate: valgono dal prossimo messaggio');
      }
    };
    return;
  }
  if (!testo) {
    nodo.appendChild(el('p', 'pr-vuoto', 'Nessuna istruzione. Scrivi come si lavora in questo progetto: '
      + 'arrivano al modello in ogni sua chat. La descrizione, invece, resta solo per te.'));
  } else {
    const box = el('div', 'pr-istruzioni-testo' + (state.istruzioniTutte ? ' tutto' : ''));
    box.textContent = testo;
    nodo.appendChild(box);
    requestAnimationFrame(() => {
      if (box.scrollHeight > box.clientHeight + 4) {
        box.classList.add('tagliato');
        const piu = el('button', 'pr-link', 'Mostra tutto');
        piu.type = 'button';
        piu.onclick = () => { state.istruzioniTutte = true; prSchedaIstruzioni(nodo); };
        nodo.appendChild(piu);
      }
    });
  }
  nodo.querySelector('[data-pr="modifica"]').onclick = () => {
    state.istruzioniInModifica = true;
    prSchedaIstruzioni(nodo);
  };
}

// --- Libreria ----------------------------------------------------------------

function prSchedaLibreria(nodo) {
  const dati = state.progettoDati;
  if (!dati) { nodo.innerHTML = prTesta('pr-h-libreria', 'Libreria') + prScheletro(); return; }
  const voci = dati.libreria || [];
  nodo.innerHTML = prTesta('pr-h-libreria', 'Libreria', voci.length || '',
    '<span class="pr-conto-mono" title="La cartella della libreria nel progetto">.memoria/</span>');
  if (!voci.length) {
    const spenta = state.settings?.libreria_concetti === false;
    nodo.appendChild(el('p', 'pr-vuoto', spenta
      ? 'La libreria è spenta: si accende in <b>Impostazioni › Contesto e memoria</b>. Conserva su disco i tratti '
        + 'di conversazione riassunti e gli estratti del ragionamento.'
      : 'Qui finiscono, parola per parola, i tratti di conversazione che la compattazione riassume e gli '
        + 'estratti del ragionamento dei punti chiusi. Il modello li ritrova da solo quando servono.'));
    return;
  }
  const lista = el('ul', 'pr-libreria');
  voci.slice(0, state.libreriaTutta ? voci.length : 8).forEach((v) => {
    const li = el('li');
    const b = el('button', '', `<span class="n">${String(v.numero).padStart(3, '0')}</span><span class="t">${esc(v.titolo)}</span>`);
    b.type = 'button';
    b.title = v.nome;
    b.onclick = () => finestraLibreria(v);
    li.appendChild(b);
    lista.appendChild(li);
  });
  nodo.appendChild(lista);
  if (voci.length > 8 && !state.libreriaTutta) {
    const piu = el('button', 'pr-link', `Tutte le ${voci.length} voci`);
    piu.type = 'button';
    piu.onclick = () => { state.libreriaTutta = true; prSchedaLibreria(nodo); };
    nodo.appendChild(piu);
  }
}

// ---------------------------------------------------------------------------
// Chat nuove
// ---------------------------------------------------------------------------

/** Comincia una chat nel progetto dalla casella della schermata.
 *
 *  Non duplica ``send``: crea la conversazione, passa alla vista chat e mette
 *  il testo nel composer vero. Un secondo invio scritto qui sarebbe un secondo
 *  posto in cui ricordarsi degli allegati, della ricerca online e del livello
 *  di pensiero -- e prima o poi in uno dei due mancherebbe qualcosa.
 */
async function nuovaChatNelProgetto() {
  const box = $('#pr-bozza');
  const testo = (box?.value || '').trim();
  if (!testo) { box?.focus(); return; }
  box.value = '';
  await newSession();
  mostraProgettoHome(false);
  const composer = $('#composer textarea');
  composer.value = testo;
  await send();
}

/** Dal menu: apre il progetto e porta il fuoco sulla casella della chat nuova. */
async function nuovaChatInProgetto(p) {
  await openProgetto(p.path);
  $('#pr-bozza')?.focus();
}

// ---------------------------------------------------------------------------
// Finestre
// ---------------------------------------------------------------------------

let prFinestraFuocoPrima = null;
let prFinestraRisolvi = null;

function apriFinestra(html, { larga = false } = {}) {
  const velo = $('#pr-velo');
  const finestra = $('#pr-finestra');
  chiudiMenu();
  prFinestraFuocoPrima = document.activeElement;
  finestra.className = 'pr-finestra' + (larga ? ' larga' : '');
  finestra.innerHTML = html;
  velo.hidden = false;
  finestra.querySelectorAll('[data-pr="chiudi"]').forEach((b) => { b.onclick = () => chiudiFinestra(); });
  const primo = finestra.querySelector('[autofocus]') || finestra.querySelector('input, textarea, select, button:not(.pr-chiudi)');
  primo?.focus();
}

function chiudiFinestra(valore = false) {
  const velo = $('#pr-velo');
  if (!velo || velo.hidden) return;
  velo.hidden = true;
  $('#pr-finestra').innerHTML = '';
  const risolvi = prFinestraRisolvi;
  prFinestraRisolvi = null;
  risolvi?.(valore);
  prFinestraFuocoPrima?.focus?.({ preventScroll: true });
}

/** Tiene il fuoco dentro la finestra, come il menu delle impostazioni. */
function prFinestraTastiera(event) {
  const velo = $('#pr-velo');
  if (!velo || velo.hidden) return;
  if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); chiudiFinestra(); return; }
  if (event.key !== 'Tab') return;
  const fuochi = [...$('#pr-finestra').querySelectorAll(
    'button:not(:disabled), input:not(:disabled), textarea, select, summary, [tabindex="0"]')]
    .filter((n) => n.offsetParent !== null);
  if (!fuochi.length) return;
  const primo = fuochi[0];
  const ultimo = fuochi[fuochi.length - 1];
  if (event.shiftKey && document.activeElement === primo) { event.preventDefault(); ultimo.focus(); }
  else if (!event.shiftKey && document.activeElement === ultimo) { event.preventDefault(); primo.focus(); }
}

function finestraConferma({ titolo, testo, conferma = 'Conferma', pericolo = false }) {
  return new Promise((risolvi) => {
    apriFinestra(`
      <div class="pr-finestra-testa"><div class="spinta"><h2 id="pr-finestra-titolo">${esc(titolo)}</h2>
        <p>${esc(testo)}</p></div></div>
      <div class="pr-finestra-piede"><span class="spinta"></span>
        <button class="pr-bottone" type="button" data-pr="chiudi">Annulla</button>
        <button class="pr-bottone ${pericolo ? 'pericolo' : 'primario'}" type="button" data-pr="ok" autofocus>${esc(conferma)}</button>
      </div>`);
    prFinestraRisolvi = risolvi;
    $('#pr-finestra [data-pr="ok"]').onclick = () => chiudiFinestra(true);
  });
}

// --- Nuovo progetto ----------------------------------------------------------

function finestraNuovoProgetto() {
  const f = { modo: 'esistente', path: '', genitore: '', nome: '', gia: false, nomeToccato: false };
  const ultima = (state.settings?.recent_workspaces || [])[0] || '';
  f.genitore = ultima ? ultima.replace(/[\\/][^\\/]+[\\/]?$/, '') : '';
  apriFinestra(`
    <div class="pr-finestra-testa"><div class="spinta"><h2 id="pr-finestra-titolo">Nuovo progetto</h2>
      <p>Una cartella, le sue conversazioni e una memoria che passa dall'una all'altra.</p></div>
      <button class="pr-chiudi" type="button" data-pr="chiudi" aria-label="Chiudi">${PR_ICONE.x}</button></div>
    <div class="pr-finestra-corpo">
      <div class="pr-campo"><label for="pr-f-nome">Nome</label>
        <input id="pr-f-nome" maxlength="120" placeholder="Es. Gestionale magazzino" autocomplete="off" autofocus></div>
      <div class="pr-campo"><span class="pr-campo-titolo">Cartella</span>
        <div class="pr-segmenti" role="radiogroup" aria-label="Cartella">
          <button type="button" role="radio" data-modo="esistente" aria-checked="true">Una che esiste</button>
          <button type="button" role="radio" data-modo="nuova" aria-checked="false">Creane una nuova</button>
        </div>
        <div class="pr-riga-campo">
          <input id="pr-f-path" placeholder="C:\\Users\\…\\cartella" spellcheck="false" aria-label="Percorso della cartella">
          <button class="pr-bottone" type="button" data-pr="sfoglia">${PR_ICONE.cartella}Sfoglia…</button>
        </div>
        <div class="pr-campo-aiuto" id="pr-f-path-aiuto"></div>
      </div>
      <div class="pr-campo"><label for="pr-f-descr">Descrizione <span class="facoltativo">· facoltativa, solo per te</span></label>
        <input id="pr-f-descr" maxlength="400" placeholder="A cosa serve, per ritrovarlo"></div>
      <div class="pr-campo"><label for="pr-f-istr">Istruzioni <span class="facoltativo">· facoltative, vanno al modello</span></label>
        <textarea id="pr-f-istr" rows="3" maxlength="${PR_MAX_ISTRUZIONI}" placeholder="Come si lavora qui: lingua, stile, cartelle da non toccare, comandi per i test…"></textarea></div>
      <details class="pr-avanzate"><summary>${PR_CHEV}Avanzate</summary>
        <div class="pr-avanzate-corpo">
          <label class="pr-interruttore"><input type="checkbox" id="pr-f-wiki"><div>
            <strong>Modalità wiki</strong>
            <span>L'agente diventa il manutentore di una wiki LLM: <code>raw/</code> per le fonti, che non tocca,
            <code>wiki/</code> per le pagine che scrive lui, lo schema in <code>CLAUDE.md</code>. Le cartelle
            vengono create.</span></div></label>
        </div></details>
      <div class="pr-errore" id="pr-f-errore" role="alert"></div>
    </div>
    <div class="pr-finestra-piede"><span class="spinta"></span>
      <button class="pr-bottone" type="button" data-pr="chiudi">Annulla</button>
      <button class="pr-bottone primario" type="button" data-pr="crea" disabled>Crea progetto</button>
    </div>`);
  const nome = $('#pr-f-nome');
  const path = $('#pr-f-path');
  const aiuto = $('#pr-f-path-aiuto');
  const crea = $('#pr-finestra [data-pr="crea"]');
  const errore = $('#pr-f-errore');
  const separatore = () => ((f.genitore || f.path).includes('\\') ? '\\' : '/');
  const aggiorna = () => {
    $$('#pr-finestra [data-modo]').forEach((b) => b.setAttribute('aria-checked', String(b.dataset.modo === f.modo)));
    if (f.modo === 'esistente') {
      path.placeholder = 'Il percorso della cartella';
      aiuto.innerHTML = f.gia
        ? '<b>Questa cartella è già un progetto:</b> verrà aggiunta all\'elenco con il suo nome, le sue istruzioni e la sua memoria.'
        : 'Le conversazioni del progetto lavoreranno in questa cartella.';
    } else {
      path.placeholder = 'Dove crearla';
      const nomeCartella = nome.value.trim().replace(/[<>:"/\\|?*]/g, '').replace(/[. ]+$/, '');
      aiuto.innerHTML = nomeCartella && f.genitore
        ? `Verrà creata: <code class="pr-percorso-inline">${esc(f.genitore.replace(/[\\/]+$/, '') + separatore() + nomeCartella)}</code>`
        : 'Scegli dove crearla: la cartella prende il nome del progetto.';
    }
    const pronto = nome.value.trim() && (f.modo === 'esistente' ? f.path.trim() : f.genitore.trim());
    crea.disabled = !pronto;
  };
  path.value = f.modo === 'esistente' ? f.path : f.genitore;
  $$('#pr-finestra [data-modo]').forEach((b) => {
    b.onclick = () => {
      f.modo = b.dataset.modo;
      path.value = f.modo === 'esistente' ? f.path : f.genitore;
      aggiorna();
    };
  });
  nome.oninput = () => { f.nomeToccato = true; aggiorna(); };
  path.oninput = () => {
    if (f.modo === 'esistente') { f.path = path.value; f.gia = false; } else f.genitore = path.value;
    aggiorna();
  };
  $('#pr-finestra [data-pr="sfoglia"]').onclick = async (e) => {
    const b = e.currentTarget;
    b.disabled = true;
    try {
      const data = await api('/api/progetti/pick', { method: 'POST' });
      if (!data.cancelled) {
        if (f.modo === 'esistente') {
          f.path = data.path;
          f.gia = !!data.gia_progetto;
          if (!f.nomeToccato || f.gia || !nome.value.trim()) nome.value = data.nome;
          if (data.wiki) $('#pr-f-wiki').checked = true;
        } else f.genitore = data.path;
        path.value = f.modo === 'esistente' ? f.path : f.genitore;
      }
    } catch (error) { errore.textContent = error.message; }
    b.disabled = false;
    aggiorna();
  };
  const invia = async () => {
    if (crea.disabled) return;
    crea.disabled = true;
    errore.textContent = '';
    const corpo = {
      path: f.modo === 'esistente' ? f.path.trim() : f.genitore.trim(),
      nome: nome.value.trim(),
      descrizione: $('#pr-f-descr').value.trim(),
      istruzioni: $('#pr-f-istr').value.trim(),
      wiki: $('#pr-f-wiki').checked,
      crea_cartella: f.modo === 'nuova' ? nome.value.trim() : '',
    };
    try {
      const data = await api('/api/progetti', { method: 'POST', body: JSON.stringify(corpo) });
      chiudiFinestra();
      toast(data.esisteva ? `«${data.progetto.nome}» aggiunto all'elenco` : `Progetto «${data.progetto.nome}» creato`);
      await refreshProgetti();
      openProgetto(data.progetto.path);
    } catch (error) {
      errore.textContent = error.message;
      crea.disabled = false;
    }
  };
  crea.onclick = invia;
  [nome, path].forEach((n) => n.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); invia(); }
  }));
  aggiorna();
}

// --- Impostazioni del progetto ----------------------------------------------

function finestraImpostazioniProgetto(p) {
  apriFinestra(`
    <div class="pr-finestra-testa">${prAvatar(p.nome, 'grande')}<div class="spinta">
      <h2 id="pr-finestra-titolo">Impostazioni del progetto</h2>
      <p>Si salvano nella cartella (<code>.progetto.json</code>): il progetto resta sé stesso anche se la sposti.</p></div>
      <button class="pr-chiudi" type="button" data-pr="chiudi" aria-label="Chiudi">${PR_ICONE.x}</button></div>
    <div class="pr-finestra-corpo">
      <div class="pr-campo"><label for="pr-f-nome">Nome</label>
        <input id="pr-f-nome" maxlength="120" value="${esc(p.nome)}" autofocus></div>
      <div class="pr-campo"><label for="pr-f-descr">Descrizione <span class="facoltativo">· solo per te, non arriva al modello</span></label>
        <textarea id="pr-f-descr" rows="2" maxlength="400">${esc(p.descrizione || '')}</textarea></div>
      <div class="pr-campo"><span class="pr-campo-titolo">Cartella</span>
        <div class="pr-anteprima-percorso">${esc(p.path)}</div>
        <span class="pr-campo-aiuto">Le istruzioni per il modello si scrivono nella loro scheda, nella schermata del progetto.</span></div>
      <details class="pr-avanzate" ${p.wiki ? 'open' : ''}><summary>${PR_CHEV}Avanzate</summary>
        <div class="pr-avanzate-corpo">
          <label class="pr-interruttore"><input type="checkbox" id="pr-f-wiki" ${p.wiki ? 'checked' : ''}><div>
            <strong>Modalità wiki</strong>
            <span>L'agente diventa il manutentore di una wiki LLM: <code>raw/</code> per le fonti, <code>wiki/</code>
            per le pagine, lo schema in <code>CLAUDE.md</code>. Accendendola la struttura viene creata; spegnendola
            le cartelle restano.${p.wiki ? ` Adesso: ${p.pagine} pagine, ${p.fonti} fonti.` : ''}</span></div></label>
          <div class="pr-zona-rischio"><div><strong>Togli dall'elenco</strong>
            Il progetto sparisce dalla colonna. La cartella, le conversazioni e <code>.progetto.json</code> restano:
            registrandola di nuovo ritrovi nome, istruzioni e memoria.</div>
            <button class="pr-bottone pericolo piccolo" type="button" data-pr="togli">Togli…</button></div>
        </div></details>
      <div class="pr-errore" id="pr-f-errore" role="alert"></div>
    </div>
    <div class="pr-finestra-piede"><span class="spinta"></span>
      <button class="pr-bottone" type="button" data-pr="chiudi">Annulla</button>
      <button class="pr-bottone primario" type="button" data-pr="salva">Salva</button>
    </div>`);
  const salva = $('#pr-finestra [data-pr="salva"]');
  const nome = $('#pr-f-nome');
  nome.oninput = () => { salva.disabled = !nome.value.trim(); };
  $('#pr-finestra [data-pr="togli"]').onclick = () => { chiudiFinestra(); toglieProgetto(p); };
  const invia = async () => {
    salva.disabled = true;
    const patch = { nome: nome.value.trim(), descrizione: $('#pr-f-descr').value };
    const wiki = $('#pr-f-wiki').checked;
    if (wiki !== !!p.wiki) patch.wiki = wiki;
    const ok = await salvaProgetto(patch, p.path, (m) => { $('#pr-f-errore').textContent = m; });
    if (ok) { chiudiFinestra(); toast('Impostazioni del progetto salvate'); } else salva.disabled = false;
  };
  salva.onclick = invia;
  nome.onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); invia(); } };
}

/** Salva un pezzo dell'identita' del progetto. Scrive nella cartella. */
async function salvaProgetto(patch, path = state.progetto?.path, errore = toast) {
  if (!path) return false;
  try {
    const data = await api('/api/progetti', { method: 'PATCH', body: JSON.stringify({ path, ...patch }) });
    if (state.progetto && prChiave(state.progetto.path) === prChiave(path)) state.progetto = data.progetto;
    await refreshProgetti();
    if (state.progettoHome) await ricaricaProgetto();
    return true;
  } catch (error) {
    errore(error.message);
    return false;
  }
}

async function toglieProgetto(p) {
  const ok = await finestraConferma({
    titolo: `Togliere «${p.nome}» dall'elenco?`,
    testo: 'La cartella, le conversazioni e la memoria restano sul disco. Le chat del progetto non compariranno più nella colonna finché non lo registri di nuovo.',
    conferma: 'Togli dall\'elenco', pericolo: true,
  });
  if (!ok) return;
  try {
    await api('/api/progetti/remove', { method: 'POST', body: JSON.stringify({ path: p.path }) });
    toast(`«${p.nome}» tolto dall'elenco`);
    if (state.progetto && prChiave(state.progetto.path) === prChiave(p.path)) {
      state.progetto = null;
      if (state.progettoHome) mostraProgettoHome(false);
    }
    state.progettiAperti.delete(p.path);
    await refreshProgetti();
    refreshSessions();
  } catch (error) { toast(error.message); }
}

// --- Libreria ----------------------------------------------------------------

async function finestraLibreria(v) {
  apriFinestra(`
    <div class="pr-finestra-testa"><div class="spinta"><h2 id="pr-finestra-titolo">${esc(v.titolo)}</h2>
      <p><code>.memoria/${esc(v.nome)}</code></p></div>
      <button class="pr-chiudi" type="button" data-pr="chiudi" aria-label="Chiudi">${PR_ICONE.x}</button></div>
    <div class="pr-finestra-corpo"><div class="pr-lettura" id="pr-lettura">${prScheletro()}</div></div>`, { larga: true });
  try {
    const data = await api(`/api/progetti/libreria?path=${encodeURIComponent(state.progetto.path)}&nome=${encodeURIComponent(v.nome)}`);
    const nodo = $('#pr-lettura');
    if (!nodo) return;
    nodo.innerHTML = markdown(data.testo) + (data.troncato ? '<p class="pr-vuoto">[troncata: il file continua]</p>' : '');
  } catch (error) {
    const nodo = $('#pr-lettura');
    if (nodo) nodo.innerHTML = `<p class="pr-errore">${esc(error.message)}</p>`;
  }
}

// ---------------------------------------------------------------------------
// Dentro una chat del progetto
// ---------------------------------------------------------------------------

/** La goccia in alto: il progetto della chat aperta, che riporta alla schermata. */
function renderProgettoChip() {
  const chip = $('#progetto-chip');
  if (!chip) return;
  const p = state.progetto;
  const visibile = !!p && !state.progettoHome;
  chip.hidden = !visibile;
  if (!visibile) return;
  chip.querySelector('.pr-chip-segno').outerHTML = `<span class="pr-chip-segno">${prAvatar(p.nome)}</span>`;
  $('#progetto-chip-nome').textContent = p.nome;
  chip.title = `Progetto «${p.nome}» — torna alla sua schermata`;
}

/** La memoria nel pannello di destra, dentro una chat del progetto. */
function renderMemoriaPannello() {
  const card = $('#pr-mem-card');
  if (!card) return;
  const p = state.progetto;
  card.hidden = !p;
  if (!p) return;
  const memoria = p.memoria || [];
  $('#pr-mem-conto').textContent = memoria.length ? `${memoria.length}/${PR_MAX_VOCI}` : 'vuota';
  const root = $('#pr-mem-voci');
  root.innerHTML = '';
  if (!memoria.length) {
    root.appendChild(el('div', 'empty', 'Ancora vuota: la scrive l\'harness alla fine dei turni in cui si lavora.'));
    return;
  }
  // Le piu' recenti prima: e' quello che e' appena cambiato che interessa
  // mentre si lavora; l'elenco completo, per tipo, sta nella schermata.
  const ordinate = [...memoria].sort((a, b) => String(b.aggiornata || '').localeCompare(String(a.aggiornata || '')));
  ordinate.slice(0, 6).forEach((v) => {
    const riga = el('div', 'pr-mem-riga' + (state.memoriaNuove.has(v.id) ? ' nuova' : ''));
    riga.dataset.tipo = v.tipo;
    riga.title = `${prTipo(v.tipo).uno}${v.titolo_chat ? ` · da «${v.titolo_chat}»` : ''}`;
    riga.innerHTML = `<span class="pr-tipo-segno"></span><span>${esc(v.testo)}</span>`;
    root.appendChild(riga);
  });
  if (memoria.length > 6) root.appendChild(el('div', 'pr-mem-altre', `e altre ${memoria.length - 6}`));
}

/** La goccia della memoria nel thread: cosa e' cambiato, e perche' se niente. */
function nodoMemoria(esito, motivo = '') {
  const agg = (esito && esito.aggiunte) || [];
  const mod = (esito && esito.modificate) || [];
  const tol = (esito && esito.tolte) || [];
  const cambiata = agg.length + mod.length + tol.length > 0;
  const nodo = el('details', 'pr-goccia' + (cambiata ? '' : ' muta'));
  const segno = `<span class="pr-goccia-segno">${PR_ICONE.memoria}</span>`;
  if (!cambiata) {
    nodo.innerHTML = `<summary>${segno}<span>Memoria del progetto: ${esc(motivo || 'niente da aggiungere.')}</span></summary>`;
    nodo.querySelector('summary').onclick = (e) => e.preventDefault();
    return nodo;
  }
  const riassunto = (esito && esito.riassunto) || '';
  const riga = (op, classe, v, extra = '') => `<div class="pr-goccia-riga"><span class="op ${classe}">${op}</span>`
    + `<span><span class="tipo">${esc(prTipo(v.tipo).uno.toLowerCase())}:</span> ${extra || esc(v.testo)}</span></div>`;
  nodo.innerHTML = `<summary>${PR_CHEV}${segno}<span><b>Memoria del progetto aggiornata</b></span>`
    + `<span class="num">${esc(riassunto)}</span></summary><div class="pr-goccia-corpo">`
    + agg.map((v) => riga('+', 'piu', v)).join('')
    + mod.map((m) => riga('~', 'tilde', m.dopo, `${esc(m.dopo.testo)}<span class="pr-goccia-prima">prima: ${esc(m.prima.testo)}</span>`)).join('')
    + tol.map((v) => riga('−', 'meno', v, `<del>${esc(v.testo)}</del>`)).join('')
    + '</div>';
  return nodo;
}

function nodoMemoriaViva() {
  return el('div', 'pr-goccia-vivo', '<i aria-hidden="true"></i><span>Aggiorno la memoria del progetto…</span>');
}

/** L'evento ``memoria`` del turno (core/agent.py, MemoriaProgetto). */
function eventoMemoria(event, turn, setStatus) {
  if (event.fase === 'inizio') {
    setStatus('Aggiorno la memoria del progetto…');
    turn.showFiles();
    turn._memoriaViva = nodoMemoriaViva();
    turn.append(turn._memoriaViva);
    turn._memoriaT0 = Date.now();
    if (typeof window !== 'undefined') window.Cruscotto?.toolInizio('memoria del progetto');
    return;
  }
  turn._memoriaViva?.remove();
  turn._memoriaViva = null;
  if (typeof window !== 'undefined') {
    window.Cruscotto?.toolFine('memoria del progetto', (Date.now() - (turn._memoriaT0 || Date.now())) / 1000);
  }
  turn.showFiles();
  turn.append(nodoMemoria(event.esito, event.motivo));
  const esito = event.esito || {};
  prSegnaNuove([...(esito.aggiunte || []), ...(esito.modificate || []).map((m) => m.dopo)].map((v) => v.id));
  refreshProgetti();
}

/** Il bus globale dice che un progetto e' cambiato (memoria, identita'). */
function progettoCambiato(event) {
  refreshProgetti();
  if (state.progettoHome && state.progetto && (!event.path || prChiave(event.path) === prChiave(state.progetto.path))) {
    // Non si ridisegna sotto le mani di chi sta scrivendo una voce o le istruzioni.
    if (!state.memoriaModulo && !state.istruzioniInModifica) ricaricaProgetto();
  }
}

/** La chat aperta appartiene a un progetto? Allora quello e' il progetto corrente. */
function allineaProgettoDellaChat(payload) {
  const suo = payload && payload.progetto;
  state.progetto = suo ? (prDelPercorso(suo.path) || { ...suo, memoria: [] }) : null;
  renderMemoriaPannello();
  renderProgettoChip();
  if (suo) state.progettiAperti.add(suo.path);
}

// ---------------------------------------------------------------------------
// Collegamenti
// ---------------------------------------------------------------------------

function bindProgettiUI() {
  $('#progetto-nuovo')?.addEventListener('click', () => finestraNuovoProgetto());
  $('#progetto-chip')?.addEventListener('click', () => {
    if (state.progetto) openProgetto(state.progetto.path);
  });
  $('#pr-mem-apri')?.addEventListener('click', () => {
    if (state.progetto) openProgetto(state.progetto.path);
  });
  const velo = $('#pr-velo');
  velo?.addEventListener('mousedown', (e) => { if (e.target === velo) chiudiFinestra(); });
  document.addEventListener('keydown', prFinestraTastiera, true);
  document.addEventListener('keydown', prMenuTastiera, true);
  document.addEventListener('mousedown', (e) => {
    const menu = $('#pr-menu');
    if (menu && !menu.hidden && !menu.contains(e.target) && !e.target.closest('.pr-piu, [data-pr="menu"]')) chiudiMenu();
  });
  window.addEventListener('resize', chiudiMenu);
  $('#sidebar')?.addEventListener('scroll', chiudiMenu, true);
}
