'use strict';
/* DebbieDavidApp renderer - a DUMB view over the Python bridge.
   It holds NO engine logic: it connects to the bridge WebSocket, renders the current `state`,
   and sends `cmd`s on user actions (see ../PROTOCOL.md). Structure is rebuilt only when the
   "view key" changes (step / overlay); per-frame it just patches waveforms, meters, transcript. */

// ---- tiny DOM helper ------------------------------------------------------------------------
function el(tag, attrs, children) {
  const node = document.createElement(tag);
  if (attrs) for (const k in attrs) {
    const v = attrs[k];
    if (v == null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else if (k === 'value') node.value = v;
    else node.setAttribute(k, v);
  }
  if (children != null) (Array.isArray(children) ? children : [children]).forEach((c) => {
    if (c == null || c === false) return;
    node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  });
  return node;
}
const $ = (id) => document.getElementById(id);
const LANG_NAME = { en: 'English (UK)', de: 'Deutsch' };
const HERO_SRC = 'assets/hero-v3.png';   // splash hero; switch to hero-v1.png for the duotone cut
const fmtTime = (ms) => {
  const s = Math.max(0, Math.floor(ms / 1000));
  return String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');
};

// ---- connection -----------------------------------------------------------------------------
const WS_URL = (window.DD && window.DD.wsUrl) ||
  ('ws://127.0.0.1:' + (new URLSearchParams(location.search).get('port') || '8765'));
let ws = null;
let hello = { inputDevices: [], outputDevices: [] };
let state = null;
let viewKey = '';
let connected = false;

function connect() {
  ws = new WebSocket(WS_URL);
  ws.onopen = () => { connected = true; };
  ws.onclose = () => { connected = false; setTimeout(connect, 800); };
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === 'hello') { hello = msg; $('winTitle').textContent = msg.appName || 'DebbieDavidApp'; }
    else if (msg.type === 'state') { state = msg; applyState(); }
  };
}
function send(cmd, extra) {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify(Object.assign({ cmd }, extra || {})));
}

// ---- view-key (when to rebuild structure) ---------------------------------------------------
function computeOverlay(s) {
  if (s.wizardStep === 'inputs' && !s.input.found) return 'notfound-input';
  if (s.wizardStep === 'outputs' && !s.output.found) return 'notfound-output';
  if (s.wizardStep === 'channel' && s.channelTest.crossed) return 'crossed';
  return '';
}

function viewSignature(s, overlay) {
  // Anything that changes STRUCTURE (not just dynamic values) must be in the key so the screen
  // rebuilds: step, overlay, channel-test phase/confirmations, and the resolved device identity.
  const ct = s.channelTest;
  const ctSig = `${ct.active ? 1 : 0}.${ct.side || '-'}.${ct.confirmed.left ? 1 : 0}${ct.confirmed.right ? 1 : 0}`;
  const devSig = `${s.input.found ? 1 : 0}${s.input.deviceIndex}.${s.output.found ? 1 : 0}${s.output.deviceIndex}`;
  const langSig = `${s.sides.left.lang}${s.sides.right.lang}`;
  const listSig = `${(s.inputDevices || []).length}.${(s.outputDevices || []).length}`;
  const guardSig = (s.guardian && s.guardian.enabled === false) ? '0' : '1';
  return `${s.wizardStep}|${overlay}|${ctSig}|${devSig}|${langSig}|${listSig}|${guardSig}`;
}

function applyState() {
  const s = state;
  const overlay = computeOverlay(s);
  const key = viewSignature(s, overlay);
  const title = { splash: '', inputs: ' — Setup', outputs: ' — Setup', channel: ' — Setup', live: ' — Live' }[s.wizardStep] || '';
  $('winTitle').textContent = (hello.appName || 'DebbieDavidApp') + title;
  if (key !== viewKey) {
    viewKey = key;
    lastTranscriptSig = '';        // force the transcript to repaint into the fresh DOM
    const body = $('body');
    body.innerHTML = '';
    body.appendChild(buildScreen(s, overlay));
    body.scrollTop = 0;
  }
  patch(s);
  renderToast(s);
}

// ---- builders -------------------------------------------------------------------------------
function buildScreen(s, overlay) {
  if (s.wizardStep === 'splash') return buildSplash();
  if (s.wizardStep === 'live') return buildLive(s);
  return buildSetup(s, overlay);
}

