const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const productDetailSource = fs.readFileSync(require.resolve('./product-detail.js'), 'utf8');

const {
  buildOptionKey,
  focusTrapIndex,
  formatAdvisorSummary,
  galleryDragOffset,
  galleryHorizontalIntent,
  galleryStatus,
  galleryThumbnailLabel,
  MODAL_FOCUSABLE_SELECTOR,
  resolveOptionSelection,
  resolvePriceBreakdown,
  resolveGalleryStep,
  resolveGalleryAxis,
  shouldFinishCancelledGallerySwipe,
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

test('size advisor rejects distant availability and never offers distant alternatives', () => {
  for (const fit of ['classic', 'oversize']) {
    assert.deepEqual(recommendTshirtSize({
      height: 205, weight: 130, fit, availableSizes: ['S', 'M', 'UNKNOWN'],
    }), { ok: false, error: 'availability' });
  }
  assert.deepEqual(recommendTshirtSize({
    height: 150, weight: 45, fit: 'classic', availableSizes: ['XXL', '3XL'],
  }), { ok: false, error: 'availability' });
  const exact = recommendTshirtSize({
    height: 180, weight: 80, fit: 'classic', availableSizes: ['S', 'L', '3XL'],
  });
  assert.equal(exact.size, 'L');
  assert.equal(exact.alternative, '');
  assert.equal(exact.limitReached, false);
  const adjacent = recommendTshirtSize({
    height: 205, weight: 130, fit: 'classic', availableSizes: ['S', '2XL', 'not-a-size'],
  });
  assert.equal(adjacent.size, 'XXL');
  assert.equal(adjacent.targetSize, 'XXXL');
  assert.equal(adjacent.alternative, '');
  assert.equal(adjacent.limitReached, true);
  assert.deepEqual(recommendTshirtSize({
    height: 180, weight: 80, fit: 'classic', availableSizes: ['unknown', '6XL'],
  }), { ok: false, error: 'availability' });
});

test('size advisor rejects nonfinite measurements before availability selection', () => {
  for (const height of [NaN, Infinity, -Infinity, 'Infinity']) {
    assert.deepEqual(recommendTshirtSize({ height, weight: 80, fit: 'classic', availableSizes: ['L'] }),
      { ok: false, error: 'height' });
  }
  for (const weight of [NaN, Infinity, -Infinity, 'Infinity']) {
    assert.deepEqual(recommendTshirtSize({ height: 180, weight, fit: 'classic', availableSizes: ['L'] }),
      { ok: false, error: 'weight' });
  }
});

test('actual advisor form removes a previous recommendation after incompatible availability and can recover', () => {
  const input = (value) => ({ value, addEventListener() {} });
  const height = input('180'), weight = input('80'), fit = input('classic');
  const error = { textContent: '', hidden: true };
  const output = { textContent: '' }, summary = { textContent: '' };
  const alternative = { textContent: '' }, limit = { textContent: '' };
  const classes = new Set();
  const result = { hidden: false, style: {}, classList: {
    add(name) { classes.add(name); }, remove(name) { classes.delete(name); },
  }, querySelector(selector) { return ({
    '[data-size-advisor-size]': output, '[data-size-advisor-summary]': summary,
    '[data-size-advisor-alternative]': alternative, '[data-size-advisor-limit]': limit,
  })[selector]; } };
  let submit;
  const form = { dataset: { errorAvailability: 'Compare measurements or contact us.',
    copyClassic: 'Classic fit.', copyNearby: 'Nearby size: compare measurements before choosing.',
    copyAlternativeTighter: 'Tighter:', copyAlternativeLooser: 'Looser:', copyLimit: 'Available limit.' },
  querySelector(selector) { return ({ '[data-size-advisor-height]': height,
    '[data-size-advisor-weight]': weight, '[data-size-advisor-error]': error,
    'input[name="size_advisor_fit"]:checked': fit })[selector] || null; },
  querySelectorAll() { return [height, weight]; },
  addEventListener(type, listener) { if (type === 'submit') submit = listener; } };
  const comparison = { querySelector(selector) { return selector === '[data-size-advisor-form]' ? form : result; },
    querySelectorAll() { return []; } };
  const variant = { available_sizes_by_fit: { classic: ['M', 'L', 'XL'] } };
  const sandbox = { recommendTshirtSize, resolveAdvisorAvailableSizes, formatAdvisorSummary,
    currentVariantData: () => variant, selectedOptionValues: () => ({ fit: 'classic' }) };
  vm.createContext(sandbox);
  const start = productDetailSource.indexOf('  function initSizeAdvisor(');
  const end = productDetailSource.indexOf('  function updateSizeGuideAvailability(', start);
  vm.runInContext(productDetailSource.slice(start, end), sandbox);
  sandbox.initSizeAdvisor({ container: { dataset: { currentFit: 'classic' } } }, comparison, () => {});
  const calculate = () => submit({ preventDefault() {} });
  calculate();
  assert.equal(output.textContent, 'L');
  assert.equal(result.hidden, false);
  assert.ok(classes.has('is-ready'));

  height.value = '205'; weight.value = '130'; variant.available_sizes_by_fit.classic = ['S'];
  calculate();
  assert.equal(error.textContent, form.dataset.errorAvailability);
  assert.equal(error.hidden, false);
  assert.equal(output.textContent, '—');
  assert.equal(summary.textContent, '');
  assert.equal(alternative.textContent, '');
  assert.equal(limit.textContent, '');
  assert.equal(result.hidden, true);
  assert.equal(result.style.display, 'none');
  assert.equal(classes.has('is-ready'), false);

  variant.available_sizes_by_fit.classic = ['2XL']; calculate();
  assert.equal(output.textContent, '2XL');
  assert.equal(error.hidden, true);
  assert.equal(result.hidden, false);
  assert.equal(result.style.display, '');
  assert.match(summary.textContent, /Nearby size: compare measurements before choosing/);
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
  assert.equal(resolveSwipe({ dx: -70, dy: 12, width: 390 }), 1);
  assert.equal(resolveSwipe({ dx: 70, dy: 12, width: 390 }), -1);
  assert.equal(resolveSwipe({ dx: -35, dy: 80 }), 0);
  assert.equal(resolveSwipe({ dx: -30, dy: 4 }), 0);
  assert.equal(resolveSwipe({ dx: -15, dy: 3, width: 390, velocityX: -0.45 }), 1);
  assert.equal(resolveSwipe({ dx: -15, dy: 2, width: 390, velocityX: -0.12, horizontalIntent: true }), 1);
  assert.equal(resolveSwipe({ dx: -14, dy: 10, width: 390, velocityX: -0.12, horizontalIntent: true }), 0);
  assert.equal(resolveSwipe({ dx: -24, dy: 7, width: 430, velocityX: -0.62 }), 1);
  assert.equal(resolveSwipe({ dx: 22, dy: 31, width: 430, velocityX: 0.72 }), 0);
  assert.equal(resolveSwipe({ dx: 74, dy: 92, width: 430, horizontalIntent: true }), -1);
});

test('gallery horizontal intent waits out a slow diagonal drift', () => {
  assert.equal(galleryHorizontalIntent({ dx: 6, dy: 2 }), false);
  assert.equal(galleryHorizontalIntent({ dx: 10, dy: 3 }), true);
  assert.equal(galleryHorizontalIntent({ dx: 14, dy: 6 }), true);
  assert.equal(galleryHorizontalIntent({ dx: -22, dy: 18 }), true);
  assert.equal(galleryHorizontalIntent({ dx: -18, dy: 22 }), false);
  assert.equal(galleryHorizontalIntent({ dx: -48, dy: 12 }), true);
});

test('gallery touch axis locks once and ignores later cross-axis drift', () => {
  assert.equal(resolveGalleryAxis({ axis: 'pending', dx: 4, dy: 2 }), 'pending');
  assert.equal(resolveGalleryAxis({ axis: 'pending', dx: 4, dy: 3 }), 'horizontal');
  assert.equal(resolveGalleryAxis({ axis: 'pending', dx: 3, dy: 4 }), 'vertical');
  assert.equal(resolveGalleryAxis({ axis: 'pending', dx: 4, dy: 4 }), 'horizontal');
  assert.equal(resolveGalleryAxis({ axis: 'pending', dx: -6, dy: 2 }), 'horizontal');
  assert.equal(resolveGalleryAxis({ axis: 'horizontal', dx: -14, dy: 42 }), 'horizontal');
  assert.equal(resolveGalleryAxis({ axis: 'pending', dx: 3, dy: -10 }), 'vertical');
  assert.equal(resolveGalleryAxis({ axis: 'vertical', dx: 48, dy: -14 }), 'vertical');
});

test('gallery owns horizontal touchmove with a non-passive local guard', () => {
  const listenerStart = productDetailSource.indexOf("stage.addEventListener('touchmove'");
  assert.ok(listenerStart >= 0, 'gallery stage must register a touchmove guard');
  const listenerSource = productDetailSource.slice(listenerStart, listenerStart + 900);
  assert.match(listenerSource, /touchAxis === 'horizontal'/);
  assert.match(listenerSource, /event\.preventDefault\(\)/);
  assert.match(listenerSource, /\{ passive: false \}\)/);
});

