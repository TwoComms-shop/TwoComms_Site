const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');

const source = fs.readFileSync(path.join(__dirname,
  '../../twocomms/management/static/management/js/ig_fulfillment_review.js'), 'utf8');
const REQUEST_A = '11111111-1111-4111-8111-111111111111';
const REQUEST_B = '22222222-2222-4222-8222-222222222222';
const SYNC_ID = '33333333-3333-4333-8333-333333333333';

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

function response(status, data) { return { status, ok: status >= 200 && status < 300, json: async () => data }; }
function reviewed(requestId = REQUEST_A, overrides = {}) {
  const digest = 'request-digest-' + requestId;
  const poolId = requestId === REQUEST_A ? 'warehouse:17' : 'warehouse:93';
  return {
    version: 1, status: 'reviewed', request_digest: digest,
    current_request: { id: requestId, scope: 'cart', customer_requirements: [], sources: [],
      lines: [{ line_id: 'line-1', label: 'Футболка · M · чорна', target: { size: 'M', quantity: 2 }, observation: { status: 'unavailable' } }] },
    decision: { id: 'decision-' + requestId, request_digest: digest, version: 1,
      lines: [{ line_id: 'line-1', availability: 'available', checked_quantity: 2, supply_basis: 'accessible_unrecorded' }] },
    stock_sync: { eligible: true, request_id: requestId, decision_id: 'decision-' + requestId,
      decision_digest: 'decision-digest-' + requestId, preview_digest: 'preview-1',
      pools: [{ pool_id: poolId, source: 'warehouse', before_total: 0, protected_total: 1,
        required: 2, minimum_total: 3, labels: ['Футболка · M · чорна'], size_counts: {} }] },
    ...overrides
  };
}
function receipt(review = reviewed()) {
  return { ...review, stock_sync: { eligible: false, reason: 'already_synced', request_id: REQUEST_A,
    sync_id: SYNC_ID, decision_id: review.decision.id, proof_digest: 'verified-receipt-digest', pools: [],
    decision_digest: review.stock_sync.decision_digest,
    checkout: { status: 'stock_ready', proposal_id: null, url: '', reservation_ids: [] } } };
}
function fixture({ canOperate = true } = {}) {
  const document = { createElement(tag) { return new Node(tag, this); } };
  const root = new Node('div', document), calls = [], replies = [], unexpected = [], uuids = [];
  const fetch = async (url, options = {}) => {
    const call = { url: String(url), method: options.method || 'GET', options }; calls.push(call);
    if (!replies.length) { unexpected.push(call); throw new Error('unexpected fetch'); }
    const next = replies.shift(); if (next instanceof Error) throw next;
    return typeof next === 'function' ? next(call) : await next;
  };
  const window = { location: { origin: 'https://management.example' }, crypto: {
    randomUUID() { const id = crypto.randomUUID(); uuids.push(id); return id; }
  } };
  vm.runInNewContext(source, { window, document, fetch, URL, AbortController, Date, Intl, Uint8Array });
  const controller = window.IgFulfillmentReview.create(root, { canOperate, csrfToken: 'synthetic-csrf',
    url: id => '/bot/api/clients/' + id + '/fulfillment-review/' });
  const sync = () => root.querySelector('.ig-fr-sync');
  const counts = () => sync()?.querySelectorAll('input[type="number"]') || [];
  const action = () => sync()?.querySelector('button[type="submit"]') || sync()?.querySelector('button');
  const refresh = () => root.querySelector('.ig-fr-head').querySelector('button');
  const posts = () => calls.filter(call => call.method === 'POST');
  const flush = async () => {
    // Public render/click methods return void; let their actual fetch/json
    // promises and subsequent renders finish before inspecting the DOM.
    await new Promise(setImmediate); await new Promise(setImmediate);
    assert.equal(unexpected.length, 0, 'all controller I/O must be accounted for');
  };
  const load = async value => { replies.push(response(200, { success: true, review: value })); controller.render(12); await flush(); };
  return { root, controller, calls, replies, uuids, sync, counts, action, refresh, posts, flush, load };
}

