// Residual contact melts only the immersed hair; retain at least 5mm.
export function shortenedHair(length, tip, residualTop) {
  return Math.max(5,Math.min(length,residualTop-tip-1));
}

/* Copyright (C) 2022-2026 Paul Morgan. GPLv3. */
export function interpolate(event, fraction) {
  const a = event.before, b = event.state;
  if (!a || fraction >= 1) return event.settled_state || b;
  const motorMode = event.telemetry?.start?.motor_mode || event.motor_mode;
  const synced = ['gear+extruder', 'extruder+gear', 'synced'].includes(motorMode);
  const hasMotorMode = event.kind === 'movement' && Boolean(motorMode);
  if (fraction <= 0) return hasMotorMode ? {...a, synced} : a;
  if (event.kind === 'gate_changed') {
    const selection=Math.max(0,Math.min(1,(fraction-.25)/.5));
    const eased = selection*selection*(3-2*selection);
    const state = structuredClone(fraction < .5 ? a : b);
    const from = a.gate >= 0 ? a.gate : b.gate;
    const to = b.gate >= 0 ? b.gate : a.gate;
    state.gate_focus = from+(to-from)*eased;
    state.gate_path_opacity = Math.abs(2*selection-1);
    const retract=fraction < .25 ? fraction/.25 : fraction > .75 ? (1-fraction)/.25 : 1;
    state.gate_retraction=retract*retract*(3-2*retract);
    return state;
  }
  if (event.kind === 'cut') return fraction < .4 ? a : b;
  const mix = (x,y) => typeof x === 'number' && typeof y === 'number' ? x+(y-x)*fraction : x;
  const s = structuredClone(a);
  if (hasMotorMode) s.synced = synced;
  s.tip_shapes = b.tip_shapes || a.tip_shapes;
  if (a.tip_shapes?.[b.gate] === 'square' && b.tip_shapes?.[b.gate] === 'cone'
      && b.tips[b.gate] < a.tips[b.gate]) s.cone_edge_opacity=fraction;
  // Older captures flagged the successful reverse-home endpoint as a clear-sensor conflict.
  const reverseHome = event.homing && event.homed && (event.distance ?? event.requested) < 0;
  s.sensor_conflicts = (b.sensor_conflicts || []).filter(warning =>
    !reverseHome || warning !== `${event.endstop}: movement past a clear sensor`);
  for (const key of ['tips','tails']) s[key] = a[key].map((x,i)=>mix(x,b[key][i]));
  if (event.raw_before && event.raw_state && !event.homing_anchor) {
    // Reach a physical stop at commanded pace, then hold while gears keep feeding.
    s.tips = a.tips.map((tip, i) => {
      const delta = i === b.gate && event.filament_travel_mm != null ? event.filament_travel_mm
        : event.raw_state.tips[i] - event.raw_before.tips[i];
      const takeup=i === b.gate ? event.buffer_takeup_mm || 0 : 0;
      const candidate = tip + (takeup && delta < 0 ? Math.min(0,delta*fraction+takeup) : delta*fraction);
      return delta >= 0 ? Math.min(candidate, b.tips[i]) : Math.max(candidate, b.tips[i]);
    });
  }
  const gate=b.gate;
  if (a.tip_shapes?.[gate] === 'cone' && b.tip_shapes?.[gate] === 'square'
      && b.tips[gate] >= b.layout.nozzle-.001) {
    s.tip_shapes=[...s.tip_shapes];
    s.tip_shapes[gate]='cone';
  }
  s.hair_lengths=(b.hair_lengths || a.hair_lengths || []).map((length,i)=>
    a.tip_shapes?.[i] === 'cone' && s.tips[i] > a.tips[i] && s.tips[i] >= s.layout.extruder_gears
      ? shortenedHair(a.hair_lengths?.[i] ?? 15,s.tips[i],s.layout.nozzle-s.filament.residual_mm) : length);
  if (a.retained_segment) {
    const segment = a.retained_segment;
    const gate = b.gate;
    const push = Math.max(0, s.tips[gate] - Math.max(a.tips[gate], segment.start));
    const nozzle = b.layout.nozzle;
    s.retained_segment = {...segment, start: Math.min(nozzle, segment.start+push),
      end: Math.min(nozzle, segment.end+push)};
    if (push > 0 && segment.tip_shape === 'cone')
      s.retained_segment.hair_length=shortenedHair(segment.hair_length ?? 15,segment.end+push,nozzle-s.filament.residual_mm);
    if (s.retained_segment.start >= s.retained_segment.end) s.retained_segment = null;
  }
  s.extruded_mm = a.extruded_mm || 0;
  const emitted = (b.extruded_mm || 0) - s.extruded_mm;
  if (emitted > 0) {
    const gate = b.gate;
    const segment = a.retained_segment;
    const approach = event.purge_approach_mm ?? (segment
      ? Math.max(0,segment.start-a.tips[gate]) + Math.max(0,(b.layout.nozzle-(b.filament?.residual_mm || 0))-segment.end)
      : Math.max(0,(b.layout?.nozzle ?? a.tips[gate])-a.tips[gate]));
    s.extruded_mm += Math.max(0, Math.min(emitted, (approach + emitted) * fraction - approach));
  }
  s.purge_base_mm = b.purge_base_mm ?? 0;
  if ((b.purge_base_mm || 0) > (a.purge_base_mm || 0)) {
    s.purge_base_mm = a.purge_base_mm || 0;
    s.purge_drop = fraction;
  }
  s.purge_transition = b.purge_transition || a.purge_transition;
  s.nozzle_color = purgeColor(s, s.extruded_mm);
  s.nozzle_outlet_color = purgeOutletColor(s,s.extruded_mm);
  if ((emitted <= 0 || a.nozzle_outlet_color === b.nozzle_outlet_color) && a.nozzle_outlet_color && b.nozzle_outlet_color) {
    s.nozzle_outlet_color=purgeColor({purge_transition:{start_mm:0,retained_mm:1,mix_mm:1,
      old_color:a.nozzle_outlet_color,new_color:b.nozzle_outlet_color}},fraction);
  }
  for (const [name,value] of Object.entries(b.motors)) s.motors[name]=mix(a.motors[name]||0,value);
  const contact=event.buffer_contact_fraction;
  const bufferFraction=contact == null ? fraction : Math.max(0,Math.min(1,(fraction-contact)/Math.max(1e-9,1-contact)));
  const bufferMix=(x,y)=>typeof x === 'number' && typeof y === 'number' ? x+(y-x)*bufferFraction : x;
  for (const [name,device] of Object.entries(a.devices)) {
    for (const [key,value] of Object.entries(device)) s.devices[name][key]=(name.endsWith(':buffer') ? bufferMix : mix)(value,b.devices[name]?.[key]);
  }
  s.bowden_unabsorbed_mm = mix(a.bowden_unabsorbed_mm || 0,b.bowden_unabsorbed_mm || 0);
  for (const [name, device] of Object.entries(s.devices)) {
    if (!name.endsWith(':buffer') || !Number.isFinite(device.displacement) || !(device.maximum > 0)) continue;
    if (event.buffer_release && name === `${s.unit}:buffer`) {
      const {approach_mm,travel_mm,from,to}=event.buffer_release;
      const released=Math.max(0,travel_mm*fraction-approach_mm);
      device.displacement=from+Math.sign(to-from)*Math.min(Math.abs(to-from),released);
    }
    device.travel = Math.max(0,Math.min(device.maximum,device.displacement));
    device.excess = device.displacement-device.travel;
    if (!device.proportional) device.value = Math.max(-1,Math.min(1,(2*device.travel-device.maximum)/(device.range || device.maximum)));
    if (name === `${s.unit}:buffer`) s.bowden_unabsorbed_mm = device.excess;
  }
  const occupancy = (state, name, position) => {
    const suffix=name.match(/_(\d+)$/), gate=suffix?Number(suffix[1]):state.gate;
    return gate>=0 && state.tips[gate]>=position && (state.tails[gate]===null || state.tails[gate]<=position);
  };
  for (const [name,sensor] of Object.entries(s.sensors)) {
    const end=b.sensors[name];
    if (!end || sensor.state===null || end.state===null || sensor.position===null) continue;
    if (/filament_(tension|compression)$/.test(name)) {
      const geometry=Object.values(a.buffers).find(g=>g.gates.includes(a.gate));
      const next=Object.values(b.buffers).find(g=>g.gates.includes(a.gate));
      if (geometry && next) {
        const travel=s.devices[`${s.unit}:buffer`]?.travel ?? Math.max(0,Math.min(geometry.maxrange,bufferMix(geometry.travel[a.gate]+(geometry.excess?.[a.gate] || 0),next.travel[a.gate]+(next.excess?.[a.gate] || 0))));
        const threshold=name.endsWith('tension')?v=>v<=(geometry.tension_travel || 0):v=>v>=geometry.compression_travel;
        if (sensor.state !== end.state && threshold(geometry.travel[a.gate])===sensor.state && threshold(next.travel[a.gate])===end.state) sensor.state=threshold(travel);
      }
    } else if (!name.includes('encoder') && occupancy(a,name,sensor.position)===sensor.state && occupancy(b,name,end.position)===end.state) {
      sensor.state=occupancy(s,name,sensor.position);
    }
  }
  // Recorded buffer sensors bound inferred relaxation, including between frames.
  for (const [name,device] of Object.entries(s.devices)) {
    if (!name.endsWith(':buffer')) continue;
    const unit=name.slice(0,-7);
    const triggered=suffix=>Object.entries(s.sensors).some(([n,v])=>
      n.startsWith(unit+':') && n.endsWith(suffix) && v.enabled !== false && v.state === true);
    const piston=bufferPiston(device,triggered('filament_tension'),triggered('filament_compression'));
    device.travel=piston.position;
    device.displacement=piston.position+piston.excess;
    device.excess=piston.excess;
  }
  return s;
}
// All positions use the same drawing-path coordinates. Sensor readings win last.
export function constrainFilament(tip, tail, sensors, fragments = [], reported = []) {
  let lower = 0, upper = Infinity;
  const warnings = [...reported];
  for (const sensor of sensors) {
    const {position,state,name} = sensor;
    if (!Number.isFinite(position) || sensor.enabled === false || typeof state !== 'boolean') continue;
    const fragment = fragments.some(part => part.start <= position && part.end >= position);
    if (!state && (tail === null || tail <= position)) {
      upper = Math.min(upper, position);
      if (tip > position || fragment) warnings.push(`${name}: estimated tip is beyond a clear sensor`);
    } else if (state && !fragment && (tail === null || tail <= position)) {
      lower = Math.max(lower,position);
    }
  }
  if (lower > upper) warnings.push('Presence sensors disagree');
  const bounded = value => Math.max(0,Math.min(upper,Math.max(lower,value)));
  return {tip:bounded(tip), warnings:[...new Set(warnings)]};
}

