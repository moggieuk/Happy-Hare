import { interpolate, duration } from './playback.js';
/* Copyright (C) 2022-2026 Paul Morgan. GPLv3. */
const $ = (id) => document.getElementById(id);
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
  document.querySelectorAll("aside button").forEach((b) => (b.disabled = true));
  try {
    await fn();
  } catch (e) {
    message(e.message, true);
  } finally {
    busy = false;
    document
      .querySelectorAll("aside button")
      .forEach((b) => (b.disabled = false));
  }
}
function connect() {
  socket = new WebSocket(
    `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/websocket`,
  );
  socket.onopen = () => {
    $("connection").textContent = "Connected to console";
    refresh().catch((e) => message(e.message, true));
  };
  socket.onmessage = (e) => {
    const data = JSON.parse(e.data);
    if (pending.has(data.id)) {
      const p = pending.get(data.id);
      pending.delete(data.id);
      data.error ? p.reject(Error(data.error.message)) : p.resolve(data.result);
    } else if (data.method === "notify_gcode_response") {
      const line = data.params[0];
      if (line.startsWith("!!")) message(line, true);
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
  options(
    $("parameter"),
    setup.parameters
      .filter((p) => p.name.includes(query))
      .map((p) => [p.name, p.name]),
  );
  if ([...$("parameter").options].some((o) => o.value === previous))
    $("parameter").value = previous;
  value();
}
function value() {
  const p = setup?.parameters.find((p) => p.name === $("parameter").value);
  $("value").value = p
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
      name: $("parameter").value,
      value: $("value").value,
    });
    filter();
    message("Runtime parameter applied.");
  });
