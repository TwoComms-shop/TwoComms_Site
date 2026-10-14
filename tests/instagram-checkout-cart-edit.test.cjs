const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const assert = require('node:assert/strict');

const source = fs.readFileSync(path.resolve(__dirname, '../twocomms/twocomms_django_theme/static/js/instagram-checkout.js'), 'utf8');
const cartSource = source.slice(source.indexOf('  const cartProducts ='), source.indexOf('  let dialogTrigger ='));
const flush = () => new Promise((resolve) => setImmediate(resolve));
const state = (changes = {}) => ({
  revision: 4, digest: 'basket-four', selection_revision: 7,
  items: [{ line_id: 'line-one', quantity: 1, unit_price: '100.00', line_total: '100.00' }, { line_id: 'line-two', quantity: 2, unit_price: '150.00', line_total: '300.00' }],
  total: '400.00', catalog_total: '400.00', discount: '0.00', charge_now: '400.00', currency: 'UAH',
  is_prepayment: false, has_discount: false, can_edit: true, can_pay: true, review_needed: false,
  ...changes,
});

function fixture(initial = state(), fetchHandler = async () => { throw new Error('offline'); }, storage = new Map()) {
  const node = (dataset = {}) => ({
    dataset, textContent: '', hidden: false, disabled: false, removed: false, style: {},
    listeners: {}, attributes: {},
    classList: { values: new Set(), add(value) { this.values.add(value); }, toggle(value, on) { on ? this.values.add(value) : this.values.delete(value); } },
    addEventListener(name, callback) { this.listeners[name] = callback; },
    setAttribute(name, value) { this.attributes[name] = value; },
    removeAttribute(name) { delete this.attributes[name]; },
    querySelector() { return null; }, querySelectorAll() { return []; },
    remove() { this.removed = true; },
  });
  const root = node({ proposalUrl: '/ig/checkout/proposal/' });
  const products = node();
  const feedback = node();
  const status = node();
  const retry = node();
  const pay = node();
  const rail = node();
  const benefit = node({ manualPayer: initial.manualPayer || '' });
  const benefitNodes = Object.fromEntries(['title', 'detail', 'meter', 'update', 'progress', 'bar', 'threshold'].map((key) => [`[data-delivery-benefit-${key}]`, node()]));
  benefitNodes['[data-delivery-policy-ack-wrap]'] = node();
  benefitNodes['[data-delivery-policy-ack]'] = node();
  benefitNodes['[data-delivery-policy-digest]'] = node();
  benefit.querySelector = (selector) => benefitNodes[selector] || null;
  const count = node({ singular: 'item', plural: 'items' });
  const totals = Object.fromEntries(['total', 'catalog-total', 'discount', 'charge-now', 'payment-amount'].map((key) => [key, node()]));
  const terms = { checked: true, dispatchEvent() {} };
  const rows = initial.items.map((item) => {
    const row = node({ cartLine: item.line_id });
    row.quantity = node(); row.amount = node(); row.unit = node();
    row.amount.textContent = `${item.line_total} UAH`;
    row.actions = ['decrease', 'increase', 'remove'].map((action) => {
      const button = node({ cartAction: action });
      button.closest = (selector) => selector === '[data-cart-line]' ? row : selector === '[data-cart-action]' ? button : null;
      return button;
    });
    row.querySelectorAll = () => [row.quantity];
    row.querySelector = (selector) => selector === '[data-cart-line-total]' ? row.amount : selector === '[data-cart-unit-price]' ? row.unit : null;
    return row;
  });
  products.querySelector = () => rows[0]?.actions[0] || null;
  products.querySelectorAll = (selector) => selector === '[data-cart-line]' ? rows.filter((row) => !row.removed) : rows.filter((row) => !row.removed).flatMap((row) => row.actions);
  const selectors = { '[data-cart-products]': products, '[data-cart-feedback]': feedback, '[data-cart-status]': status, '[data-cart-retry]': retry, '[data-cart-count]': count, '[data-delivery-benefit]': benefit, ...benefitNodes };
  Object.entries(totals).forEach(([key, value]) => { selectors[key === 'payment-amount' ? '[data-payment-amount] strong' : `[data-cart-${key}]`] = value; });
  const expectedRevision = { value: '4' }, expectedDigest = { value: 'basket-four' };
  const document = {
    documentElement: { dataset: {} },
    getElementById: () => ({ textContent: JSON.stringify(initial) }),
    querySelector: (selector) => selectors[selector] || null,
    querySelectorAll: (selector) => selector === '[name="expected_revision"]' ? [expectedRevision] : selector === '[name="expected_digest"]' ? [expectedDigest] : selectors[selector] ? [selectors[selector]] : [],
  };
  const calls = [];
  let sequence = 0;
  const context = vm.createContext({
    root, document, locale: 'en', checkoutExpired: false, paymentButton: pay, paymentRail: rail,
    checkoutForm: { elements: { namedItem: (name) => name === 'assisted_terms_accepted' ? terms : null } }, priceDetails: null, priceDialog: null,
    closeCheckoutDialog() {}, csrfToken: () => 'csrf', readJsonResponse: (response) => response.json(),
    window: { crypto: { randomUUID: () => `00000000-0000-4000-8000-${String(++sequence).padStart(12, '0')}` }, location: { pathname: '/ig/checkout/proposal/', reload() {}, assign() {} }, sessionStorage: { getItem: (key) => storage.get(key) || null, setItem: (key, value) => storage.set(key, value), removeItem: (key) => storage.delete(key) } },
    FormData, Event,
    fetch: async (_url, options) => {
      const body = Object.fromEntries(options.body.entries());
      calls.push(body);
      return fetchHandler(body, calls.length);
    },
  });
  vm.runInContext(`${cartSource}\nglobalThis.cartTest = { applyCartState, cartCanPay, submitCartOperation };`, context);
  return { node, rows, products, feedback, status, retry, pay, rail, totals, terms, calls, storage, expectedRevision, expectedDigest, context, benefit, benefitNodes,
    click(index, action) { const target = rows[index].actions.find((button) => button.dataset.cartAction === action); products.listeners.click({ target }); },
  };
}
const response = (cart_state, status = 200) => ({ ok: status < 400, status, json: async () => ({ cart_state, ...(status >= 400 ? { error: 'revision_changed' } : {}) }) });
const shipping = (changes = {}) => ({ schema: 'shipping-policy.v1', automatic: true, authority: 'automatic_threshold',
  basis: 'approved_merchandise_after_discounts', threshold: '3000.00', basis_total: '2500.00', remaining: '500.00',
  currency: 'UAH', eligible: false, progress: 83, ack_required: false, ack_digest: 'policy-one', ...changes });

