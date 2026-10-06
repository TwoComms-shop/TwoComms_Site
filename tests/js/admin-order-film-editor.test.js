const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('twocomms/twocomms_django_theme/templates/partials/admin_orders_section.html', 'utf8');

function extract(name, next) {
  const start = source.indexOf('  function ' + name + '(');
  const asyncStart = source.indexOf('  async function ' + name + '(');
  const pos = start < 0 ? asyncStart : start;
  assert.ok(pos >= 0);
  const end = source.indexOf('  function ' + next + '(', pos + 1);
  assert.ok(end > pos);
  return source.slice(pos, end).replace(/^  \/\/.*$/gm, '');
}

function context() {
  const controls = {
    '#oeditName': { value: 'Клієнт' }, '#oeditPhone': { value: '0500000000' },
    '#oeditSource': { value: '' }, '#oeditComment': { value: '' },
    '#oeditPresets input[name="oeditPreset"]:checked': { value: 'partial_manual' },
    '#oeditPayer': { value: 'Sender' }, '#oeditPaymentMethod': { value: 'NonCash' },
    '#oeditCod': { checked: true }, '#oeditPaidAmount': { value: '100,50' },
    '#oeditHandover': { value: 'Клієнту в офісі' },
    'input[name="oeditShippingMode"]:checked': { value: 'carrier_recipient' },
    '#oeditShippingCharge': { value: '0' },
  };
  const c = {
    Number, String, Math, Promise, JSON,
    controls, body: { querySelector: id => controls[id] }, document: { getElementById: id => controls['#' + id] },
    retainedDiscount: 0, originalGoodsPaid: 0, paymentControlsLocked: false, elNum: {}, elClient: {}, productMap: {}, uidSeq: 1, initialTotal: 0, items: [],
    buildBody() {}, renderItems() {}, normalizeCatalogItem() { throw new Error('Film must not use catalogue normalization'); },
    btnSave: { disabled: false }, submitUrl: '/edit', orderLoadSeq: 1, deliveryMode: 'keep', originalDeliveryMethod: 'nova_poshta',
    showToast() {}, showDeliveryError() {}, openCollapse() {}, csrf() { return ''; },
    fetch(url, options) { c.submitted = JSON.parse(options.body); return new Promise(() => {}); },
  };
  vm.createContext(c);
  vm.runInContext(extract('lineTotal', 'selectedVariant') + extract('hydrate', 'deliveryIconSvg') + extract('shippingPaymentMode', 'openPicker') + extract('save', 'openCollapse'), c);
  return c;
}

test('drawer hydrates film without catalogue conversion and totals comma metres', () => {
  const c = context();
  c.hydrate({ order_number: 'TEST', delivery_method: 'handover', items: [{ kind: 'dtf_film', film_length_m: '1,25', qty: 1, unit_price: 320, title: 'DTF-плівка', item_id: 9 }] });
  assert.equal(c.items[0].kind, 'dtf_film');
  assert.equal(c.items[0].film_length_m, '1,25');
  assert.equal(c.items[0].item_id, 9);
  assert.equal(c.initialTotal, 400);
  assert.equal(c.originalDeliveryMethod, 'handover');
});

test('film edit roundtrip sends metres and handover without COD', async () => {
  const c = context();
  c.items = [{ kind: 'dtf_film', item_id: 9, title: 'DTF-плівка', film_length_m: '1,25', unit_price: 320, qty: 1 }];
  c.deliveryMode = 'handover';
  await c.save();
  assert.equal(c.submitted.items[0].kind, 'dtf_film');
  assert.equal(c.submitted.items[0].film_length_m, '1,25');
  assert.equal(c.submitted.items[0].qty, 1);
  assert.equal(c.submitted.cod_enabled, false);
  assert.equal(c.submitted.paid_amount, '100,50');
  assert.equal(c.submitted.handover_details, 'Клієнту в офісі');
});

test('paid preset derives amount server-side and preserves clothing options', async () => {
  const c = context();
  c.controls['#oeditPresets input[name="oeditPreset"]:checked'].value = 'paid_full';
  c.items = [{ kind: 'custom', item_id: 8, title: 'Одяг', qty: 1, unit_price: 500, size: 'M', color_name: 'Чорний', option_values: { sleeve: 'long' }, option_labels: { sleeve: 'Довгий' } }];
  await c.save();
  assert.equal('paid_amount' in c.submitted, false);
  assert.equal(c.submitted.items[0].item_id, 8);
  assert.equal(c.submitted.items[0].option_values.sleeve, 'long');
});
