import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {TelemetryReplay,parseTelemetry,applyDelta} from './telemetry.js';
import {visibleSteps,interpolate,playbackFrame,purgeOutletColor,constrainFilament} from './playback.js';
const fixture=name=>JSON.parse(fs.readFileSync(new URL(`./fixtures/${name}.json`,import.meta.url)));
const header=()=>structuredClone(fixture('ercf-vvd')[0]);
function stream(pos=10) {
 const h=header();h.state.printer.mmu.filament_pos=pos;h.state.printer.mmu.filament_grip_state=1;
 h.state.printer.mmu.gate_color[0]='ff0000';
 for(const u of Object.values(h.state.printer.mmu_machine.units)) {
  u.extruder.filament_remaining_color='0000ff';u.extruder.filament_remaining=0;
 }
 const r=new TelemetryReplay();r.feed(h);matchPresence(r);return r;
}
function matchPresence(r) {
 const s=r.snapshot();
 for(const name of s.active_sensors) {
  const sensor=s.sensors[name];
  if (Number.isFinite(sensor.position) && !name.includes('filament_'))
    r.facts.printer.mmu.sensors[name].triggered=s.tips[s.gate]>=sensor.position;
 }
}
function event(r,event,fields={}) {r.feed({sequence:r.sequence+1,timestamp:r.sequence+1,event,...fields});}
function move(r,d,fields={}) {
 event(r,'movement_start',{move_id:1,label:'Test move',source:'move_filament',unit:r.facts.printer.mmu.unit,gate:0,
  motor_mode:'extruder',motors:['extruder'],requested_mm:d,speed_mm_s:10,homing:false,...fields});
 event(r,'movement_outcome',{move_id:1,actual_mm:d,elapsed_s:0,status:'submitted',...fields.outcome});
 return r.events.at(-1);
}
for(const name of ['ercf-vvd','emu']) {
 test(`${name}: batched live updates and offline JSONL reproduce identical animation`,()=>{
  const records=fixture(name), live=new TelemetryReplay(),offline=new TelemetryReplay();
  for(let i=0;i<records.length;i+=7) live.accept(records.slice(i,i+7));
  offline.accept(parseTelemetry(records.map(r=>JSON.stringify(r)).join('\n')));
  assert.deepEqual(live.events,offline.events);
  assert.ok(live.events.some(e=>e.kind==='movement'));
  const s=live.events.at(-1).state;
  assert.ok(s.tips[s.gate]>s.layout.extruder_gears);
  assert.ok(live.events.every(e=>e.state.tips.every(Number.isFinite)));
  for(const e of live.events) for(const f of [0,.25,.5,.75,1]) {
    const drawn=interpolate(e,f);
    assert.ok(drawn.tips.every(Number.isFinite));
  }
 });
}
test('real cut sequence leaves a separate fragment through pushback and unload',()=>{
 const r=new TelemetryReplay();r.accept(fixture('ercf-vvd'));
 const cut=r.events.find(e=>e.kind==='cut');
 assert.ok(cut.state.retained_segment);
 assert.equal(cut.state.tip_shapes[cut.state.gate],'square');
 const unload=r.events.find(e=>e.seq>cut.seq && e.kind==='movement' && e.label.includes('Fast unloading'));
 assert.ok(unload.state.retained_segment);
 assert.ok(unload.state.tips[unload.state.gate]<unload.state.retained_segment.start);
 assert.equal(r.events.at(-1).state.retained_segment,null);
});
test('G1 purge changes residual from old to new colour without a release event',()=>{
 const r=stream();move(r,20,{source:'mmu_gcode'});
 assert.equal(r.events.at(-1).state.nozzle_color,'ff0000');
 assert.ok(r.events.at(-1).state.extruded_mm>0);
 const retract=move(r,-2,{source:'mmu_gcode'});
 assert.equal(retract.state.purge_base_mm,retract.state.extruded_mm);
 const before=retract.state.extruded_mm;
 move(r,2,{source:'mmu_gcode'});
 assert.equal(r.events.at(-1).state.extruded_mm,before+2);
});
test('free gear motion cannot create excess bowden compression',()=>{
 const r=stream(3);move(r,5,{motor_mode:'gear',motors:['gear']});
 assert.equal(r.events.at(-1).state.bowden_unabsorbed_mm,0);
});
test('gripped filament remains at the extruder during gear-only oscillation',()=>{
 const r=stream();const initial=r.tips[0];
 move(r,-5,{motor_mode:'gear',motors:['gear']});
 assert.equal(r.tips[0],initial);assert.equal(r.events.at(-1).state.bowden_unabsorbed_mm,-5);
 move(r,5,{motor_mode:'gear',motors:['gear']});
 assert.equal(r.tips[0],initial);assert.equal(r.events.at(-1).state.bowden_unabsorbed_mm,0);
});
test('successful reverse homing clears the sensor and moves both motors',()=>{
 const r=stream();const target=r.facts.printer.mmu.active_sensors.find(n=>n.endsWith(':extruder'));
 assert.ok(target);
 for(const name of r.facts.printer.mmu.active_sensors) if(name.endsWith(':toolhead')) r.facts.printer.mmu.sensors[name].triggered=false;
 const e=move(r,-15,{motor_mode:'gear+extruder',homing:true,trigger_state:false,endstop:target,outcome:{homed:true}});
 assert.equal(e.state.sensors[target].state,false);
 assert.ok(e.state.tips[0]<e.state.sensors[target].position);
 assert.ok(Object.keys(e.motors).length>=2);
 assert.equal(interpolate(e,.5).synced,true);
});
test('per-move encoder measurement never overwrites absolute encoder status',()=>{
 const r=stream();const key=`${r.facts.printer.mmu.unit}:encoder`;
 const e=move(r,1,{outcome:{encoder_mm:123}});
 assert.notEqual(e.before.devices[key].encoder_pos,123);
 assert.equal(e.state.devices[key].encoder_pos,e.before.devices[key].encoder_pos);
});
test('configuration changes affect subsequent steps, never earlier snapshots',()=>{
 const r=stream();const initial=structuredClone(r.events[0]);
 event(r,'state_delta',{changes:[{path:['macros','_MMU_CUT_TIP_VARS','blade_pos'],value:12}]});
 const config=r.events.at(-1);move(r,1);
 assert.equal(config.kind,'config');
 assert.equal(config.state.layout.nozzle-config.state.cutters[0].position,12);
 assert.deepEqual(r.events[0],initial);
 assert.deepEqual(r.events.at(-1).before.cutters,config.state.cutters);
});
test('status-only changes fold into a step without navigation stops',()=>{
 const r=stream();event(r,'drive_sync_changed');
 assert.equal(visibleSteps(r.events).length,1);
});
test('session and sequence validation rejects unsupported and incomplete captures',()=>{
 assert.throws(()=>parseTelemetry('{"event":"movement_start"}'),/Expected|session/);
 const h=header();h.version=1;assert.throws(()=>new TelemetryReplay().feed(h),/Unsupported/);
 const r=stream();assert.throws(()=>r.feed({event:'cut',sequence:99,timestamp:1}),/gap/);
 assert.throws(()=>event(stream(),'movement_outcome',{move_id:4}),/no start/);
});
test('malicious delta paths cannot modify object prototypes',()=>{
 assert.throws(()=>applyDelta({},[{path:['__proto__','polluted'],value:true}]),/Invalid/);
 assert.throws(()=>applyDelta({},[{path:['constructor','prototype','polluted'],value:true}]),/Invalid/);
 assert.equal({}.polluted,undefined);
});
test('checkpoint accepts orphan outcome and starts inference afresh',()=>{
 const h=header();h.checkpoint=true;h.sequence=99;
 const r=new TelemetryReplay();r.feed(h);event(r,'movement_outcome',{move_id:2});
 assert.match(r.events[0].label,/earlier motion unavailable/);
});

