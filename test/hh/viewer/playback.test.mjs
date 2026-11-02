import { test } from 'node:test';
import assert from 'node:assert/strict';
import { switchFilamentDistance, syncForDisplay, encoderFilamentDistance, visibleSteps, toolchangeIndex, annotatedStep, excessFilamentLabel, bufferSpringPath, bufferPiston, interpolate, constrainFilament, duration, gearAngle, CommandHistory, traceText, homingPulse, bladeStroke, bowdenLengthLabel, toolheadReferences, coolingTubeMeasurements, dimensionParameter, lastEncoderReading, encoderReference, gateDevicePositions, purgeColor, purgeCoils, purgeStrandLength } from './playback.js';
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

test('purge starts after nozzle contact',()=>{
 const e={before:{...state(0,false),extruded_mm:0},state:{...state(5,false),layout:{nozzle:5},extruded_mm:10}};
 assert.equal(interpolate(e,.2).extruded_mm,0);
 assert.equal(interpolate(e,.5).extruded_mm,2.5);
});

test('nozzle retract releases coil while preserving deposited amount',()=>{
 const a={...state(5,false),extruded_mm:10,purge_base_mm:0,nozzle_color:'ff00ff'};
 const b={...state(3,false),extruded_mm:10,purge_base_mm:10,nozzle_color:'00ffff'};
 const mid=interpolate({before:a,state:b},.5);
 assert.equal(mid.purge_drop,.5);
 assert.equal(mid.extruded_mm,10);
 assert.equal(mid.purge_base_mm,0);
 assert.equal(mid.nozzle_color,'ff00ff');
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
 assert.equal(traceText(e), '29 · 1444.51s · homing move · GEAR: Homed after 14.5mm | requested: dist=25.0 · speed=80.0 · accel=400.0 · endstop=extruder');
 assert.match(traceText({...e,homed:false,distance:25}), /Did not home after 25.0mm/);
 assert.match(traceText({...e,homed:null}), /result unknown/);
});
test('trace formats synced, extruder, fallback and state events', () => {
 const e = {seq:31,time:1444.705,kind:'movement',telemetry:{start:{motor_mode:'synced'}},distance:25,speed:80,acceleration:400};
 assert.equal(traceText(e), '31 · 1444.70s · move · SYNCED GEAR+EXTRUDER: dist=25.0mm · speed=80.0 · accel=400.0');
 assert.equal(traceText({...e,telemetry:null,motors:{extruder:2.5},distance:2.5,speed:5,acceleration:null}), '31 · 1444.70s · move · EXTRUDER: dist=2.5mm · speed=5.0');
 assert.equal(traceText({seq:33,time:1445.105,kind:'state',label:'IN BOWDEN'}), '33 · 1445.11s · state changed · IN BOWDEN');
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
  assert.equal(bowdenLengthLabel(s), 'Bowden · 550mm');
  assert.equal(bowdenLengthLabel({...s, gate: 1}), 'Bowden · 625.3mm');
  assert.equal(bowdenLengthLabel({...s, unit: 'unit1', gate: 2}), 'Bowden · 700mm');
  const updated = structuredClone(s);
  updated.configuration.calibration.unit0.bowden_lengths['0'] = 560;
  assert.equal(bowdenLengthLabel(updated), 'Bowden · 560mm');
  assert.equal(bowdenLengthLabel(s), 'Bowden · 550mm');
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

test('dimension shortcuts select the parameter or configured macro variable', () => {
 const s={unit:'unit0',units:['unit0'],configuration:{parameters:{0:{form_tip_macro:'_MMU_FORM_TIP'}},
  macros:{'macro:_MMU_FORM_TIP_VARS:cooling_tube_length':10}}};
 assert.equal(dimensionParameter(s,'Cooling tube length'),'macro:_MMU_FORM_TIP_VARS:cooling_tube_length');
 assert.equal(dimensionParameter(s,'Extruder sensor'),'toolhead_entry_to_extruder');
 assert.equal(dimensionParameter(s,'Bowden'),'calibrated_bowden_length');
 assert.equal(dimensionParameter(s,'Nozzle'),null);
 s.configuration.parameters[0].form_tip_macro='_MMU_CUT_TIP';
 s.configuration.macros['macro:_MMU_CUT_TIP:blade_pos']=52;
 assert.equal(dimensionParameter(s,'Blade'),'macro:_MMU_CUT_TIP:blade_pos');
 assert.equal(dimensionParameter(s,'Cooling tube length'),null);
});

test('encoder reading persists through states and respects replay and unit',()=>{
 const events=[{state:{unit:'unit0',devices:{'unit0:encoder':{encoder_pos:12.3}}},telemetry:{outcome:{encoder_mm:2}}},
  {state:{unit:'unit0'}},{state:{unit:'unit0',devices:{'unit0:encoder':{encoder_pos:15.3}}},telemetry:{outcome:{encoder_mm:3}}}];
 assert.equal(lastEncoderReading(events,events[1],'unit0'),12.3);
 assert.equal(lastEncoderReading(events,events[2],'unit0'),15.3);
 assert.equal(lastEncoderReading(events,events[0],'unit1'),null);
});
test('cut fragment stays on retraction and moves only after contact',()=>{
 const a={...state(60,false),layout:{nozzle:100},retained_segment:{start:70,end:100,color:'ff0000'}};
 const b={...a,tips:[40]};
 assert.deepEqual(interpolate({before:a,state:b},.5).retained_segment,a.retained_segment);
 const c={...a,tips:[80],retained_segment:{start:80,end:100,color:'ff0000'}};
 assert.equal(interpolate({before:a,state:c},.25).retained_segment.start,70);
 assert.equal(interpolate({before:a,state:c},.75).retained_segment.start,75);
});

test('cut shortening happens at the blade stroke rather than sliding the fragment out',()=>{
 const before=state(100,false), after={...state(60,false),retained_segment:{start:60,end:100}};
 const event={kind:'cut',before,state:after};
 assert.strictEqual(interpolate(event,.2),before);
 assert.strictEqual(interpolate(event,.5),after);
});

test('purge blends through the final retained volume and deposited coils retain their colors',()=>{
 const s={purge_transition:{start_mm:0,retained_mm:7,mix_mm:2,old_color:'ff0000',new_color:'0000ff'},extruded_mm:8};
 assert.equal(purgeColor(s,5),'ff0000');
 assert.equal(purgeColor(s,6),'800080');
 assert.equal(purgeColor(s,7),'0000ff');
 const before=purgeCoils(s,1.75), after=purgeCoils({...s,extruded_mm:20},1.75);
 assert.equal(before.coils[0].color,after.coils[0].color);
 assert.equal(after.coils.at(-1).color,'0000ff');
 assert.equal(purgeCoils({...s,extruded_mm:.6},1.75).coils.length,0);
});

test('nozzle diameter converts feed length by volume without moving color boundaries',()=>{
 assert.equal(purgeStrandLength(4),76.5625);
 assert.equal(purgeStrandLength(4,.8),19.140625);
 assert.equal(purgeStrandLength(4,.1),1225);
 const s={extruded_mm:4,purge_transition:{start_mm:0,retained_mm:7,mix_mm:2,old_color:'ff0000',new_color:'0000ff'}};
 for (const diameter of [.1,.2,.4,.6,.8]) {
   assert.ok(purgeCoils(s,diameter).coils.every(coil=>coil.color==='ff0000'));
 }
 assert.notEqual(purgeCoils(s,.4).layers,purgeCoils(s,.8).layers);
});

test('G1 extrusions have their own trace type while MMU moves retain move',()=>{
 const event={seq:1,time:0,kind:'movement',distance:2,speed:5};
 assert.match(traceText({...event,source:'mmu_gcode'}), / · G1 · /);
 assert.match(traceText(event), / · move · /);
});
test('tip shape follows retraction and cut switches ownership at blade contact',()=>{
 const a={...state(100,false),tip_shapes:['square']};
 const b={...state(90,false),tip_shapes:['cone']};
 assert.deepEqual(interpolate({before:a,state:b},.5).tip_shapes,['cone']);
 const cut={kind:'cut',before:b,state:{...b,tip_shapes:['square'],retained_segment:{tip_shape:'cone'}}};
 assert.deepEqual(interpolate(cut,.3).tip_shapes,['cone']);
 assert.deepEqual(interpolate(cut,.5).tip_shapes,['square']);
 assert.equal(interpolate(cut,.5).retained_segment.tip_shape,'cone');
});

test('explicit CUT holds the blade until RELEASE',()=>{
 assert.equal(bladeStroke({kind:'cut',cutter:'toolhead',state:{cutter_pressed:true}},1,'toolhead'),1);
 assert.equal(bladeStroke({kind:'release',cutter:'toolhead'},.5,'toolhead'),.5);
 assert.equal(bladeStroke({kind:'release',cutter:'toolhead'},1,'toolhead'),0);
 assert.equal(duration({kind:'release'}),650);
});

test('residual finishes changing color when its volume is replaced, including replay',()=>{
 const plan={start_mm:10,retained_mm:2,mix_mm:2,old_color:'00ff00',new_color:'ff0000'};
 const a={...state(100,true),layout:{nozzle:100},extruded_mm:10,purge_transition:plan};
 const b={...a,extruded_mm:12,nozzle_color:'ff0000'};
 assert.equal(purgeColor(a,10),'00ff00');
 assert.equal(interpolate({before:a,state:b},.5).nozzle_color,'808000');
 assert.equal(purgeColor(b,12),'ff0000');
 assert.equal(purgeColor({...b,purge_transition:{...plan,retained_mm:0,mix_mm:0}},10),'ff0000');
});

test('retained fragment starts purge only when its front reaches the residual',()=>{
 const a={...state(10,false),layout:{nozzle:50},filament:{residual_mm:2},extruded_mm:0,
  retained_segment:{start:20,end:30,color:'ff0000'}};
 const b={...a,tips:[50],extruded_mm:12,retained_segment:null};
 assert.equal(interpolate({before:a,state:b},.5).extruded_mm,0);
 assert.equal(interpolate({before:a,state:b},.75).extruded_mm,2);
 assert.equal(interpolate({before:a,state:b},1).extruded_mm,12);
});

test('stationary extruder holds the nozzle tip through both gear wiggle directions',()=>{
 for (const delta of [.8,-.8]) {
  const a=state(100,true), b=state(100,true);
  const event={before:a,state:b,raw_before:state(110,true),raw_state:state(110+delta,true)};
  for (const fraction of [0,.25,.5,.75,1]) assert.equal(interpolate(event,fraction).tips[0],100);
 }
});

test('buffer consumes overtravel before leaving its stop',()=>{
 const snapshot=(total)=>({...state(800,false),unit:'unit0',bowden_unabsorbed_mm:Math.max(0,total-12),devices:{'unit0:buffer':{displacement:total,travel:Math.min(12,total),maximum:12,excess:Math.max(0,total-12),value:1}}});
 const e={before:snapshot(15),state:snapshot(9)};
 assert.equal(interpolate(e,.25).devices['unit0:buffer'].travel,12);
 assert.equal(interpolate(e,.25).bowden_unabsorbed_mm,1.5);
 assert.equal(interpolate(e,.5).devices['unit0:buffer'].travel,12);
 assert.equal(interpolate(e,.75).devices['unit0:buffer'].travel,10.5);
 assert.equal(interpolate(e,.75).bowden_unabsorbed_mm,0);
});

test('buffer piston honors centered switches, spring rest, analog readings and stops',()=>{
 const d={maximum:12,range:8,rest:0,travel:6,displacement:6,occupied:true};
 assert.equal(bufferPiston(d,true,false).position,2);
 assert.equal(bufferPiston(d,false,true).position,10);
 for (const rest of [0,6,12]) assert.equal(bufferPiston({...d,occupied:false,rest}).position,rest);
 assert.equal(bufferPiston({...d,proportional:true,value:.5}).position,8);
 assert.equal(bufferPiston({...d,proportional:true,value:-1}).position,2);
 assert.equal(bufferPiston({...d,proportional:true,value:1,displacement:11}).position,11);
 const beyond=bufferPiston({...d,travel:12,displacement:15});
 assert.equal(beyond.position,12); assert.equal(beyond.excess,3);
 assert.equal(bufferPiston({...d,proportional:true,value:-1,displacement:-3}).position,0);
});

test('spring coils keep attached endpoints and finite geometry at either travel limit',()=>{
 for(const [wall,edge] of [[428,441.2],[428,498.8],[512,441.2],[512,498.8],[428,429],[428,428]]) {
  const path=bufferSpringPath(wall,edge);
  assert.ok(path.startsWith(`M${wall} 52.5 H`));
  assert.ok(path.endsWith(` H${edge}`));
  assert.equal((path.match(/ C/g)||[]).length,10);
  assert.ok(!/NaN|Infinity/.test(path));
 }
});

 test('purge coil spacing scales with strand diameter while pile height stays bounded',()=>{
   let previousCount=Infinity;
   for(const diameter of [.1,.2,.4,.6,.8]) {
     // Compare the same point in the pile cycle for each nozzle.
     const feed=25/purgeStrandLength(1,diameter);
     const pile=purgeCoils({extruded_mm:feed},diameter);
     assert.ok(pile.coils.length<previousCount);
     assert.ok(Math.abs(pile.pitch-4*diameter*1.15)<1e-9);
     assert.ok(pile.layers*pile.pitch<=32);
     previousCount=pile.coils.length;
   }
 });

test('gate selection eases between lanes without interpolating physical filament travel',()=>{
 const before={gate:0,tips:[-20,-30],gate_colors:['ff0000','0000ff']};
 const state={...before,gate:1};
 const event={kind:'gate_changed',before,state};
 assert.equal(duration(event),1200);
 assert.equal(interpolate(event,0),before);
 assert.equal(interpolate(event,1),state);
 assert.equal(interpolate(event,.5).gate_focus,.5);
 assert.deepEqual(interpolate(event,.5).tips,before.tips);
 assert.equal(interpolate(event,.5).gate_path_opacity,0);
});

test('encoder reference follows configured endstop and signed offset',()=>{
 const sensors=[['unit0:mmu_shared_exit',{position:0}],['unit0:mmu_exit_2',{position:-20}]];
 const state={gate:2,encoder_reference:{endstop:'mmu_shared_exit',offset:15}};
 assert.deepEqual(encoderReference(state,sensors),{name:'unit0:mmu_shared_exit',offset:15});
 state.encoder_reference={endstop:'mmu_exit',offset:-12};
 assert.deepEqual(encoderReference(state,sensors),{name:'unit0:mmu_exit_2',offset:-12});
 assert.equal(encoderReference(state,sensors.slice(0,1)),null);
 state.encoder_reference.endstop='encoder';
 assert.equal(encoderReference(state,sensors),null);
 assert.equal(dimensionParameter(state,'Encoder offset'),'gate_endstop_to_encoder');
});

test('device stack collapses absent sensors and keeps signed encoder ordering',()=>{
 const sensors=[['mmu_exit_0',{position:0}],['unit0:mmu_shared_exit',{position:150}]];
 const state={gate:0,unit:'unit0',devices:{'unit0:encoder':{}},encoder_reference:{endstop:'mmu_shared_exit',offset:10}};
 assert.deepEqual(gateDevicePositions(state,sensors),{exit:260,sharedExit:228,encoder:196});
 assert.deepEqual(gateDevicePositions(state,sensors.slice(1)),{sharedExit:260,encoder:228});
 state.encoder_reference.offset=-10;
 assert.deepEqual(gateDevicePositions(state,sensors.slice(1)),{encoder:260,sharedExit:228});
 assert.deepEqual(gateDevicePositions(state,[]),{encoder:260});
 state.devices={};
 assert.deepEqual(gateDevicePositions(state,sensors),{exit:260,sharedExit:228});
 assert.deepEqual(gateDevicePositions(state,[]),{});
});

test('presence constraints stop forward travel at clear sensors and hold conflicting retractions',()=>{
 const sensor={name:'exit',position:20,state:false};
 assert.equal(constrainFilament(30,10,null,[sensor]).tip,19);
 assert.ok(constrainFilament(30,10,null,[sensor]).warnings.length);
 assert.equal(constrainFilament(30,null,null,[sensor]).tip,19);
 assert.equal(constrainFilament(10,30,null,[{...sensor,state:true}]).tip,30);
 assert.equal(constrainFilament(10,null,null,[{...sensor,state:true}]).tip,20);
 assert.equal(constrainFilament(30,10,null,[{...sensor,enabled:false}]).tip,30);
 assert.equal(constrainFilament(30,10,null,[{...sensor,state:null}]).tip,30);
 assert.equal(constrainFilament(10,30,null,[{...sensor,state:true}],[{start:15,end:25}]).tip,10);
 assert.equal(constrainFilament(30,10,25,[sensor]).tip,30);
 assert.equal(constrainFilament(19,10,null,[sensor],[],['exit mismatch']).tip,19);
});

test('excess filament distinguishes signed tension and compression', () => {
  assert.equal(excessFilamentLabel(-2), 'tension · -2.0mm');
  assert.equal(excessFilamentLabel(2), 'compression · +2.0mm');
});

test('toolchange navigation skips steps and uses strict adjacent markers', () => {
  const events = ['session','toolchange','movement','state','toolchange','movement'].map(kind=>({kind}));
  assert.equal(toolchangeIndex(events,3,-1),1);
  assert.equal(toolchangeIndex(events,4,-1),1);
  assert.equal(toolchangeIndex(events,1,1),4);
  assert.equal(toolchangeIndex(events,0,-1),-1);
  assert.equal(toolchangeIndex(events,4,1),-1);
});
test('manual navigation retains the selected step annotation', () => {
  const events = [{seq:1,kind:'movement',label:'Load'}, {seq:2,kind:'state',label:'IN EXTRUDER'}];
  assert.equal(annotatedStep(events,events[1],true),events[1]);
  assert.equal(annotatedStep(events,events[0],true),events[0]);
  assert.equal(annotatedStep(events,events[1],false),events[0]);
});

test('purge release has time to fall without shortening slower retractions', () => {
  const event = {kind:'movement',distance:-2,speed:20,before:{purge_base_mm:0},state:{purge_base_mm:4}};
  assert.equal(duration(event),650);
  assert.equal(duration({...event,speed:1}),2000);
  assert.equal(duration({...event,state:{purge_base_mm:0}}),120);
});

test('movement sync indication follows commanded motors from the first frame', () => {
  for (const mode of ['gear+extruder','synced','gear','extruder']) {
    const synced = ['gear+extruder','synced'].includes(mode);
    const e = {kind:'movement',before:{...state(0,false),synced:!synced},
      state:{...state(10,true),synced},telemetry:{start:{motor_mode:mode}}};
    for (const fraction of [0,.01,.5,.99,1])
      assert.equal(interpolate(e,fraction).synced,synced,`${mode} at ${fraction}`);
    assert.equal(e.before.synced,!synced);
  }
});

test('internal snapshots settle visible steps without changing animated travel', () => {
  const move = {seq:1,kind:'movement',label:'Load',before:state(0,false),state:state(10,true)};
  const hidden = {seq:2,kind:'state',label:'State changed',state:{...state(10,true),synced:false}};
  const next = {seq:3,kind:'state',label:'IN EXTRUDER',telemetry:{event:'filament_pos_changed'},state:state(10,true)};
  const steps = visibleSteps([move,hidden,next]);
  assert.deepEqual(steps.map(e=>e.seq),[1,3]);
  assert.equal(steps[0].state,move.state);
  assert.equal(interpolate(steps[0],.5).tips[0],5);
  assert.equal(interpolate(steps[0],1),hidden.state);
  assert.equal(steps[0].settled_state,hidden.state);
  assert.equal(move.settled_state,undefined);
  assert.equal(visibleSteps([move,{...hidden,seq:4,internal:true}])[0].settled_state,hidden.state);
});

test('successful reverse homing ignores the old false endpoint warning during animation', () => {
  const e = {kind:'movement',homing:true,homed:true,distance:-10,endstop:'toolhead',
    before:state(10,true),state:{...state(0,false),sensor_conflicts:['toolhead: movement past a clear sensor']}};
  assert.equal(interpolate(e,.5).tips[0],5);
  assert.deepEqual(interpolate(e,.5).sensor_conflicts,[]);
  assert.equal(interpolate({...e,homed:false},.5).sensor_conflicts.length,1);
});

test('encoder load keeps its endpoint when START OF BOWDEN reports overshoot',()=>{
 // Reduced from the reported trace: 100mm load, unchanged tip=112,
 // followed by an encoder overshoot observation of 77.341mm.
 const state=(tip,pos,overshoot=0)=>({gate:0,unit:'unit0',tips:[tip],filament_pos:pos,
   layout:{mmu_encoder:20,extruder_entry:700},encoder_reference:{endstop:'encoder',overshoot}});
 const parked={seq:0,kind:'session',state:state(12,0)};
 const move={seq:17,kind:'movement',before:parked.state,state:state(112,0),settled_state:state(112,2)};
 const start={seq:19,kind:'state',state:state(112,2,77.341)};
 const fast={seq:22,kind:'movement',before:start.state,state:state(699.999,2,77.341)};
 const end={seq:24,kind:'state',state:state(699.999,4,77.341)};
 const events=[parked,move,start,fast,end], scale=p=>200+p*.8177374;
 const at=(event,s)=>encoderFilamentDistance(events,event,s,s.tips[0],scale,210);
 assert.equal(at(parked,parked.state),192);
 assert.equal(at(move,move.state),at(move,move.settled_state));
 assert.equal(at(move,move.settled_state),at(start,start.state));
 assert.equal(at(start,start.state),at(fast,fast.before));
 assert.equal(at(fast,fast.state),at(end,end.state));
 assert.ok(at(start,start.state)>at(parked,parked.state));
 assert.equal(encoderFilamentDistance(events,fast,fast.state,700,scale,210),scale(700));
});

test('encoder overshoot in a mid-session baseline uses the Bowden scale',()=>{
 const s={gate:0,unit:'unit0',tips:[112],filament_pos:2,layout:{mmu_encoder:20,extruder_entry:700},encoder_reference:{overshoot:77.341}};
 const e={seq:0,kind:'session',state:s},scale=p=>200+p*.5;
 assert.equal(encoderFilamentDistance([e],e,s,112,scale,210),210+77.341*.5);
});

test('selected homing move shows its motor mode after temporary sync is restored',()=>{
 const event={kind:'movement',homing:true,telemetry:{start:{motor_mode:'gear+extruder'}}};
 const restored={synced:false};
 assert.equal(syncForDisplay(event,restored,true),true);
 assert.equal(syncForDisplay(event,restored,false),false);
 event.telemetry.start.motor_mode='gear';
 assert.equal(syncForDisplay(event,{synced:true},true),false);
 assert.equal(syncForDisplay({kind:'state'},restored,true),false);
});

test('parked state notification does not re-anchor the last encoder parking move',()=>{
 const state=(tip,pos)=>({gate:0,unit:'unit0',tips:[tip],filament_pos:pos,
   layout:{mmu_encoder:20,extruder_entry:700},encoder_reference:{endstop:'encoder'}});
 const initial={seq:0,kind:'session',state:state(-3,0)};
 const parking={seq:117,kind:'movement',state:state(-2.896,1)};
 const parked={seq:119,kind:'state',state:state(-2.896,0)};
 const events=[initial,parking,parked],scale=p=>200+p*.8;
 const at=e=>encoderFilamentDistance(events,e,e.state,e.state.tips[0],scale,210);
 assert.equal(at(parking),at(parked));
});

test('shared-exit parking, successful home and Bowden travel share one mapping',()=>{
 const scale=p=>200+p*.8;
 const at=tip=>switchFilamentDistance(tip,10,-100,550,180,scale);
 assert.equal(at(-90),172);
 assert.equal(at(10),180);
 assert.ok(at(9.99)<180);
 assert.ok(at(10.01)>180);
 assert.equal(at(550),scale(550));
 assert.equal(switchFilamentDistance(40,10,30,550,180,scale),188);
});

test('proportional reading controls the sensor range but allows outer modeled travel',()=>{
 for(const displacement of [3,6,9]) {
  const p=bufferPiston({maximum:12,range:8,travel:6,displacement,proportional:true,value:.5,occupied:true});
  assert.equal(p.position,8);assert.equal(p.excess,0);
 }
 for(const [displacement,position,excess] of [[-5,0,-5],[1,1,0],[11,11,0],[17,12,5]]) {
  const p=bufferPiston({maximum:12,range:8,displacement,proportional:true,value:.5,occupied:true});
  assert.equal(p.position,position);assert.equal(p.excess,excess);
 }
 assert.equal(bufferPiston({maximum:12,range:8,displacement:11,proportional:true,value:1}).position,11);
 assert.equal(bufferPiston({maximum:12,range:8,travel:0,displacement:0},false,true).position,10);
 assert.equal(bufferPiston({maximum:12,range:8,travel:6,displacement:6},true,false).position,2);
});
