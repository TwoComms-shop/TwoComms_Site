const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('twocomms/twocomms_django_theme/templates/pages/admin_manual_order.html', 'utf8');
const start = source.indexOf('  function decimalValue(');
const end = source.indexOf('  function currentPreset(', start);
const context = vm.createContext({ items: [] });
vm.runInContext(source.slice(start, end), context);

test('film accepts decimal comma and dot but rejects malformed or overprecise input', () => {
  assert.equal(context.decimalValue('1,45'), 1.45);
  assert.equal(context.decimalValue('1.40'), 1.4);
  for (const input of ['', 'NaN', 'Infinity', '1,2,3', '1.234', '1e2', '-1']) {
    assert.ok(Number.isNaN(context.decimalValue(input)), input);
  }
});
test('film total is metres times rate rather than piece quantity', () => {
  assert.equal(context.lineTotal({ kind: 'dtf_film', film_length_m: '1,45', unit_price: 320, qty: 99 }), 464);
  assert.equal(context.lineTotal({ kind: 'catalog', qty: 2, unit_price: 320 }), 640);
});
test('each film line rounds to cents before aggregation, matching Decimal server totals', () => {
  context.items = [
    { kind: 'dtf_film', film_length_m: '0.1', unit_price: '0.05' },
    { kind: 'dtf_film', film_length_m: '0.1', unit_price: '0.05' },
  ];
  assert.equal(context.orderTotal(), 0.02);
});