test('proportional compression home persists through equal gear/extruder travel',()=>{
 const h=structuredClone(fixture('emu')[0]);h.state.printer.mmu.filament_pos=6;h.state.printer.mmu.filament_grip_state=1;
 const r=new TelemetryReplay();r.feed(h);matchPresence(r);
 const target=r.facts.printer.mmu.active_sensors.find(n=>n.endsWith(':filament_compression'));
 assert.ok(target);
 const home=move(r,4,{motor_mode:'gear',homing:true,trigger_state:true,endstop:target,outcome:{homed:true}});
 const key=`${home.state.unit}:buffer`,device=home.state.devices[key];
 assert.equal(device.value,1);
 assert.equal(device.travel,(device.maximum+device.range)/2);
 const next=move(r,10,{motor_mode:'gear+extruder'});
 assert.equal(next.state.devices[key].travel,device.travel);
 assert.equal(next.state.devices[key].value,1);
});

for(const suffix of [':extruder',':toolhead']) for(const direction of [1,-1]) {
 test(`${suffix} preserves modeled travel while constraining display (${direction})`,()=>{
  const r=stream(),s=r.snapshot();
  const target=s.active_sensors.find(n=>n.endsWith(suffix));
  assert.ok(target);
  const position=s.sensors[target].position;
  r.tips[0]=position-direction*2;
  r.facts.printer.mmu.sensors[target].triggered=direction<0;
  const e=move(r,direction*7,{motor_mode:'gear+extruder'});
  const expected=position+direction*5;
  assert.ok(Math.abs(e.state.tips[0]-expected)<1e-8);
  const display=state=>constrainFilament(state.tips[0],null,null,[{...state.sensors[target],name:target}]);
  const blocked=display(e.state);
  assert.ok(blocked.warnings.length);
  assert.ok(direction>0 ? blocked.tip<position : blocked.tip>=position);
  event(r,'state_delta',{changes:[{path:['printer','mmu','sensors',target,'triggered'],value:direction>0}]});
  const corrected=r.snapshot();
  assert.equal(corrected.tips[0],e.state.tips[0]);
  assert.deepEqual(display(corrected),{tip:expected,warnings:[]});
 });
}