function makeWave(id, klass) {
  const w = el('div', { class: 'wave ' + (klass || ''), id });
  for (let i = 0; i < 48; i++) w.appendChild(el('div', { class: 'wave__bar' }));
  return w;
}

function buildSplash() {
  return el('div', { class: 'splash' }, [
    el('div', { class: 'splash__left' }, [
      el('div', { class: 'splash__center' }, [
        el('h1', {}, [
          "David's", el('br'),
          el('span', { class: 'hl-yellow' }, 'Debbie'), el('br'), 'App',
        ]),
        el('div', { class: 'splash__sub' },
          'Clip on a mic, wear one earbud each. Speak your own language and hear theirs in your ear.'),
        el('div', { class: 'splash__cta' }, [
          el('a', { class: 'rz-btn', onclick: () => send('gotoStep', { step: 'inputs' }) }, 'Configure →'),
        ]),
      ]),
      el('div', { class: 'splash__foot' }, 'Powered by Gemini 3.5 Live Transcribe'),
    ]),
    el('div', { class: 'splash__hero' }, [
      el('div', { class: 'wash' }),
      el('div', { class: 'hero-card' }, [
        el('div', { class: 'hero-card__shadow' }),
        el('img', { class: 'hero-card__img', src: HERO_SRC, alt: 'David and Debbie' }),
      ]),
    ]),
  ]);
}

const STEPS = [
  { key: 'inputs', n: '01', label: 'Inputs' },
  { key: 'outputs', n: '02', label: 'Outputs' },
  { key: 'channel', n: '03', label: 'Channel test' },
];

function stepper(active) {
  const ai = STEPS.findIndex((x) => x.key === active);
  const home = el('button', { class: 'step-pill step-pill--home',
    onclick: () => send('gotoStep', { step: 'splash' }) }, '‹ Home');
  const pills = STEPS.map((st, i) => {
    let cls = 'step-pill';
    if (i === ai) cls += ' step-pill--active';
    else if (i < ai) cls += ' step-pill--done';
    return el('button', { class: cls, onclick: () => send('gotoStep', { step: st.key }) },
      i < ai ? st.n + ' ✓' : st.n + ' ' + st.label);
  });
  return el('div', { class: 'stepper' }, [home, ...pills]);
}

function summaryRow(st, text) {
  return el('div', { class: 'row-collapsed', onclick: () => send('gotoStep', { step: st.key }) }, [
    el('span', { class: 'row-collapsed__check' }, '✓'),
    el('div', { class: 'spacer' }, [
      el('div', { class: 'mono', style: 'font-weight:700;font-size:13px;letter-spacing:0.14em;text-transform:uppercase;color:var(--rz-paper)' }, st.n + ' · ' + st.label),
      el('div', { style: 'font-size:14px;color:var(--rz-paper-70);margin-top:3px' }, text),
    ]),
    el('a', { class: 'rz-btn rz-btn--ghost rz-btn--sm' }, 'Edit'),
  ]);
}

function pendingRow(st, sub) {
  return el('div', { class: 'row-collapsed row-collapsed--pending' }, [
    el('span', { class: 'row-collapsed__num' }, st.n),
    el('div', { class: 'spacer' }, [
      el('div', { class: 'mono', style: 'font-weight:700;font-size:14px;letter-spacing:0.14em;text-transform:uppercase;color:var(--rz-paper)' }, st.label),
      el('div', { style: 'font-size:14px;color:var(--rz-paper-70);margin-top:2px' }, sub),
    ]),
    el('span', { class: 'mono', style: 'font-size:20px;color:var(--rz-paper-50)' }, '›'),
  ]);
}

function buildSetup(s, overlay) {
  const ai = STEPS.findIndex((x) => x.key === s.wizardStep);
  const head = el('div', { class: 'setup__head' }, [
    el('div', {}, [el('div', { class: 'eyebrow' }, 'Setup'),
      el('h2', {}, 'Microphone check')]),
    stepper(s.wizardStep),
  ]);
  const rows = el('div', { class: 'row-gap' });
  STEPS.forEach((st, i) => {
    if (i < ai) rows.appendChild(summaryRow(st, summaryText(s, st.key)));
    else if (i === ai) rows.appendChild(activeRow(s, st.key, overlay));
    else rows.appendChild(pendingRow(st, pendingSub(st.key)));
  });
  return el('div', { class: 'setup' }, [head, rows]);
}

