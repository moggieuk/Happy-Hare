import { bufferSpringPath, bufferPiston, interpolate, duration, purgePath, gearAngle, CommandHistory, traceText, homingPulse, bladeStroke, configurationLabel, bowdenLengthLabel, toolheadReferences, coolingTubeMeasurements, dimensionParameter, lastEncoderReading, purgeColor, purgeCoils, purgeStrandLength } from './playback.js';
/* Copyright (C) 2022-2026 Paul Morgan. GPLv3. */
const $ = (id) => document.getElementById(id);
// A shared overlay gives HTML and SVG tooltips the same delay and avoids clipping.
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
  }, 500);
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
let configRevision = null;
let socket,
  serial = 0,
  pending = new Map(),
  events = [],
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
    if (socket.readyState !== 1) return reject(Error("Console disconnected"));
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
  }
}
function connect() {
  socket = new WebSocket(
    `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/websocket`,
  );
  socket.onopen = () => {
    $("connection").textContent = "Connected · identifying source…";
    refresh().catch((e) => message(e.message, true));
  };
  socket.onmessage = (e) => {
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
    $("connection").textContent = "Disconnected · retrying";
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
  setup = await rpc("sim.viewer.setup", { unit: Number($("unit").value || 0) });
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
  filter();
}
function filter() {
  const previous = $("parameter").value,
    query = $("filter").value.toLowerCase();
  const groups = new Map();
  for (const p of [...setup.parameters, ...(setup.macro_parameters || [])]) {
    if (!`${p.macro || ''} ${p.name}`.toLowerCase().includes(query)) continue;
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
    for (const p of matches) {
      const option = document.createElement('option');
      option.value = p.id || p.name;
      option.textContent = p.name;
      group.append(option);
    }
    return group;
  }));
  if ([...$("parameter").options].some((o) => o.value === previous))
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
    if (Number(target.dataset.unit) >= 0) $('unit').value = target.dataset.unit;
    await refresh();
    $('filter').value = '';
    filter();
    const id = target.dataset.parameter;
    if (![...$('parameter').options].some(option => option.value === id)) {
      message('This dimension has no editable parameter in the current configuration.', true);
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
$("filter").oninput = filter;
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
  if (event.isComposing || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
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
let paused = false, stopAfterStep = false, replayingHistory = false, sensorPulse = null;
function currentIndex() {
  return live ? Math.max(0, events.findIndex(e => e.seq === (playing?.event.seq || played))) : cursor;
}
function followTrace() {
  const view = $("trace"), index = currentIndex();
  for (const [i, row] of [...view.children].entries()) {
    const selected = i === index;
    row.classList.toggle('selected', selected);
    if (selected) row.setAttribute('aria-current', 'step');
    else row.removeAttribute('aria-current');
  }
  const row = view.children[index];
  if (row) {
    const box = row.getBoundingClientRect(), bounds = view.getBoundingClientRect();
    if (box.top < bounds.top) view.scrollTop -= bounds.top - box.top;
    else if (box.bottom > bounds.bottom) view.scrollTop += box.bottom - bounds.bottom;
  }
}
function seek(index) {
  cursor = Math.max(0, Math.min(index, events.length - 1));
  live = false; paused = true; replayingHistory = true; playing = null; sensorPulse = null;
  stopAfterStep = false;
  draw(); followTrace();
}
// Buffer drawing is isolated from the toolhead and filament layers.
function renderBuffer(s, d, bufferSensors, sensorFill) {
  let markup = '';
  const tension=bufferSensors.find(([n])=>n.endsWith('tension'))?.[1]?.state;
  const compression=bufferSensors.find(([n])=>n.endsWith('compression'))?.[1]?.state;
  const piston=bufferPiston(d,tension,compression);
  // Keep the housing fixed on the flat Bowden section.
  // Keep the travel scale fixed while narrowing the housing around it.
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
    markup += anchors+`<path data-part="buffer-spring-piston-tick" d="M${edge} 48.5 V56.5" opacity="0.5"/>`+'</g>';
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
  $("replay").disabled = !!running || currentIndex() >= events.length - 1;
  $("play-end").disabled = live && !paused && !stopAfterStep;
  $("beginning").disabled = $("previous").disabled = currentIndex() <= 0;
  $("next").disabled = currentIndex() >= events.length - 1;
  const e = live ? (playing?.event || events.find(e=>e.seq===played) || events.at(-1)) : events[cursor];
  const
    s = live && displayed ? displayed : e.state,
    gate = s.gate >= 0 ? s.gate : 0,
    tip = s.tips[gate] ?? -1000;
  const entry = s.layout.extruder_entry ?? 700,
    end = s.layout.nozzle ?? (s.layout.toolhead ?? 760) + 30;
  const eventsBehind = events.length - 1 - events.indexOf(e);
  if (live && !paused && !stopAfterStep && !playing && eventsBehind === 0) replayingHistory = false;
  const modeLabel = replayingHistory || !live ? (paused && live ? 'Replay · paused' : 'Replay') : paused ? 'Paused' : eventsBehind >= 3 ? 'Live · catching up' : 'Live';
  // Bookkeeping events must not clear the last animated movement's annotation.
  const annotatedEvent = ['movement', 'gate_changed'].includes(e.kind) ? e : events.findLast(event => event.seq <= e.seq && ['movement', 'gate_changed'].includes(event.kind)) || e;
  const moveReason = annotatedEvent.label || annotatedEvent.telemetry?.start?.label;
  $("mode").textContent = `${modeLabel} · event ${annotatedEvent.seq}` + (moveReason ? ` · ${moveReason}` : '');
  $("position").textContent = `Gate ${gate} · ${s.filament_pos_name || "Unknown state"} · tip ${tip.toFixed(2)} mm`;
  $("timeline").max = events.length - 1;
  $("timeline").value = live ? events.indexOf(e) : cursor;
  // Fixed path with a compressed Bowden section. Position maps monotonically to it.
  const path =
    "M620 470 L620 170 Q620 75 525 75 L355 75 Q260 75 260 170 L260 445";
  const dimensionLink = label => {
    const id = dimensionParameter(s, label);
    return id ? `class="editable-dimension" role="button" tabindex="0" data-parameter="${escape(id)}" data-unit="${s.units?.indexOf(s.unit) ?? 0}" aria-label="Edit ${escape(label)}"` : '';
  };
  const nozzleDiameter = Number($("nozzle-diameter").value);
  const nozzleWidth = 7 * nozzleDiameter / 1.75;
  const nozzleFunnel = `M256.5 445 L${260-nozzleWidth/2} 455 H${260+nozzleWidth/2} L263.5 445 Z`;
  const bowdenPath = path.replace("L260 445", "L260 435");
  let svg = `<rect x="540" y="275" width="170" height="200" rx="12" fill="var(--scene-body, #edf2f6)" stroke="var(--scene-border, #96abba)"/><text x="558" y="302" fill="var(--scene-text, #304c65)">MMU</text><path id="extruder-body" d="M250 205 H242 Q240 205 240 207 V435 H280 V207 Q280 205 278 205 H270" fill="var(--scene-body, #edf2f6)" stroke="var(--scene-border, #96abba)"/><rect x="213" y="375" width="94" height="58" rx="5" fill="var(--scene-heater, #c4d3df)"/><rect id="nozzle-collar" x="240" y="433" width="40" height="2" fill="var(--scene-nozzle, #9cafbe)"/><path id="nozzle-body" d="M242 435 L${259-nozzleWidth/2} 455 H${261+nozzleWidth/2} L278 435 Z" fill="var(--scene-nozzle, #9cafbe)"/><path id="nozzle-bore" d="${nozzleFunnel}" fill="var(--scene-tube, #edf4f8)"/><g mask="url(#bowden-end-fade)"><path id="tube" d="${path}" fill="none" stroke="none"/><g mask="url(#bowden-tube-fade)"><g${s.devices[`${s.unit}:buffer`] ? ' mask="url(#buffer-tube-gap)"' : ''}><path d="${bowdenPath}" fill="none" stroke="var(--scene-tube-border, #b1c4d2)" stroke-width="20"/><path d="${bowdenPath}" fill="none" stroke="var(--scene-tube, #edf4f8)" stroke-width="14"/></g></g><path d="M260 435 V445" stroke="var(--scene-tube, #edf4f8)" stroke-width="7"/><path id="filament" d="${path}" fill="none" stroke="#${s.filament?.incoming_color || "808182"}" stroke-width="7"/></g><text ${dimensionLink('Bowden')} id="bowden-length" x="640" y="100" text-anchor="start" fill="var(--scene-muted, #6e8190)" font-size="13">${bowdenLengthLabel(s).includes('Uncalibrated') ? '<tspan x="640">Bowden</tspan><tspan x="640" dy="16">Uncalibrated length</tspan>' : bowdenLengthLabel(s)}</text>`;
  $("scene").innerHTML = svg;
  const tube = $("tube"),
    length = tube.getTotalLength();
  const mmuExitFraction = 200 / length; // Exit sits 20px above the MMU box.
  const progress = (p) =>
    p < 0
      ? Math.max(0, ((p - Math.min(-100, (s.layout.mmu_entry ?? -150)-40)) / -Math.min(-100, (s.layout.mmu_entry ?? -150)-40)) * mmuExitFraction)
      : p < entry
        ? mmuExitFraction + ((0.73-mmuExitFraction) * p) / entry
        : Math.min(1, 0.73 + (0.27 * (p - entry)) / (end - entry));
  const start = s.tails[gate] === null ? 0 : progress(s.tails[gate]) * length,
    stop = progress(s.tip_shapes?.[gate] === 'cone' ? tip-(s.filament?.residual_mm || 0)-2 : tip) * length;
  $("filament").setAttribute(
    "stroke-dasharray",
    `0 ${start} ${Math.max(0, stop - start)} ${length + 1}`,
  );
  // Fan parked ends around the selected gate, bringing its strand into focus.
  // The central strand lines up with the existing MMU filament path.
  const focus = s.gate_focus ?? s.gate;
  const entrySensorY = tube.getPointAtLength(progress(s.layout.mmu_entry ?? -150)*length).y;
  svg += `<defs><linearGradient id="gate-end-gradient" gradientUnits="userSpaceOnUse" x1="0" x2="0" y1="${entrySensorY+3}" y2="470"><stop stop-color="white"/><stop offset="1" stop-color="black"/></linearGradient><mask id="gate-end-fade" maskUnits="userSpaceOnUse" x="550" y="316" width="150" height="159"><rect x="550" y="316" width="150" height="159" fill="url(#gate-end-gradient)"/></mask></defs><g id="gate-filaments" mask="url(#gate-end-fade)">`;
  const lanes = s.tips.map((position, index) => ({position,index,offset:index-focus}))
    .filter(lane => lane.index !== s.gate && Math.abs(lane.offset) < 6)
    .sort((a,b) => Math.abs(b.offset)-Math.abs(a.offset));
  for (const {position,index,offset} of lanes) {
    const x = 620+offset*16;
    const y = Math.min(440,entrySensorY+Math.min(18,Math.abs(offset)*5))-20;
    const opacity = focus < 0 ? .2 : Math.max(.08,Math.exp(-Math.abs(offset)*.75));
    const color = s.gate_colors?.[index] || '808182';
    svg += `<g data-gate="${index}" opacity="${opacity}"><path d="M${x} 470 V${y}" stroke="#${color}" stroke-width="7" fill="none"/><text x="${x}" y="${Math.min(464,y+14)}" text-anchor="middle" font-size="8" fill="var(--scene-text, #304c65)">${index}</text></g>`;
  }
  svg += '</g>';
  const shapedTip = (position, color, part) => {
    const point = p => tube.getPointAtLength(progress(Math.min(p,end-residual))*length);
    const base = point(position-2), nose = point(position);
    const angle = Math.atan2(nose.y-base.y,nose.x-base.x), dx = -Math.sin(angle)*3.5, dy = Math.cos(angle)*3.5;
    // Overlap the cone slightly to avoid a visible seam, then taper over 15mm.
    const hair = Array.from({length:32},(_,i)=>point(position-.4+15.4*i/31));
    const sides = [1,-1].map(side=>hair.map((p,i)=>{
      const before=hair[Math.max(0,i-1)], after=hair[Math.min(hair.length-1,i+1)];
      const tangent=Math.atan2(after.y-before.y,after.x-before.x);
      const radius=.7*(1-i/31)+.04;
      return {x:p.x-side*Math.sin(tangent)*radius,y:p.y+side*Math.cos(tangent)*radius};
    }));
    const outline=[...sides[0],...sides[1].reverse()];
    return `<g data-part="${part}"><path data-part="filament-hair" d="${outline.map((p,i)=>`${i?'L':'M'}${p.x} ${p.y}`).join(' ')} Z" fill="#${color}"/><path d="M${base.x+dx} ${base.y+dy} L${nose.x} ${nose.y} L${base.x-dx} ${base.y-dy} Z" fill="#${color}"/></g>`;
  };
  const fadeY = tube.getPointAtLength(progress(s.layout.mmu_entry ?? -150) * length).y;
  svg += `<defs><linearGradient id="bowden-fade-gradient" gradientUnits="userSpaceOnUse" x1="0" x2="0" y1="${fadeY+3}" y2="470"><stop stop-color="white"/><stop offset="1" stop-color="black"/></linearGradient><mask id="bowden-end-fade" maskUnits="userSpaceOnUse" x="0" y="0" width="800" height="520"><rect width="800" height="520" fill="white"/><rect x="600" y="${fadeY+3}" width="40" height="${Math.max(0,470-fadeY-3)}" fill="url(#bowden-fade-gradient)"/></mask></defs>`;
  svg += `<defs><linearGradient id="bowden-tube-gradient" gradientUnits="userSpaceOnUse" x1="0" x2="0" y1="${fadeY-17}" y2="450"><stop stop-color="white"/><stop offset="1" stop-color="black"/></linearGradient><mask id="bowden-tube-fade" maskUnits="userSpaceOnUse" x="0" y="0" width="800" height="520"><rect width="800" height="520" fill="white"/><rect x="600" y="${fadeY-17}" width="40" height="${Math.max(0,520-fadeY+17)}" fill="url(#bowden-tube-gradient)"/></mask></defs>`;
  if (s.synced) {
    svg += `<g id="synced-indicator" aria-label="Extruder and MMU gears synced"><g transform="translate(674 295) rotate(-35)" fill="none" stroke="#53b9b0" stroke-width="2.2"><rect x="-12" y="-5" width="15" height="10" rx="5"/><rect x="-3" y="-5" width="15" height="10" rx="5"/></g><text x="674" y="316" text-anchor="middle" font-size="10" fill="var(--scene-text, #304c65)">Synced</text></g>`;
  }
  // Nozzle-relative reference guides, matching the documentation diagrams.
  const references = toolheadReferences(s).map(ref => ({...ref,
    y: tube.getPointAtLength(progress(ref.position) * length).y})).sort((a, b) => a.y - b.y);
  let previousLabelY = -Infinity;
  const mm = value => Number(value.toFixed(1));
  for (const ref of references) {
    const labelY = Math.max(ref.y, previousLabelY + 17);
    previousLabelY = labelY + (ref.gap != null ? 13 : 0);
    const extra = ref.gap != null ? `<tspan class="reference-gap" x="336" dy="13" text-anchor="middle">(${mm(ref.gap)}mm to entry)</tspan>` : '';
    svg += `<g class="toolhead-reference"><path d="M${ref.label === 'Extruder entry' ? 306 : 285} ${ref.y} H315 L330 ${labelY}" fill="none" stroke="var(--scene-border, #96abba)" stroke-width="1"/><text ${dimensionLink(ref.label)} x="336" y="${labelY}" dominant-baseline="middle" font-size="10" fill="var(--scene-muted, #6e8190)"><tspan class="reference-title">${ref.label} · ${mm(ref.distance)}mm</tspan>${extra}</text></g>`;
  }
  const cooling = coolingTubeMeasurements(s);
  if (cooling) {
    const startY = tube.getPointAtLength(progress(cooling.start) * length).y;
    const endY = tube.getPointAtLength(progress(cooling.end) * length).y;
    const midY = (startY + endY) / 2;
    svg += `<g class="cooling-measurements" fill="var(--scene-muted, #6e8190)" font-size="10"><path d="M285 ${startY} H330 M285 ${endY} H330" fill="none" stroke="var(--scene-border, #96abba)"/><path data-part="cooling-length-arrow" d="M315 ${startY-2} V${endY+2} M312 ${endY+6} L315 ${endY+2} L318 ${endY+6}" fill="none" stroke="var(--scene-text, #304c65)" stroke-width="1.2"/><text ${dimensionLink('Cooling tube position')} x="336" y="${startY}" dominant-baseline="middle">Cooling tube position · ${mm(cooling.position)}mm</text><text ${dimensionLink('Cooling tube length')} x="323" y="${midY}" dominant-baseline="middle">Cooling tube length · ${mm(cooling.length)}mm</text></g>`;
  }
  const unitSensors = Object.entries(s.sensors).filter(([name]) => {
    if (s.active_sensors) return s.active_sensors.includes(name);
    if (s.unit_sensors && !s.unit_sensors.includes(name)) return false;
    const gateSuffix = name.split(':').at(-1).match(/^mmu_.*_(\d+)$/);
    return !gateSuffix || Number(gateSuffix[1]) === s.gate;
  });
  const isBufferSensor = name => /(?:^|:)filament_(compression|tension)$/.test(name);
  const sensorFill = state => state === true ? '#399b64' : state === false ? '#fff' : '#9ba5ae';
  for (const [name, d] of unitSensors) {
    if (isBufferSensor(name)) continue;
    const suffix = name.split(':').at(-1).match(/^mmu_.*_(\d+)$/);
    if (suffix && Number(suffix[1]) !== gate) continue;
    if (d.position === null) continue;
    const pt = tube.getPointAtLength(progress(d.position) * length);
    const short = name.split(':').at(-1);
    if (/^mmu_entry(?:_\d+)?$/.test(short)) pt.y -= 33;
    if (/^mmu_exit(?:_\d+)?$/.test(short)) pt.y -= 5;
    const sensorLabel = /^mmu_entry(?:_\d+)?$/.test(short) ? 'entry' : /^mmu_exit(?:_\d+)?$/.test(short) ? 'exit' : short;
    svg += `<g class="sensor-marker${d.state === null ? ' sensor-disabled' : ''}" data-sensor="${escape(name)}"><text x="${pt.x > 400 ? pt.x + 29 : pt.x - 29}" y="${sensorLabel === 'entry' ? pt.y : pt.y + 4}" dominant-baseline="${sensorLabel === 'entry' ? 'central' : 'auto'}" text-anchor="${pt.x > 400 ? "start" : "end"}" font-size="10" fill="var(--scene-text, #456078)">${short === 'extruder' ? '' : escape(sensorLabel)}</text><circle cx="${pt.x + 16}" cy="${pt.y}" r="5" fill="${d.state === true ? "#399b64" : d.state === false ? "#fff" : "#9ba5ae"}" stroke="#698394"/></g>`;
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
    const pixelsPerMm = type === 'extruder' ? .27 * length / (end-entry)
      : 200 / -Math.min(-100, (s.layout.mmu_entry ?? -150)-40);
    const angle = gearAngle(distance, pixelsPerMm);
    for (const side of [-1, 1])
      svg += `<g transform="translate(${x + side * 23} ${y}) rotate(${(type === "extruder" ? -1 : 1) * side * angle})"><circle r="19" fill="var(--scene-gear, #d8e4ed)" stroke="var(--scene-border, #6d899f)"/><path d="M-14 0 H14 M0 -14 V14" stroke="var(--scene-spoke, #526f86)"/><circle class="gear-center" r="3" fill="var(--scene-spoke, #526f86)"/></g>`;
  }
  for (const [name, d] of Object.entries(s.devices)) {
    if (name.includes(':') && name.split(':')[0] !== s.unit) continue;
    if (name.endsWith(":encoder")) {
      const exitPositions = unitSensors.filter(([sensor, value]) => {
        const short = sensor.split(':').at(-1);
        return value.position !== null && (short === 'mmu_shared_exit' || short === 'mmu_exit' || short === `mmu_exit_${gate}`);
      }).map(([, value]) => progress(value.position)*length + 26);
      const pt = tube.getPointAtLength(Math.max(progress(s.layout.mmu_encoder ?? 40)*length, ...exitPositions));
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
  if (Math.abs(excess) >= .05) svg += `<text id="bowden-excess" x="640" y="${bowdenLengthLabel(s).includes('Uncalibrated') ? 134 : 118}" font-size="10" fill="var(--scene-text, #304c65)"><tspan x="640">Excess filament</tspan><tspan x="640" dy="14">${excess > 0 ? 'compression' : 'tension'} · ${Math.abs(excess).toFixed(1)}mm</tspan></text>`;
  const residual = s.filament?.residual_mm || 0;
  const fragment = s.filament?.fragment_mm || 0;
  const retainedColor = s.nozzle_color || s.filament?.retained_color || '808182';
  const residualTop = tube.getPointAtLength(progress(end-residual)*length).y;
  const nozzleZero = tube.getPointAtLength(progress(end)*length).y;
  if (s.retained_segment && !s.retained_segment.residual_only) {
    const segment = s.retained_segment;
    const top = tube.getPointAtLength(progress(segment.start)*length).y;
    const bottom = tube.getPointAtLength(progress(Math.max(segment.start,segment.end-(segment.tip_shape === 'cone' ? 2 : 0)))*length).y;
    const cutLength = Math.max(0, segment.end-segment.start);
    svg += `<path id="retained-fragment" d="M260 ${top} L260 ${bottom}" fill="none" stroke="#${segment.color}" stroke-width="7"/>${cutLength > .01 ? `<text id="cut-fragment-length" x="192" y="${(top+bottom)/2}" text-anchor="end" dominant-baseline="middle" font-size="9" fill="var(--scene-muted, #6e8190)">Cut fragment ${cutLength.toFixed(1)}mm</text>` : ''}`;
    if (segment.tip_shape === 'cone') svg += shapedTip(segment.end,segment.color,'fragment-tip');
  } else if (fragment > 0 && !Object.hasOwn(s, 'retained_segment')) {
    const top = tube.getPointAtLength(progress(end-residual-fragment)*length).y;
    svg += `<path id="retained-fragment" d="M260 ${top} L260 ${residualTop}" fill="none" stroke="#${retainedColor}" stroke-width="7"/>`;
  }
  if (s.tip_shapes?.[gate] === 'cone') svg += shapedTip(tip-residual,s.filament?.incoming_color || '808182','attached-tip');
  if (residual > 0) {
    svg += `<path id="residual-filament" d="M260 ${residualTop} L260 ${nozzleZero}" fill="none" stroke="#${retainedColor}" stroke-width="7"/><text ${dimensionLink('Residual')} x="241" y="452" text-anchor="end" font-size="9" fill="var(--scene-muted, #6e8190)">Residual ${residual}mm</text>`;
  }
  if (residual > 0 || tip >= end-.01) {
    svg += `<path id="nozzle-funnel" d="${nozzleFunnel}" fill="#${retainedColor}"/>`;
  }
  const {amount:coilAmount, layers, pitch, coils, drop} = purgeCoils(s, nozzleDiameter);
  if (coilAmount > 0) {
    let waste = '';
    for (const coil of coils) {
      const reveal = `fill="none" stroke-linecap="round" pathLength="100" stroke-dasharray="${coil.fraction*100} 100"`;
      waste += `<path d="${coil.path}" ${reveal} stroke="color-mix(in srgb, #${coil.color}, black 28%)" stroke-width="${nozzleWidth}" transform="translate(0,${nozzleWidth*.1})"/><path data-part="purge-material" d="${coil.path}" ${reveal} stroke="#${coil.color}" stroke-width="${nozzleWidth}"/><path d="${coil.path}" ${reveal} stroke="color-mix(in srgb, #${coil.color}, white 38%)" stroke-width="${nozzleWidth*.2}" transform="translate(0,${-nozzleWidth*.26})"/>`;
    }
    const landing = 498-Math.max(0,layers-1)*pitch;
    const strandTip = 455+(landing-455)*Math.min(1,coilAmount/1.2);
    const stem = Math.min(strandTip,463);
    const strand = `M260 455 V${stem}` + (strandTip>stem ? ` C257 ${stem+(strandTip-stem)/3} 264 ${stem+(strandTip-stem)*2/3} 260 ${strandTip}` : '');
    waste += `<path data-part="purge-stream" d="${strand}" fill="none" stroke="#${purgeColor(s,s.extruded_mm)}" stroke-width="${nozzleWidth}"/>`;
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
  $("filament").setAttribute("stroke-dasharray", measured);
  $("filament").setAttribute("opacity", s.gate_path_opacity ?? 1);
  for (const gap of $("scene").querySelectorAll('.reference-gap')) {
    const title=gap.parentNode.querySelector('.reference-title');
    gap.setAttribute('x',336+title.getComputedTextLength()/2);
  }
  const gearY = tube.getPointAtLength(progress(s.layout.extruder_gears ?? entry) * length).y;
  const bodyTop = gearY - 19 - 2 * (.27 * length / (end-entry));
  $("extruder-body").setAttribute('d', `M250 ${bodyTop} H242 Q240 ${bodyTop} 240 ${bodyTop+2} V435 H280 V${bodyTop+2} Q280 ${bodyTop} 278 ${bodyTop} H270`);
  $("sensors").replaceChildren(
    ...unitSensors.map(([name, d]) => {
      const el = document.createElement("button");
      el.type = "button";
      el.className = "chip sensor-chip" + (d.state === true ? " on" : d.state === null ? " disabled" : " off");
      el.textContent = name.split(':').at(-1);
      const enabled = d.enabled ?? d.state !== null;
      const atLatest = live && !paused && !playing && played >= events.at(-1).seq;
      el.disabled = !atLatest || busy;
      el.setAttribute('aria-pressed', String(enabled));
      el.setAttribute('aria-label', `${name}: ${enabled ? 'enabled' : 'disabled'}. Click to ${enabled ? 'disable' : 'enable'}`);
      el.dataset.tooltip = atLatest ? `${enabled ? 'Disable' : 'Enable'} ${name}` : 'Jump to live to change sensors';
      el.onclick = () => action(async () => {
        await rpc('sim.viewer.sensor', {name, enabled: !enabled});
        message(`Sensor ${name} ${enabled ? 'disabled' : 'enabled'}.`);
      });
      return el;
    }),
  );
  $("devices").replaceChildren(
    ...Object.entries(s.devices).map(([name, d]) => {
      const el = document.createElement("span");
      el.className = "chip";
      el.textContent = name + " · " + JSON.stringify(d, (key, value) =>
        name.includes('encoder') && typeof value === 'number' && Number.isFinite(value)
          ? Number(value.toFixed(1)) : value);
      return el;
    }),
  );
}
function renderTrace() {
  const view = $("trace"),
    scroll = view.scrollTop;
  view.replaceChildren(
    ...events.map((e, i) => {
      const b = document.createElement("button");
      const [sequence, time, type, ...detail] = traceText(e).split(' · ');
      const pill = document.createElement('span');
      pill.className = 'trace-type';
      pill.dataset.type = type;
      pill.textContent = type;
      b.append(`${sequence} · ${time} · `, pill);
      const reason = e.label || e.telemetry?.start?.label;
      if (e.kind === 'movement' && reason) {
        const heading = document.createElement('strong');
        heading.textContent = reason;
        const details = document.createElement('span');
        details.className = 'trace-details';
        details.textContent = detail.join(' · ');
        b.append(' · ', heading, details);
      } else b.append(` · ${detail.join(' · ')}`);
      // Keep diagnostic provenance available without crowding the movement summary.
      b.dataset.tooltip = [e.origin,
        e.fast_bowden && $('compress-bowden').checked ? 'Compressed Bowden playback' : '',
        ...(e.changes || []).map(c => `${configurationLabel(c.name)}: ${JSON.stringify(c.before)} → ${JSON.stringify(c.after)}`)]
        .filter(Boolean).join(' · ');
      b.classList.toggle("selected", i === currentIndex());
      b.onclick = () => seek(i);
      return b;
    }),
  );
  view.scrollTop = scroll;
  followTrace();
}
$("beginning").onclick = () => seek(0);
$("previous").onclick = () => seek(currentIndex() - 1);
$("next").onclick = () => seek(currentIndex() + 1);
function startPlayback(singleStep) {
  if (!events.length) return;
  if (!live) {
    const event = events[cursor];
    played = event.seq; displayed = event.state;
    playing = null; resumeAt = 0;
  }
  replayingHistory = singleStep || replayingHistory;
  stopAfterStep = singleStep;
  paused = false; live = true;
  draw(); followTrace();
}
$("replay").onclick = () => startPlayback(true);
$("play-end").onclick = () => startPlayback(false);
function followLive() {
  replayingHistory = false;
  stopAfterStep = false;
  live = true; paused = false; playing = null; sensorPulse = null; resumeAt = 0;
  played = events.at(-1)?.seq || 0;
  displayed = events.at(-1)?.state;
  draw(); followTrace();
}
$("live").onclick = followLive;
$("timeline").oninput = () => seek(Number($("timeline").value));
$("save").onclick = () => {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify({ version: 2, session, events }, null, 2)], {
      type: "application/json",
    }),
  );
  const a = document.createElement("a");
  a.href = url;
  a.download = "happy-hare-telemetry.json";
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};
$("compress-bowden").onchange = () => renderTrace();
$("nozzle-diameter").onchange = () => draw();
$("save-motion").onclick = async () => {
  try {
    const response = await fetch('/sim/viewer/telemetry');
    const { result } = await response.json();
    const text = result.records.map(r=>JSON.stringify(r)).join('\n') + '\n';
    const url = URL.createObjectURL(new Blob([text], {type:'application/x-ndjson'}));
    const link = document.createElement('a');
    link.href = url; link.download = 'happy-hare-motion.jsonl'; link.click();
    setTimeout(()=>URL.revokeObjectURL(url),1000);
  } catch (error) { message(error.message, true); }
};
async function poll() {
  try {
    const after = events.filter(e => !e.pending).at(-1)?.seq || 0;
    const res = await fetch("/sim/viewer/events?after=" + after);
    const data = (await res.json()).result;
    const changed = data.config_revision !== configRevision;
    const newSession = data.session !== session;
    if (newSession || changed) {
      const selectedSeq = events[currentIndex()]?.seq;
      const full = (await (await fetch('/sim/viewer/events')).json()).result;
      events = [...full.events, ...(full.pending_config ? [full.pending_config] : [])];
      configRevision = full.config_revision;
      if (playing) playing.event = events.find(e => e.seq === playing.event.seq) || playing.event;
      if (newSession) {
        if (session !== null) $('mini-console').replaceChildren();
        session = full.session; cursor = 0; live = true; paused = false; replayingHistory = false;
        stopAfterStep = false;
        sensorPulse = null; playing = null; resumeAt = 0;
        played = events.at(-1)?.seq || 0; displayed = events.at(-1)?.state;
      } else {
        cursor = Math.max(0, events.findIndex(e => e.seq === selectedSeq));
        if (live && !playing && played >= (events.at(-1)?.seq || 0)) {
          played = events.at(-1)?.seq || 0; displayed = events.at(-1)?.state;
        }
      }
    } else if (data.events.length) {
      const selectedSeq = events[currentIndex()]?.seq;
      events = [...events.filter(e => !e.pending && !data.events.some(n => n.seq === e.seq)),
                ...data.events, ...(data.pending_config ? [data.pending_config] : [])].slice(-3000);
      if (!live) cursor = Math.max(0, events.findIndex(e => e.seq === selectedSeq));
    }
    if (newSession || changed || data.events.length || !events.length) {
      draw(); renderTrace();
    }
  } catch (e) {
    $("connection").textContent = "Waiting for console…";
  }
  setTimeout(poll, 150);
}
function animate(now) {
  if (live && !paused && events.length) {
    if (!playing && now >= resumeAt) {
      const event = events.find(e=>e.seq>played);
      if (event) { playing={event,start:now}; followTrace(); }
    }
    if (playing) {
      const ms=duration(playing.event, $("compress-bowden").checked), f=ms?Math.min(1,(now-playing.start)/ms):1;
      playing.fraction = f;
      displayed=interpolate(playing.event,f);
      if (f===1) {
        const pulse = homingPulse(playing.event);
        if (pulse) sensorPulse = {...pulse, start:now};
        played=playing.event.seq;
        if (playing.event.kind === 'movement') resumeAt=now+300;
        playing=null;
        if (stopAfterStep) paused = true;
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
connect();
poll();
requestAnimationFrame(animate);
