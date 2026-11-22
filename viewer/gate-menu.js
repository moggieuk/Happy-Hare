/* Copyright (C) 2022-2026 Paul Morgan. GPLv3. */
// The action menu on a gate's spool or number: which commands it offers and when each can be sent.
// The rules are Mainsail Happy Hare Edition's, from its gate menu and its MMU control buttons.

const FILAMENT_POS_UNLOADED = 0, FILAMENT_POS_LOADED = 10;

// Material Design Icons (Apache 2.0), 24x24
const ICONS = {
  select: 'M21,9L17,5V8H10V10H17V13M7,11L3,15L7,19V16H14V14H7V11Z',
  changeTool: 'M12,2L16,6H13V13.85L19.53,17.61L21,15.03L22.5,20.5L17,21.96L18.53,19.35L12,15.58L5.47,19.35L7,21.96L1.5,20.5L3,15.03L4.47,17.61L11,13.85V6H8L12,2Z',
  preload: 'M13,5V11H14.17L12,13.17L9.83,11H11V5H13M15,3H9V9H5L12,16L19,9H15V3M19,18H5V20H19V18Z',
  load: 'M5,20H19V18H5M19,9H15V3H9V9H5L12,16L19,9Z',
  unload: 'M9,16V10H5L12,3L19,10H15V16H9M5,20V18H19V20H5Z',
  eject: 'M12,5L5.33,15H18.67M5,17H19V19H5V17Z',
  check: 'M21,7L9,19L3.5,13.5L4.91,12.09L9,16.17L19.59,5.59L21,7Z',
  nfc: 'M18,6H13A2,2 0 0,0 11,8V10.28C10.41,10.62 10,11.26 10,12A2,2 0 0,0 12,14C13.11,14 14,13.1 14,12C14,11.26 13.6,10.62 13,10.28V8H16V16H8V8H10V6H8L6,6V18H18M20,20H4V4H20M20,2H4A2,2 0 0,0 2,4V20A2,2 0 0,0 4,22H20C21.11,22 22,21.1 22,20V4C22,2.89 21.11,2 20,2Z',
};

// The printer objects the rules read, as a subscription: {object name: fields}
export const GATE_MENU_OBJECTS = {print_stats: ['state'], idle_timeout: ['state']};
export const GATE_MENU_MMU_FIELDS = ['enabled', 'gate', 'filament_pos'];

function unitOf(machine, gate) {
  for (let i = 0; i < (machine?.num_units ?? 1); i++) {
    const unit = machine?.[`unit_${i}`];
    if (unit && gate >= unit.first_gate && gate < unit.first_gate+unit.num_gates) return unit;
  }
  return null;
}

// Commands can be sent when connected to an enabled MMU that is not printing
function canSend(objects, connected) {
  const printing = (objects.print_stats?.state ?? objects.idle_timeout?.state ?? '') === 'printing'
    || objects.idle_timeout?.state === 'Printing';
  return connected && objects.mmu?.enabled !== false && !printing;
}

// The menu for one gate: [{id, label, icon, command, disabled}], in display order
export function gateMenu(objects, gate, connected) {
  const mmu = objects?.mmu || {}, unit = unitOf(objects?.mmu_machine, gate);
  const send = canSend(objects || {}, connected);
  const selected = gate === (mmu.gate ?? -1), position = mmu.filament_pos ?? -1;
  const loaded = position === FILAMENT_POS_LOADED, crossload = unit?.can_crossload ?? false;
  // A gate that is not selected can only be worked on where filament can be driven with another gate loaded
  const reachable = selected ? !loaded : crossload;
  // A scan jogs the gate's filament past its own reader; a reader shared by the unit cannot be scanned this way
  const hasNfc = Boolean(unit?.nfc_readers?.[gate-unit.first_gate]);
  const item = (id, label, command, disabled) => ({id, label, icon: ICONS[id], command, disabled: !send || disabled});
  return [
    item('select', 'Select', `MMU_SELECT GATE=${gate}`, selected || loaded),
    item('changeTool', 'Change Tool', `MMU_CHANGE_TOOL GATE=${gate}`, selected),
    item('preload', 'Preload', `MMU_PRELOAD GATE=${gate}`, !reachable),
    // Load and unload take no gate: they act on the selected one
    item('load', 'Load', 'MMU_LOAD', !selected || position !== FILAMENT_POS_UNLOADED),
    item('unload', 'Unload', 'MMU_UNLOAD', !selected || position === FILAMENT_POS_UNLOADED),
    item('eject', 'Eject', `MMU_EJECT GATE=${gate}`, !selected && !crossload),
    item('check', 'Check Gate', `MMU_CHECK_GATE GATE=${gate}`, false),
    ...(hasNfc ? [item('nfc', 'Scan NFC Tag', `MMU_NFC_SCAN GATE=${gate}`, !reachable)] : []),
  ];
}
