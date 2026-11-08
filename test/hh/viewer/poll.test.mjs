import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const app=readFileSync(new URL('./app.js',import.meta.url),'utf8');
const poll=app.slice(app.indexOf('async function poll()'),app.indexOf('function animate()'));
async function request(recordCount,session='session') {
  const urls=[];
  const context=vm.createContext({offline:false,sourceGeneration:0,
    sourceRecords:Array(recordCount),sourceReplay:{session,sequence:6000},rawEvents:[],
    fetch:async url=>{urls.push(url);return {ok:true,json:async()=>({result:{session_id:session,records:[]}})};},
    updateSource:()=>{},setTimeout:()=>{},
  });
  vm.runInContext(poll,context);await vm.runInContext('poll()',context);
  return urls;
}

test('a full server ring plus its session record does not trigger repeated full reloads',async()=>{
  assert.deepEqual(await request(6001),['/sim/viewer/telemetry?after=6000']);
});
test('history still refreshes after growing beyond the bounded headroom',async()=>{
  assert.deepEqual(await request(7001),['/sim/viewer/telemetry']);
  assert.deepEqual(await request(0,null),['/sim/viewer/telemetry']);
});
