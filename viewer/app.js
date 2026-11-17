import {TelemetryStream, recordControl} from './telemetry-stream.js';
import {LiveState, liveFrame} from './live-state.js';
import {liveMachine} from './live-config.js';
import {simulatorApi, detectConnection} from './simulator.js';
import {overlayAllowed, overlaySnapshot} from './overlay.js';
import {parameterCommand, macroCommand, editorParameters, macroCatalog, editorMacroParameters, toolheadObjects} from './editor.js';
import {TelemetryReplay, parseTelemetry} from './telemetry.js';
import {timelineMarkers} from './playback.js';
import {selectorNotice, orderedSteps, homingHold, nozzleArrivalTime, playbackFrame, isInternalUpdate, changeLines, visibleSteps, toolchangeIndex, annotatedStep, interpolate, duration, CommandHistory, traceText, homingPulse, configurationLabel} from './playback.js';
import {$, options, syncChildren} from './util.js';
import {consoleLine, consoleEndsWith, scrollConsoleToEnd} from './console-log.js';
import {createToolheadPanel} from './toolhead-panel.js';
import {initSpoolPicker} from './spool-picker.js';
import {createScene} from './scene.js';
import {renderControls} from './controls.js';
import {renderMacros, renderSensors, renderDevices} from './readout.js';
import './chrome.js';
const sim = simulatorApi(rpc);
// Everything the app knows: one object, so the views can be handed it (or watch it) instead of sharing module variables
const store = {
  // Connection
  socket: null, reconnectTimer: null, connectionGeneration: 0, serial: 0, pending: new Map(), busy: false,
  connectionUrl: location.protocol === 'file:' ? 'http://localhost:7125' : location.origin,
  setup: null,                                  // What the simulator offers, or the machine built from a printer's status
  // Which source is shown
  viewMode: 'live', modeChosen: false, viewGeneration: 0, offline: false,
  // Live status subscription
  liveState: new LiveState(), liveTarget: null, liveFrom: null, liveStart: 0, subscriptionGeneration: 0,
  statusSensors: null, statusRecordMode: null, overlayCache: {key: '', state: null},
  // Telemetry: the records, their replay and the steps made from them
  sourceRecords: [], sourceReplay: new TelemetryReplay(), rawEvents: [], events: [], session: null, parameterGate: null,
  // Playback position and animation
  live: true, cursor: 0, displayed: null, playing: null, played: 0, paused: false, pauseAfterStep: false,
  replayingHistory: false, sensorPulse: null, nextStepAt: 0, sceneDirty: false,
};
/* Copyright (C) 2022-2026 Paul Morgan. GPLv3. */
// The Connection panel's status: a dot coloured by `state` (connected, pending, warning, offline or disconnected) and a label.
function setConnection(text, state) {
  $('connection').textContent = text;
  $('connection').dataset.state = state;
}
function showConnection() {
  const kind = store.setup?.connection_kind;
  if (store.socket?.readyState !== 1) return setConnection('Disconnected · retrying', 'disconnected');
  setConnection(kind === 'simulator' ? 'Connected to the simulator' : kind === 'live_machine' ? 'Connected to printer' : 'Connected · unknown source', 'connected');
}
function renderRecordControl() {
  const {visible,value}=recordControl({viewMode:store.viewMode,offline:store.offline,simulator:isSimulator(),connected:store.socket?.readyState===1,mode:store.statusRecordMode});
  $('record-row').hidden=!visible;
  if (visible) $('record-mode').value=value;
}
$('record-mode').onchange=()=>rpc('printer.gcode.script',{script:`MMU_TELEMETRY MODE=${$('record-mode').value}`})
  .catch(error=>{ message(error.message,true); renderRecordControl(); });
