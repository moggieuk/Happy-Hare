import {TelemetryStream} from './telemetry-stream.js';
import {LiveState, liveFrame} from './live-state.js';
import {liveMachine} from './live-config.js';
import {simulatorApi, detectConnection} from './simulator.js';
import {overlayAllowed,overlaySnapshot} from './overlay.js';
import {parameterCommand,macroCommand,editorParameters,macroCatalog,editorMacroParameters,toolheadObjects} from './editor.js';
import { SpoolRenderer, renderSpool, spoolWindingRadius, espoolerActive } from './spool.js';
import {TelemetryReplay, parseTelemetry} from './telemetry.js';
import {timelineMarkers} from './playback.js';
import { selectorNotice, orderedSteps, homingHold, rackIdleReturn, spoolTooltip, tweenTo, virtualSelectorLayout, rackGrid, nozzleArrivalTime, toolheadProgress, toolheadDrawingRange, playbackFrame, switchFilamentDistance, syncForDisplay, isInternalUpdate, encoderFilamentDistance, visibleSteps, toolchangeIndex, annotatedStep, excessFilamentLabel, interpolate, constrainFilament, duration, gearAngle, CommandHistory, traceText, homingTarget, homingPulse, bladeStroke, configurationLabel, bowdenLengthLabel, toolheadReferences, coolingTubeMeasurements, dimensionParameter, lastEncoderReading, encoderReference, gateDevicePositions, purgeOutletColor, purgeCoils, purgeStrandLength } from './playback.js';
import {$, escape, options, short as sensorName, DEFAULT_COLOR, round1} from './util.js';
import {consoleLine} from './console-log.js';
import {createToolheadPanel} from './toolhead-panel.js';
import {renderBuffer} from './buffer-view.js';
import {initSpoolPicker} from './spool-picker.js';
import './chrome.js';
const sim = simulatorApi(rpc);
/* Copyright (C) 2022-2026 Paul Morgan. GPLv3. */
function spoolFill(state, gate) {
  return state.spool_percentages?.[gate] ?? 100;
}
let modeChosen=false;
let statusSensors=null, overlayCache={key:'',state:null};
let viewMode='live', liveState=new LiveState(), liveTarget=null, liveFrom=null, liveStart=0, subscriptionGeneration=0;
let offline = false, sourceRecords = [], sourceReplay = new TelemetryReplay();
function sourceMode() {
  toolheadPanel.element.hidden=offline || socket?.readyState !== 1 || toolheadPanel.element.dataset.available !== 'true';
  $('recording-option').hidden=!offline;
  $('view-mode').value=offline ? 'recording' : viewMode;
  $('simulator-controls').hidden=setup?.connection_kind==='live_machine';
  $('sidebar-playback').querySelectorAll('.playback-row, .playback-option').forEach(el=>el.hidden=viewMode==='live');
  $('trace').closest('.card').hidden=viewMode==='live';
  $('recording-name').hidden = !offline;
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

let socket, reconnectTimer, connectionGeneration=0,
  connectionUrl=location.origin,
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
  const generation=++connectionGeneration;
  const endpoint=new URL(connectionUrl);
  endpoint.protocol=endpoint.protocol==='https:' || endpoint.protocol==='wss:' ? 'wss:' : 'ws:';
  endpoint.pathname='/websocket'; endpoint.search=''; endpoint.hash='';
  socket = new WebSocket(endpoint);
  socket.onopen = () => {
    if (generation!==connectionGeneration) return;
    setup=null;
    liveState=new LiveState(); statusSensors=null;
    sourceMode();
    if (offline) return;
    $("connection").textContent = "Connected · identifying source…";
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
      else if (data.params[0].mmu?.sensors) { statusSensors=data.params[0].mmu.sensors; draw(); }
    } else if (['notify_klippy_ready','notify_klippy_shutdown','notify_klippy_disconnected'].includes(data.method)) {
      if (offline) return;
      if (viewMode!=='live') {
        telemetryStream.stop(false);
        if (data.method==='notify_klippy_ready') telemetryStream.start();
        return;
      }
      liveState=new LiveState(); statusSensors=null; liveTarget=null; displayed=null; playing=null; sensorPulse=null;
      if (data.method==='notify_klippy_ready') subscribeLive().catch(e=>message(e.message,true));
      else { $('mode').textContent='Live view · Printer unavailable'; $('scene').replaceChildren(); }
    } else if (data.method === "notify_gcode_command") {
      consoleLine(data.params[0], "command");
    } else if (data.method === "notify_gcode_response") {
      const line = data.params[0];
      consoleLine(line);
      if (line.startsWith("!!") || line.startsWith("Unknown command:")) message(line, true);
    }
  };
  socket.onclose = () => {
    if (generation!==connectionGeneration) return;
    subscriptionGeneration++;
    telemetryStream.stop(false);
    if (liveTarget) { liveTarget.homing=null; liveTarget.espoolers={}; }
    if (viewMode==='live' && displayed) displayed.espoolers={};
    draw();
    sourceMode();
    if (!offline) $("connection").textContent = "Disconnected · retrying";
    pending.forEach((p) => p.reject(Error("Console disconnected")));
    pending.clear();
    reconnectTimer=setTimeout(connect, 1500);
  };
}
async function initializeConnection() {
  const generation=connectionGeneration;
  editorStatic=null;
  const probe=await detectConnection(rpc,Number($('unit').value || 0));
  if (generation!==connectionGeneration) return;
  if (probe.connection_kind==='live_machine') {
    setup=probe;
    $('connection').textContent='Connected to printer'; sourceMode();
    await refreshLiveEditor().catch(error=>message(error.message,true));
  } else await refresh(probe);
  if (generation!==connectionGeneration || offline) return;
  if (!modeChosen && isSimulator()) await changeView('playback');
  else if (viewMode==='live') await subscribeLive();
  else await telemetryStream.start();
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
    /^(extruder\d*|heater_generic .+|gcode_macro .+)$/.test(n)).map(n=>[n,n.startsWith('gcode_macro ') ? ['blade_pos','cutter_before_extruder','cooling_tube_position','cooling_tube_length'] : null]));
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
  if (!next) { $('mode').textContent='Live view · Waiting for MMU configuration'; return; }
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
async function changeView(mode) {
  await telemetryStream.stop();
  subscriptionGeneration++;
  viewMode=mode; offline=false; playing=null; displayed=null; sensorPulse=null;
  events=[]; rawEvents=[]; sourceRecords=[]; sourceReplay=new TelemetryReplay();
  liveTarget=null; liveFrom=null; liveState=new LiveState(); statusSensors=null;
  live=true; paused=false; replayingHistory=false;
  $('scene').replaceChildren();
  $('mode').textContent=mode==='live' ? 'Live view · Connecting…' : isSimulator() ? 'Telemetry playback · Waiting for simulator stream' : 'Telemetry playback · Waiting for printer stream';
  sourceMode(); renderTrace();
  if (mode==='live' && socket?.readyState===1) await subscribeLive();
  else if(socket?.readyState===1) {
    // Near-live shows the stream; sensor readings are overlaid while it is idle.
    statusSensors=(await rpc('printer.objects.subscribe',{objects:{mmu:['sensors']}})).status.mmu?.sensors || null;
    await telemetryStream.start();
  }
}
$('view-mode').onchange=()=>{
  modeChosen=true;
  changeView($('view-mode').value).catch(e=>message(e.message,true));
};
// A printer has no simulator RPC: the editor lists come from configfile, mmu.config_changes and the
// macro objects, and edits are sent as G-code like the command console does.
let editorStatic=null;
async function editorState() {   // configfile once per connection, config_changes every time
  editorStatic ||= rpc('printer.objects.query',{objects:{configfile:['settings','config']}})
    .then(({status})=>({configfile:status.configfile,catalog:macroCatalog(status.configfile.config)}));
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
  if (busy || !script) return;
  if (viewMode==='playback') followLive(); // A command typed here is watched; commands from elsewhere never move a paused view
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
const spoolAngles=new Map(), spoolArrowVisibility=new Map(), spoolDirections=new Map();
let spoolLastFrame=null, spoolSession=null;
const spoolRenderers=new Map();
let renderedSpoolState=null;
function spoolMotion(index,operation) {
  const active=espoolerActive(operation);
  const visibility=spoolArrowVisibility.get(index) ?? (active ? 1 : 0);
  return {rotation:spoolAngles.get(index) || 0,
    arrowOpacity:visibility*visibility*(3-2*visibility),
    direction:active ? operation.direction : spoolDirections.get(index)};
}
function updateSpoolMotion() {
  for (const [index,renderer] of spoolRenderers)
    renderer.updateMotion(spoolMotion(index,renderedSpoolState?.espoolers?.[index]));
}
// Gate lanes and spool columns share the same spacing.
let paused = false, pauseAfterStep = false, replayingHistory = false, sensorPulse = null, nextStepAt = 0;
// The rack of units scrolls to the selected gate; the edge arrows add a temporary manual shift until the gate changes.
let rackGate = null, rackManualShift = 0, rackTween = null;
const rackScrollTime = 350, rackBleedPx = 12;
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
  spoolArrowVisibility.clear();
  cursor = Math.max(0, Math.min(index, events.length - 1));
  live = false; paused = true; replayingHistory = true; playing = null; sensorPulse = null;
  draw(); followTrace();
}
// The MMU gears ease between gripped and released instead of snapping.
let gearSpacingTween = null;
function animatedGearSpacing(target) {
  const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
  gearSpacingTween = tweenTo(reduced ? null : gearSpacingTween, target, animationNow());
  if (!gearSpacingTween.done) draw();
  return gearSpacingTween.value;
}
// An inward ring marks active homing; the outward ring marks completion.
function homingRing(name,x,y) {
  const active=viewMode==='live' ? liveTarget?.homing?.endstops.includes(name) : playing && homingTarget(playing.event)===name;
  if (!active) return '';
  const reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;
  const phase=((animationNow()-(viewMode==='live' ? 0 : playing.start))%600)/600;
  const reverse=viewMode==='live' ? !liveTarget.homing.triggered : (playing.event.requested ?? playing.event.distance ?? 0) < 0;
  const color=reverse ? '#48c982' : 'var(--sensor-clear-pulse)';
  const radius=reduced ? 9 : 13-9*phase;
  const opacity=reduced ? .55 : .6*Math.min(1,phase*8)*(1-.35*phase);
  return `<circle data-part="homing-sensor-ring" cx="${x}" cy="${y}" r="${radius}" fill="none" stroke="${color}" stroke-width="2" opacity="${opacity}" pointer-events="none"/>`;
}
// Fades the ends of the rack to nothing beneath each arrow (a luminance mask: white shows, black hides).
function rackFadeMask({left, right, bleed}) {
  const x0=-bleed, width=850+2*bleed, inner=18, fade=48;                   // Arrow width plus a little clearance
  const stop=(x,white)=>`<stop offset="${(x-x0)/width}" stop-color="${white ? 'white' : 'black'}"/>`;
  const stops=(left ? stop(x0+inner,false)+stop(x0+inner+fade,true) : stop(x0,true))
    +(right ? stop(x0+width-inner-fade,true)+stop(x0+width-inner,false) : stop(x0+width,true));
  return `<defs><linearGradient id="rack-fade-gradient" gradientUnits="userSpaceOnUse" x1="${x0}" x2="${x0+width}" y1="0" y2="0">${(left ? stop(x0,false) : '')+stops+(right ? stop(x0+width,false) : '')}</linearGradient><mask id="rack-edge-fade" maskUnits="userSpaceOnUse" x="${x0}" y="0" width="${width}" height="2000"><rect x="${x0}" y="0" width="${width}" height="2000" fill="url(#rack-fade-gradient)"/></mask></defs>`;
}
const rackScrollStep = 300;
function scrollRack(arrow) {
  rackManualShift += -Number(arrow.dataset.direction)*rackScrollStep;
  lastActivity = performance.now();
  draw();
}
$('scene').addEventListener('click',event=>{
  const arrow=event.target.closest('[data-part="rack-scroll"]');
  if (arrow) scrollRack(arrow);
});
$('scene').addEventListener('keydown',event=>{
  const arrow=event.target.closest?.('[data-part="rack-scroll"]');
  if (arrow && ['Enter',' '].includes(event.key)) { event.preventDefault(); scrollRack(arrow); }
});
// Any pointer, key or tooltip use counts as activity; a rack left scrolled by hand eases back to the selected gate.
let lastActivity = performance.now();
for (const type of ['pointermove','pointerdown','keydown','wheel','focusin'])
  document.addEventListener(type,()=>{ lastActivity=performance.now(); },{passive:true,capture:true});
