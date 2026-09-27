/* I passi del lavoro dell'agente: come si leggono in chat.
 *
 * Un passo e' un pensiero o una chiamata a tool. In chat non stanno piu' uno
 * per riquadro: stanno in un **blocco di lavoro**, una riga sola a lavoro
 * finito ("Ha lavorato 41 s · 2 letture · 1 modifica · 2 comandi") che si apre
 * in una traccia con una riga per passo, e ogni riga si apre sul suo dettaglio
 * gia' letto per tipo -- l'estratto con i numeri di riga, il diff, il
 * terminale -- con il JSON grezzo a un clic.
 *
 * Perche' un file a parte, condiviso da desktop e telefono: prima i due client
 * avevano due idee di "com'e' fatto un passo" (il desktop una tendina col nome
 * del tool e il JSON, il telefono una riga col verbo e basta), e ogni tool
 * nuovo andava insegnato due volte. Qui c'e' una copia sola. Il desktop lo
 * carica da /static/passi.js, il telefono da /comune/passi.js (server/mobile.py).
 *
 * Due strati, separati apposta:
 *   - funzioni **pure** (``riga``, ``vista``, ``riassunto``, ``frase``...):
 *     dal risultato di un tool ricavano dati, non nodi. Le prova la suite in
 *     QuickJS (tests/test_passi_web.py) senza un browser;
 *   - costruttori di nodi (``passoTool``, ``passoPensiero``, ``gruppo``): solo
 *     ``createElement`` e ``textContent`` per tutto quello che viene dal
 *     modello o dai file. ``innerHTML`` si usa solo per le icone, che sono
 *     costanti di questo file.
 *
 * Il dettaglio di un passo si costruisce **al primo clic**, come faceva la
 * tendina del desktop dalla v2.36: un ``write_file`` da 34 kB non entra nel DOM
 * per non essere guardato.
 */
