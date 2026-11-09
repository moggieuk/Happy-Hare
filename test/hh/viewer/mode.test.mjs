import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const app=readFileSync(new URL('./app.js',import.meta.url),'utf8');
const initialize=app.slice(app.indexOf('async function initializeConnection()'),app.indexOf('async function subscribeLive()'));
async function connection(kind,chosen=false,mode='live',offline=false) {
 const calls=[];
 const c=vm.createContext({connectionGeneration:1,modeChosen:chosen,viewMode:mode,setup:null,offline,
   rpc:async()=>({moonraker_version:kind==='simulator'?'v0.9-sim':'v0.9'}),
   refresh:async()=>{c.setup={connection_kind:kind};},
   telemetryStream:{start:async()=>calls.push('telemetry')},
   changeView:async mode=>calls.push(mode),subscribeLive:async()=>calls.push('subscribe')});
 vm.runInContext(initialize,c);await vm.runInContext('initializeConnection()',c);return calls;
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
 const nodes={'filament-warning':{},'filament-warning-message':{}};
 const c=vm.createContext({viewMode:'live',offline:false,socket:{readyState:1},setup:{connection_kind:'simulator'},$:id=>nodes[id]});
 vm.runInContext(warning,c);
 const draw=()=>vm.runInContext('renderWarning()',c);
 draw();assert.equal(nodes['filament-warning'].hidden,false);
 assert.match(nodes['filament-warning-message'].textContent,/subscriptions may skip/);
 c.viewMode='playback';draw();assert.equal(nodes['filament-warning'].hidden,true);
 vm.runInContext("renderWarning(['sensor conflict'])",c);
 assert.match(nodes['filament-warning-message'].textContent,/sensor conflict/);
 c.viewMode='live';c.setup.connection_kind='live_machine';draw();assert.equal(nodes['filament-warning'].hidden,true);
 c.setup.connection_kind='simulator';c.socket.readyState=3;draw();assert.equal(nodes['filament-warning'].hidden,true);
});

test('reconnect leaves loaded recordings offline',async()=>{
 assert.deepEqual(await connection('simulator',true,'playback',true),[]);
 assert.deepEqual(await connection('live_machine',true,'playback',true),[]);
});
