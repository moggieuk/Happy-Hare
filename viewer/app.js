import {TelemetryStream, recordControl} from './telemetry-stream.js';
import {LiveState, liveFrame} from './live-state.js';
import {liveMachine} from './live-config.js';
import {simulatorApi, detectConnection} from './simulator.js';
import {overlayAllowed, overlaySnapshot} from './overlay.js';
import {parameterCommand, macroCommand, editorParameters, macroCatalog, editorMacroParameters, toolheadObjects} from './editor.js';
import {TelemetryReplay, parseTelemetry} from './telemetry.js';
import {timelineMarkers} from './playback.js';
import {selectorNotice, orderedSteps, homingHold, nozzleArrivalTime, playbackFrame, isInternalUpdate, changeLines, visibleSteps, toolchangeIndex, annotatedStep, interpolate, duration, CommandHistory, traceText, homingPulse, configurationLabel} from './playback.js';
import {$, options} from './util.js';
import {consoleLine, consoleEndsWith, scrollConsoleToEnd} from './console-log.js';
import {createToolheadPanel} from './toolhead-panel.js';
import {initSpoolPicker} from './spool-picker.js';
import {createScene} from './scene.js';
import {renderControls} from './controls.js';
import {renderMacros, renderSensors, renderDevices} from './readout.js';
import './chrome.js';
const sim = simulatorApi(rpc);
/* Copyright (C) 2022-2026 Paul Morgan. GPLv3. */
let modeChosen=false, viewGeneration=0;
let statusSensors=null, statusRecordMode=null, overlayCache={key:'',state:null};
let viewMode='live', liveState=new LiveState(), liveTarget=null, liveFrom=null, liveStart=0, subscriptionGeneration=0;
let offline = false, sourceRecords = [], sourceReplay = new TelemetryReplay();
// The Connection panel's status: a dot coloured by `state` (connected, pending, warning, offline or disconnected) and a label.
function setConnection(text, state) {
  $('connection').textContent = text;
  $('connection').dataset.state = state;
}
function showConnection() {
  const kind = setup?.connection_kind;
  if (socket?.readyState !== 1) return setConnection('Disconnected · retrying', 'disconnected');
  setConnection(kind === 'simulator' ? 'Connected to the simulator' : kind === 'live_machine' ? 'Connected to printer' : 'Connected · unknown source', 'connected');
}
function renderRecordControl() {
  const {visible,value}=recordControl({viewMode,offline,simulator:isSimulator(),connected:socket?.readyState===1,mode:statusRecordMode});
  $('record-row').hidden=!visible;
  if (visible) $('record-mode').value=value;
}
$('record-mode').onchange=()=>rpc('printer.gcode.script',{script:`MMU_TELEMETRY MODE=${$('record-mode').value}`})
  .catch(error=>{ message(error.message,true); renderRecordControl(); });
function sourceMode() {
  renderRecordControl();
  toolheadPanel.element.hidden=offline || socket?.readyState !== 1 || toolheadPanel.element.dataset.available !== 'true';
  $('recording-option').hidden=!offline;
  $('view-mode').value=offline ? 'recording' : viewMode;
  $('sidebar-playback').hidden=viewMode==='live';
  $('trace').closest('.card').hidden=viewMode==='live';
  $('recording-name').hidden = !offline;
  $('connect-printer').textContent = offline ? 'Reconnect' : 'Connect';
  $('save-motion').disabled = viewMode==='live' || offline;
  $('simulator-controls').hidden = !isSimulator() || offline;
  $('runtime-parameters').hidden = offline;
  document.querySelectorAll('#simulator-controls input, #simulator-controls select, #simulator-controls button, #runtime-parameters input, #runtime-parameters select, #runtime-parameters button, #command-form input, #command-form button, [data-simulator-config] input, [data-simulator-config] select, [data-simulator-config] button')
    .forEach(node=>node.disabled=offline);
  $('live').dataset.tooltip = offline ? 'End of recording' : 'End, follow stream';
  $('live').setAttribute('aria-label', $('live').dataset.tooltip);
  renderWarning();
}

