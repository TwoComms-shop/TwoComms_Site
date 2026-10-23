const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Node {
  constructor(tag, ownerDocument) { this.tagName = tag; this.ownerDocument = ownerDocument; this.children = []; this.dataset = {}; this.attributes = {}; this.listeners = {}; this.value = ''; this.textContent = ''; }
  append(...children) { this.children.push(...children); if (this.tagName === 'select' && !this.value) this.value = children[0].value; }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  all() { return [this, ...this.children.flatMap(child => child.all())]; }
}
function mount(asset, api) {
  const dom = { createElement(tag) { return new Node(tag, this); } };
  const root = new Node('div', dom);
  const window = { fetch() { throw new Error('recipient rendering must remain passive'); } };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,
    '../../twocomms/management/static/management/', asset), 'utf8'), { window });
  return { root, controller: window[api].create(root, {}), texts: () => root.all().map(node => node.textContent).join(' ') };
}
function commerce(recipient) {
  return { schema: 'commerce-scope.v1', status: 'captured', view_mode: 'current_commerce_scope', client_id: 12,
    current: { status: 'captured', scope: { client_id: 12, order_id: null },
      lines: [{ line_id: 'gift', index: 0, recipient_id: recipient, fields: {} }] },
    history: { orders: [] }, selection: { order_id: null, mode: 'current' } };
}
function correction(recipient) {
  const scope = { client_id: 12, episode_id: null, order_id: null, line_id: 'gift', recipient_id: recipient };
  return { status: 'captured', view_mode: 'current_admin', selection_revision: 3,
    state: { status: 'captured', boundary: { ...scope, selection_revision: 3,
      view_mode: 'current_admin', historical: false, size_correction_context: {
        available: true, context_digest: 'a'.repeat(64), context: {
          schema: 'size-correction-context.v1', field: 'size', scope,
          selection_revision: 3, active_index: 0, value: 'M', source: { source_message_id: 34 }
        }
      } }, slots: {} } };
}
for (const [asset, api, dto] of [
  ['ig_commerce_scope.js', 'IgCommerceScope', commerce],
  ['ig_selection_corrections.js', 'IgSelectionCorrections', correction]
]) {
  test(`${asset} renders sibling labels and preserves the unknown-recipient fallback`, () => {
    const f = mount(asset, api);
    for (const [recipient, expected] of [['sister', 'Для сестри'], ['brother', 'Для брата'], ['unknown-role', 'Окремий отримувач']]) {
      assert.equal(f.controller.render(12, dto(recipient)), true);
      assert.match(f.texts(), new RegExp(expected));
      assert.doesNotMatch(f.texts(), /unknown-role|sister|brother/);
    }
  });
}
