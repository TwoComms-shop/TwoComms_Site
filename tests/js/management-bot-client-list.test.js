const test = require('node:test');
const assert = require('node:assert/strict');
const { create, DURATION_MS } = require('../../twocomms/management/static/management/bot_client_list.js');

// Minimal DOM contract with layout derived from the actual child order. Moving
// a focused node through insertBefore models browsers which blur that subtree.
class Element {
  constructor(document, name = 'DIV', text) {
    this.ownerDocument = document; this.nodeName = name; this.nodeType = name === '#text' ? 3 : 1;
    this.nodeValue = text || ''; this.childNodes = []; this.parentNode = null; this.attrs = new Map();
    this.scrollTop = 0; this.clientHeight = 200; this.animations = []; this.visualOffset = 0;
    const tokens = () => (this.getAttribute('class') || '').split(/\s+/).filter(Boolean);
    this.classList = {
      get 0() { return tokens()[0]; },
      add: (...names) => this.setAttribute('class', [...new Set([...tokens(), ...names])].join(' ')),
      remove: (...names) => this.setAttribute('class', tokens().filter(name => !names.includes(name)).join(' ')),
      toggle: (name, force) => { const has = tokens().includes(name); const add = force === undefined ? !has : force; if (add) this.classList.add(name); else this.classList.remove(name); return add; },
      contains: name => tokens().includes(name),
    };
  }
  get attributes() { return [...this.attrs].map(([name, value]) => ({ name, value })); }
  get firstChild() { return this.childNodes[0] || null; }
  get nextSibling() { if (!this.parentNode) return null; return this.parentNode.childNodes[this.parentNode.childNodes.indexOf(this) + 1] || null; }
  get scrollHeight() { return this.childNodes.length * 80; }
  setAttribute(name, value) { this.attrs.set(name, String(value)); }
  getAttribute(name) { return this.attrs.has(name) ? this.attrs.get(name) : null; }
  hasAttribute(name) { return this.attrs.has(name); }
  removeAttribute(name) { this.attrs.delete(name); }
  contains(node) { return node === this || this.childNodes.some(child => child.contains(node)); }
  appendChild(node) { return this.insertBefore(node, null); }
  insertBefore(node, before) {
    if (node === before) return node;
    if (node.parentNode) node.parentNode.removeChild(node);
    const index = before ? this.childNodes.indexOf(before) : this.childNodes.length;
    assert.ok(index >= 0, 'reference child belongs to parent');
    this.childNodes.splice(index, 0, node); node.parentNode = this;
    this.ownerDocument.mutations += 1;
    return node;
  }
  removeChild(node) {
    const index = this.childNodes.indexOf(node); assert.ok(index >= 0);
    if (node.contains(this.ownerDocument.activeElement)) this.ownerDocument.activeElement = null;
    this.childNodes.splice(index, 1); node.parentNode = null; this.ownerDocument.mutations += 1;
    return node;
  }
  cloneNode() { const clone = new Element(this.ownerDocument, this.nodeName); this.attrs.forEach((value, key) => clone.setAttribute(key, value)); return clone; }
  focus(options) { this.ownerDocument.activeElement = this; this.focusOptions = options; }
  getBoundingClientRect() {
    const listTop = 100 - this.ownerDocument.defaultView.scrollY;
    if (!this.parentNode || this === this.ownerDocument.list) return { top: listTop, bottom: listTop + this.clientHeight, height: this.clientHeight };
    const top = listTop + this.parentNode.childNodes.indexOf(this) * 80 - this.parentNode.scrollTop + this.visualOffset;
    return { top, bottom: top + 72, height: 72 };
  }
  animate(frames, options) {
    const row = this;
    const animation = { frames, options, cancelled: false, onfinish: null, oncancel: null,
      cancel() { this.cancelled = true; row.visualOffset = 0; if (this.oncancel) this.oncancel(); },
    };
    this.animations.push(animation); return animation;
  }
}