$("send").onclick = () =>
  action(async () => {
    message("Command running…");
    await rpc("printer.gcode.script", { script: $("command").value });
    message("Command finished. Inspect the trace and console output.");
  });
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
let displayed = null, playing = null, played = 0;
function draw() {
  if (!events.length) return;
  cursor = Math.max(0, Math.min(cursor, events.length - 1));
  const e = live ? (playing?.event || events.find(e=>e.seq===played) || events.at(-1)) : events[cursor];
  const
    s = live && displayed ? displayed : e.state,
    gate = s.gate >= 0 ? s.gate : 0,
    tip = s.tips[gate] ?? -1000;
  const entry = s.layout.extruder_entry ?? 700,
    end = (s.layout.toolhead ?? 760) + 30;
  $("mode").textContent = live ? (played < events.at(-1).seq ? "Live · catching up" : "Live") : `Replay · event ${e.seq}`;
  $("position").textContent = `Gate ${gate} · tip ${tip.toFixed(2)} mm`;
  $("timeline").max = events.length - 1;
  $("timeline").value = live ? events.indexOf(e) : cursor;
  // Fixed path with a compressed Bowden section. Position maps monotonically to it.
  const path =
    "M620 415 L620 170 Q620 75 525 75 L355 75 Q260 75 260 170 L260 435";
  let svg = `<rect x="540" y="275" width="170" height="170" rx="12" fill="#edf2f6" stroke="#96abba"/><text x="558" y="302" fill="#304c65">MMU</text><rect x="222" y="205" width="76" height="230" rx="8" fill="#edf2f6" stroke="#96abba"/><rect x="213" y="375" width="94" height="60" rx="5" fill="#c4d3df"/><path d="M242 435 L260 455 L278 435" fill="#9cafbe"/><path id="tube" d="${path}" fill="none" stroke="#b1c4d2" stroke-width="20"/><path d="${path}" fill="none" stroke="#edf4f8" stroke-width="14"/><path id="filament" d="${path}" fill="none" stroke="#d84a83" stroke-width="7"/><text x="370" y="45" fill="#6e8190" font-size="13">Bowden · compressed scale</text>`;
  $("scene").innerHTML = svg;
  const tube = $("tube"),
    length = tube.getTotalLength();
  const progress = (p) =>
    p < 0
      ? Math.max(0, ((p + 100) / 100) * 0.17)
      : p < entry
        ? 0.17 + (0.56 * p) / entry
        : Math.min(1, 0.73 + (0.27 * (p - entry)) / (end - entry));
  const start = s.tails[gate] === null ? 0 : progress(s.tails[gate]) * length,
    stop = progress(tip) * length;
  $("filament").setAttribute(
    "stroke-dasharray",
    `0 ${start} ${Math.max(0, stop - start)} ${length + 1}`,
  );
  const unitSensors = Object.entries(s.sensors).filter(([name]) =>
    s.unit_sensors ? s.unit_sensors.includes(name) : !s.units.includes(name.split(':')[0]) || name.split(':')[0] === s.unit);
  const isBufferSensor = name => /(?:^|:)filament_(compression|tension)$/.test(name);
  const sensorFill = state => state === true ? '#399b64' : state === false ? '#fff' : '#9ba5ae';
  for (const [name, d] of unitSensors) {
    if (isBufferSensor(name)) continue;
    if (s.units.includes(name.split(':')[0]) && name.split(':')[0] !== s.unit) continue;
    const suffix = name.match(/_(\d+)$/);
    if (suffix && Number(suffix[1]) !== gate) continue;
    if (d.position === null) continue;
    const pt = tube.getPointAtLength(progress(d.position) * length);
    svg += `<text x="${pt.x > 400 ? pt.x + 29 : pt.x - 29}" y="${pt.y + 4}" text-anchor="${pt.x > 400 ? "start" : "end"}" font-size="10" fill="#456078">${escape(name.split(":").at(-1))}</text><circle cx="${pt.x + 16}" cy="${pt.y}" r="5" fill="${d.state === true ? "#399b64" : d.state === false ? "#fff" : "#9ba5ae"}" stroke="#698394"/>`;
  }
  for (const [x, y, type] of [
    [620, 355, "gear"],
    [260, 265, "extruder"],
  ]) {
    const angle =
      Object.entries(s.motors)
        .filter(([n]) => n.includes(type))
        .reduce((sum, [, v]) => sum + v, 0) * 3;
    for (const side of [-1, 1])
      svg += `<g transform="translate(${x + side * 23} ${y}) rotate(${side * angle})"><circle r="19" fill="#d8e4ed" stroke="#6d899f"/><path d="M-14 0 H14 M0 -14 V14" stroke="#526f86"/></g>`;
  }
  for (const [name, d] of Object.entries(s.devices)) {
    if (name.includes(':') && name.split(':')[0] !== s.unit) continue;
    if (name.endsWith(":encoder")) {
      const pt = tube.getPointAtLength(
        progress(s.layout.mmu_encoder ?? 40) * length,
      );
      svg += `<circle cx="${pt.x}" cy="${pt.y}" r="11" fill="#faf4d9" stroke="#9d8e52"/><path d="M${pt.x - 7} ${pt.y} H${pt.x + 7}" stroke="#9d8e52"/><text x="${pt.x - 25}" y="${pt.y + 4}" text-anchor="end" font-size="11">Encoder</text>`;
    }
    if (name.endsWith(":buffer")) {
      const fraction = Math.max(0, Math.min(1, ((d.value ?? 0) + 1) / 2));
      svg += `<rect x="35" y="30" width="180" height="92" rx="7" fill="#e7eff5" stroke="#93acbf"/><text x="45" y="50" font-size="12">Sync-feedback buffer</text><path d="M50 70 H200" stroke="#a1b7c7" stroke-width="5"/><circle cx="${50 + 150 * fraction}" cy="70" r="7" fill="#539eaa"/>`;
      const bufferSensors = unitSensors.filter(([sensor]) => isBufferSensor(sensor));
      bufferSensors.forEach(([sensor, data], index) => {
        const x = 47 + index * 85;
        const label = sensor.endsWith('tension') ? 'Tension' : 'Compression';
        svg += `<circle cx="${x}" cy="101" r="4" fill="${sensorFill(data.state)}" stroke="#698394"/><text x="${x+8}" y="104" font-size="9" fill="#456078">${label}</text>`;
      });
    }
  }
  // Keep the measured filament stroke when appending markers and gears.
  const measured = $("filament").getAttribute("stroke-dasharray");
  $("scene").innerHTML = svg;
  $("filament").setAttribute("stroke-dasharray", measured);
  $("sensors").replaceChildren(
    ...unitSensors.map(([name, d]) => {
      const el = document.createElement("span");
      el.className = "chip sensor-chip" + (d.state === true ? " on" : d.state === null ? " disabled" : " off");
      el.textContent = name.split(':').at(-1);
      el.setAttribute('aria-label', `${name}: ${d.state === null ? 'disabled' : d.state ? 'present' : 'clear'}`);
      return el;
    }),
  );
  $("devices").replaceChildren(
    ...Object.entries(s.devices).map(([name, d]) => {
      const el = document.createElement("span");
      el.className = "chip";
      el.textContent = name + " · " + JSON.stringify(d);
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
      b.textContent = `${e.seq} · ${e.time.toFixed(3)}s · ${e.kind} · ${e.label}${e.distance == null ? "" : ` · ${e.distance.toFixed(3)} mm`}`;
      if (e.kind === 'movement') {
        if (e.speed > 0) b.textContent += ` · ${e.speed.toFixed(1)} mm/s`;
        if (e.fast_bowden) b.textContent += ' · compressed Bowden playback';
        b.textContent += `${e.requested == null ? '' : ` · requested ${e.requested.toFixed(3)} mm`}${e.homing ? ` · ${e.endstop || 'endstop'} ${e.homed === true ? 'reached' : e.homed === false ? 'not reached' : ''}` : ''}${e.error ? ` · ${e.error}` : ''}`;
      }
      b.classList.toggle("selected", i === (live ? events.length - 1 : cursor));
      b.onclick = () => {
        live = false;
        cursor = i;
        draw();
        renderTrace();
      };
      return b;
    }),
  );
  view.scrollTop = live ? view.scrollHeight : scroll;
}
$("previous").onclick = () => {
  cursor = Math.max(0, (live ? events.length - 1 : cursor) - 1);
  live = false;
  draw();
  renderTrace();
};
$("next").onclick = () => {
  cursor = Math.min(events.length - 1, cursor + 1);
  live = false;
  draw();
  renderTrace();
};
$("live").onclick = () => {
  live = true;
  playing = null;
  played = events.at(-1)?.seq || 0;
  displayed = events.at(-1)?.state;
  draw();
  renderTrace();
};
$("timeline").oninput = () => {
  live = false;
  cursor = Number($("timeline").value);
  draw();
};
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
async function poll() {
  try {
    const after = events.at(-1)?.seq || 0;
    const res = await fetch("/sim/viewer/events?after=" + after);
    const data = (await res.json()).result;
    if (data.session !== session) {
      session = data.session;
      events = [];
      cursor = 0;
      live = true;
      const full = (await (await fetch("/sim/viewer/events")).json()).result;
      events = full.events;
      playing = null;
      played = events.at(-1)?.seq || 0;
      displayed = events.at(-1)?.state;
    } else if (data.events.length) {
      const seq = events[cursor]?.seq;
      events.push(...data.events);
      events = events.slice(-3000);
      if (!live)
        cursor = Math.max(
          0,
          events.findIndex((e) => e.seq === seq),
        );
    }
    if (data.events.length || !events.length) {
      draw();
      renderTrace();
    }
  } catch (e) {
    $("connection").textContent = "Waiting for console…";
  }
  setTimeout(poll, 150);
}
function animate(now) {
  if (live && events.length) {
    if (!playing) {
      const event = events.find(e=>e.seq>played);
      if (event) playing={event,start:now};
    }
    if (playing) {
      const ms=duration(playing.event), f=ms?Math.min(1,(now-playing.start)/ms):1;
      displayed=interpolate(playing.event,f);
      if (f===1) {played=playing.event.seq;playing=null;}
      draw();
    }
  } else if (playing) {
    playing.start=now;
  }
  requestAnimationFrame(animate);
}
connect();
poll();
requestAnimationFrame(animate);
