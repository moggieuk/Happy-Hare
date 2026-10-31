import { test } from 'node:test';
import assert from 'node:assert/strict';
import { interpolate, duration, purgePath, gearAngle, CommandHistory, traceText, homingPulse, bladeStroke, bowdenLengthLabel, toolheadReferences, coolingTubeMeasurements } from './playback.js';
const state=(tip,on)=>({gate:0,tips:[tip],tails:[null],motors:{gear:tip},devices:{encoder:{distance:tip}},buffers:{},sensors:{toolhead:{state:on,position:5}}});
test('sensor crosses at geometric position with exact endpoints',()=>{
 const e={before:state(0,false),state:state(10,true)};
 assert.equal(interpolate(e,.4).sensors.toolhead.state,false);
 assert.equal(interpolate(e,.6).sensors.toolhead.state,true);
 assert.equal(interpolate(e,.6).tips[0],6);
 assert.equal(interpolate(e,.6).devices.encoder.distance,6);
 assert.strictEqual(interpolate(e,0),e.before);
 assert.strictEqual(interpolate(e,1),e.state);
});
test('reverse homing clears the sensor and disabled/overridden states stay authoritative',()=>{
 const e={before:state(10,true),state:state(0,false)};
 assert.equal(interpolate(e,.6).sensors.toolhead.state,false);
 e.before.sensors.toolhead.state=null;e.state.sensors.toolhead.state=null;
 assert.equal(interpolate(e,.5).sensors.toolhead.state,null);
 e.before.sensors.toolhead.state=false;e.state.sensors.toolhead.state=false;
 assert.equal(interpolate(e,.1).sensors.toolhead.state,false);
});
test('duration follows recorded time or travel and speed',()=>{
 assert.equal(duration({kind:'movement',duration:2}),2000);
 assert.equal(duration({kind:'movement',distance:300,speed:100}),3000);
 assert.equal(duration({kind:'state'}),0);
});

test('speed controls playback independently of console pacing',()=>{
 const event={kind:'movement',distance:200,speed:100,duration:99};
 assert.equal(duration(event),2000);
 assert.equal(duration({...event,speed:200}),1000);
});
test('only fast Bowden travel receives geometric time compression, in either direction',()=>{
 const before={...state(0,false),layout:{extruder_entry:1000,toolhead:1060}};
 const after={...before,tips:[1000]};
 const e={kind:'movement',distance:1000,speed:200,fast_bowden:true,before,state:after};
 const expected=5000*(.56/1000)/(.27/90);
 assert.ok(Math.abs(duration(e,true)-expected)<1e-9);
 assert.ok(Math.abs(duration({...e,distance:-1000,before:after,state:before},true)-expected)<1e-9);
 assert.equal(duration(e),5000);
 assert.equal(duration({...e,homing:1},true),5000);
 assert.equal(duration({...e,fast_bowden:false}),5000);
});

test('collision reaches hard stop at travel pace and holds while motors continue',()=>{
 const e={before:state(0,false),state:state(4,false),raw_before:state(0,false),raw_state:state(20,false)};
 e.state.motors.gear=20;
 assert.equal(interpolate(e,.1).tips[0],2);
 assert.equal(interpolate(e,.5).tips[0],4);
 assert.equal(interpolate(e,.5).motors.gear,10);
 assert.equal(interpolate(e,1).tips[0],4);
 assert.equal(interpolate(e,.5).sensors.toolhead.state,false);
});

test('purge starts after nozzle contact and coil geometry stays bounded',()=>{
 const e={before:{...state(0,false),extruded_mm:0},state:{...state(5,false),layout:{nozzle:5},extruded_mm:10}};
 assert.equal(interpolate(e,.2).extruded_mm,0);
 assert.equal(interpolate(e,.5).extruded_mm,2.5);
 assert.equal(purgePath(0),'');
 assert.ok(purgePath(10).startsWith('M'));
 assert.equal(purgePath(100000),purgePath(36));
});

