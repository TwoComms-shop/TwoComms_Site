const test = require('node:test');
const assert = require('node:assert/strict');

const {
  buildOptionKey,
  focusTrapIndex,
  formatAdvisorSummary,
  galleryStatus,
  MODAL_FOCUSABLE_SELECTOR,
  resolveOptionSelection,
  resolvePriceBreakdown,
  resolveGalleryStep,
  resolveMaterialStory,
  resolveRestockSummary,
  recommendTshirtSize,
  resolveAdvisorAvailableSizes,
  resolveSizeGuideSelection,
  resolveSwipe,
} = require('./product-detail.js');

test('size advisor validates anthropometric bounds and fit', () => {
  assert.deepEqual(recommendTshirtSize({ height: 140, weight: 70, fit: 'classic' }), {
    ok: false,
    error: 'height',
  });
  assert.deepEqual(recommendTshirtSize({ height: 180, weight: 170, fit: 'classic' }), {
    ok: false,
    error: 'weight',
  });
  assert.deepEqual(recommendTshirtSize({ height: 180, weight: 80, fit: 'relaxed' }), {
    ok: false,
    error: 'fit',
  });
});

test('size advisor recommends balanced classic and oversize sizes', () => {
  const classic = recommendTshirtSize({
    height: 180,
    weight: 80,
    fit: 'classic',
    availableSizes: ['S', 'M', 'L', 'XL', 'XXL'],
  });
  const oversize = recommendTshirtSize({
    height: 180,
    weight: 80,
    fit: 'oversize',
    availableSizes: ['XS', 'S', 'M', 'L', 'XL', 'XXL'],
  });

  assert.equal(classic.ok, true);
  assert.equal(classic.size, 'L');
  assert.equal(oversize.ok, true);
  assert.equal(oversize.size, 'L');
});

test('size advisor balances height, weight and high BMI edge cases', () => {
  assert.equal(recommendTshirtSize({
    height: 198,
    weight: 65,
    fit: 'classic',
    availableSizes: ['S', 'M', 'L', 'XL', 'XXL'],
  }).size, 'L');
  assert.equal(recommendTshirtSize({
    height: 160,
    weight: 100,
    fit: 'classic',
    availableSizes: ['S', 'M', 'L', 'XL', 'XXL'],
  }).size, 'XXL');
});

test('size advisor never recommends unavailable or informational-only sizes', () => {
  assert.deepEqual(recommendTshirtSize({
    height: 180,
    weight: 80,
    fit: 'classic',
    availableSizes: [],
  }), {
    ok: false,
    error: 'availability',
  });

  const result = recommendTshirtSize({
    height: 195,
    weight: 125,
    fit: 'classic',
    availableSizes: ['S', 'M', 'L', 'XL', 'XXL'],
  });

  assert.equal(result.size, 'XXL');
  assert.ok(!['3XL', 'XXXL'].includes(result.size));
  assert.ok(result.limitReached);

  const sparse = recommendTshirtSize({
    height: 180,
    weight: 80,
    fit: 'classic',
    availableSizes: ['M', 'XL'],
  });
  assert.equal(sparse.size, 'XL');
  assert.equal(sparse.alternative, 'M');
  assert.equal(sparse.alternativeRelation, 'tighter');
});

test('size advisor uses exact option availability and supports catalogued 3XL', () => {
  const configurations = {
    'fit=classic;lining=plain': {
      option_values: { fit: 'classic', lining: 'plain' },
      is_available: true,
      size_availability: { S: false, M: false, L: true, XL: false, XXL: false },
    },
    'fit=oversize;lining=plain': {
      option_values: { fit: 'oversize', lining: 'plain' },
      is_available: false,
      size_availability: { S: true, M: true },
    },
  };

  assert.deepEqual(resolveAdvisorAvailableSizes({
    fit: 'classic',
    baseMatrix: { classic: ['S', 'M', 'L', 'XL', 'XXL'] },
    configurations,
    selectedValues: { fit: 'classic', lining: 'plain' },
  }), ['L']);
  assert.deepEqual(resolveAdvisorAvailableSizes({
    fit: 'oversize',
    baseMatrix: { oversize: ['S', 'M'] },
    configurations,
    selectedValues: { fit: 'classic', lining: 'plain' },
  }), []);

  const catalogued3xl = recommendTshirtSize({
    height: 205,
    weight: 130,
    fit: 'classic',
    availableSizes: ['S', 'M', 'L', 'XL', 'XXL', 'XXXL'],
  });
  assert.equal(catalogued3xl.size, 'XXXL');
});

test('size advisor summary uses localized units from the template', () => {
  assert.equal(formatAdvisorSummary({
    fitCopy: 'Classic fit.',
    height: '180',
    weight: '80',
    heightUnit: 'cm',
    weightUnit: 'kg',
  }), 'Classic fit. 180 cm · 80 kg.');
  assert.equal(formatAdvisorSummary({
    fitCopy: 'Классическая посадка.',
    height: '180',
    weight: '80',
    heightUnit: 'см',
    weightUnit: 'кг',
  }), 'Классическая посадка. 180 см · 80 кг.');
});

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
