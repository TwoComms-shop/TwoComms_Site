const test = require('node:test');
const assert = require('node:assert/strict');

const {
  buildOptionKey,
  focusTrapIndex,
  galleryStatus,
  MODAL_FOCUSABLE_SELECTOR,
  resolveOptionSelection,
  resolveMaterialStory,
  resolveSwipe,
} = require('./product-detail.js');

test('buildOptionKey is stable for normalized product options', () => {
  assert.equal(
    buildOptionKey({ lining: 'fleece', fit: 'oversize' }),
    'fit=oversize;lining=fleece'
  );
  assert.equal(
    buildOptionKey({ fit: ' OVERSIZE ', lining: 'FLEECE' }),
    'fit=oversize;lining=fleece'
  );
});

test('horizontal swipe advances while vertical intent does not', () => {
  assert.equal(resolveSwipe({ dx: -70, dy: 12 }), 1);
  assert.equal(resolveSwipe({ dx: 70, dy: 12 }), -1);
  assert.equal(resolveSwipe({ dx: -35, dy: 80 }), 0);
  assert.equal(resolveSwipe({ dx: -30, dy: 4 }), 0);
});

test('material story accepts contextual payload and rejects generic fallbacks', () => {
  assert.deepEqual(resolveMaterialStory?.({
    material_story: {
      kind: 'fleece',
      title: 'Флісова основа',
      copy: 'Утеплений внутрішній шар.',
      icon: 'fleece',
    },
    marketing_html: 'Загальний опис товару',
  }), {
    kind: 'fleece',
    title: 'Флісова основа',
    copy: 'Утеплений внутрішній шар.',
    icon: 'fleece',
  });
  assert.equal(resolveMaterialStory?.({ marketing_html: 'Загальний опис товару' }), null);
});

test('gallery status and modal focus trap wrap in both directions', () => {
  assert.equal(galleryStatus?.(0, 4), 'Фото 1 з 4');
  assert.equal(galleryStatus?.(3, 4), 'Фото 4 з 4');
  assert.equal(focusTrapIndex?.({ currentIndex: 3, total: 4, shiftKey: false }), 0);
  assert.equal(focusTrapIndex?.({ currentIndex: 0, total: 4, shiftKey: true }), 3);
  assert.equal(focusTrapIndex?.({ currentIndex: 1, total: 4, shiftKey: false }), 2);
});

test('every modal focusable selector branch excludes inert and aria-hidden controls', () => {
  assert.equal(typeof MODAL_FOCUSABLE_SELECTOR, 'string');
  const branches = MODAL_FOCUSABLE_SELECTOR.split(',').map((branch) => branch.trim());
  assert.ok(branches.length >= 5);
  branches.forEach((branch) => {
    assert.match(branch, /:not\(\[tabindex="-1"\]\)/);
    assert.match(branch, /:not\(\[aria-hidden="true"\]\)/);
  });
});

test('invalid exact option selection repairs deterministically and disables impossible choice', () => {
  assert.equal(typeof resolveOptionSelection, 'function');
  const axes = [
    { code: 'lining', choices: [{ code: 'fleece' }, { code: 'no_fleece' }] },
    { code: 'fit', choices: [{ code: 'classic' }, { code: 'oversize' }] },
  ];
  const configurations = {
    'fit=classic;lining=fleece': {
      is_available: true,
      option_values: { lining: 'fleece', fit: 'classic' },
    },
    'fit=oversize;lining=fleece': {
      is_available: false,
      option_values: { lining: 'fleece', fit: 'oversize' },
    },
    'fit=classic;lining=no_fleece': {
      is_available: true,
      option_values: { lining: 'no_fleece', fit: 'classic' },
    },
    'fit=oversize;lining=no_fleece': {
      is_available: true,
      option_values: { lining: 'no_fleece', fit: 'oversize' },
    },
  };

  const resolved = resolveOptionSelection({
    axes,
    configurations,
    selectedValues: { lining: 'fleece', fit: 'oversize' },
  });

  assert.deepEqual(resolved.selectedValues, { lining: 'fleece', fit: 'classic' });
  assert.equal(resolved.isAvailable, true);
  assert.equal(resolved.choiceAvailability.fit.oversize, false);
  assert.equal(resolved.choiceAvailability.fit.classic, true);
  assert.equal(resolved.choiceAvailability.lining.no_fleece, true);
});
