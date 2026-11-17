import test from 'node:test';
import assert from 'node:assert/strict';
import {syncChildren} from './util.js';

// The few DOM calls syncChildren makes, with a count of the nodes it moved
function parentOf(names) {
  const parent={children:[], moves:0,
    insertBefore(node, before) {
      parent.moves++;
      parent.children=parent.children.filter(child=>child!==node);
      const index=before ? parent.children.indexOf(before) : parent.children.length;
      parent.children.splice(index, 0, node);
    }};
  const node=name=>({name, remove() { parent.children=parent.children.filter(child=>child!==this); }});
  parent.children=names.map(node);
  return {parent, node};
}

test('rows already in place are left alone when new ones are appended or one is replaced', () => {
  const {parent, node}=parentOf(['a', 'b', 'c']);
  const [a, b, c]=parent.children, d=node('d');
  syncChildren(parent, [a, b, c, d]);
  assert.deepEqual(parent.children, [a, b, c, d]);
  assert.equal(parent.moves, 1);                       // Only the new row was inserted
  const changed=node('c2');
  syncChildren(parent, [a, b, changed, d]);
  assert.deepEqual(parent.children, [a, b, changed, d]);
  assert.equal(parent.moves, 2);                       // One more: the replacement
  syncChildren(parent, [d]);
  assert.deepEqual(parent.children, [d]);
  assert.equal(parent.moves, 2);                       // Removing rows moves nothing
});
