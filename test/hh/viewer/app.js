import {TelemetryReplay, parseTelemetry} from './telemetry.js';
import { nozzleArrivalTime, toolheadProgress, toolheadDrawingRange, playbackFrame, switchFilamentDistance, syncForDisplay, isInternalUpdate, encoderFilamentDistance, visibleSteps, toolchangeIndex, annotatedStep, excessFilamentLabel, bufferSpringPath, bufferPiston, interpolate, constrainFilament, duration, gearAngle, CommandHistory, traceText, homingTarget, homingPulse, bladeStroke, configurationLabel, bowdenLengthLabel, toolheadReferences, coolingTubeMeasurements, dimensionParameter, lastEncoderReading, encoderReference, gateDevicePositions, purgeOutletColor, purgeCoils, purgeStrandLength } from './playback.js';
/* Copyright (C) 2022-2026 Paul Morgan. GPLv3. */
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
let displayed = null, playing = null, played = 0, resumeAt = 0;
const spoolAngles=new Map(), spoolVisibility=new Map(), spoolDirections=new Map();
let spoolLastFrame=null, spoolSession=null;
// Keep the first neighboring lane clear of the selected spool; others stay evenly spaced.
const gateLaneX=offset=>620+offset*14+Math.sign(offset)*16*Math.min(1,Math.abs(offset));
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
  spoolVisibility.clear();
  cursor = Math.max(0, Math.min(index, events.length - 1));
  live = false; paused = true; replayingHistory = true; playing = null; sensorPulse = null;
  draw(); followTrace();
}
// An inward ring marks active homing; the outward ring marks completion.
function homingRing(name,x,y) {
  if (!playing || homingTarget(playing.event) !== name) return '';
  const reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;
  const phase=((performance.now()-playing.start)%600)/600;
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
  markup += `<defs><mask id="buffer-tube-gap" maskUnits="userSpaceOnUse" x="0" y="0" width="800" height="520"><rect width="800" height="520" fill="white"/><rect x="${pistonLeft+4}" y="50" width="${508-pistonLeft-4}" height="50" fill="black"/></mask></defs><g id="sync-buffer"><path data-part="buffer-filament-guide" d="M${pistonLeft+4} 66.5 H508 M${pistonLeft+4} 83.5 H508" fill="none" stroke="var(--scene-tube-border, #b1c4d2)" stroke-width="1.6" opacity="0.3"/><text x="470" y="38" text-anchor="middle" font-size="10" fill="var(--scene-text, #304c65)">Sync-feedback buffer</text><g fill="none" stroke="var(--scene-border, #96abba)" stroke-width="1.5"><rect id="buffer-housing" x="428" y="46" width="84" height="51.88" rx="6.4"/><rect id="buffer-piston" x="${pistonLeft}" y="59" width="80" height="32" rx="5.6"/></g><g fill="var(--scene-tube-border, #b1c4d2)"><rect data-part="buffer-housing-port" x="509.6" y="62.2" width="4.8" height="25.6" rx="2.4"/><rect data-part="buffer-piston-port" x="${pistonLeft-2.4}" y="62.2" width="4.8" height="25.6" rx="2.4"/></g>`;
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
      const elapsed=(performance.now()-sensorPulse.start)/650;
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
    "M620 470 L620 150 Q620 75 525 75 L355 75 Q260 75 260 170 L260 449";
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
  let svg = `<rect x="522" y="275" width="196" height="225" rx="12" fill="var(--scene-body, #edf2f6)" stroke="var(--scene-border, #96abba)"/><text x="534" y="302" fill="var(--scene-text, #304c65)">MMU</text><path id="extruder-body" d="M250 205 H242 Q240 205 240 207 V435 H280 V207 Q280 205 278 205 H270" fill="var(--scene-body, #edf2f6)" stroke="var(--scene-border, #96abba)"/><rect x="213" y="375" width="94" height="58" rx="5" fill="var(--scene-heater, #c4d3df)"/><rect id="nozzle-collar" x="240" y="433" width="40" height="2" fill="var(--scene-nozzle, #9cafbe)"/><path id="nozzle-body" d="${nozzleBody}" fill="var(--scene-nozzle, #9cafbe)"/><path id="nozzle-bore" d="${nozzleFunnel}" fill="var(--scene-tube, #edf4f8)"/><g mask="url(#bowden-end-fade)"><path id="tube" d="${path}" fill="none" stroke="none"/><g mask="url(#bowden-tube-fade)"><g${s.devices[`${s.unit}:buffer`] ? ' mask="url(#buffer-tube-gap)"' : ''}><path d="${bowdenPath}" fill="none" stroke="var(--scene-tube-border, #b1c4d2)" stroke-width="20"/><path d="${bowdenPath}" fill="none" stroke="var(--scene-tube, #edf4f8)" stroke-width="14"/></g></g><path id="nozzle-entrance" d="${nozzleEntrance}" fill="var(--scene-tube, #edf4f8)"/><path id="filament" d="${path}" fill="none" stroke="#${s.filament?.incoming_color || "808182"}" stroke-width="7"/></g><text ${dimensionLink('Bowden')} id="bowden-length" x="660" y="100" text-anchor="start" fill="var(--scene-muted, #6e8190)" font-size="13">${bowdenLengthLabel(s).includes('Uncalibrated') ? '<tspan x="660">Bowden</tspan><tspan x="660" dy="16">Uncalibrated length</tspan>' : bowdenLengthLabel(s)}</text>`;
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
  const gateSlots = gateDevicePositions(s, unitSensors);
  const sensorPoint = (name, device) => {
    if (isTouchSensor(name)) return {x:260,y:420};
    const point = tube.getPointAtLength(progress(device.position)*length);
    const short = name.split(':').at(-1);
    if (/^mmu_entry(?:_\d+)?$/.test(short)) point.y -= 53;
    if (/^mmu_exit(?:_\d+)?$/.test(short)) { point.x = 620; point.y = gateSlots.exit; }
    if (short === 'mmu_shared_exit') { point.x = 620; point.y = gateSlots.sharedExit; }
    return point;
  };
  const isBufferSensor = name => /(?:^|:)filament_(compression|tension)$/.test(name);
  const sensorFill = state => state === true ? '#399b64' : state === false ? '#fff' : '#9ba5ae';
  const encoderPoint = () => ({x:620, y:gateSlots.encoder ?? 260});
  const tipAtPathDistance = distance => {
    const target = Math.max(0,distance)/length;
    let low = Math.min(-100,(s.layout.mmu_entry ?? -150)-40), high = end;
    for (let i=0;i<32;i++) { const mid=(low+high)/2; if(progress(mid)<target) low=mid; else high=mid; }
    return (low+high)/2;
  };
  let schematicDistance = position => progress(position)*length;
  let schematicTip = null;
  // UNKNOWN uses an illustrative endpoint between the MMU gears and its outlet.
  if (s.gate >= 0 && s.filament_pos === -1 && e.kind !== 'movement') tip = tipAtPathDistance(470-305);
  // Encoder loading keeps one reference across START OF BOWDEN bookkeeping.
  if (s.gate >= 0 && s.filament_pos >= 0 && tip < entry && s.encoder_reference?.endstop === 'encoder') {
    tip = tipAtPathDistance(encoderFilamentDistance(events,e,s,tip,p=>progress(p)*length,470-encoderPoint().y));
  } else if (s.gate >= 0 && s.filament_pos >= 0 && tip < entry) {
    const endstop = s.encoder_reference?.endstop?.split(':').at(-1);
    const sensor = unitSensors.find(([name,d]) => Number.isFinite(d.position) &&
      [endstop,`${endstop}_${gate}`].includes(name.split(':').at(-1)));
    if (sensor) {
      schematicDistance = position => switchFilamentDistance(position,sensor[1].position,
        s.encoder_reference?.parking || 0,entry,470-sensorPoint(...sensor).y,p=>progress(p)*length,
        encoderAnchor?.name === sensor[0] && s.devices?.[`${s.unit}:encoder`]
          ? {position:sensor[1].position+encoderAnchor.offset,distance:470-encoderPoint().y} : null);
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
        ? 470-sensorPoint(name,d).y : pathDistance(d.position);
      return {...d,name,position};
    });
  const fragments = s.retained_segment ? [{start:pathDistance(s.retained_segment.start),end:pathDistance(s.retained_segment.end)}] : [];
  const constrained = constrainFilament(schematicTip ?? pathDistance(tip),
    s.tails[gate] === null ? null : pathDistance(s.tails[gate]), presenceSensors, fragments, s.sensor_conflicts || []);
  const entrySensorY = tube.getPointAtLength(progress(s.layout.mmu_entry ?? -150)*length).y;
  const laneOpacity = offset => focus < 0 ? .35 : Math.max(.2,Math.exp(-Math.abs(offset)*.45));
  const rackTipY = offset => Math.min(440,entrySensorY+Math.min(18,Math.abs(offset)*5))-28;
  // Return the selected end to rack height before sliding; telemetry positions stay unchanged.
  const selectedTip=constrained.tip+(Math.min(constrained.tip,470-rackTipY(0))-constrained.tip)*(s.gate_retraction || 0);
  tip = tipAtPathDistance(selectedTip);
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
  // Fan parked ends around the selected gate, bringing its strand into focus.
  // The central strand lines up with the existing MMU filament path.
  const focus = s.gate_focus ?? s.gate;
  const showMainSpool=s.gate>=0 && Object.keys(s.espoolers || {}).length>0;
  let mainSpoolOpacity=s.gate_path_opacity ?? 1;
  const arrivingGate=playing?.reverse ? playing.event.before?.gate : playing?.event.state.gate;
  if (playing?.event.kind === 'gate_changed' && s.gate === arrivingGate) {
    // Let the new filament finish advancing with the normal fade before lifting it.
    const fadeStart=duration(playing.event)-500;
    const fade=Math.max(0,Math.min(1,(performance.now()-playing.start-fadeStart)/1000));
    mainSpoolOpacity=fade*fade*(3-2*fade);
  }
  const spoolFadeLift=showMainSpool ? mainSpoolOpacity : 0;
  svg += `<defs><linearGradient id="gate-end-gradient" gradientUnits="userSpaceOnUse" x1="0" x2="0" y1="${entrySensorY+3}" y2="470"><stop stop-color="white"/><stop offset="1" stop-color="black"/></linearGradient><mask id="gate-end-fade" maskUnits="userSpaceOnUse" x="522" y="316" width="196" height="159"><rect x="522" y="316" width="196" height="159" fill="url(#gate-end-gradient)"/></mask></defs><g id="gate-filaments" mask="url(#gate-end-fade)">`;
  const lanes = s.tips.map((_, index) => ({index,offset:index-focus}))
    .filter(lane => lane.index !== s.gate && Math.abs(lane.offset) < 6)
    .sort((a,b) => Math.abs(b.offset)-Math.abs(a.offset));
  for (const {index,offset} of lanes) {
    // Extra clearance beside the active lane; ordinary spacing beyond it.
    const x = gateLaneX(offset);
    const y = rackTipY(offset);
    const opacity = laneOpacity(offset);
    const color = s.gate_colors?.[index] || '808182';
    svg += `<g data-gate="${index}" opacity="${opacity}"><path d="M${x} 470 V${y}" stroke="#${color}" stroke-width="7" fill="none"/><text x="${x}" y="${Math.min(464,y+14)}" text-anchor="middle" font-size="8" font-weight="700" fill="var(--scene-text, #304c65)">${index}</text></g>`;
  }
  svg += '</g>';
  if (Object.keys(s.espoolers || {}).length) {
    svg += '<g id="espooler-indicators">';
    const visible=[{index:s.gate,offset:s.gate-focus},...lanes].filter(lane=>lane.index>=0);
    for (const {index,offset} of visible) {
      const operation=s.espoolers[index];
      const active=operation?.output_power>0 && ['assist','rewind'].includes(operation.direction);
      const selected=index===s.gate;
      const visibility=spoolVisibility.get(index) ?? (active ? 1 : 0);
      const fade=visibility*visibility*(3-2*visibility);
      if (!selected && fade<=0) continue;
      const x=gateLaneX(offset), y=455+(selected ? 0 : rackTipY(offset)-rackTipY(0));
      const angle=spoolAngles.get(index) || 0;
      const color=s.gate_colors[index] || '808182';
      const title=`Gate ${index} · ${active ? operation.direction+' · '+Math.round(operation.output_power*100)+'% output' : 'Stopped'}`;
      svg += `<g data-part="espooler" data-gate="${index}" data-active="${active}" data-tooltip="${escape(title)}" transform="translate(${x} ${y}) scale(${selected ? 1.1 : 1})" opacity="${selected ? mainSpoolOpacity : laneOpacity(offset)*fade}">`;
      const spoolViewAngle=65*Math.PI/180, spoolFaceScale=Math.cos(spoolViewAngle);
      const spoolDepth=34/3;
      // Compensate for the face transform so axial depth projects with sin(view angle).
      const spoolDepthOffset=spoolDepth*Math.sin(spoolViewAngle)/spoolFaceScale;
      if (selected) svg += `<g transform="scale(${spoolFaceScale} 1)"><circle cx="${spoolDepthOffset}" cy="0" r="17" fill="color-mix(in srgb, var(--scene-body, #edf2f6), black 25%)" stroke="var(--scene-border, #96abba)" stroke-width="0.5"/><defs><linearGradient id="spool-winding-shade" x1="0" y1="0" x2="0" y2="1"><stop stop-color="color-mix(in srgb, #${color}, black 25%)"/><stop offset=".4" stop-color="#${color}"/><stop offset=".65" stop-color="color-mix(in srgb, #${color}, white 15%)"/><stop offset="1" stop-color="color-mix(in srgb, #${color}, black 35%)"/></linearGradient></defs><path d="M0 -14.5 H${spoolDepthOffset} A14.5 14.5 0 0 1 ${spoolDepthOffset} 14.5 H0 Z" fill="url(#spool-winding-shade)"/>${Array.from({length:5},(_,i)=>(i+1)*spoolDepthOffset/6).map(depth=>`<path d="M${depth} -14.5 A14.5 14.5 0 0 1 ${depth} 14.5" fill="none" stroke="color-mix(in srgb, #${color}, black 30%)" stroke-width=".4" opacity=".45"/>`).join('')}`;
      if (selected) svg += `<g><circle r="17" fill="var(--scene-body, #edf2f6)" stroke="var(--scene-border, #96abba)" stroke-width="0.5"/><circle r="15" fill="#${color}"/><circle r="15.5" fill="none" stroke="var(--scene-body, #edf2f6)" stroke-width="2"/><g transform="rotate(${angle})" stroke="var(--scene-body, #edf2f6)" stroke-width="2"><path d="M0 -16.5 V16.5 M-14.29 -8.25 L14.29 8.25 M-14.29 8.25 L14.29 -8.25"/>${[0,60,120,180,240,300].map(rotation=>`<path transform="rotate(${rotation})" d="M-1 -11 Q-1.2 -14 -3.8 -16.06 A16.5 16.5 0 0 1 3.8 -16.06 Q1.2 -14 1 -11 Z" fill="var(--scene-body, #edf2f6)" stroke="none"/>`).join('')}</g><circle r="4" fill="var(--scene-body, #edf2f6)" stroke="var(--scene-border, #96abba)"/></g>`;
      if (!selected) svg += `<g transform="rotate(${angle}) scale(${(active ? operation.direction : spoolDirections.get(index))==='rewind'?-1:1} 1)"><circle r="6" fill="var(--scene-body, #edf2f6)" stroke="var(--scene-border, #96abba)" stroke-width="1.2"/><path d="M2.5 -6 L-2 -8 L-2 -4 Z" fill="#fff"/></g>`;
      if (selected && fade>0) svg += `<g opacity="${fade}" transform="rotate(${angle}) scale(${(active ? operation.direction : spoolDirections.get(index))==='rewind'?-1:1} 1)"><path d="M-12.021 -12.021 A17 17 0 0 1 15.205 -7.603" fill="none" stroke="var(--scene-text, #304c65)" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/><path d="M10.7 -10.97 L15.205 -7.603 L15.2 -13.22 Z" fill="var(--scene-text, #304c65)"/></g>`;
      if (selected) svg += '</g>';
      svg += '</g>';
    }
    svg += '</g>';
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
    return `<g data-part="${part}" opacity="${part === 'attached-tip' ? (s.gate_path_opacity ?? 1) : 1}">${hairStart < hairLimit ? `<path data-part="filament-hair" d="${outline.map((p,i)=>`${i?'L':'M'}${p.x} ${p.y}`).join(' ')} Z" fill="#${color}"/>` : ''}<path d="M${base.x+dx} ${base.y+dy} L${nose.x} ${nose.y} L${base.x-dx} ${base.y-dy} Z" fill="#${color}"/>${part === 'attached-tip' ? '' : edge}</g>`;
  };
  const fadeY = tube.getPointAtLength(progress(s.layout.mmu_entry ?? -150) * length).y;
  svg += `<defs><linearGradient id="bowden-fade-gradient" gradientUnits="userSpaceOnUse" x1="0" x2="0" y1="${fadeY+3}" y2="${470-29*spoolFadeLift}"><stop stop-color="white"/><stop offset="1" stop-color="black"/></linearGradient><mask id="bowden-end-fade" maskUnits="userSpaceOnUse" x="0" y="0" width="800" height="520"><rect width="800" height="520" fill="white"/><rect x="600" y="${fadeY+3}" width="40" height="${Math.max(0,470-fadeY-3)}" fill="url(#bowden-fade-gradient)"/></mask></defs>`;
  svg += `<defs><linearGradient id="bowden-tube-gradient" gradientUnits="userSpaceOnUse" x1="0" x2="0" y1="${fadeY-17}" y2="${458-20*spoolFadeLift}"><stop stop-color="white"/><stop offset="1" stop-color="black"/></linearGradient><mask id="bowden-tube-fade" maskUnits="userSpaceOnUse" x="0" y="0" width="800" height="520"><rect width="800" height="520" fill="white"/><rect x="600" y="${fadeY-17}" width="40" height="${Math.max(0,520-fadeY+17)}" fill="url(#bowden-tube-gradient)"/></mask></defs>`;
  if (syncForDisplay(e, s, Boolean(playing) || !live || paused)) {
    svg += `<g id="synced-indicator" aria-label="Extruder and MMU gears synced"><g transform="translate(692 295) rotate(-35)" fill="none" stroke="#53b9b0" stroke-width="2.2"><rect x="-12" y="-5" width="15" height="10" rx="5"/><rect x="-3" y="-5" width="15" height="10" rx="5"/></g><text x="692" y="316" text-anchor="middle" font-size="10" fill="var(--scene-text, #304c65)">Synced</text></g>`;
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
  svg += `<text id="current-gate" x="620" y="486" dominant-baseline="central" text-anchor="middle" font-size="12" fill="var(--scene-text, #456078)">${s.gate >= 0 ? `${s.tool >= 0 ? `T${s.tool} ` : ''}(gate ${s.gate})` : s.gate === -2 ? 'Bypass' : 'Unknown'}</text>`;
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
      const elapsed = (performance.now() - sensorPulse.start) / 650;
      if (elapsed >= 0 && elapsed < 1) {
        const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
        svg += `<circle data-part="sensor-pulse" cx="${pt.x + 16}" cy="${pt.y}" r="${reduced ? 10 : 7 + elapsed * 15}" fill="none" stroke="${sensorPulse.reverse ? 'var(--sensor-clear-pulse)' : '#48c982'}" stroke-width="2.5" opacity="${1-elapsed}"/>`;
      }
    }
  }
  for (const [x, y, type] of [
    [620, 335, "gear"],
    [260, tube.getPointAtLength(progress(s.layout.extruder_gears ?? entry) * length).y, "extruder"],
  ]) {
    const names = s.drive_motors?.[type] || Object.keys(s.motors).filter(n => n.includes(type));
    // Coupled motors share travel; do not add their distances or inactive gates.
    const distance = names.length ? (s.motors[names[0]] || 0) : 0;
    // Both gear pairs use the toolhead scale so equal travel gives equal rotation.
    const pixelsPerMm = .27 * length / (end-entry);
    const angle = gearAngle(distance, pixelsPerMm);
    const gripState = e.kind === 'movement' && (playing || !live || paused) ? e.before : s;
    const spacing = type === 'gear' && gripState?.mmu_gripped === false ? 31 : 22;
    for (const side of [-1, 1])
      svg += `<g data-part="${type}-wheel" transform="translate(${x + side * spacing} ${y}) rotate(${(type === "extruder" ? -1 : 1) * side * angle})"><circle r="19" fill="var(--scene-gear, #d8e4ed)" stroke="var(--scene-border, #6d899f)"/><path d="M-14 0 H14 M0 -14 V14" stroke="var(--scene-spoke, #526f86)"/><circle class="gear-center" r="3" fill="var(--scene-spoke, #526f86)"/></g>`;
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
  if (renderCone && (s.tip_shapes?.[gate] === 'cone' || meltingTip || s.cone_edge_opacity > 0)) {
    svg += `<defs><clipPath id="tip-nozzle-clip"><rect x="0" y="0" width="800" height="${nozzleZero}"/>${flooding ? '' : `<path d="${nozzleFunnel}"/>`}</clipPath></defs><g clip-path="url(#tip-nozzle-clip)">${shapedTip(tip,s.filament?.incoming_color || '808182','attached-tip')}</g>`;
  }
  if (tip >= end-.01 || Number.isFinite(s.purge_transition?.nozzle_arrival_mm)) {
    const fillHeight=(nozzleExit-nozzleZero)*(s.nozzle_fill_fraction ?? 1);
    const outletColor=purgeOutletColor(s,s.extruded_mm || 0);
    svg += `<defs><linearGradient id="nozzle-fill-gradient" gradientUnits="userSpaceOnUse" x1="260" x2="260" y1="${nozzleZero}" y2="${nozzleZero+Math.max(.001,fillHeight)}"><stop stop-color="#${s.filament?.incoming_color || '808182'}"/><stop offset="1" stop-color="#${outletColor}"/></linearGradient><clipPath id="nozzle-fill-clip"><rect x="250" y="${nozzleZero}" width="20" height="${fillHeight}"/></clipPath></defs><path id="nozzle-fill-front" d="${nozzleFunnel}" fill="url(#nozzle-fill-gradient)" clip-path="url(#nozzle-fill-clip)"/>`;
  }
  // Keep the dissolving tip edge above the flood, clipped to the complete nozzle bore.
  if (attachedTipEdge) svg += `<defs><clipPath id="tip-edge-clip"><rect x="0" y="0" width="800" height="${nozzleZero}"/><path d="${nozzleFunnel}"/></clipPath></defs><g clip-path="url(#tip-edge-clip)" opacity="${s.gate_path_opacity ?? 1}">${attachedTipEdge}</g>`;
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
  $("filament").setAttribute("opacity", s.gate_path_opacity ?? 1);
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
  playing = {event:events[index], start:performance.now(), reverse:true,
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
function animate(now) {
  const spoolRunning=Boolean(playing) || (live && !paused);
  if (spoolSession !== session) { spoolAngles.clear(); spoolVisibility.clear(); spoolDirections.clear(); spoolSession=session; }
  let spoolFading=false;
  const fadeStep=spoolLastFrame === null ? 0 : Math.min(100,now-spoolLastFrame)/450;
  for (const [gate,operation] of Object.entries(displayed?.espoolers || {})) {
    const index=Number(gate), active=operation.output_power>0 && ['assist','rewind'].includes(operation.direction);
    if (active) spoolDirections.set(index,operation.direction);
    const target=active ? 1 : 0, previous=spoolVisibility.get(index) ?? (spoolRunning ? 0 : target);
    const next=previous+Math.sign(target-previous)*Math.min(Math.abs(target-previous),fadeStep);
    spoolVisibility.set(index,next);
    spoolFading ||= next!==previous;
  }
  if (spoolRunning && spoolLastFrame !== null) {
    const seconds=Math.min(100,now-spoolLastFrame)/1000;
    for (const [gate,operation] of Object.entries(displayed?.espoolers || {})) {
      if (!(operation.output_power>0) || !['assist','rewind'].includes(operation.direction)) continue;
      const speed=(40+100*Math.min(1,operation.output_power))*(operation.direction==='rewind'?-1:1);
      spoolAngles.set(Number(gate),((spoolAngles.get(Number(gate)) || 0)+seconds*speed)%360);
    }
  }
  spoolLastFrame=now;
  if (spoolFading || (spoolRunning && Object.values(displayed?.espoolers || {}).some(o=>o.output_power>0))) draw();
  if (live && !paused && events.length) {
    if (!playing && now >= resumeAt) {
      const event = events.find(e=>e.seq>played);
      if (event) { playing={event,start:now,selectionGrip:displayed?.mmu_gripped ?? event.before?.mmu_gripped}; followTrace(); }
    }
    if (playing) {
      const ms=duration(playing.event, $("compress-bowden").checked);
      const arrival=nozzleArrivalTime(playing.event,ms);
      const total=Math.max(ms+(playing.event.kind === 'gate_changed' ? 500 : 0),arrival === null ? 0 : arrival+500), elapsed=Math.min(total,now-playing.start);
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
configColumn.append(bufferPanel);
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
  configColumn.append(panel); objectPanels.set(kind,panel);
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
