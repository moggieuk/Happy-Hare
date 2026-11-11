/* Copyright (C) 2022-2026 Paul Morgan. GPLv3. */
// Live mode builds the machine description from state Klipper already exposes:
// configfile.settings (startup values), mmu.config_changes (later MMU_TEST_CONFIG edits)
// and save_variables (calibration). No Happy Hare specific configuration object is needed.

const GATE_SENSOR=/^mmu_(entry|exit)$/;
const SHARED_SENSOR=/^(extruder|toolhead)$/;
const BUFFER_SENSOR=/^filament_(compression|tension|proportional)$/;
const UNREGISTERED=/^mmu_(pre_gate|gate|gear)$/; // Reported as active but never part of the sensor map
const BOWDEN_DEFAULT=700;
const list=value=>Array.isArray(value) ? value : value ? [value] : [];
const first=name=>String(name || '').trim().split(/\s+/)[0];

export function effective(objects,section) {
  const key=section.toLowerCase(); // Klipper keys settings by lower-case section name
  return {...objects.configfile?.settings?.[key],...objects.mmu?.config_changes?.[key]};
}

export function liveMachine(objects) {
  const settings=objects.configfile?.settings, names=list(settings?.mmu_machine?.units);
  if (!names.length) return null;
  const variables=objects.save_variables?.variables || {};
  const machineParameters=effective(objects,'mmu_parameters');
  const units={};
  let gate=0;
  for (const name of names) {
    const unit=effective(objects,`mmu_unit ${name}`), count=Number(unit.num_gates) || 0;
    const gates=Array.from({length:count},(_,i)=>gate+i);
    const heaters=list(unit.filament_heaters).filter(Boolean);
    const toolhead=unit.toolhead || 'default', bufferName=unit.buffer || (unit.filament_buffer ? name : '');
    const buffer=settings[`mmu_buffer ${bufferName}`.toLowerCase()] ? effective(objects,`mmu_buffer ${bufferName}`) : null;
    const lengths=list(variables[`mmu_${name}_bowden_lengths`]);
    units[name]={
      gates,selector_type:unit.selector_type,filament_always_gripped:!!Number(unit.filament_always_gripped),
      has_per_gate_heaters:heaters.length>0,heaters:heaters.length ? heaters : [unit.filament_heater || ''],
      parameters:{...unit,...effective(objects,`mmu_unit_parameters ${name}`)},
      toolhead:{name:toolhead,parameters:effective(objects,`mmu_toolhead ${toolhead}`)},
      extruder:{name:unit.extruder_name || 'extruder'},
      bowden_lengths:Object.fromEntries(gates.map((g,i)=>[String(g),lengths[i]>0 ? lengths[i] : BOWDEN_DEFAULT])),
      sensors:[],
      encoder:settings[`mmu_encoder ${unit.encoder || name}`.toLowerCase()] ? {name:unit.encoder || name,
        // The calibrated resolution is saved; the configured one is only the starting point
        parameters:{resolution:variables[`mmu_${name}_encoder_resolution`] ?? effective(objects,`mmu_encoder ${unit.encoder || name}`).encoder_resolution}} : null,
      buffer:buffer && {name:bufferName,proportional:'analog_pin' in buffer,
        buffer_range:buffer.buffer_range,buffer_maxrange:buffer.buffer_maxrange,buffer_spring_state:buffer.buffer_spring_state},
    };
    gate+=count;
  }
  return {num_gates:gate,units,parameters:machineParameters,
    form_tip_macro:first(machineParameters.form_tip_macro),purge_macro:first(machineParameters.purge_macro)};
}

// The active sensor map is keyed by short names for the selected gate. Rebuild the
// qualified names the scene uses: shared toolhead sensors, per-gate switches, buffer sensors
// (named for the buffer, which units may share) and per-unit devices.
export function qualifySensors(sensors,unit,gate,buffer,toolhead='default') {
  const result={};
  for (const [name,value] of Object.entries(sensors || {})) {
    if (UNREGISTERED.test(name)) continue;
    const key=SHARED_SENSOR.test(name) ? `${toolhead}:${name}` : GATE_SENSOR.test(name) ? `${name}_${gate}` :
      BUFFER_SENSOR.test(name) ? `${buffer || unit}:${name}` : `${unit}:${name}`;
    result[key]={triggered:value,enabled:value!==null};
  }
  return result;
}

// Grip comes from the existing mmu.selector status: servo selectors report Up/Down/Move,
// servo-less grippers report Gripped/Released. Indexed selectors are always gripped (config).
export function gripState(selector) {
  if (selector?.servo) return {Up:0,Down:1,Move:2}[selector.servo] ?? -1;
  if (selector?.grip) return selector.grip==='Gripped' ? 1 : 0;
  return undefined;
}

export function bufferValue(objects,buffer) {
  return objects[`${buffer.name}:filament_proportional`]?.value ?? null;
}
