import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {espoolerActive} from './spool.js';
import {homingHold} from './playback.js';

// Exercise the actual frame loop without SVG/layout cost or a browser clock.
const app=readFileSync(new URL('./app.js',import.meta.url),'utf8');
const invalidation=app.slice(app.indexOf('let sceneDirty='),app.indexOf('function renderScene()'));
const animation=app.slice(app.indexOf('function drawFrame()'),app.indexOf('const toolheadPanel=createToolheadPanel'));
function harness() {
  const state={espoolers:{0:{direction:'assist',output_power:1}}};
  const event={seq:1,kind:'movement',source:'mmu_gcode',state,before:state};
  const context=vm.createContext({viewMode:"playback", setup:{connection_kind:"simulator"},
    now:100,animationNow:()=>context.now,
    spoolSession:'test',session:'test',spoolLastFrame:80,
    spoolAngles:new Map(),spoolArrowVisibility:new Map(),spoolDirections:new Map(),
    playing:{event,start:0},displayed:state,live:true,paused:false,
    events:[event],played:0,pauseAfterStep:false,offline:false,
    sensorPulse:{start:0},renders:0,scheduled:0,spoolUpdates:0,
    updateSpoolMotion:()=>context.spoolUpdates++,espoolerActive,nextStepAt:0,homingHold,returnRackWhenIdle:()=>{},
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
  assert.ok(c.spoolAngles.get(0)>0);
  vm.runInContext('animate();',c);
  assert.equal(c.renders,2);
});

test('idle frames do not redraw, but a paused UI change does',()=>{
  const c=harness();c.playing=null;c.paused=true;c.sensorPulse=null;c.displayed={};
  vm.runInContext('animate();',c);assert.equal(c.renders,0);
  vm.runInContext('draw(); draw(); animate(); animate();',c);
  assert.equal(c.renders,1);
});

test('reverse-step completion and pulse expiry render their final state once',()=>{
  const c=harness();c.now=1100;c.playing.reverse=true;c.playing.targetSeq=1;
  c.seek=()=>vm.runInContext('playing=null; paused=true; sensorPulse=null; draw();',c);
  vm.runInContext('animate();',c);
  assert.equal(c.renders,1);assert.equal(c.playing,null);
  c.sensorPulse={start:0};
  vm.runInContext('animate();',c);
  assert.equal(c.renders,2);assert.equal(c.sensorPulse,null);
});

test('spool-only animation updates retained elements without redrawing the scene',()=>{
  const c=harness();c.playing=null;c.sensorPulse=null;c.played=1;
  vm.runInContext('animate(); animate();',c);
  assert.equal(c.renders,0);
  assert.equal(c.spoolUpdates,2);
  assert.ok(c.spoolAngles.get(0)>0);
});

test('a completed MMU move starts the next animation on the next frame without a hold',()=>{
 const c=harness();c.now=1000;c.sensorPulse=null;
 c.playing.event.kind='movement';c.playing.event.source='move_filament';
 c.events.push({seq:2,kind:'movement',state:c.displayed});
 vm.runInContext('animate();',c);
 assert.equal(c.played,1);assert.equal(c.playing,null);
 c.now=1016;vm.runInContext('animate();',c);
 assert.equal(c.playing.event.seq,2);assert.equal(c.playing.start,1016);
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
