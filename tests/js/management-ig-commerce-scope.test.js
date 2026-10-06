const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname,
  '../../twocomms/management/static/management/ig_commerce_scope.js'), 'utf8');
class Node {
  constructor(tag, ownerDocument) { this.tagName = tag; this.ownerDocument = ownerDocument; this.children = []; this.attributes = {}; this.listeners = {}; this.value = ''; this.textContent = ''; }
  append(...children) { this.children.push(...children); if (this.tagName === 'select' && !this.value) this.value = children[0].value; }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  all() { return [this, ...this.children.flatMap(child => child.all())]; }
}
function field(value, sourceId = 41) { return { value, status: 'confirmed', authority: 'customer_source', source_refs: [{ kind: 'message', id: sourceId }] }; }
function dto() {
  return { schema: 'commerce-scope.v1', status: 'captured', view_mode: 'current_commerce_scope', client_id: 12,
    current: { status: 'captured', scope: { client_id: 12, episode_id: 7, order_id: null }, active_line_id: 'hoodie',
      lines: [{ line_id: 'hoodie', index: 0, title: 'Худі', recipient_id: 'self', active: true, editable: true, fields: { size: field('L'), color: field('Рожевий', 44) },
        history: { status: 'captured', items: [{ field: 'color', value: 'Чорний', source: field('Чорний', 33) }] } },
      { line_id: 'shirt', index: 1, title: 'Футболка', recipient_id: 'self', active: false, fields: { size: field('M') } },
      { line_id: 'gift', index: 2, title: 'Худі на подарунок', recipient_id: 'gift-recipient', active: false, fields: { size: field('XL') } }] },
    history: { coverage: 'complete', completed_order_count: 1, orders: [{ id: 81, kind: 'physical_order', number: 'TWC-81',
      created_label: '01.10.2026', status_label: 'Отримано', payment: { label: 'Оплату підтверджено' },
      items: [{ id: 1, title: 'Попередня футболка', size: 'S', color: 'Чорний', fit: '', quantity: 1 }] }] },
    selection: { order_id: null, mode: 'current' } };
}
function fixture() {
  const dom = { createElement(tag) { return new Node(tag, this); } }, root = new Node('div', dom), selected = [];
  const window = { fetch() { throw new Error('controller must remain passive'); } };
  vm.runInNewContext(source, { window });
  const controller = window.IgCommerceScope.create(root, { onSelectOrder: (client, order) => selected.push({ client, order }) });
  const field = cls => root.all().find(node => node.className === cls);
  const texts = () => root.all().map(node => node.textContent).join(' ');
  return { root, controller, selected, field, texts };
}

test('passive current card keeps three positions, gift recipient and only one active correction target', () => {
  const f = fixture(); assert.equal(f.controller.render(12, dto()), true);
  assert.deepEqual(f.root.all().filter(node => node.className === 'ig-cs-product').map(node => node.textContent), ['Худі', 'Футболка', 'Худі на подарунок']);
  assert.equal(f.root.all().filter(node => node.className === 'ig-cs-active').length, 1);
  assert.match(f.texts(), /Окремий отримувач/); assert.doesNotMatch(f.texts(), /gift-recipient/); assert.match(f.texts(), /Без прив’язаного замовлення/);
  assert.equal(f.selected.length, 0); assert.doesNotMatch(f.texts(), /Покупка 1|Покупка 2/);
});

test('current pink is separate from collapsed source-qualified old black and unproven fields are omitted', () => {
  const f = fixture(), value = dto();
  value.current.lines[1].fields.color = { value: 'Fake availability', status: 'unknown', authority: 'none' };
  f.controller.render(12, value);
  const headings = f.root.all().filter(node => node.className === 'ig-cs-field');
  assert.match(headings[1].children[1].textContent, /Рожевий/);
  assert.equal(headings[1].children[0].children[0].textContent, '№44');
  assert.match(f.root.all().find(node => node.className === 'ig-cs-history-list').children[0].textContent, /Чорний.*№33/);
  assert.equal(f.root.all().find(node => node.className === 'ig-cs-history').open, undefined);
  assert.doesNotMatch(f.texts(), /Fake availability/);
});

test('genuine previous order has actual items/payment and never lends old payment to current choice', () => {
  const f = fixture(), data = dto(); f.controller.render(12, data);
  assert.doesNotMatch(f.texts(), /Оплату підтверджено/);
  data.selection = { order_id: 81, mode: 'order' }; f.controller.render(12, data);
  assert.match(f.texts(), /Замовлення TWC-81/); assert.match(f.texts(), /Попередня футболка/);
  assert.match(f.texts(), /Оплату підтверджено/); assert.doesNotMatch(f.texts(), /Худі на подарунок/);
  assert.match(f.texts(), /Поточні вимоги клієнта залишаються окремо/);
});

test('selector requests only a supplied real order or current choice', () => {
  const f = fixture(); f.controller.render(12, dto());
  const picker = f.field('ig-cs-picker'); picker.value = '81'; picker.listeners.change();
  picker.value = ''; picker.listeners.change(); picker.value = '999'; picker.listeners.change();
  assert.deepEqual(f.selected, [{ client: '12', order: '81' }, { client: '12', order: null }]);
});

