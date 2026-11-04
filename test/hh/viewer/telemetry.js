/* Copyright (C) 2022-2026 Paul Morgan. GPLv3. */
import {purgeColor, purgeOutletColor, shortenedHair} from './playback.js';

const copy = value => structuredClone(value);
const finite = value => typeof value === 'number' && Number.isFinite(value);
const number = (value, fallback=0) => finite(value) ? value : fallback;
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const short = name => name.split(':').at(-1);
const color = value => /^[0-9a-f]{6}$/i.test(String(value)) ? String(value) : '808182';
const positions = ['UNLOADED', 'HOMED TO GATE', 'START BOWDEN', 'IN BOWDEN', 'END BOWDEN',
  'HOMED TO EXTRUDER', 'EXTRUDER ENTRY', 'IN EXTRUDER', 'HOMED TO TOOLHEAD', 'IN TOOLHEAD', 'LOADED IN NOZZLE'];
const forbidden = new Set(['__proto__', 'prototype', 'constructor']);

function validateFacts(facts) {
  const machine=facts?.printer?.mmu_machine, mmu=facts?.printer?.mmu;
  if (!machine?.units || !mmu || !Number.isInteger(machine.num_gates) || machine.num_gates < 1 || machine.num_gates > 1000 ||
      !Object.hasOwn(machine.units,mmu.unit) || !Number.isInteger(mmu.gate) || mmu.gate < -2 || mmu.gate >= machine.num_gates ||
      !Array.isArray(mmu.gate_color) || !Array.isArray(mmu.active_sensors) || !mmu.sensors ||
      !Array.isArray(mmu.drive_motors?.gear) || !Array.isArray(mmu.drive_motors?.extruder))
    throw Error('Telemetry is missing valid MMU/machine configuration');
  for (const unit of Object.values(machine.units)) {
    if (!Array.isArray(unit.gates) || !Array.isArray(unit.sensors) || !unit.parameters ||
        !unit.toolhead?.parameters || !unit.extruder || !unit.bowden_lengths)
      throw Error('Telemetry is missing unit configuration');
  }
  for (const name of mmu.active_sensors) if (!mmu.sensors[name]) throw Error('Telemetry is missing an active sensor');
}

export function applyDelta(state, changes) {
  if (!Array.isArray(changes)) throw Error('Invalid telemetry state delta');
  for (const change of changes) {
    const path = change.path;
    if (!Array.isArray(path) || !path.length || path.some(k => typeof k !== 'string' || forbidden.has(k)))
      throw Error('Invalid telemetry state path');
    let target = state;
    for (const key of path.slice(0,-1)) {
      if (!Object.hasOwn(target,key)) target[key] = {};
      target = target[key];
      if (!target || typeof target !== 'object' || Array.isArray(target)) throw Error('Invalid telemetry state path');
    }
    const key = path.at(-1);
    if (change.remove === true) delete target[key];
    else target[key] = copy(change.value);
  }
}

export function parseTelemetry(text) {
  if (text.length > 32*1024*1024) throw Error('Telemetry file exceeds 32 MB');
  let records;
  try {
    const value = JSON.parse(text);
    records = Array.isArray(value) ? value : value?.records || value?.result?.records || (value?.event ? [value] : null);
  } catch {
    records = text.split(/\r?\n/).filter(line=>line.trim()).map((line,i)=>{
      try { return JSON.parse(line); } catch { throw Error(`Invalid JSON on line ${i+1}`); }
    });
  }
  if (!Array.isArray(records) || !records.length || records.length > 100000)
    throw Error('Expected a telemetry JSONL file or an array of telemetry records');
  if (records[0]?.event !== 'session') throw Error('This recording has no session snapshot. Save a new telemetry capture.');
  if (records.slice(1).some(r=>r.event === 'session')) throw Error('Load one telemetry session at a time');
  return records;
}

// This reducer has no printer connection or simulator imports. All durable input
// is the session baseline, state deltas and ordered Happy Hare source records.
export class TelemetryReplay {
  constructor() {
    this.session = null;
    this.sequence = -1;
    this.facts = null;
    this.events = [];
    this.pending = new Map();
    this.tips = []; this.shapes = []; this.hairLengths = []; this.motors = {};
    this.fragments = {}; this.hotends = {}; this.bufferPositions = {};
    this.excess = {}; this.overshoot = {};
    this.cutter = false;
    this.selectionBefore = null;
  }

  accept(records) { for (const record of records) this.feed(record); return this.events; }

