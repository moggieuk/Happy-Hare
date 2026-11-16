/* Copyright (C) 2022-2026 Paul Morgan. GPLv3. */
// Length and point-at-length for the scene's own paths (absolute M, L, H, V and Q), worked out without the page.
// It stands in for SVGPathElement.getTotalLength() and getPointAtLength(), so geometry needs no element in the DOM.

const STEPS = 32;                                   // Table entries per curve; t is interpolated between them
const GAUSS = [[-.9061798459, .2369268851], [-.5384693101, .4786286705], [0, .5688888889],
               [.5384693101, .4786286705], [.9061798459, .2369268851]];

function line(x0, y0, x1, y1) {
  const length = Math.hypot(x1 - x0, y1 - y0);
  return {length, at: d => { const t = length ? d / length : 0; return {x: x0 + (x1 - x0) * t, y: y0 + (y1 - y0) * t}; }};
}

function quadratic(x0, y0, cx, cy, x1, y1) {
  const point = t => { const u = 1 - t; return {x: u*u*x0 + 2*u*t*cx + t*t*x1, y: u*u*y0 + 2*u*t*cy + t*t*y1}; };
  const speed = t => Math.hypot(2*(1-t)*(cx-x0) + 2*t*(x1-cx), 2*(1-t)*(cy-y0) + 2*t*(y1-cy));
  const between = (a, b) => { const half = (b - a) / 2; return half * GAUSS.reduce((sum, [node, weight]) => sum + weight * speed(a + half + half * node), 0); };
  const lengths = [0];
  for (let i = 0; i < STEPS; i++) lengths.push(lengths[i] + between(i / STEPS, (i + 1) / STEPS));
  const length = lengths[STEPS];
  return {length, at: d => {
    let low = 0, high = STEPS;
    while (high - low > 1) { const middle = (low + high) >> 1; if (lengths[middle] <= d) low = middle; else high = middle; }
    const span = lengths[high] - lengths[low], from = low / STEPS;
    let t = from + (span ? (d - lengths[low]) / span : 0) / STEPS;
    for (let i = 0; i < 2; i++) {                    // The curve is not traversed at constant speed: correct the estimate
      const rate = speed(t);
      if (rate) t = Math.max(from, Math.min(high / STEPS, t + (d - lengths[low] - between(from, t)) / rate));
    }
    return point(t);
  }};
}

const measured = new Map();

export function measurePath(d) {
  const cached = measured.get(d);
  if (cached) return cached;
  const tokens = d.match(/[MLHVQ]|-?\d*\.?\d+(?:e[-+]?\d+)?/gi) || [];
  const segments = [];
  let x = 0, y = 0, command = null, i = 0;
  const number = () => Number(tokens[i++]);
  while (i < tokens.length) {
    if (/[MLHVQ]/i.test(tokens[i])) command = tokens[i++];
    if (command === 'M') { x = number(); y = number(); command = 'L'; continue; }
    let segment;
    if (command === 'L') { const nx = number(), ny = number(); segment = line(x, y, nx, ny); x = nx; y = ny; }
    else if (command === 'H') { const nx = number(); segment = line(x, y, nx, y); x = nx; }
    else if (command === 'V') { const ny = number(); segment = line(x, y, x, ny); y = ny; }
    else if (command === 'Q') { const cx = number(), cy = number(), nx = number(), ny = number(); segment = quadratic(x, y, cx, cy, nx, ny); x = nx; y = ny; }
    else throw Error(`Unsupported path command in "${d}"`);
    segments.push(segment);
  }
  const starts = [];
  let total = 0;
  for (const segment of segments) { starts.push(total); total += segment.length; }
  const result = {
    getTotalLength: () => total,
    getPointAtLength: distance => {
      const d = Math.max(0, Math.min(total, distance));
      let index = segments.length - 1;
      while (index > 0 && starts[index] > d) index--;
      return segments.length ? segments[index].at(d - starts[index]) : {x, y};
    }};
  if (measured.size > 200) measured.clear();          // Paths repeat from frame to frame; a changed layout starts over
  measured.set(d, result);
  return result;
}