function summaryText(s, key) {
  if (key === 'inputs') return `${s.sides.left.name} (left) · ${s.sides.right.name} (right) — ${s.input.deviceName} · gain ${Math.round(s.gain * 100)}%`;
  if (key === 'outputs') return `${s.sides.left.name} hears ${LANG_NAME[s.sides.left.lang]} · ${s.sides.right.name} hears ${LANG_NAME[s.sides.right.lang]} · ${s.output.deviceName}`;
  return 'Channel test done';
}
function pendingSub(key) {
  return key === 'outputs' ? 'The two earbuds' : key === 'channel' ? 'Confirm each person is on the right side' : '';
}

function activeRow(s, key, overlay) {
  if (key === 'inputs') return overlay === 'notfound-input' ? notFoundRow(s, 'input') : inputsRow(s);
  if (key === 'outputs') return overlay === 'notfound-output' ? notFoundRow(s, 'output') : outputsRow(s);
  return overlay === 'crossed' ? crossedRow(s) : channelRow(s);
}

// ---- ROW 1: inputs --------------------------------------------------------------------------
function micBox(s, side) {
  const isLeft = side === 'left';
  const sd = s.sides[side];
  return el('div', { class: `io io--${side}` }, [
    el('div', { class: 'io__head' }, [
      el('span', { class: `io__label io__label--${side}` }, isLeft ? 'Left input' : 'Right input'),
      el('span', { class: 'io__tx' }, [el('span', { class: 'dot', id: `tx-${side}` }), isLeft ? 'Transmitter 1' : 'Transmitter 2']),
    ]),
    el('div', { class: 'field' }, [
      el('div', { class: 'field__label' }, "Who's on this mic"),
      el('div', { class: 'input-box' }, [
        el('input', { class: 'name-input', value: sd.name, maxlength: 24,
          oninput: (e) => send('setName', { side, name: e.target.value }) }),
      ]),
    ]),
    el('div', { class: 'field' }, [
      el('div', { class: 'field__label' }, 'Microphone'),
      deviceSelect(s, 'input'),
    ]),
    makeWave(`wave-${side}`, isLeft ? '' : 'wave--pink'),
    el('div', { class: 'mono', style: 'font-size:11px;letter-spacing:0.12em;text-transform:uppercase;color:var(--rz-paper-50);margin-top:10px' },
      'Speak - your level shows live'),
  ]);
}

function deviceSelect(s, kind) {
  const devs = (kind === 'input' ? s.inputDevices : s.outputDevices) || [];
  const cur = kind === 'input' ? s.input.deviceIndex : s.output.deviceIndex;
  const opts = devs.map((d) => el('option', { value: d.index, selected: d.index === cur }, d.name));
  if (cur == null) opts.unshift(el('option', { value: '', selected: true, disabled: true }, 'Choose a device…'));
  const sel = el('select', { class: 'sel', onchange: (e) => e.target.value !== '' && send('selectDevice', { kind, index: parseInt(e.target.value, 10) }) }, opts);
  return el('div', { class: 'input-box' }, [sel, el('span', { class: 'caret' }, '▾')]);
}

function inputsRow(s) {
  return el('div', { class: 'row' }, [
    el('div', { class: 'row__head' }, [
      el('span', { class: 'row__num' }, '01'),
      el('div', { class: 'spacer' }, [el('div', { class: 'eyebrow eyebrow--yellow' }, 'Step 1 of 3 · Active'),
        el('div', { class: 'row__title' }, 'The two microphones')]),
      el('div', { class: 'row__aside', html: 'DJI Mic 3<br>Stereo · 2 channels' }),
    ]),
    el('div', { class: 'iogrid' }, [micBox(s, 'left'), micBox(s, 'right')]),
    el('div', { class: 'gain' }, [
      el('span', { class: 'gain__label' }, 'Input gain'),
      el('input', { type: 'range', min: 0, max: 100, value: Math.round(s.gain * 100),
        oninput: (e) => send('setGain', { value: parseInt(e.target.value, 10) / 100 }) }),
      el('span', { class: 'gain__val', id: 'gainVal' }, Math.round(s.gain * 100) + '%'),
      el('span', { class: 'muted', style: 'font-size:13px' }, 'applies to both mics'),
    ]),
    el('div', { class: 'row__foot' }, [
      el('span', { style: 'font-size:14px;color:var(--rz-paper-70)' }, 'Names are optional — leave them Left / Right if you like.'),
      el('a', { class: 'rz-btn', onclick: () => send('gotoStep', { step: 'outputs' }) }, 'Continue to earbuds →'),
    ]),
  ]);
}