function renderWarning(warnings=[], notice=null) {
  const simulatorLive=viewMode==='live' && !offline && socket?.readyState===1 && isSimulator();
  const text=simulatorLive
    ? 'Simulator subscriptions may skip movement updates. Select Telemetry stream for complete movement animation.'
    : viewMode!=='playback' ? ''
    : [notice, warnings.length ? 'Displayed tip adjusted to sensor readings: '+warnings.join('; ') : ''].filter(Boolean).join('\n');
  // A notice alone is informational (blue, "i"); anything that is a warning keeps the warning look
  const informational=Boolean(notice) && !warnings.length && viewMode==='playback';
  $('filament-warning').dataset.kind=informational ? 'notice' : 'warning';
  $('filament-warning').querySelector('.warning-bang').textContent=informational ? 'i' : '!';
  $('filament-warning').hidden=!text;
  $('filament-warning-message').textContent=text;
}

const RPC_TIMEOUT_MS = 15000;
let socket, reconnectTimer, connectionGeneration=0,
  connectionUrl=location.protocol === 'file:' ? 'http://localhost:7125' : location.origin,
  serial = 0,
  pending = new Map(),
  events = [],
  rawEvents = [],
  session = null,
  live = true,
  cursor = 0,
  setup = null,
  busy = false;
