import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {espoolerActive} from './spool.js';
import {homingHold} from './playback.js';

// Exercise the actual frame loop without SVG/layout cost or a browser clock.
const app=readFileSync(new URL('./app.js',import.meta.url),'utf8');
const invalidation=app.slice(app.indexOf('function draw()'),app.indexOf('function renderScene()'));
const animation=app.slice(app.indexOf('function drawFrame()'),app.indexOf('const toolheadPanel=createToolheadPanel'));
function harness() {
  const state={espoolers:{0:{direction:'assist',output_power:1}}};
  const event={seq:1,kind:'movement',source:'mmu_gcode',state,before:state};
  const context=vm.createContext({
    store:{viewMode:"playback", setup:{connection_kind:"simulator"}, session:'test',
      playing:{event,start:0},displayed:state,live:true,paused:false,
      events:[event],played:0,pauseAfterStep:false,offline:false,
      sensorPulse:{start:0},nextStepAt:0,sceneDirty:false},
    now:100,animationNow:()=>context.now,advances:[],
    renders:0,scheduled:0,spoolUpdates:0,
    // The scene owns spool rotation; here it only reports whether a spool is turning
    scene:{advanceSpools:(now,spools)=>{ context.advances.push(spools); return spools.running && Object.values(spools.espoolers || {}).some(espoolerActive); },
      updateSpoolMotion:()=>context.spoolUpdates++,returnRackWhenIdle:()=>{}},
    homingHold,
    updateLiveIndicator:()=>{},
    $:()=>({checked:false}),duration:()=>1000,nozzleArrivalTime:()=>null,
    playbackFrame:()=>({fraction:.1,state}),homingPulse:()=>null,followTrace:()=>{},
    renderScene:()=>context.renders++,requestAnimationFrame:()=>context.scheduled++,
  });
  vm.runInContext(invalidation+animation,context);
  return context;
}

test('UI invalidations, rotating spools, playback and sensor pulses share one draw',()=>{
  const c=harness();
  vm.runInContext('draw(); draw(); animate();',c);
  assert.equal(c.renders,1);
  assert.equal(c.scheduled,1);
  assert.ok(c.advances.at(-1).running);
  vm.runInContext('animate();',c);
  assert.equal(c.renders,2);
});

test('idle frames do not redraw, but a paused UI change does',()=>{
  const c=harness();c.store.playing=null;c.store.paused=true;c.store.sensorPulse=null;c.store.displayed={};
  vm.runInContext('animate();',c);assert.equal(c.renders,0);
  vm.runInContext('draw(); draw(); animate(); animate();',c);
  assert.equal(c.renders,1);
});

test('reverse-step completion and pulse expiry render their final state once',()=>{
  const c=harness();c.now=1100;c.store.playing.reverse=true;c.store.playing.targetSeq=1;
  c.seek=()=>vm.runInContext('store.playing=null; store.paused=true; store.sensorPulse=null; draw();',c);
  vm.runInContext('animate();',c);
  assert.equal(c.renders,1);assert.equal(c.store.playing,null);
  c.store.sensorPulse={start:0};
  vm.runInContext('animate();',c);
  assert.equal(c.renders,2);assert.equal(c.store.sensorPulse,null);
});

test('spool-only animation updates retained elements without redrawing the scene',()=>{
  const c=harness();c.store.playing=null;c.store.sensorPulse=null;c.store.played=1;
  vm.runInContext('animate(); animate();',c);
  assert.equal(c.renders,0);
  assert.equal(c.spoolUpdates,2);
  assert.ok(c.advances.at(-1).running);
});

test('a completed MMU move starts the next animation on the next frame without a hold',()=>{
 const c=harness();c.now=1000;c.store.sensorPulse=null;
 c.store.playing.event.kind='movement';c.store.playing.event.source='move_filament';
 c.store.events.push({seq:2,kind:'movement',state:c.store.displayed});
 vm.runInContext('animate();',c);
 assert.equal(c.store.played,1);assert.equal(c.store.playing,null);
 c.now=1016;vm.runInContext('animate();',c);
 assert.equal(c.store.playing.event.seq,2);assert.equal(c.store.playing.start,1016);
});

test('a frame that throws is reported once and the loop keeps running',()=>{
  const c=harness();const messages=[];
  c.message=text=>messages.push(text);
  c.renderScene=()=>{throw Error('bad value');};
  const log=console.error;console.error=()=>{};
  try { vm.runInContext('draw(); animate(); draw(); animate();',c); } finally { console.error=log; }
  assert.equal(c.scheduled,2);                 // Both frames still asked for the next one
  assert.deepEqual(messages,['Drawing failed: bad value']);
});
