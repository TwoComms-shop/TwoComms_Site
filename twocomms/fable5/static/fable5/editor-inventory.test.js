const test = require('node:test');
const assert = require('node:assert/strict');

let inventory = {};
try {
  inventory = require('./editor-inventory.js');
} catch (_) {
  // RED until the shared browser/Node inventory contract exists.
}

test('inventory resolution prefers exact fit and falls back to a general rule', () => {
  assert.equal(typeof inventory.resolveInventoryRule, 'function');
  const rules = [
    { fit_code: '', size: 'XXL', is_enabled: false, stock: 0 },
    { fit_code: 'oversize', size: 'XXL', is_enabled: true, stock: 2 },
  ];

  assert.deepEqual(
    inventory.resolveInventoryRule(rules, 'classic', 'XXL'),
    rules[0]
  );
  assert.deepEqual(
    inventory.resolveInventoryRule(rules, 'oversize', 'XXL'),
    rules[1]
  );
});

test('identical fit rows canonicalize back to one general inventory rule', () => {
  assert.equal(typeof inventory.canonicalizeInventoryRows, 'function');
  const canonical = inventory.canonicalizeInventoryRows([
    { fit_code: 'classic', size: 'M', is_enabled: true, stock: 7, note: '' },
    { fit_code: 'oversize', size: 'M', is_enabled: true, stock: 7, note: '' },
    { fit_code: 'classic', size: 'XXL', is_enabled: false, stock: 0, note: 'off' },
    { fit_code: 'oversize', size: 'XXL', is_enabled: false, stock: 0, note: 'off' },
  ]);

  assert.deepEqual(canonical, [
    { fit_code: '', size: 'M', is_enabled: true, stock: 7, note: '' },
    { fit_code: '', size: 'XXL', is_enabled: false, stock: 0, note: 'off' },
  ]);
});

test('different fit inventory remains fit-specific', () => {
  const rows = [
    { fit_code: 'classic', size: 'L', is_enabled: true, stock: 5, note: '' },
    { fit_code: 'oversize', size: 'L', is_enabled: false, stock: 0, note: '' },
  ];

  assert.deepEqual(inventory.canonicalizeInventoryRows?.(rows), rows);
});
