import test from 'node:test';
import assert from 'node:assert/strict';
import {ttgMapChart, spoolGroups} from './ttg-map.js';

const path=(markup,tool)=>markup.match(new RegExp(`<path data-tool="${tool}"[^>]*>`))[0];

test('the chart is sized by its gates and its endless-spool groups',()=>{
  assert.equal(ttgMapChart({numGates:4,ttgMap:[2,0,1,3],groups:[0,1,0,3]}).viewBox,'0 0 172 62');
  assert.equal(ttgMapChart({numGates:8,ttgMap:[0,1,2,3,4,5,6,7],groups:[0,1,2,3,4,5,6,7]}).viewBox,'0 0 160 110');
});

test('each tool has a label, a gate label on its row and a line to its mapped gate',()=>{
  const {markup}=ttgMapChart({numGates:4,ttgMap:[2,0,1,3],groups:[0,1,2,3]});
  assert.match(path(markup,0),/d="M 38 12 L 48 12 L 118 36 L 128 36"/);
  assert.match(path(markup,3),/d="M 38 48 L 48 48 L 118 48 L 128 48"/);
  assert.match(markup,/<text x="24" y="28" text-anchor="end" font-size="10" fill="currentColor">T1<\/text>/);
  assert.match(markup,/<text x="150" y="28" text-anchor="end" font-size="10" fill="currentColor">#1<\/text>/);
});

test('the selected tool is drawn last, thicker and highlighted, with the selected gate',()=>{
  const {markup}=ttgMapChart({numGates:4,ttgMap:[2,0,1,3],groups:[0,1,2,3],tool:1,gate:0});
  assert.match(path(markup,1),/stroke="#2ca9bc" stroke-width="4" marker-start="url\(#ttg-square-selected\)"/);
  assert.match(path(markup,0),/stroke="#808080" stroke-width="2" marker-start="url\(#ttg-square\)"/);
  assert.ok(markup.lastIndexOf('data-tool="1"')>markup.lastIndexOf('data-tool="3"'));
  assert.match(markup,/fill="#2ca9bc" font-weight="bold">T1</);
  assert.match(markup,/fill="#2ca9bc" font-weight="bold">#0</);
});

test('endless-spool groups of more than one gate get a bracket and a letter, the current one highlighted',()=>{
  assert.deepEqual(spoolGroups([0,1,0,3]),[[0,[0,2]]]);
  assert.deepEqual(spoolGroups([2,1,2,1]),[[1,[1,3]],[2,[0,2]]]);
  const {markup}=ttgMapChart({numGates:4,ttgMap:[2,0,1,3],groups:[0,1,0,3],tool:0,gate:-1});
  assert.match(markup,/<g data-group="0"><path d="M 165 12 L 160 12 M 165 36 L 160 36 M 165 12 L 165 36"/);
  // Tool 0 maps to gate 2, which is in group A
  assert.match(markup,/<text x="160" y="58" font-size="8" fill="#2ca9bc" font-weight="bold">A<\/text>/);
  assert.match(ttgMapChart({numGates:4,ttgMap:[2,0,1,3],groups:[0,1,0,3],gate:1}).markup,/fill="#808080">A</);
});