test('quantity changes wait for authoritative totals and dispatch one CAS operation', async () => {
  let resolve;
  const f = fixture(state(), () => new Promise((done) => { resolve = done; }));
  f.click(0, 'increase');
  f.click(0, 'increase');
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0].quantity, '2');
  assert.equal(f.calls[0].expected_revision, '4');
  assert.equal(f.calls[0].expected_digest, 'basket-four');
  assert.equal(f.calls[0].expected_selection_revision, '7');
  assert.equal(f.pay.disabled, true);
  assert.equal(f.rows[0].amount.textContent, '100.00 UAH');
  resolve(response(state({ revision: 5, digest: 'basket-five', items: [{ ...state().items[0], quantity: 2, line_total: '183.50' }, state().items[1]], total: '483.50' })));
  await flush();
  assert.equal(f.rows[0].amount.textContent, '183.50 UAH');
  assert.equal(f.totals.total.textContent, '483.50 UAH');
  assert.equal(f.totals['payment-amount'].textContent, '483.50 UAH');
  assert.equal(f.pay.disabled, false);
  assert.equal(f.terms.checked, false);
  assert.equal(f.expectedRevision.value, '5');
  assert.equal(f.expectedDigest.value, 'basket-five');
});

test('lost responses block new edits and replay the exact UUID and CAS after reload', async () => {
  const f = fixture();
  f.click(0, 'remove');
  await flush();
  assert.equal(f.rows[0].removed, false);
  assert.equal(f.pay.disabled, true);
  assert.equal(f.retry.hidden, false);
  f.click(1, 'increase');
  assert.equal(f.calls.length, 1);
  const updated = state({ revision: 5, digest: 'basket-five', items: [state().items[1]], total: '300.00' });
  const reload = fixture(updated, async () => response(updated), f.storage);
  assert.equal(reload.pay.disabled, true);
  await reload.context.cartTest.submitCartOperation();
  assert.deepEqual(reload.calls[0], f.calls[0]);
  assert.equal(reload.pay.disabled, false);
  assert.equal(reload.storage.size, 0);
});

