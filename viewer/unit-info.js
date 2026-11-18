/* Copyright (C) 2022-2026 Paul Morgan. GPLv3. */
// What is shown under each MMU unit: its name, vendor, climate readings and drying state.
// Read from live printer objects, following Mainsail Happy Hare Edition's unit footer.
import {finite} from './util.js';

// Klipper sections that report humidity and temperature, tried when a sensor is named without its type
const SENSOR_TYPES = ['aht10', 'aht1x', 'aht2x', 'aht3x', 'bme280', 'htu21d', 'sgp40', 'sht3x', 'temperature_combined'];
const DRYING_TEXT = {queued: 'Drying queued', complete: 'Drying complete', cancelled: 'Drying cancelled'};

const stripQuotes = name => typeof name === 'string' ? name.replace(/^"(.*)"$/, '$1') : name;
const reading = (value, suffix) => finite(value) ? `${value.toFixed(0)}${suffix}` : undefined;

function sensorObject(objects, name) {
  if (!name) return undefined;
  if (objects[name]) return objects[name];
  const bare = name.split(' ').pop();
  return SENSOR_TYPES.map(type => objects[`${type} ${bare}`]).find(Boolean);
}

// The printer objects the footers read, as a subscription: {object name: fields}
export function unitObjectFields(machine, available) {
  const wanted = {mmu_machine: null};
  const names = value => [].concat(value || []).map(stripQuotes).filter(Boolean);
  for (let i = 0; i < (machine?.num_units ?? 1); i++) {
    const unit = machine?.[`unit_${i}`];
    if (!unit) continue;
    for (const name of [...names(unit.filament_heater), ...names(unit.filament_heaters)])
      if (available.includes(name)) wanted[name] = ['target'];
    for (const name of [...names(unit.environment_sensor), ...names(unit.environment_sensors)]) {
      const bare = name.split(' ').pop();
      for (const candidate of [name, ...SENSOR_TYPES.map(type => `${type} ${bare}`)])
        if (available.includes(candidate)) wanted[candidate] = ['temperature', 'humidity'];
    }
  }
  return wanted;
}

export function unitInfo(objects, selectedGate) {
  const machine = objects?.mmu_machine;
  if (!machine) return null;
  const dryingStates = objects.mmu?.drying_state || [];
  const units = Array.from({length: machine.num_units ?? 1}, (_, i) => machine[`unit_${i}`]).filter(Boolean);
  return units.map(unit => {
    const start = unit.first_gate, count = unit.num_gates;
    const gates = Array.from({length: count}, (_, i) => start+i);
    // With one heater or sensor per gate, the unit's row shows the selected gate's
    const selected = (perGate, single) => perGate
      ? (selectedGate >= start && selectedGate < start+count ? stripQuotes(perGate[selectedGate-start]) : undefined)
      : stripQuotes(single);
    const heater = objects[selected(unit.filament_heaters, unit.filament_heater)];
    const sensor = sensorObject(objects, selected(unit.environment_sensors, unit.environment_sensor));
    const perGateHeaters = Boolean(unit.filament_heaters), perGateSensors = Boolean(unit.environment_sensors);
    const several = (text, perGate) => text && perGate ? `${text} ...` : text;
    const states = dryingStates.slice(start, start+count);
    const drying = states.some(state => state === 'active' || state === 'queued');
    const heaterTarget = heater ? (heater.target > 0 ? several(reading(heater.target, '°C'), perGateHeaters) : undefined)
      : perGateHeaters ? '...' : undefined;
    const heating = perGateHeaters
      ? unit.filament_heaters.some(name => objects[stripQuotes(name)]?.target > 0) : Boolean(heaterTarget);
    let tooltip = null;
    if (perGateHeaters || perGateSensors) {
      tooltip = gates.map((gate, i) => {
        const parts = [];
        const gateSensor = sensorObject(objects, stripQuotes(unit.environment_sensors?.[i]));
        const climate = [reading(gateSensor?.humidity, '%'), reading(gateSensor?.temperature, '°C')].filter(Boolean).join('/');
        if (climate) parts.push(climate);
        const gateHeater = objects[stripQuotes(unit.filament_heaters?.[i])];
        if (gateHeater) {
          const target = gateHeater.target > 0 ? reading(gateHeater.target, '°C') : undefined;
          parts.push(drying && states[i] === 'active' ? `Drying: ${target ?? ''}`.trim()
            : drying && DRYING_TEXT[states[i]] ? DRYING_TEXT[states[i]]
            : target ? `Heater: ${target}` : 'Heater:');
        }
        return parts.length ? `Gate ${gate}: ${parts.join(', ')}` : null;
      }).filter(Boolean).join('\n') || null;
    }
    return {
      name: unit.name, display_name: unit.display_name || unit.name, vendor: unit.vendor || 'Unknown', gates,
      humidity: perGateSensors && !sensor ? '...' : reading(sensor?.humidity, '%'),
      temperature: perGateSensors && !sensor ? '...' : several(reading(sensor?.temperature, '°C'), perGateSensors),
      heater: heaterTarget, heaterIcon: drying ? 'drying' : heating ? 'heater' : null,
      climateIcons: count > 2, tooltip,
    };
  });
}
