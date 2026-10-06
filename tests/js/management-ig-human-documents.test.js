const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const crypto = require('node:crypto');
const source = fs.readFileSync(require('node:path').join(__dirname,
  '../../twocomms/management/static/management/ig_human_documents.js'), 'utf8');

class Node {
  constructor(tag, ownerDocument) { this.tagName = tag; this.ownerDocument = ownerDocument; this.children = []; this.dataset = {}; this.attributes = {}; this.listeners = {}; this.value = ''; this.textContent = ''; }
  append(...children) { this.children.push(...children); if (this.tagName === 'select' && !this.value) this.value = children[0].value; }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  focus() { this.ownerDocument.activeElement = this; }
  click() { return this.disabled || this.hidden ? undefined : this.listeners.click?.(); }
  input(value) { assert.equal(!!this.readOnly, false); this.value = value; this.listeners.input?.(); }
  all() { return [this, ...this.children.flatMap(child => child.all())]; }
}
function fixture() {
  const dom = { createElement(tag) { return new Node(tag, this); } };
  const root = new Node('div', dom), calls = [], replies = [], refreshes = [];
  const window = { crypto: crypto.webcrypto, location: { origin: 'https://management.example' },
    fetch: async (url, options) => { calls.push({ url, options }); const next = replies.shift(); if (next instanceof Error) throw next; return await next; } };
  vm.runInNewContext(source, { window, URL, AbortController, Uint8Array });
  const opts = { clientId: 12, contextMessageId: 34, csrf: 'synthetic-csrf', onRefresh: data => refreshes.push(data) };
  const controller = window.IgHumanDocuments.mount(root, opts);
  const action = name => root.all().find(node => node.dataset.action === name);
  const textarea = () => root.all().find(node => node.tagName === 'textarea');
  const kind = () => root.all().find(node => node.tagName === 'select');
  const status = () => root.all().find(node => node.attributes.role === 'status');
  return { root, window, calls, replies, refreshes, controller, opts, action, textarea, kind, status };
}
const doc = (overrides = {}) => ({ document_id: '10000000-0000-4000-8000-000000000001',
  kind: 'reply_draft', state: 'open', version: 1, text_hash: 'a'.repeat(64), context_message_id: 34,
  text: 'Збережений текст', ...overrides });
const response = (status, data) => ({ status, ok: status >= 200 && status < 300, json: async () => data });
const replyDoc = (value = doc()) => response(200, { success: true, document: value });
async function save(f, value = doc()) {
  f.textarea().input(value.text);
  f.replies.push({ status: 200, ok: true, json: async () => ({ success: true,
    document: { ...value, document_id: JSON.parse(f.calls.at(-1).options.body).document_id } }) });
  await f.action('save').click();
}

test('mount has zero I/O; notes save privately and never expose send', async () => {
  const f = fixture(); assert.equal(f.calls.length, 0);
  f.kind().value = 'internal_note'; f.kind().listeners.change();
  assert.equal(f.action('submit').hidden, true);
  await save(f, doc({ kind: 'internal_note', text: '<img src=x onerror=alert(1)>' }));
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0].url, '/bot/api/clients/12/human-documents/create/');
  const body = JSON.parse(f.calls[0].options.body);
  assert.equal(body.kind, 'internal_note'); assert.equal(body.context_message_id, '34');
  assert.equal(f.textarea().value, '<img src=x onerror=alert(1)>');
  assert.equal(f.root.all().some(node => node.tagName === 'img'), false);
  assert.equal(f.action('submit').hidden, true);
  assert.equal(f.calls[0].options.headers['X-CSRFToken'], 'synthetic-csrf');
  assert.equal(f.calls[0].options.credentials, 'same-origin');
});

test('draft save and CAS update require explicit actions; submit never contains browser text', async () => {
  const f = fixture(); await save(f);
  f.textarea().input('Змінений локальний текст');
  assert.equal(f.calls.length, 1); assert.equal(f.action('submit').disabled, true);
  const savedId = JSON.parse(f.calls[0].options.body).document_id;
  f.replies.push(replyDoc(doc({ document_id: savedId, text: 'Змінений локальний текст', version: 2, text_hash: 'b'.repeat(64) })));
  await f.action('save').click();
  assert.deepEqual(JSON.parse(f.calls[1].options.body), { expected_version: 1, expected_hash: 'a'.repeat(64), text: 'Змінений локальний текст' });
  f.replies.push({ status: 200, ok: true, json: async () => ({ success: true, accepted: true,
    command_id: 51, operation_id: JSON.parse(f.calls.at(-1).options.body).operation_id, state: 'sent' }) });
  await f.action('submit').click();
  const body = JSON.parse(f.calls[2].options.body);
  assert.deepEqual(Object.keys(body).sort(), ['expected_hash', 'expected_version', 'operation_id']);
  assert.equal(body.expected_version, 2); assert.equal(body.expected_hash, 'b'.repeat(64));
  assert.equal(f.action('submit').disabled, true); assert.match(f.status().textContent, /підтверджено/);
});

