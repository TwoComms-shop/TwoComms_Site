const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const crypto = require('node:crypto');
const source = fs.readFileSync(require('node:path').join(__dirname,
  '../../twocomms/management/static/management/ig_selection_corrections.js'), 'utf8');

class Node {
  constructor(tag, ownerDocument) { this.tagName = tag; this.ownerDocument = ownerDocument; this.children = []; this.dataset = {}; this.attributes = {}; this.listeners = {}; this.value = ''; this.textContent = ''; }
  append(...children) { this.children.push(...children); if (this.tagName === 'select' && !this.value) this.value = children[0].value; }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  focus() { this.ownerDocument.activeElement = this; }
  click() { return this.disabled || this.hidden ? undefined : this.listeners.click?.(); }
  input(value) { assert.equal(!!this.readOnly, false); assert.equal(!!this.disabled, false); this.value = value; this.listeners.input?.(); }
  change(value) { assert.equal(!!this.disabled, false); this.value = value; this.listeners.change?.(); }
  all() { return [this, ...this.children.flatMap(child => child.all())]; }
}
function capture({ client = 12, revision = 3, digest = 'a', value = 'M', authority = 'customer_source' } = {}) {
  const scope = { client_id: client, episode_id: null, order_id: null, line_id: 'line-1', recipient_id: 'self' };
  return { status: 'captured', view_mode: 'current_admin', selection_revision: revision,
    state: { status: 'captured', boundary: { ...scope, selection_revision: revision,
      view_mode: 'current_admin', historical: false, size_correction_context: {
        available: true, context_digest: digest.repeat(64), context: {
          schema: 'size-correction-context.v1', field: 'size', scope,
          selection_revision: revision, active_index: 0, value, source: { source_message_id: 34, authority }
        }
      } }, slots: { 'choice.size': { authority, source_refs: [
        { kind: 'message', id: 34 }, { kind: 'commerce_transition', actor_id: 7 }
      ] } } } };
}
function fixture() {
  const dom = { createElement(tag) { return new Node(tag, this); } }, root = new Node('div', dom);
  const calls = [], replies = [], committed = [];
  const window = { crypto: crypto.webcrypto, fetch: async (url, options) => {
    calls.push({ url, options }); const next = replies.shift(); if (next instanceof Error) throw next; return await next;
  } };
  vm.runInNewContext(source, { window, Uint8Array });
  const controller = window.IgSelectionCorrections.create(root,
    { csrfToken: 'test-csrf', onCommitted: (clientId, result) => committed.push({ clientId, result }) });
  const action = value => root.all().find(node => node.dataset.action === value);
  const field = className => root.all().find(node => node.className === className);
  const status = () => root.all().find(node => node.attributes.role === 'status');
  return { root, window, controller, calls, replies, committed, action, field, status };
}
function response(status, data) { return { status, json: async () => data }; }
function success(f, status = 'applied') {
  return { status: 200, json: async () => ({ success: true, status, field: 'size',
    operation_id: JSON.parse(f.calls.at(-1).options.body).operation_id,
    selection_revision: 4, transition_id: status === 'noop' ? null : 51 }) };
}

test('create and supplied current render are passive; explicit normalized save binds CAS and safe body', async () => {
  const f = fixture(), supplied = capture();
  assert.equal(f.calls.length, 0); assert.equal(f.action('save').disabled, true);
  assert.equal(f.controller.render(12, supplied), true); assert.equal(f.calls.length, 0);
  assert.equal(f.field('ig-sc-editor').hidden, true); f.action('edit').click();
  f.field('ig-sc-size').input('  l ');
  supplied.state.boundary.size_correction_context.context_digest = 'b'.repeat(64);
  supplied.state.boundary.size_correction_context.context.selection_revision = 99;
  f.replies.push(success(f)); await f.action('save').click();
  assert.equal(f.calls.length, 1); assert.equal(f.calls[0].url, '/bot/api/clients/12/state/size/');
  const body = JSON.parse(f.calls[0].options.body);
  assert.equal(body.value, 'L'); assert.equal(body.expected_selection_revision, 3);
  assert.equal(body.expected_context_digest, 'a'.repeat(64));
  assert.match(body.operation_id, /^[a-f0-9-]{36}$/);
  assert.deepEqual(Object.keys(body).sort(), ['expected_context_digest', 'expected_selection_revision', 'field', 'operation', 'operation_id', 'reason_code', 'value']);
  assert.equal(f.calls[0].options.credentials, 'same-origin'); assert.equal(f.calls[0].options.headers['X-CSRFToken'], 'test-csrf');
  assert.equal(f.calls[0].options.method, 'POST'); assert.equal(f.committed.length, 1);
  assert.equal(f.committed[0].clientId, '12'); assert.equal(f.action('save').disabled, true);
});

