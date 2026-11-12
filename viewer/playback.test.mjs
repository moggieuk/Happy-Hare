import { test } from 'node:test';
import assert from 'node:assert/strict';
import { timelineMarkers, homingHold, spoolTooltip, tweenTo } from './playback.js';

test('timeline labels distinguish loads, unloads and requested toolchanges',()=>{
 const marker=(command,tool,label=command)=>({kind:'toolchange',telemetry:{command,tool,label}});
 const events=[marker('MMU_LOAD',1),{kind:'movement'},marker('MMU_UNLOAD',1),
   marker('MMU_CHANGE_TOOL',1,'MMU_CHANGE_TOOL TOOL="5"'),
   marker('MMU_CHANGE_TOOL',5,'MMU_CHANGE_TOOL GATE=2'),{kind:'state',state:{tool:3}}];
 assert.deepEqual(timelineMarkers(events),[
   {index:0,label:'T1'},{index:2,label:'U'},{index:3,label:'T5'},{index:4,label:'T3'}]);
});
test('timeline retains ticks but omits labels at thirty operations',()=>{
 const events=Array.from({length:31},()=>({kind:'toolchange',telemetry:{command:'MMU_LOAD',tool:0}}));
 assert.ok(timelineMarkers(events.slice(0,29)).every(marker=>marker.label==='T0'));
 for (const count of [30,31]) {
   const markers=timelineMarkers(events.slice(0,count));
   assert.equal(markers.length,count);
   assert.ok(markers.every(marker=>marker.label===''));
 }
});
import { virtualSelectorLayout, spoolGrid, fittingSpoolGrid, toolheadProgress, toolheadDrawingRange, playbackFrame, motionFraction, switchFilamentDistance, syncForDisplay, encoderFilamentDistance, visibleSteps, toolchangeIndex, annotatedStep, excessFilamentLabel, bufferSpringPath, bufferPiston, interpolate, constrainFilament, duration, gearAngle, CommandHistory, traceText, homingTarget, homingPulse, bladeStroke, bowdenLengthLabel, toolheadReferences, coolingTubeMeasurements, dimensionParameter, lastEncoderReading, encoderReference, gateDevicePositions, purgeColor, purgeOutletColor, purgeCoils, purgeStrandLength } from './playback.js';
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
 assert.equal(homingTarget({...e,homed:null}), 'unit0:mmu_extruder');
 assert.equal(homingTarget({...e,homed:false}), 'unit0:mmu_extruder');
 assert.equal(homingTarget({...e,homing:0}), null);
 assert.equal(homingTarget(null), null);
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
 assert.equal(duration(event),2500);
 const frame=t=>playbackFrame(event,t,duration(event)).state;
 assert.equal(frame(550).gate_full_opacity,.5);
 assert.equal(frame(550).gate_focus,0);
 assert.equal(frame(1250).gate_full_opacity,0);
 assert.equal(frame(1250).gate_focus,.5);
 assert.equal(frame(1950).gate_full_opacity,.5);
 assert.equal(frame(1950).gate_focus,1);
 assert.equal(frame(1950).gate_retraction,1);
 assert.equal(frame(2350).gate_full_opacity,1);
 assert.equal(frame(2350).gate_retraction,.5);
 assert.equal(frame(1).gate_retraction>0,true);
 assert.equal(frame(2499).gate_retraction>0,true);
 assert.equal(frame(2500).gate,1);
 assert.equal(interpolate(event,0),before);
 assert.equal(interpolate(event,1),state);
 assert.equal(interpolate(event,.5).gate_focus,.5);
 assert.deepEqual(interpolate(event,.5).tips,before.tips);
 assert.equal(interpolate(event,.125).gate_focus,0);
 assert.equal(interpolate(event,.125).gate_retraction,.5);
 assert.equal(interpolate(event,.5).gate_retraction,1);
 assert.equal(interpolate(event,.875).gate_focus,1);
 assert.equal(interpolate(event,.875).gate_retraction,.5);
 assert.deepEqual(interpolate(event,.875).tips,state.tips);
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

test('a tip within the sensor tolerance of a clear sensor is clamped without a warning',()=>{
 const sensor={name:'extruder',position:20,state:false,tolerance:1};
 const near=constrainFilament(20.4,null,[sensor]);
 assert.equal(near.tip,20);assert.deepEqual(near.warnings,[]);
 const far=constrainFilament(22,null,[sensor]);
 assert.equal(far.tip,20);assert.equal(far.warnings.length,1);
 assert.equal(constrainFilament(20.4,null,[{...sensor,tolerance:undefined}]).warnings.length,1);
});