test('stale edits render the current server basket and never retry as a new operation', async () => {
  const current = state({ revision: 6, digest: 'basket-six', items: [{ ...state().items[0], quantity: 3, line_total: '300.00' }], total: '300.00' });
  const f = fixture(state(), async () => response(current, 409));
  f.click(0, 'increase');
  await flush();
  assert.equal(f.rows[0].quantity.textContent, '3');
  assert.equal(f.rows[1].removed, true);
  assert.equal(f.totals.total.textContent, '300.00 UAH');
  assert.match(f.status.textContent, /already been updated/);
  assert.equal(f.retry.hidden, true);
  assert.equal(f.calls.length, 1);
});

test('manager review saves changes but keeps payment unavailable and unknown prices explicit', async () => {
  const pending = state({ revision: 5, digest: 'review', can_edit: false, can_pay: false, review_needed: true, total: null });
  const f = fixture(state(), async () => response(pending));
  f.click(0, 'increase');
  await flush();
  assert.match(f.status.textContent, /manager will confirm/);
  assert.equal(f.pay.disabled, true);
  assert.equal(f.totals.total.textContent, 'After confirmation');
  assert.equal(f.feedback.classList.values.has('is-review'), true);
  assert.equal(f.rows[0].actions.every((button) => button.disabled), true);
});

test('pay-only and bank-locked capabilities cannot be overridden by a DOM click', async () => {
  for (const can_pay of [true, false]) {
    const f = fixture(state({ can_edit: false, can_pay }));
    f.rows[0].actions[1].disabled = false;
    f.click(0, 'increase');
    await flush();
    assert.equal(f.calls.length, 0);
    assert.equal(f.pay.disabled, !can_pay);
  }
});

test('last-item removal shows empty basket and prevents a zero-value payment', async () => {
  const empty = state({ revision: 5, digest: 'empty', items: [], total: '0.00', catalog_total: '0.00', charge_now: '0.00', can_edit: false, can_pay: false });
  const f = fixture(state({ items: [state().items[0]] }), async () => response(empty));
  f.click(0, 'remove');
  await flush();
  assert.equal(f.rows[0].removed, true);
  assert.equal(f.pay.disabled, true);
  assert.equal(f.totals.total.textContent, '0.00 UAH');
  assert.match(f.status.textContent, /basket is empty/);
});

function paymentFixture(initial = state(), validate = async () => ({ ok: true }), fetchHandler = async () => { throw new Error('lost payment response'); }) {
  const f = fixture(initial, fetchHandler);
  const form = f.node();
  const error = f.node();
  error.focus = () => {};
  form.action = '/ig/checkout/proposal/';
  form.elements = { namedItem: () => null };
  form.checkValidity = () => true;
  form.querySelector = (selector) => selector === '[data-form-error]' ? error : null;
  Object.assign(f.context, {
    checkoutForm: form,
    HTMLElement: class {},
    prefersReducedMotion: true,
    setActionLabel(button, value) { button.label = value; },
    paymentErrorMessage: (code) => code,
    paymentErrorField: (code) => code,
    fallbackErrors: { en: 'validation unavailable' },
    trackCheckoutEvent() {},
    FormData: class extends FormData { constructor() { super(); } },
  });
  f.context.window.TwoCommsNovaPoshta = { validateForm: validate };
  f.context.window.location.assign = (url) => { f.redirect = url; };
  vm.runInContext(source.slice(source.indexOf('  const form = checkoutForm;'), source.indexOf('  const reissueForm =')), f.context);
  return { ...f, form, error, submit: () => form.listeners.submit({ preventDefault() {}, stopImmediatePropagation() {} }) };
}

