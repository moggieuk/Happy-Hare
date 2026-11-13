/* Copyright (C) 2022-2026 Paul Morgan. GPLv3. */
import {purgeOutletColor} from './playback.js';
import {DEFAULT_COLOR, finite, number, clamp, short, positions} from './util.js';
const copy = value => structuredClone(value);
const color = value => /^[0-9a-f]{6}$/i.test(String(value)) ? String(value) : DEFAULT_COLOR;

export function spoolPercentage(initial, remaining) {
  return finite(initial) && initial > 0 && finite(remaining) && remaining >= 0
    ? clamp(remaining / initial * 100, 0, 100) : null;
}

export class SceneModel {
  constructor() {
    this.tips=[]; this.shapes=[]; this.hairLengths=[]; this.motors={};
    this.fragments={}; this.hotends={}; this.bufferPositions={};
    this.excess={}; this.overshoot={}; this.cutter=false;
  }
  logicalTip(s, pos, manual=false) {
    const {extruder_entry:entry,extruder_gears:gears,nozzle} = s.layout;
    const sensor = name => s.active_sensors.map(n=>[n,s.sensors[n]])
      .find(([n])=>short(n) === name || short(n) === `${name}_${s.gate}`)?.[1]?.position;
    const home = s.encoder_reference.endstop === 'encoder' ? s.layout.mmu_encoder
      : sensor(s.encoder_reference.endstop) ?? 0;
    const toolhead = sensor('toolhead') ?? (gears+nozzle)/2;
    // Keep physical distances here; schematic parking gaps belong to drawing.
    // UNLOADED is an authoritative parked location, including after recovery.
    const exact = {0:home+s.encoder_reference.parking,5:sensor('extruder') ?? entry,6:gears,8:toolhead};
    if (manual) Object.assign(exact, {'-1':-20,1:home,2:home,3:(home+entry)/2,4:entry,7:(gears+toolhead)/2,9:(toolhead+nozzle)/2,10:nozzle});
    return exact[pos] ?? this.tips[s.gate] ?? -20;
  }