test('UNKNOWN refresh pins A even if GET returns newer B; retry preserves exact A operation and counts', async () => {
  const f = fixture(); await f.load(reviewed());
  f.counts()[0].input('8'); f.replies.push(new Error('network timeout')); f.action().click(); await f.flush();
  const original = f.posts()[0].options.body, payload = JSON.parse(original);
  assert.equal(payload.request_id, REQUEST_A); assert.deepEqual(payload.counts_by_pool, { 'warehouse:17': { total: 8 } });
  assert.equal(f.controller.canLeave(99), false); assert.equal(f.counts()[0].disabled, true);
  assert.throws(() => f.counts()[0].input('99'), /disabled count/);

  f.replies.push(response(200, { success: true, review: reviewed(REQUEST_B) })); f.refresh().click(); await f.flush();
  assert.equal(new URL(f.calls.filter(call => call.method === 'GET').at(-1).url).searchParams.get('request_id'), REQUEST_A);
  assert.equal(f.controller.canLeave(99), false); assert.equal(f.counts()[0].disabled, true);
  // Even a programmatic value change on B's disabled control cannot alter A's
  // already captured economic operation.
  f.counts()[0].value = '99'; f.replies.push(new Error('still no response')); f.action().click(); await f.flush();
  assert.equal(f.posts().length, 2); assert.equal(f.posts()[1].options.body, original);
  assert.equal(JSON.parse(f.posts()[1].options.body).operation_id, payload.operation_id);
  assert.equal(f.uuids.length, 1); assert.equal(f.controller.canLeave(99), false);

  f.replies.push(response(200, { success: true, review: receipt() })); f.refresh().click(); await f.flush();
  assert.equal(new URL(f.calls.filter(call => call.method === 'GET').at(-1).url).searchParams.get('request_id'), REQUEST_A);
  assert.equal(f.controller.canLeave(99), true); assert.equal(f.counts().length, 0); assert.equal(f.action(), null);
  f.replies.push(response(200, { success: true, review: receipt() })); f.controller.render(12); await f.flush();
  assert.equal(f.posts().length, 2, 'verified readback must not issue another stock POST');
});

test('verified stock_ready readback settles an UNKNOWN operation without inventing a proposal or reposting', async () => {
  const f = fixture(); await f.load(reviewed());
  f.counts()[0].input('6'); f.replies.push(new Error('response lost')); f.action().click(); await f.flush();
  assert.equal(f.controller.canLeave(99), false);
  f.replies.push(response(200, { success: true, review: receipt() })); f.refresh().click(); await f.flush();
  assert.equal(f.controller.canLeave(99), true); assert.equal(f.posts().length, 1);
  assert.equal(f.sync().querySelector('form'), null); assert.equal(f.sync().querySelector('button'), null);
  assert.equal(f.sync().querySelector('a'), null); assert.equal(f.uuids.length, 1);
});

test('a verified receipt for newer B cannot settle the still UNKNOWN operation bound to A', async () => {
  const f = fixture(); await f.load(reviewed());
  f.counts()[0].input('6'); f.replies.push(new Error('response lost')); f.action().click(); await f.flush();
  const original = f.posts()[0].options.body;
  const newer = receipt(reviewed(REQUEST_B)); newer.stock_sync.request_id = REQUEST_B;
  f.replies.push(response(200, { success: true, review: newer })); f.refresh().click(); await f.flush();
  assert.equal(new URL(f.calls.filter(call => call.method === 'GET').at(-1).url).searchParams.get('request_id'), REQUEST_A);
  assert.equal(f.controller.canLeave(99), false, 'receipt B does not prove the outcome of operation A');
  assert.ok(f.action(), 'the original operation must remain recoverable');
  f.replies.push(response(200, { success: true, ready: true, sync_id: SYNC_ID, status: 'stock_ready' }));
  f.action().click(); await f.flush();
  assert.equal(f.posts()[1].options.body, original); assert.equal(f.uuids.length, 1);
});