test('a header-only JSONL capture can be loaded before any motion occurs',()=>{
 const records=parseTelemetry(JSON.stringify(header())+'\n');
 assert.equal(records.length,1);
 const r=new TelemetryReplay();r.accept(records);
 assert.equal(r.events[0].kind,'session');
});

test('encoder loading uses calibrated travel rather than schematic parking distance',()=>{
 // Reported load: park -23mm, load 100mm, then feed 602.659mm.
 const h=header();h.state.printer.mmu.filament_pos=0;
 h.state.printer.mmu_machine.units.unit0.parameters.gate_parking_distance=-23;
 const r=new TelemetryReplay();r.feed(h);
 assert.equal(r.tips[0],20-23);
 move(r,100,{motor_mode:'gear',motors:['gear']});
 event(r,'filament_pos_changed',{filament_pos:2,filament_pos_name:'START OF BOWDEN',homing_overshoot_mm:77.341});
 const fast=move(r,602.659,{motor_mode:'gear',motors:['gear']});
 assert.ok(Math.abs(fast.state.tips[0]-699.659)<1e-9);
 assert.deepEqual(fast.state.sensor_conflicts,[]);
 assert.equal(fast.state.sensors['default:extruder'].state,false);
});

test('in-move bookkeeping cannot attach future sensor readings to the preceding step',()=>{
 const r=stream(4),name='default:extruder';
 r.facts.printer.mmu.sensors[name].triggered=false;
 event(r,'filament_pos_changed',{filament_pos:4,filament_pos_name:'END OF BOWDEN'});
 const previous=visibleSteps(r.events).at(-1);
 event(r,'movement_start',{move_id:17,label:'Next homing move',gate:0,unit:'unit0',motor_mode:'gear+extruder',motors:['gear','extruder'],requested_mm:10});
 event(r,'state_delta',{changes:[{path:['printer','mmu','sensors',name,'triggered'],value:true}]});
 event(r,'drive_sync_changed',{mode:0});
 assert.equal(r.events.at(-1).internal,true);
 assert.equal(r.events.at(-1).during_move,true);
 assert.equal(r.events.at(-1).telemetry.event,'drive_sync_changed');
 const during=visibleSteps(r.events).at(-1);
 assert.equal(during.seq,previous.seq);
 assert.equal((during.settled_state || during.state).sensors[name].state,false);
 event(r,'movement_outcome',{move_id:17,actual_mm:10,status:'completed'});
 assert.equal(r.events.at(-1).state.sensors[name].state,true);
});

 test('returning a retract purges only after reaching the retained residual',()=>{
  const r=stream();const s=r.snapshot();
  r.tips[0]=s.layout.nozzle-s.filament.residual_mm;
  const e=move(r,2,{source:'mmu_gcode'});
  assert.ok(Math.abs(e.state.extruded_mm-2)<1e-8);
  assert.ok(interpolate(e,.5).extruded_mm>.9);
  move(r,-5,{source:'mmu_gcode'});move(r,5,{source:'mmu_gcode'});
  assert.ok(Math.abs(r.snapshot().extruded_mm-4)<1e-8);
 });
 test('parked notification preserves the measured final parking endpoint',()=>{
  const r=stream(0);r.tips[0]=-2.896;
  event(r,'filament_pos_changed',{filament_pos:0,filament_pos_name:'UNLOADED AND PARKED'});
  assert.equal(r.snapshot().tips[0],-2.896);
 });