(function (radice) {
  'use strict';

  // -------------------------------------------------------------------------
  // Icone: tratto a 16 px, colore dal testo. Costanti, mai dati del modello.
  // -------------------------------------------------------------------------

  const S = '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" '
    + 'stroke-width="1.45" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">';
  const ICONE = {
    pensiero: S + '<circle cx="8" cy="8" r="5.5" stroke-dasharray="2.2 2.2"/></svg>',
    leggi: S + '<path d="M4 1.75h5.25L12.5 5v9.25H4z"/><path d="M9 1.75V5.25h3.5"/></svg>',
    scrivi: S + '<path d="M4 1.75h5.25L12.5 5v9.25H4z"/><path d="M8.25 7.5v4M6.25 9.5h4"/></svg>',
    modifica: S + '<path d="M10.6 2.4l3 3-8 8H2.6v-3z"/></svg>',
    cerca: S + '<circle cx="7" cy="7" r="4.25"/><path d="M10.2 10.2l3.3 3.3"/></svg>',
    esegui: S + '<path d="M2.5 4l4 4-4 4"/><path d="M8 12.5h5.5"/></svg>',
    elenca: S + '<path d="M3 4h10M3 8h10M3 12h6"/></svg>',
    piano: S + '<path d="M2.5 4.2l1.3 1.3 2.2-2.4M2.5 10.7l1.3 1.3 2.2-2.4M8 4.5h5.5M8 11h5.5"/></svg>',
    note: S + '<path d="M3.5 2h9v12h-9z"/><path d="M6 5.5h4M6 8h4M6 10.5h2.5"/></svg>',
    memoria: S + '<path d="M8 2.2l1.6 3.3 3.6.5-2.6 2.5.6 3.6L8 10.4l-3.2 1.7.6-3.6-2.6-2.5 3.6-.5z"/></svg>',
    web: S + '<circle cx="8" cy="8" r="5.8"/><path d="M2.2 8h11.6M8 2.2c1.8 1.7 2.6 3.7 2.6 5.8S9.8 12.1 8 13.8C6.2 12.1 5.4 10.1 5.4 8S6.2 3.9 8 2.2z"/></svg>',
    anteprima: S + '<rect x="2" y="3" width="12" height="9" rx="1.2"/><path d="M5.5 14h5"/></svg>',
    esplora: S + '<circle cx="8" cy="8" r="5.8"/><path d="M10.4 5.6l-1.4 3.4-3.4 1.4 1.4-3.4z"/></svg>',
    domanda: S + '<circle cx="8" cy="8" r="5.8"/><path d="M6.3 6.3a1.8 1.8 0 1 1 2.4 1.7c-.5.2-.7.6-.7 1.1v.4M8 11.4v.1"/></svg>',
    altro: S + '<circle cx="8" cy="8" r="1.2"/><circle cx="3.8" cy="8" r="1.2"/><circle cx="12.2" cy="8" r="1.2"/></svg>',
    chev: '<svg viewBox="0 0 10 10" width="10" height="10" fill="none" stroke="currentColor" '
      + 'stroke-width="1.6" aria-hidden="true"><path d="M3.5 1.5L7 5l-3.5 3.5"/></svg>',
  };

  // -------------------------------------------------------------------------
  // Piccoli attrezzi di testo
  // -------------------------------------------------------------------------

  function tronca(valore, quanti) {
    const testo = String(valore ?? '').replace(/\s+/g, ' ').trim();
    if (testo.length <= quanti) return testo;
    // trimEnd prima dei puntini: "configura il …" sembra un errore di stampa.
    return `${testo.slice(0, quanti - 1).trimEnd()}…`;
  }

  function nomeFile(percorso) {
    const pulito = String(percorso ?? '').replace(/[\\/]+$/, '');
    const pezzi = pulito.split(/[\\/]/);
    return pezzi[pezzi.length - 1] || pulito || '?';
  }

  /** Il risultato di un tool come oggetto: arriva come stringa JSON dal
   *  server, ma un risultato non-JSON e' solo testo e non un errore. */
  function analizza(risultato) {
    if (risultato && typeof risultato === 'object') return risultato;
    try {
      const dati = JSON.parse(String(risultato ?? ''));
      return dati && typeof dati === 'object' && !Array.isArray(dati) ? dati : null;
    } catch (_) {
      return null;
    }
  }

  /** Durata leggibile, alla italiana: "0,4 s", "3,4 s", "41 s", "2 min 5 s".
   *  Sotto il decimo di secondo non dice niente a nessuno: stringa vuota. */
  function durata(secondi) {
    const s = Number(secondi);
    if (!Number.isFinite(s) || s < 0.1) return '';
    if (s < 10) return `${s.toFixed(1).replace('.', ',')} s`;
    if (s < 60) return `${Math.round(s)} s`;
    const tot = Math.round(s);
    return `${Math.floor(tot / 60)} min ${tot % 60} s`;
  }

  function plurale(n, uno, tanti) {
    return `${n} ${n === 1 ? uno : tanti}`;
  }

  /** L'inizio di un pensiero, per la riga chiusa: la prima frase, al massimo
   *  una riga di testo. Chi vuole il resto apre. */
  function estratto(testo) {
    const pulito = String(testo ?? '').replace(/\s+/g, ' ').trim();
    const frase = pulito.match(/^.{20,}?[.!?](?=\s|$)/);
    return tronca(frase ? frase[0] : pulito, 160);
  }

  // -------------------------------------------------------------------------
  // I tool: icona, verbo, oggetto
  // -------------------------------------------------------------------------

  const cartella = (a) => (a.subfolder && a.subfolder !== '.' ? ` in ${String(a.subfolder).replace(/\/+$/, '')}/` : '');

  // Il verbo prima dell'oggetto: "Legge tools.py" si capisce in mezzo secondo,
  // `read_file {"filepath": "core/tools.py"}` no. Del percorso resta il nome
  // del file (il percorso intero e' il titolo della riga): *cosa*, non *dove*.
  // ``frase`` e' la stessa cosa in minuscolo e in una stringa sola: e' quella
  // della striscia di attivita' del telefono ("legge tools.py").
  const TOOL = {
    read_file: {
      icona: 'leggi', verbo: 'Legge', oggetto: (a) => nomeFile(a.filepath), titolo: (a) => a.filepath,
      frase: (a) => `legge ${nomeFile(a.filepath)}`,
    },
    write_file: {
      icona: 'scrivi', verbo: 'Scrive', oggetto: (a) => nomeFile(a.filepath), titolo: (a) => a.filepath,
      frase: (a) => `scrive ${nomeFile(a.filepath)}`,
    },
    edit_file: {
      icona: 'modifica', verbo: 'Modifica', oggetto: (a) => nomeFile(a.filepath), titolo: (a) => a.filepath,
      frase: (a) => `modifica ${nomeFile(a.filepath)}`,
    },
    list_files: {
      icona: 'elenca', verbo: 'Elenca',
      oggetto: (a) => (a.pattern ? `"${tronca(a.pattern, 40)}"${cartella(a)}`
        : (a.subfolder && a.subfolder !== '.' ? `${String(a.subfolder).replace(/\/+$/, '')}/` : 'il workspace')),
      frase: (a) => `elenca ${a.subfolder && a.subfolder !== '.' ? nomeFile(a.subfolder) : 'il workspace'}`,
    },
    search_files: {
      icona: 'cerca', verbo: 'Cerca', oggetto: (a) => `"${tronca(a.pattern, 48)}"${cartella(a)}`,
      titolo: (a) => a.pattern,
      frase: (a) => `cerca "${tronca(a.pattern, 22)}"`,
    },
    run_command: {
      icona: 'esegui', verbo: 'Esegue', oggetto: (a) => tronca(a.command, 120), titolo: (a) => a.command,
      frase: (a) => `esegue ${tronca(a.command, 30)}`,
    },
    manage_plan: {
      icona: 'piano', verbo: 'Piano', oggetto: (a) => azionePiano(a),
      frase: (a) => `piano: ${a.action || 'aggiorna'}`,
    },
    manage_notes: {
      icona: 'note', verbo: 'Appunti', oggetto: (a) => tronca(a.text ? `${a.action || ''} · ${a.text}` : a.action || '', 80),
      frase: (a) => `appunti: ${a.action || 'aggiorna'}`,
    },
    manage_memory: {
      icona: 'memoria', verbo: 'Memoria', oggetto: (a) => tronca(a.content ? `${a.action || ''} · ${a.content}` : a.action || '', 80),
      frase: (a) => `memoria: ${a.action || 'aggiorna'}`,
    },
    preview: {
      icona: 'anteprima', verbo: 'Anteprima', oggetto: (a) => tronca([a.action, a.path || a.command].filter(Boolean).join(' · '), 80),
      frase: (a) => `anteprima${a.action ? `: ${a.action}` : ''}`,
    },
    esplora: {
      icona: 'esplora', verbo: 'Esplora', oggetto: (a) => tronca(a.compito, 90), titolo: (a) => a.compito,
      frase: () => 'manda un esploratore',
    },
    wiki_search: {
      icona: 'cerca', verbo: 'Cerca nella wiki', oggetto: (a) => `"${tronca(a.query, 48)}"`,
      frase: (a) => `cerca nella wiki "${tronca(a.query, 18)}"`,
    },
    vault_search: {  // nome di prima di v2.39: resta per le sessioni salvate
      icona: 'cerca', verbo: 'Cerca nel vault', oggetto: (a) => `"${tronca(a.query, 48)}"`,
      frase: (a) => `cerca nel vault ${tronca(a.vault, 18)}`,
    },
    web_search: {
      icona: 'web', verbo: 'Cerca sul web', oggetto: (a) => `"${tronca(a.query, 60)}"`,
      frase: (a) => `cerca sul web "${tronca(a.query, 22)}"`,
    },
    ask_user_question: {
      icona: 'domanda', verbo: 'Chiede', oggetto: (a) => tronca(a.question, 90),
      frase: () => 'ti fa una domanda',
    },
  };

  function azionePiano(a) {
    const azione = String(a.action || 'aggiorna');
    if (azione === 'set' && Array.isArray(a.steps)) return `imposta ${plurale(a.steps.length, 'punto', 'punti')}`;
    if (a.step_id != null && a.step_id !== '') return `${azione} · punto ${a.step_id}`;
    return azione;
  }

  function argomenti(args) {
    return args && typeof args === 'object' ? args : {};
  }

  /** La frase breve, in minuscolo: "legge tools.py". Un tool sconosciuto
   *  degrada al suo nome, mai a un'eccezione che fa saltare il disegno. */
  function frase(nome, args) {
    const def = TOOL[nome];
    if (!def) return String(nome || 'strumento');
    try {
      return tronca(def.frase(argomenti(args)), 46) || String(nome);
    } catch (_) {
      return String(nome);
    }
  }

  // -------------------------------------------------------------------------
  // Il diff di una edit_file, dagli argomenti
  // -------------------------------------------------------------------------

  /** Le righe cambiate fra ``old_string`` e ``new_string``: via le righe
   *  uguali in testa e in coda (restano come contesto), il resto e' tolto e
   *  aggiunto. Non e' un diff di Myers e non serve che lo sia: il frammento di
   *  una edit_file e' di poche righe. I numeri sono quelli del file DOPO la
   *  modifica, presi dalla finestra che il tool restituisce
   *  (``dopo_la_modifica``, core/tools.py:finestra_modifica). */
  function diff(args, r) {
    const vecchie = String(args.old_string ?? '').split('\n');
    const nuove = String(args.new_string ?? '').split('\n');
    let testa = 0;
    while (testa < vecchie.length && testa < nuove.length && vecchie[testa] === nuove[testa]) testa += 1;
    let coda = 0;
    while (coda < vecchie.length - testa && coda < nuove.length - testa
      && vecchie[vecchie.length - 1 - coda] === nuove[nuove.length - 1 - coda]) coda += 1;

    const primo = primaRigaNuova(nuove, r);
    const numero = (i) => (primo ? String(primo + i) : '');
    const righe = [];
    for (let i = 0; i < testa; i += 1) righe.push({ n: numero(i), segno: ' ', t: nuove[i] });
    for (let i = testa; i < vecchie.length - coda; i += 1) righe.push({ n: '', segno: '-', t: vecchie[i] });
    for (let i = testa; i < nuove.length - coda; i += 1) righe.push({ n: numero(i), segno: '+', t: nuove[i] });
    for (let i = nuove.length - coda; i < nuove.length; i += 1) righe.push({ n: numero(i), segno: ' ', t: nuove[i] });
    return {
      righe,
      tolte: Math.max(0, vecchie.length - coda - testa),
      aggiunte: Math.max(0, nuove.length - coda - testa),
    };
  }

  function primaRigaNuova(nuove, r) {
    const finestra = r && r.dopo_la_modifica;
    const testo = finestra && typeof finestra === 'object' ? finestra.testo : '';
    if (!testo) return 0;
    const cercata = String(nuove.find((x) => x.trim()) ?? '').trimEnd();
    if (!cercata) return 0;
    const salto = nuove.findIndex((x) => x.trim());
    for (const riga of String(testo).split('\n')) {
      const m = riga.match(/^\s*(\d+) \| ?(.*)$/);
      if (m && m[2].trimEnd() === cercata) return Number(m[1]) - salto;
    }
    return 0;
  }

  // -------------------------------------------------------------------------
  // La riga di un tool
  // -------------------------------------------------------------------------

  /** I dati della riga di una chiamata: cosa si vede a tendina chiusa.
   *
   *  ``meta`` e' l'informazione del risultato che vale la riga -- quante
   *  righe, quanti risultati, com'e' finito un comando -- e ``tono`` dice se
   *  va letta come buona o cattiva notizia. La durata compare solo sopra il
   *  secondo: "0.01s" su ogni riga era rumore. Senza risultato (il tool sta
   *  ancora girando) la riga e' quella del verbo e basta. */
  function riga(nome, args, risultato, ok, secondi) {
    const a = argomenti(args);
    const def = TOOL[nome] || { icona: 'altro', verbo: String(nome || 'strumento'), oggetto: () => '' };
    const r = analizza(risultato);
    const dati = {
      icona: def.icona,
      verbo: def.verbo,
      oggetto: '',
      titolo: '',
      meta: '',
      tono: '',
      piu: '',
      meno: '',
    };
    try {
      dati.oggetto = String(def.oggetto(a) ?? '');
      dati.titolo = String((def.titolo ? def.titolo(a) : '') || '');
    } catch (_) { /* argomenti strani: resta il verbo */ }
    if (risultato === undefined || risultato === null) return dati;

    const tempo = durata(secondi);
    const errore = r && r.error ? String(r.error) : '';
    if (nome === 'run_command' && r && !errore) {
      const codice = r.returncode;
      const esito = String(r.esito || (codice === 0 ? 'ok' : ''));
      const fallito = ok === false || (codice != null && codice !== 0);
      dati.tono = fallito ? 'err' : 'ok';
      dati.meta = [
        fallito ? (codice != null ? `exit ${codice}` : esito.toLowerCase() || 'fallito') : 'ok',
        durata(r.duration_s ?? secondi),
      ].filter(Boolean).join(' · ');
      return dati;
    }
    if (errore || ok === false) {
      dati.tono = 'err';
      dati.meta = 'errore';
      if (errore) dati.titolo = errore;
      return dati;
    }
    let meta = '';
    if (r) {
      if (nome === 'read_file') {
        if (r.status === 'invariato') meta = 'già letto';
        else if (r.range && r.range !== 'intero file') meta = `righe ${String(r.range).replace('-', '–')}`;
        else if (r.total_lines != null) meta = plurale(Number(r.total_lines), 'riga', 'righe');
      } else if (nome === 'write_file') {
        const azione = r.action === 'sovrascritto' ? 'riscritto' : (r.action || 'scritto');
        meta = r.lines != null ? `${azione} · ${plurale(Number(r.lines), 'riga', 'righe')}` : azione;
      } else if (nome === 'edit_file') {
        const d = diff(a, r);
        const volte = Math.max(1, Number(r.replacements) || 1);
        if (d.aggiunte) dati.piu = `+${d.aggiunte * volte}`;
        if (d.tolte) dati.meno = `−${d.tolte * volte}`;
        if (!d.aggiunte && !d.tolte) meta = 'nessun cambiamento';
      } else if (nome === 'search_files' || nome === 'list_files') {
        meta = contaRisultati(nome, r);
      }
    }
    dati.meta = [meta, tempo].filter(Boolean).join(' · ');
    return dati;
  }

  const VUOTI = /^\((nessuna corrispondenza|nessun file corrisponde|cartella vuota)\)$/;

  function contaRisultati(nome, r) {
    if (Array.isArray(r.matches)) {
      const n = r.match_count ?? r.matches.filter((m) => !VUOTI.test(m)).length;
      return n ? plurale(Number(n), 'risultato', 'risultati') : 'nessun risultato';
    }
    if (Array.isArray(r.files)) {
      const n = r.match_count ?? r.files.filter((m) => !VUOTI.test(m)).length;
      return n ? plurale(Number(n), 'file', 'file') : 'nessun file';
    }
    if (Array.isArray(r.counts)) {
      const n = r.counts.filter((m) => !VUOTI.test(m)).length;
      return n ? plurale(n, 'file', 'file') : 'nessun risultato';
    }
    if (nome === 'list_files' && r.entries != null) return plurale(Number(r.entries), 'voce', 'voci');
    return '';
  }

  // -------------------------------------------------------------------------
  // Il dettaglio di un tool, letto per tipo
  // -------------------------------------------------------------------------

  // Oltre, il dettaglio diventerebbe il write_file da 34 kB che la tendina
  // costruita al primo clic esisteva per non mettere nel DOM. Il resto sta nel
  // JSON grezzo, che e' un nodo di testo solo.
  const MAX_RIGHE = 400;

  function numerate(testo, da) {
    const tutte = String(testo ?? '').split('\n');
    const righe = [];
    let n = da;
    for (const t of tutte.slice(0, MAX_RIGHE)) {
      // Il segno di smart_truncate (core/textutils.py) interrompe la
      // numerazione: le righe dopo il taglio non sono piu' quelle del file.
      if (/^\[\.\.\. .* \.\.\.\]$/.test(t.trim())) {
        righe.push({ n: '', t: t.trim(), nota: true });
        n = null;
        continue;
      }
      righe.push({ n: n == null ? '' : String(n), t });
      if (n != null) n += 1;
    }
    return { righe, oltre: Math.max(0, tutte.length - MAX_RIGHE) };
  }

  /** Il dettaglio di una chiamata, come dati. Ogni tipo dice il suo:
   *
   *    estratto  righe numerate (read_file, write_file)
   *    diff      righe con segno e numero (edit_file)
   *    ricerca   corrispondenze raggruppate per file (search_files)
   *    elenco    righe di testo (list_files, search_files a file o conteggi)
   *    terminale comando, codice di uscita e uscita (run_command)
   *    errore    il messaggio e il suggerimento del tool
   *    campi     chiave: valore, per tutti gli altri
   */
  function vista(nome, args, risultato) {
    const a = argomenti(args);
    const r = analizza(risultato);
    if (r && r.error) {
      return { tipo: 'errore', testa: nome, testo: String(r.error), suggerimento: r.hint ? String(r.hint) : '' };
    }
    if (nome === 'read_file' && r) {
      if (r.status === 'invariato') return { tipo: 'campi', testa: r.filepath || a.filepath || '', righe: [{ k: 'nota', v: String(r.note || 'già letto') }] };
      const da = r.range && r.range !== 'intero file' ? Number(String(r.range).split('-')[0]) || 1 : 1;
      const { righe, oltre } = numerate(r.content, da);
      const quale = r.range && r.range !== 'intero file' ? `righe ${String(r.range).replace('-', '–')}` : 'intero file';
      return {
        tipo: 'estratto',
        testa: [r.filepath || a.filepath, quale, r.total_lines != null ? `${r.total_lines} righe` : ''].filter(Boolean).join(' · '),
        righe,
        nota: [r.truncated ? 'contenuto tagliato dal tool' : '', oltre ? `altre ${oltre} righe nel JSON grezzo` : ''].filter(Boolean).join(' · '),
      };
    }
    if (nome === 'write_file') {
      const { righe, oltre } = numerate(a.content, 1);
      const azione = r && r.action === 'sovrascritto' ? 'riscritto' : (r && r.action) || '';
      return {
        tipo: 'estratto',
        testa: [r && r.filepath ? r.filepath : a.filepath, azione].filter(Boolean).join(' · '),
        righe,
        nota: [avvisoSintassi(r), oltre ? `altre ${oltre} righe nel JSON grezzo` : ''].filter(Boolean).join(' · '),
      };
    }
    if (nome === 'edit_file') {
      const d = diff(a, r);
      const volte = r && Number(r.replacements) > 1 ? ` · ${r.replacements} sostituzioni` : '';
      return {
        tipo: 'diff',
        testa: `${(r && r.filepath) || a.filepath || ''}${volte}`,
        righe: d.righe,
        nota: [r && r.nota_aggancio ? String(r.nota_aggancio) : '', avvisoSintassi(r)].filter(Boolean).join(' · '),
      };
    }
    if (nome === 'search_files' && r && Array.isArray(r.matches)) {
      const righe = [];
      for (const m of r.matches.slice(0, MAX_RIGHE)) {
        const p = String(m).match(/^(.+?):(\d+):([> ]?) ?(.*)$/);
        if (p) righe.push({ file: p[1], n: p[2], t: p[4], centro: p[3] !== ' ' });
        else righe.push({ file: '', n: '', t: String(m) });
      }
      return { tipo: 'ricerca', testa: `"${a.pattern ?? r.pattern ?? ''}"${cartella(a)}`, cerca: String(a.pattern ?? r.pattern ?? ''), righe, nota: r.nota ? String(r.nota) : '' };
    }
    if ((nome === 'search_files' || nome === 'list_files') && r) {
      const lista = r.files || r.counts || (r.tree != null ? String(r.tree).split('\n') : null);
      if (lista) {
        return {
          tipo: 'elenco',
          testa: nome === 'list_files' ? (a.subfolder && a.subfolder !== '.' ? a.subfolder : 'workspace') : `"${a.pattern ?? ''}"`,
          righe: lista.slice(0, MAX_RIGHE).map((t) => ({ t: String(t) })),
          nota: r.truncated ? 'elenco tagliato dal tool' : '',
        };
      }
    }
    if (nome === 'run_command' && r) {
      const uscita = [r.stdout, r.stderr ? `--- stderr ---\n${r.stderr}` : '', r.output && !r.stdout && !r.stderr ? r.output : '']
        .filter((x) => x && String(x).trim()).join('\n');
      return {
        tipo: 'terminale',
        testa: String(r.command || a.command || ''),
        codice: r.returncode != null ? `exit ${r.returncode}` : String(r.esito || ''),
        ok: r.returncode === 0,
        uscita: uscita.replace(/\s+$/, '') || '(nessuna uscita)',
      };
    }
    return { tipo: 'campi', testa: nome, righe: campi(a, r, risultato) };
  }

  function avvisoSintassi(r) {
    const avviso = r && r.avviso_sintassi;
    if (!avviso) return '';
    return typeof avviso === 'object'
      ? `avviso di sintassi${avviso.riga ? ` alla riga ${avviso.riga}` : ''}: ${avviso.messaggio || avviso.msg || ''}`.trim()
      : `avviso di sintassi: ${avviso}`;
  }

  /** Chiave: valore, per i tool senza una vista loro. Gli argomenti prima (cosa
   *  gli e' stato chiesto), poi il risultato. Le stringhe restano stringhe:
   *  niente ``\n`` letterali come nel JSON. */
  function campi(a, r, grezzo) {
    const righe = [];
    const valore = (v) => (typeof v === 'string' ? v : JSON.stringify(v, null, 2));
    for (const [k, v] of Object.entries(a)) righe.push({ k, v: valore(v), argomento: true });
    if (r) for (const [k, v] of Object.entries(r)) righe.push({ k, v: valore(v) });
    else if (grezzo != null && String(grezzo).trim()) righe.push({ k: 'risultato', v: String(grezzo) });
    return righe;
  }

  /** Argomenti e risultato come li ha visti il modello, per chi vuole il
   *  grezzo. Un nodo di testo solo. */
  function grezzo(args, risultato) {
    const r = analizza(risultato);
    return `Argomenti\n${JSON.stringify(argomenti(args), null, 2)}\n\nRisultato\n`
      + (r ? JSON.stringify(r, null, 2) : String(risultato ?? ''));
  }

  // -------------------------------------------------------------------------
  // Il riassunto di un blocco
  // -------------------------------------------------------------------------

  const CATEGORIE = [
    ['leggi', 'lettura', 'letture'],
    ['cerca', 'ricerca', 'ricerche'],
    ['web', 'ricerca web', 'ricerche web'],
    ['modifica', 'modifica', 'modifiche'],
    ['scrivi', 'file scritto', 'file scritti'],
    ['esegui', 'comando', 'comandi'],
    ['elenca', 'elenco', 'elenchi'],
  ];

  /** "2 letture · 1 ricerca · 2 modifiche · 2 comandi", piu' com'e' andato
   *  l'ultimo comando e quanti tool sono andati in errore.
   *
   *  ``passi``: ``[{nome, ok, risultato}]`` dei soli tool. L'esito dell'ultimo
   *  comando e' quello che si guarda per primo ("i test sono verdi?"), e si
   *  dice per quello che e' -- l'ultimo comando -- perche' dal client non si
   *  sa quale comando fosse una verifica. */
  function riassunto(passi) {
    const conta = {};
    let errori = 0;
    let comando = null;
    for (const p of passi) {
      const def = TOOL[p.nome];
      const icona = def ? def.icona : 'altro';
      conta[icona] = (conta[icona] || 0) + 1;
      const d = riga(p.nome, p.args, p.risultato, p.ok, 0);
      if (p.nome === 'run_command') comando = d.tono === 'err' ? 'fallito' : 'ok';
      else if (d.tono === 'err') errori += 1;
    }
    const pezzi = [];
    let noti = 0;
    for (const [icona, uno, tanti] of CATEGORIE) {
      if (conta[icona]) { pezzi.push(plurale(conta[icona], uno, tanti)); noti += conta[icona]; }
    }
    const altri = passi.length - noti;
    if (altri) pezzi.push(plurale(altri, 'altra azione', 'altre azioni'));
    return { testo: pezzi.join(' · '), comando, errori };
  }

  // -------------------------------------------------------------------------
  // Nodi
  // -------------------------------------------------------------------------

  const doc = () => radice.document;

  function nodo(tag, classe, testo) {
    const n = doc().createElement(tag);
    if (classe) n.className = classe;
    if (testo !== undefined && testo !== null) n.textContent = String(testo);
    return n;
  }

  function icona(nome) {
    const n = nodo('span', 'ps-ico');
    n.innerHTML = ICONE[nome] || ICONE.altro;
    return n;
  }

  /** Il contenuto della riga: icona, verbo, oggetto, meta. */
  function riempiRiga(bottone, d, stato) {
    bottone.textContent = '';
    const ico = stato === 'corso' ? nodo('span', 'ps-ico') : icona(d.icona);
    if (stato === 'corso') ico.appendChild(nodo('span', 'ps-ruota'));
    if (d.tono === 'err') ico.classList.add('err');
    bottone.appendChild(ico);
    bottone.appendChild(nodo('span', 'ps-verbo', d.verbo));
    if (d.oggetto) bottone.appendChild(nodo('span', 'ps-oggetto', d.oggetto));
    const meta = nodo('span', 'ps-meta' + (d.tono ? ` ${d.tono}` : ''));
    if (d.piu) meta.appendChild(nodo('span', 'ps-piu', d.piu));
    if (d.meno) meta.appendChild(nodo('span', 'ps-meno', d.meno));
    if (d.meta) meta.appendChild(nodo('span', '', d.meta));
    bottone.appendChild(meta);
    bottone.title = d.titolo || [d.verbo, d.oggetto].filter(Boolean).join(' ');
  }

  function righeCodice(contenitore, righe, conSegno) {
    for (const r of righe) {
      const riga = nodo('div', 'ps-ln' + (r.segno === '+' ? ' piu' : r.segno === '-' ? ' meno' : '') + (r.nota ? ' nota' : ''));
      riga.appendChild(nodo('span', 'ps-n', r.n ?? ''));
      if (conSegno) riga.appendChild(nodo('span', 'ps-sg', r.segno === ' ' ? '' : r.segno));
      riga.appendChild(nodo('span', 'ps-t', r.t));
      contenitore.appendChild(riga);
    }
  }

  function scatola(testa, destra) {
    const box = nodo('div', 'ps-code');
    const t = nodo('div', 'ps-code-testa');
    t.appendChild(nodo('span', 'ps-code-titolo', testa));
    if (destra) t.appendChild(destra);
    box.appendChild(t);
    return box;
  }

  /** Il nodo del dettaglio, da ``vista()``. */
  function nodoVista(v) {
    const wrap = nodo('div', 'ps-vista');
    if (v.tipo === 'estratto' || v.tipo === 'diff') {
      const box = scatola(v.testa);
      const corpo = nodo('div', 'ps-code-corpo');
      righeCodice(corpo, v.righe, v.tipo === 'diff');
      box.appendChild(corpo);
      wrap.appendChild(box);
    } else if (v.tipo === 'ricerca') {
      const box = scatola(v.testa);
      const corpo = nodo('div', 'ps-code-corpo');
      let file = null;
      for (const r of v.righe) {
        if (r.file !== file) {
          file = r.file;
          if (file) corpo.appendChild(nodo('div', 'ps-ln-file', file));
        }
        const riga = nodo('div', 'ps-ln' + (r.centro === false ? ' contesto' : ''));
        riga.appendChild(nodo('span', 'ps-n', r.n));
        const t = nodo('span', 'ps-t');
        evidenzia(t, r.t, v.cerca);
        riga.appendChild(t);
        corpo.appendChild(riga);
      }
      box.appendChild(corpo);
      wrap.appendChild(box);
    } else if (v.tipo === 'elenco') {
      const box = scatola(v.testa);
      const corpo = nodo('div', 'ps-code-corpo');
      for (const r of v.righe) {
        const riga = nodo('div', 'ps-ln');
        riga.appendChild(nodo('span', 'ps-t', r.t));
        corpo.appendChild(riga);
      }
      box.appendChild(corpo);
      wrap.appendChild(box);
    } else if (v.tipo === 'terminale') {
      const box = scatola(`$ ${v.testa}`, nodo('span', `ps-exit ${v.ok ? 'ok' : 'err'}`, v.codice));
      // column-reverse: lo scorrimento parte dal fondo, dove un comando
      // mette l'errore e il riepilogo dei test.
      const corpo = nodo('div', 'ps-code-corpo ps-term');
      corpo.appendChild(nodo('pre', 'ps-term-testo', v.uscita));
      box.appendChild(corpo);
      wrap.appendChild(box);
    } else if (v.tipo === 'errore') {
      const box = nodo('div', 'ps-errore');
      box.appendChild(nodo('div', '', v.testo));
      if (v.suggerimento) box.appendChild(nodo('div', 'ps-errore-nota', v.suggerimento));
      wrap.appendChild(box);
    } else {
      const box = nodo('div', 'ps-campi');
      for (const r of v.righe || []) {
        const riga = nodo('div', 'ps-campo');
        riga.appendChild(nodo('span', 'ps-k', r.k));
        riga.appendChild(nodo('span', 'ps-v', r.v));
        box.appendChild(riga);
      }
      if (!(v.righe || []).length) box.appendChild(nodo('span', 'ps-v', '(niente)'));
      wrap.appendChild(box);
    }
    if (v.nota) wrap.appendChild(nodo('div', 'ps-nota', v.nota));
    return wrap;
  }

  function evidenzia(contenitore, testo, cercato) {
    const t = String(testo ?? '');
    let i = -1;
    try {
      i = cercato ? t.search(new RegExp(cercato)) : -1;
    } catch (_) {
      i = cercato ? t.indexOf(cercato) : -1;
    }
    let lungo = 0;
    if (i >= 0) {
      try { lungo = (t.slice(i).match(new RegExp(cercato)) || [''])[0].length; } catch (_) { lungo = cercato.length; }
    }
    if (i < 0 || !lungo) { contenitore.textContent = t; return; }
    contenitore.appendChild(doc().createTextNode(t.slice(0, i)));
    contenitore.appendChild(nodo('mark', 'ps-hit', t.slice(i, i + lungo)));
    contenitore.appendChild(doc().createTextNode(t.slice(i + lungo)));
  }

  function apribile(passo, bottone, costruisci) {
    let corpo = null;
    bottone.setAttribute('aria-expanded', 'false');
    bottone.addEventListener('click', () => {
      if (!corpo) {
        corpo = costruisci();
        if (!corpo) return;
        passo.appendChild(corpo);
      } else {
        corpo.hidden = !corpo.hidden;
      }
      const aperto = !corpo.hidden;
      bottone.setAttribute('aria-expanded', aperto ? 'true' : 'false');
      passo.classList.toggle('aperto', aperto);
    });
  }

  /** Un passo di tool: la riga, e il dettaglio al primo clic.
   *
   *  ``dati``: ``{nome, args, risultato, ok, durata, id}``. Senza
   *  ``risultato`` il passo nasce "in corso" (evento tool_start) e si chiude
   *  con ``concludiTool``. */
  function passoTool(dati) {
    const passo = nodo('div', 'ps-passo ps-tool');
    if (dati.id) passo.dataset.call = String(dati.id);
    passo._ps = { ...dati };
    const bottone = nodo('button', 'ps-riga');
    bottone.type = 'button';
    passo.appendChild(bottone);
    const inCorso = dati.risultato === undefined;
    riempiRiga(bottone, riga(dati.nome, dati.args, dati.risultato, dati.ok, dati.durata), inCorso ? 'corso' : '');
    if (inCorso) passo.classList.add('corso');
    apribile(passo, bottone, () => {
      const d = passo._ps;
      if (d.risultato === undefined) return null;   // ancora in corso: niente da aprire
      const corpo = nodo('div', 'ps-corpo');
      corpo.appendChild(nodoVista(vista(d.nome, d.args, d.risultato)));
      const piede = nodo('div', 'ps-piede');
      const link = nodo('button', 'ps-grezzo', 'JSON grezzo');
      link.type = 'button';
      let pre = null;
      link.addEventListener('click', () => {
        if (!pre) {
          pre = nodo('pre', 'ps-grezzo-testo', grezzo(d.args, d.risultato));
          corpo.appendChild(pre);
        } else {
          pre.hidden = !pre.hidden;
        }
        link.textContent = pre.hidden ? 'JSON grezzo' : 'nascondi JSON';
      });
      piede.appendChild(link);
      corpo.appendChild(piede);
      return corpo;
    });
    return passo;
  }

  function concludiTool(passo, dati) {
    Object.assign(passo._ps, dati);
    passo.classList.remove('corso');
    const d = passo._ps;
    riempiRiga(passo.querySelector('.ps-riga'), riga(d.nome, d.args, d.risultato, d.ok, d.durata), '');
  }

  // Quanto testo tiene la finestra del pensiero dal vivo. Si vedono quattro
  // righe; il resto sta nella stringa, e si legge aprendo il pensiero chiuso.
  const CODA_FINESTRA = 1200;

  /** Un passo di pensiero. Nasce vivo, con la finestra di quattro righe che
   *  scorre; ``chiudi`` lo riduce alla riga con l'inizio del testo, che si
   *  apre sul testo intero. */
  function passoPensiero() {
    const passo = nodo('div', 'ps-passo ps-pensiero vivo');
    const bottone = nodo('button', 'ps-riga');
    bottone.type = 'button';
    const ico = icona('pensiero');
    ico.classList.add('gira');
    bottone.appendChild(ico);
    bottone.appendChild(nodo('span', 'ps-verbo', 'Sta pensando'));
    const meta = nodo('span', 'ps-meta');
    bottone.appendChild(meta);
    passo.appendChild(bottone);
    const finestra = nodo('div', 'ps-finestra');
    const testoFinestra = nodo('div', 'ps-finestra-testo');
    finestra.appendChild(testoFinestra);
    passo.appendChild(finestra);

    let testo = '';
    let chiuso = false;
    let riassunto = null;
    const ctl = {
      nodo: passo,
      get testo() { return testo; },
      get chiuso() { return chiuso; },
      imposta(t) {
        testo = String(t ?? '');
        if (!chiuso) testoFinestra.textContent = testo.slice(-CODA_FINESTRA);
        else if (riassunto) riassunto.textContent = estratto(testo) || '(pensiero vuoto)';
      },
      accoda(pezzo) {
        this.imposta(testo + String(pezzo ?? ''));
      },
      tempo(secondi) {
        if (!chiuso) meta.textContent = durata(secondi);
      },
      chiudi(secondi) {
        if (chiuso) return;
        chiuso = true;
        passo.classList.remove('vivo');
        finestra.remove();
        bottone.textContent = '';
        bottone.appendChild(icona('pensiero'));
        riassunto = nodo('span', 'ps-estratto', estratto(testo) || '(pensiero vuoto)');
        bottone.appendChild(riassunto);
        bottone.appendChild(nodo('span', 'ps-meta', durata(secondi)));
        bottone.title = 'Pensiero';
        apribile(passo, bottone, () => {
          const corpo = nodo('div', 'ps-corpo');
          corpo.appendChild(nodo('div', 'ps-pensiero-testo', testo));
          return corpo;
        });
      },
    };
    passo._ctl = ctl;
    return ctl;
  }

  /** Il blocco di lavoro: tutti i passi fra due testi del modello.
   *
   *  Vivo: intestazione "Sta lavorando · passo 6 · 31 s", i passi in colonna
   *  e, se sono tanti, i piu' vecchi piegati in "N passi prima". Chiuso: una
   *  riga sola con durata, conteggi e com'e' andato l'ultimo comando; si apre
   *  sulla traccia intera. */
  function gruppo(opzioni = {}) {
    const box = nodo('div', 'ps-gruppo vivo');
    const testa = nodo('button', 'ps-testa');
    testa.type = 'button';
    testa.setAttribute('aria-expanded', 'true');
    const segno = nodo('span', 'ps-ico');
    segno.appendChild(nodo('span', 'ps-ruota'));
    const titolo = nodo('span', 'ps-titolo', 'Sta lavorando');
    const meta = nodo('span', 'ps-meta-gruppo');
    testa.append(segno, titolo, meta);
    const traccia = nodo('div', 'ps-traccia');
    const piega = nodo('button', 'ps-piega');
    piega.type = 'button';
    piega.hidden = true;
    traccia.appendChild(piega);
    box.append(testa, traccia);

    const MOSTRATI_VIVO = opzioni.mostratiVivo ?? 4;
    const tools = [];
    let chiuso = false;
    let spiegato = false;

    const passi = () => [...traccia.childNodes].filter((n) => n !== piega);

    function ripiega() {
      if (chiuso) return;
      const tutti = passi();
      const nascosti = spiegato ? 0 : Math.max(0, tutti.length - MOSTRATI_VIVO);
      tutti.forEach((n, i) => { n.hidden = i < nascosti; });
      piega.hidden = nascosti === 0;
      piega.textContent = '';
      piega.appendChild(icona('elenca'));
      piega.appendChild(nodo('span', '', plurale(nascosti, 'passo prima', 'passi prima')));
    }
    piega.addEventListener('click', () => { spiegato = true; ripiega(); });

    const ctl = {
      nodo: box,
      aggiungi(passo) {
        traccia.appendChild(passo);
        ripiega();
      },
      segnaTool(dati) { tools.push(dati); },
      stato(testo) { if (!chiuso) meta.textContent = testo ? `· ${testo}` : ''; },
      get chiuso() { return chiuso; },
      vuoto() { return passi().length === 0; },
      chiudi(secondi) {
        if (chiuso) return;
        chiuso = true;
        box.classList.remove('vivo');
        passi().forEach((n) => { n.hidden = false; });
        piega.remove();
        const r = riassunto(tools);
        const t = durata(secondi);
        testa.textContent = '';
        const chev = nodo('span', 'ps-ico ps-chev');
        chev.innerHTML = ICONE.chev;
        testa.appendChild(chev);
        testa.appendChild(nodo('span', 'ps-titolo',
          tools.length ? (t ? `Ha lavorato ${t}` : 'Ha lavorato') : (t ? `Ha pensato ${t}` : 'Ha pensato')));
        const pezzi = [r.testo, r.errori ? plurale(r.errori, 'errore', 'errori') : ''].filter(Boolean);
        if (pezzi.length) testa.appendChild(nodo('span', 'ps-meta-gruppo', `· ${pezzi.join(' · ')}`));
        if (r.comando) {
          // "ultimo comando" si toglie dove manca lo spazio (il telefono):
          // resta l'esito, che e' la parte che si guarda.
          const pill = nodo('span', `ps-pill ${r.comando === 'ok' ? 'ok' : 'err'}`);
          pill.appendChild(nodo('span', 'ps-pill-lungo', 'ultimo comando '));
          pill.appendChild(doc().createTextNode(r.comando === 'ok' ? 'ok' : 'fallito'));
          pill.title = r.comando === 'ok' ? "L'ultimo comando del blocco e' finito bene" : "L'ultimo comando del blocco e' fallito";
          testa.appendChild(pill);
        }
        testa.title = pezzi.join(' · ');
        box.classList.remove('aperto');
        traccia.hidden = true;
        testa.setAttribute('aria-expanded', 'false');
        testa.addEventListener('click', () => {
          traccia.hidden = !traccia.hidden;
          box.classList.toggle('aperto', !traccia.hidden);
          testa.setAttribute('aria-expanded', traccia.hidden ? 'false' : 'true');
        });
      },
    };
    box._ctl = ctl;
    return ctl;
  }

  /** La regia dei blocchi di lavoro di un turno: quando se ne apre uno,
   *  dove finiscono pensieri e tool, quando si chiude, quanto e' durato.
   *
   *  Condivisa perche' e' la parte con le regole sottili, e due copie
   *  divergerebbero:
   *    - un testo del modello chiude il blocco (lo decide chi ospita,
   *      chiamando ``chiudi``); il passo dopo ne apre uno nuovo;
   *    - a fine passo il server rimanda il pensiero intero (core/agent.py,
   *      "Il testo completo, una volta per passo"), anche dopo che la risposta
   *      ha chiuso il blocco: va al pensiero di quel passo, gia' chiuso, e non
   *      deve aprire un blocco nuovo sotto la risposta;
   *    - le durate sono sull'orologio del server (``t`` degli eventi, ``ts``
   *      dei messaggi salvati), mai su quello del browser.
   *
   *  ``opzioni.inserisci(nodo)``: mette in pagina un blocco nuovo, in coda.
   *  ``opzioni.vivo()``: il turno e' dal vivo (conta i secondi).
   *  ``opzioni.dopo()``: dopo ogni cambiamento visibile (scorrimento). */
  function lavoro(opzioni = {}) {
    const inserisci = opzioni.inserisci || (() => {});
    const vivo = opzioni.vivo || (() => false);
    const dopo = opzioni.dopo || (() => {});
    let g = null;
    let pensiero = null;
    let pensieroDelPasso = null;
    let ora = null;
    let inizioPasso = null;
    let passo = null;
    let nota = '';
    let ticker = null;
    // L'ora in cui si e' chiuso l'ultimo blocco: se il modello, nello stesso
    // passo, ha scritto e poi chiamato un tool, il blocco nuovo comincia da
    // li' e non dall'inizio del passo -- il testo non e' lavoro del blocco.
    let chiusoAlle = null;

    const fin = (x) => typeof x === 'number' && Number.isFinite(x);
    const adesso = () => Date.now() / 1000;

    function stato() {
      if (!g || g.chiuso) return;
      const pezzi = [];
      if (passo) pezzi.push(`passo ${passo}`);
      if (nota) pezzi.push(nota);
      if (vivo() && fin(g.inizio)) pezzi.push(durata(adesso() - g.inizio));
      g.stato(pezzi.filter(Boolean).join(' · '));
      if (pensiero && vivo() && fin(pensiero.inizio)) pensiero.tempo(adesso() - pensiero.inizio);
    }

    function avviaTicker() {
      if (ticker || !vivo() || typeof setInterval !== 'function') return;
      ticker = setInterval(() => {
        if (!g || !g.nodo.isConnected) { clearInterval(ticker); ticker = null; return; }
        stato();
      }, 1000);
    }

    function blocco(inizio) {
      if (!g) {
        g = gruppo();
        const dalPasso = fin(inizioPasso) ? Math.max(inizioPasso, fin(chiusoAlle) ? chiusoAlle : -Infinity) : null;
        g.inizio = fin(inizio) ? inizio : (fin(dalPasso) ? dalPasso : ora);
        g.fine = null;
        inserisci(g.nodo);
        avviaTicker();
        stato();
      }
      return g;
    }

    function chiudiPensiero(fine) {
      if (!pensiero) return;
      const p = pensiero;
      pensiero = null;
      const f = fin(fine) ? fine : ora;
      p.chiudi(fin(p.inizio) && fin(f) && f >= p.inizio ? f - p.inizio : null);
      if (g && fin(f)) g.fine = f;
    }

    function pensa(inizio) {
      if (!pensiero) {
        const b = blocco(inizio);
        pensiero = passoPensiero();
        pensiero.inizio = fin(inizio) ? inizio : ora;
        pensieroDelPasso = pensiero;
        b.aggiungi(pensiero.nodo);
        stato();
      }
      return pensiero;
    }

    return {
      get aperto() { return Boolean(g); },
      get nodo() { return g ? g.nodo : null; },
      ora(t) { if (fin(t)) ora = t; },
      segnaPasso(n) {
        passo = n;
        inizioPasso = ora;
        pensieroDelPasso = null;
        nota = '';
        stato();
      },
      nota(testo) { nota = String(testo || ''); stato(); },
      setThinking(testo) {
        if (!pensiero && pensieroDelPasso) { pensieroDelPasso.imposta(testo); return; }
        pensa().imposta(testo);
        dopo();
      },
      appendThinking(pezzo) {
        if (!pensiero && pensieroDelPasso) { pensieroDelPasso.accoda(pezzo); return; }
        pensa().accoda(pezzo);
        dopo();
      },
      /** Un pensiero intero, da un messaggio salvato: nasce gia' chiuso. */
      pensieroSalvato(testo, inizio, fine) {
        pensa(inizio).imposta(testo);
        chiudiPensiero(fine);
        pensieroDelPasso = null;
      },
      /** Il tool e' partito: la riga c'e' subito, con la rotellina. */
      avviaTool(dati) {
        chiudiPensiero();
        pensieroDelPasso = null;
        nota = '';
        blocco().aggiungi(passoTool({ nome: dati.nome, args: dati.args, id: dati.id }));
        stato();
        dopo();
      },
      /** Il tool ha finito: la sua riga si completa, o nasce (messaggio
       *  salvato, o tool_start perso). ``tempi``: ``{inizio, fine}`` dei
       *  messaggi salvati; dal vivo vale l'ora dell'evento. */
      concludiTool(dati, tempi = {}) {
        chiudiPensiero(tempi.inizio);
        pensieroDelPasso = null;
        const b = blocco(tempi.inizio);
        const completo = { ...dati, risultato: dati.risultato ?? '' };
        const vivi = [...b.nodo.querySelectorAll('.ps-passo.corso')];
        const suo = vivi.find((n) => (dati.id ? n.dataset.call === String(dati.id) : n._ps && n._ps.nome === dati.nome));
        if (suo) concludiTool(suo, completo);
        else b.aggiungi(passoTool(completo));
        b.segnaTool(completo);
        const f = fin(tempi.fine) ? tempi.fine : ora;
        if (fin(f)) b.fine = f;
        dopo();
      },
      /** Chiude il blocco aperto: il modello ha parlato, o il turno e' finito. */
      chiudi(fine) {
        chiudiPensiero(fine);
        if (!g) return;
        const b = g;
        g = null;
        if (ticker) { clearInterval(ticker); ticker = null; }
        const f = fin(fine) ? fine : (fin(b.fine) ? b.fine : ora);
        chiusoAlle = fin(ora) ? ora : f;
        b.chiudi(fin(b.inizio) && fin(f) && f >= b.inizio ? f - b.inizio : null);
      },
      /** Un nodo che non e' un passo sta per entrare: il pensiero del passo non
       *  riceve piu' il testo rimandato. */
      dimenticaPasso() { pensieroDelPasso = null; },
    };
  }

  radice.Passi = {
    ICONE, tronca, nomeFile, analizza, durata, estratto, frase, riga, vista, riassunto, grezzo, diff,
    icona, nodoVista, passoTool, concludiTool, passoPensiero, gruppo, lavoro,
  };
})(typeof window !== 'undefined' ? window : globalThis);