test('a receipt for A with a different decision digest cannot settle or replace the pending operation', async () => {
  const f = fixture(); await f.load(reviewed());
  f.counts()[0].input('6'); f.replies.push(new Error('response lost')); f.action().click(); await f.flush();
  const original = f.posts()[0].options.body;
  const mismatched = receipt(); mismatched.stock_sync.decision_digest = 'different-decision-digest';
  f.replies.push(response(200, { success: true, review: mismatched })); f.refresh().click(); await f.flush();
  assert.equal(f.controller.canLeave(99), false, 'a receipt must match the captured decision authority');
  assert.ok(f.action());
  f.replies.push(response(200, { success: true, ready: true, sync_id: SYNC_ID, status: 'stock_ready' }));
  f.action().click(); await f.flush();
  assert.equal(f.posts()[1].options.body, original); assert.equal(f.uuids.length, 1);
});

test('stale or unavailable readback retains the same UNKNOWN operation recovery action', async t => {
  for (const status of ['stale', 'unavailable']) await t.test(status, async () => {
    const f = fixture(); await f.load(reviewed());
    f.counts()[0].input('7'); f.replies.push(new Error('timeout')); f.action().click(); await f.flush();
    const original = f.posts()[0].options.body;
    f.replies.push(response(200, { success: true, review: { version: 1, status, current_request: null, decision: null } }));
    f.refresh().click(); await f.flush();
    assert.equal(new URL(f.calls.filter(call => call.method === 'GET').at(-1).url).searchParams.get('request_id'), REQUEST_A);
    assert.ok(f.action(), 'recovery action must survive unavailable business projection');
    f.replies.push(response(200, { success: true, ready: true, sync_id: SYNC_ID, status: 'stock_ready' }));
    f.action().click(); await f.flush();
    assert.equal(f.posts()[1].options.body, original); assert.equal(f.uuids.length, 1);
    assert.equal(f.controller.canLeave(99), true);
  });
});

test('409 refresh keeps physical counts but requires fresh confirmation and the new preview digest', async () => {
  const f = fixture(); await f.load(reviewed());
  f.counts()[0].input('8');
  const fresh = reviewed(); fresh.stock_sync.preview_digest = 'preview-2'; fresh.stock_sync.pools[0].minimum_total = 7;
  f.replies.push(response(409, { success: false, reason: 'preview_changed' }), response(200, { success: true, review: fresh }));
  f.action().click(); await f.flush();
  const original = JSON.parse(f.posts()[0].options.body);
  assert.equal(f.counts()[0].value, '8'); assert.equal(f.counts()[0].disabled, false);
  const checkbox = f.sync().querySelector('input[type="checkbox"]'); assert.ok(checkbox);
  assert.equal(checkbox.checked, false); assert.equal(f.action().disabled, true);
  f.action().click(); f.sync().querySelector('form').emit('submit'); await f.flush();
  assert.equal(f.posts().length, 1, 'old physical count alone cannot confirm a changed preview');
  checkbox.click(); assert.equal(f.action().disabled, false);
  f.replies.push(response(200, { success: true, ready: true, sync_id: SYNC_ID, status: 'stock_ready' }));
  f.action().click(); await f.flush();
  const updated = JSON.parse(f.posts()[1].options.body);
  assert.equal(updated.request_id, REQUEST_A); assert.equal(updated.expected_preview_digest, 'preview-2');
  assert.deepEqual(updated.counts_by_pool, original.counts_by_pool); assert.notEqual(updated.operation_id, original.operation_id);
  assert.equal(f.controller.canLeave(99), true);
});

test('catalogue shared pool and exact size counts remain separate explicit quantities in the real POST', async () => {
  const f = fixture(), review = reviewed();
  review.stock_sync.pools = [{ pool_id: 'catalog_variant:19', source: 'catalog_variant', before_total: 0,
    protected_total: 2, required: 3, minimum_total: 5, labels: ['Футболка · M', 'Футболка · L'],
    size_counts: { '31': { rule_id: 31, size: 'M', fit: 'regular', before_total: 0, protected: 1, minimum_total: 3 },
      '32': { rule_id: 32, size: 'L', fit: 'regular', before_total: 0, protected: 1, minimum_total: 2 } } }];
  await f.load(review); assert.equal(f.counts().length, 3);
  f.counts().forEach((input, index) => input.input(['10', '4', '5'][index]));
  f.replies.push(response(200, { success: true, ready: true, sync_id: SYNC_ID, status: 'stock_ready' }));
  f.action().click(); await f.flush();
  assert.deepEqual(JSON.parse(f.posts()[0].options.body).counts_by_pool,
    { 'catalog_variant:19': { total: 10, size_counts: { '31': 4, '32': 5 } } });
});