for (const always of [false,true]) test(`extruder-only motion respects MMU grip (always gripped: ${always})`,()=>{
 const r=stream();const m=r.facts.printer.mmu;
 r.facts.printer.mmu_machine.units[m.unit].filament_always_gripped=always;
 event(r,'state_delta',{changes:[{path:['printer','mmu','filament_grip_state'],value:0}]});
 move(r,-28.5,{source:'mmu_gcode'});
 assert.equal(r.snapshot().bowden_unabsorbed_mm,always ? 28.5 : 0);
 move(r,14.25,{source:'mmu_gcode'});
 assert.equal(r.snapshot().bowden_unabsorbed_mm,always ? 14.25 : 0);
});
test('releasing the MMU clears stored excess before the next move',()=>{
 const r=stream();move(r,3,{motor_mode:'gear'});
 assert.equal(r.snapshot().bowden_unabsorbed_mm,3);
 event(r,'state_delta',{changes:[{path:['printer','mmu','filament_grip_state'],value:0}]});
 assert.equal(r.events.at(-1).state.bowden_unabsorbed_mm,0);
});
test('legacy recordings do not invent excess without grip evidence',()=>{
 const r=stream();delete r.facts.printer.mmu.filament_grip_state;
 move(r,-28.5,{source:'mmu_gcode'});
 assert.equal(r.snapshot().bowden_unabsorbed_mm,0);
});

test('compression homing reaches the gears before expanding the buffer',()=>{
 const h=structuredClone(fixture('emu')[0]);h.state.printer.mmu.filament_grip_state=1;
 const unit=h.state.printer.mmu_machine.units[h.state.printer.mmu.unit];
 Object.assign(unit.buffer,{buffer_maxrange:12,buffer_range:8,buffer_spring_state:'tension',proportional:false,value:null});
 const r=new TelemetryReplay();r.feed(h);
 r.tips[0]=r.snapshot().layout.extruder_gears-18;
 matchPresence(r);
 for(const [name,s] of Object.entries(r.facts.printer.mmu.sensors)) {
  if(name.endsWith('filament_compression'))s.triggered=false;
  if(name.endsWith('filament_tension'))s.triggered=true;
 }
 const target=r.facts.printer.mmu.active_sensors.find(n=>n.endsWith(':filament_compression'));
 const e=move(r,30,{motor_mode:'gear',homing:true,trigger_state:true,endstop:target,outcome:{homed:true,sensors:{[target]:true}}});
 const key=`${e.state.unit}:buffer`;
 assert.equal(interpolate(e,.3).devices[key].travel,0);
 assert.equal(interpolate(e,.6).tips[0],e.state.layout.extruder_gears);
 assert.equal(interpolate(e,.6).devices[key].travel,0);
 assert.ok(interpolate(e,.8).devices[key].travel>0);
 assert.equal(interpolate(e,.8).tips[0],e.state.layout.extruder_gears);
 assert.equal(e.state.devices[key].travel,10);
 assert.equal(interpolate(e,.8).sensors[target].state,false);
 assert.equal(e.state.sensors[target].state,true);
});

