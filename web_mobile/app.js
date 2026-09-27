/* Client mobile: lista chat, conversazione, invio messaggi e risposte.
 *
 * Tutto passa dal proxy (/api/* -> server principale): lo stato ce n'e' uno
 * solo, quindi desktop e telefono sono sempre allineati. Il flusso in tempo
 * reale arriva via EventSource su /api/stream/{id}, gli eventi sono gli
 * stessi che la UI desktop riceve (content, question, done, state...).
 */
"use strict";

const $ = (id) => document.getElementById(id);

let currentId = null;      // sessione aperta
let ultimaFirma = null;    // firma dell'ultimo pannello disegnato (messages+pending)
let source = null;         // EventSource attivo
let pollTimer = null;      // fallback di sincronizzazione
let running = false;

// I passi del turno -- pensieri e tool -- stanno in blocchi di lavoro, gli
// stessi del desktop (web/passi.js, servito da /comune/passi.js): vivo, un
// blocco dice "Sta lavorando · passo 6 · 31 s" e mostra gli ultimi passi col
// pensiero in una finestra di quattro righe; a lavoro finito e' una riga sola
// che si apre sulla traccia, e ogni riga si apre sul suo dettaglio. Un testo
// del modello chiude il blocco: la risposta resta nella sua bolla.
let lavoro = nuovoLavoro(false);
// Il passo in corso, dall'evento ``step``: serve a riconoscere la bolla di un
// passo gia' resa quando l'arretrato di un riaggancio la ripete.
let passoCorrente = 0;
// Il testo della risposta in corso, accumulato: gli eventi ``content``
// portano solo il pezzo nuovo, e un pezzo da solo non si puo' ripulire dal
// ``<think>`` ne' mostrare.
let testoInCorso = "";
let inizioTurno = 0;
let tickAttivita = null;

// ------------------------------------------- velocita', contesto, piano ----
//
// Le stesse misure del cruscotto del desktop, ridotte a tre segni: la goccia
// con i token al secondo, la riga del contesto sotto la barra, la rotaia del
// piano sul bordo destro. Dal vivo vengono dall'evento ``metriche`` e ``plan``
// dello stream; a riposo dalla rilettura della conversazione.

let misure = { tokS: null, viva: false, usato: 0, finestra: 0, esatto: false,
  cache: null, draftN: null, draftOk: null };
let pianoCorrente = [];

function fmtUno(v) {
  return v == null ? "—" : v.toLocaleString("it-IT", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
}

function fmtMigliaia(v) {
  if (v == null) return "—";
  return v < 1000 ? String(Math.round(v)) : (v / 1000).toLocaleString("it-IT", { maximumFractionDigits: 1 }) + "k";
}

/** La goccia: il numero, e se il modello sta generando adesso. */
function disegnaGoccia() {
  const goccia = $("goccia-tok");
  if (!goccia) return;
  goccia.classList.toggle("hidden", misure.tokS == null);
  goccia.classList.toggle("viva", misure.viva);
  $("goccia-tok-v").textContent = fmtUno(misure.tokS);
  goccia.title = misure.viva ? "Velocità adesso" : "Media dell'ultimo turno";
}

/** La riga del contesto: quanto della finestra occupa il prompt. */
function disegnaContesto() {
  const barra = $("barra-contesto");
  if (!barra) return;
  const quota = misure.finestra ? Math.min(1, misure.usato / misure.finestra) : 0;
  $("barra-contesto-fill").style.width = (quota * 100).toFixed(1) + "%";
  barra.classList.toggle("warn", quota > 0.7 && quota <= 0.9);
  barra.classList.toggle("err", quota > 0.9);
  barra.setAttribute("aria-label", misure.finestra
    ? `Contesto: ${Math.round(quota * 100)}% della finestra` : "Contesto");
}

/** Un evento ``metriche`` dallo stream: aggiorna goccia e contesto. */
function applicaMetriche(m) {
  const generando = ["pensiero", "risposta", "chiamata"].includes(m.fase) && !m.definitivo;
  const v = generando ? (m.tok_s ?? m.tok_s_passo) : m.tok_s_passo;
  if (v != null) misure.tokS = v;
  misure.viva = generando;
  const prompt = m.prompt ?? m.prompt_stimato;
  if (prompt) {
    misure.usato = prompt;
    misure.esatto = m.prompt != null;
    misure.finestra = m.finestra || misure.finestra;
    misure.cache = m.cache ?? misure.cache;
  }
  if (m.draft_n) { misure.draftN = m.draft_n; misure.draftOk = m.draft_accettati || 0; }
  disegnaGoccia();
  disegnaContesto();
}

/** A riposo: la media dell'ultimo turno e la stima del contesto dal server. */
function applicaStats(stats) {
  if (!stats) return;
  misure.viva = false;
  misure.usato = Number(stats.context_used) || 0;
  misure.finestra = Number(stats.context_window) || misure.finestra;
  misure.esatto = false;
  const turni = (stats.cruscotto && stats.cruscotto.turni) || [];
  const ultimo = turni[turni.length - 1];
  if (ultimo && ultimo.tok_s != null) misure.tokS = ultimo.tok_s;
  const righe = (stats.cruscotto && stats.cruscotto.ultimo && stats.cruscotto.ultimo.righe) || [];
  const riga = righe[righe.length - 1];
  if (riga) {
    misure.cache = riga.cache;
    const conDraft = righe.filter((r) => r.draft_n);
    misure.draftN = conDraft.length ? conDraft.reduce((s, r) => s + r.draft_n, 0) : null;
    misure.draftOk = conDraft.reduce((s, r) => s + (r.draft_accettati || 0), 0);
  }
  disegnaGoccia();
  disegnaContesto();
}

/** Toccando la goccia: i numeri che non stanno in una goccia. */
function dettaglioMisure() {
  const pezzi = [`${fmtUno(misure.tokS)} tok/s ${misure.viva ? "adesso" : "(ultimo turno)"}`];
  if (misure.finestra) {
    pezzi.push(`contesto ${misure.esatto ? "" : "~"}${fmtMigliaia(misure.usato)}/${fmtMigliaia(misure.finestra)}`);
  }
  if (misure.cache != null && misure.usato) {
    pezzi.push(`cache ${Math.round(100 * Math.min(1, misure.cache / misure.usato))}%`);
  }
  if (misure.draftN) pezzi.push(`MTP ${Math.round(100 * misure.draftOk / misure.draftN)}%`);
  return pezzi.join(" · ");
}

const SEGNO_PUNTO = { done: "✓", doing: "●", skipped: "⤼", todo: "○" };

/** La rotaia del piano: un pallino per punto, la linea piena fino
 *  all'ultimo punto chiuso. Senza piano non c'e'. */
function disegnaRotaia(steps) {
  pianoCorrente = Array.isArray(steps) ? steps : [];
  const rotaia = $("rotaia-piano");
  if (!rotaia) return;
  const ce = pianoCorrente.length > 0;
  rotaia.classList.toggle("hidden", !ce);
  $("messages-wrap").classList.toggle("con-rotaia", ce);
  const punti = $("rotaia-punti");
  punti.innerHTML = "";
  if (!ce) { chiudiFoglioPiano(); return; }
  let ultimoChiuso = -1;
  pianoCorrente.forEach((step, i) => {
    const pallino = document.createElement("span");
    pallino.className = step.status || "todo";
    pallino.title = `${step.id}. ${step.text || ""}`;
    punti.appendChild(pallino);
    if (step.status === "done" || step.status === "skipped") ultimoChiuso = i;
  });
  const n = pianoCorrente.length;
  const quota = n > 1 ? Math.max(0, ultimoChiuso) / (n - 1) : (ultimoChiuso >= 0 ? 1 : 0);
  $("rotaia-fatto").style.height = (ultimoChiuso < 0 ? 0 : quota * 100).toFixed(1) + "%";
  const fatti = pianoCorrente.filter((s) => s.status === "done").length;
  rotaia.setAttribute("aria-label", `Piano: ${fatti} punti fatti su ${n}`);
  if (!$("piano-foglio").classList.contains("hidden")) riempiFoglioPiano();
}

function riempiFoglioPiano() {
  const elenco = $("piano-elenco");
  elenco.innerHTML = "";
  pianoCorrente.forEach((step) => {
    const li = document.createElement("li");
    li.className = step.status || "todo";
    const segno = document.createElement("span");
    segno.textContent = SEGNO_PUNTO[step.status] || SEGNO_PUNTO.todo;
    const testo = document.createElement("span");
    testo.textContent = step.text || "";
    li.append(segno, testo);
    elenco.appendChild(li);
  });
  const fatti = pianoCorrente.filter((s) => s.status === "done").length;
  $("piano-conto").textContent = `${fatti}/${pianoCorrente.length}`;
}

function apriFoglioPiano() {
  riempiFoglioPiano();
  $("piano-foglio").classList.remove("hidden");
}

function chiudiFoglioPiano() {
  const foglio = $("piano-foglio");
  if (foglio) foglio.classList.add("hidden");
}

// ---------------------------------------------------------------- toast ----

function toast(text) {
  const el = $("toast");
  el.textContent = text;
  el.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 3200);
}

