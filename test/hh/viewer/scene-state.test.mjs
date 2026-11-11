import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {TelemetryReplay} from './telemetry.js';

test('the scene carries the encoder resolution the tip tolerance is based on',()=>{
 const r=new TelemetryReplay();
 r.accept(JSON.parse(fs.readFileSync(new URL('./fixtures/ercf-vvd.json',import.meta.url))));
 const unit=r.facts.printer.mmu_machine.units[r.facts.printer.mmu.unit];
 unit.encoder.parameters={resolution:0.979};
 assert.equal(r.snapshot().devices[`${r.facts.printer.mmu.unit}:encoder`].resolution,0.979);
 delete unit.encoder.parameters;
 assert.equal(r.snapshot().devices[`${r.facts.printer.mmu.unit}:encoder`].resolution,undefined);
});