for (const spring of ['tension','none']) test(`free buffer relaxes at filament speed toward ${spring} rest`,()=>{
 const h=structuredClone(fixture('emu')[0]);h.state.printer.mmu.filament_grip_state=1;
 const u=h.state.printer.mmu_machine.units[h.state.printer.mmu.unit];
 Object.assign(u.buffer,{buffer_maxrange:12,buffer_range:8,buffer_spring_state:spring,proportional:false,value:null});
 const r=new TelemetryReplay();r.feed(h);const gears=r.snapshot().layout.extruder_gears;
 r.tips[0]=gears+5;r.bufferPositions[0]=10;matchPresence(r);
 for(const [n,s] of Object.entries(r.facts.printer.mmu.sensors))if(n.includes('filament_'))s.triggered=false;
 r.bufferPositions[0]=10;
 const e=move(r,-8,{motor_mode:'gear+extruder'});const key=`${e.state.unit}:buffer`;
 assert.equal(interpolate(e,.5).devices[key].travel,10);
 assert.equal(interpolate(e,.75).devices[key].travel,9);
 assert.equal(e.state.devices[key].travel,7);
 const next=move(r,-30,{motor_mode:'gear'});
 assert.equal(interpolate(next,.5).devices[key].travel,spring==='tension'?0:6);
 assert.equal(next.state.devices[key].travel,spring==='tension'?0:6);
});

test('gate-homed notification preserves the clear side of a reverse-home endpoint',()=>{
 const r=stream(3),m=r.facts.printer.mmu,u=r.facts.printer.mmu_machine.units[m.unit];
 u.parameters.gate_homing_endstop='mmu_shared_exit';
 const target=`${m.unit}:mmu_shared_exit`;
 m.sensors[target]={triggered:true,enabled:true};m.active_sensors.push(target);u.sensors.push(target);
 r.tips[0]=50;matchPresence(r);
 const homed=move(r,-40,{motor_mode:'gear',homing:true,trigger_state:false,endstop:target,outcome:{homed:true}});
 assert.equal(homed.state.tips[0],9.999);
 event(r,'filament_pos_changed',{filament_pos:1,filament_pos_name:'HOMED AT GATE'});
 assert.equal(r.events.at(-1).state.tips[0],9.999);
 assert.equal(r.events.at(-1).state.sensors[target].state,false);
});

test('triggered compression sensor holds a freed buffer against spring return',()=>{
 const h=structuredClone(fixture('emu')[0]);h.state.printer.mmu.filament_grip_state=1;
 const u=h.state.printer.mmu_machine.units[h.state.printer.mmu.unit];
 Object.assign(u.buffer,{buffer_maxrange:12,buffer_range:8,buffer_spring_state:'tension',proportional:false,value:null});
 const r=new TelemetryReplay();r.feed(h);r.tips[0]=r.snapshot().layout.extruder_gears-5;matchPresence(r);
 const target=r.facts.printer.mmu.active_sensors.find(n=>n.endsWith(':filament_compression'));
 for(const [n,s] of Object.entries(r.facts.printer.mmu.sensors))if(n.includes('filament_'))s.triggered=n===target;
 r.bufferPositions[0]=10;
 const e=move(r,-30,{motor_mode:'gear'}),key=`${e.state.unit}:buffer`;
 for(const f of [0,.1,.5,1]) {
  const s=interpolate(e,f);assert.equal(s.devices[key].travel,10);assert.equal(s.sensors[target].state,true);
 }
});

