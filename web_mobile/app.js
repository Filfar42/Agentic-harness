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

// Tendina dei passi del turno in corso: <details> aperto mentre l'agente
// lavora, richiuso quando il messaggio arriva. Sullo schermo del telefono un
// elenco di passi lungo quanto la conversazione la rende illeggibile: i passi
// servono *mentre* succedono, dopo bastano contati.
let gruppoPassi = null;
let contaPassi = 0;
let contaTool = 0;
// Strumenti del SOLO passo corrente: il sommario vivo dice "passo N · M
// strumenti" e quel M deve tornare con le righe visibili sotto, non col
// totale del turno che a metà lavoro non c'entra con quello che si vede.
let contaToolPasso = 0;
// Quanto e' durato: dall'orologio mentre il turno e' vivo, dalla somma delle
// durate dei tool quando si ridisegna una conversazione salvata -- li' il
// tempo di parete non esiste piu', e inventarlo sarebbe peggio che tacerlo.
let contaSecondi = 0;
let inizioTurno = 0;
let tickAttivita = null;

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
// Due livelli, e nessuno dei due mostra il contenuto: sullo schermo del
// telefono il pensiero e i risultati dei tool sono megabyte di testo che
// coprono la conversazione. Serve sapere **che cosa** sta facendo, non che
// cosa ha letto.
//   1. la striscia sopra il composer: una riga, sempre visibile, animata --
//      e' quella che risponde a "sta ancora lavorando o si e' piantato?";
//   2. le righe dei passi nella tendina: una riga per passo, piu' piccole e
//      piu' chiare del testo, che si richiudono quando la risposta arriva.

function troncaTesto(valore, quanti) {
  const testo = String(valore ?? "").replace(/\s+/g, " ").trim();
  if (testo.length <= quanti) return testo;
  // trimEnd prima dei puntini: "configura il …" con lo spazio in mezzo
  // sembra un errore di stampa, non un troncamento.
  return `${testo.slice(0, quanti - 1).trimEnd()}…`;
}

/** L'ultima cartella di un percorso, per la sottoriga della topbar mobile:
 *  "/work/progetto" -> "progetto". Il percorso intero sta nel title. */
function nomeCartella(percorso) {
  const pezzi = String(percorso ?? "").split(/[\\/]/).filter(Boolean);
  return pezzi[pezzi.length - 1] || "";
}

function nomeFile(percorso) {
  const pulito = String(percorso ?? "").replace(/[\\/]+$/, "");
  const pezzi = pulito.split(/[\\/]/);
  return pezzi[pezzi.length - 1] || pulito || "?";
}

// Il verbo prima dell'oggetto: "legge tools.py" si capisce in mezzo secondo,
// `read_file {"filepath": "core/tools.py"}` no. Del percorso resta il nome del
// file -- e' la stessa regola delle gocce sul desktop: *cosa*, non *dove*.
const VERBI_TOOL = {
  list_files: (a) => `elenca ${a.subfolder && a.subfolder !== "." ? nomeFile(a.subfolder) : "il workspace"}`,
  read_file: (a) => `legge ${nomeFile(a.filepath)}`,
  write_file: (a) => `scrive ${nomeFile(a.filepath)}`,
  edit_file: (a) => `modifica ${nomeFile(a.filepath)}`,
  search_files: (a) => `cerca "${troncaTesto(a.pattern, 22)}"`,
  run_command: (a) => `esegue ${troncaTesto(a.command, 30)}`,
  manage_plan: (a) => `piano: ${a.action || "aggiorna"}`,
  manage_notes: (a) => `appunti: ${a.action || "aggiorna"}`,
  manage_memory: (a) => `memoria: ${a.action || "aggiorna"}`,
  preview: (a) => `anteprima${a.action ? `: ${a.action}` : ""}`,
  esplora: () => "manda un esploratore",
  vault_search: (a) => `cerca nel vault ${troncaTesto(a.vault, 18)}`,
  web_search: (a) => `cerca sul web "${troncaTesto(a.query, 22)}"`,
  ask_user_question: () => "ti fa una domanda",
};

