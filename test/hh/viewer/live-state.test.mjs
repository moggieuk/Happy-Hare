import {test} from 'node:test';
import assert from 'node:assert/strict';
import {LiveState,liveFrame} from './live-state.js';
import {liveObjects as status} from './live-fixture.mjs';
import {gripState} from './live-config.js';
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
 const length=r.objects.save_variables.variables[`mmu_${s.unit}_bowden_lengths`][s.gate];
 assert.equal(s.tips[s.gate],s.layout.extruder_gears-length/2);
 const end=r.accept({mmu:{filament_pos:6,bowden_progress:-1}},3);
 assert.equal(end.tips[end.gate],end.layout.extruder_gears);
});
test('dictionary fields replace rather than retain removed sensors',()=>{
 const r=new LiveState();r.accept(status(),1);
 const s=r.accept({mmu:{sensors:{}}},2);
 assert.deepEqual(s.sensors,{});
 assert.equal(r.accept({mmu:{gate:9}},1),null);
 assert.notEqual(r.objects.mmu.gate,9);
});
test('homing target and direction survive status adaptation',()=>{
 const r=new LiveState();r.accept(status(),1);
 const homing={endstops:['unit0:filament_compression'],triggered:false};
 assert.deepEqual(r.accept({mmu:{homing}},2).homing,homing);
 assert.equal(r.accept({mmu:{homing:null}},3).homing,null);
});
test('grip comes from the existing selector status',()=>{
 for (const [selector,grip] of [[{servo:'Down'},1],[{servo:'Move'},2],[{servo:'Up'},0],[{servo:'Unknown'},-1],
   [{grip:'Gripped'},1],[{grip:'Released'},0],[{has_bypass:false},undefined],[undefined,undefined]])
  assert.equal(gripState(selector),grip,JSON.stringify(selector));
});
test('active sensors are requalified from the short names Klipper reports',()=>{
 const r=new LiveState();r.accept(status(),1);
 const s=r.accept({mmu:{gate:0,sensors:{mmu_entry:true,encoder:false,extruder:null,toolhead:false}}},2);
 assert.deepEqual(Object.keys(s.sensors).sort(),['default:extruder','default:toolhead','mmu_entry_0','unit0:encoder']);
 assert.equal(s.sensors['mmu_entry_0'].state,true);assert.equal(s.sensors['mmu_entry_0'].enabled,true);
 assert.equal(s.sensors['default:extruder'].enabled,false);
 assert.equal(s.homing,null);
});
test('runtime edits arrive in config_changes keyed by section and override startup values',()=>{
 const r=new LiveState();r.accept(status(),1);
 const section=`mmu_toolhead ${r.objects.configfile.settings['mmu_unit unit0'].toolhead}`;
 const s=r.accept({mmu:{config_changes:{[section]:{toolhead_extruder_to_nozzle:120}}}},2);
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
 const s=r.accept({mmu:{filament_pos:6},'unit0:filament_proportional':{value:.6}},2);
 const d=s.devices['unit0:buffer'];
 assert.equal(d.displacement,d.maximum/2+.6*d.range/2);
 assert.equal(d.excess,0);
});
