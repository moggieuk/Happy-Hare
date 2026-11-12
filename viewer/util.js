/* Copyright (C) 2022-2026 Paul Morgan. GPLv3. */
// Small helpers shared by the viewer modules.
export const DEFAULT_COLOR = '808182';
export const finite = value => typeof value === 'number' && Number.isFinite(value);
export const number = (value, fallback=0) => finite(value) ? value : fallback;
export const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
export const round1 = value => Number(value.toFixed(1));
// 'unit0:extruder' -> 'extruder'
export const short = name => name.split(':').at(-1);
export const positions = ['UNLOADED', 'HOMED TO GATE', 'START BOWDEN', 'IN BOWDEN', 'END BOWDEN',
  'HOMED TO EXTRUDER', 'EXTRUDER ENTRY', 'IN EXTRUDER', 'HOMED TO TOOLHEAD', 'IN TOOLHEAD', 'LOADED IN NOZZLE'];