  feed(record) {
    if (!record || !Number.isSafeInteger(record.sequence) || record.sequence < 0 || !finite(record.timestamp))
      throw Error('Invalid telemetry sequence or timestamp');
    if (record.event === 'session') {
      if (record.schema !== 'happy_hare.motion' || record.version !== 2 || typeof record.session_id !== 'string' || !record.session_id)
        throw Error('Unsupported telemetry format; expected happy_hare.motion version 2');
      const f = record.state, machine = f?.printer?.mmu_machine, mmu = f?.printer?.mmu;
      validateFacts(f);
      Object.assign(this, new TelemetryReplay());
      this.session = record.session_id;
      this.sequence = record.sequence;
      this.checkpoint = Boolean(record.checkpoint);
      this.facts = copy(f);
      this.tips = Array(machine.num_gates).fill(-20);
      this.hairLengths = Array(machine.num_gates).fill(15);
      this.shapes = Array(machine.num_gates).fill(machine.form_tip_macro?.toLowerCase().includes('cut') ? 'square' : 'cone');
      const state = this.snapshot();
      if (mmu.gate >= 0) this.tips[mmu.gate] = this.logicalTip(state, mmu.filament_pos, true);
      this.emit(record, 'session', this.checkpoint ? 'Recording checkpoint · earlier motion unavailable' : 'Session ready');
      return;
    }
    if (!this.facts) throw Error('Telemetry must begin with a session snapshot');
    if (record.sequence !== this.sequence+1) throw Error('Telemetry has a sequence gap; reload a complete capture');
    this.sequence = record.sequence;
    if (record.event === 'state_delta') {
      const before = this.snapshot();
      if (record.changes.some(c=>c.path.join('.') === 'printer.mmu.gate')) this.selectionBefore=before;
      applyDelta(this.facts, record.changes);
      validateFacts(this.facts);
      // Positive reports refine the cut length; zero during loading does not erase an unpurged fragment.
      for (const {path,value} of record.changes) {
        if (path.length !== 6 || path.slice(0,3).join('.') !== 'printer.mmu_machine.units' ||
            path[4] !== 'extruder' || path[5] !== 'filament_remaining' || !finite(value) || value <= 0) continue;
        const extruder=this.facts.printer.mmu_machine.units[path[3]]?.extruder;
        const fragment=this.fragments[extruder?.name];
        if (fragment) fragment.end=fragment.start+value;
      }
      const configuration = record.changes.filter(({path}) => path[0] === 'macros' ||
        path.includes('parameters') || path.includes('bowden_lengths') || path.includes('form_tip_macro') || path.includes('purge_macro') ||
        path.at(-1) === 'enabled' || /^(buffer_range|buffer_maxrange|buffer_spring_state)$/.test(path.at(-1)));
      // Motion outcomes carry their sensor changes; don't insert a step between a pair.
      if (configuration.length) this.emit(record, 'config', 'Configuration changed', {before,
        changes:configuration.map(c=>({name:c.path.join('.'),after:c.remove ? null : c.value}))});
      else this.emit(record, 'state', 'State changed', {internal:true, during_move:this.pending.size > 0, telemetry:record});
      return;
    }
    if (record.event === 'movement_start') {
      this.pending.set(record.move_id, {start:copy(record), before:this.snapshot()});
      return;
    }
    if (record.event === 'movement_outcome') {
      const pair = this.pending.get(record.move_id);
      if (!pair) {
        if (this.checkpoint) return; // A rolling capture may begin inside a move.
        throw Error(`Movement ${record.move_id} has no start record`);
      }
      this.pending.delete(record.move_id);
      this.move(record, pair);
      return;
    }
    const before = this.snapshot(), mmu = this.facts.printer.mmu;
    if (record.event === 'filament_pos_changed') {
      mmu.filament_pos = record.filament_pos;
      if (finite(record.homing_overshoot_mm)) this.overshoot[mmu.gate] = record.homing_overshoot_mm;
      else if (record.filament_pos === 0) delete this.overshoot[mmu.gate];
      const state = this.snapshot();
      if (mmu.gate >= 0 && (record.filament_pos === 0 || !this.fragments[state.filament.extruder] || record.source === '_MMU_TEST'))
        this.tips[mmu.gate] = this.logicalTip(state, record.filament_pos, record.source === '_MMU_TEST');
      this.emit(record, 'state', record.filament_pos_name || positions[record.filament_pos] || 'UNKNOWN', {before,telemetry:record});
    } else if (record.event === 'gate_changed') {
      // The delta precedes this event, so recover the old selection for animation.
      Object.assign(before,this.selectionBefore || {});
      this.selectionBefore=null;
      before.gate = record.previous_gate;
      mmu.gate = record.gate;
      if (record.unit) mmu.unit = record.unit;
      // An unloaded selection uses this gate's configured home and parking distance,
      // not the generic initial position of an unvisited filament.
      if (mmu.gate >= 0 && mmu.filament_pos === 0) {
        delete this.overshoot[mmu.gate];
        this.tips[mmu.gate]=this.logicalTip(this.snapshot(),0);
      }
      this.emit(record, 'gate_changed', `Gate ${record.previous_gate} → ${record.gate}`, {before,telemetry:record});
    } else if (record.event === 'espooler_started' || record.event === 'espooler_stopped') {
      const unit=this.facts.printer.mmu_machine.units[record.unit];
      if (unit) {
        unit.espooler ??= {operations:{}};
        unit.espooler.operations[String(record.gate)]={direction:record.direction,output_power:record.output_power};
      }
      this.emit(record,'espooler',`Gate ${record.gate} · ${record.direction}`,{before,telemetry:record});
} else if (record.event === 'toolchange') {
      this.emit(record, 'toolchange', record.label || record.command, {telemetry:record});
    } else if (record.event === 'cut' || record.event === 'filament_cut') {
      const gate = mmu.gate, nozzle = before.layout.nozzle;
      const blade = finite(record.blade_pos) ? nozzle-record.blade_pos : before.cutters.find(c=>c.kind === 'toolhead')?.position;
      this.cutter = record.phase === 'CUT';
      if (gate >= 0 && finite(blade) && this.tips[gate] > blade) {
        this.fragments[before.filament.extruder] = {start:blade,end:this.tips[gate],
          tip_shape:this.shapes[gate],hair_length:this.hairLengths[gate],color:before.filament.incoming_color};
        this.tips[gate] = blade;
        this.shapes[gate] = 'square';
      }
      this.emit(record,'cut','Toolhead filament cut',{before,cutter:record.cutter || 'toolhead',telemetry:record});
    } else if (record.event === 'release') {
      this.cutter = false;
      this.emit(record,'release','Toolhead cutter release',{before,cutter:record.cutter || 'toolhead',telemetry:record});
    } else {
      // Retain diagnostics without folding in-move observations into the prior step.
      this.emit(record,'state','State changed',{internal:true,during_move:this.pending.size > 0,telemetry:record});
    }
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
      fragment_mm:number(extruder.filament_remaining),retained_color:color(extruder.filament_remaining_color),incoming_color:color(m.gate_color[m.gate])};
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
      if (u.encoder) { devices[`${name}:encoder`]={encoder_pos:u.encoder.distance}; object_status[`mmu_encoder ${u.encoder.name}`]=u.encoder.status || {encoder_pos:u.encoder.distance}; }
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
      object_status[`mmu_toolhead ${u.toolhead.name}`]=null;
    }
    const vars=f.macros?.[machine.form_tip_macro+'_VARS'] || f.macros?.[machine.form_tip_macro] || {};
    const cutters=finite(vars.blade_pos) ? [{kind:'toolhead',position:nozzle-vars.blade_pos,before_extruder:vars.cutter_before_extruder}] : [];
    return copy({unit:m.unit,units,unit_gates:unit.gates,tool:m.tool,gate:m.gate,filament_pos:m.filament_pos,filament_pos_name:positions[m.filament_pos] || 'UNKNOWN',
      tips:this.tips,tails:this.tips.map(()=>null),tip_shapes:this.shapes,hair_lengths:this.hairLengths,gate_colors:m.gate_color.map(color),gate_status:m.gate_status || [],layout,sensors,active_sensors:m.active_sensors,
      unit_sensors:unit.sensors,devices,buffers,object_status,configuration,filament,cutters,cutter_pressed:this.cutter,
      espoolers:Object.assign({},...Object.values(machine.units).map(u=>u.espooler?.operations || {})),
      extruder_target_temperature:finite(extruder.target_temperature) ? extruder.target_temperature : null,
      mmu_gripped:mmuGripped,synced:[1,3].includes(m.sync_mode),drive_motors:m.drive_motors,motors:this.motors,
      encoder_reference:{endstop:up.gate_homing_endstop,offset:up.gate_endstop_to_encoder,parking:number(up.gate_parking_distance),overshoot:this.overshoot[m.gate] || 0},
      retained_segment:this.fragments[extruder.name] || null,extruded_mm:hotend.extruded,purge_base_mm:hotend.base,purge_transition:hotend.plan,
      nozzle_color:hotend.nozzle,nozzle_outlet_color:hotend.outlet ?? purgeOutletColor({purge_transition:hotend.plan},hotend.extruded),bowden_unabsorbed_mm:devices[`${m.unit}:buffer`]?.excess ?? (this.excess[m.gate] || 0),sensor_conflicts:[]});
  }

  move(outcome,{start,before}) {
    const state=this.snapshot(), gate=start.gate, d=number(outcome.actual_mm);
    for (const [name,value] of Object.entries(outcome.sensors || {})) if (state.sensors[name]) state.sensors[name].state=value;
    const mode=start.motor_mode || 'gear';
    const extruding=mode.includes('extruder') || mode === 'synced';
    const gearing=mode.includes('gear') || mode === 'synced' || (start.source === 'mmu_gcode' && before.synced);
    const motors=new Set(start.motors || []);
    if (gearing) for (const name of before.drive_motors.gear) motors.add(name);
    if (extruding) for (const name of before.drive_motors.extruder) motors.add(name);
    for (const name of motors) this.motors[name]=number(this.motors[name])+d;
    const details={before,telemetry:{start:{...start,motor_mode:start.source === 'mmu_gcode' && before.synced ? 'synced' : mode},outcome},source:start.source,requested:start.requested_mm,distance:outcome.actual_mm,
      duration:outcome.elapsed_s,speed:outcome.extrusion_speed_mm_s ?? start.speed_mm_s,acceleration:start.acceleration_mm_s2,
      homing:start.homing,endstop:start.endstop,homed:outcome.homed,error:outcome.error,outcome:outcome.status,
      motors:Object.fromEntries([...motors].map(n=>[n,d])),fast_bowden:/Fast (loading|unloading) move through bowden/.test(start.label)};
    if (gate >= 0 && gate < this.tips.length) {
      const tip=this.tips[gate], gears=state.layout.extruder_gears,nozzle=state.layout.nozzle;
      const gripped=tip > gears+.001;
      const engaging=extruding && d > 0 && tip >= gears-.001;
      let travel=extruding ? (gripped || engaging || gearing ? d : 0) : gearing ? d : 0;
      if (gearing && !extruding) {
        if (gripped) travel=0;
        else if (d > 0) travel=Math.min(d,Math.max(0,gears-tip));
      }
      // Extruder-only retraction loses traction at the gears; discard further rotation.
      if (extruding && !gearing && d < 0) {
        travel=Math.max(travel,Math.min(0,gears-tip));
        details.filament_travel_mm=d;
      }
      const differential=(gearing ? d : 0)-(extruding && (gripped || engaging) ? travel : 0);
      const buffered=state.devices[`${state.unit}:buffer`];
      if (before.mmu_gripped && buffered && gearing && (extruding || !gripped) && d < 0) {
        const initial=before.devices[`${state.unit}:buffer`] || buffered;
        const approach=Math.max(0,tip-gears);
        details.buffer_takeup_mm=Math.min(Math.max(0,-d-approach),Math.max(0,(initial.modeled_displacement ?? initial.displacement)-initial.rest));
        details.buffer_takeup_after_mm=approach;
        details.filament_travel_mm=d;
        travel+=details.buffer_takeup_mm;
      }
      if (before.mmu_gripped && buffered && (gripped || tip+travel >= gears)) {
        // Mid-move sensor updates may already constrain the current snapshot.
        // Apply travel once, from the position captured when the move began.
        const initial=before.devices[`${state.unit}:buffer`] || buffered;
        this.bufferPositions[gate]=(initial.modeled_displacement ?? initial.displacement)+(gripped ? differential : Math.max(0,d-travel));
      } else if (before.mmu_gripped && !buffered) {
        this.excess[gate]=gripped ? number(this.excess[gate])+differential : Math.max(0,d-travel);
        if (tip+travel < gears) this.excess[gate]=0;
      }
      let next=tip+travel;
      const target=state.active_sensors.find(n=>n === start.endstop || short(n) === short(start.endstop || '') || short(n) === `${start.endstop}_${gate}`);
      if (start.homing && outcome.homed && target) {
        const sensor=state.sensors[target];
        const forward=start.trigger_state ?? (number(start.requested_mm,d)>0);
        if (!/filament_(compression|tension)|encoder/.test(target) && finite(sensor.position)) {
          next=sensor.position-(forward ? 0 : .001);
          details.homing_anchor=true;
        } else if (buffered) {
          const low=(buffered.maximum-buffered.range)/2,high=(buffered.maximum+buffered.range)/2;
          this.bufferPositions[gate]=target.endsWith('compression') ? high : low;
          if (target.endsWith('compression') && forward && gearing && !extruding && d > 0) {
            details.buffer_contact_fraction=Math.min(1,Math.max(0,gears-tip)/d);
            details.filament_travel_mm=d;
          }
          const buffer=this.facts.printer.mmu_machine.units[state.unit].buffer;
          if (buffer.proportional) buffer.value=target.endsWith('compression') ? 1 : -1;
        }
        // A homing result is more precise than the last asynchronous status sample.
        this.facts.printer.mmu.sensors[target].triggered=forward;
        sensor.state=forward;
      }
      const hotend=this.hotends[state.filament.extruder], segment=this.fragments[state.filament.extruder];
      // Presence sensors constrain drawing, never discard recorded tip travel.

      if (extruding && gripped && travel < 0) {
        // Residual geometry does not add travel. Returning a retract must first
        // recover the previous contact point before producing more purge.
        hotend.front=Math.max(hotend.front,Math.min(nozzle,tip));
        if (tip >= nozzle-state.filament.residual_mm-.01) {
          if (this.shapes[gate] !== 'cone') this.hairLengths[gate]=15;
          this.shapes[gate]='cone';
        }
        if (hotend.extruded > hotend.base) hotend.base=hotend.extruded;
      }
      if (travel > 0 && next >= gears && this.shapes[gate] === 'cone')
        this.hairLengths[gate]=shortenedHair(this.hairLengths[gate],next,nozzle-state.filament.residual_mm);
      if (extruding && travel > 0) {
        let amount=segment ? Math.max(0,next-nozzle)
          : Math.max(0,next-Math.max(tip,hotend.front));
        if (segment) {
          const push=Math.max(0,next-Math.max(tip,segment.start));
          const melt=nozzle-state.filament.residual_mm;
          amount+=Math.max(0,Math.min(nozzle,segment.end+push)-Math.max(melt,segment.end));
          amount+=Math.min(segment.end-segment.start,Math.max(0,segment.end+push-nozzle));
          if (segment.tip_shape === 'cone') segment.hair_length=shortenedHair(segment.hair_length ?? 15,segment.end+push,melt);
          segment.start=Math.min(nozzle,segment.start+push); segment.end=Math.min(nozzle,segment.end+push);
          if (segment.start>=segment.end) delete this.fragments[state.filament.extruder];
        }
        details.purge_approach_mm=Math.max(0,travel-amount);
        hotend.extruded+=amount;
        if (next >= nozzle && hotend.plan && hotend.plan.nozzle_arrival_mm == null)
          hotend.plan.nozzle_arrival_mm=hotend.extruded-Math.max(0,next-nozzle);
        hotend.nozzle=purgeColor({...state,purge_transition:hotend.plan},hotend.extruded);
        if (hotend.outlet !== state.filament.retained_color && next >= nozzle
            && hotend.plan && hotend.extruded >= hotend.plan.start_mm+hotend.plan.retained_mm
            && state.filament.retained_color === state.filament.incoming_color) {
          hotend.outlet=state.filament.retained_color;
        }
        next=Math.min(next,nozzle);
        if (next >= nozzle-.001) this.shapes[gate]='square';
      }
      if (buffered && d < 0 && (details.buffer_takeup_mm > 0 || next < gears || (!gripped && next <= gears+.001))) {
        const initial=before.devices[`${state.unit}:buffer`] || buffered;
        const approach=Math.max(0,tip-gears);
        const released=Math.max(0,Math.abs(d)-approach);
        const from=initial.modeled_displacement ?? initial.displacement, to=initial.rest;
        this.bufferPositions[gate]=from+Math.sign(to-from)*Math.min(Math.abs(to-from),released);
        details.buffer_release={approach_mm:approach,travel_mm:Math.abs(d),from,to};
      }
      if (extruding && !gearing && d < 0 && !details.homing_anchor) next=Math.max(next,Math.min(tip,gears));
      // Once unloaded from the extruder, the next load contacts the remaining melt,
      // rather than inheriting the old filament's retraction recovery point.
      if (tip > gears && next <= gears) hotend.front=nozzle-state.filament.residual_mm;
      this.tips[gate]=next;
      const rawBefore=copy(before), rawState=copy(state);
      rawState.tips[gate]=tip+travel;
      details.raw_before=rawBefore; details.raw_state=rawState;
    }
    const after=this.snapshot();
    // Outcome readings belong at the end of the animation, never its start.
    for (const [name,value] of Object.entries(outcome.sensors || {})) if (after.sensors[name] && !(start.homing && outcome.homed && name === start.endstop)) after.sensors[name].state=value;
    this.emit(outcome,'movement',start.label || start.commandline || 'Movement',details,after);
  }

  emit(record,kind,label,details={},state=this.snapshot()) {
    this.events.push({version:2,session:this.session,seq:record.sequence,time:record.timestamp,kind,label,state,...details});
  }
}