test('symbolic MMU sensor spacing cannot block a valid final parking retract',()=>{
 const r=stream(3),m=r.facts.printer.mmu,u=r.facts.printer.mmu_machine.units[m.unit];
 u.parameters.gate_homing_endstop='mmu_shared_exit';u.parameters.gate_parking_distance=-100;
 for(const [n,triggered] of [[`${m.unit}:mmu_shared_exit`,false],['mmu_exit_0',true]]) {
  m.sensors[n]={triggered,enabled:true};if(!m.active_sensors.includes(n))m.active_sensors.push(n);
 }
 r.tips[0]=9.999;
 const e=move(r,-100,{motor_mode:'gear'});
 assert.ok(Math.abs(e.state.tips[0]+90.001)<1e-8);
 assert.deepEqual(e.state.sensor_conflicts,[]);
});

test('purging a formed tip keeps its cone while advancing into the residual',()=>{
 const r=stream();r.tips[0]=r.snapshot().layout.nozzle-2;r.shapes[0]='cone';
 const e=move(r,2,{source:'mmu_gcode'});
 const frames=[.1,.5,.9].map(f=>interpolate(e,f));
 assert.ok(frames.every(s=>s.cone_opacity === undefined));
 assert.ok(frames[0].tips[0]<frames[1].tips[0]);
 assert.ok(frames[1].tips[0]<frames[2].tips[0]);
 assert.ok(frames.every(s=>s.tip_shapes[0]==='cone'));
 assert.equal(e.before.tip_shapes[0],'cone');assert.equal(e.state.tip_shapes[0],'square');
 assert.equal(interpolate(e,1),e.state);
 assert.ok(Math.abs(e.state.extruded_mm-2)<1e-8);
 assert.equal(e.state.tips[0],e.state.layout.nozzle);
});


test('buffer excess survives the settled snapshot and small nonzero values',()=>{
 const h=structuredClone(fixture('emu')[0]);h.state.printer.mmu.filament_grip_state=1;
 const u=h.state.printer.mmu_machine.units[h.state.printer.mmu.unit];
 Object.assign(u.buffer,{buffer_maxrange:12,buffer_range:8,proportional:false,value:null});
 const r=new TelemetryReplay();r.feed(h);r.tips[0]=r.snapshot().layout.extruder_gears+5;matchPresence(r);
 for(const [n,s] of Object.entries(r.facts.printer.mmu.sensors))if(n.includes('filament_'))s.triggered=false;
 for(const excess of [-2,-.01,.01,2]) {
  r.bufferPositions[0]=excess<0 ? excess : 12+excess;
  const a=r.snapshot();
  assert.ok(Math.abs(a.bowden_unabsorbed_mm-excess)<1e-9);
  const e=move(r,1,{motor_mode:'gear+extruder'});
  for(const f of [0,.5,1]) assert.ok(Math.abs(interpolate(e,f).bowden_unabsorbed_mm-excess)<1e-9);
 }
});


test('purge finishes with a color-only transition before retraction',()=>{
 const r=stream();r.tips[0]=r.snapshot().layout.nozzle-2;r.shapes[0]='cone';
 const unit=r.facts.printer.mmu_machine.units[r.facts.printer.mmu.unit];
 unit.extruder.filament_remaining_color='ff0000';
 const purge=move(r,2,{source:'mmu_gcode'});
 assert.equal(purge.state.nozzle_outlet_color,'ff0000');
 assert.equal(playbackFrame(purge,100,100).state.nozzle_outlet_color,'0000ff');
 assert.equal(playbackFrame(purge,275,100).state.nozzle_outlet_color,'800080');
 assert.equal(playbackFrame(purge,100,100).state.cone_edge_opacity,1);
 assert.equal(playbackFrame(purge,275,100).state.cone_edge_opacity,.5);
 assert.equal(playbackFrame(purge,450,100).state.cone_edge_opacity,0);
 assert.equal(playbackFrame(purge,450,100).state.nozzle_outlet_color,'ff0000');
 for(const elapsed of [100,275,450]) assert.equal(playbackFrame(purge,elapsed,100).state.tips[0],purge.state.tips[0]);
 const retract=move(r,-2,{source:'mmu_gcode'});
 assert.equal(retract.state.nozzle_outlet_color,'ff0000');
 assert.equal(interpolate(retract,.5).nozzle_outlet_color,'ff0000');
 assert.equal(interpolate(retract,.25).cone_edge_opacity,.25);
 assert.equal(interpolate(retract,.75).cone_edge_opacity,.75);
 assert.deepEqual(retract.state.purge_transition,purge.state.purge_transition);
 assert.equal(retract.state.extruded_mm,purge.state.extruded_mm);
});