async function api(path, options) {
  const res = await fetch(path, options);
  if (res.status === 401) {
    // La pagina di ingresso spiega come associare di nuovo il telefono e
    // rimuove dalla vista le chat dopo una revoca dal desktop.
    window.location.replace('/');
    throw new Error('Associazione revocata o scaduta.');
  }
  if (!res.ok) {
    let detail = `${res.status}`;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.json();
}

function jsonPost(path, payload) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

// ------------------------------------------------- che sta facendo adesso --
//
// Due livelli:
//   1. la striscia sopra il composer: una riga, sempre visibile, animata --
//      e' quella che risponde a "sta ancora lavorando o si e' piantato?";
//   2. i blocchi di lavoro nella conversazione (web/passi.js): righe piccole
//      e chiare, una per passo. Il contenuto -- il risultato di un tool, il
//      pensiero intero -- entra in pagina solo al tocco sulla sua riga: sullo
//      schermo del telefono sono migliaia di caratteri, e dal vivo del
//      pensiero si vedono solo le ultime quattro righe.

/** L'ultima cartella di un percorso, per la sottoriga della topbar mobile:
 *  "/work/progetto" -> "progetto". Il percorso intero sta nel title. */
function nomeCartella(percorso) {
  const pezzi = String(percorso ?? "").split(/[\\/]/).filter(Boolean);
  return pezzi[pezzi.length - 1] || "";
}

/** "legge tools.py": la frase della striscia di attivita'. La scrive
 *  web/passi.js, la stessa che da' il verbo alle righe dei passi. */
function descriviTool(nome, args) {
  return Passi.frase(nome, args);
}

/** La regia dei blocchi per la conversazione aperta. ``vivo``: il turno sta
 *  girando adesso (conta i secondi). I nodi di un turno vivo portano
 *  ``data-turno-vivo``: al riaggancio l'arretrato li ridisegna da capo, e i
 *  vecchi se ne vanno tutti (``pulisciTurnoVivo``). */
function nuovoLavoro(vivo) {
  return Passi.lavoro({
    inserisci(nodo) {
      if (vivo) nodo.dataset.turnoVivo = "1";
      $("messages").appendChild(nodo);
      scrollBottom();
    },
    vivo: () => vivo && running,
    dopo: scrollBottom,
  });
}

function durataBreve(secondi) {
  return secondi < 60 ? `${secondi}s` : `${Math.floor(secondi / 60)}m ${secondi % 60}s`;
}

function mostraAttivita(testo) {
  const strip = $("attivita");
  if (!strip) return;
  strip.classList.remove("hidden");
  if (testo) $("attivita-testo").textContent = testo;
  if (!tickAttivita) {
    aggiornaTempoAttivita();
    tickAttivita = setInterval(aggiornaTempoAttivita, 1000);
  }
}

function aggiornaTempoAttivita() {
  const campo = $("attivita-tempo");
  if (!campo) return;
  const secondi = inizioTurno ? Math.max(0, Math.round((Date.now() - inizioTurno) / 1000)) : 0;
  campo.textContent = durataBreve(secondi);
}

function nascondiAttivita() {
  if (tickAttivita) { clearInterval(tickAttivita); tickAttivita = null; }
  const strip = $("attivita");
  if (strip) strip.classList.add("hidden");
}

// ------------------------------------------------------------ elenco chat --

async function loadSessions() {
  try {
    // tutte=1: tutte le chat, con il progetto di ciascuna. Il raggruppamento
    // lo fa il telefono: la colonna del desktop non c'e', e un elenco solo
    // e' l'unico modo di arrivare dappertutto.
    const [data, elenco] = await Promise.all([
      api("/api/sessions?tutte=1"),
      api("/api/progetti").catch(() => ({ progetti: [] })),
    ]);
    progettiNoti = elenco.progetti || [];
    renderSessions(data.sessions || []);
    setBanner(null);
  } catch (err) {
    setBanner(`Server principale non raggiungibile: ${err.message}`);
  }
}

// ---------------------------------------------------------------- progetti --
//
// Un progetto e' una cartella con le sue chat e una memoria che passa
// dall'una all'altra (core/progetto.py). Sul telefono si vede, si apre e si
// legge; la memoria si corregge dal desktop.

let progettiNoti = [];
const progettiAperti = new Set();
const TIPI_MEMORIA = [
  ["decisione", "Decisioni"], ["convenzione", "Convenzioni"], ["fatto", "Fatti"],
  ["scartato", "Strade scartate"], ["aperto", "Lavori aperti"],
];

function iniziale(nome) {
  const s = String(nome || "").trim();
  return s ? [...s][0].toUpperCase() : "·";
}

function rigaChat(s) {
  const li = document.createElement("li");
  li.dataset.id = s.id;
  li.tabIndex = 0;
  li.setAttribute('role', 'button');

  const title = document.createElement("div");
  title.className = "session-title";
  title.textContent = s.title || "(senza titolo)";
  if (s.running) {
    const badge = document.createElement("span");
    badge.className = "badge-running";
    badge.textContent = "in corso";
    title.appendChild(badge);
  }

  const meta = document.createElement("div");
  meta.className = "session-meta";
  meta.textContent = fmtDate(s.updated_at);

  li.append(title, meta);
  li.addEventListener("click", () => openChat(s.id));
  li.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      openChat(s.id);
    }
  });
  return li;
}