test('503 and lost response retain original operation/CAS and accepted identity on retries', async () => {
  const f = fixture(); await save(f);
  f.replies.push({ status: 503, ok: false, json: async () => ({ success: false, code: 'human_dispatch_unavailable',
    accepted: true, command_id: 56, operation_id: JSON.parse(f.calls.at(-1).options.body).operation_id }) });
  await f.action('submit').click();
  const first = f.calls[1].options.body;
  assert.equal(f.action('submit').disabled, false); assert.equal(f.controller.hasUnsavedChanges(), true);
  assert.equal(f.controller.update({ ...f.opts, clientId: 99 }), false);
  assert.equal(f.controller.destroy(), false);
  f.replies.push(new Error('connection lost')); await f.action('submit').click();
  assert.equal(f.calls[2].options.body, first);
  f.replies.push({ status: 200, ok: true, json: async () => ({ success: true, accepted: true, command_id: 56,
    operation_id: JSON.parse(first).operation_id, state: 'sent', idempotent: true }) });
  await f.action('submit').click();
  assert.equal(f.calls[3].options.body, first); assert.match(f.status().textContent, /підтверджено/);
  assert.equal(f.controller.hasUnsavedChanges(), false);
});

test('UNKNOWN and provider-started outcomes disable resend and never silently send again', async () => {
  for (const state of ['unknown', 'provider_started', 'claimed', 'definite_failed', 'cancelled']) {
    const f = fixture(); await save(f);
    f.replies.push({ status: 409, ok: false, json: async () => ({ success: false, accepted: true, command_id: 58,
      operation_id: JSON.parse(f.calls.at(-1).options.body).operation_id, state }) });
    await f.action('submit').click();
    assert.equal(f.action('submit').disabled, true); await f.action('submit').click();
    assert.equal(f.calls.length, 2); assert.equal(f.textarea().readOnly, true);
    if (state === 'unknown') assert.match(f.status().textContent, /невідомий.*повторне надсилання вимкнено/);
  }
});

test('list is explicit, bounded and escaped; clicking detail cannot replace unsaved text', async () => {
  const f = fixture();
  f.replies.push(response(200, { success: true, documents: [doc()], next_before_id: 9, bounded: true }));
  await f.action('load').click();
  assert.equal(f.calls[0].url, '/bot/api/clients/12/human-documents/?state=all&limit=20');
  assert.equal(f.action('more').hidden, false);
  f.textarea().input('Локальний текст');
  await f.action('detail').click(); assert.equal(f.calls.length, 1);
  assert.equal(f.textarea().value, 'Локальний текст');
  assert.equal(f.controller.update({ ...f.opts, clientId: 99 }), false);
  assert.equal(f.window.IgHumanDocuments.mount(f.root, { ...f.opts, clientId: 99 }), f.controller);
  assert.equal(f.textarea().value, 'Локальний текст');
});

test('editing during GET detail or server refresh never overwrites text', async () => {
  const f = fixture();
  f.replies.push(response(200, { success: true, documents: [doc()], bounded: true, next_before_id: null }));
  await f.action('load').click();
  let resolve; f.replies.push(new Promise(done => { resolve = done; }));
  const loading = f.action('detail').click();
  f.textarea().input('Набрано під час GET'); resolve(replyDoc()); await loading;
  assert.equal(f.textarea().value, 'Набрано під час GET'); assert.match(f.status().textContent, /залишено/);
  f.action('discard').click(); await save(f);
  f.replies.push(new Promise(done => { resolve = done; }));
  const checking = f.action('refresh').click(); f.textarea().input('Друга локальна зміна');
  resolve(replyDoc(doc({ document_id: JSON.parse(f.calls[2].options.body).document_id, version: 3, text: 'Серверна зміна' }))); await checking;
  assert.equal(f.textarea().value, 'Друга локальна зміна');
});