// Approximate each discrete move from rest; recorded distances remain authoritative.
function motionProfile(event) {
  const distance=Math.abs(event.distance ?? 0), speed=event.speed, acceleration=event.acceleration;
  if (event.kind !== 'movement' || !Number.isFinite(distance) || distance <= 0
      || !Number.isFinite(speed) || speed <= 0 || !Number.isFinite(acceleration) || acceleration <= 0) return null;
  const stopAtSensor=Boolean(event.homing && event.homed);
  const ramps=stopAtSensor ? 1 : 2;
  const peak=Math.min(speed,Math.sqrt(2*acceleration*distance/ramps));
  const rampTime=peak/acceleration, rampDistance=peak*rampTime/2;
  const cruiseTime=Math.max(0,(distance-ramps*rampDistance)/peak);
  return {distance,acceleration,peak,rampTime,rampDistance,cruiseTime,
    seconds:ramps*rampTime+cruiseTime};
}

export function motionFraction(event, timeFraction) {
  if (timeFraction <= 0) return 0;
  if (timeFraction >= 1) return 1;
  const profile=motionProfile(event);
  if (!profile) return timeFraction;
  const {distance,acceleration,peak,rampTime,rampDistance,cruiseTime,seconds}=profile;
  const time=timeFraction*seconds;
  const travelled=time < rampTime ? acceleration*time*time/2
    : time < rampTime+cruiseTime ? rampDistance+peak*(time-rampTime)
    : distance-acceleration*(seconds-time)**2/2;
  return Math.max(0,Math.min(1,travelled/distance));
}