test('gallery cancellation keeps pointer ownership after touch cleanup but preserves pinch zoom', () => {
  assert.equal(shouldFinishCancelledGallerySwipe({
    horizontalIntent: true,
    pointerAxis: 'horizontal',
    hadMultipleTouches: false,
    touchAxis: 'pending',
  }), true);
  assert.equal(shouldFinishCancelledGallerySwipe({
    horizontalIntent: true,
    pointerAxis: 'horizontal',
    hadMultipleTouches: true,
  }), false);
  assert.equal(shouldFinishCancelledGallerySwipe({
    horizontalIntent: false,
    pointerAxis: 'vertical',
    hadMultipleTouches: false,
  }), false);
});

test('gallery drag follows the finger and adds bounded resistance at an edge', () => {
  assert.equal(galleryDragOffset({ dx: -120, width: 390, atEdge: false }), -120);
  assert.equal(galleryDragOffset({ dx: 520, width: 390, atEdge: false }), 390);
  const resisted = galleryDragOffset({ dx: 220, width: 390, atEdge: true });
  assert.ok(resisted > 0);
  assert.ok(resisted < 47);
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

test('gallery labels format server-owned locale templates', () => {
  assert.equal(galleryStatus?.(0, 4), 'Фото 1 з 4');
  assert.equal(galleryStatus?.(3, 4), 'Фото 4 з 4');
  assert.equal(
    galleryStatus?.(0, 4, 'Photo {position} of {total}'),
    'Photo 1 of 4'
  );
  assert.equal(
    galleryStatus?.(3, 4, 'Фото {position} из {total}'),
    'Фото 4 из 4'
  );
  assert.equal(
    galleryThumbnailLabel?.(0, 'Product photo {position}'),
    'Product photo 1'
  );
  assert.equal(
    galleryThumbnailLabel?.(1, 'Product photo {position}'),
    'Product photo 2'
  );
  assert.equal(
    galleryThumbnailLabel?.(2, 'Фото товара {position}'),
    'Фото товара 3'
  );
});

test('modal focus trap wraps in both directions', () => {
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