test('historical, unavailable, bare state and cross-client proof deny editing without I/O', () => {
  for (const mutate of [dto => { dto.view_mode = 'historical'; }, dto => { dto.status = 'unavailable'; },
    dto => { dto.state.boundary.historical = true; }, dto => { dto.state.boundary.size_correction_context.available = false; },
    dto => { dto.state.boundary.size_correction_context.context.scope.client_id = 99; },
    dto => { dto.state.boundary.size_correction_context.context.selection_revision = 0; }]) {
    const f = fixture(), dto = capture(); mutate(dto);
    assert.equal(f.controller.render(12, dto), false); assert.equal(f.action('save').disabled, true);
    assert.equal(f.field('ig-sc-size').disabled, true); assert.equal(f.calls.length, 0);
  }
  const f = fixture(); assert.equal(f.controller.render(12, capture().state), false);
  assert.equal(f.controller.render(9223372036854775808n.toString(), capture()), false);
});

test('canonical set whitelist and explicit clear preserve source provenance and audit actor label', async () => {
  const f = fixture(); f.controller.render(12, capture({ authority: 'audited_correction', value: 'L' }));
  assert.match(f.field('ig-sc-provenance').textContent, /повідомлення №34/);
  assert.equal(f.field('ig-sc-authority').textContent, 'Менеджер №7');
  f.action('edit').click();
  for (const invalid of ['paid 790', '<img src=x onerror=1>', '', '999', 'XL'.repeat(100)]) {
    f.field('ig-sc-size').input(invalid); assert.equal(f.action('save').disabled, true);
  }
  f.field('ig-sc-size').input('one size'); assert.equal(f.action('save').disabled, false);
  f.field('ig-sc-operation').change('clear'); assert.equal(f.calls.length, 0);
  assert.equal(f.field('ig-sc-size').disabled, true);
  f.field('ig-sc-reason').change('clear_unverified_requirement');
  f.replies.push(success(f)); await f.action('save').click();
  const body = JSON.parse(f.calls[0].options.body);
  assert.equal(body.operation, 'clear'); assert.equal(body.value, null);
  assert.equal(body.reason_code, 'clear_unverified_requirement');
  assert.equal(f.root.all().some(node => node.tagName === 'img'), false);
});

test('503, lost response and malformed success retain same UUID/input and prevent switching or discard', async () => {
  const f = fixture(); f.controller.render(12, capture()); f.action('edit').click(); f.field('ig-sc-size').input('L');
  f.replies.push(response(503, { success: false, code: 'correction_write_unavailable' })); await f.action('save').click();
  const original = f.calls[0].options.body;
  assert.equal(f.controller.canLeave(99), false); assert.equal(f.controller.render(99, capture({ client: 99 })), false);
  assert.equal(f.controller.clear(), false); assert.equal(f.controller.destroy(), false);
  assert.equal(f.action('discard').disabled, true); assert.equal(f.field('ig-sc-size').readOnly, true);
  f.replies.push(new Error('lost')); await f.action('save').click();
  f.replies.push(response(200, { success: true, status: 'applied', operation_id: 'another-operation' })); await f.action('save').click();
  assert.equal(f.committed.length, 0);
  f.replies.push(success(f, 'replayed')); await f.action('save').click();
  assert.equal(f.calls.length, 4); assert.equal(f.calls.every(call => call.options.body === original), true);
  assert.equal(f.committed.length, 1); assert.equal(f.controller.canLeave(99), true);
});

test('in-flight request disables duplicate save and cannot be hidden by client switch', async () => {
  const f = fixture(); f.controller.render(12, capture()); f.action('edit').click(); f.field('ig-sc-size').input('L');
  let resolve; f.replies.push(new Promise(done => { resolve = done; }));
  const saving = f.action('save').click();
  assert.equal(f.action('save').disabled, true); await f.action('save').click();
  assert.equal(f.calls.length, 1); assert.equal(f.controller.canLeave(99), false);
  resolve(success(f)); await saving; assert.equal(f.committed.length, 1);
});

