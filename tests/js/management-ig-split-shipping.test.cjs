const test = require('node:test');
const assert = require('node:assert/strict');
const {Controller} = require('../../twocomms/twocomms_django_theme/static/js/management-ig-split-shipping.js');
// Only the DOM surface used by the real controller. Events bubble so input
// edits exercise the production form listeners, not a test-only state setter.
class Node {
  constructor(tag, document) {
    this.tagName = tag; this.ownerDocument = document; this.children = [];
    this.parentNode = null; this.dataset = {}; this.attributes = {}; this.listeners = {};
    this.className = ''; this._text = ''; this.value = ''; this.disabled = false;
    this.hidden = false; this.checked = false;
    this.classList = {
      add: name => this.classList.toggle(name, true),
      contains: name => this.className.split(/\s+/).includes(name),
      toggle: (name, force) => {
        const values = new Set(this.className.split(/\s+/).filter(Boolean));
        const add = force === undefined ? !values.has(name) : Boolean(force);
        if (add) values.add(name); else values.delete(name);
        this.className = [...values].join(' '); return add;
      }
    };
  }
  append(...nodes) {
    nodes.forEach(node => {
      if (typeof node === 'string') { const text = new Node('#text', this.ownerDocument); text.textContent = node; node = text; }
      node.remove(); node.parentNode = this; this.children.push(node);
    });
  }
  appendChild(node) { this.append(node); return node; }
  remove() {
    if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(node => node !== this);
    this.parentNode = null;
  }
  replaceChildren(...nodes) {
    this.children.forEach(node => { node.parentNode = null; }); this.children = []; this._text = ''; this.append(...nodes);
  }
  after(node) {
    const parent = this.parentNode; if (!parent) return;
    node.remove(); node.parentNode = parent; parent.children.splice(parent.children.indexOf(this) + 1, 0, node);
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler); }
  get textContent() { return this._text + this.children.map(node => node.textContent).join(''); }
  set textContent(value) { this.replaceChildren(); this._text = String(value); }
  get firstChild() {
    if (!this._text) return this.children[0];
    const node = this;
    return { get textContent() { return node._text; }, set textContent(value) { node._text = String(value); } };
  }
  all() { return [this, ...this.children.flatMap(node => node.all())]; }
  matches(selector) {
    if (selector.startsWith('.')) return this.classList.contains(selector.slice(1));
    const match = selector.match(/^([a-z]+)?(?:\[([\w-]+)(?:="([^"]*)")?\])?$/);
    if (!match || match[1] && this.tagName !== match[1]) return false;
    if (!match[2]) return Boolean(match[1]);
    const name = match[2];
    const value = name.startsWith('data-') ? this.dataset[name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())]
      : this.attributes[name] ?? this[name];
    return match[3] === undefined ? value !== undefined : String(value) === match[3];
  }
  querySelectorAll(selector) {
    const selectors = selector.split(',').map(value => value.trim());
    return this.all().slice(1).filter(node => selectors.some(value => node.matches(value)));
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  focus() { this.ownerDocument.activeElement = this; }
  emit(type) {
    const event = { target: this, preventDefault() {} };
    for (let node = this; node; node = node.parentNode) (node.listeners[type] || []).forEach(handler => handler(event));
  }
  input(value) {
    assert.equal(this.disabled, false, 'a user cannot edit a disabled count');
    this.value = String(value); this.emit('input');
  }
  click() {
    if (this.disabled || this.hidden) return;
    if (this.tagName === 'input' && this.type === 'checkbox') { this.checked = !this.checked; this.emit('change'); }
    this.emit('click');
    if (this.tagName === 'button' && this.type === 'submit') {
      let form = this.parentNode; while (form && form.tagName !== 'form') form = form.parentNode;
      form?.emit('submit');
    }
  }
}


function projection(extra = {}) {
  return { ready: true, decision_digest: 'decision-A', decided_at: '2026-10-12T09:00:00+00:00', locale: 'uk',
    lines: [{ line_id: 'shirt', label: 'Футболка M · чорна', quantity: 2, availability: 'available',
      preparation: { min_days: 0, max_days: 0, unit: 'calendar', anchor_at: '2026-10-12T09:00:00+00:00' } },
    { line_id: 'hoodie', label: 'Худі L · сіре', quantity: 1, availability: 'wait',
      dispatch: { min_days: 3, max_days: 7, unit: 'business', anchor_at: '2026-10-12T09:00:00+00:00' } }], ...extra };
}
function offer(extra = {}) {
  const options = { together: { parcels: [{ key: 'together', line_ids: ['shirt', 'hoodie'], payer: 'customer', customer_delivery_cap: '150.00', currency: 'UAH' }] },
    separate: { parcels: [{ key: 'parcel-1', line_ids: ['shirt'], payer: 'customer', customer_delivery_cap: '100.00', currency: 'UAH' },
      { key: 'parcel-2', line_ids: ['hoodie'], payer: 'merchant', customer_delivery_cap: '0.00', currency: 'UAH' }] } };
  return projection({ offer: { id: 'actual-offer', version: 1, expires_at: '2026-10-13T09:00:00+00:00', options },
    delivery: { status: 'pending' }, ...extra });
}
function fixture(canOperate = true) {
  const document = { createElement(tag) { return new Node(tag, this); } };
  const root = new Node('section', document), calls = [], replies = [], uuids = [];
  const fetch = async (url, options = {}) => {
    calls.push({ url, method: options.method || 'GET', body: options.body });
    assert.ok(replies.length, 'fixture supplies every actual controller fetch');
    const value = replies.shift(); if (value instanceof Error) throw value;
    return { status: value.status, ok: value.status >= 200 && value.status < 300, json: async () => value.data };
  };
  const controller = new Controller(root, { apiUrl: '/bot/api/clients/12/split-shipping/', canOperate, fetch,
    csrfToken: 'synthetic-only', operationId: () => { const id = '00000000-0000-4000-8000-' + String(uuids.length + 1).padStart(12, '0'); uuids.push(id); return id; } });
  const enqueue = (shipping, status = 200) => replies.push({ status, data: { success: status === 200, shipping } });
  const flush = async () => { await new Promise(setImmediate); await new Promise(setImmediate); };
  const load = async shipping => { enqueue(shipping); assert.equal(await controller.load(), true); };
  const button = text => root.all().find(node => node.tagName === 'button' && node.textContent === text);
  const change = (control, value) => { assert.equal(control.disabled, false); control.value = String(value); control.emit('change'); };
  return { root, controller, calls, replies, uuids, enqueue, flush, load, button, change,
    posts: () => calls.filter(call => call.method === 'POST') };
}
function fill(f) {
  f.button('Підготувати варіанти').click();
  assert.equal(f.root.querySelectorAll('.ig-ss-terms').length, 1);
  f.button('Створити окремі відправлення').click();
  const assignments = f.root.querySelector('.ig-ss-assignment').querySelectorAll('select');
  f.change(assignments[0], 'parcel-1'); f.change(assignments[1], 'parcel-2');
  const terms = f.root.querySelectorAll('.ig-ss-terms');
  for (const [index, term] of terms.entries()) {
    f.change(term.querySelector('select'), index === 2 ? 'merchant' : 'customer');
    if (index !== 2) term.querySelector('input').input(index === 0 ? '150,00' : '100.00');
  }
  assert.equal(f.button('Надіслати варіанти клієнту').disabled, false);
}

test('passive constructor and GET show actual full basket/timing without auto groups or arrival promises', async () => {
  const f = fixture(); assert.equal(f.calls.length, 0); await f.load(projection());
  assert.equal(f.calls[0].method, 'GET'); assert.match(f.root.textContent, /Футболка M.*Кількість: 2.*Худі L/);
  assert.match(f.root.textContent, /У день перевірки/); assert.match(f.root.textContent, /12\.10\.2026/);
  assert.match(f.root.textContent, /Дату прибуття визначає перевізник/);
  f.button('Підготувати варіанти').click();
  assert.deepEqual(f.controller.draft.groups, []); assert.deepEqual(f.controller.draft.assignments, {});
  assert.equal(f.button('Надіслати варіанти клієнту').disabled, true);
  assert.equal(f.posts().length, 0);
});

test('only explicit complete group assignment and fee caps issue an exact whole-basket offer', async () => {
  const f = fixture(); await f.load(projection()); fill(f);
  assert.equal(f.controller.canLeave(), false);
  const assignments = f.root.querySelector('.ig-ss-assignment').querySelectorAll('select');
  f.change(assignments[1], 'parcel-1'); assert.equal(f.button('Надіслати варіанти клієнту').disabled, true);
  f.change(assignments[1], 'parcel-2');
  const merchantCap = f.root.querySelectorAll('.ig-ss-terms')[2].querySelector('input');
  assert.equal(merchantCap.value, '0.00'); assert.equal(merchantCap.disabled, true);
  f.enqueue(offer()); f.button('Надіслати варіанти клієнту').click(); await f.flush();
  const payload = JSON.parse(f.posts()[0].body);
  assert.equal(payload.action, 'issue_offer'); assert.equal(payload.expected_decision_digest, 'decision-A');
  assert.deepEqual(payload.options.together.parcels.map(row => row.key), ['p1']);
  assert.deepEqual(payload.options.separate.parcels.map(row => row.key), ['p1', 'p2']);
  assert.deepEqual(payload.options.together.parcels[0].line_ids, ['shirt', 'hoodie']);
  assert.deepEqual(payload.options.separate.parcels.map(row => row.line_ids), [['shirt'], ['hoodie']]);
  assert.deepEqual(payload.options.separate.parcels.map(row => [row.payer, row.customer_delivery_cap, row.currency]),
    [['customer', '100.00', 'UAH'], ['merchant', '0.00', 'UAH']]);
  assert.equal(f.controller.canLeave(), true);
  assert.equal(f.root.querySelector('.ig-ss-customer-options').querySelectorAll('button,input').length, 0,
    'preview is not a manager proxy for native customer choice');
});

test('adding and removing groups keeps draft identities but serializes contiguous canonical parcel keys', async () => {
  const f=fixture(),data=projection();data.lines.push({line_id:'gift',label:'Футболка S на подарунок',quantity:1,availability:'available'});
  await f.load(data);f.button('Підготувати варіанти').click();f.button('Створити окремі відправлення').click();f.button('Додати відправлення').click();
  assert.deepEqual(f.controller.draft.groups,['parcel-1','parcel-2','parcel-3']);
  const added=f.root.querySelector('.ig-ss-assignment').querySelectorAll('select');
  added.forEach((control,index)=>f.change(control,'parcel-'+(index+1)));
  for(const term of f.root.querySelectorAll('.ig-ss-terms'))f.change(term.querySelector('select'),'merchant');
  assert.deepEqual(f.controller.draftOptions().separate.parcels.map(row=>row.key),['p1','p2','p3']);
  const remove=f.root.querySelectorAll('.ig-ss-terms').slice(1).map(term=>term.querySelector('button'));
  remove[1].click();assert.deepEqual(f.controller.draft.groups,['parcel-1','parcel-3']);
  const assignments=f.root.querySelector('.ig-ss-assignment').querySelectorAll('select');
  f.change(assignments[0],'parcel-1');f.change(assignments[1],'parcel-3');f.change(assignments[2],'parcel-3');
  for(const term of f.root.querySelectorAll('.ig-ss-terms'))f.change(term.querySelector('select'),'merchant');
  f.enqueue(offer());f.button('Надіслати варіанти клієнту').click();await f.flush();
  const payload=JSON.parse(f.posts()[0].body);
  assert.deepEqual(payload.options.together.parcels.map(row=>row.key),['p1']);
  assert.deepEqual(payload.options.separate.parcels.map(row=>row.key),['p1','p2']);
  assert.deepEqual(payload.options.separate.parcels.map(row=>row.line_ids),[['shirt'],['hoodie','gift']]);
  assert.ok(payload.options.separate.parcels.every(row=>/^p[1-9][0-9]?$/.test(row.key)));
});

test('UNKNOWN retains the exact original offer operation through newer GET and retry', async () => {
  const f = fixture(); await f.load(projection()); fill(f);
  f.replies.push(new Error('network timeout')); f.button('Надіслати варіанти клієнту').click(); await f.flush();
  const original = f.posts()[0].body; assert.equal(f.controller.canLeave(), false);
  assert.equal(f.root.querySelector('.ig-ss-assignment').querySelector('select').disabled, true);
  assert.equal(f.button('Оновити').disabled, false);
  await f.load(projection({ decision_digest: 'new-decision-B' }));
  assert.equal(f.controller.canLeave(), false); assert.equal(f.controller.destroy(), false);
  f.enqueue(offer()); f.button('Перевірити ту саму операцію').click(); await f.flush();
  assert.equal(f.posts()[1].body, original); assert.equal(f.uuids.length, 1); assert.equal(f.controller.canLeave(), true);
});

test('409 refresh preserves draft but changed digest requires explicit new terms before issuance', async () => {
  const f = fixture(); await f.load(projection()); fill(f);
  f.replies.push({ status: 409, data: { success: false, code: 'decision_changed' } });
  f.enqueue(projection({ decision_digest: 'decision-B' }));
  f.button('Надіслати варіанти клієнту').click(); await f.flush();
  assert.equal(f.controller.draft.decisionDigest, 'decision-A'); assert.equal(f.controller.draft.terms.together.cap, '150,00');
  assert.equal(f.controller.pending, null); assert.equal(f.button('Надіслати варіанти клієнту'), undefined);
  assert.ok(f.button('Відкинути чернетку й почати заново')); assert.equal(f.posts().length, 1);
});

test('read-only operators can see choice and actual plans without mutation controls or POST', async () => {
  const f = fixture(false); await f.load(offer({ choice: { id: 'choice-1', option: 'separate', confirmed_at: '2026-10-12T11:00:00+00:00' },
    can_bind_order: true, plans: [{ id: 'plan-1', order_id: 81, order_number: 'TWC-81', parcels: [{ id: 'parcel-1', payer: 'customer', customer_delivery_cap: '100.00', cod_amount: '600.00', lines: [{ label: 'Футболка M', quantity: 2 }] }] }] }));
  assert.match(f.root.textContent, /Клієнт обрав: Окремими відправленнями/);
  assert.equal(f.root.querySelectorAll('input,select').length, 0);
  assert.equal(f.root.querySelectorAll('button').length, 1);
  assert.equal(await f.controller.perform({ action: 'dispatch' }), false); assert.equal(f.posts().length, 0);
});

test('binding requires native choice identity and explicit ordinary-order readiness', async () => {
  const f = fixture();
  await f.load(offer({ choice: { option: 'separate', confirmed_at: '2026-10-12T11:00:00+00:00' }, can_bind_order: true }));
  assert.equal(f.root.querySelector('.ig-ss-binding'), null);
  await f.load(offer({ choice: { id: 'actual-choice', option: 'separate', confirmed_at: '2026-10-12T11:00:00+00:00' } }));
  assert.equal(f.root.querySelector('.ig-ss-binding').querySelector('input'), null);
  const accepted = offer({ choice: { id: 'actual-choice', option: 'separate', confirmed_at: '2026-10-12T11:00:00+00:00' },
    can_bind_order: true, bindable_orders: [{ choice_id: 'actual-choice', id: 81, number: 'TWC-81' }] });
  await f.load(accepted); const orders = f.root.querySelector('.ig-ss-binding').querySelector('select'); f.change(orders, '81');
  f.enqueue(accepted); f.button('Прив’язати погоджений план').click(); await f.flush();
  const payload = JSON.parse(f.posts()[0].body);
  assert.deepEqual([payload.action, payload.choice_id, payload.order_id], ['bind_order', 'actual-choice', 81]);
  assert.equal(f.button('Підготувати нові умови'), undefined, 'accepted choice is readonly without an explicit new-offer grant');
});

test('dispatch enforces the customer cap and posts the exact readonly server COD', async () => {
  const f = fixture(); const data = offer({ plans: [{ id: 'plan-1', order_id: 81, parcels: [
    { id: 'actual-parcel', payer: 'customer', customer_delivery_cap: '100.00', cod_amount: '635.50', lines: [] } ] }] });
  await f.load(data); const parcel = f.root.querySelector('.ig-ss-plan-parcel'), inputs = parcel.querySelectorAll('input');
  assert.equal(inputs.length, 2, 'tracking and actual fee are the only editable fields');
  inputs[0].input('20400000000001'); inputs[1].input('100.01');
  assert.equal(f.button('Зафіксувати фактичну ТТН').disabled, true); inputs[1].input('85,00');
  assert.equal(f.button('Зафіксувати фактичну ТТН').disabled, false);
  const saved = structuredClone(data); saved.plans[0].parcels[0].dispatch = { tracking_number: '20400000000001', customer_delivery_amount: '85.00' };
  f.enqueue(saved); f.button('Зафіксувати фактичну ТТН').click(); await f.flush();
  const payload = JSON.parse(f.posts()[0].body);
  assert.deepEqual([payload.parcel_id, payload.tracking_number, payload.customer_delivery_amount, payload.cod_amount],
    ['actual-parcel', '20400000000001', '85.00', '635.50']);
  assert.equal(f.root.querySelector('.ig-ss-plan-parcel').querySelectorAll('input').length, 0);
  assert.match(f.root.textContent, /ТТН зафіксовано/); assert.doesNotMatch(f.root.textContent, /Замовлення оплачено|Клієнт прочитав/);
});

test('missing money/COD, unavailable basket and unverified native receipt cannot enable false claims', async () => {
  const f = fixture(); const bad = projection(); bad.lines[1].availability = 'unavailable'; await f.load(bad); fillInvalid(f);
  assert.equal(f.button('Надіслати варіанти клієнту').disabled, true);
  await f.load(offer({ delivery: { status: 'sent' }, plans: [{ id: 'plan', order_id: 81, parcels: [{ id: 'parcel', payer: 'customer', customer_delivery_cap: '100.00', lines: [] }] }] }));
  assert.match(f.root.querySelector('.ig-ss-receipt').textContent, /Підтвердження надсилання відсутнє/);
  assert.equal(f.root.querySelector('.ig-ss-plan-parcel').querySelectorAll('input').length, 0);
  assert.equal(f.posts().length, 0);
});
test('unsaved dispatch values survive GET redraw and require explicit discard before leaving', async () => {
  const f = fixture(); const data = offer({ plans: [{ id: 'plan-1', order_id: 81, parcels: [
    { id: 'actual-parcel', payer: 'customer', customer_delivery_cap: '100.00', cod_amount: '635.50', lines: [] } ] }] });
  await f.load(data); let inputs=f.root.querySelector('.ig-ss-plan-parcel').querySelectorAll('input');
  inputs[0].input('20400000000001'); inputs[1].input('85,00'); assert.equal(f.controller.canLeave(), false);
  await f.load(data); inputs=f.root.querySelector('.ig-ss-plan-parcel').querySelectorAll('input');
  assert.deepEqual(inputs.map(input=>input.value), ['20400000000001','85,00']);
  f.button('Відкинути введені дані оформлення').click();
  assert.equal(f.controller.canLeave(), true); assert.equal(f.posts().length, 0);
});
function fillInvalid(f) {
  f.button('Підготувати варіанти').click(); f.button('Створити окремі відправлення').click();
}

function trackedParcel(tracking = null) {
  return { id: 'actual-parcel', payer: 'customer', customer_delivery_cap: '100.00', cod_amount: '635.50',
    lines: [{ label: 'Футболка M', quantity: 2 }],
    dispatch: { tracking_number: '20400000000001', customer_delivery_amount: '85.00', cod_amount: '635.50' }, tracking };
}
function carrier(code, status = 'Відправлення отримано') {
  return { code, status, provider_event_at: '2026-10-12T10:00:00+00:00',
    terminal_at: '2026-10-12T10:00:00+00:00', checked_at: '2026-10-12T10:05:00+00:00' };
}
test('each successful carrier code needs actual event/terminal/check clocks and reports only its parcel received', async () => {
  for (const code of ['9', '10', '11']) {
    const f=fixture(false), received=trackedParcel(carrier(code)), waiting={ id:'not-dispatched', payer:'merchant', customer_delivery_cap:'0.00', cod_amount:'0.00', lines:[] };
    await f.load(offer({ plans:[{ id:'plan', order_id:81, parcels:[received,waiting] }] }));
    const states=f.root.querySelectorAll('.ig-ss-tracking');
    assert.equal(states[0].classList.contains('is-received'), true);
    assert.match(states[0].querySelector('.ig-ss-tracking-title').textContent, /Отримання цієї посилки підтверджено перевізником/);
    assert.match(states[0].textContent, /Подія перевізника.*12\.10\.2026.*Час завершальної події.*Перевірено/);
    assert.equal(states[1].classList.contains('is-received'), false);
    assert.equal(states[1].querySelector('.ig-ss-tracking-title').textContent, 'ТТН ще не зафіксовано');
    assert.match(f.root.textContent, /Післяплата за відправленням: 635,50 UAH/);
    assert.doesNotMatch(f.root.textContent, /Усе замовлення отримано|Замовлення виконано/);
    assert.equal(f.posts().length,0);
  }
});

test('carrier return wording and return codes never become customer receipt even with terminal clocks', async () => {
  for (const code of ['102','9']) {
    const f=fixture(false);await f.load(offer({ plans:[{ id:'plan', order_id:81,
      parcels:[trackedParcel(carrier(code,'Отримано відправлення після повернення відправнику'))] }] }));
    const state=f.root.querySelector('.ig-ss-tracking');
    assert.equal(state.classList.contains('is-received'),false);
    assert.equal(state.querySelector('.ig-ss-tracking-title').textContent,'Отримання клієнтом ще не підтверджено');
    assert.match(state.textContent,/Статус перевізника: Отримано відправлення після повернення/);
    assert.match(state.textContent,new RegExp('Код статусу'+code));
  }
});

test('registered TTN waits for carrier evidence; missing or inconsistent clocks cannot confirm receipt', async () => {
  const incomplete=carrier('9');delete incomplete.checked_at;
  const inconsistent=carrier('9');inconsistent.provider_event_at='2026-10-12T09:50:00+00:00';
  for(const tracking of [null,incomplete,inconsistent]) {
    const f=fixture(false);await f.load(offer({ plans:[{ id:'plan', order_id:81,parcels:[trackedParcel(tracking)] }] }));
    const state=f.root.querySelector('.ig-ss-tracking');assert.equal(state.classList.contains('is-received'),false);
    assert.equal(state.querySelector('.ig-ss-tracking-title').textContent,tracking?'Отримання клієнтом ще не підтверджено':'Очікує даних перевізника');
  }
});

test('concrete POST failures explain changed dispatch/configuration/money and checkout evidence without raw codes', async () => {
  const cases=[['split_dispatch_terms_changed',/звірте максимум для клієнта та післяплату/],
    ['configuration_changed',/звірте позиції перед новим погодженням/],
    ['split_order_amount_changed',/звірка сум товарів, оплати та післяплати/],
    ['checkout_allocation_failed',/складського резерву/]];
  for(const [code,expected] of cases) {
    const f=fixture(), parcel=trackedParcel();parcel.dispatch=null;
    await f.load(offer({plans:[{id:'plan',order_id:81,parcels:[parcel]}]}));
    const fields=f.root.querySelector('.ig-ss-plan-parcel').querySelectorAll('input');
    fields[0].input('20400000000001');fields[1].input('85.00');
    f.replies.push({status:400,data:{success:false,code}});f.button('Зафіксувати фактичну ТТН').click();await f.flush();
    assert.match(f.root.querySelector('.ig-ss-feedback').textContent,expected);
    assert.ok(!f.root.querySelector('.ig-ss-feedback').textContent.includes(code));
    assert.equal(f.posts().length,1);assert.equal(f.controller.canLeave(),false,'entered execution evidence remains reviewable');
  }
});

test('oversized customer offer gets actionable format guidance and preserves the complete draft', async () => {
  const f=fixture();await f.load(projection());fill(f);
  f.replies.push({status:400,data:{success:false,code:'offer_body_too_large'}});
  f.button('Надіслати варіанти клієнту').click();await f.flush();
  assert.match(f.root.querySelector('.ig-ss-feedback').textContent,/Текст повного кошика перевищує ліміт повідомлення/);
  assert.match(f.root.querySelector('.ig-ss-feedback').textContent,/коротший формат пропозиції/);
  assert.deepEqual(f.controller.draft.assignments,{shirt:'parcel-1',hoodie:'parcel-2'});
  assert.equal(f.posts().length,1);assert.equal(f.controller.pending,null);
});

test('immutable offer names, quantities and locale survive stale or conflicting current basket data', async () => {
  const f=fixture(false),data=offer({ready:false,locale:'uk',lines:[]});
  data.offer.lines=[{line_id:'shirt',label:'Оригінальна футболка M',quantity:2},{line_id:'hoodie',label:'Оригінальне худі L',quantity:1}];data.offer.locale='en';
  await f.load(data);
  assert.equal(f.root.querySelector('.ig-ss-basket'),null);assert.doesNotMatch(f.root.textContent,/Увесь кошик|Позиція попереднього кошика/);
  assert.match(f.root.querySelector('.ig-ss-offer').textContent,/Оригінальна футболка M · 2 шт\./);
  assert.equal(f.root.querySelector('.ig-ss-customer-options').textContent,'TogetherSeparately');
  const conflicting=structuredClone(data);conflicting.lines=[{line_id:'shirt',label:'Інша поточна футболка',quantity:8,availability:'available'}];
  await f.load(conflicting);
  assert.match(f.root.querySelector('.ig-ss-offer').textContent,/Оригінальна футболка M · 2 шт\./);
  assert.doesNotMatch(f.root.querySelector('.ig-ss-offer').textContent,/Інша поточна футболка|8 шт\./);
  assert.equal(f.posts().length,0);
});

test('a verified plan for the exact customer choice suppresses duplicate order binding', async () => {
  const f=fixture(),data=offer({choice:{id:'actual-choice',option:'separate',confirmed_at:'2026-10-12T11:00:00+00:00'},
    can_bind_order:true,bindable_orders:[{choice_id:'actual-choice',id:81,number:'TWC-81'}],
    plans:[{id:'bound-plan',choice_id:'actual-choice',ready:true,order_id:81,parcels:[]}]});
  await f.load(data);assert.equal(f.root.querySelector('.ig-ss-binding'),null);assert.equal(f.posts().length,0);
  data.plans[0].ready=false;await f.load(data);
  assert.ok(f.root.querySelector('.ig-ss-binding').querySelector('select'),'an unverified historical plan cannot hide current binding');
});

test('a different historical plan cannot hide current binding and only exact-choice order candidates are submitted', async () => {
  const f=fixture(),data=offer({choice:{id:'actual-choice',option:'separate',confirmed_at:'2026-10-12T11:00:00+00:00'},
    can_bind_order:true,bindable_orders:[{choice_id:'older-choice',id:80,number:'FOREIGN-80'},{choice_id:'actual-choice',id:81,number:'TWC-81'}],
    plans:[{id:'historical-plan',choice_id:'older-choice',ready:true,order_id:80,parcels:[]}]});
  await f.load(data);const picker=f.root.querySelector('.ig-ss-binding').querySelector('select');
  assert.ok(picker);assert.deepEqual(picker.querySelectorAll('option').map(row=>row.value),['','81']);assert.doesNotMatch(picker.textContent,/FOREIGN-80/);
  f.change(picker,'81');f.enqueue(data);f.button('Прив’язати погоджений план').click();await f.flush();
  const payload=JSON.parse(f.posts()[0].body);assert.equal(payload.choice_id,'actual-choice');assert.equal(payload.order_id,81);
});

test('foreign-choice candidate readiness does not expose a manual order fallback for the current choice', async () => {
  const f=fixture();await f.load(offer({choice:{id:'actual-choice',option:'separate',confirmed_at:'2026-10-12T11:00:00+00:00'},
    can_bind_order:true,bindable_orders:[{choice_id:'older-choice',id:80,number:'FOREIGN-80'}]}));
  const section=f.root.querySelector('.ig-ss-binding');assert.ok(section);
  assert.equal(section.querySelectorAll('input,select,button').length,0);
  assert.match(section.textContent,/Для цього погодження немає готового повного замовлення/);assert.equal(f.posts().length,0);
});