export function duration(event, compressBowden = false) {
  if (event.kind === 'gate_changed') return 2200;
  if (event.kind === 'cut' || event.kind === 'release') return 650;
  if (event.kind !== 'movement') return 0;
  const travel=Math.abs(event.distance ?? Math.max(0,...Object.values(event.motors||{}).map(Math.abs)));
  // Use resolved feed speed, not console pacing or time spent waiting for sensors.
  let seconds = motionProfile(event)?.seconds
    ?? (event.speed > 0 && travel > 0 ? travel/event.speed : event.duration || travel/100);
  if (compressBowden && event.fast_bowden && !event.homing && event.before && travel > 0) {
    const a=event.before, b=event.state, entry=a.layout.extruder_entry ?? 700;
    const extruderSpan=(a.layout.toolhead ?? 760)+30-entry;
    // Match the diagram's 56% transport / 27% extruder path proportions.
    // Only compress travel in the Bowden section; preserve any end sections.
    if (entry > 0 && extruderSpan > 0) {
      const ratio=Math.min(1,(0.56/entry)/(0.27/extruderSpan));
      const start=a.tips[a.gate], end=b.tips[a.gate];
      const overlap=Math.max(0,Math.min(entry,Math.max(start,end))-Math.max(0,Math.min(start,end)));
      seconds *= (travel-Math.min(travel,overlap)*(1-ratio))/travel;
    }
  }
  // Half-speed visual playback for moves that deposit filament at the nozzle.
  // Recorded feed rates and the running simulator remain unchanged.
  if ((event.state?.extruded_mm || 0) > (event.before?.extruded_mm || 0)) seconds *= 2;
  // Let the detached purge fall visibly even when the releasing retract is brief.
  const releasingPurge = (event.state?.purge_base_mm || 0) > (event.before?.purge_base_mm || 0);
  return Math.max(releasingPurge ? 0.65 : 0.12,seconds)*1000;
}

// A selected movement describes its motor mode; idle live view describes now.
export function syncForDisplay(event, state, inspectingMovement = false) {
  const mode = event?.telemetry?.start?.motor_mode || event?.motor_mode;
  if (inspectingMovement && event?.kind === 'movement' && mode)
    return ['gear+extruder', 'extruder+gear', 'synced'].includes(mode);
  return Boolean(state.synced);
}

export function gearAngle(distance, pixelsPerMm, radius = 19) {
  return distance * pixelsPerMm / radius * 180 / Math.PI;
}


