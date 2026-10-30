import { test } from 'node:test';
import assert from 'node:assert/strict';
import { interpolate, duration, purgePath } from './playback.js';
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

test('purge starts after nozzle contact and coil geometry stays bounded',()=>{
 const e={before:{...state(0,false),extruded_mm:0},state:{...state(5,false),layout:{nozzle:5},extruded_mm:10}};
 assert.equal(interpolate(e,.2).extruded_mm,0);
 assert.equal(interpolate(e,.5).extruded_mm,2.5);
 assert.equal(purgePath(0),'');
 assert.ok(purgePath(10).startsWith('M'));
 assert.equal(purgePath(100000),purgePath(36));
});

test('nozzle retract releases coil while preserving deposited amount',()=>{
 const a={...state(5,false),extruded_mm:10,purge_base_mm:0,nozzle_color:'ff00ff'};
 const b={...state(3,false),extruded_mm:10,purge_base_mm:10,nozzle_color:'00ffff'};
 const mid=interpolate({before:a,state:b},.5);
 assert.equal(mid.purge_drop,.5);
 assert.equal(mid.extruded_mm,10);
 assert.equal(mid.purge_base_mm,0);
 assert.equal(mid.nozzle_color,'8080ff');
});