// ---- ROW 2: outputs -------------------------------------------------------------------------
function earBox(s, side) {
  const isLeft = side === 'left';
  const sd = s.sides[side];
  return el('div', { class: `io io--${side}` }, [
    el('div', { class: 'io__head' }, [
      el('span', { class: `io__label io__label--${side}` }, `${isLeft ? 'Left' : 'Right'} ear · ${sd.name}`),
      el('span', { class: 'io__tx' }, isLeft ? 'Bud · L' : 'Bud · R'),
    ]),
    el('div', { class: 'field' }, [
      el('div', { class: 'field__label field__label--accent' }, `${sd.name} hears`),
      el('div', { class: isLeft ? 'input-box input-box--hot' : 'input-box input-box--hot-pink' }, [
        el('select', { class: 'sel', onchange: (e) => send('setLanguage', { side, lang: e.target.value }) }, [
          el('option', { value: 'en', selected: sd.lang === 'en' }, 'English (UK)'),
          el('option', { value: 'de', selected: sd.lang === 'de' }, 'Deutsch'),
        ]),
        el('span', { class: 'caret', style: isLeft ? 'color:var(--rz-yellow)' : 'color:var(--rz-pink)' }, '▾'),
      ]),
      el('div', { class: 'muted', style: 'font-size:12.5px;margin-top:7px' }, 'the language played into this bud'),
    ]),
    el('div', { class: 'field' }, [
      el('div', { class: 'field__label' }, 'Output device'),
      deviceSelect(s, 'output'),
    ]),
    el('div', { class: 'field__label' }, 'Output level'),
    el('div', { class: 'meter' }, [el('div', { class: isLeft ? 'meter__fill' : 'meter__fill meter__fill--pink', id: `meter-${side}` })]),
    el('a', { class: 'rz-btn rz-btn--ghost rz-btn--sm', onclick: () => send('testEar', { side }) }, `Test ${isLeft ? 'left' : 'right'} ear`),
  ]);
}

function outputsRow(s) {
  return el('div', { class: 'row' }, [
    el('div', { class: 'row__head' }, [
      el('span', { class: 'row__num' }, '02'),
      el('div', { class: 'spacer' }, [el('div', { class: 'eyebrow eyebrow--yellow' }, 'Step 2 of 3 · Active'),
        el('div', { class: 'row__title' }, 'The two earbuds')]),
      el('div', { class: 'row__aside', html: (s.output.deviceName || 'Output') + '<br>Stereo · L / R' }),
    ]),
    el('div', { style: 'font-size:15px;color:var(--rz-paper-85);margin-bottom:22px;max-width:70ch' }, [
      'Pick the language each ear should ', el('span', { class: 'hl-pink' }, 'hear'), '.',
    ]),
    el('div', { class: 'iogrid' }, [earBox(s, 'left'), earBox(s, 'right')]),
    protectToggle(s),
    el('div', { class: 'row__foot' }, [
      el('span', {}),
      el('a', { class: 'rz-btn', onclick: () => send('gotoStep', { step: 'channel' }) }, 'Continue →'),
    ]),
  ]);
}

// The default-ON "keep earbuds in stereo while in use" guard toggle. When on, the bridge silently
// re-points the system default mic back to the DJI so other apps can't collapse the buds to mono.
function protectToggle(s) {
  const g = s.guardian || { enabled: true };
  const on = g.enabled !== false;
  return el('div', { class: 'protect' }, [
    el('div', { class: 'protect__text' }, [
      el('div', { class: 'protect__title' }, 'Keep earbuds in stereo while in use'),
      el('div', { class: 'protect__sub' },
        'Stops other apps switching the earbuds to mono call mode. Recommended.'),
    ]),
    el('button', {
      class: 'switch' + (on ? ' switch--on' : ''), role: 'switch', 'aria-checked': on ? 'true' : 'false',
      onclick: () => send('setGuardEnabled', { enabled: !on }),
    }, el('span', { class: 'switch__knob' })),
  ]);
}