function renderProgettiMobile(sessions) {
  const box = $("progetti-mobile");
  if (!box) return;
  box.innerHTML = "";
  if (!progettiNoti.length) return;
  const titolo = document.createElement("div");
  titolo.className = "gruppo-titolo";
  titolo.textContent = "Progetti";
  box.appendChild(titolo);
  for (const p of progettiNoti) {
    const sue = sessions.filter((s) => s.progetto && s.progetto.path === p.path);
    // Aperto di serie il progetto con una chat al lavoro: e' quello che si
    // cerca guardando il telefono.
    if (sue.some((s) => s.running)) progettiAperti.add(p.path);
    const aperto = progettiAperti.has(p.path);
    const sezione = document.createElement("section");
    sezione.className = "progetto-m" + (aperto ? " aperto" : "") + (p.esiste === false ? " mancante" : "");

    const testa = document.createElement("div");
    testa.className = "progetto-m-testa";
    const apri = document.createElement("button");
    apri.type = "button";
    apri.className = "progetto-m-apri";
    apri.setAttribute("aria-expanded", aperto ? "true" : "false");
    const lettera = document.createElement("span");
    lettera.className = "progetto-m-lettera";
    lettera.textContent = iniziale(p.nome);
    const nome = document.createElement("span");
    nome.className = "progetto-m-nome";
    nome.textContent = p.nome;
    const conto = document.createElement("span");
    conto.className = "progetto-m-conto";
    conto.textContent = String(sue.length);
    apri.append(lettera, nome, conto);
    apri.addEventListener("click", () => {
      if (progettiAperti.has(p.path)) progettiAperti.delete(p.path);
      else progettiAperti.add(p.path);
      renderProgettiMobile(sessions);
    });
    const mem = document.createElement("button");
    mem.type = "button";
    mem.className = "progetto-m-mem";
    mem.textContent = `Memoria ${(p.memoria || []).length}`;
    mem.addEventListener("click", () => apriMemoria(p));
    testa.append(apri, mem);
    sezione.appendChild(testa);

    if (aperto) {
      const lista = document.createElement("ul");
      lista.className = "session-list annidata";
      sue.forEach((s) => lista.appendChild(rigaChat(s)));
      if (!sue.length) {
        const vuoto = document.createElement("li");
        vuoto.className = "vuoto";
        vuoto.textContent = "Nessuna conversazione.";
        lista.appendChild(vuoto);
      }
      sezione.appendChild(lista);
      if (p.esiste !== false) {
        const nuova = document.createElement("button");
        nuova.type = "button";
        nuova.className = "progetto-m-nuova";
        nuova.textContent = "+ Nuova chat nel progetto";
        nuova.addEventListener("click", () => nuovaChatNelProgetto(p));
        sezione.appendChild(nuova);
      }
    }
    box.appendChild(sezione);
  }
}

async function nuovaChatNelProgetto(p) {
  try {
    // Aprire il progetto sposta la cartella di lavoro: e' lo stesso gesto del
    // desktop, e la chat nuova nasce li'.
    await jsonPost("/api/progetti/open", { path: p.path });
    const payload = await jsonPost("/api/sessions", {});
    await loadSessions();
    openChat(payload.session_id);
  } catch (err) { toast(err.message); }
}