  snapshot() {
    const f=this.facts, m=f.printer.mmu, machine=f.printer.mmu_machine;
    const units=Object.keys(machine.units), unit=machine.units[m.unit];
    if (!unit) throw Error('Unknown MMU unit in telemetry');
    // Unknown/legacy grip data cannot establish a constrained Bowden segment.
    const mmuGripped=unit.filament_always_gripped === true || [1,2].includes(m.filament_grip_state);
    if (!mmuGripped) {
      this.excess[m.gate]=0;
      delete this.bufferPositions[m.gate];
    }
    const p=unit.toolhead.parameters, up=unit.parameters;
    const bowden=unit.bowden_lengths[m.gate];
    const gateEndstop=short(up.gate_homing_endstop || '').replace(/_\d+$/,'');
    const gateHome=gateEndstop === 'mmu_shared_exit' ? 10 : gateEndstop === 'encoder' ? 20 : 0;
    const gears=gateHome+(finite(bowden) && bowden > 0 ? bowden : 700);
    const entry=gears, entrySensor=gears-number(p.toolhead_entry_to_extruder);
    const nozzle=gears+number(p.toolhead_extruder_to_nozzle,50);
    const layout={mmu_entry:-150, mmu_exit:0, mmu_shared_exit:10, mmu_encoder:20,
      extruder_entry:entry,extruder_gears:gears,nozzle,toolhead:nozzle-number(p.toolhead_sensor_to_nozzle,20)};
    const sensors={};
    for (const [name,value] of Object.entries(m.sensors)) {
      const key=short(name).replace(/_\d+$/,'');
      const pos=key === 'extruder' ? entrySensor : key === 'toolhead' ? layout.toolhead
        : key === 'mmu_pre_gate' ? -150 : ['filament_compression','filament_tension'].includes(key) ? entry : layout[key];
      sensors[name]={state:value.triggered,enabled:value.enabled,position:finite(pos)?pos:null};
    }
    // StallGuard has no persistent filament-presence reading.
    if (up.extruder_homing_endstop === 'mmu_gear_touch')
      sensors[`${m.unit}:mmu_gear_touch`]={state:null,enabled:true,position:null};
    const macros={};
    for (const [name,values] of Object.entries(f.macros || {})) for (const [key,value] of Object.entries(values)) macros[`macro:${name}:${key}`]=value;
    const configuration={parameters:{},calibration:{},macros,sensors:Object.fromEntries(Object.entries(sensors).map(([n,v])=>[n,v.enabled]))};
    units.forEach((name,i)=>{
      const u=machine.units[name];
      configuration.parameters[i]={...u.parameters,...u.toolhead.parameters,form_tip_macro:machine.form_tip_macro,purge_macro:machine.purge_macro};
      configuration.calibration[name]={bowden_lengths:u.bowden_lengths};
    });
    const extruder=unit.extruder, residual=number(p.toolhead_residual_filament);
    const filament={extruder:extruder.name,retracted_length:extruder.retracted_length,residual_mm:residual,
      fragment_mm:number(extruder.filament_cut_fragment ?? extruder.filament_remaining),retained_color:color(extruder.filament_remaining_color),incoming_color:color(m.gate_color[m.gate])};
    if (!this.hotends[extruder.name]) {
      this.hotends[extruder.name]={extruded:0,base:0,front:nozzle-residual,plan:null,nozzle:filament.retained_color};
      if (filament.fragment_mm > 0) this.fragments[extruder.name]={start:nozzle-residual-filament.fragment_mm,end:nozzle-residual,
        tip_shape:'cone',color:filament.retained_color};
    }
    const hotend=this.hotends[extruder.name];
    if (!hotend.plan || hotend.plan.new_color !== filament.incoming_color) {
      const fragment=this.fragments[extruder.name];
      hotend.outlet=null;
      hotend.plan={start_mm:hotend.extruded,retained_mm:residual+(fragment ? fragment.end-fragment.start : 0),
        old_color:hotend.nozzle,new_color:filament.incoming_color,mix_mm:Math.max(0,residual)};
    }
    const devices={}, buffers={}, object_status={};
    for (const name of units) {
      const u=machine.units[name];
      if (u.encoder) { devices[`${name}:encoder`]={encoder_pos:u.encoder.distance,resolution:u.encoder.parameters?.resolution}; object_status[`mmu_encoder ${u.encoder.name}`]=u.encoder.status || {encoder_pos:u.encoder.distance}; }
      if (u.buffer) {
        const b=u.buffer, maximum=Math.max(0,number(b.buffer_maxrange)), range=clamp(number(b.buffer_range),0,maximum);
        const rest=b.buffer_spring_state === 'tension' ? 0 : b.buffer_spring_state === 'compression' ? maximum : maximum/2;
        const occupied=u.gates.includes(m.gate) && this.tips[m.gate] >= 0;
        let displacement=this.bufferPositions[m.gate] ?? rest;
        if (!occupied) displacement=rest;
        if (b.proportional && finite(b.value)) {
          // Preserve modeled overtravel only outside the switch interval.
          const low=(maximum-range)/2,high=(maximum+range)/2;
          if (displacement >= low && displacement <= high)
            displacement=maximum/2+clamp(b.value,-1,1)*range/2;
        } else {
          if (u.sensors.some(n=>short(n)==='filament_tension' && sensors[n]?.state === true)) displacement=Math.min(displacement,(maximum-range)/2);
          if (u.sensors.some(n=>short(n)==='filament_compression' && sensors[n]?.state === true)) displacement=Math.max(displacement,(maximum+range)/2);
        }
        if (occupied && this.bufferPositions[m.gate] == null) this.bufferPositions[m.gate]=displacement;
        const modeled=occupied ? this.bufferPositions[m.gate] : rest;
        const travel=clamp(displacement,0,maximum);
        devices[`${name}:buffer`]={maximum,range,rest,spring:b.buffer_spring_state,occupied,proportional:b.proportional,
          value:b.value,travel,displacement,modeled_displacement:modeled,excess:modeled-clamp(modeled,0,maximum)};
        buffers[b.name]={gates:u.gates,maxrange:maximum,range,spring:b.buffer_spring_state,travel:{[m.gate]:travel},excess:{[m.gate]:displacement-travel},tension_travel:(maximum-range)/2,compression_travel:(maximum+range)/2};
        object_status[`mmu_buffer ${b.name}`]=null;
      }
      object_status[`mmu_extruder ${u.extruder.name}`]=null;
    }
    const vars=f.macros?.[machine.form_tip_macro+'_VARS'] || f.macros?.[machine.form_tip_macro] || {};
    const cutters=finite(vars.blade_pos) ? [{kind:'toolhead',position:nozzle-vars.blade_pos,before_extruder:vars.cutter_before_extruder}] : [];
    return copy({unit:m.unit,units,unit_layouts:units.map(name=>({name,gates:machine.units[name].gates,selector_type:machine.units[name].selector_type || null,buffer:machine.units[name].buffer?.name ?? null})),unit_gates:unit.gates,selector_type:unit.selector_type || null,has_per_gate_heaters:unit.has_per_gate_heaters === true,tool:m.tool,gate:m.gate,filament_pos:m.filament_pos,filament_pos_name:positions[m.filament_pos] || 'UNKNOWN',
      tips:this.tips,tails:this.tips.map(()=>null),tip_shapes:this.shapes,hair_lengths:this.hairLengths,gate_colors:m.gate_color.map(color),gate_status:m.gate_status || [],layout,sensors,active_sensors:m.active_sensors,
      spool_percentages:m.gate_color.map((_,gate)=>spoolPercentage(m.gate_initial_weight?.[gate],m.gate_remaining_weight?.[gate])),
      gate_map:{filament_name:m.gate_filament_name,material:m.gate_material,vendor:m.gate_vendor,td:m.gate_td,temperature:m.gate_temperature,
        spool_id:m.gate_spool_id,speed_override:m.gate_speed_override,rfid:m.gate_spool_rfid,ttg_map:m.ttg_map,
        endless_spool_groups:m.endless_spool_groups,initial_weight:m.gate_initial_weight,remaining_weight:m.gate_remaining_weight},
      unit_sensors:unit.sensors,devices,buffers,object_status,configuration,filament,cutters,cutter_pressed:this.cutter,
      espoolers:Object.assign({},...Object.values(machine.units).map(u=>u.espooler?.operations || {})),
      extruder_target_temperature:finite(extruder.target_temperature) ? extruder.target_temperature : null,
      mmu_gripped:mmuGripped,synced:[1,3].includes(m.sync_mode),drive_motors:m.drive_motors,motors:this.motors,
      encoder_reference:{endstop:up.gate_homing_endstop,offset:up.gate_endstop_to_encoder,parking:number(up.gate_parking_distance),overshoot:this.overshoot[m.gate] || 0},
      retained_segment:this.fragments[extruder.name] || null,extruded_mm:hotend.extruded,purge_base_mm:hotend.base,purge_transition:hotend.plan,
      nozzle_color:hotend.nozzle,nozzle_outlet_color:hotend.outlet ?? purgeOutletColor({purge_transition:hotend.plan},hotend.extruded),bowden_unabsorbed_mm:devices[`${m.unit}:buffer`]?.excess ?? (this.excess[m.gate] || 0),sensor_conflicts:[]});
  }

}