test('payment reserves dispatch before asynchronous address validation and includes current CAS', async () => {
  let release;
  let validations = 0;
  const f = paymentFixture(state(), () => { validations += 1; return new Promise((resolve) => { release = resolve; }); }, async () => ({ ok: true, json: async () => ({ invoice_url: 'https://pay.example/one' }) }));
  const first = f.submit();
  await f.submit();
  f.click(0, 'increase');
  assert.equal(validations, 1);
  assert.equal(f.calls.length, 0);
  assert.equal(f.pay.disabled, true);
  release({ ok: true });
  await first;
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0].expected_revision, '4');
  assert.equal(f.calls[0].expected_digest, 'basket-four');
});

test('unknown payment outcome freezes invoice dispatch and basket mutation', async () => {
  const f = paymentFixture();
  await f.submit();
  assert.equal(f.calls.length, 1);
  assert.equal(f.pay.disabled, true);
  assert.equal(f.error.textContent, 'provider_ambiguous');
  await f.submit();
  f.click(0, 'remove');
  assert.equal(f.calls.length, 1);
});

test('manager review capability blocks payment even when the DOM submit is invoked', async () => {
  let validations = 0;
  const f = paymentFixture(state({ can_edit: false, can_pay: false, review_needed: true }), async () => { validations += 1; return { ok: true }; });
  await f.submit();
  assert.equal(validations, 0);
  assert.equal(f.calls.length, 0);
  assert.match(f.status.textContent, /manager will confirm/);
});

test('free delivery progress uses exact server remaining and never rounds a near-threshold basket to achieved', () => {
  const f = fixture(state({ free_delivery: shipping({ basis_total: '2999.99', remaining: '0.01', progress: 99 }) }));
  assert.match(f.benefitNodes['[data-delivery-benefit-title]'].textContent, /0\.01 UAH/);
  assert.equal(f.benefitNodes['[data-delivery-benefit-bar]'].style.width, '99%');
  assert.equal(f.benefitNodes['[data-delivery-benefit-progress]'].attributes['aria-valuenow'], '99');
  assert.equal(f.benefit.classList.values.has('is-achieved'), false);
  assert.match(f.benefitNodes['[data-delivery-benefit-detail]'].textContent, /Items after discounts: 2,999\.99 UAH/);
  assert.equal(f.benefitNodes['[data-delivery-policy-digest]'].disabled, false);
  assert.equal(f.benefitNodes['[data-delivery-policy-digest]'].value, 'policy-one');
  assert.equal(f.benefitNodes['[data-delivery-policy-ack]'].required, false);
});

test('quantity response announces earned delivery and renders full merchandise progress during prepayment', async () => {
  const reached = state({ revision: 5, digest: 'five', total: '3100.00', charge_now: '200.00', is_prepayment: true,
    free_delivery: shipping({ basis_total: '3100.00', remaining: '0.00', progress: 100, eligible: true }) });
  const f = fixture(state({ free_delivery: shipping() }), async () => response(reached));
  f.click(0, 'increase');
  await flush();
  assert.equal(f.benefitNodes['[data-delivery-benefit-title]'].textContent, 'Your delivery is free');
  assert.equal(f.benefitNodes['[data-delivery-benefit-bar]'].style.width, '100%');
  assert.match(f.benefitNodes['[data-delivery-benefit-detail]'].textContent, /3,100 UAH/);
  assert.match(f.benefitNodes['[data-delivery-benefit-update]'].textContent, /now free/);
  assert.equal(f.totals['payment-amount'].textContent, '200.00 UAH');
});

