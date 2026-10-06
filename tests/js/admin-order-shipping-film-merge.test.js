const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('twocomms/twocomms_django_theme/templates/partials/admin_orders_section.html', 'utf8');
function extract(name, next) {
  const pos = source.indexOf('  function ' + name + '(');
  const asyncPos = source.indexOf('  async function ' + name + '(');
  const start = pos < 0 ? asyncPos : pos;
  const end = source.indexOf('  function ' + next + '(', start + 1);
  assert.ok(start >= 0 && end > start);
  return source.slice(start, end).replace(/^  \/\/.*$/gm, '');
}
function setup(mode='customer_prepaid') {
  function control(value) { const field = {}; return { value, closest: () => field, field }; }
  const preset = control('partial_manual');
  const controls = {
    '#oeditName': control('Клієнт'), '#oeditPhone': control('0500000000'),
    '#oeditSource': control(''), '#oeditComment': control(''),
    '#oeditPresets input:checked': preset, '#oeditPresets input[name="oeditPreset"]:checked': preset,
    'input[name="oeditShippingMode"]:checked': control(mode), '#oeditShippingCharge': control('70,50'),
    '#oeditPaidAmount': control('100,50'), '#oeditPayer': control('Recipient'),
    '#oeditPaymentMethod': control('NonCash'), '#oeditCod': { checked: true },
    '#oeditShippingControls': {}, '#oeditShippingChargeWrap': {}, '#oeditShippingPolicyNote': {},
    '#oeditPaymentSummary': {}, '#oeditHandover': control('Клієнту в офісі'),
    '#oeditShippingBreakdown': {},
  };
  const c = {
    Number, String, Math, JSON, Promise, controls,
    body: { querySelector: selector => controls[selector], querySelectorAll: () => [] },
    document: { getElementById: id => controls['#' + id] },
    items: [{ kind:'dtf_film', film_length_m:'1,25', unit_price:320, qty:1, title:'DTF' }],
    retainedDiscount:50, originalGoodsPaid:0, paymentControlsLocked:false,
    deliveryMode:'keep', originalDeliveryMethod:'nova_poshta', initialTotal:420.5,
    elTotal:{}, elDelta:{}, elNum:{}, btnSave:{ disabled:false }, submitUrl:'/edit', orderLoadSeq:1,
    fmt: value => String(Math.round(value * 100) / 100), csrf: () => '',
    showToast() {}, showDeliveryError() {}, openCollapse() {},
    fetch(url, options) { c.submitted = JSON.parse(options.body); return new Promise(() => {}); },
  };
  vm.createContext(c);
  vm.runInContext(extract('lineTotal','selectedVariant') + extract('syncPaymentControls','showDeliveryError') + extract('updateTotal','openPicker') + extract('save','openCollapse'), c);
  return c;
}
test('included carriage keeps goods-only partial balance and retained discount', async () => {
  const c=setup(); c.updateTotal();
  assert.equal(c.elTotal.textContent,'420.5');
  assert.equal(c.controls['#oeditPayer'].value,'Sender');
  assert.equal(c.controls['#oeditPayer'].disabled,true);
  assert.match(c.controls['#oeditPaymentSummary'].textContent,/Залишок за товари:|Післяплата за товари:/);
  assert.match(c.controls['#oeditPaymentSummary'].textContent,/249.5 грн/);
  assert.match(c.controls['#oeditPaymentSummary'].textContent,/Отримано за доставку: 70.5 грн/);
  await c.save();
  assert.equal(c.submitted.delivery_payment_mode,'customer_prepaid');
  assert.equal(c.submitted.delivery_paid_confirmed,true);
  assert.equal(c.submitted.delivery_charge_amount,'70.50');
  assert.equal(c.submitted.delivery_payer_type,'Sender');
  assert.equal(c.submitted.delivery_payment_method,'NonCash');
  assert.equal(c.submitted.paid_amount,'100,50');
});
test('canonical modes determine payer despite contradictory disabled legacy control', async () => {
  for (const [mode,payer] of [['merchant_free','Sender'],['carrier_recipient','Recipient']]) {
    const c=setup(mode); c.controls['#oeditPayer'].value=payer==='Sender'?'Recipient':'Sender';
    await c.save();
    assert.equal(c.submitted.delivery_payer_type,payer);
    assert.equal(c.submitted.delivery_paid_confirmed,false);
    assert.equal(c.submitted.delivery_charge_amount,'0.00');
  }
});
test('handover hides carrier controls and neutralizes shipping charge and COD', async () => {
  const c=setup(); c.deliveryMode='handover'; c.updateTotal();
  assert.equal(c.elTotal.textContent,'350');
  assert.equal(c.controls['#oeditShippingControls'].hidden,true);
  assert.equal(c.controls['#oeditCod'].checked,false);
  assert.equal(c.controls['#oeditPaymentMethod'].field.hidden,true);
  await c.save();
  assert.equal(c.submitted.delivery_payment_mode,'carrier_recipient');
  assert.equal(c.submitted.delivery_charge_amount,'0.00');
  assert.equal(c.submitted.delivery_payer_type,'Recipient');
  assert.equal(c.submitted.cod_enabled,false);
});
test('reviewed goods payment survives preview refresh and is read-only', () => {
  const c=setup(); c.paymentControlsLocked=true; c.originalGoodsPaid=123.45;
  c.controls['#oeditPresets input:checked'].value='provider_prepayment';
  c.updateTotal();
  assert.equal(c.controls['#oeditPaidAmount'].value,123.45);
  assert.equal(c.controls['#oeditPaidAmount'].disabled,true);
  assert.equal(c.controls['#oeditCod'].disabled,true);
});
test('fixed 200 received total allocates included carriage before goods', async () => {
  const c=setup();
  c.controls['#oeditShippingCharge'].value='120';
  c.controls['#oeditPresets input:checked'].value='prepaid_200';
  c.updateTotal();
  assert.equal(c.controls['#oeditPaidAmount'].value,80);
  assert.match(c.controls['#oeditPaymentSummary'].textContent,/270 грн/);
  await c.save();
  assert.equal(c.submitted.delivery_paid_confirmed,true);
  assert.equal('paid_amount' in c.submitted,false);
});
test('film line rounds before order summation', () => {
  const c=setup(); c.retainedDiscount=0;
  c.items=[{kind:'dtf_film',film_length_m:'1.01',unit_price:10.5,qty:1},{kind:'dtf_film',film_length_m:'1.01',unit_price:10.5,qty:1}];
  assert.equal(c.lineTotal(c.items[0]),10.61);
  c.updateTotal();
  assert.equal(c.elTotal.textContent,'91.72');
});
