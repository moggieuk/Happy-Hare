/* Copyright (C) 2022-2026 Paul Morgan. GPLv3. */
// The tool-to-gate mapping chart: tools on the left, gates on the right, a line from each tool to its gate,
// and a bracket for each endless-spool group. The geometry is Mainsail Happy Hare Edition's (MmuTtgMap.vue).

const START_X = 10, START_Y = 8, ROW = 12, GROUP_SPACING = 12, MAP_SPACE = 80, LEADER = 10;
const GATE_X = START_X+LEADER+MAP_SPACE+LEADER+40, GROUP_X = GATE_X+10;
const REGULAR = '#808080', SELECTED = '#2ca9bc';

// Endless-spool groups of more than one gate: [[group id, gates in order]], by group id
export function spoolGroups(groups) {
  const byGroup = new Map();
  (groups || []).forEach((group, gate) => byGroup.set(group, [...(byGroup.get(group) || []), gate]));
  return [...byGroup].filter(([, gates]) => gates.length > 1).sort(([a], [b]) => a-b);
}

// {viewBox, markup}: the chart's contents for an <svg>. `tool` and `gate` are the selected ones (-1 for none).
export function ttgMapChart({numGates, ttgMap = [], groups = [], tool = -1, gate = -1}) {
  const drawn = spoolGroups(groups);
  const currentGroup = gate >= 0 ? groups[gate] : tool >= 0 && ttgMap[tool] >= 0 ? groups[ttgMap[tool]] : -1;
  const label = (x, row, text, selected) =>
    `<text x="${x}" y="${row*ROW+START_Y+8}" text-anchor="end" font-size="10" fill="${selected ? SELECTED : 'currentColor'}"${selected ? ' font-weight="bold"' : ''}>${text}</text>`;
  const line = from => {
    const selected = from === tool, to = ttgMap[from] ?? -1;
    const x = START_X+28, y1 = START_Y+from*ROW+4, y2 = START_Y+to*ROW+4, near = x+LEADER, far = near+MAP_SPACE;
    return `<path data-tool="${from}" d="M ${x} ${y1} L ${near} ${y1} L ${far-LEADER} ${y2} L ${far} ${y2}" fill="none" stroke="${selected ? SELECTED : REGULAR}" stroke-width="${selected ? 4 : 2}" marker-start="url(#ttg-square${selected ? '-selected' : ''})" marker-end="url(#ttg-arrow${selected ? '-selected' : ''})"/>`;
  };
  // The selected tool is drawn last, so its line lies over the others
  const tools = Array.from({length: numGates}, (_, i) => i).filter(i => i !== tool);
  if (tool >= 0 && tool < numGates) tools.push(tool);
  const rows = tools.map(i => label(START_X+14, i, `T${i}`, i === tool)+label(GATE_X, i, `#${i}`, i === gate)+line(i)).join('');
  const brackets = drawn.map(([group, gates], column) => {
    const x = GROUP_X+column*GROUP_SPACING, color = group === currentGroup ? SELECTED : REGULAR;
    const y = member => START_Y+4+member*ROW;
    const ticks = gates.map((member, i) => `M ${x+5} ${y(member)} L ${x} ${y(member)}`
      +(i ? ` M ${x+5} ${y(gates[i-1])} L ${x+5} ${y(member)}` : '')).join(' ');
    return `<g data-group="${group}"><path d="${ticks}" fill="none" stroke="${color}" stroke-width="2" stroke-linecap="round"/>`
      +`<text x="${x}" y="${START_Y+numGates*ROW+2}" font-size="8" fill="${color}"${group === currentGroup ? ' font-weight="bold"' : ''}>${String.fromCharCode(65+group)}</text></g>`;
  }).join('');
  const marker = (id, color, refX, shape) =>
    `<marker id="${id}" markerWidth="7" markerHeight="7" refX="${refX}" refY="3.5" orient="auto" markerUnits="userSpaceOnUse" fill="${color}">${shape}</marker>`;
  const defs = [[REGULAR, ''], [SELECTED, '-selected']].map(([color, suffix]) =>
    marker(`ttg-square${suffix}`, color, 7, '<rect width="7" height="7"/>')
    +marker(`ttg-arrow${suffix}`, color, 0, '<polygon points="0 0, 7 3.5, 0 7"/>')).join('');
  return {viewBox: `0 0 ${GROUP_X+drawn.length*GROUP_SPACING} ${START_Y+numGates*ROW+6}`, markup: `<defs>${defs}</defs>${rows}${brackets}`};
}
