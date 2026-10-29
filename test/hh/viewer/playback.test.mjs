import { test } from 'node:test';
import assert from 'node:assert/strict';
import { interpolate, duration } from './playback.js';
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
