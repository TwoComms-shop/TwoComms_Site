const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assetDir = path.join(__dirname, '../../twocomms/management/static/management');
const source = fs.readFileSync(path.join(assetDir, 'js/ig_memory_card.js'), 'utf8');

class Node {
  constructor(tag, ownerDocument) { this.tagName = tag; this.ownerDocument = ownerDocument; this.children = []; this.attributes = {}; this.listeners = {}; this._text = ''; }
  append(...nodes) { this.children.push(...nodes); }
  appendChild(node) { this.children.push(node); return node; }
  replaceChildren(...nodes) { this.children = nodes; this._text = ''; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this._text = String(value); this.children = []; }
  all() { return [this, ...this.children.flatMap(child => child.all())]; }
}
function fixture() {
  const document = { createElement(tag) { return new Node(tag, this); } }, root = new Node('div', document), window = {};
  vm.runInNewContext(source, { window, Intl, Date });
  return { root, api: window.IgMemoryCard };
}
function timeline() {
  return { schema: 'ig-memory-presentation.v1', status: 'timeline', events: [
    { event_at: '2026-10-07T14:25:00Z', time_basis: 'provider', topic_label: 'Розмір', quote: 'Ношу M, але хочу трохи вільнішу посадку.\nМожна заміри L?', source_id: 34, scope_label: 'Розмова клієнта' },
    { event_at: '2026-10-08T11:03:00Z', time_basis: 'local_ingest', topic_label: '', quote: 'Це для подарунка, упаковка не потрібна', source_id: null, scope_label: 'Розмова клієнта' }
  ], coverage: { source_count: 26, selected_count: 2, retained_count: 4, event_count: 2, omitted_count: 3, pending_count: 2,
    omissions: [{ label: 'Недостатньо даних у джерелі', count: 2 }, { label: 'Джерело недоступне', count: 1 }] },
  updated_at: '2026-10-08T11:04:00Z', as_of: '2026-10-08T11:03:00Z' };
}
function escape(value) { return String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
function html(node) {
  const attrs = { ...node.attributes, ...(node.className ? { class: node.className } : {}), ...(node.href ? { href: node.href } : {}), ...(node.title ? { title: node.title } : {}) };
  return '<' + node.tagName + Object.entries(attrs).map(([key, value]) => ' ' + key + '="' + escape(value) + '"').join('') + '>' + escape(node._text) + node.children.map(html).join('') + '</' + node.tagName + '>';
}
module.exports = { fixture, timeline, html, source, assetDir };

if (require.main === module) {
  const output = process.argv[2] || '/tmp/twc-ig-memory-card-preview.html';
  const sections = [];
  for (const [title, view] of [ ['Історія повідомлень', timeline()], ['Довга цитата', { ...timeline(), events: [{ ...timeline().events[0], quote: 'ДовгийТекстБезПробілів'.repeat(12) + '\n<img src=x onerror=alert(1)>' }] }],
    ['Порожня пам’ять', { schema: 'ig-memory-presentation.v1', status: 'empty' }],
    ['Попередній формат', { schema: 'ig-memory-presentation.v1', status: 'legacy', legacy_text: 'Клієнт цікавився футболками. Уточнити побажання у переписці.' }],
    ['Недоступні дані', { schema: 'ig-memory-presentation.v1', status: 'unavailable', reason_label: 'Джерела поки неможливо перевірити.' }] ]) {
    const { root, api } = fixture(); api.render(root, view, { sourceHref: id => '/bot/?section=clients&client_id=12&message_id=' + id });
    sections.push('<section><h2>' + title + '</h2>' + html(root) + '</section>');
  }
  const css = fs.readFileSync(path.join(assetDir, 'ig_memory_card.css'), 'utf8');
  fs.writeFileSync(output, '<!doctype html><html lang="uk"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Карточка пам’яті — тестовий стенд</title><style>body{margin:0;background:#090e16;color:#dbe5f2;font-family:system-ui,sans-serif;}main{box-sizing:border-box;width:100%;max-width:440px;padding:16px;margin:auto;}section{margin-bottom:26px;}h2{font-size:12px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:#98a7bd;margin:0 0 12px;}' + css + '</style><main>' + sections.join('') + '</main></html>');
  process.stdout.write(output + '\n');
}
