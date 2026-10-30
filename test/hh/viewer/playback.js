/* Copyright (C) 2022-2026 Paul Morgan. GPLv3. */
export function interpolate(event, fraction) {
  const a = event.before, b = event.state;
  if (!a || fraction >= 1) return b;
  if (fraction <= 0) return a;
  const mix = (x,y) => typeof x === 'number' && typeof y === 'number' ? x+(y-x)*fraction : x;
  const s = structuredClone(a);
  for (const key of ['tips','tails']) s[key] = a[key].map((x,i)=>mix(x,b[key][i]));
  if (event.raw_before && event.raw_state && !event.homing_anchor) {
    // Reach a physical stop at commanded pace, then hold while gears keep feeding.
    s.tips = a.tips.map((tip, i) => {
      const delta = event.raw_state.tips[i] - event.raw_before.tips[i];
      const candidate = tip + delta * fraction;
      return delta >= 0 ? Math.min(candidate, b.tips[i]) : Math.max(candidate, b.tips[i]);
    });
  }
  s.extruded_mm = a.extruded_mm || 0;
  const emitted = (b.extruded_mm || 0) - s.extruded_mm;
  if (emitted > 0) {
    const gate = b.gate;
    const approach = Math.max(0, (b.layout?.nozzle ?? a.tips[gate]) - a.tips[gate]);
    s.extruded_mm += Math.max(0, Math.min(emitted, (approach + emitted) * fraction - approach));
  }
  s.purge_base_mm = b.purge_base_mm ?? 0;
  if ((b.purge_base_mm || 0) > (a.purge_base_mm || 0)) {
    s.purge_base_mm = a.purge_base_mm || 0;
    s.purge_drop = fraction;
  }
  if (a.nozzle_color && b.nozzle_color) {
    s.nozzle_color = [0,2,4].map(i=>Math.round(mix(parseInt(a.nozzle_color.slice(i,i+2),16),parseInt(b.nozzle_color.slice(i,i+2),16))).toString(16).padStart(2,'0')).join('');
  }
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
  if (event.kind === 'cut') return 650;
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
  const prefix = `${event.seq} · ${event.time.toFixed(3)}s · ${event.kind === 'movement' ? (event.homing ? 'homing move' : 'move') : event.kind} · `;
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
