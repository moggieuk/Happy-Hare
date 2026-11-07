import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

// Exercise the actual frame loop without SVG/layout cost or a browser clock.
const app=readFileSync(new URL('./app.js',import.meta.url),'utf8');
const invalidation=app.slice(app.indexOf('let sceneDirty='),app.indexOf('function renderScene()'));
const animation=app.slice(app.indexOf('function animate()'),app.indexOf('// Simulator-only editors'));
function harness() {
  const state={espoolers:{0:{direction:'assist',output_power:1}}};
  const event={seq:1,kind:'movement',source:'mmu_gcode',state,before:state};
  const context=vm.createContext({
    now:100,animationNow:()=>context.now,
    spoolSession:'test',session:'test',spoolLastFrame:80,
    spoolAngles:new Map(),spoolArrowVisibility:new Map(),spoolDirections:new Map(),
    playing:{event,start:0},displayed:state,live:true,paused:false,
    events:[event],played:0,resumeAt:0,pauseAfterStep:false,offline:false,
    sensorPulse:{start:0},renders:0,scheduled:0,spoolUpdates:0,
    updateSpoolMotion:()=>context.spoolUpdates++,
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
