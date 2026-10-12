const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');

const source = fs.readFileSync(path.join(__dirname,
  '../../twocomms/management/static/management/js/ig_fulfillment_review.js'), 'utf8');
const REQUEST_A = '11111111-1111-4111-8111-111111111111';

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


const DECIDED_AT = '2026-10-12T09:35:00+00:00';
const REQUESTED_AT = '2026-10-12T08:10:00+00:00';
function kyiv(value) {
  return new Date(value).toLocaleString('uk-UA', { timeZone: 'Europe/Kyiv',
    day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' }) + ' (Київ)';
}
function interest(status = 'available', overrides = {}) {
  return { status: 'accepted', label: 'Футболка · точна позиція', target: { size: 'M', quantity: 1 },
    accepted_at: '2026-10-12T10:00:00+00:00', notification_state: 'pending', source_refs: [],
    manager_review_reuse: { origin_scope: 'cart', request_id: REQUEST_A,
      decision_id: status === 'pending' ? null : 'actual-decision',
      checked_at: status === 'pending' ? REQUESTED_AT : DECIDED_AT, status,
      provenance: 'availability_event', event_id: 'sealed-availability-event' }, ...overrides };
}
function review(restockInterest, { pending = false } = {}) {
  return { version: 1, status: pending ? 'pending' : 'reviewed', request_digest: 'actual-request-digest',
    current_request: { id: REQUEST_A, scope: 'cart', customer_requirements: [], sources: [],
      lines: [{ line_id: 'exact-line', label: 'Футболка · M', target: { size: 'M', quantity: 1 } }] },
    decision: pending ? null : { id: 'actual-decision', request_digest: 'actual-request-digest',
      decided_at: DECIDED_AT, lines: [{ line_id: 'exact-line', availability: 'available',
        checked_quantity: 1, supply_basis: 'warehouse_checked' }] }, restock_interest: restockInterest };
}
function fixture({ canOperate = true } = {}) {
  const document = { createElement(tag) { return new Node(tag, this); } };
  const root = new Node('div', document), calls = [], replies = [];
  const fetch = async (url, options = {}) => {
    calls.push({ url: String(url), method: options.method || 'GET' });
    assert.ok(replies.length, 'each real controller request must have a fixture response');
    return { status: 200, ok: true, json: async () => ({ success: true, review: replies.shift() }) };
  };
  const window = { location: { origin: 'https://management.example' }, crypto };
  vm.runInNewContext(source, { window, document, fetch, URL, AbortController, Date, Intl, Uint8Array });
  const controller = window.IgFulfillmentReview.create(root, { canOperate, csrfToken: 'synthetic-only',
    url: id => '/bot/api/clients/' + id + '/fulfillment-review/' });
  async function load(value) {
    replies.push(value); controller.render(12);
    await new Promise(setImmediate); await new Promise(setImmediate);
    assert.equal(replies.length, 0, 'controller consumed the actual GET fixture');
  }
  return { root, controller, calls, load, aside: () => root.querySelector('.ig-fr-interest'),
    reuse: () => root.querySelector('.ig-fr-interest-review') };
}

test('a verified available reuse renders original check date, cart scope, and no duplicate action', async () => {
  const f = fixture(); await f.load(review(interest()));
  const section = f.reuse(); assert.ok(section);
  assert.match(section.textContent, /Наявність підтверджено менеджером/);
  assert.ok(section.textContent.includes(kyiv(DECIDED_AT)), 'uses original decision clock in Kyiv');
  assert.match(section.textContent, /Перевірка поточного кошика/);
  assert.match(section.textContent, /Повторний запит менеджеру не створювався/);
  assert.equal(section.querySelectorAll('button,input,select,textarea,a').length, 0);
  assert.equal(f.aside().querySelectorAll('button,input,select,textarea,a').length, 0);
  assert.ok(f.root.querySelector('.ig-fr-timeline').textContent.includes(kyiv(DECIDED_AT)));
  assert.equal(f.calls.length, 1); assert.equal(f.calls[0].method, 'GET');
  assert.equal(f.controller.canLeave(99), true);
});

test('pending original checks do not claim recorded reuse or checked availability, and viewers have no form controls', async () => {
  const f = fixture({ canOperate: false }); await f.load(review(interest('pending'), { pending: true }));
  assert.equal(f.reuse(), null);
  assert.match(f.root.querySelector('.ig-fr-timeline').textContent, /Ще потрібна/);
  assert.doesNotMatch(f.aside().textContent, /Перевірено:|Наявність підтверджено менеджером|Повторний запит/);
  assert.equal(f.root.querySelectorAll('input,select,textarea').length, 0);
  assert.equal(f.root.querySelector('button[type="submit"]'), null);
  assert.equal(f.calls.filter(call => call.method === 'POST').length, 0);
});

test('wait and unavailable cannot turn an original decision into a sealed available-reuse claim', async () => {
  for (const status of ['wait', 'unavailable']) {
    const current = review(interest(status)); current.decision.lines[0].availability = status;
    const f = fixture(); await f.load(current);
    assert.equal(f.reuse(), null);
    assert.ok(f.root.querySelector('.ig-fr-timeline').textContent.includes(kyiv(DECIDED_AT)));
    assert.doesNotMatch(f.aside().textContent, /Наявність підтверджено менеджером|Повторний запит/);
    assert.equal(f.aside().querySelectorAll('button,input,select,textarea,a').length, 0);
  }
});

test('inactive, delivered, uncertain, and malformed descriptors cannot display reusable manager authority', async () => {
  const cases = ['revoked', 'expired', 'unverified'].map(status => interest('available', { status }));
  cases.push(interest('available', { notification_state: 'sent' }), interest('available', { notification_state: 'unknown' }));
  for (const change of [{ origin_scope: 'restock' }, { checked_at: 'invalid' }, { request_id: '' },
    { decision_id: null }, { status: 'clarify' }, { provenance: '' }, { event_id: '' }]) {
    const item = interest(); Object.assign(item.manager_review_reuse, change); cases.push(item);
  }
  for (const item of cases) {
    const f = fixture(); await f.load(review(item)); assert.equal(f.reuse(), null);
    assert.equal(f.calls.filter(call => call.method === 'POST').length, 0);
  }
});

test('an original CART decision without a recorded availability event shows its date but no reuse claim', async () => {
  const item = interest(); delete item.manager_review_reuse;
  const f = fixture(); await f.load(review(item));
  assert.equal(f.reuse(), null);
  assert.doesNotMatch(f.aside().textContent, /Повторний запит менеджеру|Наявність підтверджено менеджером/);
  assert.ok(f.root.querySelector('.ig-fr-timeline').textContent.includes(kyiv(DECIDED_AT)));
  assert.ok(f.calls.every(call => call.method === 'GET'));
});

test('fresh GET removal of reuse clears the readonly subsection without a POST or scope conversion', async () => {
  const f = fixture(); await f.load(review(interest())); assert.ok(f.reuse());
  const expired = interest('available', { status: 'expired' }); delete expired.manager_review_reuse;
  await f.load(review(expired)); assert.equal(f.reuse(), null);
  assert.ok(f.aside()); assert.equal(f.controller.canLeave(99), true);
  assert.equal(f.calls.length, 2); assert.ok(f.calls.every(call => call.method === 'GET'));
  assert.ok(f.root.querySelector('.ig-fr-timeline').textContent.includes(kyiv(DECIDED_AT)),
    'ordinary CART decision date survives removal of restock reuse');
});

test('stale outer projections or different original identities hide reusable authority despite an old nested descriptor', async () => {
  const stale = review(interest()); stale.status = 'stale'; stale.decision = null;
  const wrongDecision = review(interest()); wrongDecision.decision.id = 'different-decision';
  const wrongRequest = review(interest()); wrongRequest.current_request.id = 'different-request';
  const conflictedPending = review(interest('pending'), { pending: true }); conflictedPending.reason = 'review_read_conflict';
  for (const value of [stale, wrongDecision, wrongRequest, conflictedPending]) {
    const f = fixture({ canOperate: false }); await f.load(value);
    assert.equal(f.reuse(), null); assert.equal(f.calls.filter(call => call.method === 'POST').length, 0);
  }
});
