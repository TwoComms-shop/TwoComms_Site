const test = require('node:test');
const assert = require('node:assert/strict');

const {
  buildOptionKey,
  focusTrapIndex,
  galleryStatus,
  MODAL_FOCUSABLE_SELECTOR,
  resolveOptionSelection,
  resolvePriceBreakdown,
  resolveGalleryStep,
  resolveMaterialStory,
  resolveRestockSummary,
  resolveSizeGuideSelection,
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

test('size guide selection stays independent and falls back to the first enabled guide', () => {
  assert.equal(
    resolveSizeGuideSelection({
      requestedFit: 'classic',
      fitCodes: ['classic', 'oversize'],
    }),
    'classic'
  );
  assert.equal(
    resolveSizeGuideSelection({
      requestedFit: 'classic',
      fitCodes: ['classic', 'oversize'],
      disabledFits: ['classic'],
    }),
    'oversize'
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

test('gallery arrow step advances one image and clamps at both boundaries', () => {
  assert.equal(resolveGalleryStep?.({ currentIndex: 0, total: 3, direction: 1 }), 1);
  assert.equal(resolveGalleryStep?.({ currentIndex: 1, total: 3, direction: -1 }), 0);
  assert.equal(resolveGalleryStep?.({ currentIndex: 0, total: 3, direction: -1 }), 0);
  assert.equal(resolveGalleryStep?.({ currentIndex: 2, total: 3, direction: 1 }), 2);
  assert.equal(resolveGalleryStep?.({ currentIndex: 4, total: 0, direction: 1 }), 0);
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

test('configurator errors fail closed while a normal empty legacy matrix stays available', () => {
  const axes = [
    { code: 'fit', choices: [{ code: 'classic', is_enabled: true }] },
  ];
  const failed = resolveOptionSelection({
    axes,
    configurations: {},
    selectedValues: { fit: 'classic' },
    configuratorError: 'combination_limit',
  });
  const legacy = resolveOptionSelection({
    axes,
    configurations: {},
    selectedValues: { fit: 'classic' },
  });

  assert.equal(failed.isAvailable, false);
  assert.equal(failed.hasMatrix, true);
  assert.equal(failed.choiceAvailability.fit.classic, false);
  assert.equal(legacy.isAvailable, true);
  assert.equal(legacy.hasMatrix, false);
  assert.equal(legacy.choiceAvailability.fit.classic, true);
});

test('restock summary keeps immutable product title separate from current variant color', () => {
  assert.deepEqual(resolveRestockSummary?.({
    baseProductTitle: 'Худі Tokyo Drift',
    currentVariantName: 'Чорний',
  }), {
    productTitle: 'Худі Tokyo Drift',
    colorName: 'Чорний',
  });
});

test('price breakdown normalizes additive components and exact override', () => {
  assert.deepEqual(resolvePriceBreakdown?.({
    material_delta: '400',
    option_delta: 200,
    total_delta: '600',
  }), {
    materialDelta: 400,
    optionDelta: 200,
    combinationOverride: null,
    totalDelta: 600,
  });
  assert.deepEqual(resolvePriceBreakdown?.({
    material_delta: 400,
    option_delta: 200,
    combination_override: '290',
    total_delta: '290',
  }), {
    materialDelta: 400,
    optionDelta: 200,
    combinationOverride: 290,
    totalDelta: 290,
  });
});