test('presence constraints use sensor boundaries without freezing at the previous tip',()=>{
 const sensor={name:'exit',position:20,state:false};
 assert.equal(constrainFilament(30,null,[sensor]).tip,20);
 assert.ok(constrainFilament(30,null,[sensor]).warnings.length);
 assert.equal(constrainFilament(30,null,[sensor]).tip,20);
 assert.equal(constrainFilament(10,null,[{...sensor,state:true}]).tip,20);
 assert.equal(constrainFilament(10,null,[{...sensor,state:true}]).tip,20);
 assert.equal(constrainFilament(30,null,[{...sensor,enabled:false}]).tip,30);
 assert.equal(constrainFilament(30,null,[{...sensor,state:null}]).tip,30);
 assert.equal(constrainFilament(10,null,[{...sensor,state:true}],[{start:15,end:25}]).tip,10);
 assert.equal(constrainFilament(30,25,[sensor]).tip,30);
 assert.equal(constrainFilament(19,null,[sensor],[],['exit mismatch']).tip,19);
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
 assert.equal(at(-90),148);
 assert.equal(at(10),180);
 assert.ok(at(9.99)<180);
 assert.ok(at(10.01)>180);
 assert.equal(at(550),scale(550));
 assert.equal(switchFilamentDistance(40,10,30,550,180,scale),189.6);
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

test('acceleration timing uses triangular, trapezoidal and sensor-stop profiles',()=>{
 const move={kind:'movement',distance:30,speed:50,acceleration:100};
 assert.ok(Math.abs(duration({...move,homing:true,homed:true})-850)<1e-8);
 assert.ok(Math.abs(duration(move)-1100)<1e-8);
 assert.ok(Math.abs(duration({...move,distance:10})-2*Math.sqrt(.1)*1000)<1e-8);
 assert.ok(Math.abs(duration({...move,distance:10,homing:true,homed:true})-Math.sqrt(.2)*1000)<1e-8);
 assert.ok(motionFraction(move,.1)<.1);
 assert.ok(Math.abs(motionFraction(move,.5)-.5)<1e-8);
 assert.equal(duration({...move,acceleration:null}),600);
 assert.equal(motionFraction({...move,acceleration:0},.3),.3);
});
test('acceleration changes only pacing, with exact signed movement endpoints',()=>{
 for(const distance of [-100,-10,10,100]) for(const homed of [false,true]) {
  const event={kind:'movement',distance,speed:50,acceleration:100,homing:true,homed};
  let previous=0;
  for(let i=0;i<=100;i++) {
   const f=motionFraction(event,i/100);
   assert.ok(f>=previous && f<=1);previous=f;
   assert.equal(motionFraction({...event,distance:-distance},i/100),f);
  }
  assert.equal(motionFraction(event,0)*distance,0*distance);
  assert.equal(motionFraction(event,1)*distance,distance);
  assert.equal(motionFraction(event,-.1),0);
  assert.equal(motionFraction(event,1.1),1);
 }
});


test('long gate offsets expand the stack proportionally within the straight Bowden',()=>{
 const sensors=[['mmu_exit_0',{position:0}],['unit0:mmu_shared_exit',{position:150}]];
 const state={gate:0,unit:'unit0',devices:{'unit0:encoder':{}},encoder_reference:{endstop:'mmu_shared_exit',offset:25,parking:-25}};
 const compact=gateDevicePositions(state,sensors);
 state.encoder_reference.parking=-62.5;
 const midway=gateDevicePositions(state,sensors);
 state.encoder_reference.parking=-100;
 const wide=gateDevicePositions(state,sensors);
 assert.equal(compact.sharedExit-midway.sharedExit,18);
 assert.equal(compact.sharedExit-wide.sharedExit,36);
 for (const offset of [-100,100]) {
  state.encoder_reference.offset=offset;
  const bounded=gateDevicePositions(state,sensors);
  assert.ok(Object.values(bounded).every(y=>y>=160 && y<=260));
  assert.ok(offset<0 ? bounded.encoder>bounded.sharedExit : bounded.encoder<bounded.sharedExit);
  state.encoder_reference.offset=offset*5;state.encoder_reference.parking=-500;
  assert.deepEqual(gateDevicePositions(state,sensors),bounded);
  state.encoder_reference.parking=-100;
 }
});

test('parked filament respects the encoder landmark on either side of its home sensor',()=>{
 const scale=p=>200+p*.8;
 for (const offset of [-80,80]) {
  const encoder={position:150+offset,distance:250+Math.sign(offset)*50};
  for (const parking of [-100,-40,40,100]) {
   const parked=150+parking;
   const at=tip=>switchFilamentDistance(tip,150,parking,550,250,scale,encoder);
   assert.equal(at(150),250);
   assert.equal(at(encoder.position),encoder.distance);
   assert.equal(Math.sign(at(parked)-encoder.distance),Math.sign(parked-encoder.position));
   let previous=at(0);
   for(let tip=1;tip<=550;tip++) { assert.ok(at(tip)>=previous);previous=at(tip); }
  }
 }
});


test('nozzle outlet and expelled material transition only after the residual is displaced',()=>{
 const s={purge_transition:{start_mm:0,retained_mm:2,mix_mm:2,old_color:'808182',new_color:'14ba5e'}};
 assert.equal(purgeOutletColor(s,0),'808182');
 assert.equal(purgeOutletColor(s,2),'808182');
 assert.notEqual(purgeOutletColor(s,3),'808182');
 assert.notEqual(purgeOutletColor(s,3),'14ba5e');
 assert.equal(purgeOutletColor(s,4),'14ba5e');
 assert.ok(purgeCoils({...s,extruded_mm:2}).coils.every(c=>c.color==='808182'));
 assert.equal(purgeColor(s,2),'14ba5e');
});


test('stepping uses the settled absolute encoder status, including resets',()=>{
 const e={seq:1,state:{unit:'unit0',devices:{'unit0:encoder':{encoder_pos:30.3}}},
  settled_state:{unit:'unit0',devices:{'unit0:encoder':{encoder_pos:690.2}}}};
 assert.equal(lastEncoderReading([e],e,'unit0'),690.2);
 e.settled_state.devices['unit0:encoder'].encoder_pos=0;
 assert.equal(lastEncoderReading([e],e,'unit0'),0);
});


test('piston and filament share acceleration and exact movement endpoints',()=>{
 const snapshot=(tip,travel)=>({...state(tip,false),unit:'unit0',layout:{nozzle:1000},sensors:{},
  devices:{'unit0:buffer':{maximum:12,range:8,travel,displacement:travel,excess:0,occupied:true}}});
 const e={kind:'movement',distance:4,speed:80,acceleration:100,before:snapshot(800,2),state:snapshot(804,6)};
 const ms=duration(e);
 for(const t of [.1,.25,.5,.75,.9]) {
  const frame=playbackFrame(e,t*ms,ms).state;
  const moved=motionFraction(e,t)*4;
  assert.ok(Math.abs(frame.tips[0]-800-moved)<1e-9);
  assert.ok(Math.abs(frame.devices['unit0:buffer'].travel-2-moved)<1e-9);
 }
 assert.ok(playbackFrame(e,ms/4,ms).state.devices['unit0:buffer'].travel<3);
 assert.equal(playbackFrame(e,ms,ms).state.devices['unit0:buffer'].travel,6);
});

test('a lone shared exit uses the straight section for long parking distances',()=>{
 const sensors=[['unit0:mmu_shared_exit',{position:10}]];
 const state={gate:0,unit:'unit0',devices:{},encoder_reference:{endstop:'mmu_shared_exit',parking:-25}};
 assert.deepEqual(gateDevicePositions(state,sensors),{sharedExit:260});
 state.encoder_reference.parking=-62.5;
 assert.deepEqual(gateDevicePositions(state,sensors),{sharedExit:210});
 state.encoder_reference.parking=-250;
 assert.deepEqual(gateDevicePositions(state,sensors),{sharedExit:160});
 state.devices['unit0:encoder']={};state.encoder_reference.offset=20;
 assert.deepEqual(gateDevicePositions(state,sensors),{sharedExit:192,encoder:160});
});

test('extruder height stays fixed while the entry sensor has a separate visual gap',()=>{
 const state={layout:{extruder_gears:700,nozzle:785.1},sensors:{'default:extruder':{position:683.4}},active_sensors:['default:extruder']};
 assert.deepEqual(toolheadDrawingRange(state),{entry:700,end:785.1,height:243,sensorEntry:683.4,sensorGap:35});
 state.layout.nozzle=740;state.sensors['default:extruder'].position=690;
 assert.equal(toolheadDrawingRange(state).height,243);
 assert.equal(toolheadDrawingRange(state).sensorGap,35);
 state.active_sensors=[];
 assert.equal(toolheadDrawingRange(state).sensorGap,0);
 assert.equal(toolheadDrawingRange(state).height,243);
});

test('toolhead sensor clearance preserves fixed endpoints and proportional spacing beyond it',()=>{
 const state={layout:{extruder_gears:700,nozzle:785},sensors:{'default:toolhead':{position:702}}};
 assert.equal(toolheadProgress(state,700),0);
 assert.equal(toolheadProgress(state,785),1);
 assert.equal(toolheadProgress(state,702)*243,34);
 assert.ok(toolheadProgress(state,710)>toolheadProgress(state,702));
 state.sensors['default:toolhead'].position=740;
 assert.equal(toolheadProgress(state,740),40/85);
});

test('triggered sensors silently advance the visual tip but conflicting sensors still warn',()=>{
 const triggered={name:'extruder',position:20,state:true};
 assert.deepEqual(constrainFilament(10,null,[triggered]),{tip:20,warnings:[]});
 assert.ok(constrainFilament(10,null,[triggered,{name:'exit',position:15,state:false}]).warnings.includes('Presence sensors disagree'));
});


test('gate swap restores its starting grip in either playback direction',()=>{
 for (const grip of [false,true]) for (const reverse of [false,true]) {
  const before={gate:0,tips:[-20,-30],mmu_gripped:!grip};
  const state={...before,gate:1,mmu_gripped:!grip};
  const event={kind:'gate_changed',before,state};
  for (const elapsed of [0,100,300,550,1250,1950,2300,2400,2500]) {
   const frame=playbackFrame(event,reverse ? duration(event)-elapsed : elapsed,duration(event),grip);
   const time=reverse ? duration(event)-elapsed : elapsed;
   assert.equal(frame.state.mmu_gripped,time===duration(event) ? !grip : time>0 && time<duration(event) ? false : grip);
  }
 }
});


test('completed gate swap restores recorded grip including folded updates',()=>{
 for(const starting of [false,true]) for(const recorded of [false,true]) {
  const before={gate:0,tips:[0,0],mmu_gripped:starting};
  const state={...before,gate:1,mmu_gripped:!recorded};
  const settled_state={...state,mmu_gripped:recorded};
  const event={kind:'gate_changed',before,state,settled_state};
  assert.equal(playbackFrame(event,duration(event),duration(event),starting).state.mmu_gripped,recorded);
 }
});

test('spool grid shows only the supplied unit gates in rows of seven',()=>{
 const grid=spoolGrid(Array.from({length:13},(_,i)=>i+8),10);
 assert.equal(grid.columns,7); assert.equal(grid.rows,2);
 assert.deepEqual(grid.lanes.map(l=>l.index),Array.from({length:13},(_,i)=>i+8));
 assert.equal(grid.lanes.filter(l=>l.row===0).length,7);
 assert.equal(grid.lanes.filter(l=>l.row===1).length,6);
});
test('selection wave changes lift without moving spool lanes',()=>{
 const gates=Array.from({length:10},(_,i)=>i);
 const a=spoolGrid(gates,0), b=spoolGrid(gates,5);
 assert.deepEqual(a.lanes.map(l=>[l.row,l.offset]),b.lanes.map(l=>[l.row,l.offset]));
 assert.equal(a.lanes[0].lift,10); assert.equal(b.lanes[5].lift,10);
 assert.equal(b.lanes[0].lift,0);
});

test('VirtualSelector branches cover only the active row and keep tips outside the hub',()=>{
 const state={selector_type:'VirtualSelector',unit_gates:Array.from({length:13},(_,i)=>i+4),gate:12};
 const layout=virtualSelectorLayout(state);
 assert.deepEqual(layout.branches.map(b=>b.gate),[10,11,12,13,14,15]);
 assert.equal(layout.active.gate,12);
 assert.ok(layout.parkDistance < layout.distanceAtY(layout.shoulder));
 assert.ok(layout.distanceAtY(170)>layout.distanceAtY(layout.hub));
 assert.equal(virtualSelectorLayout({...state,selector_type:'LinearSelector'}),null);
});
test('VirtualSelector shared hub stays fixed when selecting another gate',()=>{
 const state={selector_type:'VirtualSelector',unit_gates:[0,1,2,3],gate:0};
 const first=virtualSelectorLayout(state), last=virtualSelectorLayout({...state,gate:3});
 assert.notEqual(first.active.x,last.active.x);
 assert.equal(first.hub,last.hub);
 assert.equal(first.path.slice(first.path.indexOf('L620')),last.path.slice(last.path.indexOf('L620')));
});

test('VirtualSelector parking interpolation advances continuously to the shared sensor',()=>{
 const map=x=>switchFilamentDistance(x,10,-100,700,400,x=>x+200,null,168);
 assert.equal(map(-90),168);
 assert.equal(map(10),400);
 assert.ok(map(-89)>168 && map(-89)<171);
 assert.equal(map(-40),284);
 // Existing non-virtual symbolic parking remains unchanged.
 assert.notEqual(switchFilamentDistance(-90,10,-100,700,400,x=>x+200),168);
});

 test('eight-column spool layout expands left and wraps without resizing spacing',()=>{
  const gates=Array.from({length:9},(_,i)=>i);
  const eight=spoolGrid(gates.slice(0,8),0,8);
  assert.equal(eight.rows,1);
  assert.deepEqual(eight.lanes.map(l=>l.offset),[-4,-3,-2,-1,0,1,2,3]);
  const wrapped=spoolGrid(gates,0,8);
  assert.equal(wrapped.rows,2);
  assert.equal(wrapped.lanes[8].row,1);
  assert.equal(wrapped.lanes[8].offset,-4);
  assert.equal(spoolGrid(gates.slice(0,8),0,7).rows,2);
 });


test('non-virtual rows fill available width and grow left after reaching the right margin',()=>{
  const gates=Array.from({length:30},(_,i)=>i), angle=77*Math.PI/180;
  const small=fittingSpoolGrid(gates.slice(0,4),0,54,angle);
  assert.ok(Math.abs((small.left+small.right)/2-620)<1e-8);
  const large=fittingSpoolGrid(gates,0,54,angle);
  assert.ok(large.columns>8);
  assert.equal(large.right,836);
  assert.ok(large.left>=14);
  assert.ok(large.left-54<14);
  assert.equal(large.rows,Math.ceil(gates.length/large.columns));
  assert.ok(fittingSpoolGrid(gates,0,75,angle).columns<large.columns);
  assert.ok(fittingSpoolGrid(gates,0,54,angle,1100).columns>large.columns);
});
test('only a homing move that found its sensor holds the next step for 0.2s',()=>{
 const move=(extra)=>({kind:'movement',...extra});
 assert.equal(homingHold(move({homing:true,homed:true})),200);
 assert.equal(homingHold(move({homing:true,homed:false})),0);
 assert.equal(homingHold(move({homing:false,homed:true})),0);
 assert.equal(homingHold({kind:'gate_changed',homing:true,homed:true}),0);
});
test('a spool tooltip lists its gate map entry and leaves out unset values',()=>{
 const state={gate_status:[1,0],gate_colors:['ff0000','808182'],spool_percentages:[75,null],
  gate_map:{material:['PLA',''],filament_name:['Matte red',''],vendor:['',''],temperature:[210,-1],speed_override:[100,100],
   spool_id:[12,-1],td:[null,null],rfid:['',''],ttg_map:[1,0],endless_spool_groups:[0,1],initial_weight:[1000,-1],remaining_weight:[750,-1]}};
 const lines=spoolTooltip(state,0).split('\n');
 assert.deepEqual(lines,['Gate 0 · Available','Material: PLA','Filament: Matte red','Color: #ff0000','Temperature: 210 °C',
  'Spool ID: 12','Weight: 750 of 1000 g (75%)','Tool: T1','Endless spool group: 0']);
 state.gate_map.speed_override[1]=85;
 const empty=spoolTooltip(state,1).split('\n');
 assert.deepEqual(empty,['Gate 1 · Empty','Color: #808182','Speed override: 85%','Weight: unavailable','Tool: T0','Endless spool group: 1']); // Only a changed speed is listed
 assert.equal(spoolTooltip({gate_status:[]},0).split('\n')[0],'Gate 0 · Unknown'); // Old recordings carry no gate map
});
test('the gear spacing eases between grip states over half a second instead of snapping',()=>{
 let tw=tweenTo(null,17.25,0);
 assert.equal(tw.value,17.25);assert.ok(tw.done);
 tw=tweenTo(tw,26.25,1000);                      // Released: starts from the old spacing
 assert.equal(tw.value,17.25);assert.ok(!tw.done);
 const mid=tweenTo(tw,26.25,1250);
 assert.ok(mid.value>17.25 && mid.value<26.25);assert.ok(!mid.done);
 assert.equal(tweenTo(mid,26.25,1250).value,mid.value);
 const reversed=tweenTo(mid,17.25,1300);          // Interrupted: eases back from where it is
 assert.equal(reversed.value,mid.value);
 const end=tweenTo(tw,26.25,1500);
 assert.equal(end.value,26.25);assert.ok(end.done);
 assert.equal(tweenTo(end,26.25,9999).value,26.25);
});
