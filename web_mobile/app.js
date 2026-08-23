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
  } catch (err) {
    toast(`Lettura fallita: ${err.message}`);
  }
}

function stripThink(text) {
  // I blocchi di ragionamento del modello non stanno bene in una bolla.
  return String(text).replace(/<think>[\s\S]*?<\/think>/g, "").trim();
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

  for (const m of messages) {
    if (m.role === "user") {
      if (m.hidden) continue;
      const bubble = addBubble("user", m.content || "");
      const names = (m.attachments || []).map((a) => a.name).filter(Boolean);
      if (names.length) {
        const att = document.createElement("span");
        att.className = "att";
        att.textContent = `📎 ${names.join(", ")}`;
        bubble.appendChild(att);
      }
    } else if (m.role === "assistant") {
      const clean = stripThink(m.content || "");
      if (clean) addBubble("agent", clean);
    }
  }

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
  let el = document.querySelector(".msg.agent.live:last-child");
  if (!el) {
    el = addBubble("agent", "", "live");
    $("chat-title").textContent = $("chat-title").textContent || "";
  }
  return el;
}

function handleEvent(data) {
  switch (data.type) {
    case "start":
      running = true;
      updateComposer();
      break;
    case "reasoning":
      // Il pensiero in diretta resta fuori dalle bolle: e' rumore su schermo piccolo.
      break;
    case "content": {
      const bubble = liveBubble();
      bubble.dataset.raw = (bubble.dataset.raw || "") + (data.delta ?? "");
      const clean = stripThink(bubble.dataset.raw);
      if (clean) bubble.textContent = clean;
      scrollBottom();
      break;
    }
    case "assistant": {
      const bubble = liveBubble();
      bubble.classList.remove("live");
      bubble.dataset.raw = "";
      const clean = stripThink(data.content ?? "");
      bubble.textContent = clean || bubble.textContent;
      scrollBottom();
      break;
    }
    case "question":
      // AwaitingUserInput arriva intero dallo stream: domanda, opzioni e
      // allow_multiple sono gia' nel frame, la card li usa tutti.
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
      document.querySelectorAll(".msg.live").forEach((el) => el.classList.remove("live"));
      refreshChat(false).then(loadSessions);
      break;
    default:
      break;
  }
}

// ---------------------------------------------------------------- polling ----

function startPolling() {
  stopPolling();
  // Rete mobile o scheda bloccata: il polling tiene comunque le due UI
  // allineate con quanto succede sul desktop.
  pollTimer = setInterval(async () => {
    if (document.hidden) return;
    loadSessions();
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