function apriMemoria(p) {
  const memoria = p.memoria || [];
  $("memoria-titolo").textContent = `Memoria · ${p.nome}`;
  $("memoria-conto").textContent = `${memoria.length}`;
  const box = $("memoria-elenco");
  box.innerHTML = "";
  if (!memoria.length) {
    const vuoto = document.createElement("p");
    vuoto.className = "memoria-vuota";
    vuoto.textContent = "Ancora vuota: la scrive l'harness alla fine dei turni in cui si lavora. "
      + "Si corregge dal desktop, nella schermata del progetto.";
    box.appendChild(vuoto);
  }
  for (const [tipo, etichetta] of TIPI_MEMORIA) {
    const voci = memoria.filter((v) => v.tipo === tipo);
    if (!voci.length) continue;
    const titolo = document.createElement("div");
    titolo.className = `memoria-gruppo tipo-${tipo}`;
    titolo.textContent = etichetta;
    box.appendChild(titolo);
    for (const v of voci) {
      const riga = document.createElement("div");
      riga.className = `memoria-voce tipo-${tipo}`;
      const testo = document.createElement("div");
      testo.textContent = v.testo;
      const meta = document.createElement("div");
      meta.className = "memoria-meta";
      const autore = { harness: "automatica", modello: "dal modello", utente: "tua" }[v.autore] || v.autore;
      meta.textContent = [autore, v.titolo_chat ? `da «${v.titolo_chat}»` : ""].filter(Boolean).join(" · ");
      riga.append(testo, meta);
      box.appendChild(riga);
    }
  }
  $("memoria-foglio").classList.remove("hidden");
}

function chiudiMemoria() {
  $("memoria-foglio").classList.add("hidden");
}

function setBanner(text) {
  const el = $("conn-banner");
  if (!text) { el.classList.add("hidden"); return; }
  el.textContent = text;
  el.classList.remove("hidden");
}

function renderSessions(sessions) {
  const list = $("session-list");
  list.innerHTML = "";
  // Qui le libere; quelle di un progetto stanno nel suo gruppo, sopra.
  const libere = sessions.filter((s) => !s.progetto && !s.archiviata);
  for (const s of libere) list.appendChild(rigaChat(s));
  if (typeof renderProgettiMobile === "function") renderProgettiMobile(sessions);
  const titolo = $("libere-titolo");
  if (titolo) titolo.classList.toggle("hidden", !(progettiNoti.length && libere.length));
}

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? String(iso) : d.toLocaleString();
}

// -------------------------------------------------------- conversazione ----

async function openChat(id) {
  currentId = id;
  ultimaFirma = null; // nuova conversazione: il pannello va ricostruito
  lavoro = nuovoLavoro(false);
  inizioTurno = 0;
  $("view-list").classList.add("hidden");
  $("view-chat").classList.remove("hidden");
  stopStream();
  // L'apertura vera si dichiara una volta sola: e' un gesto dell'utente, e ha
  // effetti (diventa la conversazione corrente, libera le porte di quella da
  // cui si veniva). Il polling qui sotto usa la GET, che non tocca niente.
  try { await jsonPost(`/api/sessions/${id}/open`, {}); } catch (_) {}
  await refreshChat(true);
  attachStream();
  startPolling();
}

async function refreshChat(attachQuestionCard) {
  if (!currentId) return;
  try {
    // GET e non POST /open: questa funzione gira ogni cinque secondi dal
    // polling, e /open ferma le anteprime della conversazione precedente --
    // il telefono avrebbe smontato il container del desktop dodici volte al
    // minuto. Rileggere non e' aprire.
    const payload = await api(`/api/sessions/${currentId}`);
    // Se messaggi e domanda in sospeso sono identici a quanto gia' disegnato,
    // non si tocca niente: e' il caso del polling ogni 5 secondi, e ridisegnare
    // cancellerebbe quello che l'utente sta scrivendo nella risposta.
    const firma = JSON.stringify([payload.messages || [], payload.pending || null]);
    if (firma !== ultimaFirma) {
      renderConversation(payload.messages || [], payload.pending || null, attachQuestionCard);
      ultimaFirma = firma;
    }
    running = Boolean(payload.running);
    updateComposer();
    // Rotaia del piano e misure: fuori dalla firma, perche' cambiano anche
    // quando i messaggi no (un punto chiuso, le statistiche di fine turno).
    disegnaRotaia(payload.plan || []);
    if (!running) applicaStats(payload.stats);
    // Sottoriga della barra: modello e workspace di QUESTA chat. Solo
    // lettura: si aggiornano a ogni rilettura (il polling li tiene freschi
    // se un'apertura da desktop ha cambiato il modello), ma niente clic.
    const modello = String(payload.model || "").trim();
    const ws = String(payload.session_workspace || "").trim();
    const suo = payload.progetto;
    const bottone = $("chat-progetto");
    if (bottone) {
      bottone.hidden = !suo;
      if (suo) {
        bottone.textContent = suo.nome;
        bottone.title = `Progetto «${suo.nome}»: tocca per leggerne la memoria`;
        bottone.onclick = () => {
          const p = progettiNoti.find((x) => x.path === suo.path);
          apriMemoria(p || { ...suo, memoria: [] });
        };
      }
    }
    $("chat-model").textContent = modello;
    // Dentro un progetto la cartella e' la sua: la goccia del progetto la dice
    // gia', e su un telefono lo spazio della riga e' poco.
    $("chat-workspace").textContent = ws && !suo ? nomeCartella(ws) : "";
    $("chat-workspace").title = ws;
    // Il separatore compare solo se le due informazioni ci sono tutte e due.
    // Qui serve querySelector: $ e' getElementById, non prende le classi.
    const sep = document.querySelector(".chat-meta .meta-sep");
    if (sep) sep.hidden = !(modello && ws && !suo);
  } catch (err) {
    toast(`Lettura fallita: ${err.message}`);
  }
}