function returnRackWhenIdle() {
  if (!$('viewer-tooltip').hidden) lastActivity=performance.now();
  if (rackIdleReturn(rackManualShift,performance.now()-lastActivity)) { rackManualShift=0; draw(); }
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
function renderScene() {
  if (viewMode==='live' ? !displayed : !events.length) return;
  const spoolRequests=new Map();
  cursor = Math.max(0, Math.min(cursor, events.length - 1));
  const running = viewMode==='playback' && live && !paused && (playing || played < events.at(-1).seq);
  $("replay").disabled = pauseAfterStep || (!running && currentIndex() >= events.length - 1);
  const playLabel = running ? 'Pause after current step' : offline ? 'Play recording' : 'Play continuously — follow stream when caught up';
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
  const e = viewMode==='live' ? {state:displayed,seq:0,kind:'state'} : live ? (playing?.event || events.find(e=>e.seq===played) || events.at(-1)) : events[cursor];
  const
    s = idleOverlay() || (live && displayed ? displayed : (e.settled_state || e.state)),
    gate = s.gate >= 0 ? s.gate : 0;
  renderedSpoolState=s;
  let tip = s.visual_tips?.[gate] ?? s.tips[gate] ?? -1000;
  const {entry,end,height:toolheadHeight,sensorEntry,sensorGap} = toolheadDrawingRange(s);
  const eventsBehind = events.length - 1 - events.indexOf(e);
  if (live && !paused && !playing && eventsBehind === 0) replayingHistory = false;
  const modeLabel = paused || !live ? 'Paused' : playing || eventsBehind>0 ? 'Playing' : offline ? 'End of recording' : 'Following stream';
  // Bookkeeping events must not clear the last animated movement's annotation.
  const annotatedEvent = viewMode==='live' ? e : annotatedStep(events, e, !live || paused);
  const moveReason = annotatedEvent.label || annotatedEvent.telemetry?.start?.label;
  $("mode").textContent = viewMode==='live' ? `Live view · ${socket?.readyState===1 ? s.action || s.filament_pos_name : 'Disconnected'}`
    : `Telemetry playback · ${modeLabel} · event ${annotatedEvent.seq}` + (moveReason ? ` · ${moveReason}` : '');
  $("position").textContent = `Gate ${gate} · ${s.filament_pos_name || "Unknown state"} · tip ${tip.toFixed(2)} mm`;
  $("timeline").max = events.length - 1;
  $("timeline").value = live ? events.indexOf(e) : cursor;
  // Fixed path with a compressed Bowden section. Position maps monotonically to it.
  const unitGates=s.unit_gates || s.tips.map((_,index)=>index);
  const previewCount=Math.max(0,Math.min(42,Math.trunc(Number($('preview-gate-count').value) || 0)));
  const displayGates=previewCount ? Array.from({length:previewCount},(_,i)=>unitGates[i] ?? s.tips.length+i-unitGates.length) : unitGates;
  // Keep the real selected gate visible when previewing a smaller unit.
  if (previewCount && s.gate>=0 && !displayGates.includes(s.gate)) displayGates[displayGates.length-1]=s.gate;
  const mmuOffset=60; // Keep the enclosure below the purge animation.
  const hardwareOffset=mmuOffset-10; // Leave clearance above the enclosure.
  const virtual=virtualSelectorLayout({...s,unit_gates:displayGates},Number($('spool-spacing').value),mmuOffset);
  const mmuDistanceAtY=y=>{
    if (!virtual) return 570+mmuOffset-y;
    // Resolve positions on the rounded branch rather than its sharp-corner approximation.
    let low=0, high=virtual.distanceAtY(150)+20;
    for (let i=0;i<24;i++) {
      const middle=(low+high)/2;
      if (tube.getPointAtLength(middle).y>y) low=middle; else high=middle;
    }
    return (low+high)/2;
  };
  const path = virtual?.path ||
    `M620 ${570+mmuOffset} L620 150 Q620 75 525 75 L355 75 Q260 75 260 170 L260 449`;
  const dimensionLink = label => {
    const id = dimensionParameter(s, label);
    return id && !offline && isSimulator() ? `class="editable-dimension" role="button" tabindex="0" data-parameter="${escape(id)}" data-unit="${s.units?.indexOf(s.unit) ?? 0}" aria-label="Edit ${escape(label)}"` : '';
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
  const bowdenPath = path.replace(new RegExp(`^M([^ ]+) ${570+mmuOffset}`), `M$1 ${470+mmuOffset}`).replace("L260 449", "L260 435");
  const branchOpacity=virtual ? Math.max(0,1-Math.abs(s.gate-(s.gate_focus ?? s.gate))) : 1;
  const branchShade=Math.round(branchOpacity*255);
  const branchMask=virtual ? `<defs><mask id="virtual-selection-fade" maskUnits="userSpaceOnUse" x="0" y="0" width="850" height="700"><rect width="850" height="700" fill="white"/><rect x="350" y="${virtual.hub}" width="500" height="${700-virtual.hub}" fill="rgb(${branchShade},${branchShade},${branchShade})"/></mask></defs>` : '';

  let svg = `${branchMask}<path id="extruder-body" d="M250 205 H242 Q240 205 240 207 V435 H280 V207 Q280 205 278 205 H270" fill="var(--scene-body, #edf2f6)" stroke="var(--scene-border, #96abba)"/><rect x="213" y="375" width="94" height="58" rx="5" fill="var(--scene-heater, #c4d3df)"/><rect id="nozzle-collar" x="240" y="433" width="40" height="2" fill="var(--scene-nozzle, #9cafbe)"/><path id="nozzle-body" d="${nozzleBody}" fill="var(--scene-nozzle, #9cafbe)"/><path id="nozzle-bore" d="${nozzleFunnel}" fill="var(--scene-tube, #edf4f8)"/><g><g${virtual ? ' mask="url(#virtual-selection-fade)"' : ''}><path id="tube" d="${path}" fill="none" stroke="none"/><g><g${s.devices[`${s.unit}:buffer`] ? ' mask="url(#buffer-tube-gap)"' : ''}><path d="${bowdenPath}" fill="none" stroke="var(--scene-tube-border, #b1c4d2)" stroke-width="19.25"/><path d="${bowdenPath}" fill="none" stroke="var(--scene-tube, #edf4f8)" stroke-width="14.75"/></g></g><path id="nozzle-entrance" d="${nozzleEntrance}" fill="var(--scene-tube, #edf4f8)"/><path id="filament" mask="url(#bowden-end-fade)" d="${path}" fill="none" stroke="#${s.filament?.incoming_color || DEFAULT_COLOR}" stroke-width="7"/></g></g><text ${dimensionLink('Bowden')} id="bowden-length" x="660" y="100" text-anchor="start" fill="var(--scene-muted, #6e8190)" font-size="13">${bowdenLengthLabel(s).includes('Uncalibrated') ? '<tspan x="660">Bowden</tspan><tspan x="660" dy="16">Uncalibrated length</tspan>' : bowdenLengthLabel(s)}</text>`;
  const parkedTips=[];
  if (virtual) {
    let branches='';
    for (const branch of virtual.branches) {
      const branchPath=virtual.branchPath(branch.x,470+mmuOffset);
      branches+='<g opacity=".4">';
      branches+=`<path data-part="virtual-gate-tube" data-gate="${branch.gate}" d="${branchPath}" fill="none" stroke="var(--scene-tube-border, #b1c4d2)" stroke-width="19.25"/><path d="${branchPath}" fill="none" stroke="var(--scene-tube, #edf4f8)" stroke-width="14.75"/>`;
      if (s.gate_status?.[branch.gate]!==0 && spoolFill(s,branch.gate)>0) {
        const cone=s.tip_shapes?.[branch.gate] === 'cone';
        branches+=`<path data-part="virtual-parked-filament" data-gate="${branch.gate}" mask="url(#bowden-end-fade)" d="M${branch.x} ${484+mmuOffset} V${virtual.bottom-virtual.parkDistance+(cone ? toolheadHeight/(end-entry) : 0)}" fill="none" stroke="#${s.gate_colors[branch.gate] || DEFAULT_COLOR}" stroke-width="7"/>`;
        if (cone && branch.gate !== gate) {
          parkedTips.push(branch);
          branches+=`<!--parked-tip-${branch.gate}-->`;
        }
      }
      branches+='</g>';
    }
    // The active tube is painted last so inactive branches cannot close its visible bore.
    svg=branches+svg;
  }
  $("scene").innerHTML = svg;
  const tube = $("tube"),
    length = tube.getTotalLength();
  const mmuExitFraction = (virtual ? mmuDistanceAtY(400+hardwareOffset) : 200) / length;
  const toolheadFraction=(length-toolheadHeight)/length;
  const progress = (p) =>
    p < 0
      ? Math.max(0, ((p - Math.min(-100, (s.layout.mmu_entry ?? -150)-40)) / -Math.min(-100, (s.layout.mmu_entry ?? -150)-40)) * mmuExitFraction)
      : p < sensorEntry
        ? mmuExitFraction + ((toolheadFraction-sensorGap/length-mmuExitFraction) * p) / sensorEntry
        : p < entry
          ? toolheadFraction-sensorGap/length+(sensorGap/length)*(p-sensorEntry)/(entry-sensorEntry)
        : Math.min(1, toolheadFraction + (1-toolheadFraction) * toolheadProgress(s,p));
  const pathDistance = position => progress(position)*length;
  if (Number.isFinite(s.extruder_target_temperature))
    svg += `<text id="extruder-temperature" x="218" y="426" font-size="9" fill="var(--scene-text, #304c65)" data-tooltip="Extruder target temperature">${round1(s.extruder_target_temperature)}°C</text>`;
  const unitSensors = Object.entries(s.sensors).filter(([name]) => {
    if (name === `${s.unit}:mmu_gear_touch`) return true;
    if (s.active_sensors && !(virtual && /^mmu_(entry|exit)(?:_\d+)?$/.test(sensorName(name)))) return s.active_sensors.includes(name);
    if (s.unit_sensors && !s.unit_sensors.includes(name)) return false;
    const gateSuffix = sensorName(name).match(/^mmu_.*_(\d+)$/);
    return !gateSuffix || (virtual ? virtual.branches.some(branch=>branch.gate===Number(gateSuffix[1])) : Number(gateSuffix[1]) === s.gate);
  });
  const isTouchSensor=name=>/(?:^|:)mmu_gear_touch(?:_\d+)?$/.test(name);
  const encoderAnchor = encoderReference(s, unitSensors);
  const gateSlots = Object.fromEntries(Object.entries(gateDevicePositions(s, unitSensors)).map(([key,value])=>[key,Number.isFinite(value) ? value+100 : value]));
  if (virtual) {
    const shared=unitSensors.some(([name])=>name.endsWith(':mmu_shared_exit'));
    const encoder=Boolean(s.devices?.[`${s.unit}:encoder`]);
    const encoderBefore=s.encoder_reference?.endstop==='mmu_shared_exit' && s.encoder_reference.offset<0;
    gateSlots.sharedExit=shared && encoder ? (encoderBefore ? 158 : 177) : 170;
    gateSlots.encoder=shared && encoder ? (encoderBefore ? 177 : 158) : 170;
  }
  for (const key of Object.keys(gateSlots)) if (Number.isFinite(gateSlots[key])) gateSlots[key]+=hardwareOffset;
  const sensorPoint = (name, device) => {
    if (isTouchSensor(name)) return {x:260,y:420};
    const point = tube.getPointAtLength(progress(device.position)*length);
    const short = name.split(':').at(-1);
    const branchSensor=short.match(/^mmu_(entry|exit)(?:_(\d+))?$/);
    if (virtual && branchSensor) {
      const sensorGate=branchSensor[2]===undefined ? gate : Number(branchSensor[2]);
      const branch=virtual.branches.find(item=>item.gate===sensorGate);
      if (branch) return {x:branch.x,y:(branchSensor[1]==='entry' ? 465 : 400)+hardwareOffset};
    }
    if (/^mmu_entry(?:_\d+)?$/.test(short)) { point.x = 620; point.y = 465+hardwareOffset; }
    if (/^mmu_exit(?:_\d+)?$/.test(short)) { point.x = 620; point.y = gateSlots.exit; }
    if (short === 'mmu_shared_exit') { point.x = 620; point.y = gateSlots.sharedExit; }
    return point;
  };
  const isBufferSensor = name => /(?:^|:)filament_(compression|tension)$/.test(name);
  const sensorFill = state => state === true ? '#399b64' : state === false ? '#fff' : '#9ba5ae';
  const encoderPoint = () => ({x:620, y:gateSlots.encoder ?? 360+hardwareOffset});
  const tipAtPathDistance = distance => {
    const target = Math.max(0,distance)/length;
    let low = Math.min(-100,(s.layout.mmu_entry ?? -150)-40), high = end;
    for (let i=0;i<32;i++) { const mid=(low+high)/2; if(progress(mid)<target) low=mid; else high=mid; }
    return (low+high)/2;
  };
  let schematicDistance = position => progress(position)*length;
  let schematicTip = null;
  // UNKNOWN uses an illustrative endpoint between the MMU gears and its outlet.
  if (s.gate >= 0 && s.filament_pos === -1 && e.kind !== 'movement') tip = tipAtPathDistance(virtual ? virtual.parkDistance : 570-405);
  // Encoder loading keeps one reference across START OF BOWDEN bookkeeping.
  if (s.gate >= 0 && s.filament_pos >= 0 && tip < entry && s.encoder_reference?.endstop === 'encoder') {
    if (virtual) {
      schematicDistance=position=>switchFilamentDistance(position,s.layout.mmu_encoder,
        s.encoder_reference.parking,entry,mmuDistanceAtY(encoderPoint().y),pathDistance,
        null,s.encoder_reference.parking<0 ? virtual.parkDistance : null);
      schematicTip=schematicDistance(tip);
      tip=tipAtPathDistance(schematicTip);
    } else tip = tipAtPathDistance(encoderFilamentDistance(events,e,s,tip,pathDistance,mmuDistanceAtY(encoderPoint().y)));
  } else if (s.gate >= 0 && s.filament_pos >= 0 && tip < entry) {
    const endstop = s.encoder_reference?.endstop?.split(':').at(-1);
    const sensor = unitSensors.find(([name,d]) => Number.isFinite(d.position) &&
      [endstop,`${endstop}_${gate}`].includes(sensorName(name)));
    if (sensor) {
      schematicDistance = position => switchFilamentDistance(position,sensor[1].position,
        s.encoder_reference?.parking || 0,entry,mmuDistanceAtY(sensorPoint(...sensor).y),pathDistance,
        encoderAnchor?.name === sensor[0] && s.devices?.[`${s.unit}:encoder`]
          ? {position:sensor[1].position+encoderAnchor.offset,distance:mmuDistanceAtY(encoderPoint().y)} : null,
        virtual && endstop==='mmu_shared_exit' && s.encoder_reference.parking<0 ? virtual.parkDistance : null);
      schematicTip = schematicDistance(tip);
      tip = tipAtPathDistance(schematicTip);
    }
  }
  const tipScale = toolheadHeight/(end-entry);
  const coneOffset = s.tip_shapes?.[gate] === 'cone' ? tipScale : 0;
  // Apply recorded transitions where the drawn tip crosses, after schematic Bowden scaling.
  const displayedSensors=unitSensors.map(([name,device])=>{
    const before=e.before?.sensors[name], after=e.state.sensors[name];
    if (playing && e.kind === 'movement' && before && after && before.state !== after.state &&
        ['extruder','toolhead'].includes(sensorName(name))) {
      return [name,{...device,state:pathDistance(tip)>=pathDistance(device.position)-1e-6}];
    }
    return [name,device];
  });
  const presenceSensors = displayedSensors.filter(([name,d]) =>
    (!virtual || !/_\d+$/.test(name) || Number(name.match(/_(\d+)$/)[1])===gate) && !isBufferSensor(name) &&
    !name.includes('encoder') && !isTouchSensor(name) && d.position !== null).map(([name,d]) => {
      const short=name.split(':').at(-1);
      const position = /^mmu_(?:entry|exit)(?:_\d+)?$/.test(short) || short === 'mmu_shared_exit'
        ? mmuDistanceAtY(sensorPoint(name,d).y) : pathDistance(d.position);
      // An encoder-derived tip may differ from a sensor by the encoder's movement threshold,
      // 1.5 x resolution as in Happy Hare's movement_min(); that much is not worth a warning
      const slack=1.5*(s.devices?.[`${s.unit}:encoder`]?.resolution || 0);
      return {...d,name,position,tolerance:slack ? Math.abs(pathDistance(d.position+slack)-pathDistance(d.position)) : 0};
    });
  const fragments = s.retained_segment ? [{start:pathDistance(s.retained_segment.start),end:pathDistance(s.retained_segment.end)}] : [];
  const constrained = constrainFilament(schematicTip ?? pathDistance(tip),
    s.tails[gate] === null ? null : pathDistance(s.tails[gate]), presenceSensors, fragments, s.sensor_conflicts || []);
  const focus = s.gate_focus ?? s.gate;
  // Start just inside the upper spool rim; the fixed fade hides the insertion point.
  const fullTipOpacity=virtual ? 1 : s.gate_full_opacity ?? 1;
  const slidingRack=fullTipOpacity===0;
  const spoolTip=570-504;
  const selectedTip=constrained.tip+(Math.min(constrained.tip,spoolTip)-constrained.tip)*(virtual ? 0 : s.gate_retraction || 0);
  tip = tipAtPathDistance(selectedTip);
  const shapedTipOpacity=fullTipOpacity*(virtual && selectedTip<virtual.distanceAtY(virtual.hub) ? branchOpacity : 1)*Math.max(0,Math.min(1,(selectedTip-(570-502))/28));
  const start = s.tails[gate] === null ? 0 : pathDistance(s.tails[gate]), stop = Math.max(0,selectedTip-coneOffset);
  const macroUnit=s.units?.indexOf(s.unit) ?? 0;
  const macroParams=s.configuration?.parameters?.[macroUnit] || {};
  $('scene-macros').innerHTML=[['purge_macro','Purge macro'],['form_tip_macro','Tip-forming macro']].map(([key,label])=>{
    const name=macroParams[key];
    const link=offline || !isSimulator() ? '' : `role="button" tabindex="0" data-parameter="${key}" data-macro="${escape(name || '')}" data-unit="${macroUnit}" aria-label="Browse ${label} variables"`;
    return `<div class="macro-label">${label}</div><span class="macro-name" ${link}>${escape(name === undefined ? 'Not recorded' : name || 'None')}</span>`;
  }).join('');
  renderWarning(constrained.warnings, viewMode==='playback' ? selectorNotice(events, e, Boolean(playing), live && !paused) : null);
  $("filament").setAttribute(
    "stroke-dasharray",
    `0 ${start} ${Math.max(0, stop - start)} ${length + 1}`,
  );
  let rackArrows='', rackFade=null, bowdenReach=1;
  if (s.tips.length) {
    const spoolViewAngle=Number($('spool-angle').value)*Math.PI/180;
    const focusGate=s.gate>=0 ? focus : displayGates[0];
    // Every unit sits side by side on one rack that slides to keep the selected spool under the Bowden exit.
    const layouts=(s.unit_layouts?.length ? s.unit_layouts : [{name:s.unit,gates:unitGates,selector_type:s.selector_type}])
      .map(layout=>layout.name===s.unit ? {...layout,gates:displayGates} : layout);
    if (s.gate!==rackGate || s.gate_slide!==undefined) { rackGate=s.gate; rackManualShift=0; }
    // The scene reaches the panel edges: it bleeds into the card padding, at the same scale.
    const sceneWidth=$('scene').clientWidth, bleed=sceneWidth>2*rackBleedPx ? rackBleedPx*850/(sceneWidth-2*rackBleedPx) : 0;
    const rackOptions={current:s.unit,focus:focusGate,spacing:Number($('spool-spacing').value),angle:spoolViewAngle,bleed,
      slide:s.gate_slide!==undefined ? {from:s.gate_from,to:s.gate_to,t:s.gate_slide} : null};
    const targetGrid=rackGrid(layouts,{...rackOptions,manual:rackManualShift});
    rackManualShift=targetGrid.manual;
    bowdenReach=targetGrid.reach;
    // The arrows scroll with a short ease instead of jumping.
    const reducedMotion=matchMedia('(prefers-reduced-motion: reduce)').matches;
    rackTween=tweenTo(reducedMotion ? null : rackTween,rackManualShift,animationNow(),rackScrollTime);
    if (!rackTween.done) draw();
    const grid=rackTween.value===rackManualShift ? targetGrid : rackGrid(layouts,{...rackOptions,manual:rackTween.value});
    bowdenReach=grid.reach;
    const visible=grid.lanes.filter(lane=>lane.x>-140 && lane.x<990).sort((a,b)=>a.row-b.row || b.x-a.x);
    const rowSpacing=135;
    const lastRowY=Math.max(0,grid.rows-1)*rowSpacing;
    const enclosureBottom=659+lastRowY+mmuOffset;
    $('scene').setAttribute('viewBox',`${-bleed} 0 ${850+2*bleed} ${enclosureBottom+21}`);
    const shells=grid.units.map(unit=>{
      const enclosureLeft=unit.left, enclosureRight=unit.right, unitLastRow=(unit.rows-1)*rowSpacing;
      const glassLeft=enclosureLeft+6, glassRight=enclosureRight-6;
      return {unit,enclosureLeft,enclosureRight,glassLeft,glassRight,unitLastRow,
        coverPath:`M${glassLeft} ${585+mmuOffset} V${510+mmuOffset} Q${glassLeft} ${470+mmuOffset} ${glassLeft+40} ${470+mmuOffset} H${glassRight-40} Q${glassRight} ${470+mmuOffset} ${glassRight} ${510+mmuOffset} V${585+mmuOffset} Z`,
        lowerHousingPath:unitLastRow ? `M${enclosureLeft} ${580+mmuOffset} Q${enclosureLeft} ${575+mmuOffset} ${enclosureLeft+5} ${575+mmuOffset} H${enclosureRight-5} Q${enclosureRight} ${575+mmuOffset} ${enclosureRight} ${580+mmuOffset} V${585+unitLastRow+mmuOffset} H${enclosureLeft} Z` : ''};
    });
    const coverPath=shells.map(shell=>shell.coverPath).join('');
    const lowerHousingPath=shells.map(shell=>shell.lowerHousingPath).join('');
    const enclosurePath=coverPath+lowerHousingPath;
    svg += `<defs><linearGradient id="spool-shelf-fade" x1="0" y1="0" x2="0" y2="1"><stop stop-color="color-mix(in srgb, var(--scene-body, #edf2f6), white 18%)"/><stop offset=".12" stop-color="var(--scene-body, #edf2f6)"/><stop offset=".65" stop-color="var(--scene-body, #edf2f6)"/><stop offset="1" stop-color="var(--scene-body, #edf2f6)" stop-opacity="0"/></linearGradient><linearGradient id="spool-glass" x1="0" y1="0" x2=".3" y2="1"><stop stop-color="white" stop-opacity=".22"/><stop offset=".12" stop-color="white" stop-opacity=".07"/><stop offset=".3" stop-color="white" stop-opacity=".02"/><stop offset="1" stop-color="black" stop-opacity=".1"/></linearGradient><linearGradient id="spool-cabinet" x1="0" y1="0" x2="0" y2="1"><stop stop-color="color-mix(in srgb, var(--scene-body, #edf2f6), white 18%)"/><stop offset=".055" stop-color="var(--scene-body, #edf2f6)"/><stop offset="1" stop-color="color-mix(in srgb, var(--scene-body, #edf2f6), black 18%)"/></linearGradient><mask id="outside-mmu" maskUnits="userSpaceOnUse" x="0" y="0" width="1200" height="2000"><rect width="1200" height="2000" fill="white"/><path d="${enclosurePath}" fill="black"/></mask><clipPath id="spool-cover-clip"><path d="${enclosurePath}"/></clipPath></defs>`;
    // Put the enclosure behind the short filament fade; the glass stays above it.
    svg = `<path data-part="spool-enclosure" d="${enclosurePath}" fill="color-mix(in srgb, var(--scene-body, #edf2f6), black 12%)"/>` + svg;
    if ($('heating-preview').checked || (viewMode==='live' && s.heaters?.some(h=>h.target>0))) {
      svg += `<defs><radialGradient id="mmu-heating-glow" cx=".5" cy=".9" r=".85"><stop stop-color="#ffbd59" stop-opacity=".65"/><stop offset=".45" stop-color="#ef8d27" stop-opacity=".3"/><stop offset="1" stop-color="#e87516" stop-opacity="0"/></radialGradient></defs><g data-part="heating-preview" clip-path="url(#spool-cover-clip)" pointer-events="none">`;
      for (const {unit,glassLeft,glassRight} of shells) for (let row=0; row<unit.rows; row++) {
        const lanes=grid.lanes.filter(lane=>lane.unit===unit.name && lane.row===row).sort((a,b)=>a.x-b.x);
        const current=unit.name===s.unit;
        const regions=current && s.has_per_gate_heaters ? lanes.map((lane,i)=>({
          heating:s.heaters?.[unitGates.indexOf(lane.index)]?.target>0,
          left:i ? (lanes[i-1].x+lane.x)/2 : glassLeft,
          right:i<lanes.length-1 ? (lane.x+lanes[i+1].x)/2 : glassRight,
        })) : [{left:glassLeft,right:glassRight,heating:current && s.heaters?.some(h=>h.target>0)}];
        for (const region of regions.filter(r=>$('heating-preview').checked || r.heating))
          svg += `<rect data-part="heating-glow" x="${region.left}" y="${470+mmuOffset+row*rowSpacing}" width="${region.right-region.left}" height="105" fill="url(#mmu-heating-glow)"/>`;
      }
      svg += '</g>';
    }
    svg += '<defs><filter id="spool-separation-shadow" x="-30%" y="-20%" width="160%" height="145%"><feDropShadow dx="1.2" dy=".8" stdDeviation=".6" flood-color="#000" flood-opacity=".65"/></filter></defs><g id="espooler-indicators" clip-path="url(#spool-cover-clip)">';
    const spoolStyle=document.querySelector('input[name=spool-style]:checked').value;
    const spoolDepth=34/3, spoolScale=3.3;

    for (const {index,x:laneX,row,lift} of visible) {
      const operation=s.espoolers?.[index];
      const active=espoolerActive(operation);
      // Offset the winding slightly right of the filament lane.
      const x=laneX-spoolDepth*Math.sin(spoolViewAngle)*spoolScale/2;
      const y=549.6+row*rowSpacing-lift+mmuOffset;
      const color=s.gate_colors[index] || DEFAULT_COLOR;
      const percent=spoolFill(s,index);
      const branch=virtual?.branches.find(branch=>branch.gate===index);
      if (branch && percent>0 && s.gate_status?.[index]!==0) {
        const top=470+mmuOffset, bottom=y-spoolWindingRadius(percent)*spoolScale;
        const opacity=index===gate ? .5 : .2;
        svg += `<defs><linearGradient id="spool-feed-fade-${index}" gradientUnits="userSpaceOnUse" x1="0" x2="0" y1="${top}" y2="${bottom}"><stop stop-color="#${color}" stop-opacity="${opacity}"/><stop offset="1" stop-color="#${color}" stop-opacity="0"/></linearGradient></defs><path data-part="virtual-spool-feed" data-gate="${index}" d="M${branch.x} ${top} V${bottom}" stroke="url(#spool-feed-fade-${index})" stroke-width="7" fill="none"/>`;
      }
      const knownWeight=Number.isFinite(s.spool_percentages?.[index]);
      const title=spoolTooltip(s,index);
      const shelfY=575+row*rowSpacing+mmuOffset;
      svg += `<defs><linearGradient id="spool-shelf-edge-${index}" gradientUnits="userSpaceOnUse" x1="0" y1="${shelfY-3}" x2="0" y2="${shelfY}"><stop stop-color="white"/><stop offset="1" stop-color="black"/></linearGradient><mask id="spool-shelf-mask-${index}" maskUnits="userSpaceOnUse" x="${x-80}" y="${y-70}" width="160" height="150"><rect x="${x-80}" y="${y-70}" width="160" height="150" fill="url(#spool-shelf-edge-${index})"/></mask></defs><g mask="url(#spool-shelf-mask-${index})">`;
      svg += `<g data-part="espooler" data-gate="${index}" data-active="${active}" data-fill-percent="${percent}" data-tooltip="${escape(title)}" transform="translate(${x} ${y}) scale(${spoolScale})" filter="url(#spool-separation-shadow)">`;
      spoolRequests.set(index,{idPrefix:`gate-${index}`, style:spoolStyle, color,
        fillPercent:percent, showFillPercent:knownWeight, shadowStyle:$('spool-realism').checked ? 'blur' : 'crisp',windingLines:$('spool-realism').checked, viewAngle:spoolViewAngle, ...spoolMotion(index,operation),
        glow:index===s.gate ? Math.max(0,1-Math.abs(index-focus)) : 0});
      svg += '</g></g>';
    }
    svg += '</g>';
    let glassDividers='';
    for (const {unit} of shells) for (let row=0; s.has_per_gate_heaters && unit.name===s.unit && row<unit.rows; row++) {
      const lanes=grid.lanes.filter(lane=>lane.unit===unit.name && lane.row===row).sort((a,b)=>a.x-b.x);
      const top=470+mmuOffset+row*rowSpacing, bottom=575+mmuOffset+row*rowSpacing;
      for (let i=1;i<lanes.length;i++) {
        const x=(lanes[i-1].x+lanes[i].x)/2;
        const perspective=Math.max(0,Math.cos(spoolViewAngle)/Math.cos(74*Math.PI/180));
        const shift=10*perspective, bend=18*perspective;
        const divider=perspective<.001 ? `M${x} ${top} V${bottom}`
          : `M${x} ${top} C${x+shift*.55} ${top+bend*.3} ${x+shift} ${top+bend*.5} ${x+shift} ${top+bend} V${bottom}`;
        glassDividers+=`<defs><filter id="spool-divider-blur-${row}-${i}" filterUnits="userSpaceOnUse" x="${x-8}" y="${top-5}" width="${shift+16}" height="${bottom-top+10}"><feGaussianBlur stdDeviation="1.3"/></filter></defs><g data-part="spool-glass-divider"><path data-part="spool-divider-shadow" d="${divider}" transform="translate(-2.2 0)" fill="none" stroke="black" stroke-opacity=".2" stroke-width="3" filter="url(#spool-divider-blur-${row}-${i})"/><path d="${divider}" fill="none" stroke="black" stroke-opacity=".17" stroke-width="2"/><path d="${divider}" transform="translate(.9 0)" fill="none" stroke="white" stroke-opacity=".18" stroke-width="1.1"/></g>`;
      }
    }
    glassDividers=`<g clip-path="url(#spool-cover-clip)" pointer-events="none">${glassDividers}</g>`;
    const glassDefs=`<defs><filter id="spool-glass-soften" x="-5%" y="-25%" width="110%" height="150%"><feGaussianBlur stdDeviation=".7"/></filter><linearGradient id="spool-glass-reflection"><stop stop-color="white" stop-opacity="0"/><stop offset=".15" stop-color="white" stop-opacity=".25"/><stop offset=".55" stop-color="white" stop-opacity=".08"/><stop offset="1" stop-color="white" stop-opacity="0"/></linearGradient></defs>`;
    const glassHighlight=glassDefs+shells.map(({glassLeft,glassRight})=>`<path data-part="spool-glass-highlight" filter="url(#spool-glass-soften)" d="M${glassLeft+4} ${510+mmuOffset} Q${glassLeft+4} ${474+mmuOffset} ${glassLeft+40} ${474+mmuOffset} H${glassRight-40} Q${glassRight-4} ${474+mmuOffset} ${glassRight-4} ${510+mmuOffset}" fill="none" stroke="url(#spool-glass-reflection)" stroke-width="2" stroke-linecap="round" pointer-events="none"/>`).join('');
    svg += `${lowerHousingPath ? `<path data-part="spool-lower-housing" d="${lowerHousingPath}" fill="none" stroke="color-mix(in srgb, var(--scene-border, #96abba), transparent 60%)" stroke-width=".6"/>` : ''}<path data-part="spool-glass-cover" d="${coverPath}" fill="url(#spool-glass)" stroke="color-mix(in srgb, var(--scene-border, #96abba), transparent 55%)" stroke-width=".8" pointer-events="none"/>${glassDividers}${glassHighlight}${shells.map(({enclosureLeft,enclosureRight,unitLastRow})=>`<rect data-part="spool-cabinet" x="${enclosureLeft}" y="${575+unitLastRow+mmuOffset}" width="${enclosureRight-enclosureLeft}" height="84" rx="6" fill="url(#spool-cabinet)" stroke="color-mix(in srgb, var(--scene-border, #96abba), transparent 60%)" stroke-width=".6"/>`).join('')}`;
    for (const {unit,enclosureLeft,enclosureRight} of shells) for (let row=0; row<unit.rows-1; row++) {
      svg += `<rect data-part="spool-row-shelf" x="${enclosureLeft}" y="${575+row*rowSpacing+mmuOffset}" width="${enclosureRight-enclosureLeft}" height="49" rx="5" fill="url(#spool-shelf-fade)"/>`;
    }
    for (const {index,x:laneX,row} of visible) {
      const status=s.gate_status?.[index];
      const border=status===0 ? '#858b91' : status>0 ? '#399b64' : '#d58a30';
      // A pill is selected only while its spool is fully raised.
      const selected=index===s.gate && focus===index;
      const x=laneX+4;
      svg += `<g data-part="gate-status-pill" data-gate="${index}" data-tooltip="Gate ${index} · ${status===0 ? 'Empty' : status>0 ? 'Available' : 'Unknown'}"><rect x="${x-16}" y="${587+row*rowSpacing+mmuOffset}" width="32" height="18" rx="9" fill="${selected ? '#36ca42' : 'color-mix(in srgb, var(--scene-body, #edf2f6), black 12%)'}" stroke="${border}" stroke-width="2"/><text x="${x}" y="${596+row*rowSpacing+mmuOffset}" dominant-baseline="central" text-anchor="middle" font-size="11" font-weight="700" fill="${selected ? '#102514' : 'var(--scene-text, #304c65)'}">${index}</text></g>`;
    }
    // Narrow vertical arrows at the panel edges scroll to units that are off screen; the rack fades out beneath them.
    if (grid.overflow.left || grid.overflow.right) rackFade={left:grid.overflow.left,right:grid.overflow.right,bleed};
    for (const side of [-1,1]) {
      if (!(side<0 ? grid.overflow.left : grid.overflow.right)) continue;
      // The same size whatever the row count, level with the top row of spools.
      const top=470+mmuOffset, height=105, width=16, left=side<0 ? -bleed+2 : 850+bleed-2-width, middle=top+height/2;
      const label=side<0 ? 'Scroll to units on the left' : 'Scroll to units on the right';
      rackArrows += `<g data-part="rack-scroll" data-direction="${side}" role="button" tabindex="0" aria-label="${label}" data-tooltip="${label}"><rect x="${left}" y="${top}" width="${width}" height="${height}" rx="4" fill="var(--scene-body, #edf2f6)" fill-opacity=".85" stroke="var(--scene-border, #96abba)" stroke-width=".8"/><path d="M${left+width/2-side*4} ${middle-30} L${left+width/2+side*4} ${middle} L${left+width/2-side*4} ${middle+30}" fill="none" stroke="var(--scene-text, #304c65)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></g>`;
    }
  }
  let attachedTipEdge='';
  const shapedTip = (position, color, part, hairLength=s.hair_lengths?.[gate] ?? 15, route=null) => {
    const hairScale = tipScale;
    const along = distance => route ? route.getPointAtLength(distance) : distance <= length ? tube.getPointAtLength(distance)
      : {x:260,y:Math.min(nozzleExit,449+distance-length)};
    // Keep the cone at toolhead scale even along the compressed Bowden.
    const center=route ? position : progress(position)*length;
    const base = along(Math.max(0,center-tipScale)), nose = along(center+tipScale);
    const angle = Math.atan2(nose.y-base.y,nose.x-base.x), dx = -Math.sin(angle)*3.5, dy = Math.cos(angle)*3.5;
    // Overlap the cone slightly, then taper the surviving hair.
    const hairStart = center+tipScale;
    const hairLimit=route ? route.getTotalLength() : progress(end-(s.filament?.residual_mm || 0))*length;
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
  for (const branch of parkedTips) {
    const route=document.createElementNS('http://www.w3.org/2000/svg','path');
    route.setAttribute('d',virtual.branchPath(branch.x));
    svg=svg.replace(`<!--parked-tip-${branch.gate}-->`,shapedTip(virtual.parkDistance,
      s.gate_colors[branch.gate] || DEFAULT_COLOR,'parked-tip',s.hair_lengths?.[branch.gate] ?? 15,route));
  }
  svg += `<defs><linearGradient id="bowden-fade-gradient" gradientUnits="userSpaceOnUse" x1="0" x2="0" y1="${470+mmuOffset}" y2="${484+mmuOffset}"><stop stop-color="#808080"/><stop offset="1" stop-color="black"/></linearGradient><mask id="bowden-end-fade" maskUnits="userSpaceOnUse" x="0" y="0" width="850" height="700"><rect width="850" height="700" fill="white"/><rect x="${virtual ? virtual.branches[0].x-20 : 600}" y="${470+mmuOffset}" width="${virtual ? virtual.branches.at(-1).x-virtual.branches[0].x+40 : 40}" height="230" fill="${virtual ? 'black' : 'url(#bowden-fade-gradient)'}"/></mask></defs>`;
  const extruderGearY=tube.getPointAtLength(progress(s.layout.extruder_gears ?? entry) * length).y;
  if (syncForDisplay(e, s, Boolean(playing) || !live || paused)) {
    const firstBranch=virtual?.branches[0];
    const syncY=435+hardwareOffset;
    const syncRight=virtual
      ? firstBranch.x-(virtual.active.gate === firstBranch.gate ? 38 : 15)
      : 575;
    const syncIcon=(x,y,scale=1)=>`<g transform="translate(${x} ${y}) scale(${scale}) rotate(-35)" fill="none" stroke="#53b9b0" stroke-width="2.2"><rect x="-12" y="-5" width="15" height="10" rx="5"/><rect x="-3" y="-5" width="15" height="10" rx="5"/></g>`;
    svg += `<g id="extruder-synced-indicator" aria-label="Extruder and MMU gears synced">${syncIcon(213,extruderGearY,.7)}</g>`;
    svg += `<g id="synced-indicator" aria-label="Extruder and MMU gears synced">${syncIcon(syncRight-18,syncY)}<text x="${syncRight-18}" y="${syncY+21}" text-anchor="middle" font-size="10" fill="var(--scene-text, #304c65)">Synced</text></g>`;
  } else if (!virtual) {
    const gripState=e.kind === 'movement' && (playing || !live || paused) ? e.before : s;
    if (gripState?.mmu_gripped === true)
      svg += `<text id="grip-indicator" x="552" y="${435+hardwareOffset}" dominant-baseline="middle" text-anchor="middle" font-size="10" fill="var(--scene-text, #304c65)">Gripped</text>`;
  }
  // Nozzle-relative reference guides, matching the documentation diagrams.
  const references = toolheadReferences(s).map(ref => ({...ref,
    y: tube.getPointAtLength(progress(ref.position) * length).y})).sort((a, b) => a.y - b.y);
  let previousLabelY = -Infinity;
  const mm = round1;
  for (const ref of references) {
    if (ref.label === 'Blade' && ref.y < extruderGearY) {
      svg += `<g class="toolhead-reference"><text ${dimensionLink('Blade')} x="174" y="${ref.y}" text-anchor="end" dominant-baseline="middle" font-size="10" fill="var(--scene-muted, #6e8190)"><tspan class="reference-title">Blade · ${mm(ref.distance)}mm</tspan></text></g>`;
      continue;
    }
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
    const suffix = sensorName(name).match(/^mmu_.*_(\d+)$/);
    if (suffix && Number(suffix[1]) !== gate && !virtual) continue;
    if (d.position === null && !isTouchSensor(name)) continue;
    const pt = sensorPoint(name, d);
    const short = name.split(':').at(-1);
    const sensorLabel = isTouchSensor(name) ? 'touch' : /^mmu_entry(?:_\d+)?$/.test(short) ? 'entry' : /^mmu_exit(?:_\d+)?$/.test(short) ? 'exit' : short === 'mmu_shared_exit' ? 'shared exit' : short;
    // Leave a one-unit gap outside the tube and the five-unit sensor radius.
    const sensorX=pt.x+16;
    const sensorClass=virtual && ['entry','exit'].includes(sensorLabel) && pt.x!==virtual.active.x ? ' virtual-gate-sensor' : '';
    const showLabel=!virtual || !['entry','exit'].includes(sensorLabel) || pt.x===virtual.branches.at(-1).x;
    svg += `<g class="sensor-marker${sensorClass}${d.state === null ? ' sensor-disabled' : ''}" data-sensor="${escape(name)}">${showLabel ? `<text x="${isTouchSensor(name) || pt.x > 400 ? pt.x + 29 : pt.x - 29}" y="${sensorLabel === 'entry' ? pt.y : pt.y + 4}" dominant-baseline="${sensorLabel === 'entry' ? 'central' : 'auto'}" text-anchor="${isTouchSensor(name) || pt.x > 400 ? "start" : "end"}" font-size="10" fill="var(--scene-text, #456078)">${escape(sensorLabel)}</text>` : ''}<circle cx="${sensorX}" cy="${pt.y}" r="5" fill="${d.state === true ? "#399b64" : d.state === false ? "#fff" : "#9ba5ae"}" stroke="#698394"/></g>`;
    svg += homingRing(name,sensorX,pt.y);
    if (sensorPulse?.sensor === name) {
      const elapsed = (animationNow() - sensorPulse.start) / 650;
      if (elapsed >= 0 && elapsed < 1) {
        const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
        svg += `<circle data-part="sensor-pulse" cx="${sensorX}" cy="${pt.y}" r="${reduced ? 10 : 7 + elapsed * 15}" fill="none" stroke="${sensorPulse.reverse ? 'var(--sensor-clear-pulse)' : '#48c982'}" stroke-width="2.5" opacity="${1-elapsed}"/>`;
      }
    }
  }
  for (const [x, y, type, gearGate] of [
    ...(virtual ? [[virtual.active.x,435+hardwareOffset,"gear",gate]] : [[620,435+hardwareOffset,"gear",gate]]),
    [260, extruderGearY, "extruder"],
  ]) {
    const names = s.drive_motors?.[type] || Object.keys(s.motors).filter(n => n.includes(type));
    // Coupled motors share travel; do not add their distances or inactive gates.
    const distance = names.length ? (s.motors[names[0]] || 0) : 0;
    // Both gear pairs use the toolhead scale so equal travel gives equal rotation.
    const pixelsPerMm = .27 * length / (end-entry);
    const angle = gearAngle(virtual && type==='gear' && gearGate!==gate ? 0 : distance, pixelsPerMm, 14.25);
    const gripState = e.kind === 'movement' && (playing || !live || paused) ? e.before : s;
    const gripSpacing = type === 'gear' && !virtual && gripState?.mmu_gripped === false ? 26.25 : 17.25;
    const spacing = type === 'gear' && !virtual ? animatedGearSpacing(gripSpacing) : gripSpacing;
    for (const side of [-1, 1])
      svg += `<g data-part="${type}-wheel" opacity="${virtual && type==='gear' ? branchOpacity : 1}" transform="translate(${x + side * spacing} ${y}) rotate(${(type === "extruder" ? -1 : 1) * side * angle}) scale(.75)"><circle r="19" fill="var(--scene-gear, #d8e4ed)" stroke="var(--scene-border, #6d899f)"/><path d="M-14 0 H14 M0 -14 V14" stroke="var(--scene-spoke, #526f86)"/><circle class="gear-center" r="3" fill="var(--scene-spoke, #526f86)"/></g>`;
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
      svg += renderBuffer(s,d,unitSensors.filter(([sensor])=>isBufferSensor(sensor)),sensorFill,{homingRing,pulse:sensorPulse,now:animationNow});
    }
  }
  const excess = s.bowden_unabsorbed_mm || 0;
  if (Number.isFinite(excess) && excess !== 0) svg += `<text id="bowden-excess" x="640" y="${bowdenLengthLabel(s).includes('Uncalibrated') ? 134 : 118}" font-size="10" fill="var(--scene-text, #304c65)"><tspan x="640">Excess filament</tspan><tspan x="640" dy="14">${excessFilamentLabel(excess)}</tspan></text>`;
  const residual = s.filament?.residual_mm || 0;
  const fragment = s.filament?.fragment_mm || 0;
  const retainedColor = s.nozzle_color || s.filament?.retained_color || DEFAULT_COLOR;
  const residualTop = tube.getPointAtLength(progress(end-residual)*length).y;
  const nozzleZero = tube.getPointAtLength(progress(end)*length).y;
  if (s.retained_segment && !s.retained_segment.residual_only) {
    const segment = s.retained_segment;
    const top = tube.getPointAtLength(progress(segment.start)*length).y;
    const coneTip = segment.end;
    const bottom = tube.getPointAtLength(Math.max(progress(segment.start)*length,progress(segment.end)*length-(segment.tip_shape === 'cone' ? tipScale : 0))).y;
    const cutLength = Math.max(0, segment.end-segment.start);
    svg += `<path id="retained-fragment" d="M260 ${top} L260 ${bottom}" fill="none" stroke="#${segment.color}" stroke-width="7"/>`;
    if (cutLength > .01) {
      const centerY=tube.getPointAtLength(progress((segment.start+segment.end)/2)*length).y;
      const rows=Array.from({length:281},(_,i)=>200+i)
        .filter(y=>Math.abs(y-452)>=20);
      const labelY=rows.reduce((best,y)=>Math.abs(y-centerY)<Math.abs(best-centerY) ? y : best,rows[0] ?? 470);
      svg += `<g id="cut-fragment-label"><text id="cut-fragment-length" x="196" y="${labelY}" text-anchor="end" dominant-baseline="middle" font-size="9" fill="var(--scene-muted, #6e8190)">Cut fragment · ${mm(cutLength)}mm</text></g>`;
    }
    if (segment.tip_shape === 'cone') {
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
    svg += `<path id="residual-incoming" d="M260 ${residualTop} L260 ${frontY}" fill="none" stroke="#${s.filament?.incoming_color || DEFAULT_COLOR}" stroke-width="7"/>`;
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
    const funnelColor=tip >= end-.01 ? s.filament?.incoming_color || DEFAULT_COLOR
      : s.nozzle_outlet_color || purgeOutletColor(s,s.extruded_mm || 0);
    svg += `<path id="nozzle-funnel" d="${nozzleFunnel}" fill="${flooding ? (s.nozzle_fill_from ? `#${s.nozzle_fill_from}` : 'var(--scene-tube, #edf4f8)') : `#${funnelColor}`}"/>`;
  }
  const plan=s.purge_transition;
  const meltingTip=tip >= end-.001 && plan && s.extruded_mm < plan.start_mm+plan.retained_mm+plan.mix_mm;
  if (spoolFill(s,gate)>0 && (s.tip_shapes?.[gate] === 'cone' || meltingTip || s.cone_edge_opacity > 0)) {
    svg += `<defs><clipPath id="tip-nozzle-clip"><rect x="0" y="0" width="850" height="${nozzleZero}"/><rect x="${virtual ? virtual.branches[0].x-20 : 500}" y="${nozzleZero}" width="${virtual ? 850-virtual.branches[0].x+20 : 350}" height="${700-nozzleZero}"/>${flooding ? '' : `<path d="${nozzleFunnel}"/>`}</clipPath></defs><g opacity="${shapedTipOpacity}" mask="url(#bowden-end-fade)" clip-path="url(#tip-nozzle-clip)"><g mask="url(#outside-mmu)">${shapedTip(tip,s.filament?.incoming_color || DEFAULT_COLOR,'attached-tip')}</g></g>`;
  }
  if (tip >= end-.01 || Number.isFinite(s.purge_transition?.nozzle_arrival_mm)) {
    const fillHeight=(nozzleExit-nozzleZero)*(s.nozzle_fill_fraction ?? 1);
    const outletColor=purgeOutletColor(s,s.extruded_mm || 0);
    svg += `<defs><linearGradient id="nozzle-fill-gradient" gradientUnits="userSpaceOnUse" x1="260" x2="260" y1="${nozzleZero}" y2="${nozzleZero+Math.max(.001,fillHeight)}"><stop stop-color="#${s.filament?.incoming_color || DEFAULT_COLOR}"/><stop offset="1" stop-color="#${outletColor}"/></linearGradient><clipPath id="nozzle-fill-clip"><rect x="250" y="${nozzleZero}" width="20" height="${fillHeight}"/></clipPath></defs><path id="nozzle-fill-front" d="${nozzleFunnel}" fill="url(#nozzle-fill-gradient)" clip-path="url(#nozzle-fill-clip)"/>`;
  }
  // Keep the dissolving tip edge above the flood, clipped to the complete nozzle bore.
  if (attachedTipEdge) svg += `<defs><clipPath id="tip-edge-clip"><rect x="0" y="0" width="850" height="${nozzleZero}"/><rect x="${virtual ? virtual.branches[0].x-20 : 500}" y="${nozzleZero}" width="${virtual ? 850-virtual.branches[0].x+20 : 350}" height="${700-nozzleZero}"/><path d="${nozzleFunnel}"/></clipPath></defs><g opacity="${shapedTipOpacity}" mask="url(#bowden-end-fade)" clip-path="url(#tip-edge-clip)">${attachedTipEdge}</g>`;
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
    svg += `<defs><clipPath id="purge-screen"><rect x="210" y="435" width="150" height="70"/></clipPath></defs><g clip-path="url(#purge-screen)"><g id="purge-pile" transform="translate(0,${drop*75})" aria-label="Extruded filament">${waste}</g></g><text x="217" y="491" text-anchor="end" font-size="10" fill="var(--scene-muted, #6e8190)">${purgeStrandLength(s.extruded_mm, nozzleDiameter).toFixed(1)} mm strand</text>`;
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
  // Away from the exit, everything attached to the MMU side fades: Bowden, branches, gears, sensors, tips.
  // The mask covers only the scene above the unit enclosures (y<528) and the MMU side (x>=470, clear of the toolhead labels),
  // so the rack, the toolhead and the top of the Bowden stay put.
  const reachCut=240+bowdenReach*328;
  svg = `<defs><linearGradient id="bowden-reach-gradient" gradientUnits="userSpaceOnUse" x1="0" x2="0" y1="${reachCut-40}" y2="${reachCut}"><stop stop-color="white"/><stop offset="1" stop-color="black"/></linearGradient><mask id="bowden-reach-fade" maskUnits="userSpaceOnUse" x="-1000" y="-100" width="3000" height="4000"><rect x="-1000" y="-100" width="3000" height="4000" fill="white"/><rect x="470" y="0" width="1530" height="528" fill="url(#bowden-reach-gradient)"/></mask></defs><g mask="url(#bowden-reach-fade)">${svg}</g>`;
  $("scene").innerHTML = rackFade ? `${rackFadeMask(rackFade)}<g mask="url(#rack-edge-fade)">${svg}</g>${rackArrows}` : svg+rackArrows;
  for (const [index] of spoolRenderers) {
    if (!spoolRequests.has(index)) spoolRenderers.delete(index);
  }
  for (const host of $('scene').querySelectorAll('[data-part="espooler"]')) {
    const index=Number(host.dataset.gate);
    let renderer=spoolRenderers.get(index);
    if (!renderer) { renderer=new SpoolRenderer(host); spoolRenderers.set(index,renderer); }
    renderer.update(spoolRequests.get(index));
    renderer.mount(host);
  }
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
      el.textContent = sensorName(name);
      const enabled = d.enabled ?? d.state !== null;
      const atLatest = viewMode==='live' || (live && !paused && !playing && played >= events.at(-1)?.seq);
      el.disabled = offline || !atLatest || busy || !isSimulator();
      el.setAttribute('aria-pressed', String(enabled));
      el.setAttribute('aria-label', `${name}: ${enabled ? 'enabled' : 'disabled'}. Click to ${enabled ? 'disable' : 'enable'}`);
      el.dataset.tooltip = offline ? 'Recorded sensor state' : atLatest ? `${enabled ? 'Disable' : 'Enable'} ${name}` : 'Jump to live to change sensors';
      el.onclick = () => action(async () => {
        await sim.sensor(name, !enabled);
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
          ? round1(value) : value));
      return el;
    }),
  );
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
    if (!offline && socket?.readyState===1) await rpc('printer.objects.subscribe',{objects:{}}).catch(()=>{});
    await telemetryStream.stop();
    modeChosen=true; viewMode='playback'; offline=true; subscriptionGeneration++; liveTarget=null; liveFrom=null;
    pending.forEach(p=>p.reject(Error('Switched to offline recording'))); pending.clear();
    sourceReplay=replay; sourceRecords=records;
    rawEvents=replay.events; events=visibleSteps(rawEvents); session=replay.session;
    live=false; paused=true; cursor=0; playing=null; sensorPulse=null; pauseAfterStep=false; displayed=null;
    $('recording-name').textContent=file.name;
    $('connection').textContent='Offline recording';
    sourceMode(); draw(); renderTrace();
    message(replay.checkpoint ? 'Loaded checkpoint: earlier motion and inferred geometry are unavailable.' : 'Recording loaded. Use Play or step through the trace.');
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
      $('unit').value=String(unit); await refresh();
    }
  }
  return sourceRecords.length>7000;
}, error=>{
  $('connection').textContent='Telemetry stream unavailable';
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
function animate() {
  updateLiveIndicator();
  const now=animationNow();
  const spoolRunning=Boolean(playing) || (live && !paused);
  if (spoolSession !== session) { spoolAngles.clear(); spoolLastFrame=null; spoolArrowVisibility.clear(); spoolDirections.clear(); spoolSession=session; }
  let spoolFading=false;
  const fadeStep=spoolLastFrame === null ? 0 : Math.min(100,now-spoolLastFrame)/450;
  for (const [gate,operation] of Object.entries(displayed?.espoolers || {})) {
    const index=Number(gate), active=espoolerActive(operation);
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
      if (!espoolerActive(operation)) continue;
      const speed=(40+100*Math.min(1,operation.output_power))*(operation.direction==='rewind'?-1:1);
      spoolAngles.set(Number(gate),((spoolAngles.get(Number(gate)) || 0)+seconds*speed)%360);
      spoolRotating=true;
    }
  }
  spoolLastFrame=now;
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
  returnRackWhenIdle();
  if (sceneDirty) {
    sceneDirty=false;
    renderScene();
  } else if (spoolFading || spoolRotating) {
    updateSpoolMotion();
  }
  requestAnimationFrame(animate);
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
    $('scene').replaceChildren(); $('mode').textContent='Live view · Connecting…';
    connect();
  } catch(error) { message(error.message,true); }
};
connect();
requestAnimationFrame(animate);
