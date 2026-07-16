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

function makeSurface(rows, fitCode = 'classic', size = 'M') {
  const rule = inventory.resolveInventoryRule(rows, fitCode, size);
  return { enabled: rule?.is_enabled !== false, stock: rule?.stock ?? null };
}

function writeFromSurface(variant, surfaces, source, stock) {
  surfaces[source] = { enabled: true, stock };
  const draft = inventory.replaceInventoryDraft(variant, [
    { fit_code: 'classic', size: 'M', is_enabled: true, stock, note: '' },
    { fit_code: 'oversize', size: 'M', is_enabled: true, stock, note: '' },
  ]);
  surfaces.card = makeSurface(draft);
  surfaces.stock = makeSurface(draft);
}

test('card edit wins after a pending stock save and becomes the next global payload', () => {
  assert.equal(typeof inventory.replaceInventoryDraft, 'function');
  assert.equal(typeof inventory.snapshotInventoryDraft, 'function');
  const variant = { sizes: [], _revision: 0, _sizesRevision: 0 };
  const surfaces = { card: {}, stock: {} };

  writeFromSurface(variant, surfaces, 'stock', 7);
  const pendingStockSave = inventory.snapshotInventoryDraft(variant);
  writeFromSurface(variant, surfaces, 'card', 8);
  const nextGlobalPayload = inventory.snapshotInventoryDraft(variant);

  assert.equal(makeSurface(pendingStockSave).stock, 7);
  assert.equal(makeSurface(variant.sizes).stock, 8);
  assert.deepEqual(surfaces, {
    card: { enabled: true, stock: 8 },
    stock: { enabled: true, stock: 8 },
  });
  assert.equal(makeSurface(nextGlobalPayload).stock, 8);
  assert.equal(variant._sizesRevision, 2);
  assert.equal(variant._revision, 2);
  assert.equal(variant._sizesDirty, true);
  assert.equal(variant._dirty, true);
});

test('stock edit wins after a card draft and both surfaces follow shared state', () => {
  const variant = { sizes: [], _revision: 0, _sizesRevision: 0 };
  const surfaces = { card: {}, stock: {} };

  writeFromSurface(variant, surfaces, 'card', 3);
  writeFromSurface(variant, surfaces, 'stock', 6);

  assert.equal(makeSurface(variant.sizes).stock, 6);
  assert.deepEqual(surfaces, {
    card: { enabled: true, stock: 6 },
    stock: { enabled: true, stock: 6 },
  });
  assert.equal(makeSurface(inventory.snapshotInventoryDraft(variant)).stock, 6);
});

test('shared draft keeps exact inventory where fits differ and generalizes equal rows', () => {
  const variant = { sizes: [], _revision: 0, _sizesRevision: 0 };

  inventory.replaceInventoryDraft(variant, [
    { fit_code: 'classic', size: 'L', is_enabled: true, stock: 2, note: '' },
    { fit_code: 'oversize', size: 'L', is_enabled: false, stock: 0, note: '' },
  ]);
  assert.equal(variant.sizes.length, 2);
  assert.equal(makeSurface(variant.sizes, 'classic', 'L').stock, 2);
  assert.equal(makeSurface(variant.sizes, 'oversize', 'L').enabled, false);

  inventory.replaceInventoryDraft(variant, [
    { fit_code: 'classic', size: 'L', is_enabled: true, stock: 5, note: '' },
    { fit_code: 'oversize', size: 'L', is_enabled: true, stock: 5, note: '' },
  ]);
  assert.deepEqual(variant.sizes, [
    { fit_code: '', size: 'L', is_enabled: true, stock: 5, note: '' },
  ]);
});