test('409 keeps edits and requires explicit supplied new-context acceptance before a new UUID', async () => {
  const f = fixture(); f.controller.render(12, capture()); f.action('edit').click(); f.field('ig-sc-size').input('XL');
  f.replies.push(response(409, { success: false, code: 'correction_context_conflict', retryable: false })); await f.action('save').click();
  assert.equal(f.field('ig-sc-size').value, 'XL'); assert.equal(f.action('save').disabled, true);
  assert.equal(f.controller.render(12, capture({ revision: 4, digest: 'b', value: 'L' })), false);
  assert.equal(f.field('ig-sc-size').value, 'XL'); assert.equal(f.action('accept-context').hidden, false);
  assert.equal(f.action('save').disabled, true); f.action('accept-context').click();
  assert.equal(f.field('ig-sc-size').value, 'XL'); assert.equal(f.action('save').disabled, false);
  f.replies.push(success(f)); await f.action('save').click();
  const first = JSON.parse(f.calls[0].options.body), second = JSON.parse(f.calls[1].options.body);
  assert.notEqual(first.operation_id, second.operation_id); assert.equal(second.expected_selection_revision, 4);
  assert.equal(second.expected_context_digest, 'b'.repeat(64)); assert.equal(second.value, 'XL');
});

test('dirty same-client refresh never silently rebinds and explicit discard permits switching', () => {
  const f = fixture(); f.controller.render(12, capture()); f.action('edit').click(); f.field('ig-sc-size').input('L');
  assert.equal(f.controller.render(12, capture({ revision: 4, digest: 'b', value: 'XL' })), false);
  assert.equal(f.field('ig-sc-size').value, 'L'); assert.equal(f.controller.canLeave(99), false);
  f.action('discard').click(); assert.equal(f.field('ig-sc-size').value, 'XL');
  assert.equal(f.controller.canLeave(99), true); assert.equal(f.controller.render(99, capture({ client: 99 })), true);
  assert.equal(f.calls.length, 0); assert.equal(f.controller.clear(), true); assert.equal(f.controller.destroy(), true);
});

test('ambiguous operation is disabled in historical view and resumes only same-operation retry on current view', async () => {
  const f = fixture(); f.controller.render(12, capture()); f.action('edit').click(); f.field('ig-sc-size').input('L');
  f.replies.push(new Error('lost')); await f.action('save').click();
  const history = capture(); history.view_mode = 'historical';
  assert.equal(f.controller.render(12, history), false); assert.equal(f.action('save').disabled, true);
  await f.action('save').click(); assert.equal(f.calls.length, 1);
  f.controller.render(12, capture({ revision: 4, digest: 'b', value: 'XL' }));
  f.replies.push(success(f, 'replayed')); await f.action('save').click();
  assert.equal(f.calls[0].options.body, f.calls[1].options.body);
});

test('noop is a proven result and unavailable fresh card never grants editing', async () => {
  const f = fixture(); f.controller.render(12, capture()); f.action('edit').click(); f.field('ig-sc-size').input('m');
  f.replies.push(success(f, 'noop')); await f.action('save').click();
  assert.equal(f.committed[0].result.status, 'noop'); assert.equal(f.controller.canLeave(99), true);
  const unavailable = capture(); unavailable.status = 'unavailable';
  f.controller.render(12, unavailable); assert.equal(f.field('ig-sc-size').disabled, true);
  assert.equal(f.action('save').disabled, true);
});

test('exact episode/order/line/recipient scope is visible before editing and mismatches deny editing', () => {
  const f = fixture(), dto = capture();
  const context = dto.state.boundary.size_correction_context.context;
  Object.assign(context.scope, { episode_id: 41, order_id: 72, line_id: 'gift-hoodie', recipient_id: 'gift-recipient' });
  Object.assign(dto.state.boundary, context.scope); context.active_index = 2;
  assert.equal(f.controller.render(12, dto), true);
  assert.match(f.field('ig-sc-scope').textContent, /Прив’язане замовлення.*Позиція 3.*Окремий отримувач/);
  assert.doesNotMatch(f.field('ig-sc-scope').textContent, /gift-hoodie|gift-recipient|№41|№72/);
  assert.equal(f.field('ig-sc-editor').hidden, true); assert.equal(f.calls.length, 0);
  dto.state.boundary.line_id = 'different-position';
  assert.equal(f.controller.render(12, dto), false); assert.equal(f.action('edit').disabled, true);
  const unknown = capture(); f.controller.render(12, unknown);
  assert.match(f.field('ig-sc-scope').textContent, /Замовлення ще не прив’язане.*Позиція 1.*Для себе/);
  assert.doesNotMatch(f.field('ig-sc-scope').textContent, /line-1/);
});