function fixture(extraOptions = {}) {
  const visibility = new Set(); const motionHandlers = new Set();
  const selection = { rangeCount: 0, setBaseAndExtent(anchor, anchorOffset, focus, focusOffset) { Object.assign(this, { anchorNode: anchor, anchorOffset, focusNode: focus, focusOffset, rangeCount: 1 }); } };
  const document = { activeElement: null, hidden: false, mutations: 0,
    getSelection: () => selection,
    addEventListener: (_, handler) => visibility.add(handler), removeEventListener: (_, handler) => visibility.delete(handler),
    defaultView: { scrollY: 0, scrollX: 0, scrollTo(position) { this.scrollY = position.top; }, getComputedStyle: () => ({ overflowY: 'auto' }) },
  };
  const motion = { matches: false, addEventListener: (_, handler) => motionHandlers.add(handler), removeEventListener: (_, handler) => motionHandlers.delete(handler) };
  const list = document.list = new Element(document);
  let renders = 0; let focusRemoved = 0;
  const render = (row, item) => {
    renders += 1;
    row.setAttribute('data-client-id', item.id); row.setAttribute('class', 'bot-client-row');
    const name = new Element(document, 'SPAN'); name.setAttribute('class', 'name');
    name.appendChild(new Element(document, '#text', item.name || 'Client ' + item.id));
    while (row.firstChild) row.removeChild(row.firstChild);
    row.appendChild(name);
  };
  const controller = create(list, {
    createRow(item) { const row = new Element(document); render(row, item); return row; },
    updateRow: render, motionQuery: motion, getActivity: item => item.activity,
    isSelected: item => item.id === 3,
    onFocusRemoved: () => { focusRemoved += 1; }, ...extraOptions,
  });
  return { controller, list, document, motion, selection, visibility, motionHandlers,
    get renders() { return renders; }, get focusRemoved() { return focusRemoved; },
    items: ids => ids.map(id => ({ id, activity: id })),
    ids: () => list.childNodes.map(row => Number(row.getAttribute('data-client-id'))),
    hide() { document.hidden = true; visibility.forEach(handler => handler()); },
    reduceMotion() { motion.matches = true; motionHandlers.forEach(handler => handler()); },
  };
}

test('fifth client moves to first while every displaced keyed row shifts in place', () => {
  const f = fixture(); const items = f.items([1, 2, 3, 4, 5]);
  f.list.clientHeight = 500;
  f.controller.reconcile(items);
  const originals = f.list.childNodes.slice();
  const result = f.controller.reconcile([{ ...items[4], activity: 6 }, ...items.slice(0, 4)], { animate: true });
  assert.deepEqual(f.ids(), [5, 1, 2, 3, 4]);
  assert.equal(result.activityCount, 1);
  assert.equal(f.renders, 6);
  assert.equal(f.list.childNodes[0], originals[4]);
  originals.forEach(row => assert.equal(row.animations.length, 1));
  assert.equal(originals[4].animations[0].frames[0].transform, 'translate3d(0, 320px, 0)');
  assert.equal(originals[0].animations[0].frames[0].transform, 'translate3d(0, -80px, 0)');
  assert.equal(originals[4].animations[0].options.duration, DURATION_MS);
  assert.equal(originals[2].classList.contains('active'), true);
});

test('unchanged snapshots create no nodes or moves and duplicate keys cannot duplicate rows', () => {
  const f = fixture(); const items = f.items([1, 2, 3]);
  f.controller.reconcile(items); const mutations = f.document.mutations;
  f.controller.reconcile([...items, items[1]], { animate: true });
  assert.equal(f.renders, 3); assert.equal(f.document.mutations, mutations);
  assert.deepEqual(f.ids(), [1, 2, 3]);
  f.list.childNodes.forEach(row => assert.equal(row.animations.length, 0));
});