function message(text, error = false) {
  $("message").textContent = text;
  $("message").classList.toggle("error", error);
}
function isSimulator() { return setup?.connection_kind === 'simulator'; }
function rpc(method, params = {}) {
  return new Promise((resolve, reject) => {
    if (offline) return reject(Error("Select a live source before changing the printer"));
    if (socket?.readyState !== 1) return reject(Error("Console disconnected"));
    const id = ++serial;
    // A lost reply must not hold the viewer forever; only G-code is allowed to take as long as it takes.
    const timer = method === 'printer.gcode.script' ? null
      : setTimeout(() => { if (pending.delete(id)) reject(Error(`${method} timed out`)); }, RPC_TIMEOUT_MS);
    const done = (settle) => (value) => { clearTimeout(timer); settle(value); };
    pending.set(id, { resolve: done(resolve), reject: done(reject) });
    socket.send(JSON.stringify({ jsonrpc: "2.0", id, method, params }));
  });
}
// `report` shows a failure in the status line too; the command box leaves that to the console, which already has it.
async function action(fn, report = true) {
  if (busy) return;
  busy = true;
  const disabled = [...document.querySelectorAll("aside button, #send")].filter((b) => !b.disabled);
  disabled.forEach((b) => (b.disabled = true));
  try {
    await fn();
  } catch (e) {
    if (report) message(e.message, true);
    if (!consoleEndsWith(e.message)) consoleLine(e.message, 'error');
  } finally {
    busy = false;
    disabled.forEach((b) => (b.disabled = false));
    if (offline) sourceMode();
  }
}
function connect() {
  const generation=++connectionGeneration;
  const endpoint=new URL(connectionUrl);
  endpoint.protocol=endpoint.protocol==='https:' || endpoint.protocol==='wss:' ? 'wss:' : 'ws:';
  endpoint.pathname='/websocket'; endpoint.search=''; endpoint.hash='';
  socket = new WebSocket(endpoint);
  socket.onopen = () => {
    if (generation!==connectionGeneration) return;
    setup=null;
    liveState=new LiveState(); statusSensors=null; statusRecordMode=null;
    sourceMode();
    if (offline) return;
    setConnection("Connected · identifying source…", "pending");
    initializeConnection().catch((e) => message(e.message, true));
  };
  socket.onmessage = (e) => {
    if (generation!==connectionGeneration) return;
    if (offline) return;
    const data = JSON.parse(e.data);
    if (pending.has(data.id)) {
      const p = pending.get(data.id);
      pending.delete(data.id);
      data.error ? p.reject(Error(data.error.message)) : p.resolve(data.result);
    } else if (data.method === 'notify_mmu_telemetry') {
      telemetryStream.receive(data.params[0]);
    } else if (data.method === 'notify_status_update') {
      if (viewMode==='live') acceptLive(data.params[0],data.params[1]);
      else {
        const mmu=data.params[0].mmu;
        if (mmu?.sensors) { statusSensors=mmu.sensors; draw(); }
        if (mmu && 'telemetry_mode' in mmu) { statusRecordMode=mmu.telemetry_mode; renderRecordControl(); }
      }
    } else if (['notify_klippy_ready','notify_klippy_shutdown','notify_klippy_disconnected'].includes(data.method)) {
      if (offline) return;
      if (data.method==='notify_klippy_ready') reloadEditor();
      if (viewMode!=='live') {
        telemetryStream.stop(false);
        if (data.method==='notify_klippy_ready') startStream().catch(e=>message(e.message,true));
        return;
      }
      liveState=new LiveState(); statusSensors=null; statusRecordMode=null; liveTarget=null; displayed=null; playing=null; sensorPulse=null;
      if (data.method==='notify_klippy_ready') subscribeLive().catch(e=>message(e.message,true));
      else { $('mode').textContent='Live view · Printer unavailable'; $('scene').replaceChildren(); }
    } else if (data.method === "notify_gcode_command") {
      consoleLine(data.params[0], "command");
    } else if (data.method === "notify_gcode_response") {
      consoleLine(data.params[0]);
    }
  };
  socket.onclose = () => {
    if (generation!==connectionGeneration) return;
    subscriptionGeneration++;
    telemetryStream.stop(false);
    statusSensors=null; statusRecordMode=null;
    if (liveTarget) { liveTarget.homing=null; liveTarget.espoolers={}; }
    if (viewMode==='live' && displayed) displayed.espoolers={};
    draw();
    sourceMode();
    if (!offline) setConnection("Disconnected · retrying", "disconnected");
    pending.forEach((p) => p.reject(Error("Console disconnected")));
    pending.clear();
    reconnectTimer=setTimeout(connect, 1500);
  };
}
async function initializeConnection() {
  const generation=connectionGeneration;
  editorStatic=null;
  const probe=await detectConnection(rpc,Number($('unit').value || 0));
  if (generation!==connectionGeneration || socket?.readyState!==1) return;
  if (probe.connection_kind==='live_machine') {
    setup=probe;
    if ($('command').value==='MMU_LOAD') $('command').value=''; // The simulator's default must not be one Enter away on a printer
    setConnection('Connected to printer', 'connected'); sourceMode();
    await refreshLiveEditor().catch(error=>message(error.message,true));
  } else await refresh(probe);
  if (generation!==connectionGeneration || offline) return;
  if (!modeChosen && isSimulator()) await changeView('playback');
  else if (viewMode==='live') await subscribeLive();
  else await startStream();
}
async function subscribeLive() {
  const generation=++subscriptionGeneration;
  const {objects}=await rpc('printer.objects.list');
  // Startup configuration and calibration come from Klipper itself; later edits arrive in mmu.config_changes.
  const startup=(await rpc('printer.objects.query',{objects:{configfile:['settings'],save_variables:['variables']}})).status;
  const config=liveMachine(startup);
  const references=new Set(Object.values(config?.units || {}).flatMap(u=>[u.extruder.name,...(u.heaters || [])]));
  const wanted=Object.fromEntries(objects.filter(n=>references.has(n) || n==='mmu' || n==='save_variables' ||
    /^mmu_(encoder|buffer|extruder|toolhead|espooler) /.test(n) || /:filament_proportional$/.test(n) ||
    /^(extruder\d*|heater_generic .+|gcode_macro .+)$/.test(n)).map(n=>[n,n.startsWith('gcode_macro ') ? ['blade_pos','cutter_before_extruder','cooling_tube_position','cooling_tube_length']
      : /^(extruder\d*|heater_generic .+)$/.test(n) ? ['target'] : null]));
  wanted.mmu=['unit','gate','tool','filament_pos','bowden_progress','homing','selector','sync_drive','sensors','config_changes',
    'gate_color_rgb','gate_status','gate_initial_weight','gate_remaining_weight','espooler','action',
    'filament_cut_fragment','filament_remaining_color'];
  wanted.save_variables=['variables'];
  const initial=await rpc('printer.objects.subscribe',{objects:wanted});
  liveState.accept({configfile:startup.configfile},initial.eventtime-1);
  if (viewMode==='live' && generation===subscriptionGeneration) acceptLive(initial.status,initial.eventtime);
}
function acceptLive(delta,time) {
  const next=liveState.accept(delta,time);
  if (!next) { if (!liveTarget) $('mode').textContent='Live view · Waiting for MMU configuration'; return; }
  const previous=liveTarget;
  if (setup?.connection_kind==='live_machine' || liveState.objects.configfile) toolheadPanel.show(toolheadObjects(liveState.objects));
  if (previous?.homing && !next.homing) {
    const name=previous.homing.endstops.find(n=>next.sensors[n]?.state===previous.homing.triggered);
    if (name) sensorPulse={sensor:name,reverse:!previous.homing.triggered,start:animationNow()};
  }
  liveFrom=displayed;
  const sameGate=liveFrom?.unit===next.unit && liveFrom?.gate===next.gate && next.gate>=0;
  const distance=sameGate ? next.tips[next.gate]-liveFrom.tips[next.gate] : 0;
  next.motors={gear:(liveFrom?.motors.gear || 0)+(next.mmu_gripped ? distance : 0),
    extruder:(liveFrom?.motors.extruder || 0)+(next.synced || next.filament_pos>=6 ? distance : 0)};
  liveTarget=next; liveStart=animationNow();
  if (isSimulator() && previous?.unit!==next.unit) {
    const unit=next.units.indexOf(next.unit);
    if (unit>=0 && $('unit').value!==String(unit)) {
      $('unit').value=String(unit); refresh().catch(error=>message(error.message,true));
    }
  }
  live=true; paused=false; playing=null; session='subscription';
  draw();
}
// The near-live stream shows the printer's event stream; sensor readings are overlaid while it is idle, and the
// recording mode (a status variable) drives the Record control.
async function startStream() {
  const generation=viewGeneration;
  const mmu=(await rpc('printer.objects.subscribe',{objects:{mmu:['sensors','telemetry_mode']}})).status.mmu;
  if (generation!==viewGeneration || viewMode!=='playback' || offline) return;
  statusSensors=mmu?.sensors || null; statusRecordMode=mmu?.telemetry_mode ?? null;
  renderRecordControl();
  await telemetryStream.start();
}
async function changeView(mode) {
  const generation=++viewGeneration;
  await telemetryStream.stop();
  subscriptionGeneration++;
  viewMode=mode; offline=false; playing=null; displayed=null; sensorPulse=null;
  events=[]; rawEvents=[]; sourceRecords=[]; sourceReplay=new TelemetryReplay();
  liveTarget=null; liveFrom=null; liveState=new LiveState(); statusSensors=null; statusRecordMode=null;
  live=true; paused=false; replayingHistory=false;
  $('scene').replaceChildren();
  $('mode').textContent=mode==='live' ? 'Live view · Connecting…' : isSimulator() ? 'Telemetry playback · Waiting for simulator stream' : 'Telemetry playback · Waiting for printer stream';
  sourceMode(); showConnection(); renderTrace();
  if (socket?.readyState!==1 || generation!==viewGeneration) return;
  // A reconnect while a recording was loaded left the connection unidentified.
  if (!setup) return initializeConnection();
  if (mode==='live') await subscribeLive();
  else await startStream();
}
$('view-mode').onchange=()=>{
  modeChosen=true;
  changeView($('view-mode').value).catch(e=>message(e.message,true));
};
// A printer has no simulator RPC: the editor lists come from configfile, mmu.config_changes and the
// macro objects, and edits are sent as G-code like the command console does.
let editorStatic=null;
function reloadEditor() {
  editorStatic=null;
  return (isSimulator() ? refresh() : refreshLiveEditor()).catch(error=>message(error.message,true));
}
async function editorState() {   // configfile once per connection, config_changes every time
  editorStatic ||= rpc('printer.objects.query',{objects:{configfile:['settings','config']}})
    .then(({status})=>({configfile:status.configfile,catalog:macroCatalog(status.configfile.config)}))
    .catch(error=>{ editorStatic=null; throw error; });
  const {configfile,catalog}=await editorStatic;
  const {status:{mmu}}=await rpc('printer.objects.query',{objects:{mmu:['config_changes']}});
  return {configfile,catalog,mmu};
}
async function refreshToolheads() { toolheadPanel.show(toolheadObjects(await editorState())); }
async function refreshLiveEditor() {
  const state=await editorState(), {configfile,catalog}=state, unit=Number($('unit').value || 0);
  const names=[].concat(configfile.settings.mmu_machine?.units || []);
  const macroObjects=Object.fromEntries([...new Set(catalog.map(([macro])=>`gcode_macro ${macro}`))].map(name=>[name,null]));
  const {status:macros}=await rpc('printer.objects.query',{objects:macroObjects});
  setup={...setup,units:names,parameters:editorParameters(state,unit),macro_parameters:editorMacroParameters(catalog,macros)};
  options($('unit'),names.map((name,i)=>[i,name]));
  $('unit').value=String(unit);
  filter();
  toolheadPanel.show(toolheadObjects(state));
}
async function refresh(probed) {
  if (offline) return;
  if (setup?.connection_kind==='live_machine') return refreshLiveEditor();
  setup = probed || await sim.setup(Number($("unit").value || 0));
  if (offline) return;
  sourceMode();
  showConnection();
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
  await refreshToolheads().catch(error=>message(error.message,true));
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
    const p = selectedParameter();
    if (!p) return;
    const script = p.macro ? macroCommand(p.macro, p.name, $("value").value, p.value)
      : parameterCommand(Number($("unit").value), p.name, $("value").value);
    await rpc("printer.gcode.script", { script });
    await refresh();
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
  if (!script) return;
  if (viewMode==='playback') followLive(); // A command typed here is watched; commands from elsewhere never move a paused view
  scrollConsoleToEnd();
  // Not under the busy lock: a second command (a pause, an emergency stop) must go through while the first runs.
  rpc("printer.gcode.script", { script }).catch((e) => { if (!consoleEndsWith(e.message)) consoleLine(e.message, 'error'); });
  if (socket.readyState === WebSocket.OPEN) {
    commandHistory.add(script);
    $("command").value = "";
  }
};
$("restart").onclick = () =>
  action(async () => {
    message("Restarting simulator…");
    await sim.restart({ profile: $("profiles").value });
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
    await sim.restart({ files });
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
let displayed = null, playing = null, played = 0;
const scene=createScene($('scene'), {now:animationNow, requestFrame:draw, tooltipOpen:()=>!$('viewer-tooltip').hidden});
// Gate lanes and spool columns share the same spacing.
let paused = false, pauseAfterStep = false, replayingHistory = false, sensorPulse = null, nextStepAt = 0;
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
  pauseAfterStep = false; nextStepAt = 0;
  scene.resetSpoolArrows();
  cursor = Math.max(0, Math.min(index, events.length - 1));
  live = false; paused = true; replayingHistory = true; playing = null; sensorPulse = null;
  draw(); followTrace();
}
// UI, telemetry, and animation changes share the existing frame loop.
let sceneDirty=false;
function draw() { sceneDirty=true; }
function idleOverlay() {
  const last=events.at(-1);
  if (!statusSensors || !last || !overlayAllowed({viewMode,offline,live,paused,playing,caughtUp:played>=last.seq,
      pendingMoves:sourceReplay.pending.size})) return null;
  const key=`${sourceReplay.session}:${sourceReplay.sequence}:${JSON.stringify(statusSensors)}`;
  if (overlayCache.key!==key) overlayCache={key,state:overlaySnapshot(sourceReplay,statusSensors)};
  return overlayCache.state;
}
function sceneSettings() {
  return {spoolSpacing:Number($('spool-spacing').value), spoolAngle:Number($('spool-angle').value),
    spoolRealism:$('spool-realism').checked, heatingPreview:$('heating-preview').checked,
    previewGateCount:Number($('preview-gate-count').value), nozzleDiameter:Number($("nozzle-diameter").value)};
}
// Everything one frame draws from, derived once: the scene, the controls and the readout all take it.
function currentView() {
  cursor = Math.max(0, Math.min(cursor, events.length - 1));
  const running = viewMode==='playback' && live && !paused && (playing || played < events.at(-1).seq);
  const index = currentIndex();
  const e = viewMode==='live' ? {state:displayed,seq:0,kind:'state'} : live ? (playing?.event || events.find(e=>e.seq===played) || events.at(-1)) : events[cursor];
  const s = idleOverlay() || (live && displayed ? displayed : (e.settled_state || e.state)),
    gate = s.gate >= 0 ? s.gate : 0;
  const tip = s.visual_tips?.[gate] ?? s.tips[gate] ?? -1000;
  const eventIndex = events.indexOf(e), eventsBehind = events.length - 1 - eventIndex;
  if (live && !paused && !playing && eventsBehind === 0) replayingHistory = false;
  const modeLabel = paused || !live ? 'Paused' : playing || eventsBehind>0 ? 'Playing' : offline ? 'End of recording' : 'Following stream';
  // Bookkeeping events must not clear the last animated movement's annotation.
  const annotatedEvent = viewMode==='live' ? e : annotatedStep(events, e, !live || paused);
  const moveReason = annotatedEvent.label || annotatedEvent.telemetry?.start?.label;
  const connected = socket?.readyState===1;
  return {viewMode, offline, live, paused, playing, played, events, sensorPulse, event:e, state:s,
    homing:liveTarget?.homing, simulator:isSimulator(), running, index, count:events.length, pauseAfterStep,
    previousToolchange:toolchangeIndex(events, index, -1) >= 0, nextToolchange:toolchangeIndex(events, index, 1) >= 0,
    modeText: viewMode==='live' ? `Live view · ${connected ? s.action || s.filament_pos_name : 'Disconnected'}`
      : `Telemetry playback · ${modeLabel} · event ${annotatedEvent.seq}` + (moveReason ? ` · ${moveReason}` : ''),
    positionText: `Gate ${gate} · ${s.filament_pos_name || "Unknown state"} · tip ${tip.toFixed(2)} mm`,
    timelineValue: live ? eventIndex : cursor,
    atLatest: viewMode==='live' || (live && !paused && !playing && played >= events.at(-1)?.seq)};
}
function renderScene() {
  if (viewMode==='live' ? !displayed : !events.length) return;
  const view=currentView();
  renderControls(view);
  const {warnings, unitSensors}=scene.render(view, sceneSettings());
  renderMacros(view.state, !offline && isSimulator());
  renderWarning(warnings, viewMode==='playback' ? selectorNotice(events, view.event, Boolean(playing), live && !paused) : null);
  renderSensors(unitSensors, {offline, atLatest:view.atLatest, editable:!busy && isSimulator(),
    onToggle:(name, enable)=>action(async () => {
      await sim.sensor(name, enable);
      message(`Sensor ${name} ${enable ? 'enabled' : 'disabled'}.`);
    })});
  renderDevices(view.state);
}
function renderTrace() {
  const markers=timelineMarkers(events);
  $('toolchange-ticks').closest('.timeline-control').classList.toggle('has-labels',markers.some(marker=>marker.label));
  $('toolchange-ticks').replaceChildren(...markers.map(({index,label}) => {
    const tick = document.createElement('span');
    tick.style.left = `${events.length > 1 ? 100*index/(events.length-1) : 0}%`;
    if (label) {
      const text=document.createElement('small');
      text.textContent=label;
      tick.append(text);
    }
    return tick;
  }));
  const view = $("trace"),
    scroll = view.scrollTop;
  const expanded = new Set([...view.querySelectorAll('details[open]')].map(row=>row.dataset.seq));
  const showDetails=$('show-internal').checked;
  const records=new Map(showDetails ? sourceRecords.map(record=>[record.sequence,record]) : []);
  view.replaceChildren(
    ...($('show-internal').checked ? orderedSteps(rawEvents) : events).map(e => {
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
        details.textContent = e.telemetry?.changes ? changeLines(e.telemetry.changes) : JSON.stringify(e.telemetry || e.state, null, 2);
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
            outcome:records.get(e.telemetry?.outcome?.sequence) || e.telemetry?.outcome
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
  if (viewMode==='live' || (!paused && live) || event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey
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
  pauseAfterStep = false; nextStepAt = 0;
  if (!events.length) return;
  if (!live) {
    const event = events[cursor];
    played = event.seq; displayed = event.settled_state || event.state;
    playing = null;
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
  pauseAfterStep = false; nextStepAt = 0;
  replayingHistory = false;
  live = true; paused = offline; playing = null; sensorPulse = null;
  played = events.at(-1)?.seq || 0;
  displayed = events.at(-1)?.settled_state || events.at(-1)?.state;
  draw(); followTrace();
}
$("live").onclick = followLive;
$("timeline").oninput = () => seek(Number($("timeline").value));
$("compress-bowden").onchange = () => renderTrace();
initSpoolPicker(draw);
$('preview-gate-count').oninput=draw;
$('heating-preview').onchange=draw;
$("slow-animation").onchange = () => {
  animationNow();
  animationRate=$("slow-animation").checked ? .1 : 1;
};
$("show-dimensions").onchange = () => {
  $("scene").classList.toggle('hide-dimensions', !$("show-dimensions").checked);
};
$("nozzle-diameter").onchange = () => draw();
function downloadTelemetry() {
  const records = sourceRecords;
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
    if (!offline && socket?.readyState===1) rpc('printer.objects.subscribe',{objects:{}}).catch(()=>{}); // Not awaited: a long G-code would hold the load up
    await telemetryStream.stop();
    modeChosen=true; viewMode='playback'; offline=true; subscriptionGeneration++; liveTarget=null; liveFrom=null;
    pending.forEach(p=>p.reject(Error('Switched to offline recording'))); pending.clear();
    sourceReplay=replay; sourceRecords=records;
    rawEvents=replay.events; events=visibleSteps(rawEvents); session=replay.session;
    live=false; paused=true; cursor=0; playing=null; sensorPulse=null; pauseAfterStep=false; displayed=null;
    $('recording-name').textContent=file.name;
    setConnection('Offline recording · not connected', 'offline');
    sourceMode(); draw(); renderTrace();
    if (replay.checkpoint) message('Loaded checkpoint: earlier motion and inferred geometry are unavailable.');
  } catch(error) { message(error.message,true); }
  finally { $('telemetry-file').value=''; }
};

let parameterGate = null;
function updateSource(records, reset=false) {
  const selectedSeq=events[currentIndex()]?.seq;
  const newSession=reset || records.some(r=>r.event === 'session');
  if (newSession) { sourceReplay=new TelemetryReplay(); sourceRecords=[]; }
  sourceReplay.accept(records);
  sourceRecords.push(...records);
  if (!records.length) return;
  const replay=sourceReplay;
  rawEvents=replay.events; events=visibleSteps(rawEvents);
  if (newSession) {
    if (session !== replay.session) $('mini-console').replaceChildren();
    session=replay.session; live=true; paused=false; replayingHistory=false; pauseAfterStep=false;
    cursor=0; playing=null; sensorPulse=null;
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
const telemetryStream=new TelemetryStream(rpc, async data=>{
  if (offline || viewMode!=='playback') return false;
  updateSource(data.records,data.reset);
  const latest=rawEvents.at(-1)?.state;
  const selectedGate=latest ? `${session}:${latest.unit}:${latest.gate}` : null;
  if (latest && selectedGate!==parameterGate && isSimulator()) {
    parameterGate=selectedGate;
    const unit=latest.units?.indexOf(latest.unit) ?? -1;
    if (unit>=0 && $('unit').value!==String(unit)) {
      $('unit').value=String(unit); await refresh().catch(error=>message(error.message,true));
    }
  }
  return sourceRecords.length>7000;
}, error=>{
  setConnection('Telemetry stream unavailable', 'warning');
  message(`${error.message}. On a printer, enable [mmu_telemetry] in moonraker.conf. You can also load a recording.`,true);
});
let liveIndicator=null;
function updateLiveIndicator() {
  const connected=!offline && socket?.readyState===1;
  const active=Boolean(connected && (viewMode==='live' ? liveTarget
    : telemetryStream.active && telemetryStream.ready && live && !paused
      && (playing?.event.seq ?? played) >= (events.at(-1)?.seq ?? 0)));
  if (active===liveIndicator) return;
  liveIndicator=active;
  $('viewer-live').classList.toggle('active',active);
  $('viewer-live').setAttribute('aria-label',active ? 'Live' : 'Not live');
}
function drawFrame() {
  updateLiveIndicator();
  const now=animationNow();
  const spoolRunning=Boolean(playing) || (live && !paused);
  const spoolMoving=scene.advanceSpools(now, {espoolers:displayed?.espoolers, running:spoolRunning, session});
  if (viewMode==='live' && liveTarget) {
    const fraction=Math.min(1,(now-liveStart)/350);
    displayed=liveFrame(liveFrom,liveTarget,fraction);
    if (fraction<1 || sceneDirty || liveTarget.homing) draw();
  }
  if (viewMode==='playback' && live && !paused && events.length) {
    if (!playing && now>=nextStepAt) {
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
        nextStepAt=now+homingHold(playing.event);
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
  scene.returnRackWhenIdle();
  if (sceneDirty) {
    sceneDirty=false;
    renderScene();
  } else if (spoolMoving) {
    scene.updateSpoolMotion();
  }
}
// One bad frame must not end the animation loop: report it once and carry on.
let lastFrameError = '';
function animate() {
  try {
    drawFrame();
  } catch (error) {
    if (error.message !== lastFrameError) { lastFrameError = error.message; message(`Drawing failed: ${error.message}`, true); }
    console.error(error);
  } finally {
    requestAnimationFrame(animate);
  }
}

const toolheadPanel=createToolheadPanel({rpc, action, refresh, changed:()=>sourceMode()});

$('printer-url').value=connectionUrl;
$('connect-printer').onclick=()=>{
  try {
    const value=$('printer-url').value.trim();
    const endpoint=new URL(value.includes('://') ? value : 'http://'+value);
    if (!['http:','https:','ws:','wss:'].includes(endpoint.protocol)) throw Error('Use an HTTP or WebSocket printer URL');
    connectionUrl=endpoint.href;
    clearTimeout(reconnectTimer); connectionGeneration++; subscriptionGeneration++;
    telemetryStream.stop(false);
    socket?.close();
    pending.forEach(p=>p.reject(Error('Connection changed'))); pending.clear();
    modeChosen=false; offline=false; viewMode='live'; liveTarget=null; displayed=null; playing=null; sensorPulse=null;
    events=[]; rawEvents=[]; sourceRecords=[]; sourceReplay=new TelemetryReplay();
    scene.clear(); $('mode').textContent='Live view · Connecting…';
    connect();
  } catch(error) { message(error.message,true); }
};
try { connect(); } catch (error) { message(error.message, true); }
requestAnimationFrame(animate);