function descriviTool(nome, args) {
  const a = args && typeof args === "object" ? args : {};
  const verbo = VERBI_TOOL[nome];
  if (!verbo) return String(nome || "strumento");
  try {
    return troncaTesto(verbo(a), 46) || String(nome);
  } catch (_) {
    return String(nome);
  }
}

// Il risultato di un tool non si mostra, ma se e' andato male si deve vedere:
// e' l'unica informazione del contenuto che vale la riga.
function toolAndatoBene(contenuto, esplicito) {
  if (esplicito === false) return false;
  if (esplicito === true) return true;
  try {
    return !JSON.parse(String(contenuto ?? "")).error;
  } catch (_) {
    return true;
  }
}

/** Gli argomenti di una tool call salvata: dal function calling nativo
 *  arrivano come stringa JSON, dal recupero dal testo come oggetto gia' fatto. */
function argomentiDi(call) {
  const grezzi = (call.function || {}).arguments ?? call.args ?? call.arguments;
  if (grezzi && typeof grezzi === "object") return grezzi;
  try {
    return JSON.parse(String(grezzi || "{}"));
  } catch (_) {
    return {};
  }
}

/** La riga del passo che corrisponde a una chiamata, o l'ultima scritta se
 *  quell'id non c'e' (sessioni salvate da versioni precedenti). */
function rigaDellaChiamata(callId) {
  if (!gruppoPassi) return null;
  if (callId) {
    const esatta = gruppoPassi.querySelector(`.passo.tool[data-call="${callId}"]`);
    if (esatta) return esatta;
  }
  const righe = [...gruppoPassi.querySelectorAll(".passo.tool")];
  return righe[righe.length - 1] || null;
}

function apriGruppoPassi() {
  if (gruppoPassi && gruppoPassi.isConnected) return gruppoPassi;
  const det = document.createElement("details");
  det.className = "passi";
  det.open = true;
  const sum = document.createElement("summary");
  sum.className = "passi-sommario";
  sum.textContent = "sta lavorando…";
  det.appendChild(sum);
  $("messages").appendChild(det);
  gruppoPassi = det;
  return det;
}

function rigaPasso(testo, tipo) {
  const gruppo = apriGruppoPassi();
  const riga = document.createElement("div");
  riga.className = `passo${tipo ? ` ${tipo}` : ""}`;
  riga.textContent = testo;
  gruppo.appendChild(riga);
  scrollBottom();
  return riga;
}

function sommarioPassi(chiuso) {
  if (!gruppoPassi) return;
  const sum = gruppoPassi.querySelector(".passi-sommario");
  if (!sum) return;
  if (!chiuso) {
    // "passo N · M strumenti": M e' quello che il passo corrente sta usando,
    // non il totale del turno -- deve tornare con le righe aperte sotto.
    if (!contaPassi) {
      sum.textContent = "sta lavorando…";
    } else {
      const strumenti = contaToolPasso
        ? ` · ${contaToolPasso} ${contaToolPasso === 1 ? "strumento" : "strumenti"}`
        : "";
      sum.textContent = `passo ${contaPassi}${strumenti}`;
    }
    return;
  }
  const pezzi = [`${contaPassi || 1} ${contaPassi === 1 ? "passo" : "passi"}`];
  if (contaTool) pezzi.push(`${contaTool} ${contaTool === 1 ? "strumento" : "strumenti"}`);
  const secondi = inizioTurno
    ? Math.round((Date.now() - inizioTurno) / 1000)
    : Math.round(contaSecondi);
  if (secondi > 0) pezzi.push(durataBreve(secondi));
  sum.textContent = pezzi.join(" · ");
}

/** Richiude la tendina: e' il gesto che tiene snella la conversazione quando
 *  il messaggio e' pronto. I passi restano, a un tocco di distanza. */
