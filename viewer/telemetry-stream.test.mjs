import test from 'node:test';
import assert from 'node:assert/strict';
import {TelemetryStream, recordControl} from './telemetry-stream.js';
const batch={stream_id:'a',delivery_id:1,records:[]};
test('accept a batch before acknowledging it; no HTTP polling',async()=>{
 const calls=[];
 const stream=new TelemetryStream(async (method,args)=>{calls.push([method,args]);return batch;},
  ()=>{calls.push(['accepted']);},assert.fail);
 await stream.start();
 assert.deepEqual(calls.map(c=>c[0]),['server.mmu.telemetry.subscribe','accepted','server.mmu.telemetry.ack']);
 await stream.stop();
 assert.equal(calls.at(-1)[0],'server.mmu.telemetry.unsubscribe');
 await stream.receive(batch);assert.equal(calls.length,4);
});
test('disconnect while subscribing cannot apply a late baseline',async()=>{
 let resolve;let applied=false;
 const stream=new TelemetryStream(()=>new Promise(r=>resolve=r),()=>{applied=true;},assert.fail);
 const starting=stream.start();await stream.stop(false);resolve(batch);await starting;
 assert.equal(applied,false);
});
test('history renewal requests a fresh baseline instead of retaining unbounded records',async()=>{
 let accepted=0;const calls=[];
 const stream=new TelemetryStream(async method=>{calls.push(method);return batch;},()=>++accepted===1,assert.fail);
 await stream.start();assert.deepEqual(calls,['server.mmu.telemetry.subscribe','server.mmu.telemetry.subscribe','server.mmu.telemetry.ack']);
});
test('failed replay unsubscribes instead of acknowledging lost data',async()=>{
 const calls=[],errors=[];
 const stream=new TelemetryStream(async method=>{calls.push(method);return batch;},()=>{throw Error('invalid');},e=>errors.push(e.message));
 await stream.start();assert.deepEqual(errors,['invalid']);
 assert.deepEqual(calls,['server.mmu.telemetry.subscribe','server.mmu.telemetry.unsubscribe']);
});

test('all available history is requested by default and on reconnect',async()=>{
 const requests=[];
 const stream=new TelemetryStream(async (method,args)=>{if(method.endsWith('subscribe'))requests.push(args);return batch;},()=>{},assert.fail);
 await stream.start(); await stream.start('start'); await stream.start();
 assert.deepEqual(requests,[{history:'start'},{history:'start'},{history:'start'}]);
});
test('history renewal uses current checkpoint without replacing the user history choice',async()=>{
 let accepted=0;const requests=[];
 const stream=new TelemetryStream(async (method,args)=>{if(method.endsWith('.subscribe'))requests.push(args);return batch;},()=>++accepted===1,assert.fail);
 await stream.start('start');await stream.start();
 assert.deepEqual(requests,[{history:'start'},{history:'current'},{history:'start'}]);
});

test('the record control follows the printer stream and reflects the telemetry_mode status variable',()=>{
 const base={viewMode:'playback',offline:false,simulator:false,connected:true,mode:'on'};
 assert.deepEqual(recordControl(base),{visible:true,value:'on'});
 assert.equal(recordControl({...base,mode:null}).value,'off');
 for (const change of [{viewMode:'live'},{offline:true},{simulator:true},{connected:false}])
  assert.equal(recordControl({...base,...change}).visible,false,JSON.stringify(change));
});
test('a batch repeated while it is still being accepted, or after, is not replayed twice',async()=>{
 const calls=[];let accepted=0,release;
 const stream=new TelemetryStream(async method=>{calls.push(method);return {stream_id:'a',delivery_id:1,records:[]};},
  ()=>{accepted++;if(accepted===2)return new Promise(r=>release=r);},assert.fail);
 await stream.start();
 const slow={stream_id:'a',delivery_id:2,records:[]};
 const first=stream.receive(slow);          // Accepting it takes longer than the sender waits
 await stream.receive(slow);                // The repeat arrives meanwhile
 assert.equal(accepted,2);
 release();await first;
 await stream.receive(slow);                // A repeat after it was acknowledged: the acknowledgement was lost
 assert.equal(accepted,2);
 assert.deepEqual(calls.slice(-2),['server.mmu.telemetry.ack','server.mmu.telemetry.ack']);
});
