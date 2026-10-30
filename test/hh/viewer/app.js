import { interpolate, duration, purgePath, gearAngle, CommandHistory, traceText, homingPulse, bladeStroke, configurationLabel } from './playback.js';
/* Copyright (C) 2022-2026 Paul Morgan. GPLv3. */
const $ = (id) => document.getElementById(id);
function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const next = theme === 'dark' ? 'Light' : 'Dark';
  const icons = {"light": "<svg aria-hidden=\"true\" xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 24 24\"><path d=\"M12 18c-.89 0-1.74-.2-2.5-.55C11.56 16.5 13 14.42 13 12s-1.44-4.5-3.5-5.45C10.26 6.2 11.11 6 12 6a6 6 0 0 1 6 6 6 6 0 0 1-6 6m8-9.31V4h-4.69L12 .69 8.69 4H4v4.69L.69 12 4 15.31V20h4.69L12 23.31 15.31 20H20v-4.69L23.31 12z\"/></svg>", "dark": "<svg aria-hidden=\"true\" xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 24 24\"><path d=\"M12 8a4 4 0 0 0-4 4 4 4 0 0 0 4 4 4 4 0 0 0 4-4 4 4 0 0 0-4-4m0 10a6 6 0 0 1-6-6 6 6 0 0 1 6-6 6 6 0 0 1 6 6 6 6 0 0 1-6 6m8-9.31V4h-4.69L12 .69 8.69 4H4v4.69L.69 12 4 15.31V20h4.69L12 23.31 15.31 20H20v-4.69L23.31 12z\"/></svg>"};
  $('theme-toggle').innerHTML = icons[next.toLowerCase()];
  $('theme-toggle').title = 'Switch to ' + next.toLowerCase() + ' mode';
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
    const label = p.macro ? p.macro : p.group ? `${p.group} · ${p.section}` : (p.section || 'Parameters');
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push(p);
  }
  $("parameter").replaceChildren(...[...groups].map(([label, matches]) => {
    const group = document.createElement('optgroup');
    group.label = `-- ${label} --`;
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
let paused = false, pausedAt = 0, sensorPulse = null;
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
  live = false; paused = true; playing = null; sensorPulse = null;
  draw(); followTrace();
}
function draw() {
  if (!events.length) return;
  cursor = Math.max(0, Math.min(cursor, events.length - 1));
  $("replay").disabled = !events.length || (live && !paused);
  $("pause").disabled = !live || paused;
  $("beginning").disabled = $("previous").disabled = currentIndex() <= 0;
  $("next").disabled = currentIndex() >= events.length - 1;
  const e = live ? (playing?.event || events.find(e=>e.seq===played) || events.at(-1)) : events[cursor];
  const
    s = live && displayed ? displayed : e.state,
    gate = s.gate >= 0 ? s.gate : 0,
    tip = s.tips[gate] ?? -1000;
  const entry = s.layout.extruder_entry ?? 700,
    end = s.layout.nozzle ?? (s.layout.toolhead ?? 760) + 30;
  $("mode").textContent = live && paused ? `Paused · event ${e.seq}` : live ? (played < events.at(-1).seq ? "Live · catching up" : "Live") : `Replay · event ${e.seq}`;
  $("position").textContent = `Gate ${gate} · ${s.filament_pos_name || "Unknown state"} · tip ${tip.toFixed(2)} mm`;
  $("timeline").max = events.length - 1;
  $("timeline").value = live ? events.indexOf(e) : cursor;
  // Fixed path with a compressed Bowden section. Position maps monotonically to it.
  const path =
    "M620 455 L620 170 Q620 75 525 75 L355 75 Q260 75 260 170 L260 445";
  const bowdenPath = path.replace("L260 445", "L260 435");
  let svg = `<rect x="540" y="275" width="170" height="200" rx="12" fill="var(--scene-body, #edf2f6)" stroke="var(--scene-border, #96abba)"/><text x="558" y="302" fill="var(--scene-text, #304c65)">MMU</text><path id="extruder-body" d="M250 205 H240 V435 H280 V205 H270" fill="var(--scene-body, #edf2f6)" stroke="var(--scene-border, #96abba)"/><rect x="213" y="375" width="94" height="60" rx="5" fill="var(--scene-heater, #c4d3df)"/><path d="M242 435 L260 455 L278 435" fill="var(--scene-nozzle, #9cafbe)"/><g mask="url(#bowden-end-fade)"><path id="tube" d="${path}" fill="none" stroke="none"/><path d="${bowdenPath}" fill="none" stroke="var(--scene-tube-border, #b1c4d2)" stroke-width="20"/><path d="${bowdenPath}" fill="none" stroke="var(--scene-tube, #edf4f8)" stroke-width="14"/><path d="M260 435 V445" stroke="var(--scene-tube, #edf4f8)" stroke-width="7"/><path id="filament" d="${path}" fill="none" stroke="#${s.filament?.incoming_color || "808182"}" stroke-width="7"/></g><text x="370" y="45" fill="var(--scene-muted, #6e8190)" font-size="13">Bowden · compressed scale</text>`;
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
    stop = progress(tip) * length;
  $("filament").setAttribute(
    "stroke-dasharray",
    `0 ${start} ${Math.max(0, stop - start)} ${length + 1}`,
  );
  const fadeY = tube.getPointAtLength(progress(s.layout.mmu_entry ?? -150) * length).y;
  svg += `<defs><linearGradient id="bowden-fade-gradient" gradientUnits="userSpaceOnUse" x1="0" x2="0" y1="${fadeY+3}" y2="455"><stop stop-color="white"/><stop offset="1" stop-color="black"/></linearGradient><mask id="bowden-end-fade" maskUnits="userSpaceOnUse" x="0" y="0" width="800" height="520"><rect width="800" height="520" fill="white"/><rect x="600" y="${fadeY+3}" width="40" height="${Math.max(0,455-fadeY-3)}" fill="url(#bowden-fade-gradient)"/></mask></defs>`;
  if (s.synced) {
    svg += `<g id="synced-indicator" aria-label="Extruder and MMU gears synced"><g transform="translate(674 295) rotate(-35)" fill="none" stroke="#53b9b0" stroke-width="2.2"><rect x="-12" y="-5" width="15" height="10" rx="5"/><rect x="-3" y="-5" width="15" height="10" rx="5"/></g><text x="674" y="316" text-anchor="middle" font-size="10" fill="var(--scene-text, #304c65)">Synced</text></g>`;
  }
  for (const cutter of s.cutters || []) {
    const pt = tube.getPointAtLength(progress(cutter.position) * length);
    const right = cutter.kind === 'mmu';
    const direction = right ? -1 : 1;
    const pressed = bladeStroke(live ? playing?.event : null, playing?.fraction || 0, cutter.kind);
    // The triangle tip rests at the 20px tube's outer wall, then crosses it.
    const x = pt.x - direction * 10 + direction * 20 * pressed;
    svg += `<g class="cutter-blade" aria-label="${right ? 'MMU servo cutter' : cutter.before_extruder ? 'Cutter before extruder' : 'Cutter inside extruder'}"><path data-part="blade" data-pressed="${pressed}" d="M${x} ${pt.y} l${-direction*42} -5 v10 Z" fill="var(--scene-nozzle, #9cafbe)" stroke="var(--scene-spoke, #526f86)"/><text x="${pt.x+(right?52:-52)}" y="${pt.y-8}" text-anchor="${right?'start':'end'}" font-size="10" fill="var(--scene-text, #304c65)">${right?'MMU cutter':'Blade position'}</text></g>`;
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
    const sensorLabel = /^mmu_entry(?:_\d+)?$/.test(short) ? 'entry' : /^mmu_exit(?:_\d+)?$/.test(short) ? 'exit' : short;
    svg += `<g class="sensor-marker${d.state === null ? ' sensor-disabled' : ''}" data-sensor="${escape(name)}"><text x="${pt.x > 400 ? pt.x + 29 : pt.x - 29}" y="${pt.y + 4}" text-anchor="${pt.x > 400 ? "start" : "end"}" font-size="10" fill="var(--scene-text, #456078)">${escape(sensorLabel)}</text><circle cx="${pt.x + 16}" cy="${pt.y}" r="5" fill="${d.state === true ? "#399b64" : d.state === false ? "#fff" : "#9ba5ae"}" stroke="#698394"/></g>`;
    if (sensorPulse?.sensor === name) {
      const elapsed = (performance.now() - sensorPulse.start) / 650;
      if (elapsed >= 0 && elapsed < 1) {
        const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
        svg += `<circle data-part="sensor-pulse" cx="${pt.x + 16}" cy="${pt.y}" r="${reduced ? 10 : 7 + elapsed * 15}" fill="none" stroke="${sensorPulse.reverse ? 'var(--sensor-clear-pulse)' : '#48c982'}" stroke-width="2.5" opacity="${1-elapsed}"/>`;
      }
    }
  }
  for (const [x, y, type] of [
    [620, 355, "gear"],
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
      svg += `<g id="encoder-marker"><g transform="rotate(${angle} ${pt.x} ${pt.y})"><circle cx="${pt.x}" cy="${pt.y}" r="11" fill="#faf4d9" stroke="#9d8e52"/><path d="M${pt.x - 7} ${pt.y} H${pt.x + 7} M${pt.x} ${pt.y - 7} V${pt.y + 7}" stroke="#9d8e52"/><circle cx="${pt.x + 7}" cy="${pt.y}" r="2" fill="#9d8e52"/></g><text x="${pt.x - 25}" y="${pt.y + 4}" text-anchor="end" font-size="11" fill="var(--scene-text, #304c65)">Encoder</text></g>`;
    }
    if (name.endsWith(":buffer")) {
      const fraction = Math.max(0, Math.min(1, ((d.value ?? 0) + 1) / 2));
      svg += `<rect x="35" y="30" width="180" height="92" rx="7" fill="var(--scene-body, #e7eff5)" stroke="#93acbf"/><text x="45" y="50" font-size="12" fill="var(--scene-text, #304c65)">Sync-feedback buffer</text><path d="M50 70 H200" stroke="#a1b7c7" stroke-width="5"/><circle cx="${50 + 150 * fraction}" cy="70" r="7" fill="#539eaa"/>`;
      const bufferSensors = unitSensors.filter(([sensor]) => isBufferSensor(sensor));
      bufferSensors.forEach(([sensor, data], index) => {
        const x = 47 + index * 85;
        const label = sensor.endsWith('tension') ? 'Tension' : 'Compression';
        svg += `<circle cx="${x}" cy="101" r="4" fill="${sensorFill(data.state)}" stroke="#698394"/><text x="${x+8}" y="104" font-size="9" fill="var(--scene-text, #456078)">${label}</text>`;
      });
    }
  }
  const residual = s.filament?.residual_mm || 0;
  const fragment = s.filament?.fragment_mm || 0;
  const retainedColor = s.nozzle_color || s.filament?.retained_color || '808182';
  const residualTop = tube.getPointAtLength(progress(end-residual)*length).y;
  if (fragment > 0) {
    const top = tube.getPointAtLength(progress(end-residual-fragment)*length).y;
    svg += `<path id="retained-fragment" d="M260 ${top} L260 ${residualTop}" fill="none" stroke="#${retainedColor}" stroke-width="7"/>`;
  }
  if (residual > 0) {
    svg += `<path id="residual-filament" d="M260 ${residualTop} L260 445" fill="none" stroke="#${retainedColor}" stroke-width="7"/><text x="241" y="452" text-anchor="end" font-size="9" fill="var(--scene-muted, #6e8190)">Residual ${residual}mm</text>`;
  }
  if (residual > 0 || tip >= end-.01) {
    svg += `<path id="nozzle-funnel" d="M256.5 445 Q256.5 451 258.25 455 L261.75 455 Q263.5 451 263.5 445 Z" fill="#${retainedColor}"/>`;
  }
  const coilAmount = Math.max(0, (s.extruded_mm || 0)-(s.purge_base_mm || 0)) % 36;
  const drop = s.purge_drop ?? Math.max(0,(coilAmount-30)/6);
  const purge = purgePath(Math.min(30,coilAmount));
  if (purge) {
    const turns = Math.min(10, coilAmount / 3);
    const x = 260 + 22 * Math.min(1, turns / .5) * (1 - .065 * turns) * Math.sin(turns * Math.PI * 2);
    const y = 498 - turns * 2.6 + 2 * Math.cos(turns * Math.PI * 2);
    const strand = !drop && tip >= end - .01 ? `M260 455 Q260 ${y - 8} ${x} ${y}` : '';
    const startColor = `#${s.purge_color_start || s.nozzle_color || '808182'}`;
    const endColor = `#${s.nozzle_color || '808182'}`;
    // Match the documentation: keep a broad material-colored center, with a
    // tinted lower edge and a narrow highlight rather than black/white stripes.
    const gradient = (id, shade) => `<linearGradient id="${id}" gradientUnits="userSpaceOnUse" x1="0" y1="500" x2="0" y2="${y}"><stop stop-color="${shade(startColor)}"/><stop offset="1" stop-color="${shade(endColor)}"/></linearGradient>`;
    svg += `<defs>${gradient('purge-color', c => c)}${gradient('purge-shadow', c => `color-mix(in srgb, ${c}, black 28%)`)}${gradient('purge-highlight', c => `color-mix(in srgb, ${c}, white 38%)`)}<clipPath id="purge-screen"><rect x="210" y="435" width="150" height="70"/></clipPath></defs><g clip-path="url(#purge-screen)"><g id="purge-pile" transform="translate(0,${drop*75})" aria-label="Extruded filament"><path d="${strand} ${purge}" fill="none" stroke="url(#purge-shadow)" stroke-width="5.8" transform="translate(0,.5)" stroke-linecap="round" stroke-linejoin="round"/><path data-part="purge-material" d="${strand} ${purge}" fill="none" stroke="url(#purge-color)" stroke-width="4.6" stroke-linecap="round" stroke-linejoin="round"/><path d="${purge}" transform="translate(0,-1.2)" fill="none" stroke="url(#purge-highlight)" stroke-width="1.1" stroke-linecap="round" stroke-linejoin="round"/><text x="303" y="491" font-size="10" fill="var(--scene-muted, #6e8190)">${s.extruded_mm.toFixed(1)} mm extruded</text></g></g>`;
  }
  // Keep the measured filament stroke when appending markers and gears.
  const measured = $("filament").getAttribute("stroke-dasharray");
  $("scene").innerHTML = svg;
  $("filament").setAttribute("stroke-dasharray", measured);
  const gearY = tube.getPointAtLength(progress(s.layout.extruder_gears ?? entry) * length).y;
  const bodyTop = gearY - 19 - 2 * (.27 * length / (end-entry));
  $("extruder-body").setAttribute('d', `M250 ${bodyTop} H240 V435 H280 V${bodyTop} H270`);
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
      el.title = atLatest ? `${enabled ? 'Disable' : 'Enable'} ${name}` : 'Jump to live to change sensors';
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
      b.append(`${sequence} · ${time} · `, pill, ` · ${detail.join(' · ')}`);
      // Keep diagnostic provenance available without crowding the movement summary.
      b.title = [e.label, e.origin,
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
$("pause").onclick = () => {
  paused = true; pausedAt = performance.now();
  draw(); followTrace();
};
$("replay").onclick = () => {
  if (!events.length) return;
  if (live && paused) {
    const delay = performance.now() - pausedAt;
    if (playing) playing.start += delay;
    resumeAt += delay;
  } else {
    const event = events[cursor];
    played = event.seq; displayed = event.state;
    playing = null; resumeAt = 0;
  }
  paused = false; live = true;
  draw(); followTrace();
};
$("live").onclick = () => {
  live = true; paused = false; playing = null; sensorPulse = null; resumeAt = 0;
  played = events.at(-1)?.seq || 0;
  displayed = events.at(-1)?.state;
  draw(); followTrace();
};
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
      if (newSession) {
        session = full.session; cursor = 0; live = true; paused = false;
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