test('lost free delivery requires fresh consent bound to the server economics digest', async () => {
  const lost = state({ revision: 5, digest: 'five', free_delivery: shipping({ ack_required: true, ack_digest: 'loss-one' }) });
  const f = fixture(state({ free_delivery: shipping({ eligible: true, progress: 100, remaining: '0.00' }) }), async () => response(lost));
  f.click(0, 'decrease');
  // The first line is already quantity one; remove another line instead.
  f.click(1, 'remove');
  await flush();
  const ack = f.benefitNodes['[data-delivery-policy-ack]'];
  const digest = f.benefitNodes['[data-delivery-policy-digest]'];
  assert.equal(ack.required, true);
  assert.equal(ack.checked, false);
  assert.equal(digest.value, 'loss-one');
  assert.match(f.benefitNodes['[data-delivery-benefit-update]'].textContent, /paid separately/);
  ack.checked = true;
  f.context.cartTest.applyCartState(lost);
  assert.equal(ack.checked, true);
  f.context.cartTest.applyCartState(state({ revision: 6, digest: 'six', free_delivery: shipping({ ack_required: true, ack_digest: 'loss-two' }) }));
  assert.equal(ack.checked, false);
  assert.equal(digest.value, 'loss-two');
});

test('manual merchant terms and pending review cannot acquire an automatic threshold bar', () => {
  const manual = fixture(state({ free_delivery: null, manualPayer: 'merchant' }));
  assert.equal(manual.benefitNodes['[data-delivery-benefit-title]'].textContent, 'TwoComms covers delivery');
  assert.equal(manual.benefitNodes['[data-delivery-benefit-meter]'].hidden, true);
  assert.match(manual.benefitNodes['[data-delivery-benefit-detail]'].textContent, /separate from/);
  manual.context.cartTest.applyCartState(state({ free_delivery: null, review_needed: true, can_pay: false }));
  assert.equal(manual.benefitNodes['[data-delivery-benefit-title]'].textContent, 'Confirming your delivery terms');
  assert.equal(manual.benefitNodes['[data-delivery-policy-ack]'].required, false);
});

test('promo loss response applies evaluated delivery terms and fresh consent before another invoice', async () => {
  const evaluated = state({ total: '2870.00', charge_now: '200.00', catalog_total: '3100.00', discount: '230.00', payment_preview: true, is_prepayment: true,
    free_delivery: shipping({ basis_total: '2870.00', remaining: '130.00', progress: 95, ack_required: true, ack_digest: 'promo-policy' }) });
  const f = paymentFixture(state({ free_delivery: shipping({ eligible: true, progress: 100, remaining: '0.00' }) }), undefined,
    async () => ({ ok: false, json: async () => ({ error: 'delivery_policy_ack', cart_state: evaluated }) }));
  await f.submit();
  assert.equal(f.calls.length, 1);
  assert.equal(f.benefitNodes['[data-delivery-policy-digest]'].value, 'promo-policy');
  assert.equal(f.benefitNodes['[data-delivery-policy-ack]'].required, true);
  assert.equal(f.benefitNodes['[data-delivery-policy-ack]'].checked, false);
  assert.match(f.benefitNodes['[data-delivery-benefit-title]'].textContent, /130 UAH/);
  assert.match(f.error.textContent, /Confirm the updated delivery terms/);
  assert.equal(f.totals.total.textContent, '2870.00 UAH');
  assert.equal(f.totals['payment-amount'].textContent, '200.00 UAH');
});

