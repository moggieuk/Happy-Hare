import test from 'node:test';
import assert from 'node:assert/strict';
import { renderSpool } from './spool.js';

const options={idPrefix:'test',style:'cardboard-windowed',color:'24ba5e',
  fillPercent:90,viewAngle:78*Math.PI/180};

test('spools own every SVG resource and can coexist without ID collisions',()=>{
  const ids=new Set();
  for (const idPrefix of ['gate-0','preview-0']) {
    const svg=renderSpool({...options,idPrefix,arrowOpacity:1,glow:1});
    const local=new Set([...svg.matchAll(/id="([^"]+)"/g)].map(match=>match[1]));
    for (const id of local) { assert.ok(!ids.has(id)); ids.add(id); }
    for (const [,id] of svg.matchAll(/url\(#([^)]+)\)/g)) assert.ok(local.has(id),id);
  }
});

test('all styles, fill levels and edge-on projections produce finite geometry',()=>{
  for (const style of ['cardboard-windowed','dark-windowed','cardboard-solid','dark-solid'])
  for (const fillPercent of [0,45,100]) for (const degrees of [0,45,78,90]) {
    const svg=renderSpool({...options,style,fillPercent,viewAngle:degrees*Math.PI/180,color:'24ba5e80'});
    assert.doesNotMatch(svg,/NaN|Infinity|undefined/);
    assert.match(svg,/data-part="spool-core-tube"/);
    assert.equal(svg.includes('data-part="spool-window-sidewall"'),!style.endsWith('-solid'));
    assert.equal(svg.includes('data-part="spool-contact-shadow"'),fillPercent>0);
  }
});

test('rotation changes the face while the direction arrow stays stationary',()=>{
  const a=renderSpool({...options,arrowOpacity:.5,rotation:0,direction:'rewind'});
  const b=renderSpool({...options,arrowOpacity:.5,rotation:73,direction:'rewind'});
  const arrow=svg=>svg.match(/<g data-part="espooler-direction".*?<\/g>/)[0];
  assert.notEqual(a,b);
  assert.equal(arrow(a),arrow(b));
  assert.match(arrow(a),/scale\(1 -1\)/);
  assert.doesNotMatch(renderSpool(options),/data-part="espooler-direction"/);
});


test('percentage labels are optional, centered, and bounded from 0 to 100',()=>{
  assert.doesNotMatch(renderSpool(options),/data-part="spool-fill-label"/);
  for (const [fillPercent,label] of [[0,0],[45.6,46],[100,100]]) {
    const svg=renderSpool({...options,fillPercent,showFillPercent:true});
    assert.match(svg,new RegExp(`>${label}%</text>`));
    assert.match(svg,/data-part="spool-fill-label"[^>]*text-anchor="middle"/);
  }
});