// Session-local command history preserves the unfinished input when returning to the end.
export class CommandHistory {
  constructor() { this.entries = []; this.index = 0; this.draft = ''; }
  add(command) {
    if (command && this.entries.at(-1) !== command) this.entries.push(command);
    if (this.entries.length > 200) this.entries.shift();
    this.index = this.entries.length;
    this.draft = '';
  }
  navigate(direction, input) {
    if (!this.entries.length) return input;
    if (this.index === this.entries.length) this.draft = input;
    this.index = Math.max(0, Math.min(this.entries.length, this.index + direction));
    return this.index === this.entries.length ? this.draft : this.entries[this.index];
  }
}

export function traceText(event) {
  let type = {state:'state changed', gate_changed:'gate changed'}[event.kind] || event.kind;
  if (event.kind === 'movement') {
    const source = event.source || event.telemetry?.start?.source;
    type = event.homing ? 'homing move' : source === 'mmu_gcode' ? 'G1' : 'move';
  }
  const prefix = `${event.seq} · ${event.time.toFixed(2)}s · ${type} · `;
  if (event.kind === 'config') return prefix + (event.changes || []).map(c => `${configurationLabel(c.name)}: ${JSON.stringify(c.after)}`).join(' · ');
  if (event.kind !== 'movement') return prefix + event.label;
  const start = event.telemetry?.start;
  let motor = start?.motor_mode || event.motor_mode;
  if (!motor) {
    const names = Object.keys(event.motors || {}).filter(n => event.motors[n] !== 0);
    const gear = names.some(n => /gear/i.test(n));
    const extruder = names.some(n => /extruder/i.test(n));
    motor = gear && extruder ? 'gear+extruder' : gear ? 'gear' : extruder ? 'extruder' : 'move';
  }
  const name = motor === 'synced' ? 'SYNCED GEAR+EXTRUDER' : motor.toUpperCase();
  const number = v => Number.isFinite(v) ? v.toFixed(1) : '?';
  const distance = event.distance ?? event.requested;
  let text;
  if (event.homing) {
    const result = event.homed === true ? 'Homed' : event.homed === false ? 'Did not home' : 'Homing result unknown';
    text = `${name}: ${result} after ${number(event.distance)}mm`;
    if (event.requested != null) text += ` | requested: dist=${number(event.requested)}`;
  } else text = `${name}: dist=${number(distance)}mm`;
  if (Number.isFinite(event.speed)) text += ` · speed=${number(event.speed)}`;
  if (Number.isFinite(event.acceleration)) text += ` · accel=${number(event.acceleration)}`;
  if (event.homing && event.endstop) text += ` · endstop=${event.endstop}`;
  if (event.error) text += ` · error: ${event.error}`;
  return prefix + text;
}

export function homingPulse(event) {
  if (event.kind !== 'movement' || !event.homing || event.homed !== true) return null;
  const sensors = event.state?.sensors || {};
  const target = event.endstop;
  const short = name => name.split(':').at(-1).replace(/^mmu_/, '');
  const candidates = Object.keys(sensors).filter(name => target &&
    (name === target || (!target.includes(':') && short(name) === short(target))) &&
    (!event.state.active_sensors || event.state.active_sensors.includes(name)));
  if (candidates.length !== 1 || sensors[candidates[0]].position == null) return null;
  return {sensor:candidates[0], reverse:(event.requested ?? event.distance ?? 0) < 0};
}

export function bladeStroke(event, fraction, kind) {
  if (event?.cutter === kind && event.kind === 'release') return 1-Math.max(0,Math.min(1,fraction));
  if (event?.cutter === kind && event.kind === 'cut' && event.state?.cutter_pressed) return Math.min(1,Math.max(0,fraction)/.4);
  if (event?.kind !== 'cut' || event.cutter !== kind) return 0;
  const t = Math.max(0, Math.min(1, fraction));
  return t < .4 ? t / .4 : t <= .6 ? 1 : (1 - t) / .4;
}

export function configurationLabel(name) {
  if (name.startsWith('macros.macro:')) return name.slice(13).replace(':', '.');
  if (name.startsWith('parameters.')) return name.replace(/^parameters\.(\d+)\./, 'Unit $1 · ');
  if (name.startsWith('sensors.')) return name.slice(8) + ' enabled';
  return name;
}

export function bowdenLengthLabel(state) {
  const length = state.configuration?.calibration?.[state.unit]?.bowden_lengths?.[state.gate];
  return state.gate >= 0 && Number.isFinite(length) && length > 0
    ? `Bowden · ${Number(length.toFixed(1))}mm` : 'Bowden · Uncalibrated length';
}

export function toolheadReferences(state) {
  const {nozzle, extruder_gears: gears} = state.layout || {};
  if (!Number.isFinite(nozzle)) return [];
  const refs = [{label: 'Nozzle', position: nozzle}];
  if (Number.isFinite(gears)) refs.push({label: 'Extruder entry', position: gears});
  for (const cutter of state.cutters || []) {
    if (cutter.kind === 'toolhead') refs.push({label: 'Blade', position: cutter.position});
  }
  for (const [name, sensor] of Object.entries(state.sensors || {})) {
    if (state.active_sensors && !state.active_sensors.includes(name)) continue;
    const short = name.split(':').at(-1);
    if (!['extruder', 'toolhead'].includes(short)) continue;
    if (Number.isFinite(sensor.position)) refs.push({
      label: short === 'extruder' ? 'Extruder sensor' : 'Toolhead sensor', position: sensor.position,
      gap: short === 'extruder' && Number.isFinite(gears) ? gears - sensor.position : null});
  }
  return refs.filter(r => Number.isFinite(r.position)).map(r => ({...r, distance: nozzle - r.position}));
}

