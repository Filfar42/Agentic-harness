/* Il cruscotto della colonna di destra.
 *
 * Quattro schede -- velocita', turno, contesto, velocita' e contesto -- che si
 * nutrono degli eventi del turno (`metriche`, `step`, `tool_start`,
 * `tool_end`, `compacted`, `done`) e, a turno fermo, dello storico che il
 * server ricava dalla telemetria salvata (`stats.cruscotto`, vedi
 * core/cruscotto.py). app.js non sa niente di come si disegna: chiama
 * `window.Cruscotto.<evento>` e basta, con l'optional chaining, cosi' chi
 * carica app.js senza questo file (i test in QuickJS) non si rompe.
 *
 * Regole di questo file:
 * - nessun `innerHTML` con testo che viene da fuori: i nomi dei tool e i
 *   numeri passano da `textContent` o da attributi;
 * - un disegno per fotogramma al piu': gli eventi segnano "sporco", e
 *   `requestAnimationFrame` ridisegna una volta sola;
 * - "non si sa" e' un trattino, mai uno zero inventato.
 */
(function () {
  'use strict';

  // Le fasi di un passo, nell'ordine in cui accadono. L'ordine e' anche
  // quello dei colori (validati per daltonismo sulle coppie adiacenti, in
  // chiaro e in scuro): `attesa` e' neutra di proposito, e' il tempo in cui
  // il modello non produce niente.
  const FASI = ['attesa', 'pensiero', 'risposta', 'chiamate', 'tool'];
  const NOMI = {
    attesa: 'attesa', pensiero: 'pensiero', risposta: 'risposta',
    chiamate: 'argomenti', tool: 'tool',
  };
  const SPIEGAZIONI = {
    attesa: 'fino al primo token, prefill compreso',
    pensiero: 'token di ragionamento',
    risposta: 'testo della risposta',
    chiamate: 'argomenti delle chiamate ai tool, scritti dal modello',
    tool: 'esecuzione dei tool',
  };
  // Campioni della traccia dal vivo: a quattro al secondo sono cinque minuti.
  // Oltre, si dimezzano (media a coppie) invece di buttare i piu' vecchi: la
  // traccia deve raccontare tutto il turno, non l'ultimo pezzo.
  const TRACCIA_MAX = 1200;
  const CHIAVE_CHIUSE = 'ah-cruscotto-chiuse';

  const S = {
    vivo: false,
    turnoInizio: null,      // secondi epoch, dal server quando c'e'
    passoCorrente: 0,
    passiTotali: 0,
    righe: [],
    ultima: null,           // ultime metriche ricevute
    ricevuta: 0,            // performance.now() dell'ultima metrica
    traccia: [],
    punti: [],              // storico: [prompt, tok/s, turno relativo]
    puntiTurno: [],         // passi chiusi del turno in corso
    riassunto: null,
    contesto: { usato: 0, finestra: 0, esatto: false, cache: null, nuovi: null },
    compattato: false,
    inPartenza: false,
  };

  // ---------------------------------------------------------------------
  // Utilita'
  // ---------------------------------------------------------------------

  const $id = (id) => document.getElementById(id);
  const NS = 'http://www.w3.org/2000/svg';
  const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);

  function fmt1(v) {
    return v == null ? '—' : v.toLocaleString('it-IT', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  }
  function fmtInt(v) {
    return v == null ? '—' : Math.round(v).toLocaleString('it-IT');
  }
  function fmtK(v) {
    if (v == null) return '—';
    const a = Math.abs(v);
    if (a < 1000) return String(Math.round(v));
    if (a < 100000) return (v / 1000).toLocaleString('it-IT', { maximumFractionDigits: 1 }) + 'k';
    return Math.round(v / 1000) + 'k';
  }
  function fmtDurata(ms) {
    if (ms == null) return '—';
    const s = ms / 1000;
    if (s < 10) return s.toLocaleString('it-IT', { maximumFractionDigits: 1 }) + ' s';
    if (s < 60) return Math.round(s) + ' s';
    const m = Math.floor(s / 60);
    const r = Math.round(s - m * 60);
    return `${m}m ${String(r).padStart(2, '0')}s`;
  }
  function svg(tag, attrs) {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, String(v));
    return n;
  }
  function adesso() { return (typeof performance !== 'undefined' ? performance.now() : Date.now()); }
  function epoca() { return Date.now() / 1000; }

  // Estremo "tondo" per un asse: 1, 2, 2,5, 5 per potenze di dieci.
  function tondo(v) {
    if (!(v > 0)) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
    return 10 * p;
  }

  // ---------------------------------------------------------------------
  // Suggerimento al passaggio del puntatore: uno solo, per tutte le schede
  // ---------------------------------------------------------------------

  let tip = null;
  function mostraTip(x, y, righe) {
    if (!tip) {
      tip = document.createElement('div');
      tip.className = 'cr-tip';
      tip.setAttribute('role', 'tooltip');
      document.body.appendChild(tip);
    }
    tip.replaceChildren();
    righe.forEach(([valore, etichetta, fase], i) => {
      const r = document.createElement('div');
      r.className = i === 0 ? 'cr-tip-testa' : 'cr-tip-riga';
      if (fase) {
        const k = document.createElement('i');
        k.className = 'cr-tip-chiave';
        k.dataset.fase = fase;
        r.appendChild(k);
      }
      const v = document.createElement('b');
      v.textContent = valore;
      r.appendChild(v);
      if (etichetta) {
        const e = document.createElement('span');
        e.textContent = ' ' + etichetta;
        r.appendChild(e);
      }
      tip.appendChild(r);
    });
    tip.hidden = false;
    const w = tip.offsetWidth;
    const h = tip.offsetHeight;
    const left = Math.max(8, Math.min(window.innerWidth - w - 8, x - w - 14));
    const top = Math.max(8, Math.min(window.innerHeight - h - 8, y - h / 2));
    tip.style.left = left + 'px';
    tip.style.top = top + 'px';
  }
  function nascondiTip() { if (tip) tip.hidden = true; }

  // ---------------------------------------------------------------------
  // Stato: le righe della timeline
  // ---------------------------------------------------------------------

  function rigaVuota(passo) {
    return {
      passo, attesa_ms: null, generazione_ms: null, tool_ms: 0, durata_ms: null,
      generati: 0, pensiero: 0, risposta: 0, chiamate: 0, tok_s: null, picco: null,
      prompt: null, prompt_esatto: false, cache: null, prefill_token: null, prefill_ms: null,
      draft_n: null, draft_accettati: null, finestra: 0, fonte: 'stima', tool: {},
      compattato: false,
      // Solo dal vivo: la generazione del passo e' ancora aperta, e un tool
      // sta girando (nome e istante di partenza sull'orologio locale).
      vivo: true, generando: true, toolCorrente: null,
    };
  }

  function riga(passo) {
    let r = S.righe.find((x) => x.passo === passo);
    if (!r) {
      r = rigaVuota(passo);
      if (S.compattato) { r.compattato = true; S.compattato = false; }
      S.righe.push(r);
      S.righe.sort((a, b) => a.passo - b.passo);
    }
    return r;
  }

  function rigaCorrente() {
    return S.righe.length ? S.righe[S.righe.length - 1] : null;
  }

  // Quanto e' durata la fase di attesa, adesso: dal server se il primo token
  // e' arrivato, altrimenti l'ultima misura piu' il tempo passato da allora.
  function attesaViva(r) {
    if (r.attesa_ms != null) return r.attesa_ms;
    if (!r.vivo || !S.ultima || S.ultima.passo !== r.passo) return null;
    return (S.ultima.durata_ms || 0) + (adesso() - S.ricevuta);
  }

  function toolVivo(r) {
    let ms = r.tool_ms || 0;
    if (r.toolCorrente) ms += adesso() - r.toolCorrente.t0;
    return ms;
  }

  // I millisecondi di ogni fase di un passo. La generazione si divide fra
  // pensiero, risposta e argomenti in proporzione ai token di ciascuno: il
  // server da' il tempo totale, lo stream dice di che cosa era fatto.
  function segmenti(r) {
    const out = { attesa: attesaViva(r) || 0, pensiero: 0, risposta: 0, chiamate: 0, tool: toolVivo(r) };
    const gen = num(r.generazione_ms) || 0;
    const tot = (r.pensiero || 0) + (r.risposta || 0) + (r.chiamate || 0);
    if (gen > 0) {
      if (tot > 0) {
        out.pensiero = gen * (r.pensiero || 0) / tot;
        out.risposta = gen * (r.risposta || 0) / tot;
        out.chiamate = gen * (r.chiamate || 0) / tot;
      } else {
        // Righe ricostruite dalla telemetria di prima: il tempo c'e', la
        // ripartizione no. Si mostra come risposta, e il suggerimento lo dice.
        out.risposta = gen;
      }
    }
    return out;
  }

  function totaleRiga(seg) {
    return FASI.reduce((s, f) => s + (seg[f] || 0), 0);
  }

  // ---------------------------------------------------------------------
  // Ingressi (chiamati da app.js)
  // ---------------------------------------------------------------------

  function reset() {
    S.vivo = false;
    S.inPartenza = false;
    S.turnoInizio = null;
    S.passoCorrente = 0;
    S.passiTotali = 0;
    S.righe = [];
    S.ultima = null;
    S.traccia = [];
    S.punti = [];
    S.puntiTurno = [];
    S.riassunto = null;
    S.contesto = { usato: 0, finestra: 0, esatto: false, cache: null, nuovi: null };
    S.compattato = false;
    fermaTicker();
    sporco();
  }

  // Il messaggio e' partito: i numeri a schermo sono del turno di prima, e
  // fra l'invio e il primo evento possono passare i secondi del caricamento
  // del modello. Si dice che si sta partendo invece di lasciarli li' fermi.
  function preparaTurno() {
    S.inPartenza = epoca();
    sporco();
    // Se l'invio fallisce il turno non parte: dopo un po' si smette di dirlo.
    setTimeout(sporco, 20500);
  }
  function inPartenza() {
    return !S.vivo && S.inPartenza && epoca() - S.inPartenza < 20;
  }

  function inizioTurno() {
    // I passi del turno appena chiuso diventano storia: scalano di un turno.
    S.punti = S.punti.map(([x, y, t]) => [x, y, t - 1]);
    if (S.puntiTurno.length) S.punti = S.punti.concat(S.puntiTurno.map(([x, y]) => [x, y, -1]));
    S.puntiTurno = [];
    S.vivo = true;
    S.inPartenza = false;
    S.turnoInizio = epoca();
    S.passoCorrente = 0;
    S.righe = [];
    S.ultima = null;
    S.traccia = [];
    S.riassunto = null;
    S.compattato = false;
    avviaTicker();
    sporco();
  }

  function passo(n, totale) {
    const prima = rigaCorrente();
    if (prima && prima.passo !== n) chiudiRiga(prima);
    S.passoCorrente = n;
    S.passiTotali = totale || S.passiTotali;
    riga(n);
    sporco();
  }

  function chiudiRiga(r) {
    if (r.toolCorrente) {
      r.tool_ms = toolVivo(r);
      r.toolCorrente = null;
    }
    r.vivo = false;
    r.generando = false;
  }

  function metriche(m) {
    if (!m || typeof m.passo !== 'number') return;
    if (!S.vivo) {
      // Un frame arrivato senza `start` (riattacco a turno avanzato): il
      // turno c'e', lo si apre.
      inizioTurno();
    }
    if (num(m.turno_inizio)) S.turnoInizio = m.turno_inizio;
    S.passoCorrente = Math.max(S.passoCorrente, m.passo);
    const r = riga(m.passo);
    S.ultima = m;
    S.ricevuta = adesso();
    if (m.attesa_ms != null) r.attesa_ms = m.attesa_ms;
    for (const k of ['generazione_ms', 'cache', 'prefill_token', 'prefill_ms', 'draft_n', 'draft_accettati', 'picco']) {
      if (m[k] != null) r[k] = m[k];
    }
    r.generati = m.generati || 0;
    r.pensiero = m.pensiero || 0;
    r.risposta = m.risposta || 0;
    r.chiamate = m.chiamate || 0;
    r.tok_s = m.tok_s_passo;
    r.fonte = m.fonte || r.fonte;
    r.finestra = m.finestra || r.finestra;
    r.prompt = m.prompt != null ? m.prompt : (m.prompt_stimato || r.prompt);
    r.prompt_esatto = m.prompt != null;
    r.durata_ms = m.durata_ms;

    // Il contesto del passo: il prompt e' quello che il modello sta leggendo
    // adesso -- piu' vero della stima che il server fa a fine turno.
    if (r.prompt) {
      S.contesto = {
        usato: r.prompt,
        finestra: r.finestra || S.contesto.finestra,
        esatto: r.prompt_esatto,
        cache: r.cache,
        nuovi: r.prefill_token,
      };
    }

    const generando = ['pensiero', 'risposta', 'chiamata'].includes(m.fase);
    if (m.definitivo) {
      r.generando = false;
      if (num(r.tok_s) && num(r.prompt)) S.puntiTurno.push([r.prompt, r.tok_s]);
    } else {
      const t = S.turnoInizio ? Math.max(0, epoca() - S.turnoInizio) : 0;
      const fase = m.fase === 'chiamata' ? 'chiamate' : (generando ? m.fase : 'attesa');
      aggiungiCampione({ t, v: generando ? (num(m.tok_s) ?? num(m.tok_s_passo) ?? 0) : 0, fase });
    }
    sporco();
  }

  function aggiungiCampione(c) {
    S.traccia.push(c);
    if (S.traccia.length > TRACCIA_MAX) {
      const meta = [];
      for (let i = 0; i + 1 < S.traccia.length; i += 2) {
        const a = S.traccia[i];
        const b = S.traccia[i + 1];
        meta.push({ t: a.t, v: (a.v + b.v) / 2, fase: b.v > a.v ? b.fase : a.fase });
      }
      S.traccia = meta;
    }
  }

  function toolInizio(nome) {
    const r = rigaCorrente();
    if (!r) return;
    r.generando = false;
    r.toolCorrente = { nome: String(nome || 'tool'), t0: adesso() };
    sporco();
  }

  function toolFine(nome, durataS) {
    const r = rigaCorrente();
    if (!r) return;
    const ms = Math.max(0, (Number(durataS) || 0) * 1000);
    r.tool_ms = (r.tool_ms || 0) + ms;
    const n = String(nome || 'tool');
    r.tool[n] = (r.tool[n] || 0) + 1;
    r.toolCorrente = null;
    sporco();
  }

  function compattato() {
    S.compattato = true;
  }

  function fine(evento) {
    const usage = (evento && evento.usage) || {};
    S.righe.forEach(chiudiRiga);
    S.vivo = false;
    S.inPartenza = false;
    const gen = num(usage.completion_tokens);
    const ms = num(usage.eval_ms);
    const nudges = usage.nudges && typeof usage.nudges === 'object'
      ? Object.entries(usage.nudges).filter(([, n]) => n > 0) : [];
    S.riassunto = {
      tok_s: gen && ms ? gen / ms * 1000 : mediaRighe(),
      generati: gen,
      durata_ms: num(usage.total_ms),
      passi: num(evento && evento.steps),
      motivo: String((evento && evento.reason) || ''),
      nudges,
    };
    fermaTicker();
    sporco();
  }

  // Lo storico della conversazione, dal server. A turno fermo e' la fonte
  // della timeline; a turno vivo aggiorna solo la storia dei punti.
  function stats(stats) {
    if (!stats) return;
    const c = stats.cruscotto;
    if (c && Array.isArray(c.punti)) {
      // A turno vivo le statistiche arrivano da prima che il turno le tocchi:
      // l'ultimo turno che conoscono e' gia' quello di prima.
      S.punti = c.punti.filter((p) => Array.isArray(p) && p.length >= 2)
        .map(([x, y, t]) => [x, y, (Number(t) || 0) - (S.vivo ? 1 : 0)]);
      if (!S.vivo) S.puntiTurno = [];
    }
    if (!S.vivo) {
      const ultimo = c && c.ultimo;
      if (ultimo && Array.isArray(ultimo.righe)) {
        S.righe = ultimo.righe.filter((r) => r && typeof r === 'object').map((r) => ({
          ...rigaVuota(r.passo), ...r, tool: r.tool || {}, vivo: false, generando: false,
        }));
        S.passoCorrente = S.righe.length ? S.righe[S.righe.length - 1].passo : 0;
        if (!S.riassunto) {
          S.riassunto = {
            tok_s: mediaRighe(), generati: sommaRighe('generati'),
            durata_ms: num(ultimo.durata_ms), passi: S.righe.length,
            motivo: ultimo.motivo || '', nudges: [],
          };
        } else if (S.riassunto.durata_ms == null) {
          S.riassunto.durata_ms = num(ultimo.durata_ms);
        }
      }
      // Il contesto a riposo e' quello che partira' col prossimo messaggio:
      // la stima del server su tutta la conversazione. Cache e ricalcolati
      // vengono dall'ultimo passo misurato.
      const ultima = S.righe[S.righe.length - 1];
      S.contesto = {
        usato: Number(stats.context_used) || 0,
        finestra: (ultima && ultima.finestra) || Number(stats.context_window) || 0,
        esatto: false,
        cache: ultima ? num(ultima.cache) : null,
        nuovi: ultima ? num(ultima.prefill_token) : null,
        promptUltimo: ultima ? num(ultima.prompt) : null,
      };
    }
    sporco();
  }

  function mediaRighe() {
    let n = 0;
    let ms = 0;
    S.righe.forEach((r) => {
      if (num(r.tok_s) && num(r.generazione_ms) && r.generati) {
        n += r.generati;
        ms += r.generazione_ms;
      }
    });
    return ms ? n / ms * 1000 : null;
  }
  function sommaRighe(k) {
    return S.righe.reduce((s, r) => s + (Number(r[k]) || 0), 0);
  }

  // ---------------------------------------------------------------------
  // Disegno
  // ---------------------------------------------------------------------

  let pendente = false;
  function sporco() {
    if (pendente || typeof document === 'undefined') return;
    pendente = true;
    const fai = () => { pendente = false; disegna(); };
    if (typeof requestAnimationFrame === 'function') requestAnimationFrame(fai);
    else setTimeout(fai, 16);
  }

  // Fra un evento e l'altro il tempo passa lo stesso: l'attesa del primo
  // token, un tool che gira, la durata del turno. Il ticker ridisegna solo
  // quello, quattro volte al secondo, finche' il turno e' vivo.
  let ticker = null;
  function avviaTicker() {
    if (ticker || typeof setInterval !== 'function') return;
    ticker = setInterval(() => { if (!document.hidden) sporco(); }, 250);
  }
  function fermaTicker() {
    if (ticker) clearInterval(ticker);
    ticker = null;
  }

  function disegna() {
    if (!$id('cr-tachimetro')) return;
    disegnaTachimetro();
    disegnaTurno();
    disegnaContesto();
    disegnaDispersione();
  }

  function faseAdesso() {
    const r = rigaCorrente();
    if (inPartenza()) return ['attesa', 'invio…'];
    if (!S.vivo) return ['riposo', S.righe.length ? 'ultimo turno' : 'a riposo'];
    if (r && r.toolCorrente) {
      return ['tool', `${r.toolCorrente.nome} · ${fmtDurata(adesso() - r.toolCorrente.t0)}`];
    }
    const m = S.ultima;
    if (!m || !r || m.passo !== r.passo) return ['attesa', 'prepara il passo'];
    switch (m.fase) {
      case 'attesa': return ['attesa', `attesa · ${fmtDurata(attesaViva(r))}`];
      case 'prefill': {
        const q = m.prefill_totale ? Math.round(100 * (m.prefill_fatti || 0) / m.prefill_totale) : null;
        return ['attesa', q == null ? 'prefill' : `prefill ${q}%`];
      }
      case 'pensiero': return ['pensiero', 'pensa'];
      case 'risposta': return ['risposta', 'risponde'];
      case 'chiamata': return ['chiamate', 'scrive la chiamata'];
      default: return ['attesa', r.toolCorrente ? 'tool' : 'prepara il passo'];
    }
  }

  function disegnaTachimetro() {
    const [fase, testo] = faseAdesso();
    const pill = $id('cr-fase');
    pill.dataset.fase = fase;
    pill.classList.toggle('vivo', S.vivo);
    $id('cr-fase-testo').textContent = testo;

    const m = S.ultima;
    const r = rigaCorrente();
    const generando = S.vivo && m && ['pensiero', 'risposta', 'chiamata'].includes(m.fase)
      && !(r && r.toolCorrente);
    let valore = null;
    let sotto = '';
    if (S.vivo) {
      valore = generando ? (num(m.tok_s) ?? num(m.tok_s_passo)) : (r ? num(r.tok_s) : null);
      if (generando) {
        sotto = `media passo ${fmt1(num(m.tok_s_passo))} · picco ${fmt1(num(m.picco))}`;
      } else if (m && m.fase === 'prefill' && m.prefill_totale) {
        sotto = `prefill ${fmtInt(m.prefill_fatti)} / ${fmtInt(m.prefill_totale)} token`;
      } else if (m && m.fase === 'attesa') {
        sotto = `prompt ${m.prompt != null ? '' : '~'}${fmtK(m.prompt ?? m.prompt_stimato)} token · in attesa del primo`;
      } else {
        sotto = r && num(r.tok_s) ? `ultimo passo ${fmt1(r.tok_s)} tok/s` : 'tra un passo e l\'altro';
      }
    } else if (S.riassunto && num(S.riassunto.tok_s)) {
      valore = S.riassunto.tok_s;
      const picco = Math.max(0, ...S.righe.map((x) => num(x.picco) || 0), ...S.traccia.map((c) => c.v));
      sotto = 'media dell\'ultimo turno' + (picco ? ` · picco ${fmt1(picco)}` : '');
    } else {
      sotto = inPartenza() ? 'il messaggio è partito' : 'Nessun turno misurato in questa chat.';
    }
    const cifra = $id('cr-tok');
    // Durante il prefill la cifra grande e' il prefill: e' l'unica cosa che
    // si muove, e su un contesto lungo sono i secondi che si stanno pagando.
    const quotaPrefill = S.vivo && m && m.fase === 'prefill' && m.prefill_totale
      ? Math.min(1, (m.prefill_fatti || 0) / m.prefill_totale) : null;
    if (quotaPrefill != null) {
      cifra.textContent = Math.round(quotaPrefill * 100) + '%';
      $id('cr-unita').textContent = 'prefill';
    } else {
      cifra.textContent = valore == null ? '—' : fmt1(valore);
      $id('cr-unita').textContent = 'tok/s';
    }
    $id('cr-hero').classList.toggle('spento', !generando && quotaPrefill == null);
    $id('cr-sub').textContent = sotto;

    const barra = $id('cr-prefill-barra');
    const inPrefill = S.vivo && m && m.fase === 'prefill' && m.prefill_totale;
    barra.hidden = !inPrefill;
    if (inPrefill) {
      $id('cr-prefill-fill').style.width =
        (100 * Math.min(1, (m.prefill_fatti || 0) / m.prefill_totale)).toFixed(1) + '%';
    }

    // I tre numeri piccoli: del passo in corso, o dell'ultimo misurato.
    const rif = r && (r.attesa_ms != null || r.generati) ? r
      : [...S.righe].reverse().find((x) => x.attesa_ms != null) || r;
    const ttft = rif ? (rif.vivo ? attesaViva(rif) : num(rif.attesa_ms)) : null;
    $id('cr-ttft').textContent = fmtDurata(ttft);
    const pre = $id('cr-prefill');
    if (rif && num(rif.prefill_token) && num(rif.prefill_ms)) {
      const vel = rif.prefill_token / Math.max(1, rif.prefill_ms) * 1000;
      pre.textContent = fmtInt(vel);
      pre.title = `${fmtInt(rif.prefill_token)} token calcolati in ${fmtDurata(rif.prefill_ms)}` +
        (num(rif.cache) ? `, ${fmtInt(rif.cache)} ripresi dalla cache` : '');
    } else {
      pre.textContent = '—';
      pre.title = '';
    }
    const gen = S.vivo ? sommaRighe('generati') : (S.riassunto && S.riassunto.generati) || sommaRighe('generati');
    $id('cr-gen').textContent = gen ? fmtInt(gen) : '—';

    disegnaTraccia();
  }

  // La traccia: barrette verticali, una per campione (dal vivo) o una per
  // passo (turni riletti dal disco). L'altezza e' la velocita', il colore la
  // fase. La scala in alto e' il picco, scritto.
  function disegnaTraccia() {
    const el = $id('cr-traccia');
    const W = Math.max(120, el.clientWidth || 240);
    const H = 46;
    el.setAttribute('viewBox', `0 0 ${W} ${H}`);
    el.replaceChildren();
    const dalVivo = S.traccia.length >= 8;
    const dati = dalVivo
      ? S.traccia.map((c) => ({ v: c.v, fase: c.fase, etichetta: `${fmtDurata(c.t * 1000)} dall'inizio` }))
      : S.righe.filter((r) => num(r.tok_s)).map((r) => ({
        v: r.tok_s, fase: faseDominante(r), etichetta: `passo ${r.passo}`,
      }));
    el.appendChild(svg('line', { x1: 0, x2: W, y1: H - 0.5, y2: H - 0.5, class: 'cr-asse' }));
    if (!dati.length) return;
    const massimo = Math.max(1, ...dati.map((d) => d.v));
    const scala = tondo(massimo);
    const colonne = Math.min(dati.length, Math.floor(W / 3));
    // Troppi campioni per la larghezza: si raggruppano, tenendo il massimo
    // di ogni gruppo (un picco non deve sparire nel riassunto).
    const gruppo = Math.ceil(dati.length / colonne);
    const passoX = W / Math.ceil(dati.length / gruppo);
    const larg = Math.max(1, Math.min(dalVivo ? 3 : 14, passoX - (passoX > 4 ? 1.5 : 0.5)));
    for (let i = 0, k = 0; i < dati.length; i += gruppo, k += 1) {
      const pezzo = dati.slice(i, i + gruppo);
      const d = pezzo.reduce((a, b) => (b.v > a.v ? b : a));
      const h = d.v > 0 ? Math.max(2, (d.v / scala) * (H - 8)) : 1.5;
      const rect = svg('rect', {
        x: (k * passoX + (passoX - larg) / 2).toFixed(1), y: (H - 1 - h).toFixed(1),
        width: larg.toFixed(1), height: h.toFixed(1), rx: Math.min(1.5, larg / 2),
        class: 'cr-barretta', 'data-fase': d.fase,
      });
      rect.addEventListener('pointerenter', (ev) => mostraTip(ev.clientX, ev.clientY, [
        [`${fmt1(d.v)} tok/s`, NOMI[d.fase] || d.fase, d.fase], [d.etichetta],
      ]));
      rect.addEventListener('pointerleave', nascondiTip);
      el.appendChild(rect);
    }
    const tacca = svg('text', { x: W - 2, y: 8, class: 'cr-scala', 'text-anchor': 'end' });
    tacca.textContent = fmtInt(scala);
    el.appendChild(tacca);
    el.appendChild(svg('line', { x1: 0, x2: W - 20, y1: 7.5 - 3, y2: 7.5 - 3, class: 'cr-griglia' }));
  }

  function faseDominante(r) {
    const seg = segmenti(r);
    let migliore = 'risposta';
    let max = -1;
    for (const f of ['pensiero', 'risposta', 'chiamate']) {
      if (seg[f] > max) { max = seg[f]; migliore = f; }
    }
    return migliore;
  }

  function disegnaTurno() {
    const box = $id('cr-righe');
    const meta = $id('cr-turno-meta');
    const piede = $id('cr-turno-piede');
    const righe = S.righe;
    if (!righe.length) {
      meta.textContent = S.vivo ? 'parte…' : '—';
      $id('cr-somma').replaceChildren();
      $id('cr-legenda').replaceChildren();
      if (!box.querySelector('.empty')) {
        box.replaceChildren();
        const e = document.createElement('div');
        e.className = 'empty';
        e.textContent = 'I passi del prossimo turno compariranno qui.';
        box.appendChild(e);
      }
      piede.hidden = true;
      return;
    }
    const segs = righe.map(segmenti);
    const totali = segs.map(totaleRiga);
    const durataTurno = S.vivo && S.turnoInizio
      ? (epoca() - S.turnoInizio) * 1000
      : (S.riassunto && num(S.riassunto.durata_ms)) || totali.reduce((a, b) => a + b, 0);
    meta.textContent = S.vivo
      ? `passo ${S.passoCorrente}${S.passiTotali ? '/' + S.passiTotali : ''} · ${fmtDurata(durataTurno)}`
      : `${righe.length} ${righe.length === 1 ? 'passo' : 'passi'} · ${fmtDurata(durataTurno)}`;

    // La somma: dove e' andato il tempo di tutto il turno.
    const somma = { attesa: 0, pensiero: 0, risposta: 0, chiamate: 0, tool: 0 };
    segs.forEach((s) => FASI.forEach((f) => { somma[f] += s[f] || 0; }));
    const tot = FASI.reduce((a, f) => a + somma[f], 0) || 1;
    const barra = $id('cr-somma');
    barra.replaceChildren();
    const legenda = $id('cr-legenda');
    legenda.replaceChildren();
    FASI.forEach((f) => {
      if (somma[f] <= 0) return;
      const quota = somma[f] / tot;
      const pezzo = document.createElement('span');
      pezzo.dataset.fase = f;
      pezzo.style.flexGrow = String(quota);
      pezzo.addEventListener('pointerenter', (ev) => mostraTip(ev.clientX, ev.clientY, [
        [fmtDurata(somma[f]), `${NOMI[f]} · ${Math.round(quota * 100)}%`, f], [SPIEGAZIONI[f]],
      ]));
      pezzo.addEventListener('pointerleave', nascondiTip);
      barra.appendChild(pezzo);
      const voce = document.createElement('span');
      voce.className = 'cr-voce';
      const chiave = document.createElement('i');
      chiave.dataset.fase = f;
      const nome = document.createElement('span');
      nome.textContent = NOMI[f];
      const perc = document.createElement('b');
      // Una fase c'e' stata ma pesa meno dell'1%: "0%" direbbe che non c'e'.
      perc.textContent = quota < 0.005 ? '<1%' : Math.round(quota * 100) + '%';
      voce.title = SPIEGAZIONI[f];
      voce.append(chiave, nome, perc);
      legenda.appendChild(voce);
    });

    // Le righe: una per passo, in scala sul passo piu' lungo.
    const scala = Math.max(1000, ...totali);
    const inFondo = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
    const esistenti = new Map([...box.querySelectorAll('.cr-riga')].map((n) => [Number(n.dataset.passo), n]));
    box.querySelector('.empty')?.remove();
    righe.forEach((r, i) => {
      let nodo = esistenti.get(r.passo);
      if (!nodo) {
        nodo = document.createElement('div');
        nodo.className = 'cr-riga';
        nodo.dataset.passo = String(r.passo);
        const n = document.createElement('span');
        n.className = 'cr-n';
        const pista = document.createElement('span');
        pista.className = 'cr-pista';
        const striscia = document.createElement('span');
        striscia.className = 'cr-striscia';
        FASI.forEach((f) => {
          const s = document.createElement('span');
          s.dataset.fase = f;
          striscia.appendChild(s);
        });
        pista.appendChild(striscia);
        const v = document.createElement('span');
        v.className = 'cr-dur';
        nodo.append(n, pista, v);
        nodo.addEventListener('pointerenter', (ev) => mostraTip(ev.clientX, ev.clientY, tipRiga(r.passo)));
        nodo.addEventListener('pointermove', (ev) => mostraTip(ev.clientX, ev.clientY, tipRiga(r.passo)));
        nodo.addEventListener('pointerleave', nascondiTip);
        box.appendChild(nodo);
      }
      esistenti.delete(r.passo);
      const vivo = S.vivo && r.vivo && i === righe.length - 1;
      nodo.classList.toggle('vivo', vivo);
      nodo.classList.toggle('compattato', !!r.compattato);
      const n = nodo.querySelector('.cr-n');
      n.textContent = String(r.passo);
      n.title = r.compattato ? 'Prima di questo passo la cronologia è stata compattata' : '';
      const striscia = nodo.querySelector('.cr-striscia');
      striscia.style.width = (100 * totali[i] / scala).toFixed(2) + '%';
      [...striscia.children].forEach((s) => {
        const ms = segs[i][s.dataset.fase] || 0;
        s.style.flexGrow = String(ms);
        s.hidden = ms <= 0;
      });
      nodo.querySelector('.cr-dur').textContent = fmtDurata(totali[i]);
    });
    esistenti.forEach((n) => n.remove());
    // Si segue il passo nuovo solo se si stava gia' guardando il fondo: chi
    // e' risalito a leggere un passo vecchio non deve essere riportato giu'
    // quattro volte al secondo.
    if (S.vivo && inFondo) box.scrollTop = box.scrollHeight;

    // Il piede: il riassunto del turno chiuso, e le correzioni se ce ne sono.
    if (!S.vivo && S.riassunto) {
      const pezzi = [];
      if (num(S.riassunto.tok_s)) pezzi.push(`${fmt1(S.riassunto.tok_s)} tok/s medi`);
      const g = S.riassunto.generati || sommaRighe('generati');
      if (g) pezzi.push(`${fmtInt(g)} token`);
      piede.replaceChildren();
      const t = document.createElement('span');
      t.textContent = pezzi.join(' · ');
      piede.appendChild(t);
      const nudges = S.riassunto.nudges || [];
      if (nudges.length) {
        const c = document.createElement('span');
        c.className = 'cr-correzioni';
        const quante = nudges.reduce((s, [, n]) => s + n, 0);
        c.textContent = `${quante} ${quante === 1 ? 'correzione' : 'correzioni'}`;
        c.title = nudges.map(([k, n]) => `${n}× ${ETICHETTE_NUDGE[k] || k}`).join('\n');
        piede.appendChild(c);
      }
      piede.hidden = !pezzi.length && !nudges.length;
    } else {
      piede.hidden = true;
    }
  }

  // I nomi interni dei solleciti non dicono niente a chi guarda.
  const ETICHETTE_NUDGE = {
    tool: 'ha risposto a parole invece di agire',
    ask: 'ha chiesto a parole invece di usare il tool',
    verify: 'ha lasciato una verifica rossa',
    loop: 'ha ripetuto lo stesso comando fallito',
    coverage: 'ha verificato codice diverso da quello scritto',
    summary: 'ha chiuso il turno senza scrivere',
    summary_failed: 'ha chiuso in rosso senza dirlo',
    json_leak: 'ha stampato la tool call come testo',
  };

  function tipRiga(passo) {
    const r = S.righe.find((x) => x.passo === passo);
    if (!r) return [['—']];
    const seg = segmenti(r);
    const out = [[fmtDurata(totaleRiga(seg)), `passo ${r.passo}` + (r.compattato ? ' · dopo una compattazione' : '')]];
    FASI.forEach((f) => {
      if (seg[f] > 0) out.push([fmtDurata(seg[f]), NOMI[f], f]);
    });
    if (num(r.tok_s)) out.push([`${fmt1(r.tok_s)} tok/s`, `${fmtInt(r.generati)} token generati`]);
    if (num(r.prompt)) out.push([`${r.prompt_esatto ? '' : '~'}${fmtK(r.prompt)}`, 'token di prompt']);
    const nomi = Object.entries(r.tool || {});
    if (nomi.length) out.push([nomi.map(([n, k]) => (k > 1 ? `${n} ×${k}` : n)).join(', ')]);
    if (r.fonte === 'telemetria') out.push(['', 'pensiero e risposta non separati (turno registrato prima del cruscotto)']);
    return out;
  }

  function disegnaContesto() {
    const c = S.contesto;
    const meta = $id('ctx-used');
    const meter = $id('ctx-meter');
    const finestra = c.finestra || 0;
    if (!finestra || !c.usato) {
      meta.textContent = finestra ? `— / ${fmtK(finestra)}` : '—';
      $id('cr-ctx-cache').style.width = '0%';
      $id('ctx-bar').style.width = '0%';
      $id('ctx-pct').textContent = '—';
      $id('cr-ctx-dettaglio').textContent = '';
    } else {
      const quota = Math.min(1, c.usato / finestra);
      meta.textContent = `${c.esatto ? '' : '~'}${fmtK(c.usato)} / ${fmtK(finestra)}`;
      meta.title = `${fmtInt(c.usato)} token su ${fmtInt(finestra)}` + (c.esatto ? '' : ' (stima)');
      meter.classList.toggle('warn', quota > 0.7 && quota <= 0.9);
      meter.classList.toggle('err', quota > 0.9);
      // La parte dalla cache e' piena, quella ricalcolata tratteggiata: la
      // differenza si legge anche senza distinguere i colori.
      // Senza il dato della cache la barra e' piena e basta: tratteggiarla
      // direbbe "tutto ricalcolato", che non si sa.
      const cache = num(c.cache);
      const base = c.esatto ? c.usato : (num(c.promptUltimo) || c.usato);
      const quotaCache = cache != null && base ? Math.min(quota, quota * cache / base) : quota;
      $id('cr-ctx-cache').style.width = (quotaCache * 100).toFixed(2) + '%';
      $id('ctx-bar').style.width = ((quota - quotaCache) * 100).toFixed(2) + '%';
      $id('ctx-bar').classList.toggle('solo', quotaCache <= 0);
      $id('ctx-pct').textContent = `${Math.round(quota * 100)}% della finestra`;
      const det = $id('cr-ctx-dettaglio');
      if (cache != null && base) {
        det.textContent = `cache ${Math.round(100 * Math.min(1, cache / base))}%`;
        det.title = `${fmtInt(cache)} token del prompt ripresi dalla cache del server` +
          (num(c.nuovi) ? `, ${fmtInt(c.nuovi)} ricalcolati (la parte tratteggiata)` : '');
      } else {
        det.textContent = c.esatto ? '' : 'stima';
        det.title = c.esatto ? '' : 'Il server non dichiara il prompt intero: è la stima dell\'harness.';
      }
    }

    // Draft MTP: la quota di token accettati, del passo in corso dal vivo e
    // del turno a riposo; sotto, una barretta per passo.
    const conDraft = S.righe.filter((r) => num(r.draft_n) && r.draft_n > 0);
    const box = $id('cr-draft');
    box.hidden = !conDraft.length;
    if (!conDraft.length) return;
    const r = rigaCorrente();
    const suPasso = S.vivo && r && num(r.draft_n) && r.draft_n > 0;
    const n = suPasso ? r.draft_n : conDraft.reduce((s, x) => s + x.draft_n, 0);
    const ok = suPasso ? (r.draft_accettati || 0) : conDraft.reduce((s, x) => s + (x.draft_accettati || 0), 0);
    const quota = n ? ok / n : 0;
    $id('cr-draft-v').textContent = `${Math.round(quota * 100)}% · ${fmtInt(ok)}/${fmtInt(n)}`;
    $id('cr-draft-v').title = `${suPasso ? 'Passo in corso' : 'Turno'}: ${fmtInt(ok)} token accettati su ` +
      `${fmtInt(n)} proposti dal draft.\nQuota bassa = il draft costa calcolo senza far guadagnare tempo.`;
    $id('cr-draft-fill').style.width = (quota * 100).toFixed(1) + '%';
    const passi = $id('cr-draft-passi');
    passi.replaceChildren();
    conDraft.slice(-32).forEach((x) => {
      const q = (x.draft_accettati || 0) / x.draft_n;
      const b = document.createElement('span');
      b.style.height = Math.max(8, q * 100).toFixed(0) + '%';
      b.addEventListener('pointerenter', (ev) => mostraTip(ev.clientX, ev.clientY, [
        [`${Math.round(q * 100)}%`, `accettati al passo ${x.passo}`],
        [`${fmtInt(x.draft_accettati || 0)} su ${fmtInt(x.draft_n)}`],
      ]));
      b.addEventListener('pointerleave', nascondiTip);
      passi.appendChild(b);
    });
  }

  function disegnaDispersione() {
    const el = $id('cr-grafico');
    const W = Math.max(160, el.clientWidth || 260);
    const H = 150;
    const M = { l: 26, r: 8, t: 8, b: 18 };
    el.setAttribute('viewBox', `0 0 ${W} ${H}`);
    el.replaceChildren();
    const r = rigaCorrente();
    const vivo = S.vivo && r && r.generando && num(r.prompt) && S.ultima
      ? [r.prompt, num(S.ultima.tok_s_passo) ?? num(S.ultima.tok_s)] : null;
    const tutti = [
      ...S.punti.map(([x, y, t]) => ({ x, y, t, tipo: t >= 0 && !S.vivo ? 'ultimo' : 'vecchio' })),
      ...S.puntiTurno.map(([x, y]) => ({ x, y, t: 0, tipo: 'ultimo' })),
    ].filter((p) => num(p.x) && num(p.y));
    if (vivo && num(vivo[1])) tutti.push({ x: vivo[0], y: vivo[1], t: 0, tipo: 'vivo' });
    const meta = $id('cr-tendenza');
    const didascalia = $id('cr-didascalia');
    didascalia.replaceChildren();
    if (tutti.length < 2) {
      const t = svg('text', { x: W / 2, y: H / 2, class: 'cr-vuoto', 'text-anchor': 'middle' });
      t.textContent = 'Servono almeno due passi misurati.';
      el.appendChild(t);
      meta.textContent = '—';
      return;
    }
    const xMax = tondo(Math.max(...tutti.map((p) => p.x)) * 1.05);
    const yMax = tondo(Math.max(...tutti.map((p) => p.y)) * 1.08);
    const X = (v) => M.l + (v / xMax) * (W - M.l - M.r);
    const Y = (v) => H - M.b - (v / yMax) * (H - M.t - M.b);

    // Griglia: tre righe orizzontali e le tacche, sottili.
    [0, 0.5, 1].forEach((q) => {
      const y = Y(q * yMax);
      el.appendChild(svg('line', { x1: M.l, x2: W - M.r, y1: y, y2: y, class: q ? 'cr-griglia' : 'cr-asse' }));
      const t = svg('text', { x: M.l - 4, y: y + 3, class: 'cr-scala', 'text-anchor': 'end' });
      t.textContent = fmtInt(q * yMax);
      el.appendChild(t);
    });
    [0, 0.5, 1].forEach((q) => {
      const t = svg('text', {
        x: X(q * xMax), y: H - 4, class: 'cr-scala',
        'text-anchor': q === 0 ? 'start' : q === 1 ? 'end' : 'middle',
      });
      t.textContent = q === 1 ? `${fmtK(q * xMax)} tok` : fmtK(q * xMax);
      el.appendChild(t);
    });

    // La tendenza: minimi quadrati su tutti i punti chiusi, se sono abbastanza
    // e abbastanza sparsi da voler dire qualcosa.
    const chiusi = tutti.filter((p) => p.tipo !== 'vivo');
    const xs = chiusi.map((p) => p.x);
    const spread = Math.max(...xs) - Math.min(...xs);
    if (chiusi.length >= 6 && spread >= 2000) {
      const n = chiusi.length;
      const mx = xs.reduce((a, b) => a + b, 0) / n;
      const my = chiusi.reduce((a, p) => a + p.y, 0) / n;
      let sxy = 0;
      let sxx = 0;
      chiusi.forEach((p) => { sxy += (p.x - mx) * (p.y - my); sxx += (p.x - mx) ** 2; });
      const b = sxx ? sxy / sxx : 0;
      const a = my - b * mx;
      const x0 = Math.min(...xs);
      const x1 = Math.max(...xs);
      el.appendChild(svg('line', {
        x1: X(x0), x2: X(x1), y1: Y(Math.max(0, a + b * x0)), y2: Y(Math.max(0, a + b * x1)),
        class: 'cr-tendenza',
      }));
      const per10k = b * 10000;
      const segno = per10k > 0 ? '+' : per10k < 0 ? '−' : '±';
      const forte = document.createElement('b');
      forte.textContent = `${segno}${fmt1(Math.abs(per10k))} tok/s`;
      didascalia.append('Tendenza: ', forte, ' ogni 10k token di prompt in più');
    } else {
      didascalia.textContent = 'La tendenza compare con almeno sei passi su prompt di lunghezza diversa.';
    }
    meta.textContent = `${chiusi.length} ${chiusi.length === 1 ? 'passo' : 'passi'}`;

    // I punti: prima i vecchi, poi l'ultimo turno, poi quello vivo -- chi e'
    // disegnato dopo sta sopra.
    const ordine = { vecchio: 0, ultimo: 1, vivo: 2 };
    tutti.sort((a, b) => ordine[a.tipo] - ordine[b.tipo]);
    tutti.forEach((p) => {
      const c = svg('circle', {
        cx: X(p.x).toFixed(1), cy: Y(p.y).toFixed(1), r: p.tipo === 'vivo' ? 5 : p.tipo === 'ultimo' ? 3.5 : 3,
        class: 'cr-punto cr-punto-' + p.tipo,
      });
      el.appendChild(c);
    });
    // Un solo bersaglio per tutto il grafico: vince il punto piu' vicino al
    // puntatore, entro 24 px. Un pallino da sei pixel non si centra.
    const bersaglio = svg('rect', { x: 0, y: 0, width: W, height: H, class: 'cr-bersaglio' });
    bersaglio.addEventListener('pointermove', (ev) => {
      const box = el.getBoundingClientRect();
      const sx = (ev.clientX - box.left) * (W / box.width);
      const sy = (ev.clientY - box.top) * (H / box.height);
      let vicino = null;
      let d2 = 24 * 24;
      tutti.forEach((p) => {
        const d = (X(p.x) - sx) ** 2 + (Y(p.y) - sy) ** 2;
        if (d < d2) { d2 = d; vicino = p; }
      });
      if (!vicino) { nascondiTip(); return; }
      const quando = vicino.tipo === 'vivo' ? 'passo in corso'
        : vicino.tipo === 'ultimo' ? (S.vivo ? 'questo turno' : 'ultimo turno')
          : `${Math.abs(vicino.t)} ${Math.abs(vicino.t) === 1 ? 'turno' : 'turni'} fa`;
      mostraTip(ev.clientX, ev.clientY, [
        [`${fmt1(vicino.y)} tok/s`, quando], [`${fmtInt(vicino.x)} token di prompt`],
      ]);
    });
    bersaglio.addEventListener('pointerleave', nascondiTip);
    el.appendChild(bersaglio);
  }

  // ---------------------------------------------------------------------
  // Schede richiudibili
  // ---------------------------------------------------------------------

  function chiuse() {
    try { return new Set(JSON.parse(localStorage.getItem(CHIAVE_CHIUSE) || '[]')); } catch { return new Set(); }
  }
  function salvaChiuse(insieme) {
    try { localStorage.setItem(CHIAVE_CHIUSE, JSON.stringify([...insieme])); } catch { /* niente */ }
  }

  function monta() {
    const giaChiuse = chiuse();
    document.querySelectorAll('[data-cr-chiudi]').forEach((testa) => {
      const card = testa.closest('.cr-card');
      const nome = testa.dataset.crChiudi;
      const applica = (chiusa) => {
        card.classList.toggle('cr-chiusa', chiusa);
        testa.setAttribute('aria-expanded', String(!chiusa));
      };
      applica(giaChiuse.has(nome));
      testa.addEventListener('click', () => {
        const insieme = chiuse();
        const ora = !card.classList.contains('cr-chiusa');
        if (ora) insieme.add(nome); else insieme.delete(nome);
        salvaChiuse(insieme);
        applica(ora);
        sporco();
      });
    });
    // La larghezza della colonna si trascina: i grafici si ridisegnano sulla
    // larghezza nuova invece di stirarsi.
    if (typeof ResizeObserver === 'function' && $id('panel')) {
      new ResizeObserver(() => sporco()).observe($id('panel'));
    }
    document.addEventListener('visibilitychange', () => { if (!document.hidden) sporco(); });
    sporco();
  }

  if (typeof window !== 'undefined') {
    window.Cruscotto = {
      reset, preparaTurno, inizioTurno, passo, metriche, toolInizio, toolFine,
      compattato, fine, stats,
      // Per i test e per chi vuole guardarci dentro dalla console.
      _stato: S,
    };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', monta);
    else monta();
  }
}());
