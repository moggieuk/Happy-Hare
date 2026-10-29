/* Copyright (C) 2022-2026 Paul Morgan. GPLv3. */
export function interpolate(event, fraction) {
  const a = event.before, b = event.state;
  if (!a || fraction >= 1) return b;
  if (fraction <= 0) return a;
  const mix = (x,y) => typeof x === 'number' && typeof y === 'number' ? x+(y-x)*fraction : x;
  const s = structuredClone(a);
  for (const key of ['tips','tails']) s[key] = a[key].map((x,i)=>mix(x,b[key][i]));
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
export function duration(event) {
  if (event.kind !== 'movement') return 0;
  const travel=Math.abs(event.distance ?? Math.max(0,...Object.values(event.motors||{}).map(Math.abs)));
  // Use resolved feed speed, not console pacing or time spent waiting for sensors.
  let seconds = event.speed > 0 && travel > 0 ? travel/event.speed : event.duration || travel/100;
  if (event.fast_bowden && !event.homing && event.before && travel > 0) {
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