export function coolingTubeMeasurements(state) {
  const unit = state.units?.indexOf(state.unit) ?? 0;
  const params = state.configuration?.parameters?.[unit];
  const macro = params?.form_tip_macro?.trim().split(/\s+/)[0];
  const vars = state.configuration?.macros || {};
  if (!macro || !Number.isFinite(state.layout?.nozzle)) return null;
  const get = name => vars[`macro:${macro}_VARS:${name}`] ?? vars[`macro:${macro}:${name}`];
  const position = get('cooling_tube_position'), length = get('cooling_tube_length');
  if (!Number.isFinite(position) || !Number.isFinite(length) || position < 0 || length <= 0) return null;
  return {position, length, start: state.layout.nozzle-position, end: state.layout.nozzle-position-length};
}

export function dimensionParameter(state, label) {
  const parameters = {'Bowden': 'calibrated_bowden_length',
    'Extruder entry': 'toolhead_extruder_to_nozzle',
    'Extruder sensor': 'toolhead_entry_to_extruder',
    'Toolhead sensor': 'toolhead_sensor_to_nozzle',
    'Encoder offset': 'gate_endstop_to_encoder',
    'Residual': 'toolhead_residual_filament'};
  if (parameters[label]) return parameters[label];
  const variable = {'Blade':'blade_pos', 'Cooling tube position':'cooling_tube_position',
    'Cooling tube length':'cooling_tube_length'}[label];
  if (!variable) return null;
  const unit = state.units?.indexOf(state.unit) ?? 0;
  const macro = state.configuration?.parameters?.[unit]?.form_tip_macro?.trim().split(/\s+/)[0];
  const vars = state.configuration?.macros || {};
  return [`macro:${macro}_VARS:${variable}`, `macro:${macro}:${variable}`]
    .find(key => Object.hasOwn(vars, key)) || null;
}

// Keep the encoder parking reference through bookkeeping state changes. Fade
// its schematic offset along the Bowden so the toolhead landmarks stay exact.
export function encoderFilamentDistance(events, event, state, tip, pathDistance, encoderDistance) {
  let anchor = null;
  for (const item of events) {
    if (item.seq > event.seq) break;
    const value = item.state;
    if (value?.gate !== state.gate || value.unit !== state.unit || item.kind === 'movement') continue;
    const pos = value.filament_pos;
    const manual = item.telemetry?.source === '_MMU_TEST';
    if (manual || item.kind === 'session' || item.kind === 'gate_changed' || (!anchor && [0,1].includes(pos))) {
      anchor = [0,1,2].includes(pos) ? value : null;
    } else if (!anchor && pos === 2) anchor = value;
  }
  if (!anchor) return pathDistance(tip);
  const anchorTip = anchor.tips[state.gate];
  const encoder = state.layout.mmu_encoder ?? 20;
  const overshoot = Math.max(0, anchor.encoder_reference?.overshoot || 0);
  const origin = encoderDistance + (anchor.filament_pos === 0 ? -18
    : pathDistance(encoder + overshoot) - pathDistance(encoder));
  const start = pathDistance(anchorTip), end = pathDistance(state.layout.extruder_entry);
  const position = pathDistance(tip);
  const remaining = end > start ? Math.max(0, Math.min(1, (end-position)/(end-start))) : 0;
  return position + (origin-start)*remaining;
}

// Keep schematic parking and switch homing on the same continuous path.
export function switchFilamentDistance(tip, home, parking, entry, sensorDistance, pathDistance, encoder = null) {
  const knots = [[home,sensorDistance],[entry,pathDistance(entry)]];
  if (encoder && encoder.position !== home && encoder.position < entry) knots.push([encoder.position,encoder.distance]);
  if (parking && home+parking < entry) {
    const parked=home+parking;
    let distance=sensorDistance+Math.sign(parking)*(8+24*gateSpacingScale(parking));
    for (const [position, landmark] of knots) {
      if (parked < position) distance=Math.min(distance,landmark-8);
      else if (parked > position) distance=Math.max(distance,landmark+8);
      else distance=landmark;
    }
    knots.push([parked,distance]);
  }
  // A parked tip can coincide with the encoder.
  for (let i=knots.length-1;i>0;i--) if (knots.slice(0,i).some(k=>k[0]===knots[i][0])) knots.splice(i,1);
  knots.sort((a,b)=>a[0]-b[0]);
  if (tip >= entry) return pathDistance(tip);
  let index=knots.findIndex(([position])=>position>=tip);
  if (index <= 0) index=1;
  const [a,b]=[knots[index-1],knots[index]];
  return a[1]+(b[1]-a[1])*(pathDistance(tip)-pathDistance(a[0]))/(pathDistance(b[0])-pathDistance(a[0]));
}