function chiudiGruppoPassi() {
  if (!gruppoPassi) return;
  gruppoPassi.querySelectorAll(".passo.corso").forEach((r) => r.classList.remove("corso"));
  sommarioPassi(true);
  gruppoPassi.open = false;
  gruppoPassi = null;
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
    const data = await api("/api/sessions");
    renderSessions(data.sessions || []);
    setBanner(null);
  } catch (err) {
    setBanner(`Server principale non raggiungibile: ${err.message}`);
  }
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
  for (const s of sessions) {
    const li = document.createElement("li");
    li.dataset.id = s.id;

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
    list.appendChild(li);
  }
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
  gruppoPassi = null;
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
    // Sottoriga della barra: modello e workspace di QUESTA chat. Solo
    // lettura: si aggiornano a ogni rilettura (il polling li tiene freschi
    // se un'apertura da desktop ha cambiato il modello), ma niente clic.
    const modello = String(payload.model || "").trim();
    const ws = String(payload.session_workspace || "").trim();
    $("chat-model").textContent = modello;
    $("chat-workspace").textContent = ws ? nomeCartella(ws) : "";
    $("chat-workspace").title = ws;
    // Il separatore compare solo se le due informazioni ci sono tutte e due.
    // Qui serve querySelector: $ e' getElementById, non prende le classi.
    const sep = document.querySelector(".chat-meta .meta-sep");
    if (sep) sep.hidden = !(modello && ws);
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

function addBubble(role, text, extraClass) {
  const div = document.createElement("div");
  div.className = `msg ${role}` + (extraClass ? ` ${extraClass}` : "");
  div.textContent = text;
  $("messages").appendChild(div);
  scrollBottom();
  return div;
}

function renderConversation(messages, pending, withCard) {
  const box = $("messages");
  box.innerHTML = "";
  $("chat-title").textContent = "";
  // Il DOM e' stato svuotato: qualunque tendina fosse aperta non esiste piu'.
  gruppoPassi = null;
  contaPassi = 0;
  contaTool = 0;
  contaSecondi = 0;
  // Le tendine che stiamo per ricostruire sono di turni gia' chiusi: il
  // cronometro del turno vivo non c'entra e va messo da parte, o i riassunti
  // direbbero "3 passi · 41m" contando da quando si e' aperta la pagina.
  const cronometro = inizioTurno;
  inizioTurno = 0;

  // La cronologia si ridisegna con le stesse righe della diretta: i messaggi
  // `tool` salvati portano gia' nome, argomenti, esito e durata. Una sola
  // idea di "com'e' fatto un passo", non due che poi divergono.
  for (const m of messages) {
    if (m.role === "user") {
      if (m.hidden) continue;
      // Un messaggio dell'utente apre un turno nuovo: i conti ripartono, o la
      // tendina del turno dopo direbbe "31 passi" contandoli tutti dall'inizio.
      chiudiGruppoPassi();
      contaPassi = 0;
      contaTool = 0;
      contaSecondi = 0;
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
      const chiamate = m.tool_calls || [];
      if (/<think>/.test(grezzo) && chiamate.length) {
        contaPassi += 1;
        rigaPasso("ragiona", "pensiero");
      }
      for (const call of chiamate) {
        const nome = (call.function || {}).name || call.name;
        if (nome === "ask_user_question") continue;
        contaTool += 1;
        const riga = rigaPasso(descriviTool(nome, argomentiDi(call)), "tool");
        if (call.id) riga.dataset.call = call.id;
      }
      const clean = stripThink(grezzo);
      if (clean) {
        chiudiGruppoPassi();
        addBubble("agent", clean);
      }
    } else if (m.role === "tool") {
      if (m.name === "ask_user_question") {
        // Una domanda gia' risposta non e' un passo: e' un pezzo di
        // conversazione, e resta in chiaro come sul desktop. Prima spariva del
        // tutto appena si rispondeva -- la card se ne andava e nella
        // cronologia non restava traccia ne' della domanda ne' della scelta.
        chiudiGruppoPassi();
        aggiungiScambio((m.args || {}).question || "", rispostaData(m.content));
        continue;
      }
      // Del risultato entra in pagina una cosa sola: se e' andato male. Si
      // appende alla riga della sua chiamata, trovata per id.
      contaSecondi += Number(m.duration_s) || 0;
      const riga = rigaDellaChiamata(m.tool_call_id);
      if (riga && !toolAndatoBene(m.content, m.ok)) riga.classList.add("male");
    }
  }
  chiudiGruppoPassi();
  inizioTurno = cronometro;

  if (pending && pending.question) {
    addBubble("pending", `❓ ${pending.question}`);
    showQuestionCard(pending, withCard);
  } else {
    hideQuestionCard();
  }
  scrollBottom();
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
  try { await jsonPost(`/api/stop/${currentId}`, {}); } catch (err) { toast(err.message); }
}