// ---- ROW 3: channel test --------------------------------------------------------------------
function channelTestCard(s, side) {
  const ct = s.channelTest;
  const isActive = ct.active && ct.side === side;
  const sd = s.sides[side];
  const isLeft = side === 'left';
  if (!isActive && !ct.confirmed[side]) {
    // waiting / not yet
    return el('div', { class: 'ct ct--waiting' }, [
      el('div', { class: 'io__head' }, [
        el('span', { class: `io__label io__label--${side}` }, `${sd.name} · ${isLeft ? 'Left' : 'Right'}`),
        el('span', { class: 'io__tx' }, 'Waiting'),
      ]),
      el('div', { style: 'font-size:15px;color:var(--rz-paper-70);margin-bottom:18px' },
        'When ready, say a few words and hear them played back to you.'),
      el('a', { class: 'rz-btn rz-btn--ghost rz-btn--sm', onclick: () => send('startChannelTest', { side }) }, 'Start ›'),
    ]);
  }
  if (ct.confirmed[side]) {
    return el('div', { class: `ct ct--${side}` }, [
      el('div', { class: 'io__head' }, [
        el('span', { class: `io__label io__label--${side}` }, `${sd.name} · ${isLeft ? 'Left' : 'Right'}`),
        el('span', { class: 'io__tx' }, [el('span', { class: 'dot dot--live' }), 'Confirmed ✓']),
      ]),
      el('div', { style: 'font-size:15px;color:var(--rz-paper-85)' }, `${sd.name} heard themselves in the ${isLeft ? 'left' : 'right'} ear. This side is wired correctly.`),
    ]);
  }
  // active
  return el('div', { class: `ct ct--active ct--${side}` }, [
    isLeft ? el('div', { class: 'ct__wash' }) : null,
    el('div', { class: 'io__head' }, [
      el('span', { class: `io__label io__label--${side}` }, `${sd.name} · ${isLeft ? 'Left' : 'Right'}`),
      el('span', { class: 'io__tx' }, [el('span', { class: isLeft ? 'dot dot--live' : 'dot dot--live-pink' }), 'Listening']),
    ]),
    el('div', { style: 'font-size:13px;color:var(--rz-paper-70)' }, 'Say into your mic:'),
    el('div', { class: 'ct__say' }, 'A few words…'),
    makeWave('wave-capture', isLeft ? '' : 'wave--pink'),
    el('div', { class: 'flow' }, [
      el('span', { class: 'box' }, `Mic ${isLeft ? 'L' : 'R'}`), el('span', { class: 'arr' }, '→'),
      el('span', { class: 'hot' }, '+0.3s'), el('span', { class: 'arr' }, '→'),
      el('span', { class: 'box' }, `Ear ${isLeft ? 'L' : 'R'}`),
    ]),
    el('div', { style: 'font-size:14px;color:var(--rz-paper);margin-bottom:12px' },
      [`Can you hear yourself in your `, el('strong', { style: `color:var(--rz-${isLeft ? 'yellow' : 'pink'})` }, isLeft ? 'left' : 'right'), ' ear?']),
    el('div', { class: 'btn-row' }, [
      el('a', { class: 'rz-btn', onclick: () => send('channelTestAnswer', { side, ok: true }) }, "Yes, that's me ✓"),
      el('a', { class: 'rz-btn rz-btn--ghost', onclick: () => send('channelTestAnswer', { side, ok: false }) }, 'No'),
    ]),
  ]);
}

function channelRow(s) {
  const bothOk = s.channelTest.confirmed.left && s.channelTest.confirmed.right;
  return el('div', { class: 'row' }, [
    el('div', { class: 'row__head' }, [
      el('span', { class: 'row__num' }, '03'),
      el('div', { class: 'spacer' }, [el('div', { class: 'eyebrow eyebrow--yellow' }, 'Step 3 of 3 · Active'),
        el('div', { class: 'row__title' }, 'Testing testing 1 2 3')]),
    ]),
    el('div', { style: 'font-size:15px;line-height:1.5;color:var(--rz-paper-85);margin-bottom:22px;max-width:74ch' },
      'One at a time, press start and say a few words. You should hear these words played back to you.'),
    el('div', { class: 'iogrid' }, [channelTestCard(s, 'left'), channelTestCard(s, 'right')]),
    el('div', { class: 'row__foot' }, [
      el('span', {}),
      el('a', { class: bothOk ? 'rz-btn' : 'rz-btn rz-btn--ghost', style: bothOk ? '' : 'opacity:0.7',
        onclick: () => { send('startLive'); send('gotoStep', { step: 'live' }); } }, 'Start live translation →'),
    ]),
  ]);
}