// Resolve the recorded endstop, never an arbitrary nearby exit sensor.
export function encoderReference(state, sensors) {
  const unit = state.units?.indexOf(state.unit) ?? 0;
  const params = state.configuration?.parameters?.[unit] || {};
  const endstop = state.encoder_reference?.endstop ?? params.gate_homing_endstop;
  const offset = state.encoder_reference?.offset ?? params.gate_endstop_to_encoder ?? 0;
  const short = name => name.split(':').at(-1).replace(/^mmu_/, '');
  const target = short(endstop || '');
  if (!['exit', 'shared_exit', `exit_${state.gate}`].includes(target)) return null;
  const sensor = sensors.find(([name, value]) => Number.isFinite(value.position) &&
    (short(name) === target || (target === 'exit' && short(name) === `exit_${state.gate}`)));
  return sensor && Number.isFinite(offset) ? {name:sensor[0], offset} : null;
}

function gateSpacingScale(distance) {
  return Number.isFinite(distance) ? Math.max(0,Math.min(1,(Math.abs(distance)-25)/75)) : 0;
}

// Expand the ordered stack for long offsets, keeping every device below the curve.
export function gateDevicePositions(state, sensors) {
  const short = name => name.split(':').at(-1);
  const hasExit = sensors.some(([name,d]) => Number.isFinite(d.position) &&
    ['mmu_exit',`mmu_exit_${state.gate}`].includes(short(name)));
  const hasShared = sensors.some(([name,d]) => Number.isFinite(d.position) && short(name) === 'mmu_shared_exit');
  const devices = [...(hasExit ? ['exit'] : []), ...(hasShared ? ['sharedExit'] : [])];
  if (state.devices?.[`${state.unit}:encoder`]) {
    const reference = encoderReference(state,sensors);
    const target = reference?.name.split(':').at(-1) === 'mmu_shared_exit' ? 'sharedExit' : 'exit';
    const index = reference ? devices.indexOf(target)+(reference.offset < 0 ? 0 : 1) : 0;
    devices.splice(index,0,'encoder');
  }
  // Allocate the unused straight section, rather than a fixed expansion per gap.
  const available=100-Math.max(0,devices.length-1)*32;
  const extra=devices.map(()=>0);
  const reference=encoderReference(state,sensors);
  const params=state.configuration?.parameters?.[state.units?.indexOf(state.unit) ?? 0] || {};
  const endstop=state.encoder_reference?.endstop ?? params.gate_homing_endstop ?? '';
  const target=endstop.includes('shared_exit') ? 'sharedExit' : endstop.includes('exit') ? 'exit' : 'encoder';
  const homeIndex=devices.indexOf(target);
  if (homeIndex >= 0) extra[homeIndex]+=available*gateSpacingScale(state.encoder_reference?.parking ?? params.gate_parking_distance);
  if (reference && devices.includes('encoder')) {
    const refDevice=reference.name.includes('shared_exit') ? 'sharedExit' : 'exit';
    extra[Math.max(devices.indexOf(refDevice),devices.indexOf('encoder'))]+=available*gateSpacingScale(reference.offset);
  }
  const total=extra.reduce((sum,value)=>sum+value,0);
  const scale=total ? Math.min(1,available/total) : 1;
  let y=260;
  return Object.fromEntries(devices.map((device,index)=>{
    y-=(index ? 32 : 0)+extra[index]*scale;
    return [device,y];
  }));
}

export function lastEncoderReading(events, current, unit) {
  const index = events.findIndex(event => event === current || (current?.seq != null && event.seq === current.seq));
  for (let i = index; i >= 0; --i) {
    const event = events[i];
    const state = event.settled_state || event.state;
    const value = state?.devices?.[`${unit}:encoder`]?.encoder_pos;
    if (state?.unit === unit && Number.isFinite(value)) return value;
  }
  return null;
}

// Blend through the last retained melt, finishing when its volume has cleared.
export function purgeColor(state, deposited) {
  const plan = state.purge_transition;
  if (!plan) return state.nozzle_color || state.filament?.retained_color || '808182';
  let fraction = plan.mix_mm > 0
    ? Math.max(0, Math.min(1, (deposited-plan.start_mm-plan.retained_mm+plan.mix_mm)/plan.mix_mm))
    : Number(deposited >= plan.start_mm+plan.retained_mm);
  fraction = fraction*fraction*(3-2*fraction);
  return [0,2,4].map(i=>Math.round(parseInt(plan.old_color.slice(i,i+2),16)*(1-fraction)
    +parseInt(plan.new_color.slice(i,i+2),16)*fraction).toString(16).padStart(2,'0')).join('');
}

// The material below the zero reference changes only after the new front arrives.
export function purgeOutletColor(state, deposited) {
  const plan=state.purge_transition;
  if (!plan) return purgeColor(state,deposited);
  if (Number.isFinite(plan.settled_mm) && deposited > plan.settled_mm) return plan.new_color;
  return purgeColor({...state,purge_transition:{...plan,retained_mm:plan.retained_mm+plan.mix_mm}},deposited);
}

