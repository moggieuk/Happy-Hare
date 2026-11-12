import fs from 'node:fs';

// Klipper-shaped live objects: configfile.settings, save_variables and the mmu status.
export function settingsOf(machine) {
 const settings={mmu_machine:{units:Object.keys(machine.units)},
  mmu_parameters:{form_tip_macro:machine.form_tip_macro,purge_macro:machine.purge_macro}};
 for (const [name,u] of Object.entries(machine.units)) {
  settings[`mmu_unit ${name}`]={num_gates:u.gates.length,selector_type:u.selector_type,
   filament_always_gripped:u.filament_always_gripped ? 1 : 0,toolhead:u.toolhead.name,extruder_name:u.extruder.name,
   ...(u.encoder ? {encoder:u.encoder.name} : {}),...(u.buffer ? {buffer:u.buffer.name} : {}),
   ...(u.has_per_gate_heaters ? {filament_heaters:u.heaters} : {filament_heater:u.heaters?.[0] || ''})};
  settings[`mmu_unit_parameters ${name}`]=u.parameters;
  settings[`mmu_toolhead ${u.toolhead.name}`]=u.toolhead.parameters;
  if (u.encoder) settings[`mmu_encoder ${u.encoder.name}`]={};
  if (u.buffer) settings[`mmu_buffer ${u.buffer.name}`]={buffer_range:u.buffer.buffer_range,
   buffer_maxrange:u.buffer.buffer_maxrange,buffer_spring_state:u.buffer.buffer_spring_state,
   ...(u.buffer.proportional ? {analog_pin:'PF6'} : {})};
 }
 return settings;
}
export function liveObjects() {
 const facts=JSON.parse(fs.readFileSync(new URL('./fixtures/emu.json',import.meta.url)))[0].state;
 const m=facts.printer.mmu, machine=facts.printer.mmu_machine;
 const unit=machine.units[m.unit];
 const {sensors,active_sensors,...rest}=m;
 return {configfile:{settings:settingsOf(machine)},
  save_variables:{variables:{[`mmu_${m.unit}_bowden_lengths`]:unit.gates.map(g=>unit.bowden_lengths[g])}},
  mmu:{...rest,unit:0,sensors:{mmu_entry:false,encoder:false,extruder:false,toolhead:false},
   config_changes:{},bowden_progress:-1,sync_drive:false,filament_pos:0},
  [`${unit.buffer.name}:filament_proportional`]:{value:-1},extruder:{target:210}};
}