// ---- crossed (wrong side) -------------------------------------------------------------------
function crossedRow(s) {
  return el('div', { class: 'row' }, [
    el('div', { class: 'row__head' }, [
      el('span', { class: 'row__num', style: 'background:var(--rz-pink)' }, '03'),
      el('div', { class: 'spacer' }, [el('div', { class: 'eyebrow' }, 'Channel test'),
        el('div', { class: 'row__title' }, 'The sides look crossed')]),
    ]),
    el('div', { class: 'plate' }, [
      el('div', { class: 'plate__shadow' }),
      el('div', { class: 'plate__body' }, [
        el('div', { style: 'display:flex;align-items:center;gap:12px;margin-bottom:10px' }, [
          el('span', { class: 'mono', style: 'font-weight:700;font-size:12px;letter-spacing:0.16em;text-transform:uppercase;border:1.5px solid var(--rz-ink);padding:4px 10px;border-radius:4px' }, 'Heads up'),
          el('span', { class: 'display', style: 'font-size:24px' }, 'Mic and ear on different sides'),
        ]),
        el('div', { style: 'font-weight:600;font-size:16px;line-height:1.45;max-width:72ch' },
          "The person spoke into one mic but heard the echo in the other ear. Their mic and earbud aren't on the same side, so live translation would play into the wrong person."),
      ]),
    ]),
    el('div', { style: 'display:grid;grid-template-columns:1fr;gap:14px' }, [
      el('div', { class: 'btn-row' }, [
        el('a', { class: 'rz-btn', onclick: () => send('swapEarbuds') }, 'Swap earbuds L ↔ R'),
        el('a', { class: 'rz-btn rz-btn--ghost', onclick: () => send('swapPeople') }, 'Swap the two people'),
        el('a', { class: 'rz-btn rz-btn--ghost', onclick: () => send('startChannelTest', { side: (state.channelTest.side || 'left') }) }, 'Run the test again'),
      ]),
      el('div', { style: 'font-size:13px;color:var(--rz-paper-70)' }, 'Easiest is usually to just switch which ear each person wears, then test again.'),
    ]),
  ]);
}

// ---- device not found -----------------------------------------------------------------------
function notFoundRow(s, kind) {
  const what = kind === 'input' ? 'DJI receiver' : 'earbuds';
  const dev = kind === 'input' ? s.input.deviceName : s.output.deviceName;
  return el('div', { class: 'row', style: 'position:relative;overflow:hidden' }, [
    el('div', { style: 'position:absolute;inset:0;opacity:0.08;background-image:var(--rz-halftone-pink);background-size:var(--rz-halftone-size);pointer-events:none' }),
    el('div', { class: 'row__head' }, [
      el('div', { class: 'spacer' }, [el('div', { class: 'eyebrow' }, 'Setup · ' + (kind === 'input' ? 'Inputs' : 'Outputs')),
        el('div', { class: 'row__title' }, kind === 'input' ? 'The two microphones' : 'The two earbuds')]),
      el('span', { class: 'mono', style: 'display:flex;align-items:center;gap:8px;font-weight:700;font-size:12px;letter-spacing:0.12em;text-transform:uppercase;color:var(--rz-ink);background:var(--rz-pink);padding:7px 13px;border-radius:999px' },
        [el('span', { class: 'dot', style: 'background:var(--rz-ink)' }), kind === 'input' ? 'Receiver offline' : 'Earbuds offline']),
    ]),
    el('div', { class: 'notfound' }, [
      el('div', {}, [
        el('h2', {}, ["Can't find the ", el('span', { class: 'hl-pink' }, what + '.')]),
        el('div', { style: 'font-size:16px;line-height:1.5;color:var(--rz-paper-85);margin-top:18px;max-width:48ch' },
          `The “${dev}” device isn't showing up. ${kind === 'input' ? 'Without it there are no mics to read.' : 'Without it there is nowhere to play the translation.'}`),
        el('ol', { class: 'steps' }, (kind === 'input' ? [
          'Plug the USB-C receiver into your Mac.', 'Power on both clip-on transmitters.', 'Set the receiver to stereo / dual-channel.',
        ] : [
          'Power on the earbuds.', 'Connect them over Bluetooth.', 'Pick them as the output device.',
        ]).map((t, i) => el('li', {}, [el('span', { class: 'n' }, '0' + (i + 1)), el('span', {}, t)]))),
        el('div', { style: 'margin-top:22px;max-width:90%' }, [
          el('div', { class: 'field__label' }, kind === 'input' ? 'Or choose another microphone' : 'Or choose another output device'),
          deviceSelect(s, kind),
        ]),
        el('div', { class: 'btn-row', style: 'margin-top:18px;align-items:center' }, [
          el('a', { class: 'rz-btn', onclick: () => send('rescan') }, 'Rescan'),
          el('span', { class: 'mono', style: 'display:flex;align-items:center;gap:8px;font-size:12px;letter-spacing:0.12em;text-transform:uppercase;color:var(--rz-paper-70)' },
            ['Scanning', el('span', { style: 'width:9px;height:16px;background:var(--rz-yellow);animation:rzblink 0.9s step-end infinite' })]),
        ]),
      ]),
      el('div', { style: 'display:flex;flex-direction:column;gap:14px' }, [
        deadIo('Left input'), deadIo('Right input'),
      ]),
    ]),
  ]);
}
function deadIo(label) {
  return el('div', { class: 'dead-io' }, [
    el('div', { style: 'display:flex;align-items:center;justify-content:space-between;margin-bottom:12px' }, [
      el('span', { class: 'mono', style: 'font-weight:700;font-size:12px;letter-spacing:0.14em;text-transform:uppercase;color:var(--rz-paper-70)' }, label),
      el('span', { class: 'mono', style: 'font-size:11px;letter-spacing:0.1em;text-transform:uppercase;color:var(--rz-pink)' }, 'No signal'),
    ]),
    el('div', { class: 'bar' }, '— — — — — —'),
  ]);
}