// Equal volume of round 1.75mm feedstock and a round nozzle-sized strand.
export function purgeStrandLength(feedLength, nozzleDiameter = .4) {
  return Math.max(0, feedLength) * (1.75 / nozzleDiameter) ** 2;
}

export function purgeCoils(state, nozzleDiameter = .4) {
  const ratio = purgeStrandLength(1, nozzleDiameter);
  const total = purgeStrandLength((state.extruded_mm || 0)-(state.purge_base_mm || 0), nozzleDiameter);
  const amount = total % 36;
  const base = (state.purge_base_mm || 0) + (total-amount)/ratio;
  // The drawing uses 7px for 1.75mm filament; leave a small gap between strands.
  const pitch = (7 * nozzleDiameter / 1.75) * 1.15;
  const capacity = 32 / pitch;
  const layers = Math.max(0, (Math.min(30,amount)-1.2)/28.8*capacity);
  const coils = [];
  for (let layer=0; layer<Math.ceil(layers); layer++) {
    const height=layer*pitch, y=498-height, radius=23-height*1.75/3.2;
    const bend=pitch/3.2;
    coils.push({path:`M260,${y} C${260-radius},${y-5*bend} ${260-radius},${y+5*bend} 260,${y+2*bend} S${260+radius*1.6},${y-3*bend} 260,${y-3*bend}`,
      fraction: Math.min(1,layers-layer), color:purgeOutletColor(state,base+(1.2+layer*28.8/capacity)/ratio)});
  }
  return {amount, layers, pitch, coils, drop:state.purge_drop ?? Math.max(0,(amount-30)/6)};
}

// Sensor readings constrain the illustration; modeled displacement supplies overtravel.
export function bufferPiston(device, tension=false, compression=false) {
  const maximum=Math.max(0,device.maximum || 0), range=Math.min(maximum,Math.max(0,device.range ?? maximum));
  const center=maximum/2, low=center-range/2, high=center+range/2;
  let position=device.travel ?? device.rest ?? center;
  if (device.occupied === false) position=device.rest ?? center;
  if (device.proportional) {
    const reading=Math.max(-1,Math.min(1,device.value || 0));
    position=center+reading*range/2;
    if (device.displacement < low) position=device.displacement;
    if (device.displacement > high) position=device.displacement;
  } else {
    if (tension) position=Math.min(position,low);
    if (compression) position=Math.max(position,high);
  }
  const bounded=Math.max(0,Math.min(maximum,position));
  const total=device.occupied === false ? bounded : device.displacement ?? position;
  return {fraction:maximum ? bounded/maximum : .5,position:bounded,low,high,maximum,
    excess:total-Math.max(0,Math.min(maximum,total))};
}

// Five rounded turns with straight ends; fixed height, variable pitch.
export function bufferSpringPath(wall, edge, centerY = 52.5) {
  const direction = Math.sign(edge - wall);
  const lead = Math.min(4, Math.abs(edge - wall) / 4);
  const start = wall + direction * lead, finish = edge - direction * lead;
  const turns = 5, pitch = (finish - start) / turns, amplitude = 3.7 * 4 / 3;
  let path = `M${wall} ${centerY} H${start}`;
  for (let turn = 0; turn < turns; turn++) {
    const x = start + turn * pitch;
    path += ` C${x+.8*pitch} ${centerY-amplitude} ${x-.3*pitch} ${centerY-amplitude} ${x+.5*pitch} ${centerY}`;
    path += ` C${x+1.3*pitch} ${centerY+amplitude} ${x+.2*pitch} ${centerY+amplitude} ${x+pitch} ${centerY}`;
  }
  return path + ` H${edge}`;
}

export function excessFilamentLabel(excess) {
  return `${excess > 0 ? 'compression' : 'tension'} · ${excess > 0 ? '+' : ''}${excess.toFixed(1)}mm`;
}

export function toolchangeIndex(events, index, direction) {
  for (let i=index+direction; i>=0 && i<events.length; i+=direction)
    if (events[i].kind === 'toolchange') return i;
  return -1;
}

export function annotatedStep(events, event, navigating) {
  if (navigating) return event;
  const animated = item => ['movement', 'gate_changed', 'toolchange'].includes(item.kind);
  return animated(event) ? event : events.findLast(item => item.seq <= event.seq && animated(item)) || event;
}

export function isInternalUpdate(record) {
  return Boolean(record.internal || (record.kind === 'state'
    && record.label === 'State changed' && !record.telemetry));
}

// Fold idle bookkeeping into the preceding step; in-move updates belong to its outcome.
export function visibleSteps(records) {
  const steps = [];
  for (const record of records) {
    const internal = isInternalUpdate(record);
    if (internal) {
      const previous=steps.at(-1);
      // Gate deltas belong to the following selection animation, not the prior step.
      if (previous && !record.during_move && record.state.gate === previous.state.gate)
        previous.settled_state = record.state;
    } else {
      steps.push({...record});
    }
  }
  return steps;
}