function sourceMode() {
  renderRecordControl();
  toolheadPanel.element.hidden=store.offline || store.socket?.readyState !== 1 || toolheadPanel.element.dataset.available !== 'true';
  $('recording-option').hidden=!store.offline;
  $('view-mode').value=store.offline ? 'recording' : store.viewMode;
  $('sidebar-playback').hidden=store.viewMode==='live';
  $('trace').closest('.card').hidden=store.viewMode==='live';
  $('recording-name').hidden = !store.offline;
  $('connect-printer').textContent = store.offline ? 'Reconnect' : 'Connect';
  $('save-motion').disabled = store.viewMode==='live' || store.offline;
  $('simulator-controls').hidden = !isSimulator() || store.offline;
  $('runtime-parameters').hidden = store.offline;
  document.querySelectorAll('#simulator-controls input, #simulator-controls select, #simulator-controls button, #runtime-parameters input, #runtime-parameters select, #runtime-parameters button, #command-form input, #command-form button, [data-simulator-config] input, [data-simulator-config] select, [data-simulator-config] button')
    .forEach(node=>node.disabled=store.offline);
  $('live').dataset.tooltip = store.offline ? 'End of recording' : 'End, follow stream';
  $('live').setAttribute('aria-label', $('live').dataset.tooltip);
  renderWarning();
}

function renderWarning(warnings=[], notice=null) {
  const simulatorLive=store.viewMode==='live' && !store.offline && store.socket?.readyState===1 && isSimulator();
  const text=simulatorLive
    ? 'Simulator subscriptions may skip movement updates. Select Telemetry stream for complete movement animation.'
    : store.viewMode!=='playback' ? ''
    : [notice, warnings.length ? 'Displayed tip adjusted to sensor readings: '+warnings.join('; ') : ''].filter(Boolean).join('\n');
  // A notice alone is informational (blue, "i"); anything that is a warning keeps the warning look
  const informational=Boolean(notice) && !warnings.length && store.viewMode==='playback';
  $('filament-warning').dataset.kind=informational ? 'notice' : 'warning';
  $('filament-warning').querySelector('.warning-bang').textContent=informational ? 'i' : '!';
  $('filament-warning').hidden=!text;
  $('filament-warning-message').textContent=text;
}

