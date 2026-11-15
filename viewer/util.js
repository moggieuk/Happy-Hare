/* Copyright (C) 2022-2026 Paul Morgan. GPLv3. */
// Small helpers shared by the viewer modules.
export const $ = id => document.getElementById(id);
export const DEFAULT_COLOR = '808182';
export const finite = value => typeof value === 'number' && Number.isFinite(value);
export const number = (value, fallback=0) => finite(value) ? value : fallback;
export const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
export const clamp01 = v => clamp(v, 0, 1);
export const round1 = value => Number(value.toFixed(1));
// A printer object's status for the readout, with distances (and any other number) to two decimal places.
export const statusText = status => status === null ? 'No status exposed'
  : JSON.stringify(status, (key, value) => finite(value) ? Number(value.toFixed(2)) : value);
// 'unit0:extruder' -> 'extruder'
export const short = name => name.split(':').at(-1);
export const positions = ['UNLOADED', 'HOMED TO GATE', 'START BOWDEN', 'IN BOWDEN', 'END BOWDEN',
  'HOMED TO EXTRUDER', 'EXTRUDER ENTRY', 'IN EXTRUDER', 'HOMED TO TOOLHEAD', 'IN TOOLHEAD', 'LOADED IN NOZZLE'];

export const escape = (s) =>
  String(s).replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );

export function options(node, items) {
  node.replaceChildren(
    ...items.map(([value, label]) => {
      const o = document.createElement("option");
      o.value = value;
      o.textContent = label;
      return o;
    }),
  );
}