// ---- LIVE -----------------------------------------------------------------------------------
function buildLive(s) {
  return el('div', {}, [
    el('div', { class: 'live__bar' }, [
      el('span', { class: 'live__status' }, [el('span', { class: 'dot dot--live' }), 'Live translation']),
      el('span', { class: 'mono', style: 'font-size:13px;letter-spacing:0.12em;text-transform:uppercase;color:var(--rz-paper-70)' }, 'Deutsch ↔ English'),
      el('div', { style: 'display:flex;align-items:center;gap:18px' }, [
        el('span', { class: 'mono', id: 'live-time', style: 'font-size:15px;color:var(--rz-paper)' }, fmtTime(s.session.elapsedMs)),
        el('a', { class: 'rz-btn rz-btn--pink rz-btn--sm', onclick: () => { send('stopLive'); send('gotoStep', { step: 'channel' }); } }, 'Stop'),
      ]),
    ]),
    el('div', { class: 'live__cols' }, [liveCol(s, 'left'), liveCol(s, 'right')]),
    el('div', { class: 'live__foot' }, [
      el('span', {}, 'Automatic language detection · ~0.3s delay'),
      el('span', {}, 'Powered by Gemini 3.5 Live Transcribe'),
    ]),
  ]);
}
function liveCol(s, side) {
  const sd = s.sides[side];
  const speaks = LANG_NAME[sd.lang].includes('English') ? 'EN' : 'DE';
  return el('div', { class: side === 'left' ? 'col col--left' : 'col' }, [
    el('div', { class: 'col__head' }, [
      el('span', { class: `col__name col__name--${side}` }, sd.name),
      el('span', { class: 'col__meta' }, `${sd.lang === 'en' ? 'Speaks EN · Hears EN' : 'Spricht DE · Hört DE'}`),
    ]),
    el('div', { id: `col-${side}`, class: 'col__list' }),
  ]);
}

// ---- per-frame patch ------------------------------------------------------------------------
let lastTranscriptSig = '';
function patch(s) {
  // waveforms
  patchWave('wave-left', s.sides.left.waveform);
  patchWave('wave-right', s.sides.right.waveform);
  if (s.channelTest.active && s.channelTest.side) patchWave('wave-capture', s.sides[s.channelTest.side].waveform);
  // input tx dots reflect speaking
  setDot('tx-left', s.sides.left.speaking, 'left');
  setDot('tx-right', s.sides.right.speaking, 'right');
  // gain value + output meters
  const gv = $('gainVal'); if (gv) gv.textContent = Math.round(s.gain * 100) + '%';
  setMeter('meter-left', s.sides.left.outLevel);
  setMeter('meter-right', s.sides.right.outLevel);
  // test-mic button labels
  setTestBtn('testmic-left', s.sides.left.testing);
  setTestBtn('testmic-right', s.sides.right.testing);
  // live timer + transcript
  const lt = $('live-time'); if (lt) lt.textContent = fmtTime(s.session.elapsedMs);
  if (s.wizardStep === 'live') patchTranscript(s);
}
function patchWave(id, wf) {
  const c = $(id); if (!c || !wf) return;
  const bars = c.children;
  for (let i = 0; i < bars.length && i < wf.length; i++) bars[i].style.height = Math.max(8, Math.round(wf[i] * 100)) + '%';
}
function setDot(id, on, side) {
  const d = $(id); if (!d) return;
  d.className = 'dot' + (on ? (side === 'left' ? ' dot--live' : ' dot--live-pink') : '');
}
function setMeter(id, lvl) { const m = $(id); if (m) m.style.width = Math.round((lvl || 0) * 100) + '%'; }
function setTestBtn(id, testing) { const b = $(id); if (b) b.textContent = testing ? 'Stop test' : 'Test mic'; }