const RPC_TIMEOUT_MS = 15000;
function message(text, error = false) {
  $("message").textContent = text;
  $("message").classList.toggle("error", error);
}
function isSimulator() { return store.setup?.connection_kind === 'simulator'; }
function rpc(method, params = {}) {
  return new Promise((resolve, reject) => {
    if (store.offline) return reject(Error("Select a live source before changing the printer"));
    if (store.socket?.readyState !== 1) return reject(Error("Console disconnected"));
    const id = ++store.serial;
    // A lost reply must not hold the viewer forever; only G-code is allowed to take as long as it takes.
    const timer = method === 'printer.gcode.script' ? null
      : setTimeout(() => { if (store.pending.delete(id)) reject(Error(`${method} timed out`)); }, RPC_TIMEOUT_MS);
    const done = (settle) => (value) => { clearTimeout(timer); settle(value); };
    store.pending.set(id, { resolve: done(resolve), reject: done(reject) });
    store.socket.send(JSON.stringify({ jsonrpc: "2.0", id, method, params }));
  });
}
// `report` shows a failure in the status line too; the command box leaves that to the console, which already has it.
async function action(fn, report = true) {
  if (store.busy) return;
  store.busy = true;
  const disabled = [...document.querySelectorAll("aside button, #send")].filter((b) => !b.disabled);
  disabled.forEach((b) => (b.disabled = true));
  try {
    await fn();
  } catch (e) {
    if (report) message(e.message, true);
    if (!consoleEndsWith(e.message)) consoleLine(e.message, 'error');
  } finally {
    store.busy = false;
    disabled.forEach((b) => (b.disabled = false));
    if (store.offline) sourceMode();
  }
}
function connect() {
  const generation=++store.connectionGeneration;
  const endpoint=new URL(store.connectionUrl);
  endpoint.protocol=endpoint.protocol==='https:' || endpoint.protocol==='wss:' ? 'wss:' : 'ws:';
  endpoint.pathname='/websocket'; endpoint.search=''; endpoint.hash='';
  store.socket = new WebSocket(endpoint);
  store.socket.onopen = () => {
    if (generation!==store.connectionGeneration) return;
    store.setup=null;
    store.liveState=new LiveState(); store.statusSensors=null; store.statusRecordMode=null;
    sourceMode();
    if (store.offline) return;
    setConnection("Connected · identifying source…", "pending");
    initializeConnection().catch((e) => message(e.message, true));
  };
  store.socket.onmessage = (e) => {
    if (generation!==store.connectionGeneration) return;
    if (store.offline) return;
    const data = JSON.parse(e.data);
    if (store.pending.has(data.id)) {
      const p = store.pending.get(data.id);
      store.pending.delete(data.id);
      data.error ? p.reject(Error(data.error.message)) : p.resolve(data.result);
    } else if (data.method === 'notify_mmu_telemetry') {
      telemetryStream.receive(data.params[0]);
    } else if (data.method === 'notify_status_update') {
      if (store.viewMode==='live') acceptLive(data.params[0],data.params[1]);
      else {
        const mmu=data.params[0].mmu;
        if (mmu?.sensors) { store.statusSensors=mmu.sensors; draw(); }
        if (mmu && 'telemetry_mode' in mmu) { store.statusRecordMode=mmu.telemetry_mode; renderRecordControl(); }
      }
    } else if (['notify_klippy_ready','notify_klippy_shutdown','notify_klippy_disconnected'].includes(data.method)) {
      if (store.offline) return;
      if (data.method==='notify_klippy_ready') reloadEditor();
      if (store.viewMode!=='live') {
        telemetryStream.stop(false);
        if (data.method==='notify_klippy_ready') startStream().catch(e=>message(e.message,true));
        return;
      }
      store.liveState=new LiveState(); store.statusSensors=null; store.statusRecordMode=null; store.liveTarget=null; store.displayed=null; store.playing=null; store.sensorPulse=null;
      if (data.method==='notify_klippy_ready') subscribeLive().catch(e=>message(e.message,true));
      else { $('mode').textContent='Live view · Printer unavailable'; $('scene').replaceChildren(); }
    } else if (data.method === "notify_gcode_command") {
      consoleLine(data.params[0], "command");
    } else if (data.method === "notify_gcode_response") {
      consoleLine(data.params[0]);
    }
  };
  store.socket.onclose = () => {
    if (generation!==store.connectionGeneration) return;
    store.subscriptionGeneration++;
    telemetryStream.stop(false);
    store.statusSensors=null; store.statusRecordMode=null;
    if (store.liveTarget) { store.liveTarget.homing=null; store.liveTarget.espoolers={}; }
    if (store.viewMode==='live' && store.displayed) store.displayed.espoolers={};
    draw();
    sourceMode();
    if (!store.offline) setConnection("Disconnected · retrying", "disconnected");
    store.pending.forEach((p) => p.reject(Error("Console disconnected")));
    store.pending.clear();
    store.reconnectTimer=setTimeout(connect, 1500);
  };
}
async function initializeConnection() {
  const generation=store.connectionGeneration;
  editorStatic=null;
  const probe=await detectConnection(rpc,Number($('unit').value || 0));
  if (generation!==store.connectionGeneration || store.socket?.readyState!==1) return;
  if (probe.connection_kind==='live_machine') {
    store.setup=probe;
    if ($('command').value==='MMU_LOAD') $('command').value=''; // The simulator's default must not be one Enter away on a printer
    setConnection('Connected to printer', 'connected'); sourceMode();
    await refreshLiveEditor().catch(error=>message(error.message,true));
  } else await refresh(probe);
  if (generation!==store.connectionGeneration || store.offline) return;
  if (!store.modeChosen && isSimulator()) await changeView('playback');
  else if (store.viewMode==='live') await subscribeLive();
  else await startStream();
}
async function subscribeLive() {
  const generation=++store.subscriptionGeneration;
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
  store.liveState.accept({configfile:startup.configfile},initial.eventtime-1);
  if (store.viewMode==='live' && generation===store.subscriptionGeneration) acceptLive(initial.status,initial.eventtime);
}
function acceptLive(delta,time) {
  const next=store.liveState.accept(delta,time);
  if (!next) { if (!store.liveTarget) $('mode').textContent='Live view · Waiting for MMU configuration'; return; }
  const previous=store.liveTarget;
  if (store.setup?.connection_kind==='live_machine' || store.liveState.objects.configfile) toolheadPanel.show(toolheadObjects(store.liveState.objects));
  if (previous?.homing && !next.homing) {
    const name=previous.homing.endstops.find(n=>next.sensors[n]?.state===previous.homing.triggered);
    if (name) store.sensorPulse={sensor:name,reverse:!previous.homing.triggered,start:animationNow()};
  }
  store.liveFrom=store.displayed;
  const sameGate=store.liveFrom?.unit===next.unit && store.liveFrom?.gate===next.gate && next.gate>=0;
  const distance=sameGate ? next.tips[next.gate]-store.liveFrom.tips[next.gate] : 0;
  next.motors={gear:(store.liveFrom?.motors.gear || 0)+(next.mmu_gripped ? distance : 0),
    extruder:(store.liveFrom?.motors.extruder || 0)+(next.synced || next.filament_pos>=6 ? distance : 0)};
  store.liveTarget=next; store.liveStart=animationNow();
  if (isSimulator() && previous?.unit!==next.unit) {
    const unit=next.units.indexOf(next.unit);
    if (unit>=0 && $('unit').value!==String(unit)) {
      $('unit').value=String(unit); refresh().catch(error=>message(error.message,true));
    }
  }
  store.live=true; store.paused=false; store.playing=null; store.session='subscription';
  draw();
}
// The near-live stream shows the printer's event stream; sensor readings are overlaid while it is idle, and the
// recording mode (a status variable) drives the Record control.
async function startStream() {
  const generation=store.viewGeneration;
  const mmu=(await rpc('printer.objects.subscribe',{objects:{mmu:['sensors','telemetry_mode']}})).status.mmu;
  if (generation!==store.viewGeneration || store.viewMode!=='playback' || store.offline) return;
  store.statusSensors=mmu?.sensors || null; store.statusRecordMode=mmu?.telemetry_mode ?? null;
  renderRecordControl();
  await telemetryStream.start();
}
async function changeView(mode) {
  const generation=++store.viewGeneration;
  await telemetryStream.stop();
  store.subscriptionGeneration++;
  store.viewMode=mode; store.offline=false; store.playing=null; store.displayed=null; store.sensorPulse=null;
  store.events=[]; store.rawEvents=[]; store.sourceRecords=[]; store.sourceReplay=new TelemetryReplay();
  store.liveTarget=null; store.liveFrom=null; store.liveState=new LiveState(); store.statusSensors=null; store.statusRecordMode=null;
  store.live=true; store.paused=false; store.replayingHistory=false;
  $('scene').replaceChildren();
  $('mode').textContent=mode==='live' ? 'Live view · Connecting…' : isSimulator() ? 'Telemetry playback · Waiting for simulator stream' : 'Telemetry playback · Waiting for printer stream';
  sourceMode(); showConnection(); renderTrace();
  if (store.socket?.readyState!==1 || generation!==store.viewGeneration) return;
  // A reconnect while a recording was loaded left the connection unidentified.
  if (!store.setup) return initializeConnection();
  if (mode==='live') await subscribeLive();
  else await startStream();
}
$('view-mode').onchange=()=>{
  store.modeChosen=true;
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
  store.setup={...store.setup,units:names,parameters:editorParameters(state,unit),macro_parameters:editorMacroParameters(catalog,macros)};
  options($('unit'),names.map((name,i)=>[i,name]));
  $('unit').value=String(unit);
  filter();
  toolheadPanel.show(toolheadObjects(state));
}
async function refresh(probed) {
  if (store.offline) return;
  if (store.setup?.connection_kind==='live_machine') return refreshLiveEditor();
  store.setup = probed || await sim.setup(Number($("unit").value || 0));
  if (store.offline) return;
  sourceMode();
  showConnection();
  const unit = $("unit").value;
  options(
    $("unit"),
    store.setup.units.map((name, i) => [i, name]),
  );
  $("unit").value = unit || "0";
  options(
    $("profiles"),
    store.setup.profiles.map((p) => [p.name, p.name + " · " + p.description]),
  );
  if (store.setup.profiles.some((p) => p.name === store.setup.profile))
    $("profiles").value = store.setup.profile;
  $("profile").textContent = "Running: " + store.setup.profile;
  filter();
  await refreshToolheads().catch(error=>message(error.message,true));
}
function filter() {
  const previous = $("parameter").value,
    query = $("filter").value.trim().toLowerCase();
  const groups = new Map();
  for (const p of [...store.setup.parameters, ...(store.setup.macro_parameters || [])]) {
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
  return [...(store.setup?.parameters || []), ...(store.setup?.macro_parameters || [])]
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
  if (store.setup) filter();
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
  if (store.viewMode==='playback') followLive(); // A command typed here is watched; commands from elsewhere never move a paused view
  scrollConsoleToEnd();
  // Not under the busy lock: a second command (a pause, an emergency stop) must go through while the first runs.
  rpc("printer.gcode.script", { script }).catch((e) => { if (!consoleEndsWith(e.message)) consoleLine(e.message, 'error'); });
  if (store.socket.readyState === WebSocket.OPEN) {
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
const scene=createScene($('scene'), {now:animationNow, requestFrame:draw, tooltipOpen:()=>!$('viewer-tooltip').hidden});
// Gate lanes and spool columns share the same spacing.
function currentIndex() {
  return store.live ? Math.max(0, store.events.findIndex(e => e.seq === (store.playing?.event.seq || store.played))) : store.cursor;
}
function followTrace() {
  const view = $("trace"), index = currentIndex();
  const sequence = store.events[index]?.seq;
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
  store.pauseAfterStep = false; store.nextStepAt = 0;
  scene.resetSpoolArrows();
  store.cursor = Math.max(0, Math.min(index, store.events.length - 1));
  store.live = false; store.paused = true; store.replayingHistory = true; store.playing = null; store.sensorPulse = null;
  draw(); followTrace();
}
// UI, telemetry, and animation changes share the existing frame loop.
function draw() { store.sceneDirty=true; }
function idleOverlay() {
  const last=store.events.at(-1);
  if (!store.statusSensors || !last || !overlayAllowed({viewMode:store.viewMode,offline:store.offline,live:store.live,paused:store.paused,playing:store.playing,caughtUp:store.played>=last.seq,
      pendingMoves:store.sourceReplay.pending.size})) return null;
  const key=`${store.sourceReplay.session}:${store.sourceReplay.sequence}:${JSON.stringify(store.statusSensors)}`;
  if (store.overlayCache.key!==key) store.overlayCache={key,state:overlaySnapshot(store.sourceReplay,store.statusSensors)};
  return store.overlayCache.state;
}
function sceneSettings() {
  return {spoolSpacing:Number($('spool-spacing').value), spoolAngle:Number($('spool-angle').value),
    spoolRealism:$('spool-realism').checked, heatingPreview:$('heating-preview').checked,
    previewGateCount:Number($('preview-gate-count').value), nozzleDiameter:Number($("nozzle-diameter").value)};
}
// Everything one frame draws from, derived once: the scene, the controls and the readout all take it.
function currentView() {
  store.cursor = Math.max(0, Math.min(store.cursor, store.events.length - 1));
  const running = store.viewMode==='playback' && store.live && !store.paused && (store.playing || store.played < store.events.at(-1).seq);
  const index = currentIndex();
  const e = store.viewMode==='live' ? {state:store.displayed,seq:0,kind:'state'} : store.live ? (store.playing?.event || store.events.find(e=>e.seq===store.played) || store.events.at(-1)) : store.events[store.cursor];
  const s = idleOverlay() || (store.live && store.displayed ? store.displayed : (e.settled_state || e.state)),
    gate = s.gate >= 0 ? s.gate : 0;
  const tip = s.visual_tips?.[gate] ?? s.tips[gate] ?? -1000;
  const eventIndex = store.events.indexOf(e), eventsBehind = store.events.length - 1 - eventIndex;
  if (store.live && !store.paused && !store.playing && eventsBehind === 0) store.replayingHistory = false;
  const modeLabel = store.paused || !store.live ? 'Paused' : store.playing || eventsBehind>0 ? 'Playing' : store.offline ? 'End of recording' : 'Following stream';
  // Bookkeeping events must not clear the last animated movement's annotation.
  const annotatedEvent = store.viewMode==='live' ? e : annotatedStep(store.events, e, !store.live || store.paused);
  const moveReason = annotatedEvent.label || annotatedEvent.telemetry?.start?.label;
  const connected = store.socket?.readyState===1;
  return {viewMode:store.viewMode, offline:store.offline, live:store.live, paused:store.paused, playing:store.playing, played:store.played, events:store.events, sensorPulse:store.sensorPulse, event:e, state:s,
    homing:store.liveTarget?.homing, simulator:isSimulator(), running, index, count:store.events.length, pauseAfterStep:store.pauseAfterStep,
    previousToolchange:toolchangeIndex(store.events, index, -1) >= 0, nextToolchange:toolchangeIndex(store.events, index, 1) >= 0,
    modeText: store.viewMode==='live' ? `Live view · ${connected ? s.action || s.filament_pos_name : 'Disconnected'}`
      : `Telemetry playback · ${modeLabel} · event ${annotatedEvent.seq}` + (moveReason ? ` · ${moveReason}` : ''),
    positionText: `Gate ${gate} · ${s.filament_pos_name || "Unknown state"} · tip ${tip.toFixed(2)} mm`,
    timelineValue: store.live ? eventIndex : store.cursor,
    atLatest: store.viewMode==='live' || (store.live && !store.paused && !store.playing && store.played >= store.events.at(-1)?.seq)};
}
function renderScene() {
  if (store.viewMode==='live' ? !store.displayed : !store.events.length) return;
  const view=currentView();
  renderControls(view);
  const {warnings, unitSensors}=scene.render(view, sceneSettings());
  renderMacros(view.state, !store.offline && isSimulator());
  renderWarning(warnings, store.viewMode==='playback' ? selectorNotice(store.events, view.event, Boolean(store.playing), store.live && !store.paused) : null);
  renderSensors(unitSensors, {offline:store.offline, atLatest:view.atLatest, editable:!store.busy && isSimulator(),
    onToggle:(name, enable)=>action(async () => {
      await sim.sensor(name, enable);
      message(`Sensor ${name} ${enable ? 'enabled' : 'disabled'}.`);
    })});
  renderDevices(view.state);
}
// What a trace row shows, as one string: a row is rebuilt only when this changes
function traceRowKey(e, showDetails, compressed) {
  return [showDetails, compressed && e.fast_bowden, traceText(e), e.label || e.telemetry?.start?.label, e.origin,
    Boolean(e.telemetry?.outcome), (e.changes || []).map(c=>`${c.name}=${JSON.stringify(c.after)}`).join()].join('|');
}
let traceRows=new Map();
function traceRow(e, {showDetails, records, expanded}) {
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
    b.onclick = () => seek(store.events.findIndex(item => item.seq === e.seq));
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
}
function renderTrace() {
  const markers=timelineMarkers(store.events);
  $('toolchange-ticks').closest('.timeline-control').classList.toggle('has-labels',markers.some(marker=>marker.label));
  $('toolchange-ticks').replaceChildren(...markers.map(({index,label}) => {
    const tick = document.createElement('span');
    tick.style.left = `${store.events.length > 1 ? 100*index/(store.events.length-1) : 0}%`;
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
  const showDetails=$('show-internal').checked, compressed=$('compress-bowden').checked;
  const records=new Map(showDetails ? store.sourceRecords.map(record=>[record.sequence,record]) : []);
  // Most steps are unchanged from one batch to the next: keep their rows and build only new or changed ones
  const rows=new Map();
  for (const e of showDetails ? orderedSteps(store.rawEvents) : store.events) {
    const key=traceRowKey(e, showDetails, compressed), kept=traceRows.get(e.seq);
    rows.set(e.seq, kept?.key===key ? kept : {key, row:traceRow(e, {showDetails, records, expanded})});
  }
  traceRows=rows;
  syncChildren(view, [...rows.values()].map(entry=>entry.row));
  view.scrollTop = scroll;
  followTrace();
}
$("show-internal").onchange = renderTrace;
$("beginning").onclick = () => seek(0);
$("previous").onclick = () => seek(currentIndex() - 1);
for (const [id, direction] of [['previous-toolchange', -1], ['next-toolchange', 1]]) {
  $(id).onclick = () => {
    const index = toolchangeIndex(store.events, currentIndex(), direction);
    if (index >= 0) seek(index);
  };
}
$("next").onclick = () => seek(currentIndex() + 1);
document.addEventListener('keydown', event => {
  if (store.viewMode==='live' || (!store.paused && store.live) || event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey
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
  store.pauseAfterStep = false; store.nextStepAt = 0;
  if (!store.events.length) return;
  if (!store.live) {
    const event = store.events[store.cursor];
    store.played = event.seq; store.displayed = event.settled_state || event.state;
    store.playing = null;
  }
  store.paused = false; store.live = true;
  draw(); followTrace();
}
$("play-previous").onclick = () => {
  if ($("play-previous").disabled) return;
  const index = currentIndex();
  store.playing = {event:store.events[index], start:animationNow(), reverse:true,
    targetSeq:store.events[index-1].seq, fraction:1,
    selectionGrip:(store.live && store.displayed ? store.displayed : store.events[index].settled_state || store.events[index].state).mmu_gripped};
  store.displayed = interpolate(store.playing.event,1);
  store.live = true; store.paused = false; store.pauseAfterStep = true; store.replayingHistory = true;
  store.sensorPulse = null;
  draw(); followTrace();
};
$("play-step").onclick = () => {
  if ($("play-step").disabled) return;
  startPlayback();
  store.pauseAfterStep = true;
  draw();
};
$("replay").onclick = () => {
  if (store.live && !store.paused) {
    if (store.playing) store.pauseAfterStep = true;
    else store.paused = true;
    draw();
  } else startPlayback();
};
function followLive() {
  store.pauseAfterStep = false; store.nextStepAt = 0;
  store.replayingHistory = false;
  store.live = true; store.paused = store.offline; store.playing = null; store.sensorPulse = null;
  store.played = store.events.at(-1)?.seq || 0;
  store.displayed = store.events.at(-1)?.settled_state || store.events.at(-1)?.state;
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
  const records = store.sourceRecords;
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
    if (!store.offline && store.socket?.readyState===1) rpc('printer.objects.subscribe',{objects:{}}).catch(()=>{}); // Not awaited: a long G-code would hold the load up
    await telemetryStream.stop();
    store.modeChosen=true; store.viewMode='playback'; store.offline=true; store.subscriptionGeneration++; store.liveTarget=null; store.liveFrom=null;
    store.pending.forEach(p=>p.reject(Error('Switched to offline recording'))); store.pending.clear();
    store.sourceReplay=replay; store.sourceRecords=records;
    store.rawEvents=replay.events; store.events=visibleSteps(store.rawEvents); store.session=replay.session;
    store.live=false; store.paused=true; store.cursor=0; store.playing=null; store.sensorPulse=null; store.pauseAfterStep=false; store.displayed=null;
    $('recording-name').textContent=file.name;
    setConnection('Offline recording · not connected', 'offline');
    sourceMode(); draw(); renderTrace();
    if (replay.checkpoint) message('Loaded checkpoint: earlier motion and inferred geometry are unavailable.');
  } catch(error) { message(error.message,true); }
  finally { $('telemetry-file').value=''; }
};

function updateSource(records, reset=false) {
  const selectedSeq=store.events[currentIndex()]?.seq;
  const newSession=reset || records.some(r=>r.event === 'session');
  if (newSession) { store.sourceReplay=new TelemetryReplay(); store.sourceRecords=[]; }
  store.sourceReplay.accept(records);
  store.sourceRecords.push(...records);
  if (!records.length) return;
  const replay=store.sourceReplay;
  store.rawEvents=replay.events; store.events=visibleSteps(store.rawEvents);
  if (newSession) {
    if (store.session !== replay.session) $('mini-console').replaceChildren();
    store.session=replay.session; store.live=true; store.paused=false; store.replayingHistory=false; store.pauseAfterStep=false;
    store.cursor=0; store.playing=null; store.sensorPulse=null;
    store.played=store.events.at(-1)?.seq || 0; store.displayed=store.events.at(-1)?.settled_state || store.events.at(-1)?.state;
  } else {
    store.cursor=Math.max(0,store.events.findIndex(e=>e.seq === selectedSeq));
    if (store.playing) store.playing.event=store.events.find(e=>e.seq === store.playing.event.seq) || store.playing.event;
    else if (store.live) {
      const current=store.events.find(e=>e.seq === store.played);
      if (current) store.displayed=current.settled_state || current.state;
      else if (store.played > (store.events.at(-1)?.seq ?? 0)) followLive();
    }
  }
  draw(); renderTrace();
}
const telemetryStream=new TelemetryStream(rpc, async data=>{
  if (store.offline || store.viewMode!=='playback') return false;
  updateSource(data.records,data.reset);
  const latest=store.rawEvents.at(-1)?.state;
  const selectedGate=latest ? `${store.session}:${latest.unit}:${latest.gate}` : null;
  if (latest && selectedGate!==store.parameterGate && isSimulator()) {
    store.parameterGate=selectedGate;
    const unit=latest.units?.indexOf(latest.unit) ?? -1;
    if (unit>=0 && $('unit').value!==String(unit)) {
      $('unit').value=String(unit); refresh().catch(error=>message(error.message,true));  // Not awaited: the batch must be acknowledged promptly
    }
  }
  return store.sourceRecords.length>7000;
}, error=>{
  setConnection('Telemetry stream unavailable', 'warning');
  message(`${error.message}. On a printer, enable [mmu_telemetry] in moonraker.conf. You can also load a recording.`,true);
});
let liveIndicator=null;
function updateLiveIndicator() {
  const connected=!store.offline && store.socket?.readyState===1;
  const active=Boolean(connected && (store.viewMode==='live' ? store.liveTarget
    : telemetryStream.active && telemetryStream.ready && store.live && !store.paused
      && (store.playing?.event.seq ?? store.played) >= (store.events.at(-1)?.seq ?? 0)));
  if (active===liveIndicator) return;
  liveIndicator=active;
  $('viewer-live').classList.toggle('active',active);
  $('viewer-live').setAttribute('aria-label',active ? 'Live' : 'Not live');
}
function drawFrame() {
  updateLiveIndicator();
  const now=animationNow();
  const spoolRunning=Boolean(store.playing) || (store.live && !store.paused);
  const spoolMoving=scene.advanceSpools(now, {espoolers:store.displayed?.espoolers, running:spoolRunning, session:store.session});
  if (store.viewMode==='live' && store.liveTarget) {
    const fraction=Math.min(1,(now-store.liveStart)/350);
    store.displayed=liveFrame(store.liveFrom,store.liveTarget,fraction);
    if (fraction<1 || store.sceneDirty || store.liveTarget.homing) draw();
  }
  if (store.viewMode==='playback' && store.live && !store.paused && store.events.length) {
    if (!store.playing && now>=store.nextStepAt) {
      const event = store.events.find(e=>e.seq>store.played);
      if (event) { store.playing={event,start:now,selectionGrip:store.displayed?.mmu_gripped ?? event.before?.mmu_gripped}; followTrace(); }
    }
    if (store.playing) {
      const ms=duration(store.playing.event, $("compress-bowden").checked);
      const arrival=nozzleArrivalTime(store.playing.event,ms);
      const total=Math.max(ms,arrival === null ? 0 : arrival+500), elapsed=Math.min(total,now-store.playing.start);
      const f=total ? elapsed/total : 1;
      const frame=playbackFrame(store.playing.event,store.playing.reverse ? total-elapsed : elapsed,ms,store.playing.selectionGrip);
      store.playing.fraction=frame.fraction;
      store.displayed=frame.state;
      if (f===1 && store.playing.reverse) {
        seek(store.events.findIndex(e=>e.seq === store.playing.targetSeq));
      } else if (f===1) {
        const pulse = homingPulse(store.playing.event);
        if (pulse) store.sensorPulse = {...pulse, start:now};
        store.played=store.playing.event.seq;
        store.nextStepAt=now+homingHold(store.playing.event);
        store.playing=null;
        if (store.pauseAfterStep || (store.offline && store.played >= store.events.at(-1).seq)) { store.paused = true; store.pauseAfterStep = false; }
        followTrace();
      }
      draw();
    }
  }
  if (store.sensorPulse) {
    if (now - store.sensorPulse.start >= 650) store.sensorPulse = null;
    draw();
  }
  scene.returnRackWhenIdle();
  if (store.sceneDirty) {
    store.sceneDirty=false;
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

$('printer-url').value=store.connectionUrl;
$('connect-printer').onclick=()=>{
  try {
    const value=$('printer-url').value.trim();
    const endpoint=new URL(value.includes('://') ? value : 'http://'+value);
    if (!['http:','https:','ws:','wss:'].includes(endpoint.protocol)) throw Error('Use an HTTP or WebSocket printer URL');
    store.connectionUrl=endpoint.href;
    clearTimeout(store.reconnectTimer); store.connectionGeneration++; store.subscriptionGeneration++;
    telemetryStream.stop(false);
    store.socket?.close();
    store.pending.forEach(p=>p.reject(Error('Connection changed'))); store.pending.clear();
    store.modeChosen=false; store.offline=false; store.viewMode='live'; store.liveTarget=null; store.displayed=null; store.playing=null; store.sensorPulse=null;
    store.events=[]; store.rawEvents=[]; store.sourceRecords=[]; store.sourceReplay=new TelemetryReplay();
    scene.clear(); $('mode').textContent='Live view · Connecting…';
    connect();
  } catch(error) { message(error.message,true); }
};
try { connect(); } catch (error) { message(error.message, true); }
requestAnimationFrame(animate);