test('nozzle retract releases coil while preserving deposited amount',()=>{
 const a={...state(5,false),extruded_mm:10,purge_base_mm:0,nozzle_color:'ff00ff'};
 const b={...state(3,false),extruded_mm:10,purge_base_mm:10,nozzle_color:'00ffff'};
 const mid=interpolate({before:a,state:b},.5);
 assert.equal(mid.purge_drop,.5);
 assert.equal(mid.extruded_mm,10);
 assert.equal(mid.purge_base_mm,0);
 assert.equal(mid.nozzle_color,'8080ff');
});

test('reverse homing reconciles schematic offset without passing the sensor',()=>{
 const e={before:state(22.9,true),state:state(4.999,false),raw_before:state(35,true),raw_state:state(4.999,false),homing_anchor:true};
 let previous=22.9;
 for(let i=0;i<=100;i++) {
   const tip=interpolate(e,i/100).tips[0];
   assert.ok(tip>=4.999 && tip<=previous);
   previous=tip;
 }
 assert.ok(Math.abs(interpolate(e,.5).tips[0]-13.9495)<1e-9);
});

test('gear rim travel matches local filament travel rather than an arbitrary angle',()=>{
 assert.ok(Math.abs(gearAngle(2*Math.PI*19/3,3)-360)<1e-9);
 assert.equal(gearAngle(0,3),0);
 assert.ok(Math.abs(gearAngle(-10,2)+gearAngle(10,2))<1e-9);
});

test('purge playback is half speed without changing ordinary movement pacing',()=>{
 const move={kind:'movement',distance:10,speed:5,before:{extruded_mm:2},state:{extruded_mm:12}};
 assert.equal(duration(move),4000);
 move.state.extruded_mm=2;
 assert.equal(duration(move),2000);
});

test('command history traverses commands and restores the unfinished draft', () => {
 const h = new CommandHistory();
 assert.equal(h.navigate(-1, 'draft'), 'draft');
 h.add('MMU_HOME'); h.add('MMU_LOAD'); h.add('MMU_LOAD');
 assert.equal(h.navigate(-1, 'MMU_TEST '), 'MMU_LOAD');
 assert.equal(h.navigate(-1, ''), 'MMU_HOME');
 assert.equal(h.navigate(-1, ''), 'MMU_HOME');
 assert.equal(h.navigate(1, ''), 'MMU_LOAD');
 assert.equal(h.navigate(1, ''), 'MMU_TEST ');
 h.add('MMU_UNLOAD');
 assert.equal(h.navigate(-1, ''), 'MMU_UNLOAD');
});
test('trace describes homing results and requested motion without losing the endstop', () => {
 const e = {seq:29,time:1444.505,kind:'movement',telemetry:{start:{motor_mode:'gear'}},homing:1,homed:true,distance:14.5,requested:25,speed:80,acceleration:400,endstop:'extruder'};
 assert.equal(traceText(e), '29 · 1444.505s · homing move · GEAR: Homed after 14.5mm | requested: dist=25.0 · speed=80.0 · accel=400.0 · endstop=extruder');
 assert.match(traceText({...e,homed:false,distance:25}), /Did not home after 25.0mm/);
 assert.match(traceText({...e,homed:null}), /result unknown/);
});
test('trace formats synced, extruder, fallback and state events', () => {
 const e = {seq:31,time:1444.705,kind:'movement',telemetry:{start:{motor_mode:'synced'}},distance:25,speed:80,acceleration:400};
 assert.equal(traceText(e), '31 · 1444.705s · move · SYNCED GEAR+EXTRUDER: dist=25.0mm · speed=80.0 · accel=400.0');
 assert.equal(traceText({...e,telemetry:null,motors:{extruder:2.5},distance:2.5,speed:5,acceleration:null}), '31 · 1444.705s · move · EXTRUDER: dist=2.5mm · speed=5.0');
 assert.equal(traceText({seq:33,time:1445.105,kind:'state',label:'IN BOWDEN'}), '33 · 1445.105s · state · IN BOWDEN');
});

