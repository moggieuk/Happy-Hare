import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {TelemetryReplay} from './telemetry.js';
import {statusText} from './util.js';

test('the scene carries the encoder resolution the tip tolerance is based on',()=>{
 const r=new TelemetryReplay();
 r.accept(JSON.parse(fs.readFileSync(new URL('./fixtures/ercf-vvd.json',import.meta.url))));
 const unit=r.facts.printer.mmu_machine.units[r.facts.printer.mmu.unit];
 unit.encoder.parameters={resolution:0.979};
 assert.equal(r.snapshot().devices[`${r.facts.printer.mmu.unit}:encoder`].resolution,0.979);
 delete unit.encoder.parameters;
 assert.equal(r.snapshot().devices[`${r.facts.printer.mmu.unit}:encoder`].resolution,undefined);
});

test('the device readout names the extruder, encoder and buffer objects by their MMU names',()=>{
 const r=new TelemetryReplay();
 r.accept(JSON.parse(fs.readFileSync(new URL('./fixtures/ercf-vvd.json',import.meta.url))));
 const names=Object.keys(r.snapshot().object_status);
 assert.ok(names.some(n=>n.startsWith('mmu_extruder ')));
 assert.ok(names.every(n=>!n.startsWith('mmu_toolhead ')));
 assert.ok(names.some(n=>n.startsWith('mmu_encoder ')));
});

test('a cut fragment stays in the extruder when another gate has a different Bowden length',()=>{
 const r=new TelemetryReplay();
 r.accept(JSON.parse(fs.readFileSync(new URL('./fixtures/ercf-vvd.json',import.meta.url))));
 const first=r.snapshot(), name=first.filament.extruder;
 r.fragments[name]={start:first.layout.nozzle-30,end:first.layout.nozzle-10,tip_shape:'cone',color:'ff0000'};
 const mmu=r.facts.printer.mmu, unit=r.facts.printer.mmu_machine.units[mmu.unit];
 unit.bowden_lengths[mmu.gate]=(unit.bowden_lengths[mmu.gate] || 700)+120;
 const moved=r.snapshot();
 assert.equal(moved.layout.nozzle,first.layout.nozzle+120);
 assert.equal(moved.layout.nozzle-moved.retained_segment.end,10);
 assert.equal(moved.layout.nozzle-moved.retained_segment.start,30);
});

test('the extruder readout shows the status the MMU exposes',()=>{
 const r=new TelemetryReplay();
 r.accept(JSON.parse(fs.readFileSync(new URL('./fixtures/ercf-vvd.json',import.meta.url))));
 const status=r.snapshot().object_status;
 const key=Object.keys(status).find(n=>n.startsWith('mmu_extruder '));
 assert.equal(status[key].extruder_name,r.snapshot().filament.extruder);
 assert.ok('filament_remaining_color' in status[key]);
});

test('the status readout rounds distances to two decimal places',()=>{
 assert.equal(statusText({filament_cut_fragment:4.999375000000001,extruder_name:'extruder',filament_remaining_temp:220}),
  '{"filament_cut_fragment":5,"extruder_name":"extruder","filament_remaining_temp":220}');
 assert.equal(statusText({encoder_pos:2199.8123}),'{"encoder_pos":2199.81}');
 assert.equal(statusText(null),'No status exposed');
});
