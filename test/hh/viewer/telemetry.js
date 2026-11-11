import {SceneModel} from './scene-state.js';
/* Copyright (C) 2022-2026 Paul Morgan. GPLv3. */
import {purgeColor, purgeOutletColor, shortenedHair} from './playback.js';

const copy = value => structuredClone(value);
const finite = value => typeof value === 'number' && Number.isFinite(value);
const number = (value, fallback=0) => finite(value) ? value : fallback;
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const short = name => name.split(':').at(-1);
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
export class TelemetryReplay extends SceneModel {
  constructor() {
    super();
    this.session = null;
    this.sequence = -1;
    this.facts = null;
    this.events = [];
    this.pending = new Map();
    this.pendingSelectors = new Map();
    this.selectionBefore = null;
  }

  accept(records) { for (const record of records) this.feed(record); return this.events; }

  feed(record) {
    if (!record || !Number.isSafeInteger(record.sequence) || record.sequence < 0 || !finite(record.timestamp))
      throw Error('Invalid telemetry sequence or timestamp');
    if (record.event === 'session') {
      if (record.schema !== 'happy_hare.motion' || record.version !== 1 || typeof record.session_id !== 'string' || !record.session_id)
        throw Error('Unsupported telemetry format; expected happy_hare.motion version 1');
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
            path[4] !== 'extruder' || !['filament_cut_fragment','filament_remaining'].includes(path[5]) || !finite(value) || value <= 0) continue;
        const extruder=this.facts.printer.mmu_machine.units[path[3]]?.extruder;
        const fragment=this.fragments[extruder?.name];
        if (fragment) fragment.end=fragment.start+value;
      }
      const configuration = record.changes.filter(({path}) => path[0] === 'macros' ||
        path.includes('parameters') || path.includes('bowden_lengths') || path.includes('form_tip_macro') || path.includes('purge_macro') ||
        path.at(-1) === 'enabled' || /^(buffer_range|buffer_maxrange|buffer_spring_state)$/.test(path.at(-1)));
      // Explicit espooler callbacks own activity transitions, not preceding status deltas.
      const state=this.snapshot();
      state.espoolers=before.espoolers;
      // Keep in-move bookkeeping separate from the preceding visible step.
      if (configuration.length) this.emit(record, 'config', 'Configuration changed', {before,
        changes:configuration.map(c=>({name:c.path.join('.'),after:c.remove ? null : c.value}))},state);
      else this.emit(record, 'state', 'State changed', {internal:true, during_move:this.pending.size > 0, telemetry:record},state);
      return;
    }
    if (record.event === 'selector_movement_start') {
      this.pendingSelectors.set(record.move_id, {start:copy(record), before:this.snapshot()});
      this.emit(record,'selector',record.label || 'Selector movement',{internal:true,telemetry:record});
      return;
    }
    if (record.event === 'selector_movement_outcome') {
      const pair = this.pendingSelectors.get(record.move_id);
      this.pendingSelectors.delete(record.move_id);
      if (!pair) { // A rolling capture may begin inside a selector move
        this.emit(record,'selector','Selector movement result',{internal:true,telemetry:record});
        return;
      }
      // Inside a filament move the pair stays bookkeeping, so that move's animation is not interrupted
      const inMove = this.pending.size > 0;
      this.emit(record,'selector_move',pair.start.label || 'Selector movement',
        {before:pair.before,actuator:pair.start.actuator,internal:inMove,during_move:inMove,
         telemetry:{start:pair.start,outcome:copy(record)}});
      return;
    }
    if (record.event === 'movement_start') {
      this.pending.set(record.move_id, {start:copy(record), before:this.snapshot()});
      this.emit(record,'pending_move',record.label || 'Movement',{internal:true,telemetry:record});
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
      const pending=this.pending.get(record.move_id);
      const state=this.snapshot();
      // Mid-move espooler callbacks must not reveal the outcome's sensor readings early.
      if (pending) {
        state.sensors=copy(pending.before.sensors);
        before.sensors=copy(pending.before.sensors);
      }
      this.emit(record,'espooler',`Gate ${record.gate} · ${record.direction}`,{before,telemetry:record,internal:Boolean(pending),during_move:Boolean(pending)},state);
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
      // Encoder reverse-homing is a series of ordinary moves; its shortfall
      // identifies clearance, using Happy Hare's 20% step threshold.
      if (start.source === 'move_filament' && /^Unloading step #\d+ from encoder$/.test(start.label || '')
          && state.encoder_reference?.endstop === 'encoder' && d < 0
          && finite(outcome.encoder_delta_mm) && outcome.encoder_delta_mm >= Math.abs(d)*.2) {
        next=state.layout.mmu_encoder-outcome.encoder_delta_mm;
        details.homing_anchor=true;
        details.encoder_rehome=true;
      }

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
        // Detaching from a full nozzle leaves calibrated melt behind once.
        // Further retracts preserve that contact point until the next extrusion.
        if (!segment && tip >= nozzle-.001) {
          details.residual_separation_mm=state.filament.residual_mm;
          next-=details.residual_separation_mm;
          details.filament_travel_mm=(details.filament_travel_mm ?? travel)-details.residual_separation_mm;
          hotend.front=nozzle-state.filament.residual_mm;
        } else hotend.front=Math.max(hotend.front,Math.min(nozzle,tip));
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
      rawState.tips[gate]=tip+travel-(details.residual_separation_mm || 0);
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