test('a changed delivery policy re-renders the displayed digest and requests positive consent', async () => {
  const changed = state({ free_delivery: shipping({ threshold: '3500.00', basis_total: '3100.00', remaining: '400.00', progress: 88, ack_required: true, ack_digest: 'new-threshold' }) });
  const f = paymentFixture(state({ free_delivery: shipping({ eligible: true, progress: 100, remaining: '0.00' }) }), undefined,
    async () => ({ ok: false, json: async () => ({ error: 'delivery_policy_changed', cart_state: changed }) }));
  await f.submit();
  assert.equal(f.benefitNodes['[data-delivery-policy-digest]'].value, 'new-threshold');
  assert.equal(f.benefitNodes['[data-delivery-policy-digest]'].disabled, false);
  assert.equal(f.benefitNodes['[data-delivery-policy-ack]'].required, true);
  assert.equal(f.benefitNodes['[data-delivery-policy-ack]'].checked, false);
  assert.match(f.benefitNodes['[data-delivery-benefit-title]'].textContent, /400 UAH/);
  assert.match(f.error.textContent, /Confirm the updated delivery terms/);
});

test('approved child redirects freeze the old checkout instead of paying its old form', async () => {
  let redirect;
  const f = paymentFixture(state(), undefined, async () => ({ ok: false, json: async () => ({ error: 'revision_changed', redirect: '/ig/checkout/approved-child/' }) }));
  f.context.window.location.assign = (url) => { redirect = url; };
  await f.submit();
  await f.submit();
  assert.equal(redirect, '/ig/checkout/approved-child/');
  assert.equal(f.calls.length, 1);
  assert.equal(f.pay.disabled, true);
});

test('readonly paid, invoice and share pages show the actual recipient payer without inviting additions', async () => {
  for (const phase of ['paid', 'invoice', 'share']) {
    const f = fixture(state({ can_edit: false, can_pay: phase !== 'paid', free_delivery: shipping({ basis_total: '2850.00', remaining: '150.00', progress: 95, mode: 'carrier_recipient' }) }));
    assert.equal(f.benefitNodes['[data-delivery-benefit-title]'].textContent, 'You pay Nova Poshta for delivery separately', phase);
    assert.equal(f.benefitNodes['[data-delivery-benefit-meter]'].hidden, true, phase);
    assert.equal(f.benefitNodes['[data-delivery-benefit-update]'].textContent, '', phase);
    assert.match(f.benefitNodes['[data-delivery-benefit-detail]'].textContent, /2,850 UAH/, phase);
    assert.doesNotMatch(f.benefitNodes['[data-delivery-benefit-detail]'].textContent, /free from|3,000/, phase);
    assert.equal(f.rows[0].actions.every((button) => button.disabled), true, phase);
    f.rows[0].actions[1].disabled = false;
    f.click(0, 'increase');
    await flush();
    assert.equal(f.calls.length, 0, phase);
  }
});

test('readonly merchant-funded delivery remains free without an actionable progress meter', () => {
  const f = fixture(state({ can_edit: false, can_pay: false, free_delivery: shipping({ eligible: true, basis_total: '3100.00', remaining: '0.00', progress: 100, mode: 'merchant_free' }) }));
  assert.equal(f.benefitNodes['[data-delivery-benefit-title]'].textContent, 'Your delivery is free');
  assert.equal(f.benefitNodes['[data-delivery-benefit-meter]'].hidden, true);
  assert.equal(f.benefit.classList.values.has('is-achieved'), true);
});

test('starting invoice dispatch removes the add-items invitation before bank work starts', async () => {
  let release;
  const f = paymentFixture(state({ free_delivery: shipping() }), () => new Promise((resolve) => { release = resolve; }));
  assert.match(f.benefitNodes['[data-delivery-benefit-title]'].textContent, /add/);
  const pending = f.submit();
  assert.equal(f.benefitNodes['[data-delivery-benefit-title]'].textContent, 'You pay Nova Poshta for delivery separately');
  assert.equal(f.benefitNodes['[data-delivery-benefit-meter]'].hidden, true);
  assert.equal(f.rows[0].actions.every((button) => button.disabled), true);
  release({ ok: true });
  await pending;
  assert.equal(f.calls.length, 1);
  assert.equal(f.benefitNodes['[data-delivery-benefit-meter]'].hidden, true);
});
