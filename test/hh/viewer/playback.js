/* Copyright (C) 2022-2026 Paul Morgan. GPLv3. */
export function interpolate(event, fraction) {
  const a = event.before, b = event.state;
  if (!a || fraction >= 1) return b;
  if (fraction <= 0) return a;
  if (event.kind === 'cut') return fraction < .4 ? a : b;
  const mix = (x,y) => typeof x === 'number' && typeof y === 'number' ? x+(y-x)*fraction : x;
  const s = structuredClone(a);
  s.tip_shapes = b.tip_shapes || a.tip_shapes;
  for (const key of ['tips','tails']) s[key] = a[key].map((x,i)=>mix(x,b[key][i]));
  if (event.raw_before && event.raw_state && !event.homing_anchor) {
    // Reach a physical stop at commanded pace, then hold while gears keep feeding.
    s.tips = a.tips.map((tip, i) => {
      const delta = i === b.gate && event.filament_travel_mm != null ? event.filament_travel_mm
        : event.raw_state.tips[i] - event.raw_before.tips[i];
      const candidate = tip + delta * fraction;
      return delta >= 0 ? Math.min(candidate, b.tips[i]) : Math.max(candidate, b.tips[i]);
    });
  }
  if (a.retained_segment) {
    const segment = a.retained_segment;
    const gate = b.gate;
    const push = Math.max(0, s.tips[gate] - Math.max(a.tips[gate], segment.start));
    const nozzle = b.layout.nozzle;
    s.retained_segment = {...segment, start: Math.min(nozzle, segment.start+push),
      end: Math.min(nozzle, segment.end+push)};
    if (s.retained_segment.start >= s.retained_segment.end) s.retained_segment = null;
  }
  s.extruded_mm = a.extruded_mm || 0;
  const emitted = (b.extruded_mm || 0) - s.extruded_mm;
  if (emitted > 0) {
    const gate = b.gate;
    const segment = a.retained_segment;
    const approach = segment
      ? Math.max(0,segment.start-a.tips[gate]) + Math.max(0,(b.layout.nozzle-(b.filament?.residual_mm || 0))-segment.end)
      : Math.max(0,(b.layout?.nozzle ?? a.tips[gate])-a.tips[gate]);
    s.extruded_mm += Math.max(0, Math.min(emitted, (approach + emitted) * fraction - approach));
  }
  s.purge_base_mm = b.purge_base_mm ?? 0;
  if ((b.purge_base_mm || 0) > (a.purge_base_mm || 0)) {
    s.purge_base_mm = a.purge_base_mm || 0;
    s.purge_drop = fraction;
  }
  s.purge_transition = b.purge_transition || a.purge_transition;
  s.nozzle_color = purgeColor(s, s.extruded_mm);
  for (const [name,value] of Object.entries(b.motors)) s.motors[name]=mix(a.motors[name]||0,value);
  for (const [name,device] of Object.entries(a.devices)) {
    for (const [key,value] of Object.entries(device)) s.devices[name][key]=mix(value,b.devices[name]?.[key]);
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
        const travel=mix(geometry.travel[a.gate],next.travel[a.gate]);
        const threshold=name.endsWith('tension')?v=>v<=0:v=>v>=geometry.compression_travel;
        if (threshold(geometry.travel[a.gate])===sensor.state && threshold(next.travel[a.gate])===end.state) sensor.state=threshold(travel);
      }
    } else if (!name.includes('encoder') && occupancy(a,name,sensor.position)===sensor.state && occupancy(b,name,end.position)===end.state) {
      sensor.state=occupancy(s,name,sensor.position);
    }
  }
  return s;
}
export function duration(event, compressBowden = false) {
  if (event.kind === 'cut' || event.kind === 'release') return 650;
  if (event.kind === 'purge_release') return 650;
  if (event.kind !== 'movement') return 0;
  const travel=Math.abs(event.distance ?? Math.max(0,...Object.values(event.motors||{}).map(Math.abs)));
  // Use resolved feed speed, not console pacing or time spent waiting for sensors.
  let seconds = event.speed > 0 && travel > 0 ? travel/event.speed : event.duration || travel/100;
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
  return Math.max(0.12,seconds)*1000;
}

// A bounded schematic coil; long purges continue accumulating without leaving the scene.
export function purgePath(amount) {
  if (!(amount > 0)) return '';
  const turns = Math.min(12, amount / 3);
  const points = [];
  const count = Math.max(1, Math.ceil(turns * 32));
  for (let i = 0; i <= count; i++) {
    const t = turns * i / count;
    const radius = 22 * Math.min(1, t / .5) * (1 - .065 * t);
    points.push(`${(260 + radius * Math.sin(t * Math.PI * 2)).toFixed(2)} ${(498 - t * 2.6 + 2 * Math.cos(t * Math.PI * 2)).toFixed(2)}`);
  }
  return 'M' + points.join(' L');
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
  const prefix = `${event.seq} · ${event.time.toFixed(3)}s · ${event.kind === 'movement' ? (event.homing ? 'homing move' : (event.source || event.telemetry?.start?.source) === 'mmu_gcode' ? 'G1' : 'move') : event.kind} · `;
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
    ? `Bowden · ${Number(length.toFixed(1))}mm · compressed scale` : 'Bowden · Uncalibrated length';
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

export function lastEncoderReading(events, current, unit) {
  const index = events.findIndex(event => event === current || (current?.seq != null && event.seq === current.seq));
  for (let i = index; i >= 0; --i) {
    const event = events[i];
    const value = event.state?.devices?.[`${unit}:encoder`]?.encoder_pos;
    if (event.state?.unit === unit && Number.isFinite(value)) return value;
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

// Equal volume of round 1.75mm feedstock and a round nozzle-sized strand.
export function purgeStrandLength(feedLength, nozzleDiameter = .4) {
  return Math.max(0, feedLength) * (1.75 / nozzleDiameter) ** 2;
}

export function purgeCoils(state, nozzleDiameter = .4) {
  const ratio = purgeStrandLength(1, nozzleDiameter);
  const total = purgeStrandLength((state.extruded_mm || 0)-(state.purge_base_mm || 0), nozzleDiameter);
  const amount = total % 36;
  const base = (state.purge_base_mm || 0) + (total-amount)/ratio;
  const layers = Math.max(0, (Math.min(30,amount)-1.2)/28.8*10);
  const coils = [];
  for (let layer=0; layer<Math.ceil(layers); layer++) {
    const y=498-layer*3.2, radius=23-layer*1.75;
    coils.push({path:`M260,${y} C${260-radius},${y-5} ${260-radius},${y+5} 260,${y+2} S${260+radius*1.6},${y-3} 260,${y-3}`,
      fraction: Math.min(1,layers-layer), color:purgeColor(state,base+(1.2+layer*2.88)/ratio)});
  }
  return {amount, layers, coils, drop:state.purge_drop ?? Math.max(0,(amount-30)/6)};
}
