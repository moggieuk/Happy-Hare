import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {TelemetryReplay} from './telemetry.js';
import {overlayAllowed,overlaySnapshot} from './overlay.js';

const replay=()=>{
 const r=new TelemetryReplay();
 r.accept(JSON.parse(fs.readFileSync(new URL('./fixtures/emu.json',import.meta.url))));
 return r;
};
const rest={viewMode:'playback',offline:false,live:true,paused:false,playing:null,caughtUp:true,pendingMoves:0};

test('reported sensor readings replace the recorded ones in the scene, and nothing else',()=>{
 const r=replay(), facts=structuredClone(r.facts), before=r.snapshot();
 const s=overlaySnapshot(r,{filament_compression:true,filament_tension:false,mmu_entry:true,mmu_exit:null});
 assert.equal(s.sensors['unit0:filament_compression'].state,true);
 assert.equal(s.sensors['unit0:filament_tension'].state,false);
 assert.equal(s.sensors['mmu_entry_0'].state,true);
 assert.equal(s.sensors['mmu_exit_0'].enabled,false);
 assert.equal(s.sensors['mmu_exit_0'].state,null);
 assert.equal(s.filament_pos,before.filament_pos);assert.equal(s.gate,before.gate);
 assert.deepEqual(r.facts,facts);                       // The replay is untouched
 assert.deepEqual(r.snapshot().sensors,before.sensors);
});
test('sensors the status names but the recording does not know are ignored',()=>{
 const r=replay(), before=r.snapshot().sensors;
 assert.deepEqual(overlaySnapshot(r,{mmu_pre_gate:true,mmu_gear:true,nonsense:false}).sensors,before);
});
test('there is nothing to overlay without a recording or a status',()=>{
 assert.equal(overlaySnapshot(new TelemetryReplay(),{mmu_entry:true}),null);
 assert.equal(overlaySnapshot(replay(),null),null);
});
test('the overlay applies only when following the stream at rest',()=>{
 assert.equal(overlayAllowed(rest),true);
 for (const change of [{viewMode:'live'},{offline:true},{live:false},{paused:true},{playing:{}},{caughtUp:false},{pendingMoves:1}])
  assert.equal(overlayAllowed({...rest,...change}),false,JSON.stringify(change));
});