test('bounded history shows explicit coverage and no invented global completed-count or LTV', () => {
  const f = fixture(), data = dto(); data.history.coverage = 'bounded'; data.history.completed_order_count = null;
  f.controller.render(12, data); assert.equal(f.field('ig-cs-count').textContent, '');
  assert.match(f.field('ig-cs-status').textContent, /останні 20.*старіша історія.*не охоплена/);
  assert.doesNotMatch(f.texts(), /LTV|Покупка 21|Покупка 1/);
});

test('historical envelope/client mismatch fail closed and markup remains text', () => {
  const f = fixture(), data = dto(); data.current.lines[0].title = '<img src=x onerror=alert(1)>';
  f.controller.render(12, data); assert.equal(f.root.all().some(node => node.tagName === 'img'), false);
  assert.match(f.texts(), /<img/); data.view_mode = 'historical'; assert.equal(f.controller.render(12, data), false);
  assert.equal(f.root.all().filter(node => node.className === 'ig-cs-product').length, 0);
  assert.equal(f.controller.render(99, dto()), false); assert.equal(f.controller.clear(), true); assert.equal(f.controller.destroy(), true);
});

test('copied source and cart default remain distinct from new pink customer assertion', () => {
  const f = fixture(), data = dto(), line = data.current.lines[0];
  line.fields.size.copied_from = { scope: { episode_id: 1 }, proof: { source_message_id: 17 } };
  line.defaults = { quantity: { value: 1, authority: 'existing_cart_default', source_confirmed: false } };
  line.history.coverage = { complete: false, event_limit: 8, reason: 'bounded_validated_history' };
  f.controller.render(12, data);
  const sources = f.root.all().filter(node => node.className === 'ig-cs-field-source').map(node => node.textContent);
  assert.deepEqual(sources.slice(0, 3), ['Повторено · №17', '№44', 'За замовчуванням']);
  assert.match(f.texts(), /Показано до 8 підтверджених змін/);
});

test('unavailable selected order cannot fall back to editable current positions', () => {
  const f = fixture(), data = dto(); data.selection.order_id = 999;
  f.controller.render(12, data);
  assert.equal(f.root.all().filter(node => node.className === 'ig-cs-product').length, 0);
  assert.equal(f.root.all().filter(node => node.className === 'ig-cs-active').length, 0);
  assert.match(f.texts(), /Обране замовлення недоступне/);
});

test('read-only active position gets a neutral scope label and no edit claim', () => {
  const f = fixture(), data = dto(); data.current.lines[0].editable = false;
  f.controller.render(12, data);
  assert.equal(f.field('ig-cs-active').textContent, 'Поточна позиція');
  assert.doesNotMatch(f.texts(), /Поточна для уточнення/);
});

function knownRevenue(overrides = {}) {
  return Object.assign({ status: 'confirmed', currency: 'UAH', currency_code: 980,
    refund_coverage: 'confirmed_projection_as_of_capture', value: '650.00', gross: '900.00', refunded: '250.00',
    order_ids: [81], proofs: [{ order_id: 81, event_id: 5, projection_id: 8 }] }, overrides);
}

test('canonical net revenue is a separate compact metric with refunds and current draft unchanged', () => {
  const f = fixture(), data = dto(); data.history.ltv = knownRevenue(); f.controller.render(12, data);
  assert.equal(f.field('ig-cs-revenue-value').textContent, '650,00 грн');
  assert.match(f.texts(), /Підтверджена виручка/); assert.match(f.texts(), /мінус повернення \(250,00 грн\)/);
  assert.equal(f.root.all().filter(node => node.className === 'ig-cs-product').length, 3);
  assert.doesNotMatch(f.texts(), /Оплату підтверджено/);
});

test('known full refund zero is distinct from unknown missing payment/refund coverage', () => {
  const f = fixture(), data = dto(); data.history.ltv = knownRevenue({ value: '0.00', refunded: '900.00' });
  f.controller.render(12, data); assert.equal(f.field('ig-cs-revenue-value').textContent, '0,00 грн');
  data.history.ltv = { status: 'unknown', value: null, reason: 'payment_projection_missing' };
  f.controller.render(12, data); assert.equal(f.field('ig-cs-revenue-value').textContent, 'Не визначено');
  assert.match(f.field('ig-cs-revenue-note').textContent, /дані про повернення/);
});

test('partial history and malformed/currency-conflicted net proof cannot display a lifetime total', () => {
  for (const overrides of [{ value: '650.00', refunded: '0.00' }, { currency: 'USD' }, { value: 'NaN' },
    { proofs: [] }, { order_ids: [999] }, { refund_coverage: 'unknown' }]) {
    const f = fixture(), data = dto(); data.history.ltv = knownRevenue(overrides); f.controller.render(12, data);
    assert.equal(f.field('ig-cs-revenue-value').textContent, 'Не визначено');
  }
  const f = fixture(), data = dto(); data.history.ltv = knownRevenue(); data.history.coverage = 'bounded';
  f.controller.render(12, data); assert.equal(f.field('ig-cs-revenue-value').textContent, 'Не визначено');
  assert.match(f.field('ig-cs-revenue-note').textContent, /охоплена частково/);
});

