import test from 'node:test';
import assert from 'node:assert/strict';
import {unitInfo, unitObjectFields} from './unit-info.js';
import {unitLogo} from './unit-icons.js';

const machine=(...units)=>({num_units:units.length,...Object.fromEntries(units.map((u,i)=>[`unit_${i}`,u]))});
const unit=(fields={})=>({name:'unit0',display_name:'KMS-0',vendor:'KMS',first_gate:0,num_gates:4,...fields});

test('each vendor has its logo, an unknown one gets Happy Hare, and EMU has a dark variant',()=>{
  assert.notEqual(unitLogo('ERCF').path,unitLogo('BoxTurtle').path);
  assert.equal(unitLogo('ERCF').square,true);
  assert.equal(unitLogo('BoxTurtle').square,false);
  assert.equal(unitLogo('Prusa').path,unitLogo('Unknown').path);
  assert.notEqual(unitLogo('EMU',true).path,unitLogo('EMU').path);
  assert.equal(unitLogo('ERCF',true).path,unitLogo('ERCF').path);
});

test('a unit is named by its display name, or its name when it has none',()=>{
  const [first,second]=unitInfo({mmu_machine:machine(unit(),unit({name:'unit1',display_name:'',vendor:undefined,first_gate:4}))},0);
  assert.equal(first.display_name,'KMS-0');
  assert.equal(second.display_name,'unit1');
  assert.equal(second.vendor,'Unknown');
  assert.deepEqual(second.gates,[4,5,6,7]);
  assert.equal(unitInfo({},0),null);
});

test('a single heater and sensor give whole-number readings, and the heater shows only with a target',()=>{
  const objects={mmu_machine:machine(unit({filament_heater:'"heater_generic dry"',environment_sensor:'bme280 box'})),
    'heater_generic dry':{temperature:31.2,target:0},'bme280 box':{temperature:44.6,humidity:12.4}};
  let [info]=unitInfo(objects,0);
  assert.deepEqual([info.humidity,info.temperature,info.heater,info.heaterIcon],['12%','45°C',undefined,null]);
  assert.equal(info.tooltip,null);
  objects['heater_generic dry'].target=60;
  [info]=unitInfo(objects,0);
  assert.deepEqual([info.heater,info.heaterIcon],['60°C','heater']);
});

test('a sensor named without its type is found under the known sensor types',()=>{
  const objects={mmu_machine:machine(unit({environment_sensor:'sensor box'})),'aht2x box':{temperature:30,humidity:20}};
  assert.equal(unitInfo(objects,0)[0].temperature,'30°C');
});

test('per-gate heaters and sensors show the selected gate, marked as one of several',()=>{
  const objects={mmu_machine:machine(unit({filament_heaters:['heater_generic h0','heater_generic h1','',''],
      environment_sensors:['bme280 s0','bme280 s1','','']})),
    'heater_generic h0':{target:0},'heater_generic h1':{target:55},
    'bme280 s0':{temperature:25,humidity:30},'bme280 s1':{temperature:48,humidity:9}};
  let [info]=unitInfo(objects,1);
  assert.deepEqual([info.humidity,info.temperature,info.heater,info.heaterIcon],['9%','48°C ...','55°C ...','heater']);
  [info]=unitInfo(objects,-1);
  assert.deepEqual([info.humidity,info.temperature,info.heater,info.heaterIcon],['...','...','...','heater']);
  assert.equal(info.tooltip,'Gate 0: 30%/25°C, Heater:\nGate 1: 9%/48°C, Heater: 55°C');
});

test('a drying unit shows the drying icon and each gate\'s drying state in the tooltip',()=>{
  const objects={mmu:{drying_state:['active','queued','complete','cancelled']},
    mmu_machine:machine(unit({filament_heaters:['heater_generic h0','heater_generic h1','heater_generic h2','heater_generic h3']})),
    'heater_generic h0':{target:60},'heater_generic h1':{target:0},'heater_generic h2':{target:0},'heater_generic h3':{target:0}};
  const [info]=unitInfo(objects,0);
  assert.equal(info.heaterIcon,'drying');
  assert.equal(info.tooltip,'Gate 0: Drying: 60°C\nGate 1: Drying queued\nGate 2: Drying complete\nGate 3: Drying cancelled');
});

test('icons beside the readings are dropped on a unit of two gates or fewer',()=>{
  assert.equal(unitInfo({mmu_machine:machine(unit({num_gates:2}))},0)[0].climateIcons,false);
  assert.equal(unitInfo({mmu_machine:machine(unit())},0)[0].climateIcons,true);
});

test('the subscription names the machine and each unit\'s heaters and sensors that the printer has',()=>{
  const wanted=unitObjectFields(machine(unit({filament_heater:'"heater_generic dry"',environment_sensor:'sensor box'}),
    unit({filament_heaters:['heater_generic h0',''],environment_sensors:['bme280 s0','bme280 gone']})),
    ['heater_generic dry','aht2x box','heater_generic h0','bme280 s0','extruder']);
  assert.deepEqual(wanted,{mmu_machine:null,'heater_generic dry':['target'],'aht2x box':['temperature','humidity'],
    'heater_generic h0':['target'],'bme280 s0':['temperature','humidity']});
});
