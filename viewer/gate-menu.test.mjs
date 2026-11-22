import test from 'node:test';
import assert from 'node:assert/strict';
import {gateMenu} from './gate-menu.js';

const objects=(mmu={},unit={})=>({mmu:{enabled:true,gate:0,filament_pos:0,...mmu},
  mmu_machine:{num_units:1,unit_0:{first_gate:0,num_gates:4,can_crossload:false,...unit}},
  print_stats:{state:'standby'},idle_timeout:{state:'Idle'}});
const enabled=(o,gate,connected=true)=>gateMenu(o,gate,connected).filter(item=>!item.disabled).map(item=>item.id);
const command=(o,gate,id)=>gateMenu(o,gate,true).find(item=>item.id===id).command;

test('each item sends its command for the gate, and load and unload act on the selected one',()=>{
  const o=objects();
  assert.deepEqual(gateMenu(o,2,true).map(item=>item.label),['Select','Change Tool','Preload','Load','Unload','Eject','Check Gate']);
  assert.equal(command(o,2,'select'),'MMU_SELECT GATE=2');
  assert.equal(command(o,2,'changeTool'),'MMU_CHANGE_TOOL GATE=2');
  assert.equal(command(o,2,'preload'),'MMU_PRELOAD GATE=2');
  assert.equal(command(o,2,'eject'),'MMU_EJECT GATE=2');
  assert.equal(command(o,2,'check'),'MMU_CHECK_GATE GATE=2');
  assert.equal(command(o,2,'load'),'MMU_LOAD');
  assert.equal(command(o,2,'unload'),'MMU_UNLOAD');
});

test('nothing can be sent while printing, disconnected or with the MMU disabled',()=>{
  assert.deepEqual(enabled(objects(),0,false),[]);
  assert.deepEqual(enabled(objects({enabled:false}),0),[]);
  assert.deepEqual(enabled({...objects(),print_stats:{state:'printing'}},0),[]);
  assert.deepEqual(enabled({...objects(),idle_timeout:{state:'Printing'}},0),[]);
  assert.notDeepEqual(enabled({...objects(),print_stats:{state:'paused'}},0),[]);
});

test('the selected gate, unloaded, can be preloaded, loaded, ejected and checked',()=>{
  assert.deepEqual(enabled(objects(),0),['preload','load','eject','check']);
});

test('the selected gate, loaded, can be unloaded, ejected and checked',()=>{
  assert.deepEqual(enabled(objects({filament_pos:10}),0),['unload','eject','check']);
});

test('another gate can be selected or changed to, and worked on only where the unit can crossload',()=>{
  assert.deepEqual(enabled(objects(),2),['select','changeTool','check']);
  assert.deepEqual(enabled(objects({},{can_crossload:true}),2),['select','changeTool','preload','eject','check']);
  // With filament loaded another gate cannot be selected, but a tool change unloads first
  assert.deepEqual(enabled(objects({filament_pos:10}),2),['changeTool','check']);
});

test('a gate with its own NFC reader offers a tag scan, under the preload rule',()=>{
  assert.equal(gateMenu(objects(),0,true).some(item=>item.id==='nfc'),false);
  assert.equal(gateMenu(objects({},{nfc_reader:'shared'}),0,true).some(item=>item.id==='nfc'),false);
  const perGate=objects({},{nfc_readers:['r0','','r2','']});
  assert.equal(gateMenu(perGate,1,true).some(item=>item.id==='nfc'),false);
  assert.equal(command(perGate,2,'nfc'),'MMU_NFC_SCAN GATE=2');
  assert.ok(enabled(perGate,0).includes('nfc'));
  assert.ok(!enabled(perGate,2).includes('nfc'));
});

test('a gate belongs to the unit that holds it',()=>{
  const o=objects();
  o.mmu_machine={num_units:2,unit_0:{first_gate:0,num_gates:4,can_crossload:false},unit_1:{first_gate:4,num_gates:4,can_crossload:true}};
  assert.ok(!enabled(o,2).includes('preload'));
  assert.ok(enabled(o,5).includes('preload'));
});