function stripThink(text) {
  // I blocchi di ragionamento del modello non stanno bene in una bolla.
  return String(text).replace(/<think>[\s\S]*?<\/think>/g, "").trim();
}

/** La risposta che l'utente ha dato a una domanda dell'agente.
 *
 *  Il risultato del tool ``ask_user_question`` e' un JSON con ``user_answer``,
 *  che puo' essere una stringa (testo o scelta singola) o una lista (scelta
 *  multipla). Se il formato non si riconosce, meglio niente che un JSON
 *  crudo in mezzo alla conversazione. */
function rispostaData(contenuto) {
  try {
    const data = JSON.parse(String(contenuto ?? "")).user_answer;
    if (Array.isArray(data)) return data.join(", ");
    return data == null ? "" : String(data);
  } catch (_) {
    return "";
  }
}

/** Domanda dell'agente e risposta dell'utente, come scambio chiuso: la
 *  domanda con la sua cornice, la risposta nella bolla di chi l'ha data. */
function aggiungiScambio(domanda, risposta) {
  if (domanda) addBubble("pending", `❓ ${domanda}`, "risposta-data");
  if (risposta) addBubble("user", risposta, "risposta-scelta");
}

// I tre segni che l'agente usa in ogni risposta: **grassetto**, `codice` e gli
// elenchi puntati. Il resto del markdown resta testo, ed e' voluto -- su uno
// schermo da telefono i titoli e le tabelle non aggiungono niente.
//
// Si costruiscono **nodi**, mai HTML: niente `innerHTML`, niente escaping da
// ricordarsi. Il testo del modello finisce sempre e solo in `textContent`,
// quindi questa funzione non puo' reintrodurre la superficie che `textContent`
// esisteva per chiudere. E' l'unica forma in cui valeva la pena farlo.
function scriviFormattato(nodo, testo) {
  nodo.textContent = "";
  const righe = String(testo ?? "").split("\n");
  righe.forEach((riga, i) => {
    const punto = riga.match(/^\s*[-*+]\s+(.*)$/);
    if (punto) {
      // "• " al posto del trattino: e' un elenco, e sul telefono si vede.
      nodo.appendChild(document.createTextNode("• "));
      inline(nodo, punto[1]);
    } else {
      inline(nodo, riga);
    }
    if (i < righe.length - 1) nodo.appendChild(document.createTextNode("\n"));
  });
  return nodo;
}

