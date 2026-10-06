const test = require('node:test');
const assert = require('node:assert/strict');
const { create, pricing, missingFiles, MAX_ITEMS } = require('../twocomms/twocomms_django_theme/static/js/custom-print-collection.js');
const item = (id, total = 500, quantity = 1) => ({ id, snapshot: { product: { type: 'tshirt' }, order: { quantity }, artwork: { service_kind: 'ready', files: [{ name: 'front.png', placement_key: 'front' }] }, pricing: { final_total: total } }, state: { product: { type: 'tshirt' } }, files: new Map([['front', [{ name: 'front.png' }]]]) });
test('different garments keep their files; editing replaces one item in place', () => {
  const store = create(); const first = item('a', 1000, 2); store.save(first); store.save(item('b', 900));
  first.snapshot.order.quantity = 9; first.files.get('front').push({ name: 'wrong.png' });
  assert.equal(store.get('a').snapshot.order.quantity, 2); assert.equal(store.get('a').files.get('front').length, 1);
  store.save(item('a', 1200, 2)); assert.deepEqual(store.list().map(i => i.id), ['a', 'b']);
  assert.equal(pricing(store.list(), 100).final_total, 2200); assert.equal(pricing(store.list()).quantity, 3);
});
test('drafts preserve configured items and explicitly require reupload', () => {
  const store = create(); store.save(item('a')); assert.equal(missingFiles(store.get('a')), false);
  const restored = create(); restored.restore(store.serialize()); assert.equal(missingFiles(restored.get('a')), true);
  restored.get('a').snapshot.artwork.service_kind = 'design'; assert.equal(missingFiles(restored.get('a')), true);
  restored.get('a').snapshot.artwork.files = []; assert.equal(missingFiles(restored.get('a')), false);
});
test('text-only zones do not require an impossible upload', () => {
  const saved = item('text'); saved.snapshot.artwork.files = []; saved.files.clear();
  saved.snapshot.placement_specs = [{ placement_key: 'sleeve_left', requires_artwork_file: false }];
  assert.equal(missingFiles(saved), false);
  saved.snapshot.placement_specs = [{ placement_key: 'front', requires_artwork_file: true }];
  assert.equal(missingFiles(saved), true);
});
test('unknown item price keeps collection total unknown and gift is charged once', () => {
  const store = create(); store.save(item('a', 1000, 2)); store.save(item('b', null));
  const result = pricing(store.list(), 100); assert.equal(result.final_total, null); assert.equal(result.known_total, 1000); assert.equal(result.gift_price, 100);
});
test('limit, stable IDs, removal and empty restore are enforced', () => {
  const store = create(); for (let i = 0; i < MAX_ITEMS; i++) store.save(item(String(i)));
  assert.throws(() => store.save(item('extra')), /item_limit/); store.save(item('0', 600));
  store.remove('0'); assert.equal(store.list().length, MAX_ITEMS - 1);
  store.restore([{ ...item('invalid!'), state: {} }]); assert.equal(store.list().length, 0);
});
