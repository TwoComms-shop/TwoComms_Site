const test = require('node:test');
const assert = require('node:assert/strict');
const { capture, restore, merge } = require('../../twocomms/management/static/management/bot_chat_reading.js');

function conversation(ids) {
  const messages = {
    isConnected: true, scrollTop: 150, clientHeight: 200, scrollHeight: 800,
    top: 100, rows: [],
    getBoundingClientRect() { return { top: this.top }; },
    querySelectorAll() { return this.rows; },
    contains(row) { return this.rows.includes(row); },
    get lastElementChild() { return this.rows.at(-1); },
  };
  messages.rows = ids.map((id, index) => ({
    dataset: { mid: String(id) },
    getBoundingClientRect() {
      const top = messages.top + index * 100 - messages.scrollTop;
      return { top, bottom: top + 100 };
    },
  }));
  return messages;
}

test('retains an older reading anchor when refreshed controls change chat position', () => {
  const messages = conversation([1, 2, 3, 4, 5, 6, 7, 8]);
  const state = capture(messages);
  assert.equal(state.atBottom, false);
  assert.equal(state.anchor.dataset.mid, '2');
  // Reattaching a scroll container can reset its scrollTop; controls can move it.
  messages.scrollTop = 0;
  messages.top = 180;
  restore(messages, state);
  assert.equal(messages.scrollTop, 150);
  assert.equal(state.anchor.getBoundingClientRect().top - messages.top, state.offset);
});

test('refresh/poll overlap keeps older nodes and inserts each new message once', () => {
  const messages = conversation([1, 2, 3]);
  const original = messages.rows[0];
  const append = (root, item) => root.rows.push({ dataset: { mid: String(item.id) } });
  merge(messages, [{ id: 2 }, { id: 3 }, { id: 4 }, { id: 4 }], append);
  merge(messages, [{ id: 4 }, { id: 5 }], append);
  assert.equal(messages.rows[0], original);
  assert.deepEqual(messages.rows.map(row => Number(row.dataset.mid)), [1, 2, 3, 4, 5]);
});

test('captures following the tail separately from reading old messages', () => {
  const messages = conversation([1, 2, 3]);
  messages.scrollTop = 580;
  assert.equal(capture(messages).atBottom, true);
  messages.scrollTop = 300;
  assert.equal(capture(messages).atBottom, false);
  assert.equal(capture(null), null);
});

test('refreshes changed media presentation without replacing the reading anchor', () => {
  const messages = conversation([1]);
  const row = messages.rows[0];
  const originalPayload = { id: 1, text: '(зображення)', media: [{ capture_state: 'fetching' }] };
  row.dataset.messageSignature = JSON.stringify(originalPayload);
  let updates = 0;
  const refresh = (existing, item) => {
    updates += 1;
    assert.equal(existing, row);
    existing.dataset.messageSignature = JSON.stringify(item);
  };
  const changed = { ...originalPayload, media: [{ capture_state: 'owned', preview_url: '/bot/private-media/1/preview/' }] };
  merge(messages, [originalPayload], () => assert.fail('must not append'), refresh);
  assert.equal(updates, 0);
  merge(messages, [changed], () => assert.fail('must not append'), refresh);
  merge(messages, [changed], () => assert.fail('must not append'), refresh);
  assert.equal(updates, 1);
  assert.equal(messages.rows[0], row);
});

test('does not change disconnected conversations after a client switch', () => {
  const messages = conversation([1, 2, 3]);
  const state = capture(messages);
  messages.scrollTop = 0;
  messages.isConnected = false;
  restore(messages, state);
  assert.equal(messages.scrollTop, 0);
});
