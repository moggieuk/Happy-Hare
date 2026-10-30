/* Copyright (C) 2022-2026 Paul Morgan. GPLv3. */
export function interpolate(event, fraction) {
  const a = event.before, b = event.state;
  if (!a || fraction >= 1) return b;
  if (fraction <= 0) return a;
  const mix = (x,y) => typeof x === 'number' && typeof y === 'number' ? x+(y-x)*fraction : x;
  const s = structuredClone(a);
  for (const key of ['tips','tails']) s[key] = a[key].map((x,i)=>mix(x,b[key][i]));
  if (event.raw_before && event.raw_state) {
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
