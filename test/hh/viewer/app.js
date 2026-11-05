import {TelemetryReplay, parseTelemetry} from './telemetry.js';
import { spoolGrid, nozzleArrivalTime, toolheadProgress, toolheadDrawingRange, playbackFrame, switchFilamentDistance, syncForDisplay, isInternalUpdate, encoderFilamentDistance, visibleSteps, toolchangeIndex, annotatedStep, excessFilamentLabel, bufferSpringPath, bufferPiston, interpolate, constrainFilament, duration, gearAngle, CommandHistory, traceText, homingTarget, homingPulse, bladeStroke, configurationLabel, bowdenLengthLabel, toolheadReferences, coolingTubeMeasurements, dimensionParameter, lastEncoderReading, encoderReference, gateDevicePositions, purgeOutletColor, purgeCoils, purgeStrandLength } from './playback.js';
/* Copyright (C) 2022-2026 Paul Morgan. GPLv3. */
// Temporary preview values; explicit per-gate percentages can replace these later.
const previewSpoolFill = new Map();
function spoolFill(state, gate) {
  const supplied = state.spool_percentages?.[gate];
  if (Number.isFinite(supplied)) return Math.max(0, Math.min(100, supplied));
  if (!previewSpoolFill.has(gate))
    previewSpoolFill.set(gate, gate === 1 ? 0 : 10 + Math.floor(Math.random() * 91));
  return previewSpoolFill.get(gate);
}
const $ = (id) => document.getElementById(id);
// A shared overlay styles HTML and SVG tooltips consistently and avoids clipping.
const tooltip = document.createElement('div');
tooltip.id = 'viewer-tooltip';
tooltip.setAttribute('role', 'tooltip');
tooltip.hidden = true;
document.body.append(tooltip);
let tooltipTimer, tooltipTarget;
function hideTooltip() {
  clearTimeout(tooltipTimer);
  tooltip.hidden = true;
  if (tooltipTarget) {
    const ids = (tooltipTarget.getAttribute('aria-describedby') || '').split(' ').filter(id => id && id !== tooltip.id);
    if (ids.length) tooltipTarget.setAttribute('aria-describedby', ids.join(' '));
    else tooltipTarget.removeAttribute('aria-describedby');
  }
  tooltipTarget = null;
}
function showTooltipLater(target) {
  if (target === tooltipTarget) return;
  hideTooltip();
  if (!target?.dataset.tooltip) return;
  tooltipTarget = target;
  tooltipTimer = setTimeout(() => {
    if (!target.isConnected) return hideTooltip();
    tooltip.textContent = target.dataset.tooltip;
    tooltip.hidden = false;
    const rect = target.getBoundingClientRect();
    const box = tooltip.getBoundingClientRect();
    tooltip.style.left = `${Math.max(8, Math.min(rect.left, innerWidth - box.width - 8))}px`;
    tooltip.style.top = `${Math.max(8, rect.bottom + box.height + 6 <= innerHeight ? rect.bottom + 6 : rect.top - box.height - 6)}px`;
    target.setAttribute('aria-describedby', [target.getAttribute('aria-describedby'), tooltip.id].filter(Boolean).join(' '));
  }, target.closest('#sidebar-playback') ? 1000 : 500);
}
document.addEventListener('pointerover', e => showTooltipLater(e.target.closest('[data-tooltip]')));
document.addEventListener('pointerout', e => {
  if (!tooltipTarget?.contains(e.relatedTarget)) hideTooltip();
});
document.addEventListener('focusin', e => showTooltipLater(e.target.closest('[data-tooltip]')));
document.addEventListener('focusout', hideTooltip);
document.addEventListener('keydown', e => { if (e.key === 'Escape') hideTooltip(); });
document.addEventListener('scroll', hideTooltip, true);
window.addEventListener('blur', hideTooltip);
function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const next = theme === 'dark' ? 'Light' : 'Dark';
  const icons = {"light": "<svg aria-hidden=\"true\" xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 24 24\"><path d=\"M12 18c-.89 0-1.74-.2-2.5-.55C11.56 16.5 13 14.42 13 12s-1.44-4.5-3.5-5.45C10.26 6.2 11.11 6 12 6a6 6 0 0 1 6 6 6 6 0 0 1-6 6m8-9.31V4h-4.69L12 .69 8.69 4H4v4.69L.69 12 4 15.31V20h4.69L12 23.31 15.31 20H20v-4.69L23.31 12z\"/></svg>", "dark": "<svg aria-hidden=\"true\" xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 24 24\"><path d=\"M12 8a4 4 0 0 0-4 4 4 4 0 0 0 4 4 4 4 0 0 0 4-4 4 4 0 0 0-4-4m0 10a6 6 0 0 1-6-6 6 6 0 0 1 6-6 6 6 0 0 1 6 6 6 6 0 0 1-6 6m8-9.31V4h-4.69L12 .69 8.69 4H4v4.69L.69 12 4 15.31V20h4.69L12 23.31 15.31 20H20v-4.69L23.31 12z\"/></svg>"};
  $('theme-toggle').innerHTML = icons[next.toLowerCase()];
  $('theme-toggle').dataset.tooltip = 'Switch to ' + next.toLowerCase() + ' mode';
  $('theme-toggle').setAttribute('aria-label', 'Switch to ' + next.toLowerCase() + ' mode');
}
let savedTheme;
try { savedTheme = localStorage.getItem('hh-viewer-theme'); } catch {}
setTheme(savedTheme === 'light' ? 'light' : 'dark');
$('theme-toggle').onclick = () => {
  const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  setTheme(theme);
  try { localStorage.setItem('hh-viewer-theme', theme); } catch {}
};
let offline = false, sourceGeneration = 0, sourceRecords = [], sourceReplay = new TelemetryReplay();
let pendingSource = null, pendingFingerprint = '';
function sourceMode() {
  const simulatorConnected=!offline && socket?.readyState === 1 && setup && (setup.connection_kind || 'simulator') === 'simulator';
  document.querySelectorAll('[data-simulator-config]').forEach(panel=>{
    panel.hidden=!simulatorConnected || panel.dataset.available !== 'true';
  });
  $('return-live').hidden = !offline;
  $('recording-name').hidden = !offline;
  document.querySelectorAll('#simulator-controls input, #simulator-controls select, #simulator-controls button, #command-form input, #command-form button, [data-simulator-config] input, [data-simulator-config] select, [data-simulator-config] button')
    .forEach(node=>node.disabled=offline);
  $('live').dataset.tooltip = offline ? 'End of recording' : 'End, go live';
  $('live').setAttribute('aria-label', $('live').dataset.tooltip);
}

let socket,
  serial = 0,
  pending = new Map(),
  events = [],
  rawEvents = [],
  session = null,
  live = true,
  cursor = 0,
  setup = null,
  busy = false;
const escape = (s) =>
  String(s).replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