test('sensor pulses only successful homes and distinguishes reverse travel', () => {
 const e = {kind:'movement',homing:1,homed:true,endstop:'extruder',requested:25,state:{sensors:{'unit0:mmu_extruder':{position:700}},active_sensors:['unit0:mmu_extruder']}};
 assert.deepEqual(homingPulse(e), {sensor:'unit0:mmu_extruder',reverse:false});
 assert.equal(homingPulse({...e,requested:-25}).reverse, true);
 assert.equal(homingPulse({...e,homed:false}), null);
 assert.equal(homingPulse({...e,homed:null}), null);
 assert.equal(homingPulse({...e,homing:0}), null);
 assert.equal(homingPulse({...e,endstop:'encoder'}), null);
});

test('blade crosses and retracts only for its explicit cut event', () => {
 const e = {kind:'cut',cutter:'toolhead'};
 assert.equal(duration(e),650);
 assert.equal(bladeStroke(e,0,'toolhead'),0);
 assert.equal(bladeStroke(e,.5,'toolhead'),1);
 assert.equal(bladeStroke(e,1,'toolhead'),0);
 assert.equal(bladeStroke(e,.5,'mmu'),0);
 assert.equal(bladeStroke({kind:'movement'},.5,'toolhead'),0);
});

test('Bowden label follows recorded unit, gate and calibration', () => {
  const s = {unit: 'unit0', gate: 0, configuration: {calibration: {
    unit0: {bowden_lengths: {'0': 550, '1': 625.25}},
    unit1: {bowden_lengths: {'2': 700}},
  }}};
  assert.equal(bowdenLengthLabel(s), 'Bowden · 550mm · compressed scale');
  assert.equal(bowdenLengthLabel({...s, gate: 1}), 'Bowden · 625.3mm · compressed scale');
  assert.equal(bowdenLengthLabel({...s, unit: 'unit1', gate: 2}), 'Bowden · 700mm · compressed scale');
  const updated = structuredClone(s);
  updated.configuration.calibration.unit0.bowden_lengths['0'] = 560;
  assert.equal(bowdenLengthLabel(updated), 'Bowden · 560mm · compressed scale');
  assert.equal(bowdenLengthLabel(s), 'Bowden · 550mm · compressed scale');
  for (const length of [0, -1, null, undefined]) {
    updated.configuration.calibration.unit0.bowden_lengths['0'] = length;
    assert.equal(bowdenLengthLabel(updated), 'Bowden · Uncalibrated length');
  }
  assert.equal(bowdenLengthLabel({...s, gate: -1}), 'Bowden · Uncalibrated length');
  assert.equal(bowdenLengthLabel({}), 'Bowden · Uncalibrated length');
});

test('toolhead measurements use nozzle zero and recorded geometry', () => {
 const s = {layout:{nozzle:780, extruder_entry:700, extruder_gears:708},
  cutters:[{kind:'toolhead',position:728},{kind:'mmu',position:10}],
  active_sensors:['extruder','toolhead'],
  sensors:{extruder:{position:700},toolhead:{position:718},'unit1:toolhead':{position:720}}};
 const refs = toolheadReferences(s);
 assert.deepEqual(refs.map(r=>[r.label,r.distance]), [
  ['Nozzle',0],['Extruder entry',72],['Blade',52],['Extruder sensor',80],['Toolhead sensor',62]]);
 assert.equal(refs.find(r=>r.label==='Extruder sensor').gap,8);
 assert.deepEqual(toolheadReferences({}),[]);
 const changed=structuredClone(s); changed.cutters[0].position=695;
 assert.equal(toolheadReferences(changed).find(r=>r.label==='Blade').distance,85);
 assert.equal(toolheadReferences(s).find(r=>r.label==='Blade').distance,52);
});

test('cooling tube length starts at the configured cooling position', () => {
 const s={unit:'unit0',units:['unit0'],layout:{nozzle:780},configuration:{
  parameters:{0:{form_tip_macro:'_MMU_FORM_TIP'}},macros:{
   'macro:_MMU_FORM_TIP_VARS:cooling_tube_position':35,
   'macro:_MMU_FORM_TIP_VARS:cooling_tube_length':15}}};
 assert.deepEqual(coolingTubeMeasurements(s),{position:35,length:15,start:745,end:730});
 s.configuration.parameters[0].form_tip_macro='_MMU_CUT_TIP';
 assert.equal(coolingTubeMeasurements(s),null);
 assert.equal(coolingTubeMeasurements({}),null);
});