function patchTranscript(s) {
  const utts = s.session.utterances;
  // key on actual content (not lengths) so same-length ASR corrections still repaint
  const sig = utts.map((u) => u.id + ':' + u.source + '␟' + u.translation + ':' + (u.live ? 1 : 0)).join('|');
  if (sig === lastTranscriptSig) return;
  lastTranscriptSig = sig;
  const t0 = utts.length ? Math.min(...utts.map((u) => u.tStartMs)) : 0;
  ['left', 'right'].forEach((side) => {
    const col = $('col-' + side); if (!col) return;
    col.innerHTML = '';
    const mine = utts.filter((u) => u.side === side);
    if (!mine.length) { col.appendChild(el('div', { class: 'empty-hint' }, 'Waiting for speech…')); return; }
    mine.forEach((u) => col.appendChild(utteranceCard(u, side, t0)));
  });
}
function utteranceCard(u, side, t0) {
  const isLeft = side === 'left';
  const other = isLeft ? 'right' : 'left';
  const arrowName = (state.sides[other] || {}).name || (isLeft ? 'Right' : 'Left');
  return el('div', { class: 'utt ' + (u.live ? `utt--live utt--${side}` : '') }, [
    el('div', { class: 'utt__top' }, [
      el('span', { class: `utt__who utt__who--${side}` }, u.live ? `${u.speaker} speaking…` : `${u.speaker} said`),
      el('span', { class: 'utt__time' }, fmtTime(u.tStartMs - t0)),
    ]),
    el('div', { class: 'utt__src' }, u.source || '…'),
    u.translation ? el('div', { class: 'utt__xlate' }, [
      el('span', { class: 'utt__arrow ' + (isLeft ? 'utt__arrow--toPink' : 'utt__arrow--toYel') }, `→ ${arrowName} · ${u.dstLang.toUpperCase()}`),
      el('span', { class: 'utt__dst' }, u.translation),
    ]) : null,
  ]);
}

// ---- guardian surface + error toast ---------------------------------------------------------
// The guardian's status is the primary earbud-health surface (idle/preventing/fixing/needs_recovery/
// blocked). needs_recovery and blocked are PERSISTENT (the bridge sets persistent:true) so a
// mid-session collapse stays visible with a Fix/Reconnect button until David acts - not a toast that
// vanishes. Plain `error` toasts (non-guardian) still render the old way.
let toastEl = null;
function renderToast(s) {
  const g = s.guardian;
  const err = s.error;
  // Guardian surface wins whenever it has something to say (preventing/fixing/needs/blocked).
  const gActive = g && g.phase && g.phase !== 'idle';
  if (gActive) {
    showToast(g.message, g.actionable ? g.action : null, g.persistent);
    return;
  }
  if (s.fixingOutput) { showToast('Fixing the earbuds — a few seconds…', null, true); return; }
  if (err && err.message) { showToast(err.message, err.fixable ? 'fix' : null, !!err.fixable); return; }
  if (toastEl) toastEl.style.display = 'none';
}

function showToast(message, action, persistent) {
  if (!toastEl) { toastEl = el('div', { class: 'toast' }); document.body.appendChild(toastEl); }
  toastEl.className = 'toast' + (persistent ? ' toast--persistent' : '');
  toastEl.innerHTML = '';
  toastEl.appendChild(el('span', { class: 'toast__msg' }, message || ''));
  if (action === 'fix') {
    toastEl.appendChild(el('button', { class: 'toast__fix', onclick: () => send('fixEarbuds') }, 'Fix'));
  } else if (action === 'reconnect') {
    toastEl.appendChild(el('button', { class: 'toast__fix', onclick: () => send('fixEarbuds') }, 'Reconnect'));
  }
  toastEl.style.display = 'flex';
}

connect();