function consoleMarkup(text) {
  const template = document.createElement('template');
  template.innerHTML = text;
  const fragment = document.createDocumentFragment();
  const copy = (node, parent) => {
    if (node.nodeType === Node.TEXT_NODE) { parent.append(document.createTextNode(node.textContent)); return; }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    if (node.tagName === 'SPAN') {
      const span = document.createElement('span');
      const color = node.style.color;
      if (color && /^(#[0-9a-f]{3,8}|[a-z]+|rgba?\([0-9.,% /]+\)|hsla?\([0-9.,% /a-z]+\))$/i.test(color) && CSS.supports('color', color)) span.style.color = color;
      for (const child of node.childNodes) copy(child, span);
      parent.append(span);
    } else if (node.tagName === 'B' || node.tagName === 'STRONG') {
      const bold = document.createElement('strong');
      for (const child of node.childNodes) copy(child, bold);
      parent.append(bold);
    } else if (node.tagName === 'BR') parent.append(document.createElement('br'));
    else parent.append(document.createTextNode(node.outerHTML));
  };
  for (const node of template.content.childNodes) copy(node, fragment);
  return fragment;
}
function consoleLine(text, kind = 'response') {
  const log = $('mini-console');
  const follow = log.scrollHeight-log.scrollTop-log.clientHeight < 32;
  for (const line of String(text).split('\n')) {
    const row = document.createElement('div');
    row.className = 'console-' + ((line.startsWith('!!') || line.startsWith('Unknown command:')) ? 'error' : kind);
    const time = document.createElement('span');
    time.className = 'console-time';
    time.textContent = new Date().toLocaleTimeString('en-US', {hour:'numeric', minute:'2-digit'}) + ' ';
    row.append(time);
    if (kind === 'command') {
      const command = document.createElement('span');
      command.className = 'console-command-text';
      command.textContent = line;
      row.append(command);
    }
    else row.append(consoleMarkup(line));
    log.append(row);
  }
  while (log.children.length > 500) log.firstChild.remove();
  if (follow) log.scrollTop = log.scrollHeight;
}
$('clear-console').onclick = () => $('mini-console').replaceChildren();
function message(text, error = false) {
  $("message").textContent = text;
  $("message").classList.toggle("error", error);
}
function rpc(method, params = {}) {
  return new Promise((resolve, reject) => {
    if (offline) return reject(Error("Return to live before changing the printer"));
    if (socket?.readyState !== 1) return reject(Error("Console disconnected"));
    const id = ++serial;
    pending.set(id, { resolve, reject });
    socket.send(JSON.stringify({ jsonrpc: "2.0", id, method, params }));
  });
}
async function action(fn) {
  if (busy) return;
  busy = true;
  document.querySelectorAll("aside button, #send").forEach((b) => (b.disabled = true));
  try {
    await fn();
  } catch (e) {
    message(e.message, true);
    consoleLine(e.message, 'error');
  } finally {
    busy = false;
    document
      .querySelectorAll("aside button, #send")
      .forEach((b) => (b.disabled = false));
    if (offline) sourceMode();
  }
}
function connect() {
  socket = new WebSocket(
    `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/websocket`,
  );
  socket.onopen = () => {
    setup=null;
    sourceMode();
    if (offline) return;
    $("connection").textContent = "Connected · identifying source…";
    refresh().catch((e) => message(e.message, true));
  };
  socket.onmessage = (e) => {
    if (offline) return;
    const data = JSON.parse(e.data);
    if (pending.has(data.id)) {
      const p = pending.get(data.id);
      pending.delete(data.id);
      data.error ? p.reject(Error(data.error.message)) : p.resolve(data.result);
    } else if (data.method === "notify_gcode_command") {
      followLive();
      consoleLine(data.params[0], "command");
    } else if (data.method === "notify_gcode_response") {
      const line = data.params[0];
      consoleLine(line);
      if (line.startsWith("!!") || line.startsWith("Unknown command:")) message(line, true);
    }
  };
  socket.onclose = () => {
    sourceMode();
    if (!offline) $("connection").textContent = "Disconnected · retrying";
    pending.forEach((p) => p.reject(Error("Console disconnected")));
    pending.clear();
    setTimeout(connect, 1500);
  };
}
function options(node, items) {
  node.replaceChildren(
    ...items.map(([value, label]) => {
      const o = document.createElement("option");
      o.value = value;
      o.textContent = label;
      return o;
    }),
  );
}
async function refresh() {
  if (offline) return;
  setup = await rpc("sim.viewer.setup", { unit: Number($("unit").value || 0) });
  if (offline) return;
  sourceMode();
  const kind = setup.connection_kind || 'simulator'; // Older sim.viewer.setup responses are simulator-only.
  $("connection").textContent = kind === 'simulator' ? 'Connected to the simulator'
    : kind === 'live_machine' ? 'Connected to a live machine' : 'Connected · unknown source';
  const unit = $("unit").value;
  options(
    $("unit"),
    setup.units.map((name, i) => [i, name]),
  );
  $("unit").value = unit || "0";
  options(
    $("profiles"),
    setup.profiles.map((p) => [p.name, p.name + " · " + p.description]),
  );
  if (setup.profiles.some((p) => p.name === setup.profile))
    $("profiles").value = setup.profile;
  $("profile").textContent = "Running: " + setup.profile;
  await refreshSimulatorObjects();
  filter();
}
function filter() {
  const previous = $("parameter").value,
    query = $("filter").value.trim().toLowerCase();
  const groups = new Map();
  for (const p of [...setup.parameters, ...(setup.macro_parameters || [])]) {
    if (!p.name.toLowerCase().includes(query)) continue;
    // Older servers already expose MMU_TEST_CONFIG section names, even when
    // owner metadata is unavailable. Never misclassify these as "Other".
    const label = p.macro ? `Macro · ${p.macro}`
      : p.scope === 'machine' || p.group === 'Machine Wide' ? 'Machine wide'
      : p.scope === 'other' || p.group?.startsWith('Other') ? 'Other calibration'
      : p.group || p.section || 'Parameters';
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push(p);
  }
  $("parameter").replaceChildren(...[...groups].map(([label, matches]) => {
    const group = document.createElement('optgroup');
    group.label = label;
    if (matches[0]?.macro) group.dataset.macro=matches[0].macro;
    const sections = new Map();
    for (const p of matches) {
      const section = p.section || '';
      if (!sections.has(section)) sections.set(section, []);
      sections.get(section).push(p);
    }
    for (const [section, parameters] of sections) {
      if (section) {
        const heading = document.createElement('option');
        heading.disabled = true;
        heading.value = '';
        heading.className = 'parameter-section';
        heading.textContent = section;
        group.append(heading);
      }
      for (const p of parameters) {
        const option = document.createElement('option');
        option.value = p.id || p.name;
        option.textContent = p.name;
        group.append(option);
      }
    }
    return group;
  }));
  if ([...$("parameter").options].some((o) => !o.disabled && o.value === previous))
    $("parameter").value = previous;
  value();
}
function selectedParameter() {
  return [...(setup?.parameters || []), ...(setup?.macro_parameters || [])]
    .find(p => (p.id || p.name) === $("parameter").value);
}
function value() {
  const p = selectedParameter();
  $("apply").textContent = p?.macro ? 'Apply macro variable' : 'Apply MMU_TEST_CONFIG';
  $("value").value = p?.macro ? p.literal : p
    ? Array.isArray(p.value)
      ? p.value.join(",")
      : (p.value ?? "")
    : "";
}
async function selectDimension(event) {
  const target = event.target.closest('[data-parameter]');
  if (!target) return;
  if (event.type === 'keydown' && !['Enter', ' '].includes(event.key)) return;
  event.preventDefault();
  try {
    if (target.dataset.macro) {
      $('filter').value='';
      filter();
      const menu=$('parameter');
      const name=target.dataset.macro.trim().split(/\s+/)[0].toUpperCase();
      const groups=[...menu.querySelectorAll('optgroup')];
      const group=[name+'_VARS',name].map(candidate=>groups.find(item=>item.dataset.macro?.toUpperCase()===candidate)).find(Boolean);
      if (!group) { message('This macro has no editable variables in the current configuration.',true); return; }
      menu.scrollIntoView({block:'nearest'});
      menu.focus();
      menu.style.setProperty('--macro-picker-padding','400px');
      menu.showPicker();
      requestAnimationFrame(()=>group.scrollIntoView({block:'start',inline:'nearest'}));
      return;
    }
    if (Number(target.dataset.unit) >= 0) $('unit').value = target.dataset.unit;
    await refresh();
    $('filter').value = '';
    filter();
    const id = target.dataset.parameter;
    if (![...$('parameter').options].some(option => option.value === id)) {
      message('This item has no editable parameter in the current configuration.', true);
      return;
    }
    $('parameter').value = id;
    value();
    $('value').focus();
    $('value').select();
    $('parameter').scrollIntoView({block: 'nearest'});
    message('Parameter selected. Apply to update the live configuration.');
  } catch (error) { message(error.message, true); }
}
$('scene').addEventListener('click', selectDimension);
$('scene').addEventListener('keydown', selectDimension);
$('parameter').addEventListener('pointerdown',()=> $('parameter').style.removeProperty('--macro-picker-padding'));
$('parameter').addEventListener('change',()=> $('parameter').style.removeProperty('--macro-picker-padding'));
$('scene-macros').addEventListener('click', selectDimension);
$('scene-macros').addEventListener('keydown', selectDimension);
$("filter").oninput = filter;
$("clear-filter").onclick = () => {
  $("filter").value = '';
  if (setup) filter();
  $("filter").focus();
};
$("parameter").onchange = value;
$("unit").onchange = () => refresh().catch((e) => message(e.message, true));
$("apply").onclick = () =>
  action(async () => {
    setup = await rpc("sim.viewer.configure", {
      unit: Number($("unit").value),
      name: selectedParameter()?.name,
      macro: selectedParameter()?.macro,
      value: $("value").value,
    });
    filter();
    message("Runtime parameter applied.");
  });
const commandHistory = new CommandHistory();
$("command").addEventListener('keydown', event => {
  if (event.isComposing || event.altKey || event.ctrlKey || event.metaKey) return;
  if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return;
  event.preventDefault();
  const input = event.currentTarget;
  input.value = commandHistory.navigate(event.key === 'ArrowUp' ? -1 : 1, input.value);
  input.setSelectionRange(input.value.length, input.value.length);
});
$("command-form").onsubmit = (event) => {
  event.preventDefault();
  const script = $("command").value.trim();
  if (busy || !script) return;
  action(async () => {
    const sent = rpc("printer.gcode.script", { script });
    if (socket.readyState === WebSocket.OPEN) {
      commandHistory.add(script);
      $("command").value = "";
    }
    message("Command running…");
    await sent;
    message("Command finished. Inspect the trace and console output.");
  });
};
$("restart").onclick = () =>
  action(async () => {
    message("Restarting simulator…");
    await rpc("sim.viewer.restart", { profile: $("profiles").value });
    $("unit").value = "0";
    await refresh();
    message("Fresh profile session started.");
  });
$("files").onchange = () =>
  action(async () => {
    const files = await Promise.all(
      [...$("files").files]
        .filter((f) => f.name.endsWith(".cfg"))
        .map(async (f) => ({
          name: f.webkitRelativePath || f.name,
          text: await f.text(),
        })),
    );
    message("Loading uploaded profile…");
    await rpc("sim.viewer.restart", { files });
    $("unit").value = "0";
    await refresh();
    message("Uploaded profile started.");
  });
// Temporary visual-debug clock: telemetry and simulator execution keep their original timing.
let animationRate=1, animationTime=0, animationRealTime=performance.now();
function animationNow() {
  const now=performance.now();
  animationTime+=(now-animationRealTime)*animationRate;
  animationRealTime=now;
  return animationTime;
}
let displayed = null, playing = null, played = 0, resumeAt = 0;
const spoolAngles=new Map(), spoolArrowVisibility=new Map(), spoolDirections=new Map();
let spoolLastFrame=null, spoolSession=null;
// Keep the first neighboring lane clear of the selected spool; others stay evenly spaced.
const gateLaneX=offset=>620+offset*Number($('spool-spacing').value);
let paused = false, pauseAfterStep = false, replayingHistory = false, sensorPulse = null;
function currentIndex() {
  return live ? Math.max(0, events.findIndex(e => e.seq === (playing?.event.seq || played))) : cursor;
}
function followTrace() {
  const view = $("trace"), index = currentIndex();
  const sequence = events[index]?.seq;
  for (const row of view.children) {
    const selected = Number(row.dataset.seq) === sequence;
    row.classList.toggle('selected', selected);
    if (selected) row.setAttribute('aria-current', 'step');
    else row.removeAttribute('aria-current');
  }
  const row = [...view.children].find(row => Number(row.dataset.seq) === sequence);
  if (row) {
    const box = row.getBoundingClientRect(), bounds = view.getBoundingClientRect();
    if (box.top < bounds.top) view.scrollTop -= bounds.top - box.top;
    else if (box.bottom > bounds.bottom) view.scrollTop += box.bottom - bounds.bottom;
  }
}
function seek(index) {
  pauseAfterStep = false;
  spoolArrowVisibility.clear();
  cursor = Math.max(0, Math.min(index, events.length - 1));
  live = false; paused = true; replayingHistory = true; playing = null; sensorPulse = null;
  draw(); followTrace();
}
// An inward ring marks active homing; the outward ring marks completion.
function homingRing(name,x,y) {
  if (!playing || homingTarget(playing.event) !== name) return '';
  const reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;
  const phase=((animationNow()-playing.start)%600)/600;
  const reverse=(playing.event.requested ?? playing.event.distance ?? 0) < 0;
  const color=reverse ? '#48c982' : 'var(--sensor-clear-pulse)';
  const radius=reduced ? 9 : 13-9*phase;
  const opacity=reduced ? .55 : .6*Math.min(1,phase*8)*(1-.35*phase);
  return `<circle data-part="homing-sensor-ring" cx="${x}" cy="${y}" r="${radius}" fill="none" stroke="${color}" stroke-width="2" opacity="${opacity}" pointer-events="none"/>`;
}
// Buffer drawing is isolated from the toolhead and filament layers.
function renderBuffer(s, d, bufferSensors, sensorFill) {
  let markup = '';
  const tension=bufferSensors.find(([n])=>n.endsWith('tension'))?.[1]?.state;
  const compression=bufferSensors.find(([n])=>n.endsWith('compression'))?.[1]?.state;
  const piston=bufferPiston(d,tension,compression);
  // Fixed housing; the inner piston tracks buffer travel.
  const tick=(travel)=>498.8-57.6*(piston.maximum ? travel/piston.maximum : .5);
  const edge=tick(piston.position);
  // Retain protrusion even at the maximum-tension stop.
  const pistonLeft=edge-80;
  markup += `<defs><mask id="buffer-tube-gap" maskUnits="userSpaceOnUse" x="0" y="0" width="850" height="700"><rect width="850" height="700" fill="white"/><rect x="${pistonLeft+4}" y="50" width="${508-pistonLeft-4}" height="50" fill="black"/></mask></defs><g id="sync-buffer"><path data-part="buffer-filament-guide" d="M${pistonLeft+4} 66.5 H508 M${pistonLeft+4} 83.5 H508" fill="none" stroke="var(--scene-tube-border, #b1c4d2)" stroke-width="1.6" opacity="0.3"/><text x="470" y="38" text-anchor="middle" font-size="10" fill="var(--scene-text, #304c65)">Sync-feedback buffer</text><g fill="none" stroke="var(--scene-border, #96abba)" stroke-width="1.5"><rect id="buffer-housing" x="428" y="46" width="84" height="51.88" rx="6.4"/><rect id="buffer-piston" x="${pistonLeft}" y="59" width="80" height="32" rx="5.6"/></g><g fill="var(--scene-tube-border, #b1c4d2)"><rect data-part="buffer-housing-port" x="509.6" y="62.2" width="4.8" height="25.6" rx="2.4"/><rect data-part="buffer-piston-port" x="${pistonLeft-2.4}" y="62.2" width="4.8" height="25.6" rx="2.4"/></g>`;
  const geometry=Object.values(s.buffers || {}).find(buffer=>buffer.gates.includes(s.gate));
  const spring=geometry?.spring ?? d.spring ?? 'none';
  const springSides = spring === 'neutral' || spring === 0 ? ['left','right']
    : spring === 'tension' || spring === -1 ? ['right']
    : spring === 'compression' || spring === 'compressed' || spring === 1 ? ['left'] : [];
  if (springSides.length) {
    markup += `<defs><linearGradient id="buffer-coil-shading" gradientUnits="userSpaceOnUse" x1="0" y1="48.8" x2="0" y2="56.2"><stop offset="0" stop-color="color-mix(in srgb, var(--scene-border, #96abba), white 45%)"/><stop offset="0.45" stop-color="color-mix(in srgb, var(--scene-border, #96abba), white 15%)"/><stop offset="1" stop-color="color-mix(in srgb, var(--scene-border, #96abba), black 30%)"/></linearGradient></defs><g id="buffer-springs" opacity="0.5" fill="none" stroke="var(--scene-border, #96abba)" stroke-width="1.2" stroke-linejoin="round">`;
    let anchors=`<circle data-part="buffer-spring-anchor" cx="${edge}" cy="52.5" r="1.5" fill="var(--scene-border, #96abba)" stroke="none"/>`;
    for (const side of springSides) {
      const wall=side === 'left' ? 428 : 512;
      const coil=bufferSpringPath(wall,edge);
      markup += `<path data-part="buffer-spring" data-side="${side}" stroke="url(#buffer-coil-shading)" d="${coil}"/>`;
      anchors += `<circle data-part="buffer-spring-wall-anchor" cx="${wall}" cy="52.5" r="1.5" fill="var(--scene-border, #96abba)" stroke="none"/>`;
    }
    markup += anchors+`<path data-part="buffer-spring-piston-tick" d="M${edge} 48.5 V56.5" stroke-width="1.8" opacity="0.9"/>`+'</g>';
  }
  for (const travel of [0,piston.maximum]) {
    markup += `<path data-part="buffer-maxrange-tick" opacity="0.4" d="M${tick(travel)} 93.88 V101.88" stroke="var(--scene-border, #96abba)" stroke-width="1.5" data-tooltip="Physical travel limit · ${travel.toFixed(1)}mm"/>`;
  }
  for (const [x,label,on] of [[tick(piston.low),'tension',tension],[tick(piston.maximum/2),'',null],[tick(piston.high),'compression',compression]]) {
    if (!label) {
      markup += `<path d="M${x} 95.08 V101.08" stroke="var(--scene-border, #96abba)" stroke-width="1.5"/>`;
      continue;
    }
    const sensorName=bufferSensors.find(([n])=>n.endsWith(label.toLowerCase()))?.[0];
    markup += `<g class="sensor-marker${on === null ? ' sensor-disabled' : ''}" data-sensor="${escape(sensorName || '')}"><circle cx="${x}" cy="97.88" r="5" fill="${sensorFill(on)}" stroke="#698394"/><text x="${x}" y="116.88" text-anchor="${label === 'compression' ? 'end' : 'start'}" font-size="9" fill="var(--scene-text, #304c65)">${label}</text></g>`;
    if (sensorName) markup += homingRing(sensorName,x,97.88);
    if (sensorName && sensorPulse?.sensor === sensorName) {
      const elapsed=(animationNow()-sensorPulse.start)/650;
      if (elapsed >= 0 && elapsed < 1) {
        const reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;
        markup += `<circle data-part="sensor-pulse" cx="${x}" cy="97.88" r="${reduced ? 10 : 7+elapsed*15}" fill="none" stroke="${sensorPulse.reverse ? 'var(--sensor-clear-pulse)' : '#48c982'}" stroke-width="2.5" opacity="${1-elapsed}"/>`;
      }
    }
  }
  if (d.proportional) markup += `<text id="buffer-reading" x="470" y="128.88" text-anchor="middle" font-size="10" fill="var(--scene-text, #304c65)">${(d.value || 0).toFixed(1)}</text>`;
  markup += '</g>';
  return markup;
}

function draw() {
  if (!events.length) return;
  cursor = Math.max(0, Math.min(cursor, events.length - 1));
  const running = live && !paused && (playing || played < events.at(-1).seq);
  $("replay").disabled = pauseAfterStep || (!running && currentIndex() >= events.length - 1);
  const playLabel = running ? 'Pause after current step' : offline ? 'Play recording' : 'Play continuously — follow live when caught up';
  $("replay").setAttribute('aria-label', playLabel);
  $("replay").dataset.tooltip = playLabel;
  $("replay").querySelector('path').setAttribute('d', running
    ? 'M6 4h4v16H6z M14 4h4v16h-4z' : 'M7 4v16l13-8z');
  $("beginning").disabled = $("previous").disabled = currentIndex() <= 0;
  $("next").disabled = currentIndex() >= events.length - 1;
  $("play-step").disabled = Boolean(running) || $("next").disabled;
  $("play-previous").disabled = Boolean(running) || $("previous").disabled;
  $("previous-toolchange").disabled = toolchangeIndex(events, currentIndex(), -1) < 0;
  $("next-toolchange").disabled = toolchangeIndex(events, currentIndex(), 1) < 0;
  const e = live ? (playing?.event || events.find(e=>e.seq===played) || events.at(-1)) : events[cursor];
  const
    s = live && displayed ? displayed : (e.settled_state || e.state),
    gate = s.gate >= 0 ? s.gate : 0;
  let tip = s.visual_tips?.[gate] ?? s.tips[gate] ?? -1000;
  const {entry,end,height:toolheadHeight,sensorEntry,sensorGap} = toolheadDrawingRange(s);
  const eventsBehind = events.length - 1 - events.indexOf(e);
  if (live && !paused && !playing && eventsBehind === 0) replayingHistory = false;
  const modeLabel = paused || !live ? 'Paused' : offline ? 'Replay' : replayingHistory ? 'Replay' : eventsBehind >= 3 ? 'Live · catching up' : 'Live';
  // Bookkeeping events must not clear the last animated movement's annotation.
  const annotatedEvent = annotatedStep(events, e, !live || paused);
  const moveReason = annotatedEvent.label || annotatedEvent.telemetry?.start?.label;
  $("mode").textContent = `${modeLabel} · event ${annotatedEvent.seq}` + (moveReason ? ` · ${moveReason}` : '');
  $("position").textContent = `Gate ${gate} · ${s.filament_pos_name || "Unknown state"} · tip ${tip.toFixed(2)} mm`;
  $("timeline").max = events.length - 1;
  $("timeline").value = live ? events.indexOf(e) : cursor;
  // Fixed path with a compressed Bowden section. Position maps monotonically to it.
  const path =
    "M620 570 L620 150 Q620 75 525 75 L355 75 Q260 75 260 170 L260 449";
  const dimensionLink = label => {
    const id = dimensionParameter(s, label);
    return id && !offline ? `class="editable-dimension" role="button" tabindex="0" data-parameter="${escape(id)}" data-unit="${s.units?.indexOf(s.unit) ?? 0}" aria-label="Edit ${escape(label)}"` : '';
  };
  const nozzleDiameter = Number($("nozzle-diameter").value);
  const nozzleWidth = 7 * nozzleDiameter / 1.75;
  // Match the 2mm tip cone, truncated at the selected outlet diameter.
  const funnelShoulder=449+2*toolheadHeight/(end-entry)*(1-nozzleWidth/7);
  const nozzleExit=Math.max(455,449+2*toolheadHeight/(end-entry)+2);
  // Round shadow caps extend 0.4 units inward; stop early to preserve the outlet width.
  const shadowEndX=nozzleWidth/2+.4;
  const shadowEndY=449+(funnelShoulder-449)*(3.5-shadowEndX)/(3.5-nozzleWidth/2);
  const nozzleFunnel = `M256.5 449 L${260-nozzleWidth/2} ${funnelShoulder} V${nozzleExit} H${260+nozzleWidth/2} V${funnelShoulder} L263.5 449 Z`;
  const nozzleBody=`M242 435 L257.2 ${nozzleExit} H262.8 L278 435 Z`;
  const nozzleEntrance='M254.5 435 Q256.5 435 256.5 437 V449 H263.5 V437 Q263.5 435 265.5 435 Z';
  const bowdenPath = path.replace("L260 449", "L260 435");
  let svg = `<text x="518" y="402" fill="var(--scene-text, #304c65)">MMU</text><path id="extruder-body" d="M250 205 H242 Q240 205 240 207 V435 H280 V207 Q280 205 278 205 H270" fill="var(--scene-body, #edf2f6)" stroke="var(--scene-border, #96abba)"/><rect x="213" y="375" width="94" height="58" rx="5" fill="var(--scene-heater, #c4d3df)"/><rect id="nozzle-collar" x="240" y="433" width="40" height="2" fill="var(--scene-nozzle, #9cafbe)"/><path id="nozzle-body" d="${nozzleBody}" fill="var(--scene-nozzle, #9cafbe)"/><path id="nozzle-bore" d="${nozzleFunnel}" fill="var(--scene-tube, #edf4f8)"/><g mask="url(#bowden-end-fade)"><path id="tube" d="${path}" fill="none" stroke="none"/><g mask="url(#bowden-tube-fade)"><g${s.devices[`${s.unit}:buffer`] ? ' mask="url(#buffer-tube-gap)"' : ''}><path d="${bowdenPath}" fill="none" stroke="var(--scene-tube-border, #b1c4d2)" stroke-width="20"/><path d="${bowdenPath}" fill="none" stroke="var(--scene-tube, #edf4f8)" stroke-width="14"/></g></g><path id="nozzle-entrance" d="${nozzleEntrance}" fill="var(--scene-tube, #edf4f8)"/><path id="filament" d="${path}" fill="none" stroke="#${s.filament?.incoming_color || "808182"}" stroke-width="7"/></g><text ${dimensionLink('Bowden')} id="bowden-length" x="660" y="100" text-anchor="start" fill="var(--scene-muted, #6e8190)" font-size="13">${bowdenLengthLabel(s).includes('Uncalibrated') ? '<tspan x="660">Bowden</tspan><tspan x="660" dy="16">Uncalibrated length</tspan>' : bowdenLengthLabel(s)}</text>`;
  $("scene").innerHTML = svg;
  const tube = $("tube"),
    length = tube.getTotalLength();
  const mmuExitFraction = 200 / length; // Exit sits 20px above the MMU box.
  const toolheadFraction=(length-toolheadHeight)/length;
  const progress = (p) =>
    p < 0
      ? Math.max(0, ((p - Math.min(-100, (s.layout.mmu_entry ?? -150)-40)) / -Math.min(-100, (s.layout.mmu_entry ?? -150)-40)) * mmuExitFraction)
      : p < sensorEntry
        ? mmuExitFraction + ((toolheadFraction-sensorGap/length-mmuExitFraction) * p) / sensorEntry
        : p < entry
          ? toolheadFraction-sensorGap/length+(sensorGap/length)*(p-sensorEntry)/(entry-sensorEntry)
        : Math.min(1, toolheadFraction + (1-toolheadFraction) * toolheadProgress(s,p));
  if (Number.isFinite(s.extruder_target_temperature))
    svg += `<text id="extruder-temperature" x="218" y="426" font-size="9" fill="var(--scene-text, #304c65)" data-tooltip="Extruder target temperature">${Number(s.extruder_target_temperature.toFixed(1))}°C</text>`;
  const unitSensors = Object.entries(s.sensors).filter(([name]) => {
    if (name === `${s.unit}:mmu_gear_touch`) return true;
    if (s.active_sensors) return s.active_sensors.includes(name);
    if (s.unit_sensors && !s.unit_sensors.includes(name)) return false;
    const gateSuffix = name.split(':').at(-1).match(/^mmu_.*_(\d+)$/);
    return !gateSuffix || Number(gateSuffix[1]) === s.gate;
  });
  const isTouchSensor=name=>/(?:^|:)mmu_gear_touch(?:_\d+)?$/.test(name);
  const encoderAnchor = encoderReference(s, unitSensors);
  const gateSlots = Object.fromEntries(Object.entries(gateDevicePositions(s, unitSensors)).map(([key,value])=>[key,Number.isFinite(value) ? value+100 : value]));
  const sensorPoint = (name, device) => {
    if (isTouchSensor(name)) return {x:260,y:420};
    const point = tube.getPointAtLength(progress(device.position)*length);
    const short = name.split(':').at(-1);
    if (/^mmu_entry(?:_\d+)?$/.test(short)) point.y -= 93;
    if (/^mmu_exit(?:_\d+)?$/.test(short)) { point.x = 620; point.y = gateSlots.exit; }
    if (short === 'mmu_shared_exit') { point.x = 620; point.y = gateSlots.sharedExit; }
    return point;
  };
  const isBufferSensor = name => /(?:^|:)filament_(compression|tension)$/.test(name);
  const sensorFill = state => state === true ? '#399b64' : state === false ? '#fff' : '#9ba5ae';
  const encoderPoint = () => ({x:620, y:gateSlots.encoder ?? 360});
  const tipAtPathDistance = distance => {
    const target = Math.max(0,distance)/length;
    let low = Math.min(-100,(s.layout.mmu_entry ?? -150)-40), high = end;
    for (let i=0;i<32;i++) { const mid=(low+high)/2; if(progress(mid)<target) low=mid; else high=mid; }
    return (low+high)/2;
  };
  let schematicDistance = position => progress(position)*length;
  let schematicTip = null;
  // UNKNOWN uses an illustrative endpoint between the MMU gears and its outlet.
  if (s.gate >= 0 && s.filament_pos === -1 && e.kind !== 'movement') tip = tipAtPathDistance(570-405);
  // Encoder loading keeps one reference across START OF BOWDEN bookkeeping.
  if (s.gate >= 0 && s.filament_pos >= 0 && tip < entry && s.encoder_reference?.endstop === 'encoder') {
    tip = tipAtPathDistance(encoderFilamentDistance(events,e,s,tip,p=>progress(p)*length,570-encoderPoint().y));
  } else if (s.gate >= 0 && s.filament_pos >= 0 && tip < entry) {
    const endstop = s.encoder_reference?.endstop?.split(':').at(-1);
    const sensor = unitSensors.find(([name,d]) => Number.isFinite(d.position) &&
      [endstop,`${endstop}_${gate}`].includes(name.split(':').at(-1)));
    if (sensor) {
      schematicDistance = position => switchFilamentDistance(position,sensor[1].position,
        s.encoder_reference?.parking || 0,entry,570-sensorPoint(...sensor).y,p=>progress(p)*length,
        encoderAnchor?.name === sensor[0] && s.devices?.[`${s.unit}:encoder`]
          ? {position:sensor[1].position+encoderAnchor.offset,distance:570-encoderPoint().y} : null);
      schematicTip = schematicDistance(tip);
      tip = tipAtPathDistance(schematicTip);
    }
  }
  const renderCone = $('show-filament-shape')?.checked ?? true;
  const tipScale = toolheadHeight/(end-entry);
  const coneOffset = renderCone && s.tip_shapes?.[gate] === 'cone' ? tipScale : 0;
  const pathDistance = position => progress(position)*length;
  // Apply recorded transitions where the drawn tip crosses, after schematic Bowden scaling.
  const displayedSensors=unitSensors.map(([name,device])=>{
    const before=e.before?.sensors[name], after=e.state.sensors[name];
    if (playing && e.kind === 'movement' && before && after && before.state !== after.state &&
        ['extruder','toolhead'].includes(name.split(':').at(-1))) {
      return [name,{...device,state:pathDistance(tip)>=pathDistance(device.position)-1e-6}];
    }
    return [name,device];
  });
  const presenceSensors = displayedSensors.filter(([name,d]) => !isBufferSensor(name) &&
    !name.includes('encoder') && !isTouchSensor(name) && d.position !== null).map(([name,d]) => {
      const short=name.split(':').at(-1);
      const position = /^mmu_(?:entry|exit)(?:_\d+)?$/.test(short) || short === 'mmu_shared_exit'
        ? 570-sensorPoint(name,d).y : pathDistance(d.position);
      return {...d,name,position};
    });
  const fragments = s.retained_segment ? [{start:pathDistance(s.retained_segment.start),end:pathDistance(s.retained_segment.end)}] : [];
  const constrained = constrainFilament(schematicTip ?? pathDistance(tip),
    s.tails[gate] === null ? null : pathDistance(s.tails[gate]), presenceSensors, fragments, s.sensor_conflicts || []);
  const focus = s.gate_focus ?? s.gate;
  // Start just inside the upper spool rim; the fixed fade hides the insertion point.
  const fullTipOpacity=s.gate_full_opacity ?? 1;
  const slidingRack=fullTipOpacity===0;
  const spoolTip=570-504;
  const selectedTip=constrained.tip+(Math.min(constrained.tip,spoolTip)-constrained.tip)*(s.gate_retraction || 0);
  tip = tipAtPathDistance(selectedTip);
  const shapedTipOpacity=fullTipOpacity*Math.max(0,Math.min(1,(selectedTip-(570-502))/28));
  const start = s.tails[gate] === null ? 0 : pathDistance(s.tails[gate]), stop = Math.max(0,selectedTip-coneOffset);
  const macroUnit=s.units?.indexOf(s.unit) ?? 0;
  const macroParams=s.configuration?.parameters?.[macroUnit] || {};
  $('scene-macros').innerHTML=[['purge_macro','Purge macro'],['form_tip_macro','Tip-forming macro']].map(([key,label])=>{
    const name=macroParams[key];
    const link=offline ? '' : `role="button" tabindex="0" data-parameter="${key}" data-macro="${escape(name || '')}" data-unit="${macroUnit}" aria-label="Browse ${label} variables"`;
    return `<div class="macro-label">${label}</div><span class="macro-name" ${link}>${escape(name === undefined ? 'Not recorded' : name || 'None')}</span>`;
  }).join('');
  const warningBox = $('filament-warning');
  warningBox.hidden = !constrained.warnings.length;
  $('filament-warning-message').textContent = constrained.warnings.length
    ? 'Displayed tip adjusted to sensor readings: '+constrained.warnings.join('; ') : '';
  $("filament").setAttribute(
    "stroke-dasharray",
    `0 ${start} ${Math.max(0, stop - start)} ${length + 1}`,
  );
  if (s.tips.length) {
    const unitGates=s.unit_gates || s.tips.map((_,index)=>index);
    const grid=spoolGrid(unitGates,s.gate>=0 ? focus : unitGates[0]);
    const visible=[...grid.lanes].sort((a,b)=>a.row-b.row || b.offset-a.offset);
    const spoolViewAngle=Number($('spool-angle').value)*Math.PI/180;
    const edgePadding=17*3.3*Math.cos(spoolViewAngle)+(34/3)*3.3*Math.sin(spoolViewAngle)/2+16;
    const enclosureLeft=gateLaneX(-(grid.columns-1)/2)+4-edgePadding;
    const enclosureRight=gateLaneX((grid.columns-1)/2)+4+edgePadding;
    const lastRowY=Math.max(0,grid.rows-1)*150;
    const enclosureBottom=693+lastRowY;
    $('scene').setAttribute('viewBox',`0 0 ${Math.max(850,enclosureRight+14)} ${enclosureBottom+21}`);
    const coverPath=`M${enclosureLeft} ${585+lastRowY} V515 Q${enclosureLeft} 470 ${enclosureLeft+45} 470 H${enclosureRight-45} Q${enclosureRight} 470 ${enclosureRight} 515 V${585+lastRowY} Z`;
    svg += `<defs><radialGradient id="spool-backlight" cx=".5" cy=".85" r=".65"><stop stop-color="#fff" stop-opacity="1"/><stop offset=".45" stop-color="#fff2b8" stop-opacity=".8"/><stop offset="1" stop-color="#fff2b8" stop-opacity="0"/></radialGradient><linearGradient id="spool-shelf-fade" x1="0" y1="0" x2="0" y2="1"><stop stop-color="color-mix(in srgb, var(--scene-body, #edf2f6), white 18%)"/><stop offset=".12" stop-color="var(--scene-body, #edf2f6)"/><stop offset=".65" stop-color="var(--scene-body, #edf2f6)"/><stop offset="1" stop-color="var(--scene-body, #edf2f6)" stop-opacity="0"/></linearGradient><linearGradient id="spool-glass" x1="0" y1="0" x2=".3" y2="1"><stop stop-color="white" stop-opacity=".14"/><stop offset=".25" stop-color="white" stop-opacity=".025"/><stop offset="1" stop-color="black" stop-opacity=".1"/></linearGradient><linearGradient id="spool-cabinet" x1="0" y1="0" x2="0" y2="1"><stop stop-color="color-mix(in srgb, var(--scene-body, #edf2f6), white 18%)"/><stop offset=".13" stop-color="var(--scene-body, #edf2f6)"/><stop offset="1" stop-color="color-mix(in srgb, var(--scene-body, #edf2f6), black 18%)"/></linearGradient><clipPath id="spool-cover-clip"><path d="${coverPath}"/></clipPath></defs><path d="${coverPath}" fill="color-mix(in srgb, var(--scene-body, #edf2f6), black 12%)"/>`;
    svg += '<defs><filter id="spool-separation-shadow" x="-30%" y="-20%" width="160%" height="145%"><feDropShadow dx="1.2" dy=".8" stdDeviation=".6" flood-color="#000" flood-opacity=".65"/></filter></defs><g id="espooler-indicators" clip-path="url(#spool-cover-clip)">';
    const spoolStyle=document.querySelector('input[name=spool-style]:checked').value;
    const solidSpool=spoolStyle.endsWith('-solid');
    const spoolFaceScale=Math.max(.001,Math.cos(spoolViewAngle));
    const spoolDepth=34/3, spoolHubRadius=6.4, spoolBoreRadius=5.8, spoolScale=3.3;
    // Compensate for the face transform so axial depth projects with sin(view angle).
    const spoolDepthOffset=spoolDepth*Math.sin(spoolViewAngle)/spoolFaceScale;

    for (const {index,offset,row,lift} of visible) {
      const operation=s.espoolers?.[index];
      const active=operation?.output_power>0 && ['assist','rewind'].includes(operation.direction);
      const visibility=spoolArrowVisibility.get(index) ?? (active ? 1 : 0);
      const fade=visibility*visibility*(3-2*visibility);
      // Offset the winding slightly right of the filament lane.
      const x=gateLaneX(offset)+4-spoolDepth*Math.sin(spoolViewAngle)*spoolScale/2;
      const y=549.6+row*150-lift;
      const angle=spoolAngles.get(index) || 0;
      const color=s.gate_colors[index] || '808182';
      const percent=spoolFill(s,index);
      // Scale winding area above the core, leaving a narrow clearance at the rim when full.
      const fullWindingRadius=16;
      const fillRadius=Math.sqrt(spoolHubRadius**2+(fullWindingRadius**2-spoolHubRadius**2)*percent/100);
      const windingRadius=fillRadius;
      const title=`Gate ${index} · ${percent}% filament · ${active ? operation.direction+' · '+Math.round(operation.output_power*100)+'% output' : 'Stopped'}`;
      const shelfY=575+row*150;
      svg += `<defs><linearGradient id="spool-shelf-edge-${index}" gradientUnits="userSpaceOnUse" x1="0" y1="${shelfY-3}" x2="0" y2="${shelfY}"><stop stop-color="white"/><stop offset="1" stop-color="black"/></linearGradient><mask id="spool-shelf-mask-${index}" maskUnits="userSpaceOnUse" x="${x-80}" y="${y-70}" width="160" height="150"><rect x="${x-80}" y="${y-70}" width="160" height="150" fill="url(#spool-shelf-edge-${index})"/></mask></defs><g mask="url(#spool-shelf-mask-${index})">`;
      svg += `<g data-part="espooler" data-gate="${index}" data-active="${active}" data-fill-percent="${percent}" data-tooltip="${escape(title)}" transform="translate(${x} ${y}) scale(${spoolScale})" filter="url(#spool-separation-shadow)">`;
      // Lighting stays fixed while the spokes turn underneath it.
      const material=spoolStyle.startsWith('dark-') ? '#343638' : 'var(--spool-material, #b49164)';
      // Annular windows flare outward; rounded joins soften each of the four corners.
      const windowPoint=(radius,degrees)=>{
        const radians=degrees*Math.PI/180;
        return `${radius*Math.sin(radians)} ${-radius*Math.cos(radians)}`;
      };
      const inner=8, outer=14.4, corner=.55;
      const windowPath=`M${windowPoint(inner+corner,-16)} L${windowPoint(outer-corner,-16)} Q${windowPoint(outer,-16)} ${windowPoint(outer,-13)} A${outer} ${outer} 0 0 1 ${windowPoint(outer,13)} Q${windowPoint(outer,16)} ${windowPoint(outer-corner,16)} L${windowPoint(inner+corner,16)} Q${windowPoint(inner,16)} ${windowPoint(inner,12)} A${inner} ${inner} 0 0 0 ${windowPoint(inner,-12)} Q${windowPoint(inner,-16)} ${windowPoint(inner+corner,-16)} Z`;
      const spokes=(front=false)=>`<circle r="17" fill="white"/><g transform="rotate(${angle})">${[30,90,150,210,270,330].filter(rotation=>!front || rotation!==270).map(rotation=>`<path transform="rotate(${rotation})" d="${windowPath}" fill="black"/>`).join('')}</g>`;
      svg += `<defs><filter id="spool-cutout-shadow-${index}" x="-20%" y="-20%" width="140%" height="140%"><feDropShadow dx="1.1" dy=".8" stdDeviation=".25" flood-color="#000" flood-opacity=".65"/></filter><linearGradient id="spool-face-${index}" x1="0" y1="0" x2="1" y2="1"><stop stop-color="color-mix(in srgb, ${material}, white 32%)"/><stop offset=".45" stop-color="${material}"/><stop offset="1" stop-color="color-mix(in srgb, ${material}, black 38%)"/></linearGradient><linearGradient id="spool-edge-${index}" x1="0" y1="0" x2="1" y2="1"><stop stop-color="color-mix(in srgb, ${material}, white 60%)"/><stop offset=".4" stop-color="${material}"/><stop offset="1" stop-color="color-mix(in srgb, ${material}, black 65%)"/></linearGradient><linearGradient id="spool-bore-${index}" x1="0" y1="0" x2="1" y2="1"><stop stop-color="color-mix(in srgb, ${material}, black 88%)"/><stop offset=".6" stop-color="color-mix(in srgb, ${material}, black 65%)"/><stop offset="1" stop-color="color-mix(in srgb, ${material}, black 35%)"/></linearGradient><mask id="spool-spokes-${index}" maskUnits="userSpaceOnUse" x="-18" y="-18" width="36" height="36">${spokes()}</mask><mask id="spool-front-spokes-${index}" maskUnits="userSpaceOnUse" x="-18" y="-18" width="36" height="36">${spokes(true)}</mask></defs>`;
      // The dot lives on the front face, so its orbit and shape share the face projection.
      const faceDot=`<g data-part="spool-rotation-dot" transform="rotate(${angle})"><circle cx="-13.5" cy="0" r="2.3" fill="${spoolStyle.startsWith('dark-') ? '#f1f0e8' : '#842c2c'}" stroke="color-mix(in srgb, ${material}, black 35%)" stroke-width=".25"/></g>`;
      svg += `<g transform="scale(${spoolFaceScale} 1)"><g data-part="spool-rear-face" transform="translate(${spoolDepthOffset} 0)"><circle r="17" fill="none" stroke="url(#spool-edge-${index})" stroke-width="1"/><g filter="url(#spool-cutout-shadow-${index})"><circle r="17" fill="url(#spool-face-${index})" ${solidSpool ? '' : `mask="url(#spool-spokes-${index})"`}/><circle r="${spoolHubRadius}" fill="${material}"/></g><circle r="${spoolBoreRadius}" fill="url(#spool-bore-${index})"/></g><defs><linearGradient id="spool-winding-shade-${index}" x1="0" y1="0" x2="0" y2="1"><stop stop-color="color-mix(in srgb, #${color}, black 25%)"/><stop offset=".4" stop-color="#${color}"/><stop offset=".65" stop-color="color-mix(in srgb, #${color}, white 15%)"/><stop offset="1" stop-color="color-mix(in srgb, #${color}, black 35%)"/></linearGradient></defs><path data-part="spool-core-tube" d="M0 -${spoolHubRadius} H${spoolDepthOffset} A${spoolHubRadius} ${spoolHubRadius} 0 0 1 ${spoolDepthOffset} ${spoolHubRadius} H0 Z" fill="${material}"/><defs><linearGradient id="spool-core-contact-fade-${index}" gradientUnits="userSpaceOnUse" x1="0" y1="${-spoolHubRadius}" x2="0" y2="${spoolHubRadius}"><stop stop-color="black" stop-opacity="0"/><stop offset=".25" stop-color="black" stop-opacity=".55"/><stop offset=".75" stop-color="black" stop-opacity=".55"/><stop offset="1" stop-color="black" stop-opacity="0"/></linearGradient></defs><path data-part="spool-core-contact-shadow" d="M${spoolDepthOffset} -${spoolHubRadius} A${spoolHubRadius} ${spoolHubRadius} 0 0 1 ${spoolDepthOffset} ${spoolHubRadius}" fill="none" stroke="url(#spool-core-contact-fade-${index})" stroke-width="1.2"/><path d="M0 -${windingRadius} H${spoolDepthOffset} A${windingRadius} ${windingRadius} 0 0 1 ${spoolDepthOffset} ${windingRadius} H0 Z" fill="${percent ? `url(#spool-winding-shade-${index})` : 'none'}"/>${Array.from({length:percent ? 18 : 0},(_,i)=>(i+1)*spoolDepthOffset/19).map(depth=>`<path d="M${depth} -${windingRadius} A${windingRadius} ${windingRadius} 0 0 1 ${depth} ${windingRadius}" fill="none" stroke="color-mix(in srgb, #${color}, black 50%)" stroke-width=".06" opacity=".85"/><path d="M${depth+.18} -${windingRadius} A${windingRadius} ${windingRadius} 0 0 1 ${depth+.18} ${windingRadius}" fill="none" stroke="color-mix(in srgb, #${color}, white 55%)" stroke-width=".06" opacity=".75"/>`).join('')}`;
      if (percent>0) {
        // Contact shadow from the front flange, fading into the first winding turns.
        svg += `<defs><linearGradient id="spool-contact-shadow-${index}" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="${spoolDepthOffset*.2}" y2="0"><stop stop-color="black" stop-opacity=".75"/><stop offset=".35" stop-color="black" stop-opacity=".4"/><stop offset="1" stop-color="black" stop-opacity="0"/></linearGradient><clipPath id="spool-contact-clip-${index}"><path d="M0 -${windingRadius} H${spoolDepthOffset} A${windingRadius} ${windingRadius} 0 0 1 ${spoolDepthOffset} ${windingRadius} H0 Z"/></clipPath></defs><rect data-part="spool-contact-shadow" x="0" y="${-windingRadius}" width="${spoolDepthOffset*.2}" height="${windingRadius*2}" fill="url(#spool-contact-shadow-${index})" clip-path="url(#spool-contact-clip-${index})"/>`;
      }
      svg += `<g><circle r="17" fill="none" stroke="url(#spool-edge-${index})" stroke-width="1"/>${percent ? `<circle r="${fillRadius}" fill="#${color}"/>` : ''}<g filter="url(#spool-cutout-shadow-${index})"><circle r="17" fill="url(#spool-face-${index})" ${solidSpool ? '' : `mask="url(#spool-front-spokes-${index})"`}/>${faceDot}<circle r="${spoolHubRadius}" fill="${material}"/></g><circle data-part="spool-core-bore" r="${spoolBoreRadius}" fill="url(#spool-bore-${index})"/></g>`;
      svg += '</g>';
      if (fade>0) {
        const direction=active ? operation.direction : spoolDirections.get(index);
        // Keep the arrow on the near side of the full winding, independent of spool rotation.
        const arrowX=spoolDepth*Math.sin(spoolViewAngle)/2+(fullWindingRadius+.6)*Math.cos(spoolViewAngle)-.8-2/spoolScale;
        svg += `<defs><linearGradient id="spool-arrow-fade-${index}" gradientUnits="userSpaceOnUse" x1="0" y1="-16.6" x2="0" y2="-5"><stop stop-color="white" stop-opacity="0"/><stop offset=".4" stop-color="white" stop-opacity=".5"/><stop offset="1" stop-color="white"/></linearGradient><mask id="spool-arrow-mask-${index}" maskUnits="userSpaceOnUse" x="-6" y="-18" width="12" height="23"><rect x="-6" y="-18" width="12" height="23" fill="url(#spool-arrow-fade-${index})"/></mask></defs><g data-part="espooler-direction" data-direction="${direction}" opacity="${fade}" transform="translate(${arrowX} 0) scale(1 ${direction==='rewind' ? -1 : 1})" pointer-events="none"><path d="M-3.2 -16.6 C.8 -15.2 2.2 -11 2.1 -7 L3.7 -7 L1 -2.5 L-2.2 -6.8 L-.7 -6.8 C-.2 -10.3 -.8 -14.2 -3.2 -16.6 Z" fill="white" fill-opacity=".55" stroke="white" stroke-opacity=".9" stroke-width=".4" stroke-linejoin="round" mask="url(#spool-arrow-mask-${index})"/></g>`;
      }
      if (index===s.gate) {
        const glow=Math.max(0,1-Math.abs(index-focus));
        svg += `<ellipse data-part="selected-spool-backlight" cx="${spoolDepth*Math.sin(spoolViewAngle)/2+2}" cy="9" rx="7" ry="12" opacity="${glow}" fill="url(#spool-backlight)" pointer-events="none"/>`;
      }
      svg += '</g></g>';
    }
    svg += '</g>';
    svg += `<path data-part="spool-glass-cover" d="${coverPath}" fill="url(#spool-glass)" stroke="color-mix(in srgb, var(--scene-border, #96abba), transparent 55%)" stroke-width=".8"/><rect data-part="spool-cabinet" x="${enclosureLeft}" y="${575+lastRowY}" width="${enclosureRight-enclosureLeft}" height="118" rx="9" fill="url(#spool-cabinet)" stroke="color-mix(in srgb, var(--scene-border, #96abba), transparent 60%)" stroke-width=".6"/>`;
    for (let row=0; row<grid.rows-1; row++) {
      svg += `<rect data-part="spool-row-shelf" x="${enclosureLeft}" y="${575+row*150}" width="${enclosureRight-enclosureLeft}" height="64" rx="5" fill="url(#spool-shelf-fade)"/>`;
    }
    for (const {index,offset,row} of visible) {
      const status=s.gate_status?.[index];
      const border=status===0 ? '#858b91' : status>0 ? '#208b35' : '#d58a30';
      const selected=index===s.gate;
      const x=gateLaneX(offset)+8;
      svg += `<g data-part="gate-status-pill" data-gate="${index}" data-tooltip="Gate ${index} · ${status===0 ? 'Empty' : status>0 ? 'Available' : 'Unknown'}"><rect x="${x-16}" y="${587+row*150}" width="32" height="18" rx="9" fill="${selected ? '#36ca42' : 'color-mix(in srgb, var(--scene-body, #edf2f6), black 12%)'}" stroke="${border}" stroke-width="2"/><text x="${x}" y="${596+row*150}" dominant-baseline="central" text-anchor="middle" font-size="11" font-weight="700" fill="${selected ? '#102514' : 'var(--scene-text, #304c65)'}">${index}</text></g>`;
    }
  }
  let attachedTipEdge='';
  const shapedTip = (position, color, part, hairLength=s.hair_lengths?.[gate] ?? 15) => {
    const hairScale = tipScale;
    const along = distance => distance <= length ? tube.getPointAtLength(distance)
      : {x:260,y:Math.min(nozzleExit,449+distance-length)};
    // Keep the cone at toolhead scale even along the compressed Bowden.
    const center=progress(position)*length;
    const base = along(Math.max(0,center-tipScale)), nose = along(center+tipScale);
    const angle = Math.atan2(nose.y-base.y,nose.x-base.x), dx = -Math.sin(angle)*3.5, dy = Math.cos(angle)*3.5;
    // Overlap the cone slightly, then taper the surviving hair.
    const hairStart = center+tipScale;
    const hairLimit=progress(end-(s.filament?.residual_mm || 0))*length;
    const hair = Array.from({length:32},(_,i)=>along(Math.min(hairLimit,hairStart+(-.4+(hairLength+.4)*i/31)*hairScale)));
    const sides = [1,-1].map(side=>hair.map((p,i)=>{
      const before=hair[Math.max(0,i-1)], after=hair[Math.min(hair.length-1,i+1)];
      const tangent=Math.atan2(after.y-before.y,after.x-before.x);
      const radius=.7*(1-i/31)+.04;
      return {x:p.x-side*Math.sin(tangent)*radius,y:p.y+side*Math.cos(tangent)*radius};
    }));
    const outline=[...sides[0],...sides[1].reverse()];
    const edge=position+1 >= end-(s.filament?.residual_mm || 0) ? `<path data-part="cone-edge" opacity="${s.cone_edge_opacity ?? (s.tip_shapes?.[gate] === 'cone' || part === 'fragment-tip' ? 1 : 0)}" d="M${base.x+dx} ${base.y+dy} L${nose.x} ${nose.y} L${base.x-dx} ${base.y-dy}" fill="none" stroke="color-mix(in srgb, #${color}, black 40%)" stroke-width="0.6"/>` : '';
    if (part === 'attached-tip') attachedTipEdge=edge;
    return `<g data-part="${part}">${hairStart < hairLimit ? `<path data-part="filament-hair" d="${outline.map((p,i)=>`${i?'L':'M'}${p.x} ${p.y}`).join(' ')} Z" fill="#${color}"/>` : ''}<path d="M${base.x+dx} ${base.y+dy} L${nose.x} ${nose.y} L${base.x-dx} ${base.y-dy} Z" fill="#${color}"/>${part === 'attached-tip' ? '' : edge}</g>`;
  };
  svg += `<defs><linearGradient id="bowden-fade-gradient" gradientUnits="userSpaceOnUse" x1="0" x2="0" y1="474" y2="502"><stop stop-color="white"/><stop offset="1" stop-color="black"/></linearGradient><mask id="bowden-end-fade" maskUnits="userSpaceOnUse" x="0" y="0" width="850" height="700"><rect width="850" height="700" fill="white"/><rect x="600" y="474" width="40" height="226" fill="url(#bowden-fade-gradient)"/></mask></defs>`;
  svg += `<defs><linearGradient id="bowden-tube-gradient" gradientUnits="userSpaceOnUse" x1="0" x2="0" y1="458" y2="490"><stop stop-color="white"/><stop offset=".65" stop-color="#777"/><stop offset="1" stop-color="black"/></linearGradient><mask id="bowden-tube-fade" maskUnits="userSpaceOnUse" x="0" y="0" width="850" height="700"><rect width="850" height="700" fill="white"/><rect x="600" y="458" width="40" height="242" fill="url(#bowden-tube-gradient)"/></mask></defs>`;
  if (syncForDisplay(e, s, Boolean(playing) || !live || paused)) {
    svg += `<g id="synced-indicator" aria-label="Extruder and MMU gears synced"><g transform="translate(724 395) rotate(-35)" fill="none" stroke="#53b9b0" stroke-width="2.2"><rect x="-12" y="-5" width="15" height="10" rx="5"/><rect x="-3" y="-5" width="15" height="10" rx="5"/></g><text x="724" y="416" text-anchor="middle" font-size="10" fill="var(--scene-text, #304c65)">Synced</text></g>`;
  }
  // Nozzle-relative reference guides, matching the documentation diagrams.
  const references = toolheadReferences(s).map(ref => ({...ref,
    y: tube.getPointAtLength(progress(ref.position) * length).y})).sort((a, b) => a.y - b.y);
  let previousLabelY = -Infinity;
  const mm = value => Number(value.toFixed(1));
  for (const ref of references) {
    if (ref.label === 'Extruder sensor' && ref.gap != null) {
      const gearY=references.find(item=>item.label === 'Extruder entry').y;
      const top=Math.min(ref.y,gearY)+2, bottom=Math.max(ref.y,gearY)-2;
      svg += `<g class="toolhead-reference"><path d="M285 ${ref.y} H330 M315 ${top} V${bottom} M312 ${top+4} L315 ${top} L318 ${top+4} M312 ${bottom-4} L315 ${bottom} L318 ${bottom-4}" fill="none" stroke="var(--scene-border, #96abba)"/><text ${dimensionLink('Extruder to entry')} x="323" y="${(ref.y+gearY)/2}" dominant-baseline="middle" font-size="10" fill="var(--scene-muted, #6e8190)">Extruder to entry · ${mm(ref.gap)}mm</text></g>`;
      continue;
    }
    const labelY = Math.max(ref.y, previousLabelY + 17);
    previousLabelY = labelY;
    svg += `<g class="toolhead-reference"><path d="M${ref.label === 'Extruder entry' ? 306 : 285} ${ref.y} H315 L330 ${labelY}" fill="none" stroke="var(--scene-border, #96abba)" stroke-width="1"/><text ${dimensionLink(ref.label)} x="336" y="${labelY}" dominant-baseline="middle" font-size="10" fill="var(--scene-muted, #6e8190)"><tspan class="reference-title">${ref.label} · ${mm(ref.distance)}mm</tspan></text></g>`;
  }
  const cooling = coolingTubeMeasurements(s);
  if (cooling) {
    const startY = tube.getPointAtLength(progress(cooling.start) * length).y;
    const endY = tube.getPointAtLength(progress(cooling.end) * length).y;
    const midY = (startY + endY) / 2;
    svg += `<g class="cooling-measurements" fill="var(--scene-muted, #6e8190)" font-size="10"><path d="M285 ${startY} H330 M285 ${endY} H330" fill="none" stroke="var(--scene-border, #96abba)"/><path data-part="cooling-length-arrow" d="M315 ${startY-2} V${endY+2} M312 ${endY+6} L315 ${endY+2} L318 ${endY+6} M312 ${startY-6} L315 ${startY-2} L318 ${startY-6}" fill="none" stroke="var(--scene-border, #96abba)"/><text ${dimensionLink('Cooling tube position')} x="336" y="${startY}" dominant-baseline="middle">Cooling tube position · ${mm(cooling.position)}mm</text><text ${dimensionLink('Cooling tube length')} x="323" y="${midY}" dominant-baseline="middle">Cooling tube length · ${mm(cooling.length)}mm</text></g>`;
  }
  for (const [name, d] of displayedSensors) {
    if (isBufferSensor(name)) continue;
    const suffix = name.split(':').at(-1).match(/^mmu_.*_(\d+)$/);
    if (suffix && Number(suffix[1]) !== gate) continue;
    if (d.position === null && !isTouchSensor(name)) continue;
    const pt = sensorPoint(name, d);
    const short = name.split(':').at(-1);
    const sensorLabel = isTouchSensor(name) ? 'touch' : /^mmu_entry(?:_\d+)?$/.test(short) ? 'entry' : /^mmu_exit(?:_\d+)?$/.test(short) ? 'exit' : short === 'mmu_shared_exit' ? 'shared exit' : short;
    svg += `<g class="sensor-marker${d.state === null ? ' sensor-disabled' : ''}" data-sensor="${escape(name)}"><text x="${isTouchSensor(name) || pt.x > 400 ? pt.x + 29 : pt.x - 29}" y="${sensorLabel === 'entry' ? pt.y : pt.y + 4}" dominant-baseline="${sensorLabel === 'entry' ? 'central' : 'auto'}" text-anchor="${isTouchSensor(name) || pt.x > 400 ? "start" : "end"}" font-size="10" fill="var(--scene-text, #456078)">${escape(sensorLabel)}</text><circle cx="${pt.x + 16}" cy="${pt.y}" r="5" fill="${d.state === true ? "#399b64" : d.state === false ? "#fff" : "#9ba5ae"}" stroke="#698394"/></g>`;
    svg += homingRing(name,pt.x+16,pt.y);
    if (sensorPulse?.sensor === name) {
      const elapsed = (animationNow() - sensorPulse.start) / 650;
      if (elapsed >= 0 && elapsed < 1) {
        const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
        svg += `<circle data-part="sensor-pulse" cx="${pt.x + 16}" cy="${pt.y}" r="${reduced ? 10 : 7 + elapsed * 15}" fill="none" stroke="${sensorPulse.reverse ? 'var(--sensor-clear-pulse)' : '#48c982'}" stroke-width="2.5" opacity="${1-elapsed}"/>`;
      }
    }
  }
  for (const [x, y, type] of [
    [620, 395, "gear"],
    [260, tube.getPointAtLength(progress(s.layout.extruder_gears ?? entry) * length).y, "extruder"],
  ]) {
    const names = s.drive_motors?.[type] || Object.keys(s.motors).filter(n => n.includes(type));
    // Coupled motors share travel; do not add their distances or inactive gates.
    const distance = names.length ? (s.motors[names[0]] || 0) : 0;
    // Both gear pairs use the toolhead scale so equal travel gives equal rotation.
    const pixelsPerMm = .27 * length / (end-entry);
    const angle = gearAngle(distance, pixelsPerMm, 14.25);
    const gripState = e.kind === 'movement' && (playing || !live || paused) ? e.before : s;
    const spacing = type === 'gear' && gripState?.mmu_gripped === false ? 26.25 : 17.25;
    for (const side of [-1, 1])
      svg += `<g data-part="${type}-wheel" transform="translate(${x + side * spacing} ${y}) rotate(${(type === "extruder" ? -1 : 1) * side * angle}) scale(.75)"><circle r="19" fill="var(--scene-gear, #d8e4ed)" stroke="var(--scene-border, #6d899f)"/><path d="M-14 0 H14 M0 -14 V14" stroke="var(--scene-spoke, #526f86)"/><circle class="gear-center" r="3" fill="var(--scene-spoke, #526f86)"/></g>`;
  }
  for (const [name, d] of Object.entries(s.devices)) {
    if (name.includes(':') && name.split(':')[0] !== s.unit) continue;
    if (name.endsWith(":encoder")) {
      const pt = encoderPoint();
      if (encoderAnchor) {
        const sensor = unitSensors.find(([name]) => name === encoderAnchor.name);
        const anchor = sensorPoint(...sensor);
        const top = Math.min(pt.y,anchor.y), bottom = Math.max(pt.y,anchor.y);
        const value = `${encoderAnchor.offset > 0 ? '+' : ''}${mm(encoderAnchor.offset)}mm`;
        const unitIndex = s.units?.indexOf(s.unit) ?? 0;
        if (Object.hasOwn(s.configuration?.parameters?.[unitIndex] || {}, 'gate_endstop_to_encoder'))
        svg += `<g id="encoder-offset" fill="var(--scene-muted, #6e8190)" font-size="10"><path d="M714 ${anchor.y} H726 M714 ${pt.y} H726 M720 ${top+2} V${bottom-2} M717 ${top+6} L720 ${top+2} L723 ${top+6} M717 ${bottom-6} L720 ${bottom-2} L723 ${bottom-6}" fill="none" stroke="var(--scene-border, #96abba)"/><text ${dimensionLink('Encoder offset')} x="728" y="${(top+bottom)/2-6.5}" text-anchor="start" dominant-baseline="middle"><tspan class="encoder-offset-title">Gate endstop to</tspan><tspan class="encoder-offset-value" x="728" dy="13" text-anchor="middle">encoder · ${value}</tspan></text></g>`;
      }
      // Device positions are interpolated alongside each captured move.
      const angle = gearAngle(d.encoder_pos || 0, 1, 11);
      const encoderReading = lastEncoderReading(events, live && playing ? events.find(event => event.seq === played) : e, s.unit);
      svg += `<g id="encoder-marker"><g transform="rotate(${angle} ${pt.x} ${pt.y})"><circle cx="${pt.x}" cy="${pt.y}" r="11" fill="#faf4d9" stroke="#9d8e52"/><path d="M${pt.x - 7} ${pt.y} H${pt.x + 7} M${pt.x} ${pt.y - 7} V${pt.y + 7}" stroke="#9d8e52"/><circle cx="${pt.x + 7}" cy="${pt.y}" r="2" fill="#9d8e52"/></g><text x="${pt.x - 25}" y="${pt.y + 4}" text-anchor="end" font-size="11" fill="var(--scene-text, #304c65)">Encoder</text>${Number.isFinite(encoderReading) ? `<text id="encoder-measurement" x="${pt.x+25}" y="${pt.y+4}" font-size="11" fill="var(--scene-text, #304c65)">${encoderReading.toFixed(1)}mm</text>` : ''}</g>`;
    }
    if (name.endsWith(":buffer")) {
      svg += renderBuffer(s,d,unitSensors.filter(([sensor])=>isBufferSensor(sensor)),sensorFill);
    }
  }
  const excess = s.bowden_unabsorbed_mm || 0;
  if (Number.isFinite(excess) && excess !== 0) svg += `<text id="bowden-excess" x="640" y="${bowdenLengthLabel(s).includes('Uncalibrated') ? 134 : 118}" font-size="10" fill="var(--scene-text, #304c65)"><tspan x="640">Excess filament</tspan><tspan x="640" dy="14">${excessFilamentLabel(excess)}</tspan></text>`;
  const residual = s.filament?.residual_mm || 0;
  const fragment = s.filament?.fragment_mm || 0;
  const retainedColor = s.nozzle_color || s.filament?.retained_color || '808182';
  const residualTop = tube.getPointAtLength(progress(end-residual)*length).y;
  const nozzleZero = tube.getPointAtLength(progress(end)*length).y;
  if (s.retained_segment && !s.retained_segment.residual_only) {
    const segment = s.retained_segment;
    const top = tube.getPointAtLength(progress(segment.start)*length).y;
    const coneTip = segment.end;
    const bottom = tube.getPointAtLength(Math.max(progress(segment.start)*length,progress(segment.end)*length-(renderCone && segment.tip_shape === 'cone' ? tipScale : 0))).y;
    const cutLength = Math.max(0, segment.end-segment.start);
    svg += `<path id="retained-fragment" d="M260 ${top} L260 ${bottom}" fill="none" stroke="#${segment.color}" stroke-width="7"/>`;
    if (cutLength > .01) {
      const centerY=tube.getPointAtLength(progress((segment.start+segment.end)/2)*length).y;
      const rows=Array.from({length:281},(_,i)=>200+i)
        .filter(y=>Math.abs(y-452)>=20);
      const labelY=rows.reduce((best,y)=>Math.abs(y-centerY)<Math.abs(best-centerY) ? y : best,rows[0] ?? 470);
      svg += `<g id="cut-fragment-label"><text id="cut-fragment-length" x="196" y="${labelY}" text-anchor="end" dominant-baseline="middle" font-size="9" fill="var(--scene-muted, #6e8190)">Cut fragment · ${mm(cutLength)}mm</text></g>`;
    }
    if (renderCone && segment.tip_shape === 'cone') {
      // The shrinking fragment's cone must never cover the incoming filament behind it.
      svg += `<defs><clipPath id="fragment-tip-clip"><rect x="250" y="${top}" width="20" height="${Math.max(0,nozzleExit-top)}"/></clipPath></defs><g clip-path="url(#fragment-tip-clip)">${shapedTip(coneTip,segment.color,'fragment-tip',segment.hair_length ?? 15)}</g>`;
    }
  } else if (fragment > 0 && !Object.hasOwn(s, 'retained_segment')) {
    const top = tube.getPointAtLength(progress(end-residual-fragment)*length).y;
    svg += `<path id="retained-fragment" d="M260 ${top} L260 ${residualTop}" fill="none" stroke="#${retainedColor}" stroke-width="7"/>`;
  }
  if (residual > 0) {
    svg += `<path id="residual-filament" d="M260 ${residualTop} L260 ${nozzleZero}" fill="none" stroke="#${retainedColor}" stroke-width="7"/><text id="residual-dimension" ${dimensionLink('Residual')} x="241" y="452" text-anchor="end" font-size="9" fill="var(--scene-muted, #6e8190)">Residual · ${residual}mm</text>`;
  }
  // Show the incoming front inside the residual instead of hiding it under a uniform color.
  if (residual > 0 && tip > end-residual) {
    const frontY=tube.getPointAtLength(Math.max(progress(end-residual)*length,progress(tip)*length-coneOffset)).y;
    svg += `<path id="residual-incoming" d="M260 ${residualTop} L260 ${frontY}" fill="none" stroke="#${s.filament?.incoming_color || '808182'}" stroke-width="7"/>`;
  }
  // A cosmetic seam keeps touching cut pieces distinct until the joint reaches the nozzle.
  const cutJoint = s.retained_segment;
  if (cutJoint && !cutJoint.residual_only && cutJoint.start < end-.01
      && tip >= cutJoint.start-.01) {
    const jointY = tube.getPointAtLength(progress(cutJoint.start)*length).y;
    const jointOpacity = Math.max(0, Math.min(1, (end-cutJoint.start)/5));
    svg += `<path id="cut-joint-gap" d="M256.5 ${jointY} H263.5" stroke="var(--scene-tube, #edf4f8)" stroke-width="0.75" opacity="${jointOpacity}"/>`;
  }
  const flooding=s.nozzle_fill_fraction != null && s.nozzle_fill_fraction < 1 && tip >= end-.001;
  if (residual > 0 || tip >= end-.01) {
    // Once the incoming end reaches zero, do not leave an old-color rim around its cone.
    const funnelColor=tip >= end-.01 ? s.filament?.incoming_color || '808182'
      : s.nozzle_outlet_color || purgeOutletColor(s,s.extruded_mm || 0);
    svg += `<path id="nozzle-funnel" d="${nozzleFunnel}" fill="${flooding ? (s.nozzle_fill_from ? `#${s.nozzle_fill_from}` : 'var(--scene-tube, #edf4f8)') : `#${funnelColor}`}"/>`;
  }
  const plan=s.purge_transition;
  const meltingTip=tip >= end-.001 && plan && s.extruded_mm < plan.start_mm+plan.retained_mm+plan.mix_mm;
  if (spoolFill(s,gate)>0 && renderCone && (s.tip_shapes?.[gate] === 'cone' || meltingTip || s.cone_edge_opacity > 0)) {
    svg += `<defs><clipPath id="tip-nozzle-clip"><rect x="0" y="0" width="850" height="${nozzleZero}"/><rect x="500" y="${nozzleZero}" width="350" height="${700-nozzleZero}"/>${flooding ? '' : `<path d="${nozzleFunnel}"/>`}</clipPath></defs><g opacity="${shapedTipOpacity}" mask="url(#bowden-end-fade)" clip-path="url(#tip-nozzle-clip)">${shapedTip(tip,s.filament?.incoming_color || '808182','attached-tip')}</g>`;
  }
  if (tip >= end-.01 || Number.isFinite(s.purge_transition?.nozzle_arrival_mm)) {
    const fillHeight=(nozzleExit-nozzleZero)*(s.nozzle_fill_fraction ?? 1);
    const outletColor=purgeOutletColor(s,s.extruded_mm || 0);
    svg += `<defs><linearGradient id="nozzle-fill-gradient" gradientUnits="userSpaceOnUse" x1="260" x2="260" y1="${nozzleZero}" y2="${nozzleZero+Math.max(.001,fillHeight)}"><stop stop-color="#${s.filament?.incoming_color || '808182'}"/><stop offset="1" stop-color="#${outletColor}"/></linearGradient><clipPath id="nozzle-fill-clip"><rect x="250" y="${nozzleZero}" width="20" height="${fillHeight}"/></clipPath></defs><path id="nozzle-fill-front" d="${nozzleFunnel}" fill="url(#nozzle-fill-gradient)" clip-path="url(#nozzle-fill-clip)"/>`;
  }
  // Keep the dissolving tip edge above the flood, clipped to the complete nozzle bore.
  if (attachedTipEdge) svg += `<defs><clipPath id="tip-edge-clip"><rect x="0" y="0" width="850" height="${nozzleZero}"/><rect x="500" y="${nozzleZero}" width="350" height="${700-nozzleZero}"/><path d="${nozzleFunnel}"/></clipPath></defs><g opacity="${shapedTipOpacity}" mask="url(#bowden-end-fade)" clip-path="url(#tip-edge-clip)">${attachedTipEdge}</g>`;
  svg += `<path id="nozzle-side-shadows" d="M254.5 435 Q256.5 435 256.5 437 V449 L${260-shadowEndX} ${shadowEndY} V${nozzleExit} M265.5 435 Q263.5 435 263.5 437 V449 L${260+shadowEndX} ${shadowEndY} V${nozzleExit}" fill="none" stroke="color-mix(in srgb, var(--scene-nozzle, #9cafbe), black 50%)" stroke-width="0.8" stroke-linecap="round"/>`;
  const {amount:coilAmount, layers, pitch, coils, drop} = purgeCoils(s, nozzleDiameter);
  if (coilAmount > 0) {
    const strandWidth = nozzleWidth * 1.5; // Visual emphasis only; volume uses the selected diameter.
    let waste = '';
    for (const coil of coils) {
      const reveal = `fill="none" stroke-linecap="round" pathLength="100" stroke-dasharray="${coil.fraction*100} 100"`;
      waste += `<path d="${coil.path}" ${reveal} stroke="color-mix(in srgb, #${coil.color}, black 28%)" stroke-width="${strandWidth}" transform="translate(0,${strandWidth*.1})"/><path data-part="purge-material" d="${coil.path}" ${reveal} stroke="#${coil.color}" stroke-width="${strandWidth}"/><path d="${coil.path}" ${reveal} stroke="color-mix(in srgb, #${coil.color}, white 38%)" stroke-width="${strandWidth*.2}" transform="translate(0,${-nozzleWidth*.26})"/>`;
    }
    const landing = 498-Math.max(0,layers-1)*pitch;
    const strandTip = nozzleExit+(landing-nozzleExit)*Math.min(1,coilAmount/1.2);
    const stem = Math.min(strandTip,463);
    const strand = `M260 ${nozzleExit} V${stem}` + (strandTip>stem ? ` C257 ${stem+(strandTip-stem)/3} 264 ${stem+(strandTip-stem)*2/3} 260 ${strandTip}` : '');
    waste += `<path data-part="purge-stream" d="${strand}" fill="none" stroke="#${purgeOutletColor(s,s.extruded_mm)}" stroke-width="${strandWidth}"/>`;
    svg += `<defs><clipPath id="purge-screen"><rect x="210" y="435" width="150" height="70"/></clipPath></defs><g clip-path="url(#purge-screen)"><g id="purge-pile" transform="translate(0,${drop*75})" aria-label="Extruded filament">${waste}</g></g><text x="303" y="491" font-size="10" fill="var(--scene-muted, #6e8190)">${purgeStrandLength(s.extruded_mm, nozzleDiameter).toFixed(1)} mm strand</text>`;
  }
  // Paint cutters last so they remain above attached and retained filament.
  for (const cutter of s.cutters || []) {
    const pt = tube.getPointAtLength(progress(cutter.position) * length);
    const right = cutter.kind === 'mmu';
    const direction = right ? -1 : 1;
    const bladeEvent = live ? playing?.event : null;
    const pressed = bladeEvent && ['cut','release'].includes(bladeEvent.kind)
      ? bladeStroke(bladeEvent, playing?.fraction || 0, cutter.kind)
      : cutter.kind === 'toolhead' && s.cutter_pressed ? 1 : 0;
    // The triangle tip rests at the 20px tube's outer wall, then crosses it.
    const x = pt.x - direction * 10 + direction * 20 * pressed;
    svg += `<g class="cutter-blade" aria-label="${right ? 'MMU servo cutter' : cutter.before_extruder ? 'Cutter before extruder' : 'Cutter inside extruder'}"><path data-part="blade" data-pressed="${pressed}" d="M${x} ${pt.y} l${-direction*42} -5 v10 Z" fill="var(--scene-nozzle, #9cafbe)" stroke="var(--scene-spoke, #526f86)"/>${right ? `<text x="${pt.x+52}" y="${pt.y}" dominant-baseline="middle" font-size="10" fill="var(--scene-text, #304c65)">MMU cutter</text>` : ''}</g>`;
  }
  // Keep the measured filament stroke when appending markers and gears.
  const measured = $("filament").getAttribute("stroke-dasharray");
  $("scene").innerHTML = svg;
  const encoderMarker=$("encoder-marker");
  if (encoderMarker) $("scene").append(encoderMarker);
  const excessLabel = $("bowden-excess");
  if (excessLabel) {
    const bounds = $("bowden-length").getBBox();
    excessLabel.setAttribute('text-anchor', 'middle');
    for (const line of excessLabel.querySelectorAll('tspan'))
      line.setAttribute('x', bounds.x + bounds.width/2);
  }
  $("filament").setAttribute("stroke-dasharray", measured);
  $("filament").setAttribute("opacity", fullTipOpacity);
  $("filament").setAttribute("visibility", slidingRack || spoolFill(s,gate)===0 ? "hidden" : "visible");
  const encoderTitle = $("scene").querySelector('.encoder-offset-title');
  if (encoderTitle) $("scene").querySelector('.encoder-offset-value')
    .setAttribute('x',728+encoderTitle.getComputedTextLength()/2);
  const bodyTop = 181; // Fixed 254-unit housing, ending at y=435.
  $("extruder-body").setAttribute('d', `M250 ${bodyTop} H242 Q240 ${bodyTop} 240 ${bodyTop+2} V435 H280 V${bodyTop+2} Q280 ${bodyTop} 278 ${bodyTop} H270`);
  $("sensors").replaceChildren(
    ...unitSensors.map(([name, d]) => {
      const el = document.createElement("button");
      el.type = "button";
      el.className = "chip sensor-chip" + (d.state === true ? " on" : d.state === null ? " disabled" : " off");
      el.textContent = name.split(':').at(-1);
      const enabled = d.enabled ?? d.state !== null;
      const atLatest = live && !paused && !playing && played >= events.at(-1).seq;
      el.disabled = offline || !atLatest || busy;
      el.setAttribute('aria-pressed', String(enabled));
      el.setAttribute('aria-label', `${name}: ${enabled ? 'enabled' : 'disabled'}. Click to ${enabled ? 'disable' : 'enable'}`);
      el.dataset.tooltip = offline ? 'Recorded sensor state' : atLatest ? `${enabled ? 'Disable' : 'Enable'} ${name}` : 'Jump to live to change sensors';
      el.onclick = () => action(async () => {
        await rpc('sim.viewer.sensor', {name, enabled: !enabled});
        message(`Sensor ${name} ${enabled ? 'disabled' : 'enabled'}.`);
      });
      return el;
    }),
  );
  $("devices").replaceChildren(
    ...Object.entries(s.object_status || {}).map(([name, d]) => {
      const el = document.createElement("span");
      el.className = "chip";
      el.textContent = name + " · " + (d === null ? "No status exposed" : JSON.stringify(d, (key, value) =>
        name.includes('encoder') && typeof value === 'number' && Number.isFinite(value)
          ? Number(value.toFixed(1)) : value));
      return el;
    }),
  );
}
function renderTrace() {
  $('toolchange-ticks').replaceChildren(...events.flatMap((event, index) => {
    if (event.kind !== 'toolchange') return [];
    const tick = document.createElement('span');
    tick.style.left = `${events.length > 1 ? 100*index/(events.length-1) : 0}%`;
    return [tick];
  }));
  const view = $("trace"),
    scroll = view.scrollTop;
  const expanded = new Set([...view.querySelectorAll('details[open]')].map(row=>row.dataset.seq));
  const showDetails=$('show-internal').checked;
  const records=new Map(showDetails ? [...sourceRecords,...(pendingSource ? [pendingSource] : [])].map(record=>[record.sequence,record]) : []);
  view.replaceChildren(
    ...($('show-internal').checked ? rawEvents : events).map(e => {
      const internal = isInternalUpdate(e);
      const row = document.createElement(internal ? 'details' : 'div');
      row.dataset.seq = e.seq;
      const b = document.createElement(internal ? 'summary' : 'button');
      if (!internal) { row.className='trace-event'; row.append(b); }
      if (internal) { row.className = 'trace-internal'; row.open = expanded.has(String(e.seq)); }
      const [sequence, time, type, ...detail] = traceText(e).split(' · ');
      const pill = document.createElement('span');
      pill.className = 'trace-type';
      pill.dataset.type = internal ? 'internal' : type;
      pill.textContent = internal ? 'internal' : type;
      b.append(`${sequence} · ${time} · `, pill);
      const reason = e.label || e.telemetry?.start?.label;
      if (e.kind === 'movement' && reason) {
        const heading = document.createElement('strong');
        heading.textContent = reason;
        const details = document.createElement('span');
        details.className = 'trace-details';
        details.textContent = detail.join(' · ');
        b.append(' · ', heading, details);
      } else if (internal) {
        const name = e.telemetry?.event;
        b.append(' · ' + (name && name !== 'state_delta' ? name.replaceAll('_',' ') : 'State update'));
        const details = document.createElement('pre');
        details.textContent = JSON.stringify(e.telemetry?.changes || e.telemetry || e.state, null, 2);
        row.append(b, details);
      } else b.append(` · ${detail.join(' · ')}`);
      // Detailed provenance stays in the tooltip.
      b.dataset.tooltip = [e.origin,
        e.fast_bowden && $('compress-bowden').checked ? 'Compressed Bowden playback' : '',
        ...(e.changes || []).map(c => `${configurationLabel(c.name)}: ${JSON.stringify(c.before)} → ${JSON.stringify(c.after)}`)]
        .filter(Boolean).join(' · ');
      if (!internal) {
        b.onclick = () => seek(events.findIndex(item => item.seq === e.seq));
        if (showDetails) {
          const details=document.createElement('details'), summary=document.createElement('summary'), payload=document.createElement('pre');
          details.className='trace-telemetry'; details.dataset.seq=e.seq;
          details.open=expanded.has(String(e.seq));
          summary.textContent='Telemetry details';
          const telemetry=e.kind === 'movement' ? {
            start:records.get(e.telemetry?.start?.sequence) || e.telemetry?.start,
            outcome:records.get(e.seq) || e.telemetry?.outcome
          } : records.get(e.seq) || e.telemetry || e.changes || {event:e.kind,label:e.label};
          payload.textContent=JSON.stringify(telemetry,null,2);
          details.append(summary,payload); row.append(details);
        }
      }
      return row;
    }),
  );
  view.scrollTop = scroll;
  followTrace();
}
$("show-internal").onchange = renderTrace;
$("beginning").onclick = () => seek(0);
$("previous").onclick = () => seek(currentIndex() - 1);
for (const [id, direction] of [['previous-toolchange', -1], ['next-toolchange', 1]]) {
  $(id).onclick = () => {
    const index = toolchangeIndex(events, currentIndex(), direction);
    if (index >= 0) seek(index);
  };
}
$("next").onclick = () => seek(currentIndex() + 1);
document.addEventListener('keydown', event => {
  if ((!paused && live) || event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey
      || !['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
  if (event.target.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"]), [role="slider"]')) return;
  const button = $(event.shiftKey
    ? (event.key === 'ArrowLeft' ? 'play-previous' : 'play-step')
    : (event.key === 'ArrowLeft' ? 'previous' : 'next'));
  if (!button.disabled) {
    event.preventDefault();
    button.click();
  }
});
function startPlayback() {
  pauseAfterStep = false;
  if (!events.length) return;
  if (!live) {
    const event = events[cursor];
    played = event.seq; displayed = event.settled_state || event.state;
    playing = null; resumeAt = 0;
  }
  paused = false; live = true;
  draw(); followTrace();
}
$("play-previous").onclick = () => {
  if ($("play-previous").disabled) return;
  const index = currentIndex();
  playing = {event:events[index], start:animationNow(), reverse:true,
    targetSeq:events[index-1].seq, fraction:1,
    selectionGrip:(live && displayed ? displayed : events[index].settled_state || events[index].state).mmu_gripped};
  displayed = interpolate(playing.event,1);
  live = true; paused = false; pauseAfterStep = true; replayingHistory = true;
  sensorPulse = null;
  draw(); followTrace();
};
$("play-step").onclick = () => {
  if ($("play-step").disabled) return;
  startPlayback();
  pauseAfterStep = true;
  resumeAt = 0;
  draw();
};
$("replay").onclick = () => {
  if (live && !paused) {
    if (playing) pauseAfterStep = true;
    else paused = true;
    draw();
  } else startPlayback();
};
function followLive() {
  pauseAfterStep = false;
  replayingHistory = false;
  live = true; paused = offline; playing = null; sensorPulse = null; resumeAt = 0;
  played = events.at(-1)?.seq || 0;
  displayed = events.at(-1)?.settled_state || events.at(-1)?.state;
  draw(); followTrace();
}
$("live").onclick = followLive;
$("timeline").oninput = () => seek(Number($("timeline").value));
$("compress-bowden").onchange = () => renderTrace();
$("show-filament-shape").onchange = draw;
for (const [value,name] of [['cardboard-windowed','Cardboard · windowed'],['dark-windowed','Dark grey · windowed'],['cardboard-solid','Cardboard · solid'],['dark-solid','Dark grey · solid']]) {
  const dark=value.startsWith('dark'), solid=value.endsWith('solid');
  const material=dark ? '#343638' : '#b49164', id=`spool-preview-${value}`;
  // Match the scene: 75-degree face projection and depth of one-third diameter.
  const face=x=>`<g transform="translate(${x} 22) scale(.258819 1)"><circle r="17" fill="${solid ? `url(#${id})` : 'none'}" stroke="${material}" stroke-width="4"/>${solid ? '' : `<path d="M0 -15 V15 M-13 -7.5 L13 7.5 M-13 7.5 L13 -7.5" stroke="${material}" stroke-width="4"/>`}<circle r="6.4" fill="${material}"/><circle r="5.8" fill="#202529"/></g>`;
  const label=document.createElement('label');
  label.className='spool-style-choice';
  label.dataset.tooltip=name;
  label.innerHTML=`<input type="radio" name="spool-style" value="${value}" aria-label="${name}" ${value==='cardboard-windowed' ? 'checked' : ''}><svg viewBox="0 0 40 44" aria-hidden="true"><defs><linearGradient id="${id}" x1="0" y1="0" x2="1" y2="1"><stop stop-color="${dark ? '#66696c' : '#d4b88f'}"/><stop offset="1" stop-color="${material}"/></linearGradient><linearGradient id="${id}-filament" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#287e62"/><stop offset=".5" stop-color="#6ac49d"/><stop offset="1" stop-color="#245d4b"/></linearGradient></defs>${face(25)}<path d="M14 6 H25 A4.14 16 0 0 1 25 38 H14 Z" fill="url(#${id}-filament)"/><ellipse cx="14" cy="22" rx="4.14" ry="16" fill="url(#${id}-filament)"/>${face(14)}</svg>`;
  label.querySelector('input').onchange=()=>{
    $('spool-style-current').replaceChildren(label.querySelector('svg').cloneNode(true));
    $('spool-style-menu').open=false;
    draw();
  };
  if (value==='cardboard-windowed') $('spool-style-current').replaceChildren(label.querySelector('svg').cloneNode(true));
  $('spool-style-grid').append(label);
}
for (const id of ['spool-angle','spool-spacing']) {
  $(id).oninput=()=>{
    $(`${id}-value`).value=$(id).value+(id==='spool-angle' ? '°' : '');
    draw();
  };
}
$("slow-animation").onchange = () => {
  animationNow();
  animationRate=$("slow-animation").checked ? .1 : 1;
};
$("show-dimensions").onchange = () => {
  $("scene").classList.toggle('hide-dimensions', !$("show-dimensions").checked);
};
$("nozzle-diameter").onchange = () => draw();
function downloadTelemetry() {
  const records = [...sourceRecords, ...(pendingSource ? [pendingSource] : [])];
  if (!records.length) return message('No telemetry captured yet', true);
  const text = records.map(r=>JSON.stringify(r)).join('\n')+'\n';
  const url = URL.createObjectURL(new Blob([text],{type:'application/x-ndjson'}));
  const link = document.createElement('a');
  link.href=url; link.download='happy-hare-motion.jsonl'; link.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
}
$('save-motion').onclick = downloadTelemetry;
$('load-telemetry').onclick = () => $('telemetry-file').click();
$('telemetry-file').onchange = async () => {
  const file = $('telemetry-file').files[0];
  if (!file) return;
  try {
    if (file.size > 32*1024*1024) throw Error('Telemetry file exceeds 32 MB');
    const records = parseTelemetry(await file.text());
    const replay = new TelemetryReplay();
    replay.accept(records); // Validate completely before replacing the current view.
    sourceGeneration++;
    offline=true;
    pending.forEach(p=>p.reject(Error('Switched to offline recording'))); pending.clear();
    sourceReplay=replay; sourceRecords=records; pendingSource=null; pendingFingerprint='';
    rawEvents=replay.events; events=visibleSteps(rawEvents); session=replay.session;
    live=false; paused=true; cursor=0; playing=null; sensorPulse=null; pauseAfterStep=false; displayed=null;
    $('recording-name').textContent=file.name;
    $('connection').textContent='Offline recording';
    sourceMode(); draw(); renderTrace();
    message(replay.checkpoint ? 'Loaded checkpoint: earlier motion and inferred geometry are unavailable.' : 'Recording loaded. Use Play or step through the trace.');
  } catch(error) { message(error.message,true); }
  finally { $('telemetry-file').value=''; }
};
$('return-live').onclick = () => {
  sourceGeneration++; offline=false; sourceReplay=new TelemetryReplay(); sourceRecords=[]; pendingSource=null; pendingFingerprint='';
  sourceMode(); refresh().catch(error=>message(error.message,true));
  message('Returning to live telemetry…');
};
let parameterGate = null;
function updateSource(records, reset=false) {
  const selectedSeq=events[currentIndex()]?.seq;
  const newSession=reset || records.some(r=>r.event === 'session');
  const committed=records.filter(r=>!r.pending);
  const preview=records.find(r=>r.pending) || null;
  const fingerprint=JSON.stringify(preview?.changes || []);
  if (newSession) { sourceReplay=new TelemetryReplay(); sourceRecords=[]; }
  sourceReplay.accept(committed);
  sourceRecords.push(...committed);
  pendingSource=preview;
  if (!committed.length && fingerprint === pendingFingerprint) return;
  pendingFingerprint=fingerprint;
  let replay=sourceReplay;
  if (preview) { replay=new TelemetryReplay(); replay.accept([...sourceRecords,preview]); }
  rawEvents=replay.events; events=visibleSteps(rawEvents);
  if (newSession) {
    if (session !== replay.session) $('mini-console').replaceChildren();
    session=replay.session; live=true; paused=false; replayingHistory=false; pauseAfterStep=false;
    cursor=0; playing=null; sensorPulse=null; resumeAt=0;
    played=events.at(-1)?.seq || 0; displayed=events.at(-1)?.settled_state || events.at(-1)?.state;
  } else {
    cursor=Math.max(0,events.findIndex(e=>e.seq === selectedSeq));
    if (playing) playing.event=events.find(e=>e.seq === playing.event.seq) || playing.event;
    else if (live) {
      const current=events.find(e=>e.seq === played);
      if (current) displayed=current.settled_state || current.state;
      else if (played > (events.at(-1)?.seq ?? 0)) followLive();
    }
  }
  draw(); renderTrace();
}
async function poll() {
  const generation=sourceGeneration;
  try {
    if (offline) return;
    // Bound browser history with a new source checkpoint as well as the server ring.
    const query=sourceRecords.length > 6000 || !sourceReplay.session ? '' : '?after='+sourceReplay.sequence;
    const res=await fetch('/sim/viewer/telemetry'+query);
    if (!res.ok) throw Error('Telemetry connection unavailable');
    let data=(await res.json()).result;
    if (sourceReplay.session && data.session_id !== sourceReplay.session && !data.reset)
      data=(await (await fetch('/sim/viewer/telemetry')).json()).result;
    if (offline || generation !== sourceGeneration) return;
    updateSource(data.records,data.reset);
    const latest=rawEvents.at(-1)?.state;
    const selectedGate=latest ? `${session}:${latest.unit}:${latest.gate}` : null;
    if (latest && selectedGate !== parameterGate) {
      parameterGate=selectedGate;
      const unit=latest.units?.indexOf(latest.unit) ?? -1;
      if (unit>=0 && $('unit').value !== String(unit) && socket?.readyState === 1) {
        $('unit').value=String(unit); await refresh();
      }
    }
  } catch(error) {
    if (!offline && generation === sourceGeneration) {
      $('connection').textContent='Telemetry unavailable';
      message(error.message,true);
    }
  } finally { setTimeout(poll,150); }
}
function animate() {
  const now=animationNow();
  const spoolRunning=Boolean(playing) || (live && !paused);
  if (spoolSession !== session) { spoolAngles.clear(); spoolLastFrame=null; spoolArrowVisibility.clear(); spoolDirections.clear(); spoolSession=session; }
  let spoolFading=false;
  const fadeStep=spoolLastFrame === null ? 0 : Math.min(100,now-spoolLastFrame)/450;
  for (const [gate,operation] of Object.entries(displayed?.espoolers || {})) {
    const index=Number(gate), active=operation.output_power>0 && ['assist','rewind'].includes(operation.direction);
    if (active) spoolDirections.set(index,operation.direction);
    const target=active ? 1 : 0, previous=spoolArrowVisibility.get(index) ?? (spoolRunning ? 0 : target);
    const next=previous+Math.sign(target-previous)*Math.min(Math.abs(target-previous),fadeStep);
    spoolArrowVisibility.set(index,next);
    spoolFading ||= next!==previous;
  }
  let spoolRotating=false;
  if (spoolRunning && spoolLastFrame !== null) {
    const seconds=Math.min(100,now-spoolLastFrame)/1000;
    for (const [gate,operation] of Object.entries(displayed?.espoolers || {})) {
      if (!(operation.output_power>0) || !['assist','rewind'].includes(operation.direction)) continue;
      const speed=(40+100*Math.min(1,operation.output_power))*(operation.direction==='rewind'?-1:1);
      spoolAngles.set(Number(gate),((spoolAngles.get(Number(gate)) || 0)+seconds*speed)%360);
      spoolRotating=true;
    }
  }
  spoolLastFrame=now;
  if (spoolFading || spoolRotating) draw();
  if (live && !paused && events.length) {
    if (!playing && now >= resumeAt) {
      const event = events.find(e=>e.seq>played);
      if (event) { playing={event,start:now,selectionGrip:displayed?.mmu_gripped ?? event.before?.mmu_gripped}; followTrace(); }
    }
    if (playing) {
      const ms=duration(playing.event, $("compress-bowden").checked);
      const arrival=nozzleArrivalTime(playing.event,ms);
      const total=Math.max(ms,arrival === null ? 0 : arrival+500), elapsed=Math.min(total,now-playing.start);
      const f=total ? elapsed/total : 1;
      const frame=playbackFrame(playing.event,playing.reverse ? total-elapsed : elapsed,ms,playing.selectionGrip);
      playing.fraction=frame.fraction;
      displayed=frame.state;
      if (f===1 && playing.reverse) {
        seek(events.findIndex(e=>e.seq === playing.targetSeq));
      } else if (f===1) {
        const pulse = homingPulse(playing.event);
        if (pulse) sensorPulse = {...pulse, start:now};
        played=playing.event.seq;
        if (playing.event.kind === 'movement' && playing.event.source !== 'mmu_gcode') resumeAt=now+300;
        playing=null;
        if (pauseAfterStep || (offline && played >= events.at(-1).seq)) { paused = true; pauseAfterStep = false; }
        followTrace();
      }
      draw();
    }
  }
  if (sensorPulse) {
    if (now - sensorPulse.start >= 650) sensorPulse = null;
    draw();
  }
  requestAnimationFrame(animate);
}

// Simulator-only editors for shared hardware objects.
const configColumn=document.querySelector('.left-column');
const bufferPanel=document.createElement('aside');
bufferPanel.id='buffer-config';
bufferPanel.hidden=true;
bufferPanel.dataset.simulatorConfig='buffer';
bufferPanel.innerHTML=`<h2>Buffer Config</h2><p class="note">Runtime simulator settings. Shared objects affect every listed unit. Restart restores defaults.</p>
<label>Buffer object<select id="buffer-unit"></select></label>
<label>Parameter<select id="buffer-parameter"><option>buffer_maxrange</option><option>buffer_range</option><option>buffer_spring_state</option></select></label>
<label>Value<input id="buffer-value" type="number" min="0.1" step="0.1"><select id="buffer-spring" hidden><option value="none">Free moving (none)</option><option value="tension">Tension (−1)</option><option value="neutral">Neutral (0)</option><option value="compression">Compression (+1)</option></select></label>
<button id="buffer-apply">Apply</button><p class="note" id="buffer-status" role="status"></p>`;
configColumn.insertBefore(bufferPanel,$('viewer-settings'));
function showBufferValues(values) {
  bufferPanel.bufferValues=values;
  const spring=$('buffer-parameter').value === 'buffer_spring_state';
  $('buffer-value').hidden=spring;
  $('buffer-spring').hidden=!spring;
  for(const id of ['buffer-parameter','buffer-value','buffer-spring','buffer-apply']) $(id).disabled=!values.available;
  if(values.available) {
    $('buffer-value').value=spring ? '' : values[$('buffer-parameter').value];
    $('buffer-spring').value=values.buffer_spring_state;
  }
  $('buffer-status').textContent=values.available ? '' : 'No buffer fitted to this unit.';
}
$('buffer-parameter').onchange=()=>showBufferValues(bufferPanel.bufferValues || {available:false});
async function refreshSimulatorObjects() {
  if (offline || socket?.readyState !== 1 || !setup || (setup.connection_kind || 'simulator') !== 'simulator') { sourceMode(); return; }
  try {
    const result=await rpc('sim.viewer.objects',{kind:'buffer'});
    const available=result.objects;
    bufferPanel.dataset.available=String(available.length>0);
    const previous=$('buffer-unit').value;
    options($('buffer-unit'),available.map(item=>[item.name,`${item.name} (${item.units.map(i=>setup.units[i]).join(', ')})`]));
    const selected=available.find(item=>item.name===previous) || available[0];
    bufferPanel.bufferObjects=available;
    if(selected) {
      $('buffer-unit').value=selected.name;
      showBufferValues(await rpc('sim.viewer.configure_buffer',{unit:selected.units[0]}));
    }
  } catch(error) { bufferPanel.dataset.available='false'; message(error.message,true); }
  await Promise.all(['encoder','toolhead'].map(refreshObjectPanel));
  sourceMode();
}
$('buffer-unit').onchange=()=>refreshSimulatorObjects();
$('buffer-apply').onclick=()=>action(async()=>{
  const unit=bufferPanel.bufferObjects.find(item=>item.name===$('buffer-unit').value).units[0];
  const current=await rpc('sim.viewer.configure_buffer',{unit});
  const name=$('buffer-parameter').value;
  const values={buffer_maxrange:current.buffer_maxrange,buffer_range:current.buffer_range,buffer_spring_state:current.buffer_spring_state};
  values[name]=name === 'buffer_spring_state' ? $('buffer-spring').value : Number($('buffer-value').value);
  const result=await rpc('sim.viewer.configure_buffer',{unit,values});
  showBufferValues(result);
  $('buffer-status').textContent='Applied. Restart restores profile defaults.';
  followLive();
});
const objectPanels=new Map();
for (const kind of ['encoder','toolhead']) {
  const panel=document.createElement('aside');
  panel.hidden=true; panel.dataset.simulatorConfig=kind;
  panel.innerHTML=`<h2>${kind === 'encoder' ? 'Encoder' : 'Toolhead'} Config</h2><p class="note">Runtime simulator settings. Shared objects affect every listed unit. Restart restores defaults.</p><label>${kind === 'encoder' ? 'Encoder' : 'Toolhead'} object<select class="object-choice"></select></label><label>Parameter<select class="object-parameter"></select></label><label>Value<input class="object-value" type="number" step="any"></label><button class="object-apply">Apply</button><p class="note object-status" role="status"></p>`;
  configColumn.insertBefore(panel,$('viewer-settings')); objectPanels.set(kind,panel);
  panel.querySelector('.object-choice').onchange=()=>renderObjectParameters(panel);
  panel.querySelector('.object-parameter').onchange=()=>showObjectValue(panel);
  panel.querySelector('.object-apply').onclick=()=>action(async()=>{
    const result=await rpc('sim.viewer.objects',{kind,name:panel.querySelector('.object-choice').value,
      parameter:panel.querySelector('.object-parameter').value,value:panel.querySelector('.object-value').value});
    panel.objects=result.objects; renderObjectParameters(panel);
    panel.querySelector('.object-status').textContent='Applied. Restart restores profile defaults.';
    await refresh(); followLive();
  });
}
function renderObjectParameters(panel) {
  const selected=panel.objects.find(item=>item.name===panel.querySelector('.object-choice').value);
  const menu=panel.querySelector('.object-parameter'), previous=menu.value;
  options(menu,(selected?.parameters || []).map(item=>[item.name,item.name]));
  if(selected?.parameters.some(item=>item.name===previous)) menu.value=previous;
  showObjectValue(panel);
}
function showObjectValue(panel) {
  const selected=panel.objects.find(item=>item.name===panel.querySelector('.object-choice').value);
  const parameter=selected?.parameters.find(item=>item.name===panel.querySelector('.object-parameter').value);
  panel.querySelector('.object-value').value=parameter?.value ?? '';
  panel.querySelector('.object-apply').disabled=!parameter;
}
async function refreshObjectPanel(kind) {
  const panel=objectPanels.get(kind);
  try {
    const result=await rpc('sim.viewer.objects',{kind});
    panel.objects=result.objects;
    panel.dataset.available=String(result.objects.length>0);
    const menu=panel.querySelector('.object-choice'), previous=menu.value;
    options(menu,result.objects.map(item=>[item.name,`${item.name} (${item.units.map(i=>setup.units[i]).join(', ')})`]));
    if(result.objects.some(item=>item.name===previous)) menu.value=previous;
    renderObjectParameters(panel);
  } catch(error) { panel.dataset.available='false'; message(error.message,true); }
}

connect();
poll();
requestAnimationFrame(animate);
