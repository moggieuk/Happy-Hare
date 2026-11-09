import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {LiveState,liveFrame} from './live-state.js';
function status() {
 const facts=JSON.parse(fs.readFileSync(new URL('./fixtures/emu.json',import.meta.url)))[0].state;
 const m=facts.printer.mmu, machine=facts.printer.mmu_machine;
 const unit=machine.units[m.unit];
 return {mmu_machine:{configuration:machine},mmu:{...m,unit:0,sensor_states:m.sensors,
   bowden_progress:-1,homing:null,sync_drive:false,filament_pos:0},
   [`mmu_buffer ${unit.buffer.name}`]:{value:-1},extruder:{target:210}};
}
test('live status needs no telemetry session or movement records',()=>{
 const r=new LiveState(),s=r.accept(status(),1);
 assert.equal(s.filament_pos,0);assert.equal(s.extruder_target_temperature,210);
 assert.equal(r.events,undefined);assert.equal(r.tips,undefined);
 assert.equal(s.devices[`${s.unit}:buffer`].value,-1);
});
test('live spool fill follows subscribed gate weights',()=>{
 const r=new LiveState();r.accept(status(),1);
 assert.deepEqual(r.accept({mmu:{gate_initial_weight:[1000,1000,-1],gate_remaining_weight:[750,0,-1]}},2)
  .spool_percentages.slice(0,3),[75,0,null]);
 assert.equal(r.accept({mmu:{gate_remaining_weight:[500,0,-1]}},3).spool_percentages[0],50);
});
test('Bowden progress sets the target independently of motor positions',()=>{
 const r=new LiveState();r.accept(status(),1);
 const s=r.accept({mmu:{filament_pos:3,bowden_progress:50,filament_position:99999}},2);
 const length=r.objects.mmu_machine.configuration.units[s.unit].bowden_lengths[s.gate];
 assert.equal(s.tips[s.gate],s.layout.extruder_gears-length/2);
 const end=r.accept({mmu:{filament_pos:6,bowden_progress:-1}},3);
 assert.equal(end.tips[end.gate],end.layout.extruder_gears);
});
test('dictionary fields replace rather than retain removed sensors',()=>{
 const r=new LiveState();r.accept(status(),1);
 const s=r.accept({mmu:{sensor_states:{},active_sensors:[]}},2);
 assert.deepEqual(s.sensors,{});
 assert.equal(r.accept({mmu:{gate:9}},1),null);
 assert.notEqual(r.objects.mmu.gate,9);
});
test('homing direction and sensor identity survive status adaptation',()=>{
 const r=new LiveState();r.accept(status(),1);
 const homing={endstops:['unit0:filament_compression'],triggered:false};
 assert.deepEqual(r.accept({mmu:{homing}},2).homing,homing);
 assert.equal(r.accept({mmu:{homing:null}},3).homing,null);
});
test('runtime configuration updates are effective without telemetry',()=>{
 const r=new LiveState();r.accept(status(),1);
 const c=structuredClone(r.objects.mmu_machine.configuration);
 c.units.unit0.toolhead.parameters.toolhead_extruder_to_nozzle=120;
 const s=r.accept({mmu_machine:{configuration:c}},2);
 assert.equal(s.layout.nozzle-s.layout.extruder_gears,120);
});
test('cosmetic frames land exactly on reported state and do not mutate it',()=>{
 const r=new LiveState(),a=r.accept(status(),1);
 const b=r.accept({mmu:{filament_pos:6,bowden_progress:-1}},2);
 const before=structuredClone(b),half=liveFrame(a,b,.5);
 assert.equal(half.tips[b.gate],(a.tips[a.gate]+b.tips[b.gate])/2);
 assert.deepEqual(b,before);assert.equal(liveFrame(a,b,1),b);
});

test('proportional position remains authoritative with released gears',()=>{
 const r=new LiveState();r.accept(status(),1);
 const s=r.accept({mmu:{filament_grip_state:0,filament_pos:6},'mmu_buffer unit0':{value:.6}},2);
 const d=s.devices['unit0:buffer'];
 assert.equal(d.displacement,d.maximum/2+.6*d.range/2);
 assert.equal(d.excess,0);
});