test('visible positions are plainly numbered without exposing technical line or episode identifiers', () => {
  const f = fixture(), data = dto();
  data.current.scope.episode_id = 787878;
  data.current.lines[0].line_id = 'line:opaque:current:992288';
  data.current.lines[2].recipient_id = 'recipient:private:opaque:token';
  f.controller.render(12, data);
  assert.deepEqual(f.root.all().filter(node => node.className === 'ig-cs-position-number').map(node => node.textContent),
    ['Позиція 1', 'Позиція 2', 'Позиція 3']);
  assert.doesNotMatch(f.texts(), /787878|line:opaque|992288|recipient:private|Цикл №/);
  assert.match(f.field('ig-cs-scope-label').textContent, /Чернетка вибору/);
  assert.equal(f.root.all().filter(node => node.className === 'ig-cs-line-id').length, 0);
  assert.equal(f.field('ig-cs-field-source').attributes.title, 'З повідомлення клієнта · №41');
});

test('a current bound order uses its real number while historical item numbering stays inside that order', () => {
  const f = fixture(), data = dto(); data.current.scope.order_id = 81;
  data.current.order_binding = { status: 'confirmed', order_id: 81 };
  f.controller.render(12, data);
  assert.equal(f.field('ig-cs-scope-label').textContent, 'Для замовлення TWC-81');
  data.selection = { mode: 'order', order_id: 81 }; f.controller.render(12, data);
  assert.deepEqual(f.root.all().filter(node => node.className === 'ig-cs-position-number').map(node => node.textContent), ['Позиція 1']);
  assert.match(f.texts(), /Історія конкретного замовлення/); assert.doesNotMatch(f.texts(), /Покупка 1|Цикл №/);
  data.selection = { mode: 'current', order_id: null }; data.current.scope.order_id = 888887;
  data.current.order_binding = { status: 'confirmed', order_id: 888887 }; f.controller.render(12, data);
  assert.equal(f.field('ig-cs-scope-label').textContent, 'Є прив’язане замовлення'); assert.doesNotMatch(f.texts(), /888887/);
  data.current.order_binding.status = 'unknown'; f.controller.render(12, data);
  assert.equal(f.field('ig-cs-scope-label').textContent, 'Прив’язку замовлення не підтверджено');
});

test('known color and fit codes use Ukrainian display labels without changing captured values or evidence', () => {
  const f = fixture(), data = dto(), line = data.current.lines[0];
  line.fields.color = field('pink', 44); line.fields.fit_option_code = field('classic', 45);
  line.history.items = [
    { field: 'color', value: 'pink', previous: 'black', previous_source: field('black', 33), source: field('pink', 44) },
    { field: 'fit_option_code', value: 'oversize', previous: 'classic', previous_source: field('classic', 31), source: field('oversize', 45) },
    { field: 'garment_type', value: 'hoodie', previous: 'tshirt', previous_source: field('tshirt', 30), source: field('hoodie', 46) }
  ];
  const before = JSON.stringify(data); f.controller.render(12, data);
  const values = f.root.all().filter(node => node.className === 'ig-cs-field').map(node => node.children[1].textContent);
  assert.equal(values[1], 'Рожевий'); assert.equal(values[2], 'Класична');
  assert.match(f.field('ig-cs-history-list').children[0].textContent, /Чорний → Рожевий.*№44/);
  assert.match(f.field('ig-cs-history-list').children[1].textContent, /Класична → Оверсайз/);
  assert.match(f.field('ig-cs-history-list').children[2].textContent, /Футболка → Худі/);
  assert.equal(JSON.stringify(data), before); assert.equal(line.fields.size.value, 'L');
  assert.doesNotMatch(f.texts(), /black|pink|classic|oversize|tshirt|hoodie/);
});

test('unknown catalog labels and numeric product history remain literal escaped text', () => {
  const f = fixture(), data = dto(), line = data.current.lines[0];
  line.fields.color = field('Dusty Rose <custom>', 44); line.fields.fit_option_code = field('Relaxed fit', 45);
  line.history.items = [{ field: 'product_id', value: 102, previous: 101, previous_source: field(101, 33), source: field(102, 44) }];
  f.controller.render(12, data);
  assert.match(f.texts(), /Dusty Rose <custom>/); assert.match(f.texts(), /Relaxed fit/);
  assert.match(f.field('ig-cs-history-list').children[0].textContent, /Модель: 101 → 102/);
  assert.equal(f.root.all().some(node => node.tagName === 'custom'), false);
});

test('actual order item known codes share the same display labels', () => {
  const f = fixture(), data = dto(); data.selection = { mode: 'order', order_id: 81 };
  data.history.orders[0].items[0].color = 'white'; data.history.orders[0].items[0].fit = 'oversize';
  f.controller.render(12, data);
  assert.match(f.field('ig-cs-order-item-detail').textContent, /Розмір S · Білий · Оверсайз/);
  assert.doesNotMatch(f.texts(), /white|oversize/);
});