test('fresh data updates existing name and text nodes while preserving focus and text selection', () => {
  const f = fixture(); const items = f.items([1, 2, 3]);
  f.controller.reconcile(items);
  const row = f.list.childNodes[2]; const name = row.firstChild; const text = name.firstChild;
  row.focus(); f.selection.setBaseAndExtent(text, 1, text, 7);
  f.controller.reconcile([{ ...items[2], name: 'New name', activity: 4 }, ...items.slice(0, 2)], { animate: true });
  assert.equal(f.list.firstChild, row); assert.equal(row.firstChild, name); assert.equal(name.firstChild, text);
  assert.equal(text.nodeValue, 'New name'); assert.equal(f.document.activeElement, row);
  assert.deepEqual(row.focusOptions, { preventScroll: true });
  assert.equal(f.selection.anchorNode, text); assert.equal(f.selection.focusOffset, 7);
});

test('rapid updates rebase from the visible animation frame and stale cleanup cannot cancel the replacement', () => {
  const f = fixture(); const items = f.items([1, 2, 3, 4, 5]);
  f.controller.reconcile(items);
  const row = f.list.firstChild;
  f.controller.reconcile([items[4], ...items.slice(0, 4)], { animate: true });
  const previous = row.animations.at(-1); const staleFinish = previous.onfinish;
  row.visualOffset = -42;
  const visibleTop = row.getBoundingClientRect().top;
  f.controller.reconcile([items[0], items[4], ...items.slice(1, 4)], { animate: true });
  const next = row.animations.at(-1);
  assert.equal(previous.cancelled, true); assert.notEqual(previous, next);
  assert.equal(next.frames[0].transform, `translate3d(0, ${visibleTop - row.getBoundingClientRect().top}px, 0)`);
  staleFinish();
  assert.equal(next.cancelled, false); assert.equal(row.classList.contains('is-live-moving'), true);
  next.onfinish();
  assert.equal(row.classList.contains('is-live-moving'), false); assert.equal(next.cancelled, true);
});

test('reading anchor stays at the same viewport position away from the list top', () => {
  const f = fixture(); const items = f.items([1, 2, 3, 4, 5, 6]);
  f.controller.reconcile(items); f.list.scrollTop = 170;
  const anchor = f.list.childNodes[2]; const before = anchor.getBoundingClientRect().top;
  f.controller.reconcile([items[5], ...items.slice(0, 5)], { animate: true });
  assert.equal(f.list.scrollTop, 250); assert.equal(anchor.getBoundingClientRect().top, before);
});

test('mobile document scrolling preserves the visible client when the list has no inner scroll', () => {
  const f = fixture(); const items = f.items([1, 2, 3, 4, 5, 6]);
  f.document.defaultView.getComputedStyle = () => ({ overflowY: 'visible' });
  f.controller.reconcile(items); f.document.defaultView.scrollY = 260;
  const anchor = f.list.childNodes[2]; const before = anchor.getBoundingClientRect().top;
  f.controller.reconcile([items[5], ...items.slice(0, 5)], { animate: true });
  assert.equal(f.document.defaultView.scrollY, 340);
  assert.equal(anchor.getBoundingClientRect().top, before);
});

test('visibility and reduced-motion changes immediately settle active movement', () => {
  for (const settle of ['hide', 'reduceMotion']) {
    const f = fixture(); const items = f.items([1, 2, 3]);
    f.controller.reconcile(items);
    f.controller.reconcile(items.slice().reverse(), { animate: true });
    const row = f.list.firstChild; const animation = row.animations.at(-1);
    f[settle]();
    assert.equal(animation.cancelled, true); assert.equal(row.classList.contains('is-live-moving'), false);
    const count = row.animations.length;
    f.controller.reconcile(items, { animate: true });
    assert.deepEqual(f.ids(), [1, 2, 3]); assert.equal(row.animations.length, count);
  }
});

test('removed focused client has a deliberate focus fallback and teardown releases listeners', () => {
  const f = fixture(); const items = f.items([1, 2]); f.controller.reconcile(items);
  f.list.firstChild.focus(); f.controller.reconcile([items[1]], { animate: true });
  assert.equal(f.focusRemoved, 1);
  f.controller.destroy(); assert.equal(f.visibility.size, 0); assert.equal(f.motionHandlers.size, 0);
  f.controller.reconcile(items); assert.deepEqual(f.ids(), [2]);
});