test('PII viewer sees verified stock facts without editable stock controls or any POST', async () => {
  const f = fixture({ canOperate: false }); await f.load(reviewed());
  assert.ok(f.sync()); assert.equal(f.sync().querySelectorAll('input,select,textarea,button').length, 0);
  assert.equal(f.posts().length, 0); assert.equal(f.uuids.length, 0); assert.equal(f.controller.canLeave(99), true);
});

test('warehouse-checked discrepancy uses explicit eligibility and preserves normal stock-sync authority', async () => {
  const f=fixture(),review=reviewed();review.decision.lines[0].supply_basis='warehouse_checked';
  await f.load(review);assert.ok(f.sync(),'typed eligibility opens recount regardless of supply basis');
  f.counts()[0].input('8');f.replies.push(response(200,{success:true,sync_id:SYNC_ID,status:'stock_ready'}));
  f.action().click();await f.flush();
  const payload=JSON.parse(f.posts()[0].options.body);
  assert.equal(payload.request_id,REQUEST_A);
  assert.equal(payload.expected_decision_digest,review.stock_sync.decision_digest);
  assert.deepEqual(payload.counts_by_pool,{'warehouse:17':{total:8}});
  assert.equal(f.uuids.length,1);
});

test('off-ledger supply without server eligibility cannot expose an actionable recount', async () => {
  for(const sync of [undefined,{eligible:false,reason:'inventory_mapping_unknown'},{eligible:'true'}]){
    const f=fixture();await f.load(reviewed(REQUEST_A,{stock_sync:sync}));
    assert.equal(f.sync(),null);assert.equal(f.posts().length,0);assert.equal(f.uuids.length,0);
  }
});

test('fulfillment display uses structured title and leaves historical label and quantity evidence unchanged', async () => {
  const f=fixture(),review=reviewed();
  const line=review.current_request.lines[0];line.label='Модель × 2 · M · × 2';line.display_label='Модель × 2 · M';
  await f.load(review);
  const card=f.root.querySelector('.ig-fr-line');assert.equal(card.querySelector('legend').textContent,'Модель × 2 · M');
  assert.match(card.textContent,/Кількість: 2/);assert.equal(line.label,'Модель × 2 · M · × 2');
  delete line.display_label;await f.load(review);
  assert.equal(f.root.querySelector('.ig-fr-line').querySelector('legend').textContent,line.label);
  assert.doesNotMatch(f.root.querySelector('.ig-fr-line').textContent,/Кількість: 2/);
  assert.equal(f.posts().length,0);
});

test('selecting WAIT exposes confirmed incoming basis without turning it into available quantity or stock authority', async () => {
  const f=fixture(),review=reviewed(REQUEST_A,{status:'pending',decision:null,stock_sync:null});
  await f.load(review);
  const card=f.root.querySelector('.ig-fr-line'),selectors=card.querySelectorAll('select');
  const availability=selectors[0],basis=selectors[1];
  availability.value='wait';availability.emit('change');
  assert.equal(basis.children.find(node=>node.value==='incoming_confirmed').textContent,'Підтверджене надходження');
  const quantity=card.querySelector('input[type="number"]');assert.equal(quantity.parentNode.hidden,true);
  assert.equal(f.sync(),null);assert.equal(f.posts().length,0);
  availability.value='available';availability.emit('change');
  assert.equal(basis.children.some(node=>node.value==='incoming_confirmed'),false,'incoming basis cannot survive transition to available');
  assert.equal(quantity.parentNode.hidden,false);
});
