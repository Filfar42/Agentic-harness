"""Exercise the real mascot controller with a DOM and a deterministic clock.

These tests check synchronized feedback and lifecycle, not CSS implementation details.
The clock runs expired callbacks in deadline order so an accidental recurring
animation fails without spending real time. Rendering and layout need browser QA.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

quickjs = pytest.importorskip("quickjs")
SOURCE = Path(__file__).resolve().parents[1] / "web" / "companion.js"

DOM_AND_CLOCK = r"""
class EventTarget {
  constructor() { this.listeners = []; }
  addEventListener(type, callback, options = false) {
    const capture = typeof options === 'boolean' ? options : Boolean(options.capture);
    this.listeners.push({type, callback, capture});
  }
  removeEventListener(type, callback, options = false) {
    const capture = typeof options === 'boolean' ? options : Boolean(options.capture);
    this.listeners = this.listeners.filter(item =>
      !(item.type === type && item.callback === callback && item.capture === capture));
  }
  dispatch(type, event = {}) {
    [...this.listeners].filter(item => item.type === type).forEach(item => item.callback(event));
  }
}
class Node extends EventTarget {
  constructor(tag, nodeType = 1) {
    super();
    this.tag = tag;
    this.nodeType = nodeType;
    this.childNodes = [];
    this.parentNode = null;
    this.attributes = {};
    this.dataset = {};
    this.style = {};
    this.isContentEditable = false;
    this.editingAncestor = null;
    const classes = new Set();
    this.classList = {
      add(name) { classes.add(name); },
      remove(name) { classes.delete(name); },
      contains(name) { return classes.has(name); },
      toggle(name, force) {
        const present = force === undefined ? !classes.has(name) : Boolean(force);
        if (present) classes.add(name); else classes.delete(name);
        return present;
      },
    };
  }
  appendChild(node) {
    node.remove();
    this.childNodes.push(node);
    node.parentNode = this;
    return node;
  }
  append(...nodes) { nodes.forEach(node => this.appendChild(node)); }
  set innerHTML(value) {
    if (value !== '') throw new Error('This harness only models empty innerHTML');
    [...this.childNodes].forEach(node => node.remove());
  }
  remove() {
    if (!this.parentNode) return;
    const siblings = this.parentNode.childNodes;
    siblings.splice(siblings.indexOf(this), 1);
    this.parentNode = null;
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  closest() {
    if (['input', 'textarea', 'select'].includes(this.tag)) return this;
    if (this.getAttribute('role') === 'textbox') return this;
    return this.editingAncestor;
  }
}
var host = new Node('div');
var otherHost = new Node('div');
var original = new Node('img');
var caption = new Node('#text', 3);
var clicks = 0;
original.addEventListener('click', () => { clicks += 1; });
host.appendChild(original);
host.appendChild(caption);
var otherOriginal = new Node('img');
otherHost.appendChild(otherOriginal);
var document = new EventTarget();
document.hidden = false;
document.activeElement = null;
document.querySelector = selector => {
  if (selector === '#sidebar .logo') return host;
  if (selector === '#other') return otherHost;
  if (selector === '[') throw new SyntaxError('Invalid selector');
  return null;
};
document.createElementNS = (namespace, tag) => new Node(tag);
document.createElement = tag => new Node(tag);
var motion = new EventTarget();
motion.matches = false;
motion.addListener = callback => motion.addEventListener('change', callback);
motion.removeListener = callback => motion.removeEventListener('change', callback);
var now = 0;
var sequence = 0;
var timers = new Map();
var window = {
  matchMedia: () => motion,
  setTimeout(callback, delay) {
    const id = ++sequence;
    timers.set(id, {callback, deadline: now + delay});
    return id;
  },
  clearTimeout(id) { timers.delete(id); },
};
function advance(milliseconds) {
  const target = now + milliseconds;
  let callbacks = 0;
  while (true) {
    const due = [...timers].filter(([, timer]) => timer.deadline <= target)
      .sort((a, b) => a[1].deadline - b[1].deadline || a[0] - b[0]);
    if (!due.length) break;
    if (++callbacks > 100) throw new Error('Unexpected recurring timers');
    const [id, timer] = due[0];
    timers.delete(id);
    now = timer.deadline;
    timer.callback();
  }
  now = target;
}
function graphic() { return host.childNodes[0]; }
function gesture() { return graphic().getAttribute('data-gesture'); }
function speech() { return host.childNodes[1]; }
function pose() { return graphic().childNodes[0]; }
function hide(value) { document.hidden = value; document.dispatch('visibilitychange'); }
function reduce(value) { motion.matches = value; motion.dispatch('change'); }
"""


@pytest.fixture
def js() -> quickjs.Context:
    """Load the whole production module; no controller functions are mocked."""
    context = quickjs.Context()
    context.eval(DOM_AND_CLOCK)
    context.eval(SOURCE.read_text(encoding="utf-8"))
    context.eval("var companion = window.HarnessCompanion;")
    return context


def test_mount_is_decorative_and_destroy_restores_original_nodes_and_listeners(js) -> None:
    assert js.eval("companion.init()") is True
    assert js.eval("host.childNodes.length") == 2
    assert js.eval("graphic().getAttribute('aria-hidden')") == "true"
    assert js.eval("graphic().getAttribute('focusable')") == "false"
    assert js.eval("speech().getAttribute('aria-hidden')") == "true"
    assert js.eval("host.classList.contains('harness-companion-host')") is True
    js.eval("companion.destroy(); original.dispatch('click');")
    assert js.eval("host.childNodes[0] === original && host.childNodes[1] === caption") is True
    assert js.eval("clicks") == 1
    assert js.eval("host.classList.contains('harness-companion-host')") is False
    assert js.eval("companion.getState()") == "idle"


def test_reinitialization_restores_previous_host_without_duplicating_listeners(js) -> None:
    js.eval("host.classList.add('harness-companion-host'); companion.init();")
    listeners = js.eval("document.listeners.length + motion.listeners.length")
    assert js.eval("companion.init({mount: '#other', state: 'working'})") is True
    assert js.eval("host.childNodes[0] === original && host.childNodes[1] === caption") is True
    assert js.eval("host.classList.contains('harness-companion-host')") is True
    assert js.eval("document.listeners.length + motion.listeners.length") == listeners
    js.eval("companion.destroy();")
    assert js.eval("otherHost.childNodes.length === 1 && otherHost.childNodes[0] === otherOriginal") is True
    assert js.eval("otherHost.classList.contains('harness-companion-host')") is False
    assert js.eval("document.listeners.length + motion.listeners.length") == 0


def test_same_state_does_not_restart_or_extend_a_gesture(js) -> None:
    js.eval("companion.init({state: 'working'}); advance(700);")
    assert js.eval("gesture()") is not None
    assert js.eval("companion.setState('working')") is True
    js.eval("advance(700);")
    assert js.eval("gesture()") is None
    js.eval("companion.setState('working'); advance(600000);")
    assert js.eval("gesture()") is None
    assert js.eval("timers.size") == 0


def test_new_state_cancels_old_gesture_deadline(js) -> None:
    js.eval("companion.init({state: 'working'}); advance(100); companion.setState('success');")
    assert js.eval("gesture()") is not None
    js.eval("advance(1300);")
    assert js.eval("companion.getState()") == "success"
    assert js.eval("gesture()") is not None
    js.eval("advance(100);")
    assert js.eval("gesture()") is None


def test_destroy_is_idempotent_and_cleans_every_resource(js) -> None:
    js.eval("companion.init(); var detached = graphic(); companion.destroy(); companion.destroy();")
    assert js.eval("timers.size") == 0
    assert js.eval("document.listeners.length + motion.listeners.length") == 0
    js.eval("advance(600000); document.dispatch('pointerdown'); hide(true); reduce(true);")
    assert js.eval("timers.size") == 0
    assert js.eval("detached.getAttribute('data-gesture')") is None
    assert js.eval("host.childNodes.length") == 2
    assert js.eval("companion.setState('working')") is False


@pytest.mark.parametrize(
    "options",
    ["{state: 'unknown'}", "{mount: '#missing'}", "{mount: '['}", "{mount: null}",
     "{mount: {nodeType: 3}}", "{mounts: []}", "{mounts: null}",
     "{mounts: ['#sidebar .logo', '#missing']}"],
)
def test_invalid_reinitialization_preserves_active_mount(js, options: str) -> None:
    js.eval("companion.init({state: 'working'}); var mounted = graphic();")
    assert js.eval(f"companion.init({options})") is False
    assert js.eval("graphic() === mounted") is True
    assert js.eval("companion.getState()") == "working"
    assert js.eval("companion.setState('invented')") is False
    assert js.eval("companion.getState()") == "working"


def test_hidden_tab_cancels_movement_and_resume_does_not_replay_completion(js) -> None:
    js.eval("companion.init({state: 'success'}); hide(true);")
    assert js.eval("gesture()") is None
    assert js.eval("timers.size") == 0
    assert js.eval("graphic().getAttribute('data-paused')") == "true"
    js.eval("advance(600000); hide(false);")
    assert js.eval("companion.getState()") == "success"
    assert js.eval("gesture()") is None
    assert js.eval("graphic().getAttribute('data-paused')") == "false"
    # A newly active tab can become idle again, but cannot replay the victory.
    js.eval("advance(45000);")
    assert js.eval("companion.getState()") == "resting"


def test_hidden_initial_mount_has_no_timers_or_delayed_greeting(js) -> None:
    js.eval("document.hidden = true; companion.init(); advance(600000); hide(false);")
    assert js.eval("companion.getState()") == "welcome"
    assert js.eval("gesture()") is None
    js.eval("companion.destroy();")
    assert js.eval("timers.size") == 0


def test_reduced_motion_is_respected_initially_and_when_preference_changes(js) -> None:
    js.eval("motion.matches = true; companion.init();")
    assert js.eval("gesture()") is None
    assert js.eval("speech().textContent") == "Ciao! ✨"
    assert js.eval("timers.size") == 1  # Static speech is still available for four seconds.
    assert js.eval("graphic().getAttribute('data-reduced-motion')") == "true"
    js.eval("companion.setState('working'); reduce(false);")
    assert js.eval("gesture()") is None
    assert js.eval("timers.size") == 1
    js.eval("advance(4000);")
    assert js.eval("speech().hidden") is True
    js.eval("companion.setState('success');")
    assert js.eval("gesture()") is not None
    js.eval("reduce(true); advance(600000);")
    assert js.eval("gesture()") is None
    assert js.eval("timers.size") == 0
    assert js.eval("companion.getState()") == "success"


def test_legacy_media_query_listener_is_removed_on_destroy(js) -> None:
    js.eval("""
      motion.addListener = callback => EventTarget.prototype.addEventListener.call(motion, 'change', callback);
      motion.removeListener = callback => EventTarget.prototype.removeEventListener.call(motion, 'change', callback);
      motion.addEventListener = undefined;
      companion.init(); reduce(true);
    """)
    assert js.eval("timers.size") == 1
    assert js.eval("motion.listeners.length") == 1
    js.eval("companion.destroy();")
    assert js.eval("motion.listeners.length") == 0


@pytest.mark.parametrize(
    "active",
    ["new Node('input')", "new Node('textarea')", "new Node('select')",
     "Object.assign(new Node('div'), {isContentEditable: true})",
     "Object.assign(new Node('span'), {editingAncestor: new Node('div')})"],
)
def test_typing_keeps_explicit_feedback_and_suppresses_inactivity_until_focus_leaves(js, active: str) -> None:
    js.eval(f"document.activeElement = {active}; companion.init();")
    assert js.eval("gesture()") == "greet"
    assert js.eval("speech().textContent") == "Ciao! ✨"
    assert js.eval("timers.size") == 2
    js.eval("companion.setState('success'); document.dispatch('keydown');")
    assert js.eval("gesture()") == "nod"
    assert js.eval("speech().textContent") == "Fatto! ✨"
    js.eval("advance(600000); document.dispatch('keydown');")
    assert js.eval("companion.getState()") == "success"
    assert js.eval("timers.size") == 0
    js.eval("document.activeElement = null; document.dispatch('focusout'); advance(45000);")
    assert js.eval("companion.getState()") == "resting"


def test_focusing_text_during_gesture_keeps_it_until_its_deadline(js) -> None:
    js.eval("companion.init(); document.activeElement = new Node('textarea'); document.dispatch('focusin');")
    assert js.eval("gesture()") == "greet"
    assert js.eval("timers.size") == 2
    js.eval("advance(1400);")
    assert js.eval("gesture()") is None
    assert js.eval("speech().hidden") is False
    js.eval("advance(2600);")
    assert js.eval("timers.size") == 0


def test_idle_cue_is_finite_and_user_activity_rearms_one_deadline(js) -> None:
    js.eval("companion.init({state: 'idle'}); advance(45000);")
    assert js.eval("companion.getState()") == "resting"
    js.eval("advance(600000);")
    assert js.eval("timers.size") == 0
    assert js.eval("gesture()") is None
    js.eval("document.dispatch('pointerdown'); advance(30000); document.dispatch('keydown'); advance(30000);")
    assert js.eval("companion.getState()") == "idle"
    js.eval("advance(15000);")
    assert js.eval("companion.getState()") == "resting"


def test_failure_gesture_is_finite_and_expression_persists_until_explicit_recovery(js) -> None:
    """L'espressione resta finche' non si dichiara il contrario.

