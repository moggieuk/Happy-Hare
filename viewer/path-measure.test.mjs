import test from 'node:test';
import assert from 'node:assert/strict';
import {measurePath} from './path-measure.js';
import {bowdenLane, virtualSelectorLayout} from './playback.js';

const close = (actual, expected, tolerance = 1e-6) => assert.ok(Math.abs(actual - expected) <= tolerance, `${actual} is not ${expected}`);

test('straight runs add up and points lie along them', () => {
  const path = measurePath('M10 20 V50 H40 L40 10');
  close(path.getTotalLength(), 30 + 30 + 40);
  assert.deepEqual(path.getPointAtLength(15), {x: 10, y: 35});
  assert.deepEqual(path.getPointAtLength(45), {x: 25, y: 50});
  assert.deepEqual(path.getPointAtLength(1000), {x: 40, y: 10});      // Clamped to the end, as the browser does
  assert.deepEqual(path.getPointAtLength(-5), {x: 10, y: 20});
});

test('a quadratic curve has its exact length and its points are on the curve', () => {
  // A parabola from (0,0) to (2,0) through its control point (1,1): length = sqrt(2) + asinh(1)
  const path = measurePath('M0 0 Q1 1 2 0');
  close(path.getTotalLength(), Math.SQRT2 + Math.asinh(1), 1e-9);
  const middle = path.getPointAtLength(path.getTotalLength() / 2);
  close(middle.x, 1, 1e-6); close(middle.y, .5, 1e-6);
});

test('equal steps along the Bowden are equal distances apart', () => {
  for (const guide of [bowdenLane(54, 60), virtualSelectorLayout({selector_type: 'VirtualSelector', gate: 1, tips: [0, 0, 0, 0]}, 54, 60)]) {
    const path = measurePath(guide.pathFrom()), length = path.getTotalLength();
    let previous = path.getPointAtLength(0);
    for (let d = 1; d <= length; d++) {
      const point = path.getPointAtLength(d);
      const chord = Math.hypot(point.x - previous.x, point.y - previous.y);
      assert.ok(chord <= 1 + 1e-9 && chord > .99, `step ${d} moved ${chord}`);   // A chord of a bend is slightly shorter
      previous = point;
    }
  }
});