function inline(nodo, testo) {
  // Un'unica passata: chi combacia prima vince, e i due gruppi si escludono.
  const re = /\*\*([^*\n]+)\*\*|`([^`\n]+)`/g;
  let ultimo = 0;
  let m;
  while ((m = re.exec(testo)) !== null) {
    if (m.index > ultimo) {
      nodo.appendChild(document.createTextNode(testo.slice(ultimo, m.index)));
    }
    const tag = m[1] !== undefined ? "strong" : "code";
    const el = document.createElement(tag);
    el.textContent = m[1] !== undefined ? m[1] : m[2];
    nodo.appendChild(el);
    ultimo = m.index + m[0].length;
  }
  if (ultimo < testo.length) {
    nodo.appendChild(document.createTextNode(testo.slice(ultimo)));
  }
}

function addBubble(role, text, extraClass) {
  const div = document.createElement("div");
  div.className = `msg ${role}` + (extraClass ? ` ${extraClass}` : "");
  // Solo le risposte dell'agente: quello che scrive l'utente e' suo, e
  // trasformargli due asterischi in grassetto sarebbe correggerlo.
  if (role === "agent") scriviFormattato(div, text);
  else div.textContent = text;
  $("messages").appendChild(div);
  scrollBottom();
  return div;
}

function renderConversation(messages, pending, withCard) {
  const box = $("messages");
  box.innerHTML = "";
  $("chat-title").textContent = "";
  // Il DOM e' stato svuotato: la regia dei blocchi riparte da zero, ferma.
  lavoro = nuovoLavoro(false);
  // L'ora del messaggio precedente (``ts``, orologio del server): da li' il
  // modello ha cominciato a lavorare sul passo dopo. Le durate dei blocchi e
  // dei pensieri si ricavano cosi', le stesse del desktop.
  let prima = null;

  // La cronologia si ridisegna con gli stessi blocchi della diretta: i
  // messaggi `tool` salvati portano gia' nome, argomenti, risultato, esito e
  // durata. Una sola idea di "com'e' fatto un passo", non due che divergono.
  for (const m of messages) {
    const ts = Number.isFinite(m.ts) ? m.ts : null;
    const inizio = prima;
    if (ts !== null) prima = ts;
    if (m.role === "user") {
      if (m.hidden) continue;
      lavoro.chiudi();
      const bubble = addBubble("user", m.content || "");
      const names = (m.attachments || []).map((a) => a.name).filter(Boolean);
      if (names.length) {
        const att = document.createElement("span");
        att.className = "att";
        att.textContent = `📎 ${names.join(", ")}`;
        bubble.appendChild(att);
      }
    } else if (m.role === "assistant") {
      const grezzo = String(m.content || "");
      const pensiero = [...grezzo.matchAll(/<think>([\s\S]*?)(?:<\/think>|$)/g)]
        .map((x) => x[1]).join("").trim();
      if (pensiero) lavoro.pensieroSalvato(pensiero, inizio, ts);
      const clean = stripThink(grezzo);
      if (clean) {
        lavoro.chiudi();
        addBubble("agent", clean);
      }
    } else if (m.role === "memoria") {
      // La memoria del progetto aggiornata a fine turno: una riga, non una bolla.
      lavoro.chiudi();
      addBubble("nota", notaMemoria(m.esito));
    } else if (m.role === "error") {
      // L'errore che ha chiuso il turno: dal vivo arriva come toast, che
      // sparisce; qui resta scritto nella conversazione.
      lavoro.chiudi();
      addBubble("agent", m.content || "", "errore");
    } else if (m.role === "tool") {
      if (m.name === "ask_user_question") {
        // Una domanda gia' risposta non e' un passo: e' un pezzo di
        // conversazione, e resta in chiaro come sul desktop.
        lavoro.chiudi();
        aggiungiScambio((m.args || {}).question || "", rispostaData(m.content));
        continue;
      }
      lavoro.concludiTool({
        nome: m.name, args: m.args, risultato: m.content,
        ok: m.ok !== false, durata: m.duration_s, id: m.tool_call_id,
      }, { inizio, fine: ts });
    }
  }
  lavoro.chiudi();

  if (pending && pending.question) {
    addBubble("pending", `❓ ${pending.question}`);
    showQuestionCard(pending, withCard);
  } else {
    hideQuestionCard();
  }
  scrollBottom();
}

function notaMemoria(esito, motivo) {
  const riassunto = esito && esito.riassunto;
  if (riassunto && riassunto !== "nessun cambiamento") return `Memoria del progetto aggiornata: ${riassunto}`;
  return `Memoria del progetto: ${motivo || "niente da aggiungere"}`;
}

function scrollBottom() {
  const box = $("messages");
  box.scrollTop = box.scrollHeight;
}

function showQuestionCard(pending, focus) {
  // Stessa domanda gia' a schermo (es. ridisegno dopo un evento ripetuto):
  // si aggiornano i bottoni ma NON si svuota la risposta a meta' scrittura.
  const stessa =
    !$("question-card").classList.contains("hidden") &&
    $("question-card").dataset.domanda === (pending.question || "");
  $("question-card").dataset.domanda = pending.question || "";
  $("question-card").classList.remove("hidden");
  $("question-text").textContent = pending.question || "";
  if (!stessa) $("answer-input").value = "";

  // Le scelte del tool ask-user-question: bottoni che rispondono al tocco,
  // o caselle da spuntare se l'agente ammette piu' risposte.
  const box = $("question-options");
  box.innerHTML = "";
  const options = Array.isArray(pending.options) ? pending.options : [];
  if (options.length) {
    for (const opt of options) {
      const label = typeof opt === "string" ? opt : (opt.label || opt.value || "");
      const value = typeof opt === "string" ? opt : (opt.value ?? opt.label ?? label);
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "option-btn";
      btn.textContent = label;
      if (pending.allow_multiple) {
        btn.addEventListener("click", () => btn.classList.toggle("on"));
        btn.dataset.value = value;
      } else {
        btn.addEventListener("click", () => submitAnswer(value));
      }
      box.appendChild(btn);
    }
    if (pending.allow_multiple) {
      const many = document.createElement("button");
      many.type = "button";
      many.className = "primary";
      many.textContent = "Rispondi";
      many.addEventListener("click", () => {
        const scelte = [...box.querySelectorAll(".option-btn.on")].map((b) => b.dataset.value);
        if (scelte.length) submitAnswer(scelte);
      });
      box.appendChild(many);
    }
    $("answer-input").placeholder = pending.allow_multiple
      ? "Oppure scrivi liberamente…"
      : "Oppure rispondi a mano…";
  } else {
    $("answer-input").placeholder = "La tua risposta…";
  }
  if (focus) $("answer-input").focus();
}

function hideQuestionCard() {
  $("question-card").classList.add("hidden");
  $("question-text").textContent = "";
  $("question-card").dataset.domanda = "";
}

function updateComposer() {
  $("btn-stop").classList.toggle("hidden", !running);
  $("btn-send").classList.toggle("hidden", running);
  // La striscia segue lo stato vero, non solo gli eventi: aprendo dal
  // telefono una chat che sta gia' lavorando sul desktop, ``running`` arriva
  // dalla rilettura e la striscia deve comparire lo stesso.
  if (running) {
    if (!inizioTurno) inizioTurno = Date.now();
    mostraAttivita();
  } else {
    nascondiAttivita();
  }
}

// ------------------------------------------------------- invio messaggi ----

async function sendMessage() {
  const input = $("prompt-input");
  const prompt = input.value.trim();
  if (!prompt || !currentId) return;
  input.value = "";
  input.style.height = "auto";
  try {
    await jsonPost("/api/chat", { session_id: currentId, prompt });
    addBubble("user", prompt);
    // La striscia si accende **al tocco**, non al primo evento: fra l'invio e
    // il primo token del modello passano secondi -- caricamento in VRAM
    // compreso -- ed e' proprio l'intervallo in cui sembra non sia successo
    // niente.
    running = true;
    inizioTurno = Date.now();
    updateComposer();
    mostraAttivita("il modello sta partendo");
    await refreshChat(false);
    attachStream();
  } catch (err) {
    toast(err.message);
  }
}

async function sendAnswer() {
  const input = $("answer-input");
  const answer = input.value.trim();
  if (!answer || !currentId) return;
  await submitAnswer(answer);
}

// Una sola strada per /api/answer: testo libero, scelta singola (stringa)
// o scelta multipla (lista) -- il server accetta tutti e tre.
async function submitAnswer(answer) {
  try {
    await jsonPost("/api/answer", { session_id: currentId, answer });
    hideQuestionCard();
    running = true;
    inizioTurno = Date.now();
    updateComposer();
    mostraAttivita("il modello riprende");
    await refreshChat(false);
    attachStream();
  } catch (err) {
    toast(err.message);
  }
}

async function stopTurn() {
  try {
    await jsonPost(`/api/stop/${currentId}`, {});
    // La POST e' andata: da qui in poi il turno **e' fermo**, e l'interfaccia
    // deve dirlo subito. Prima si aspettava il frame `done` dallo stream, che
    // e' esattamente cio' che puo' non arrivare -- filo caduto, telefono
    // bloccato, server riavviato -- e la striscia "sta lavorando" restava
    // accesa fino al turno successivo. Se il `done` arriva, rifa' le stesse
    // due righe: sono idempotenti.
    running = false;
    updateComposer();
    nascondiAttivita();
  } catch (err) {
    toast(err.message);
  }
}

// ------------------------------------------------------------- streaming ----

function stopStream() {
  if (source) { source.close(); source = null; }
}

/** Toglie dal DOM il turno in corso, prima che l'arretrato lo ridisegni.
 *
 *  Solo il turno vivo: i messaggi dei turni chiusi sono cronologia, li ha gia'
 *  scritti refreshChat e nessun frame dell'arretrato li ripete.
 */
function pulisciTurnoVivo() {
  document.querySelectorAll(".msg.agent.live, [data-turno-vivo]").forEach((n) => n.remove());
  lavoro = nuovoLavoro(true);
  testoInCorso = "";
  passoCorrente = 0;
}

function attachStream() {
  if (!currentId) return;
  stopStream();
  // Il server manda tutto l'arretrato ad ogni sottoscrizione (runner.py:
  // "prima tutto l'arretrato e poi il flusso dal vivo"). Al riaggancio -- rete
  // che torna, schermo riacceso, i 1,5 s dopo un onerror -- quel backlog viene
  // rigiocato: senza ripulire, liveBubble non trova piu' la bolla (l'evento
  // 'assistant' le ha tolto .live) e ne crea una seconda, e ogni tool_start
  // accoda una riga che c'e' gia'. Il turno appariva due volte. Il desktop lo
  // risolve gia' cosi', in web/app.js (`vecchio.wrap.remove()`).
  pulisciTurnoVivo();
  source = new EventSource(`/api/stream/${currentId}`);

  source.onmessage = (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch (_) { return; }
    handleEvent(data);
  };
  source.onerror = () => {
    // L'upstream chiude lo stream a fine turno: si riaggancia solo se il
    // turno e' ancora vivo, altrimenti e' polling normale.
    source.close();
    source = null;
    if (running) setTimeout(() => { if (running && !source) attachStream(); }, 1500);
  };
}

function liveBubble() {
  // Non ``:last-child``: fra la bolla e il fondo dell'elenco puo' esserci la
  // tendina dei passi, e la bolla smetterebbe di essere l'ultimo figlio --
  // ne nascerebbe una seconda ad ogni pezzo di testo.
  const vive = document.querySelectorAll(".msg.agent.live");
  return vive[vive.length - 1] || addBubble("agent", "", "live");
}

function handleEvent(data) {
  // L'ora del server viaggia su ogni frame: le durate si misurano con quella.
  if (Number.isFinite(data.t)) lavoro.ora(data.t);
  switch (data.type) {
    case "start":
      running = true;
      inizioTurno = Date.now();
      testoInCorso = "";
      passoCorrente = 0;
      lavoro = nuovoLavoro(true);
      if (Number.isFinite(data.t)) lavoro.ora(data.t);
      updateComposer();
      mostraAttivita("sta ragionando");
      break;
    case "step":
      passoCorrente = Number(data.step) || passoCorrente + 1;
      lavoro.segnaPasso(passoCorrente);
      mostraAttivita("sta ragionando");
      break;
    case "reasoning":
      // Dal vivo si vedono le ultime quattro righe; il pensiero intero si
      // legge toccando la sua riga, a passo chiuso.
      if (data.append) lavoro.appendThinking(data.append);
      else lavoro.setThinking(data.text ?? "");
      mostraAttivita("sta ragionando");
      break;
    case "tool_start":
      lavoro.avviaTool({ nome: data.name, args: data.args, id: data.call_id });
      mostraAttivita(descriviTool(data.name, data.args));
      break;
    case "tool_end":
      lavoro.concludiTool({
        nome: data.name, args: data.args, risultato: data.result,
        ok: data.ok, durata: data.duration_s, id: data.call_id,
      });
      break;
    case "content": {
      // L'evento porta ``append`` (i soli caratteri nuovi) oppure ``text`` (il
      // testo completo, che sostituisce): ne arriva uno solo dei due. Prima
      // era sempre cumulativo, un evento per token -- e da un telefono in 4G
      // erano megabyte per una risposta di due righe.
      //
      // (Storia: qui si leggeva ``data.delta``, che non e' mai esistito: la
      // risposta restava invisibile fino a fine turno.)
      const grezzo = data.append
        ? (testoInCorso += data.append)
        : (testoInCorso = String(data.text ?? ""));
      const clean = stripThink(grezzo);
      const viva = document.querySelector(".msg.agent.live");
      // Niente bolla vuota: un passo che non dice niente non ne apre una, e
      // non chiude il blocco di lavoro.
      if (!clean && !viva) break;
      // Il modello parla: il blocco di lavoro si chiude qui, e la bolla viene
      // dopo di lui.
      lavoro.chiudi();
      const bubble = liveBubble();
      // Assegnazione secca, non `if (clean)`: quando il modello passa dal
      // testo al ragionamento, `stripThink` torna vuoto e la bolla restava con
      // il testo di prima -- una frase vecchia congelata sotto "sta scrivendo".
      // Il desktop fa gia' cosi'.
      scriviFormattato(bubble, clean);
      mostraAttivita("sta scrivendo");
      scrollBottom();
      break;
    }
    case "assistant": {
      // Fine del passo: la versione autorevole arriva sempre intera, e da qui
      // l'accumulo riparte da zero per il passo successivo.
      testoInCorso = "";
      // Un 'assistant' per un passo gia' reso arriva solo dall'arretrato di un
      // riaggancio: e' lo stesso testo, e va ignorato.
      if (document.querySelector(`.msg.agent[data-passo="${passoCorrente}"]`)) break;
      const clean = stripThink(data.content ?? "");
      const viva = document.querySelector(".msg.agent.live");
      if (!clean && !viva) break;
      lavoro.chiudi();
      const bubble = liveBubble();
      bubble.dataset.passo = String(passoCorrente);
      bubble.classList.remove("live");
      if (clean) scriviFormattato(bubble, clean);
      if (!clean && !bubble.textContent) bubble.remove();
      scrollBottom();
      break;
    }
    case "metriche":
      applicaMetriche(data);
      break;
    case "memoria":
      // L'ultimo passo del turno: l'harness aggiorna la memoria del progetto.
      if (data.fase === "inizio") mostraAttivita("aggiorna la memoria del progetto");
      else {
        lavoro.chiudi();
        addBubble("nota", notaMemoria(data.esito, data.motivo));
        loadSessions();
      }
      break;
    case "plan":
      disegnaRotaia(data.steps || []);
      break;
    case "question":
      // AwaitingUserInput arriva intero dallo stream: domanda, opzioni e
      // allow_multiple sono gia' nel frame, la card li usa tutti.
      lavoro.chiudi();
      nascondiAttivita();
      addBubble("pending", `❓ ${data.question ?? ""}`);
      showQuestionCard(
        {
          question: data.question ?? "",
          options: data.options ?? [],
          allow_multiple: Boolean(data.allow_multiple),
        },
        true,
      );
      running = false;
      updateComposer();
      break;
    case "done":
    case "error":
      if (data.type === "error" && data.message) toast(data.message);
      running = false;
      updateComposer();
      lavoro.chiudi();
      nascondiAttivita();
      document.querySelectorAll(".msg.live").forEach((el) => el.classList.remove("live"));
      refreshChat(false).then(loadSessions);
      break;
    default:
      break;
  }
}

// ------------------------------------------------------------ bus globale ----

/** Le notizie larghe del server (`/api/events`), le stesse che ascolta il
 *  desktop: e' partito un turno, e' finito, l'agente ha fatto una domanda,
 *  l'elenco delle chat e' cambiato.
 *
 *  Senza questo, il telefono si accorgeva di un turno partito dal desktop solo
 *  al giro di polling successivo, e -- peggio -- appena `running` diventava
 *  vero il polling smetteva di rileggere la conversazione mentre nessuno
 *  stream era attaccato: la chat restava ferma fino a un refresh a mano.
 *  Succedeva soprattutto con `ask_user_question`, perche' li' il turno si
 *  chiude subito e la novita' e' tutta nella domanda in sospeso. */
function bindGlobalEvents() {
  const bus = new EventSource("/api/events");
  bus.onmessage = async (msg) => {
    let event;
    try { event = JSON.parse(msg.data); } catch (_) { return; }

    if (event.type === "sessions" || event.type === "progetto") { loadSessions(); return; }
    if (event.type !== "turn" && event.type !== "question") return;

    loadSessions();
    if (!currentId || event.session_id !== currentId) return;
    // La chat aperta si rilegge dal disco: li' c'e' gia' il messaggio finale
    // o la domanda in sospeso. Se il turno e' vivo e questa pagina non e'
    // attaccata allo stream, ci si attacca adesso -- e' il caso del turno
    // fatto partire dall'altro schermo.
    await refreshChat(false);
    if (running && !source) attachStream();
  };
}

// ---------------------------------------------------------------- polling ----

function startPolling() {
  stopPolling();
  // Rete mobile o scheda bloccata: il polling resta la rete di sicurezza sotto
  // al bus globale, per quando la connessione SSE cade e nessuno se ne accorge.
  pollTimer = setInterval(async () => {
    if (document.hidden) return;
    loadSessions();
    if (running && !source) {
      // Turno vivo ma nessuno stream: e' la situazione in cui la chat
      // rimaneva congelata. Riagganciarsi costa una connessione e sblocca.
      attachStream();
      return;
    }
    if (!running && !source) {
      const prima = $("messages").childElementCount;
      await refreshChat(false);
      if ($("messages").childElementCount !== prima) scrollBottom();
    }
  }, 5000);
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

// ------------------------------------------------------------------ init ----

$("btn-back").addEventListener("click", () => {
  currentId = null;
  stopStream();
  stopPolling();
  nascondiAttivita();
  lavoro = nuovoLavoro(false);
  inizioTurno = 0;
  $("view-chat").classList.add("hidden");
  $("view-list").classList.remove("hidden");
  loadSessions();
});

$("btn-new").addEventListener("click", async () => {
  try {
    const payload = await jsonPost("/api/sessions", {});
    await loadSessions();
    openChat(payload.session_id);
  } catch (err) { toast(err.message); }
});

$("btn-refresh").addEventListener("click", () => refreshChat(true));
$("memoria-chiudi").addEventListener("click", chiudiMemoria);
$("memoria-foglio").addEventListener("click", (ev) => {
  if (ev.target === $("memoria-foglio")) chiudiMemoria();
});
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape" && !$("memoria-foglio").classList.contains("hidden")) chiudiMemoria();
});
$("goccia-tok").addEventListener("click", () => toast(dettaglioMisure()));
$("rotaia-piano").addEventListener("click", (ev) => {
  ev.stopPropagation();
  if ($("piano-foglio").classList.contains("hidden")) apriFoglioPiano();
  else chiudiFoglioPiano();
});
// Un tocco fuori dal foglio del piano lo chiude.
document.addEventListener("click", (ev) => {
  const foglio = $("piano-foglio");
  if (foglio && !foglio.classList.contains("hidden") && !foglio.contains(ev.target)) chiudiFoglioPiano();
});

$("btn-send").addEventListener("click", sendMessage);
$("btn-answer").addEventListener("click", sendAnswer);
$("btn-stop").addEventListener("click", stopTurn);

for (const [input, button] of [
  ["prompt-input", "btn-send"],
  ["answer-input", "btn-answer"],
]) {
  $(input).addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) {
      ev.preventDefault();
      $(button).click();
    }
  });
}

const promptInput = $("prompt-input");
promptInput.addEventListener("input", () => {
  promptInput.style.height = "auto";
  promptInput.style.height = `${Math.min(promptInput.scrollHeight, 120)}px`;
});

loadSessions();
// Il bus resta attaccato per tutta la vita della pagina, anche nell'elenco:
// e' cosi' che una chat aperta sul desktop compare qui senza toccare niente.
bindGlobalEvents();