// Time when the true incoming end reaches zero, including acceleration.
export function nozzleArrivalTime(event, movementMs) {
  const gate=event.state.gate, start=event.before?.tips[gate], nozzle=event.state.layout?.nozzle;
  if (event.kind !== 'movement' || !(start < nozzle) || !(event.state.tips[gate] >= nozzle-.001)) return null;
  const travel=event.raw_before && event.raw_state && !event.homing_anchor
    ? event.filament_travel_mm ?? event.raw_state.tips[gate]-event.raw_before.tips[gate]
    : event.state.tips[gate]-start;
  const arrival=Math.min(1,(nozzle-start)/travel);
  let low=0,high=1;
  for(let i=0;i<24;i++) {
    const mid=(low+high)/2;
    if(motionFraction(event,mid)<arrival) low=mid; else high=mid;
  }
  return high*movementMs;
}

// Finish color mixing after motion stops, without adding filament travel.
export function playbackFrame(event, elapsed, movementMs, selectionGrip = event.before?.mmu_gripped) {
  const fraction=event.kind === 'gate_changed'
    ? Math.max(0,Math.min(1,(elapsed-500)/(movementMs-1000)))
    : motionFraction(event,movementMs ? Math.min(1,elapsed/movementMs) : 1);
  let state=interpolate(event,fraction);
  if (event.kind === 'gate_changed' && elapsed < movementMs) {
    // Release before retreat and restore the starting grip after advance, in either direction.
    state={...state,mmu_gripped:elapsed >= 250 && elapsed < movementMs-250 ? false : selectionGrip};
  }
  if (event.purge_settle && elapsed >= movementMs) {
    const blend=Math.max(0,Math.min(1,(elapsed-movementMs)/700));
    const color=purgeColor({purge_transition:{start_mm:0,retained_mm:1,mix_mm:1,
      old_color:event.purge_settle.from,new_color:event.purge_settle.to}},blend);
    state={...state,nozzle_outlet_color:color};
  }
  const gate=event.state.gate;
  const arrivalTime=nozzleArrivalTime(event,movementMs);
  if (arrivalTime !== null) {
    const flood=Math.max(0,Math.min(1,(elapsed-arrivalTime)/500));
    state={...state,nozzle_fill_fraction:flood,
      nozzle_fill_from:event.before.filament?.residual_mm > 0
        ? event.before.nozzle_outlet_color || event.before.filament.retained_color : null};
    if (event.before?.tip_shapes?.[gate] === 'cone') state.cone_edge_opacity=1-flood;
  }
  if (event.kind === 'movement' && event.before) {
    const visualEndpoint=s=>{
      const tip=s.tips[gate];
      let result=tip;
      for (const [name,sensor] of Object.entries(s.sensors || {})) {
        if (s.active_sensors && !s.active_sensors.includes(name)) continue;
        if (!['extruder','toolhead'].includes(name.split(':').at(-1)) || sensor.enabled === false || !Number.isFinite(sensor.position)) continue;
        const fragment=s.retained_segment;
        if (fragment && fragment.start<=sensor.position && fragment.end>=sensor.position) continue;
        if (sensor.state === true) result=Math.max(result,sensor.position);
        else if (sensor.state === false) result=Math.min(result,sensor.position);
      }
      return result;
    };
    // Blend display corrections over the same accelerated travel as the move.
    // Recorded/model positions remain untouched for subsequent accounting.
    const before=event.before, after=event.settled_state || event.state;
    const from=visualEndpoint(before)-before.tips[gate];
    const to=visualEndpoint(after)-after.tips[gate];
    const tips=[...state.tips];
    tips[gate]+=from+(to-from)*fraction;
    state={...state,visual_tips:tips};
  }
  return {state,fraction};
}

// Keep the housing and gear-to-nozzle span fixed; the entry sensor has its own gap.
export function toolheadDrawingRange(state) {
  const entry=state.layout.extruder_gears ?? state.layout.extruder_entry ?? 700;
  const sensor=Object.entries(state.sensors || {}).find(([name,sensor])=>
    (!state.active_sensors || state.active_sensors.includes(name)) &&
    name.split(':').at(-1) === 'extruder' && Number.isFinite(sensor.position) && sensor.position < entry);
  return {entry,end:state.layout.nozzle ?? (state.layout.toolhead ?? 760)+30,
    height:243,sensorEntry:sensor?.[1].position ?? entry,sensorGap:sensor ? 35 : 0};
}

// Reserve a 10-unit visible gap between the 19-unit gear and 5-unit sensor.
export function toolheadProgress(state, position, range=toolheadDrawingRange(state)) {
  const fraction=(position-range.entry)/(range.end-range.entry);
  const sensor=Object.entries(state.sensors || {}).find(([name,sensor])=>
    (!state.active_sensors || state.active_sensors.includes(name)) &&
    name.split(':').at(-1) === 'toolhead' && Number.isFinite(sensor.position));
  const pivot=sensor ? (sensor[1].position-range.entry)/(range.end-range.entry) : 0;
  if (pivot <= 0 || pivot >= 1) return fraction;
  const drawn=Math.max(pivot,34/range.height);
  return fraction <= pivot ? fraction*drawn/pivot
    : drawn+(fraction-pivot)*(1-drawn)/(1-pivot);
}