    Prima la misura era il tracciato della bocca. La bocca non c'e' piu' -- il
    simbolo del marchio non ne ha una, e disegnarla solo nella mascotte faceva
    due robot diversi -- quindi l'espressione e' ``data-state`` sulla grafica,
    che e' cio' che il CSS legge per inclinare e stringere gli occhi.
    """
    js.eval("companion.init({state: 'working'}); companion.setState('error');")
    assert js.eval("gesture()") == "shake"
    assert js.eval("speech().textContent") == "Ops, riproviamo?"
    js.eval("advance(600000);")
    assert js.eval("companion.getState()") == "error"
    assert js.eval("gesture()") is None
    assert js.eval("timers.size") == 0
    js.eval("document.dispatch('pointerdown');")
    assert js.eval("companion.getState()") == "error"
    assert js.eval("graphic().getAttribute('data-state')") == "error"
    assert js.eval("companion.setState('idle')") is True
    assert js.eval("graphic().getAttribute('data-state')") == "idle"


def test_the_face_has_no_mouth(js) -> None:
    """Fedelta' al simbolo: testa, occhi, antenna. Niente altro.

    Le due copie della mascotte stanno una accanto al logo e una sopra l'invio:
    una bocca presente solo nell'animata rendeva visibile la differenza proprio
    dove le due geometrie si confrontano.
    """
    js.eval("companion.init({state: 'welcome'});")
    assert js.eval("pose().childNodes.length") == 2
    for stato in ("welcome", "working", "success", "idle", "resting", "error"):
        js.eval(f"companion.setState('{stato}');")
        assert js.eval("pose().childNodes.length") == 2, stato


def test_multiple_mounts_share_feedback_timers_and_restore_all_original_content(js) -> None:
    js.eval("""
      otherHost.setAttribute('data-state', 'original-state');
      companion.init({mounts: ['#sidebar .logo', otherHost, '#sidebar .logo']});
      var firstGraphic = graphic(), secondGraphic = otherHost.childNodes[0];
    """)
    assert js.eval("host.childNodes.length === 2 && otherHost.childNodes.length === 2") is True
    assert js.eval("timers.size") == 3  # One gesture, speech and inactivity deadline for both.
    assert js.eval("document.listeners.length + motion.listeners.length") == 6
    js.eval("companion.setState('working');")
    assert js.eval("[firstGraphic, secondGraphic].every(node => node.getAttribute('data-state') === 'working' && node.getAttribute('data-gesture') === 'look')") is True
    assert js.eval("otherHost.childNodes[1].textContent === speech().textContent") is True
    assert js.eval("timers.size") == 2
    js.eval("advance(1400);")
    assert js.eval("[firstGraphic, secondGraphic].every(node => node.getAttribute('data-gesture') === null)") is True
    js.eval("reduce(true); hide(true);")
    assert js.eval("[host, otherHost, firstGraphic, secondGraphic].every(node => node.getAttribute('data-paused') === 'true' && node.getAttribute('data-reduced-motion') === 'true')") is True
    js.eval("companion.destroy(); original.dispatch('click'); advance(600000);")
    assert js.eval("timers.size + document.listeners.length + motion.listeners.length") == 0
    assert js.eval("host.childNodes[0] === original && host.childNodes[1] === caption") is True
    assert js.eval("otherHost.childNodes.length === 1 && otherHost.childNodes[0] === otherOriginal") is True
    assert js.eval("otherHost.getAttribute('data-state')") == "original-state"
    assert js.eval("host.getAttribute('data-speaking')") is None
    assert js.eval("clicks") == 1


def test_speech_expires_after_four_seconds_without_replaying_for_duplicate_state(js) -> None:
    js.eval("companion.init({mounts: [host, otherHost], state: 'working'}); advance(2000);")
    assert js.eval("speech().hidden") is False
    js.eval("companion.setState('working'); advance(1999);")
    assert js.eval("speech().textContent") == "Ci penso io…"
    js.eval("advance(1);")
    assert js.eval("[host, otherHost].every(node => node.childNodes[1].hidden && node.getAttribute('data-speaking') === 'false')") is True
    assert js.eval("companion.getState()") == "working"
    assert js.eval("timers.size") == 0


def test_new_state_replaces_speech_deadline_and_idle_clears_it_immediately(js) -> None:
    js.eval("companion.init({state: 'working'}); advance(3000); companion.setState('success'); advance(1000);")
    assert js.eval("speech().textContent") == "Fatto! ✨"
    assert js.eval("speech().hidden") is False
    js.eval("advance(2999);")
    assert js.eval("speech().hidden") is False
    js.eval("advance(1);")
    assert js.eval("speech().hidden") is True
    js.eval("companion.setState('working'); companion.setState('idle');")
    assert js.eval("speech().hidden") is True
    assert js.eval("speech().textContent") == ""
    assert js.eval("host.getAttribute('data-speaking')") == "false"


def _load_functions(context: quickjs.Context, relative_path: str, *names: str) -> None:
    """Load complete top-level functions from the real UI source, including async ones."""
    source = (SOURCE.parents[1] / relative_path).read_text(encoding="utf-8")
    for name in names:
        found = re.search(
            rf"^(?:async )?function {re.escape(name)}\([^\n]*\) \{{.*?^\}}",
            source,
            re.MULTILINE | re.DOTALL,
        )
        assert found, f"Cannot find {name} in {relative_path}"
        context.eval(found.group())


@pytest.fixture
def desktop(js: quickjs.Context) -> quickjs.Context:
    """Use the real controller and stream handlers, stubbing unrelated renderers."""
    js.eval(r"""
      var nodes = new Map();
      function $(selector) {
        if (!nodes.has(selector)) nodes.set(selector, new Node('div'));
        return nodes.get(selector);
      }
      document.body = new Node('body');
      var state = {sessionId: 'visible', running: new Set(), busy: false,
        settings: {}, attachToken: 0, attachAbort: null};
      var companionCalls = [];
      window.HarnessCompanion = {
        getState: companion.getState,
        setState(next) { companionCalls.push(next); return companion.setState(next); },
      };
      var uiStatus = '';
      var usage = null;
      var attached = [];
      function renderSessions() {}
      function renderUsage(next) { usage = next; }
      function renderPlan() {}
      function renderNotes() {}
      function renderPreview() {}
      function renderHistory() {}
      function allineaProgettoDellaChat() {}
      function resetUsage() {}
      function applyStats() {}
      function attachStream(id) { attached.push(id); }
      function esc(text) { return String(text); }
      function el(tag, className, text) {
        const node = new Node(tag);
        node.className = className;
        node.textContent = text;
        return node;
      }
      function makeTestTurn() {
        return {appended: [], files: 0, statusNode: null,
          append(node) { this.appended.push(node); },
          showFiles() { this.files += 1; }};
      }
      var turn = makeTestTurn();
      function deliver(event) {
        handleEvent(event, turn, null, message => { uiStatus = message; });
      }
      companion.init({state: 'idle'});
    """)
    _load_functions(js, "web/app.js", "setRunning", "handleEvent", "showSession", "leggiLoStream")
    return js


@pytest.mark.parametrize(
    ("reason", "steps", "expected"),
    [("completed", 4, "success"), ("completed", 1, "idle"),
     ("stopped", 7, "idle"), ("max_steps", 7, "idle"),
     ("awaiting_user", 7, "idle"), ("error", 7, "error")],
)
def test_stream_termination_maps_to_truthful_feedback(desktop, reason, steps, expected) -> None:
    desktop.eval("setRunning('visible', true);")
    desktop.eval(f"deliver({json.dumps({'type': 'done', 'reason': reason, 'steps': steps})});")
    assert desktop.eval("companion.getState()") == expected
    desktop.eval("setRunning('visible', false);")
    assert desktop.eval("companion.getState()") == expected
    assert desktop.eval("turn.files") == (0 if reason == "awaiting_user" else 1)


def test_error_event_blocks_later_completion_celebration(desktop) -> None:
    desktop.eval("""
      setRunning('visible', true);
      deliver({type: 'error', message: 'Il modello non risponde'});
      deliver({type: 'done', reason: 'completed', steps: 4});
      setRunning('visible', false);
    """)
    assert desktop.eval("companion.getState()") == "error"
    assert desktop.eval("companionCalls.includes('success')") is False
    assert desktop.eval("turn.appended[0].className") == "error-box"


def test_reconnection_notice_does_not_mark_a_recovered_turn_as_failed(desktop) -> None:
    desktop.eval("""
      setRunning('visible', true);
      deliver({type: 'error', message: 'Connessione interrotta: riprendo da dove eravamo.'});
    """)
    assert desktop.eval("companion.getState()") == "working"
    assert desktop.eval("Boolean(turn.companionFailed)") is False
    assert desktop.eval("turn.appended[0].className") == "notice"
    desktop.eval("deliver({type: 'done', reason: 'completed', steps: 4});")
    assert desktop.eval("companion.getState()") == "success"


def test_repeated_done_frame_does_not_replay_completion(desktop) -> None:
    desktop.eval("""
      setRunning('visible', true);
      deliver({type: 'done', reason: 'completed', steps: 4});
      advance(700);
      deliver({type: 'done', reason: 'completed', steps: 4});
      advance(700);
    """)
    assert desktop.eval("companionCalls.filter(state => state === 'success').length") == 1
    assert desktop.eval("gesture()") is None


def test_repeated_busy_updates_keep_working_without_replaying_or_clearing_it(desktop) -> None:
    desktop.eval("setRunning('visible', true); advance(700); setRunning('visible', true);")
    assert desktop.eval("companion.getState()") == "working"
    desktop.eval("advance(700);")
    assert desktop.eval("gesture()") is None
    assert desktop.eval("companionCalls.filter(state => state === 'working').length") == 1


def test_busy_refresh_after_failure_does_not_erase_failure(desktop) -> None:
    desktop.eval("""
      setRunning('visible', true);
      deliver({type: 'error', message: 'Errore definitivo'});
      setRunning('visible', true);
    """)
    assert desktop.eval("companion.getState()") == "error"
    desktop.eval("setRunning('visible', false); setRunning('visible', true);")
    assert desktop.eval("companion.getState()") == "working"


@pytest.mark.parametrize("settled", ["success", "error"])
def test_other_conversation_activity_does_not_overwrite_visible_outcome(desktop, settled) -> None:
    desktop.eval(f"companion.setState({json.dumps(settled)}); setRunning('background', true);")
    assert desktop.eval("state.busy") is False
    assert desktop.eval("companion.getState()") == settled
    desktop.eval("setRunning('background', false);")
    assert desktop.eval("companion.getState()") == settled


@pytest.mark.parametrize("settled", ["success", "error"])
def test_opening_another_conversation_clears_stale_outcome(desktop, settled) -> None:
    desktop.eval(f"companion.setState({json.dumps(settled)});")
    desktop.eval("showSession({session_id: 'different', running: false, messages: []});")
    assert desktop.eval("state.sessionId") == "different"
    assert desktop.eval("companion.getState()") == "idle"
    assert desktop.eval("speech().hidden") is True


@pytest.mark.parametrize("settled", ["success", "error"])
def test_refresh_of_same_conversation_preserves_outcome(desktop, settled) -> None:
    desktop.eval(f"companion.setState({json.dumps(settled)});")
    desktop.eval("showSession({session_id: 'visible', running: false, messages: []});")
    assert desktop.eval("companion.getState()") == settled


@pytest.mark.parametrize("event_type", ["error", "done"])
def test_abandoned_stream_cannot_change_current_conversation_feedback(desktop, event_type) -> None:
    frame = "data: " + json.dumps(
        {"type": event_type, "message": "Old failure", "reason": "completed", "steps": 5}
    ) + "\n\n"
    desktop.eval(f"var oldFrame = {json.dumps(frame)};")
    desktop.eval("""
      var TextDecoder = class { decode(value) { return value; } };
      var cancelled = 0, released = 0, read = 0, result = null, streamError = null;
      var response = {body: {getReader() { return {
        async read() { return ++read === 1 ? {value: oldFrame, done: false} : {done: true}; },
        async cancel() { cancelled += 1; },
        releaseLock() { released += 1; },
      }; }}};
      leggiLoStream(response, turn, () => false)
        .then(value => { result = value; }, error => { streamError = String(error); });
    """)
    for _ in range(20):
        if not desktop.execute_pending_job():
            break
    assert desktop.eval("streamError") is None
    assert desktop.eval("result.tipo") == "estraneo"
    assert desktop.eval("companionCalls.length") == 0
    assert desktop.eval("cancelled === 1 && released === 1") is True


@pytest.fixture
def mobile(js: quickjs.Context) -> quickjs.Context:
    js.eval("""
      var sessionList = new Node('ul');
      function $(id) { if (id === 'session-list') return sessionList; return null; }
      document.createElement = tag => new Node(tag);
      function fmtDate(value) { return value; }
      var opened = [];
      function openChat(id) { opened.push(id); }
    """)
    # La riga sta in ``rigaChat``: la stessa per le libere e per le chat dei
    # progetti, raggruppate sopra.
    _load_functions(js, "web_mobile/app.js", "rigaChat", "renderSessions")
    js.eval("renderSessions([{id: 'first', title: 'Primo'}, {id: 'second', title: 'Secondo'}]);")
    return js


@pytest.mark.parametrize("key", ["Enter", " "])
def test_mobile_session_keyboard_activation_prevents_default_and_opens_correct_chat(mobile, key) -> None:
    mobile.eval(f"var activationKey = {json.dumps(key)};")
    mobile.eval("""
      var prevented = 0;
      sessionList.childNodes[1].dispatch('keydown', {
        key: activationKey, preventDefault() { prevented += 1; },
      });
    """)
    assert mobile.eval("sessionList.childNodes.every(node => node.tabIndex === 0)") is True
    assert mobile.eval("sessionList.childNodes[1].getAttribute('role')") == "button"
    assert mobile.eval("JSON.stringify(opened)") == '["second"]'
    assert mobile.eval("prevented") == 1


@pytest.mark.parametrize("key", ["Tab", "ArrowDown", "Escape", "a"])
def test_other_mobile_keys_preserve_browser_behavior_and_do_not_open_chat(mobile, key) -> None:
    mobile.eval(f"var navigationKey = {json.dumps(key)};")
    mobile.eval("""
      var prevented = 0;
      sessionList.childNodes[0].dispatch('keydown', {
        key: navigationKey, preventDefault() { prevented += 1; },
      });
    """)
    assert mobile.eval("opened.length") == 0
    assert mobile.eval("prevented") == 0


def test_mobile_pointer_activation_and_rerender_keep_correct_session_binding(mobile) -> None:
    mobile.eval("""
      sessionList.childNodes[0].dispatch('click');
      renderSessions([{id: 'replacement', title: 'Nuova lista'}]);
      sessionList.childNodes[0].dispatch('click');
    """)
    assert mobile.eval("sessionList.childNodes.length") == 1
    assert mobile.eval("JSON.stringify(opened)") == '["first","replacement"]'
