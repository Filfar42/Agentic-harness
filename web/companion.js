/**
 * A finite-state companion shared by the brand and active chat composer.
 *
 * HarnessCompanion.init({mounts: ['#sidebar .logo', '#composer-companion']});
 * HarnessCompanion.setState('working');
 * HarnessCompanion.setState('success'); // Only after a completed operation.
 * HarnessCompanion.destroy();
 *
 * Hosts share one controller and one set of timers. Their original nodes and
 * attributes are restored by destroy(), including existing node listeners.
 */
(function installCompanion(global) {
  'use strict';

  const SVG_NS = 'http://www.w3.org/2000/svg';
  const STATES = new Set(['welcome', 'working', 'success', 'idle', 'resting', 'error']);
  const GESTURE_MS = 1400;
  const SPEECH_MS = 4000;
  const REST_AFTER_MS = 45_000;
  const DEFAULT_MOUNT = '#sidebar .logo';
  const HOST_ATTRIBUTES = ['data-state', 'data-speaking', 'data-paused', 'data-reduced-motion'];
  const CUES = {welcome: 'greet', working: 'look', success: 'nod', resting: 'blink', error: 'shake'};
  const SPEECH = {
    welcome: 'Ciao! ✨', working: 'Ci penso io…', success: 'Fatto! ✨',
    resting: 'Sono qui!', error: 'Ops, riproviamo?', idle: '',
  };
  let mounts = [];
  let currentState = 'idle';
  let gestureTimer = null;
  let speechTimer = null;
  let idleTimer = null;
  let motionPreference = null;
  let reducedMotion = false;
  let listeners = [];

  /** Create SVG nodes from constant geometry; no HTML strings are interpreted. */
  function svgNode(tag, attributes = {}) {
    const node = document.createElementNS(SVG_NS, tag);
    Object.entries(attributes).forEach(([name, value]) => node.setAttribute(name, String(value)));
    return node;
  }

  function makeGraphic() {
    const svg = svgNode('svg', {
      class: 'harness-companion', viewBox: '0 0 32 32',
      width: 28, height: 28, 'aria-hidden': 'true', focusable: 'false',
    });
    const pose = svgNode('g', {class: 'harness-companion__pose'});
    const face = svgNode('g', {class: 'harness-companion__face', fill: 'currentColor'});
    face.appendChild(svgNode('path', {
      d: 'M11 8H21C25.418 8 29 11.582 29 16V21C29 25.418 25.418 29 21 29H11C6.582 29 3 25.418 3 21V16C3 11.582 6.582 8 11 8Z',
    }));
    face.appendChild(svgNode('rect', {x: 15, y: 3, width: 2, height: 5, rx: 1}));
    face.appendChild(svgNode('circle', {cx: 16, cy: 3, r: 2}));
    const eyes = svgNode('g', {class: 'harness-companion__eyes'});
    eyes.appendChild(svgNode('rect', {x: 9, y: 15, width: 4, height: 6, rx: 2}));
    eyes.appendChild(svgNode('rect', {x: 19, y: 15, width: 4, height: 6, rx: 2}));
    // Niente bocca: il simbolo del marchio ha testa, occhi e antenna, e basta.
    // Una bocca disegnata solo nella versione animata faceva due robot diversi
    // -- quello statico degli asset e quello della chat -- e la differenza si
    // vedeva proprio dove le due copie stanno vicine, nello slot del logo.
    // L'espressione la portano gli occhi, che nel marchio ci sono gia'.
    pose.appendChild(face);
    pose.appendChild(eyes);
    svg.appendChild(pose);
    return {graphic: svg};
  }

  function clearGesture() {
    if (gestureTimer !== null) global.clearTimeout(gestureTimer);
    gestureTimer = null;
    mounts.forEach(({graphic}) => graphic.removeAttribute('data-gesture'));
  }

  function clearSpeech() {
    if (speechTimer !== null) global.clearTimeout(speechTimer);
    speechTimer = null;
    mounts.forEach(({host, speech}) => {
      host.setAttribute('data-speaking', 'false');
      speech.hidden = true;
      speech.textContent = '';
    });
  }

  function showSpeech(state) {
    clearSpeech();
    if (document.hidden || !SPEECH[state]) return;
    mounts.forEach(({host, speech}) => {
      speech.textContent = SPEECH[state];
      speech.hidden = false;
      host.setAttribute('data-speaking', 'true');
    });
    speechTimer = global.setTimeout(clearSpeech, SPEECH_MS);
  }

  function clearIdleTimer() {
    if (idleTimer !== null) global.clearTimeout(idleTimer);
    idleTimer = null;
  }

  function mayMove() {
    return mounts.length > 0 && !document.hidden && !reducedMotion;
  }

  function editingText() {
    const active = document.activeElement;
    if (!active) return false;
    if (active.isContentEditable) return true;
    return Boolean(active.closest?.('input, textarea, select, [role="textbox"], [contenteditable]:not([contenteditable="false"])'));
  }

  /** A state transition has one clear gesture, even while the composer has focus. */
  function gesture(name) {
    clearGesture();
    if (!mayMove()) return;
    mounts.forEach(({graphic}) => graphic.setAttribute('data-gesture', name));
    gestureTimer = global.setTimeout(clearGesture, GESTURE_MS);
  }

  /** One inactivity deadline, never an interval. Editing suppresses idle cues. */
  function armIdleTimer() {
    clearIdleTimer();
    if (!mayMove() || editingText() || !['welcome', 'success', 'idle'].includes(currentState)) return;
    idleTimer = global.setTimeout(() => {
      idleTimer = null;
      if (mayMove() && !editingText() && ['welcome', 'success', 'idle'].includes(currentState)) {
        setState('resting');
      }
    }, REST_AFTER_MS);
  }

  /**
   * Set the visual state. Repeated events of the same kind do not replay cues.
   * Invalid states are rejected. Stop/cancel should map to idle, failures to error.
   * @param {'welcome'|'working'|'success'|'idle'|'resting'|'error'} next
   * @returns {boolean}
   */
  function setState(next) {
    if (!mounts.length || !STATES.has(next)) return false;
    if (next === currentState) return true;
    clearGesture();
    clearIdleTimer();
    currentState = next;
    mounts.forEach(({host, graphic}) => {
      host.setAttribute('data-state', next);
      graphic.setAttribute('data-state', next);
    });
    showSpeech(next);
    if (CUES[next]) gesture(CUES[next]);
    armIdleTimer();
    return true;
  }

  function onActivity() {
    if (!mounts.length) return;
    if (currentState === 'resting') setState('idle');
    armIdleTimer();
  }

  function onVisibilityChange() {
    clearGesture();
    clearIdleTimer();
    clearSpeech();
    mounts.forEach(({host, graphic}) => {
      host.setAttribute('data-paused', document.hidden ? 'true' : 'false');
      graphic.setAttribute('data-paused', document.hidden ? 'true' : 'false');
    });
    // Becoming visible never replays a completion, bubble or working gesture.
    if (!document.hidden) armIdleTimer();
  }

  function onMotionChange() {
    reducedMotion = Boolean(motionPreference?.matches);
    mounts.forEach(({host, graphic}) => {
      host.setAttribute('data-reduced-motion', reducedMotion ? 'true' : 'false');
      graphic.setAttribute('data-reduced-motion', reducedMotion ? 'true' : 'false');
    });
    clearGesture();
    armIdleTimer();
  }

  function listen(target, type, callback, options = false) {
    target.addEventListener(type, callback, options);
    listeners.push(() => target.removeEventListener(type, callback, options));
  }

  /** Restore every host and release every timer and listener. Idempotent. */
  function destroy() {
    clearGesture();
    clearSpeech();
    clearIdleTimer();
    listeners.forEach((remove) => remove());
    listeners = [];
    mounts.forEach(({host, graphic, speech, previousNodes, hadHostClass, previousAttributes}) => {
      graphic.remove();
      speech.remove();
      previousNodes.forEach((node) => host.appendChild(node));
      if (!hadHostClass) host.classList.remove('harness-companion-host');
      previousAttributes.forEach(([name, value]) => {
        if (value === null) host.removeAttribute(name);
        else host.setAttribute(name, value);
      });
    });
    mounts = [];
    motionPreference = null;
    reducedMotion = false;
    currentState = 'idle';
  }

  /**
   * Mount in existing slots; never creates an overlay or dock. The legacy single
   * mount option remains supported. Invalid options preserve every active host.
   * @param {{mount?: string|Element, mounts?: Array<string|Element>, state?: string}} [options]
   * @returns {boolean}
   */
  function init({mount = DEFAULT_MOUNT, mounts: requestedMounts, state = 'welcome'} = {}) {
    if (!STATES.has(state)) return false;
    const requested = requestedMounts === undefined ? [mount] : requestedMounts;
    if (!Array.isArray(requested) || !requested.length) return false;
    let targets;
    try {
      targets = requested.map((target) => typeof target === 'string' ? document.querySelector(target) : target);
    } catch (_) {
      return false;
    }
    if (targets.some((target) => !target || target.nodeType !== 1 || typeof target.appendChild !== 'function')) return false;
    targets = [...new Set(targets)];
    destroy();
    mounts = targets.map((host) => {
      const previousNodes = [...host.childNodes];
      const previousAttributes = HOST_ATTRIBUTES.map((name) => [name, host.getAttribute(name)]);
      const hadHostClass = host.classList.contains('harness-companion-host');
      previousNodes.forEach((node) => node.remove());
      host.classList.add('harness-companion-host');
      const {graphic} = makeGraphic();
      const speech = document.createElement('span');
      speech.setAttribute('class', 'harness-companion__speech');
      speech.setAttribute('aria-hidden', 'true');
      speech.hidden = true;
      host.appendChild(graphic);
      host.appendChild(speech);
      return {host, graphic, speech, previousNodes, hadHostClass, previousAttributes};
    });
    motionPreference = typeof global.matchMedia === 'function'
      ? global.matchMedia('(prefers-reduced-motion: reduce)') : null;
    if (motionPreference?.addEventListener) {
      listen(motionPreference, 'change', onMotionChange);
    } else if (motionPreference?.addListener) {
      motionPreference.addListener(onMotionChange);
      listeners.push(() => motionPreference?.removeListener(onMotionChange));
    }
    listen(document, 'visibilitychange', onVisibilityChange);
    listen(document, 'pointerdown', onActivity, {passive: true, capture: true});
    listen(document, 'keydown', onActivity, true);
    listen(document, 'focusin', onActivity, true);
    listen(document, 'focusout', onActivity, true);
    // The sentinel lets the requested initial state render even when it is idle.
    currentState = null;
    onMotionChange();
    mounts.forEach(({host, graphic}) => {
      host.setAttribute('data-paused', document.hidden ? 'true' : 'false');
      graphic.setAttribute('data-paused', document.hidden ? 'true' : 'false');
    });
    setState(state);
    return true;
  }

  global.HarnessCompanion = Object.freeze({
    init, setState, destroy, getState: () => currentState,
  });
})(window);