test('mid-move buffer switch update does not count gear travel twice',()=>{
 const h=structuredClone(fixture('emu')[0]);h.state.printer.mmu.filament_grip_state=1;
 const u=h.state.printer.mmu_machine.units[h.state.printer.mmu.unit];
 Object.assign(u.buffer,{buffer_maxrange:12,buffer_range:8,proportional:false,value:null});
 const r=new TelemetryReplay();r.feed(h);r.tips[0]=r.snapshot().layout.nozzle;matchPresence(r);
 for(const [n,s] of Object.entries(r.facts.printer.mmu.sensors))if(n.includes('filament_'))s.triggered=false;
 r.bufferPositions[0]=6;
 const tension=Object.keys(r.facts.printer.mmu.sensors).find(n=>n.endsWith('filament_tension'));
 event(r,'movement_start',{move_id:99,source:'move_filament',unit:r.facts.printer.mmu.unit,gate:0,
  motor_mode:'gear',motors:['gear'],requested_mm:-4,speed_mm_s:80,homing:false});
 event(r,'state_delta',{changes:[{path:['printer','mmu','sensors',tension,'triggered'],value:true}]});
 assert.equal(r.snapshot().devices[`${r.facts.printer.mmu.unit}:buffer`].travel,2);
 event(r,'movement_outcome',{move_id:99,actual_mm:-4,elapsed_s:.1,status:'completed'});
 const e=r.events.at(-1),key=`${e.state.unit}:buffer`;
 assert.equal(e.before.devices[key].displacement,6);
 assert.equal(e.state.devices[key].displacement,2);
 assert.equal(e.state.bowden_unabsorbed_mm,0);
 for(const f of [0,.25,.5,.75,1]) assert.equal(interpolate(e,f).bowden_unabsorbed_mm,0);
});


test('cone edge starts dissolving on nozzle arrival inside a longer purge move',()=>{
 const r=stream();r.tips[0]=r.snapshot().layout.nozzle-2;r.shapes[0]='cone';
 const e=move(r,4,{source:'mmu_gcode'});
 assert.equal(playbackFrame(e,400,1000).state.cone_edge_opacity,1);
 assert.ok(Math.abs(playbackFrame(e,675,1000).state.cone_edge_opacity-.5)<1e-6);
 assert.equal(playbackFrame(e,900,1000).state.cone_edge_opacity,0);
});


test('manual extrusion after completed purge contacts residual and emits the settled color',()=>{
 const r=stream();r.tips[0]=r.snapshot().layout.nozzle-2;
 r.facts.printer.mmu_machine.units[r.facts.printer.mmu.unit].extruder.filament_remaining_color='ff0000';
 const first=move(r,2,{source:'mmu_gcode'});
 move(r,-2,{source:'mmu_gcode'});
 const next=move(r,1,{source:'mmu_gcode'});
 assert.equal(next.purge_approach_mm,0);
 assert.equal(next.state.extruded_mm-first.state.extruded_mm,1);
 assert.ok(next.state.tips[0]<next.state.layout.nozzle);
 assert.equal(purgeOutletColor(next.state,2.5),'ff0000');
 assert.equal(purgeOutletColor(next.state,1),'0000ff');
 assert.equal(first.state.purge_transition.settled_mm,2);
});