test('client switch aborts stale GET and never renders old client documents', async () => {
  const f = fixture(); let resolve;
  f.replies.push(new Promise(done => { resolve = done; }));
  const loading = f.action('load').click();
  assert.equal(f.controller.update({ ...f.opts, clientId: 99, contextMessageId: 100 }), true);
  assert.equal(f.calls[0].options.signal.aborted, true);
  resolve(response(200, { success: true, documents: [doc()], next_before_id: null })); await loading;
  assert.equal(f.action('detail'), undefined);
  f.replies.push(response(200, { success: true, documents: [], next_before_id: null })); await f.action('load').click();
  assert.match(f.calls[1].url, /clients\/99\/human-documents/);
});

test('ambiguous create save uses same UUID/body; explicit server check resolves without duplicate', async () => {
  const f = fixture(); f.textarea().input('  Збережений текст  ');
  f.replies.push(new Error('lost save')); await f.action('save').click();
  const first = f.calls[0].options.body;
  assert.equal(f.textarea().readOnly, true); assert.equal(f.controller.destroy(), false);
  f.replies.push(response(503, { success: false })); await f.action('save').click();
  assert.equal(f.calls[1].options.body, first);
  f.replies.push(replyDoc(doc({ document_id: JSON.parse(first).document_id }))); await f.action('refresh').click();
  assert.equal(f.textarea().value, 'Збережений текст'); assert.equal(f.controller.hasUnsavedChanges(), false);
  assert.equal(f.calls.length, 3); assert.equal(f.calls[2].options.method, 'GET');
});

test('CAS conflict preserves local text and cannot submit stale text; explicit discard then reload', async () => {
  const f = fixture(); await save(f); f.textarea().input('Локальна версія');
  const savedId = JSON.parse(f.calls[0].options.body).document_id;
  f.replies.push(response(409, { success: false, code: 'private_document_stale' })); await f.action('save').click();
  assert.equal(f.textarea().value, 'Локальна версія'); assert.equal(f.action('submit').disabled, true);
  f.replies.push(replyDoc(doc({ document_id: savedId, version: 2, text: 'Інша серверна версія' }))); await f.action('refresh').click();
  assert.equal(f.textarea().value, 'Локальна версія');
  f.action('discard').click();
  f.replies.push(replyDoc(doc({ document_id: savedId, version: 2, text: 'Інша серверна версія' }))); await f.action('refresh').click();
  assert.equal(f.textarea().value, 'Інша серверна версія');
});

test('finite access errors never echo response text, unsafe base never sends, text limit uses Unicode characters', async () => {
  const f = fixture(); f.replies.push(response(403, { code: '<script>private body</script>' }));
  await f.action('load').click(); assert.doesNotMatch(f.status().textContent, /private body|script/);
  f.textarea().input('😀'.repeat(4000)); assert.equal(f.action('save').disabled, false);
  f.textarea().input('😀'.repeat(4001)); assert.equal(f.action('save').disabled, true);
  const other = fixture(); other.controller.update({ ...other.opts, baseUrl: 'https://foreign.example/bot/api' });
  await other.action('load').click(); assert.equal(other.calls.length, 0);
});

test('unexpected document identity on success preserves pending body and cannot enable sending', async () => {
  const f = fixture(); f.textarea().input('Збережений текст');
  f.replies.push(replyDoc()); await f.action('save').click();
  assert.equal(f.action('submit').disabled, true); assert.equal(f.controller.hasUnsavedChanges(), true);
  assert.equal(f.textarea().value, 'Збережений текст'); assert.equal(f.textarea().readOnly, true);
  const first = f.calls[0].options.body;
  f.replies.push({ status: 200, ok: true, json: async () => ({ success: true,
    document: doc({ document_id: JSON.parse(first).document_id }) }) });
  await f.action('save').click();
  assert.equal(f.calls[1].options.body, first); assert.equal(f.action('submit').disabled, false);
});

test('explicit pagination bounds DOM at 100 documents and refresh restarts the list', async () => {
  const f = fixture();
  for (let page = 0; page < 5; page++) {
    f.replies.push(response(200, { success: true, documents: Array.from({ length: 20 }, () => doc()), next_before_id: 100 - page }));
    await f.action(page === 0 ? 'load' : 'more').click();
  }
  assert.equal(f.root.all().filter(node => node.dataset.action === 'detail').length, 100);
  assert.equal(f.action('more').disabled, true); await f.action('more').click(); assert.equal(f.calls.length, 5);
  f.replies.push(response(200, { success: true, documents: [doc()], next_before_id: null }));
  await f.action('load').click();
  assert.equal(f.root.all().filter(node => node.dataset.action === 'detail').length, 1);
});
