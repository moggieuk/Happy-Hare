/* Copyright (C) 2022-2026 Paul Morgan. GPLv3. */
import {SceneModel} from './scene-state.js';
const clone=value=>structuredClone(value);
const rgb=value=>Array.isArray(value) ? value.slice(0,3).map(v=>Math.round(v*255).toString(16).padStart(2,'0')).join('') : String(value || '808182').replace(/^#/,'').slice(0,6);

// Moonraker replaces individual status fields, including dictionary-valued fields.
// Keep one current snapshot, never a history of subscription messages.
export class LiveState {
  constructor() { this.objects={}; this.eventtime=-Infinity; }
  accept(delta,eventtime=0) {
    if (eventtime<this.eventtime) return null;
    this.eventtime=eventtime;
    for (const [name,fields] of Object.entries(delta)) this.objects[name]={...this.objects[name],...clone(fields)};
    return this.snapshot();
  }
  snapshot() {
    const status=this.objects.mmu, config=this.objects.mmu_machine?.configuration;
    if (!status || !config?.units) return null;
    const machine=clone(config), names=Object.keys(machine.units), unitName=names[status.unit] || names[0];
    if (!unitName) return null;
    const m={...clone(status),unit:unitName, sensors:clone(status.sensor_states || {}),active_sensors:status.active_sensors || [],
      gate_color:(status.gate_color_rgb || status.gate_color || []).map(rgb),
      drive_motors:{gear:['gear'],extruder:['extruder']},sync_mode:status.sync_drive ? 1 : 0};
    while (m.gate_color.length<machine.num_gates) m.gate_color.push('808182');
    const macros={};
    for (const [name,object] of Object.entries(this.objects)) if (name.startsWith('gcode_macro ')) macros[name.slice(12)]=clone(object);
    for (const u of Object.values(machine.units)) {
      const ext=this.objects[`mmu_extruder ${u.extruder.name}`] || (u===machine.units[unitName] ? status : {});
      Object.assign(u.extruder,ext,{filament_remaining_color:rgb(ext.filament_remaining_color),
        target_temperature:this.objects[u.extruder.name]?.target});
      if (u.encoder) {
        u.encoder.status=this.objects[`mmu_encoder ${u.encoder.name}`] || {};
        u.encoder.distance=u.encoder.status.encoder_pos ?? 0;
      }
      if (u.buffer) u.buffer.value=this.objects[`mmu_buffer ${u.buffer.name}`]?.value ?? null;
      u.espooler={operations:Object.fromEntries(u.gates.map(g=>[g,{direction:status.espooler?.[g] || 'stopped',
        output_power:['assist','rewind'].includes(status.espooler?.[g]) ? 1 : 0}]))};
    }
    const model=new SceneModel();
    model.facts={printer:{mmu:m,mmu_machine:machine},macros};
    model.tips=Array(machine.num_gates).fill(-20);
    model.shapes=model.tips.map(()=>machine.form_tip_macro?.toLowerCase().includes('cut') ? 'square' : 'cone');
    model.hairLengths=model.tips.map(()=>15);
    let s=model.snapshot();
    for (const g of s.unit_gates) model.tips[g]=model.logicalTip({...s,gate:g},0,true);
    if (m.gate>=0) {
      model.tips[m.gate]=model.logicalTip(s,m.filament_pos,true);
      if (Number.isFinite(status.bowden_progress) && status.bowden_progress>=0) {
        const home=s.layout.extruder_gears-(machine.units[unitName].bowden_lengths[m.gate] || 700);
        model.tips[m.gate]=home+(s.layout.extruder_gears-home)*Math.min(100,status.bowden_progress)/100;
      }
    }
    // Buffer position is reported state, never a result of integrating motor travel.
    model.bufferPositions={};
    const buffer=machine.units[unitName].buffer;
    if (buffer && m.gate>=0 && model.tips[m.gate]>=0) {
      const center=buffer.buffer_maxrange/2, half=Math.min(buffer.buffer_range,buffer.buffer_maxrange)/2;
      const reading=buffer.proportional && Number.isFinite(buffer.value) ? Math.max(-1,Math.min(1,buffer.value))
        : m.sensors[`${buffer.name}:filament_compression`]?.triggered ? 1
        : m.sensors[`${buffer.name}:filament_tension`]?.triggered ? -1 : 0;
      model.bufferPositions[m.gate]=center+reading*half;
    }
    s=model.snapshot();
    // A released MMU grip does not invalidate a directly reported buffer position.
    if (buffer?.proportional && Number.isFinite(buffer.value)) {
      const d=s.devices[`${unitName}:buffer`];
      d.travel=d.displacement=d.modeled_displacement=buffer.buffer_maxrange/2+
        Math.max(-1,Math.min(1,buffer.value))*Math.min(buffer.buffer_range,buffer.buffer_maxrange)/2;
      d.excess=0;
    }
    s.homing=clone(status.homing || null);
    const touch=Object.keys(s.sensors).find(n=>n.endsWith(':mmu_gear_touch'));
    if (touch && s.homing) s.homing.endstops=s.homing.endstops.map(n=>n.split(':').at(-1).startsWith('mmu_gear_touch') ? touch : n);
    s.bowden_progress=status.bowden_progress;
    s.action=status.action;
    s.heaters=(machine.units[unitName].heaters || []).map(name=>name ? this.objects[name] || {} : {});
    s.object_status=Object.fromEntries(Object.entries(this.objects).filter(([n])=>/^mmu_(encoder|buffer|toolhead|espooler) /.test(n)));
    return s;
  }
}

// Cosmetic smoothing only: the target always comes from subscribed state.
export function liveFrame(from,to,fraction) {
  if (!from || fraction>=1) return to;
  const f=Math.max(0,fraction), s={...to,tips:[...to.tips],devices:clone(to.devices)};
  if (from.unit===to.unit && from.gate===to.gate && to.gate>=0) {
    s.tips[to.gate]=from.tips[to.gate]+(to.tips[to.gate]-from.tips[to.gate])*f;
    s.motors=Object.fromEntries(Object.entries(to.motors).map(([n,v])=>[n,(from.motors[n] || 0)+(v-(from.motors[n] || 0))*f]));
  }
  if (from.unit===to.unit) s.gate_focus=(from.gate_focus ?? from.gate)+(to.gate-(from.gate_focus ?? from.gate))*f;
  for (const [name,d] of Object.entries(s.devices)) {
    const a=from.devices[name];
    if (a && Number.isFinite(d.travel)) d.travel=a.travel+(d.travel-a.travel)*f;
  }
  return s;
}