test('Bowden calibration ends at gears; entry offset only moves the entry sensor',()=>{
 for(const [endstop,origin] of [['mmu_exit',0],['mmu_shared_exit',10],['encoder',20]]) {
  for(const offset of [0,8.9,16.6]) {
   const h=header(),m=h.state.printer.mmu,u=h.state.printer.mmu_machine.units[m.unit];
   u.parameters.gate_homing_endstop=endstop;
   u.bowden_lengths[m.gate]=700;
   u.toolhead.parameters.toolhead_entry_to_extruder=offset;
   m.sensors.extruder={triggered:false,enabled:true};
   const r=new TelemetryReplay();r.feed(h);const s=r.snapshot();
   assert.equal(s.layout.extruder_gears,origin+700);
   assert.equal(s.sensors.extruder.position,origin+700-offset);
   assert.ok(Math.abs(s.layout.nozzle-s.layout.extruder_gears-u.toolhead.parameters.toolhead_extruder_to_nozzle)<1e-9);
  }
 }
});


test('free Bowden retraction consumes buffer return before tip travel on one acceleration curve',()=>{
 const h=structuredClone(fixture('emu')[0]);h.state.printer.mmu.filament_grip_state=1;
 const u=h.state.printer.mmu_machine.units[h.state.printer.mmu.unit];
 Object.assign(u.buffer,{buffer_maxrange:13,buffer_range:8,buffer_spring_state:'tension',proportional:false,value:null});
 const r=new TelemetryReplay();r.feed(h);r.tips[0]=r.snapshot().layout.extruder_gears-8;matchPresence(r);
 for(const [n,s] of Object.entries(r.facts.printer.mmu.sensors))if(n.includes('filament_'))s.triggered=false;
 r.bufferPositions[0]=2.5;
 const e=move(r,-20,{motor_mode:'gear',speed_mm_s:80,acceleration_mm_s2:100});
 assert.equal(e.buffer_takeup_mm,2.5);
 assert.equal(e.state.tips[0],e.before.tips[0]-17.5);
 const held=interpolate(e,.1),moving=interpolate(e,.2);
 assert.equal(held.tips[0],e.before.tips[0]);
 assert.equal(held.devices[`${e.state.unit}:buffer`].travel,.5);
 assert.equal(moving.tips[0],e.before.tips[0]-1.5);
 assert.equal(moving.devices[`${e.state.unit}:buffer`].travel,0);
});


test('contact is not grip, and a delayed tension update cannot erase buffer travel',()=>{
 const h=structuredClone(fixture('emu')[0]);h.state.printer.mmu.filament_grip_state=1;
 const u=h.state.printer.mmu_machine.units[h.state.printer.mmu.unit];
 Object.assign(u.buffer,{buffer_maxrange:13,buffer_range:8,buffer_spring_state:'tension',proportional:false,value:null});
 const r=new TelemetryReplay();r.feed(h);const gears=r.snapshot().layout.extruder_gears;
 r.tips[0]=gears;matchPresence(r);r.bufferPositions[0]=0;
 const tension=Object.keys(r.facts.printer.mmu.sensors).find(n=>n.endsWith('filament_tension'));
 r.facts.printer.mmu.sensors[tension].triggered=true;
 const pushed=move(r,10,{motor_mode:'gear'}),key=`${pushed.state.unit}:buffer`;
 assert.equal(pushed.state.tips[0],gears);
 assert.equal(pushed.state.devices[key].modeled_displacement,10);
 assert.equal(pushed.state.devices[key].travel,2.5);
 event(r,'state_delta',{changes:[{path:['printer','mmu','sensors',tension,'triggered'],value:false}]});
 assert.equal(r.snapshot().devices[key].travel,10);
 const partial=move(r,-4,{motor_mode:'gear'});
 assert.equal(partial.state.tips[0],gears);
 assert.equal(partial.state.devices[key].modeled_displacement,6);
 const withdrawn=move(r,-16,{motor_mode:'gear'});
 assert.equal(withdrawn.state.tips[0],gears-10);
 assert.equal(withdrawn.state.bowden_unabsorbed_mm,0);
});
