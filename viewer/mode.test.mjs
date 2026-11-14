import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {detectConnection} from './simulator.js';
const app=readFileSync(new URL('./app.js',import.meta.url),'utf8');
const isSimulator=app.match(/function isSimulator\(\) \{.*\}/)[0];
const initialize=app.slice(app.indexOf('async function initializeConnection()'),app.indexOf('async function subscribeLive()'));
async function connection(kind,chosen=false,mode='live',offline=false) {
 const calls=[];
 const c=vm.createContext({connectionGeneration:1,modeChosen:chosen,viewMode:mode,setup:null,offline,
   rpc:async method=>{ if (kind!=='simulator') throw Error(`Method not found: ${method}`); return {units:[]}; },
   detectConnection,$:()=>({value:'0'}),sourceMode:()=>{},setConnection:()=>{},refreshLiveEditor:async()=>{},message:()=>{},
   refresh:async probed=>{c.setup=probed;},
   socket:{readyState:1},startStream:async()=>calls.push('telemetry'),
   changeView:async mode=>calls.push(mode),subscribeLive:async()=>calls.push('subscribe')});
 vm.runInContext(isSimulator+initialize,c);await vm.runInContext('initializeConnection()',c);return calls;
}
test('simulator defaults to telemetry, printer defaults to subscriptions',async()=>{
 assert.deepEqual(await connection('simulator'),['playback']);
 assert.deepEqual(await connection('live_machine'),['subscribe']);
});
test('reconnect preserves explicitly selected live or playback mode',async()=>{
 assert.deepEqual(await connection('simulator',true,'live'),['subscribe']);
 assert.deepEqual(await connection('simulator',true,'playback'),['telemetry']);
 assert.deepEqual(await connection('live_machine',true,'playback'),['telemetry']);
});

const warning=app.slice(app.indexOf('function renderWarning('),app.indexOf('let socket, reconnectTimer'));
test('simulator subscription warning clears on mode, source, and connection changes',()=>{
 const bang={}, nodes={'filament-warning':{dataset:{},querySelector:()=>bang},'filament-warning-message':{}};
 const c=vm.createContext({viewMode:'live',offline:false,socket:{readyState:1},setup:{connection_kind:'simulator'},$:id=>nodes[id]});
 vm.runInContext(isSimulator+warning,c);
 const draw=()=>vm.runInContext('renderWarning()',c);
 draw();assert.equal(nodes['filament-warning'].hidden,false);
 assert.match(nodes['filament-warning-message'].textContent,/subscriptions may skip/);
 c.viewMode='playback';draw();assert.equal(nodes['filament-warning'].hidden,true);
 vm.runInContext("renderWarning(['sensor conflict'])",c);
 assert.match(nodes['filament-warning-message'].textContent,/sensor conflict/);
 assert.equal(nodes['filament-warning'].dataset.kind,'warning');assert.equal(bang.textContent,'!');
 vm.runInContext("renderWarning([], 'Selector: moved')",c);
 assert.equal(nodes['filament-warning-message'].textContent,'Selector: moved');
 assert.equal(nodes['filament-warning'].dataset.kind,'notice');assert.equal(bang.textContent,'i');
 vm.runInContext("renderWarning(['sensor conflict'], 'Selector: moved')",c);   // A warning always keeps the warning look
 assert.equal(nodes['filament-warning'].dataset.kind,'warning');assert.match(nodes['filament-warning-message'].textContent,/Selector: moved\nDisplayed tip/);
 c.viewMode='live';c.setup.connection_kind='live_machine';draw();assert.equal(nodes['filament-warning'].hidden,true);
 c.setup.connection_kind='simulator';c.socket.readyState=3;draw();assert.equal(nodes['filament-warning'].hidden,true);
});

test('reconnect leaves loaded recordings offline',async()=>{
 assert.deepEqual(await connection('simulator',true,'playback',true),[]);
 assert.deepEqual(await connection('live_machine',true,'playback',true),[]);
});

test('a failed simulator probe is a live machine and nothing else calls sim.* afterwards',async()=>{
 const methods=[];
 const live=await detectConnection(async method=>{methods.push(method);throw Error('Method not found');},2);
 assert.equal(live.connection_kind,'live_machine');assert.deepEqual(methods,['sim.viewer.setup']);
 const sim=await detectConnection(async(method,params)=>({units:[],unit:params.unit}),1);
 assert.equal(sim.connection_kind,'simulator');assert.equal(sim.unit,1);
});

test('a command from elsewhere never pulls a paused view back to live; one typed in the viewer follows it',()=>{
 const handler=app.slice(app.indexOf('data.method === "notify_gcode_command"'),app.indexOf('data.method === "notify_gcode_response"'));
 assert.ok(!handler.includes('followLive'));
 const form=app.slice(app.indexOf('$("command-form").onsubmit'),app.indexOf('$("restart").onclick'));
 assert.match(form,/followLive\(\)/);
});