// ------------------------------------------------------------- streaming ----

function stopStream() {
  if (source) { source.close(); source = null; }
}

function attachStream() {
  if (!currentId) return;
  stopStream();
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
  switch (data.type) {
    case "start":
      running = true;
      inizioTurno = Date.now();
      contaPassi = 0;
      contaTool = 0;
      contaToolPasso = 0;
      contaSecondi = 0;
      updateComposer();
      apriGruppoPassi();
      mostraAttivita("sta ragionando");
      break;
    case "step":
      contaPassi = Number(data.step) || contaPassi + 1;
      contaToolPasso = 0; // nuovo passo: il contatore per-passo riparte
      sommarioPassi(false);
      mostraAttivita("sta ragionando");
      break;
    case "reasoning": {
      // Il pensiero non entra in pagina: sono migliaia di caratteri che su un
      // telefono seppelliscono la conversazione. Entra il **fatto** che sta
      // ragionando, una riga sola per passo.
      if (!gruppoPassi || !gruppoPassi.querySelector(`.passo.pensiero[data-passo="${contaPassi}"]`)) {
        const riga = rigaPasso("ragiona", "pensiero corso");
        riga.dataset.passo = String(contaPassi);
      }
      mostraAttivita("sta ragionando");
      break;
    }
    case "tool_start": {
      contaTool += 1;
      contaToolPasso += 1;
      const riga = rigaPasso(descriviTool(data.name, data.args), "tool corso");
      if (data.call_id) riga.dataset.call = data.call_id;
      gruppoPassi?.querySelectorAll(".passo.pensiero.corso")
        .forEach((r) => r.classList.remove("corso"));
      sommarioPassi(false);
      mostraAttivita(descriviTool(data.name, data.args));
      break;
    }
    case "tool_end": {
      const riga = rigaDellaChiamata(data.call_id);
      if (riga) {
        riga.classList.remove("corso");
        if (data.ok === false) riga.classList.add("male");
      }
      break;
    }
    case "content": {
      // Il campo e' ``text`` ed e' **cumulativo** (come sul desktop). Il
      // client mobile leggeva ``data.delta``, che non esiste: la risposta
      // restava invisibile finche' il turno non finiva, ed era il motivo
      // principale per cui dal telefono non si capiva se stesse lavorando.
      const bubble = liveBubble();
      const clean = stripThink(data.text ?? "");
      if (clean) bubble.textContent = clean;
      // Il messaggio sta arrivando: i passi hanno finito di servire.
      chiudiGruppoPassi();
      mostraAttivita("sta scrivendo");
      scrollBottom();
      break;
    }
    case "assistant": {
      const bubble = liveBubble();
      bubble.classList.remove("live");
      const clean = stripThink(data.content ?? "");
      bubble.textContent = clean || bubble.textContent;
      if (!clean && !bubble.textContent) bubble.remove();
      chiudiGruppoPassi();
      scrollBottom();
      break;
    }
    case "question":
      // AwaitingUserInput arriva intero dallo stream: domanda, opzioni e
      // allow_multiple sono gia' nel frame, la card li usa tutti.
      chiudiGruppoPassi();
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
      chiudiGruppoPassi();
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

    if (event.type === "sessions") { loadSessions(); return; }
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
  gruppoPassi = null;
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
