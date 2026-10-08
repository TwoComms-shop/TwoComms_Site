const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { fixture, timeline, html, source, assetDir } = require('./management-ig-memory-card-harness.cjs');

test('literal quotes and hints are escaped text; machine prompt and unlisted diagnostics never render', () => {
  const { root, api } = fixture(), dto = timeline();
  const quote = '<img src=x onerror=alert(1)>\nA&B "цитата"';
  dto.events[0].quote = quote; dto.events[0].topic_label = '<script>bad()</script>';
  dto.raw = '[HISTORICAL SOURCE OBSERVATIONS] coverageJSON'; dto.events[0].diagnostic = 'ISOUTC machine-only';
  assert.equal(api.render(root, dto), 'timeline');
  assert.equal(root.all().find(node => node.tagName === 'blockquote').textContent, quote);
  assert.ok(!root.all().some(node => ['img', 'script', 'pre'].includes(node.tagName)));
  assert.ok(!root.textContent.includes('machine-only')); assert.ok(!root.textContent.includes('coverageJSON'));
  assert.match(html(root), /&lt;img src=x/); assert.ok(!source.includes('innerHTML'));
});

test('Kyiv local date uses correct summer and winter offsets and refuses ambiguous timestamps', () => {
  const { api } = fixture();
  assert.match(api.kyivDate('2026-10-08T21:05:00Z'), /09\.10\.2026.*00:05/);
  assert.match(api.kyivDate('2026-12-08T21:05:00Z'), /08\.12\.2026.*23:05/);
  for (const value of ['2026-10-08T21:05:00', 'yesterday', '', null, '2026-10-08']) assert.equal(api.kyivDate(value), null);
});

test('ingestion time stays visibly distinct from original message time; exact time machine attr remains', () => {
  const { root, api } = fixture(); api.render(root, timeline());
  assert.match(root.textContent, /Час збереження джерела/); assert.match(root.textContent, /Початковий час повідомлення не визначено/);
  const times = root.all().filter(node => node.tagName === 'time');
  assert.equal(times[0].attributes.datetime, '2026-10-07T14:25:00Z');
  assert.match(root.textContent, /Тема \(підказка\): Розмір/);
});

test('link opens exact internal source, preserves native modified clicks, and import has no false link', () => {
  const { root, api } = fixture(), opened = [];
  api.render(root, timeline(), { sourceHref: id => '/bot/?client_id=12&message_id=' + id, onSource: id => opened.push(id) });
  const links = root.all().filter(node => node.tagName === 'a'); assert.equal(links.length, 1);
  assert.equal(links[0].href, '/bot/?client_id=12&message_id=34');
  let prevented = false; links[0].listeners.click({ preventDefault() { prevented = true; } });
  assert.equal(prevented, true); assert.deepEqual(opened, [34]);
  links[0].listeners.click({ ctrlKey: true, preventDefault() { assert.fail('modified click intercepted'); } });
  assert.deepEqual(opened, [34]);
});

test('unsafe IDs and URLs cannot become source links', () => {
  for (const id of [null, 0, -1, '34evil', Number.MAX_SAFE_INTEGER + 1]) {
    const { root, api } = fixture(), dto = timeline(); dto.events[0].source_id = id;
    api.render(root, dto, { sourceHref: () => '/bot/' }); assert.ok(!root.all().some(node => node.tagName === 'a'));
  }
  for (const url of ['javascript:alert(1)', '//evil.example/', '/\\evil', '/bot/\nattack']) {
    const { root, api } = fixture(); api.render(root, timeline(), { sourceHref: () => url });
    assert.ok(!root.all().some(node => node.tagName === 'a'));
  }
});

test('coverage prose preserves dimensions, pending updates and finite omission labels', () => {
  const { root, api } = fixture(); api.render(root, timeline());
  assert.match(root.textContent, /Огляд охоплює 26 повідомлень клієнта\. Показано 2 події/);
  assert.match(root.textContent, /Нових повідомлень після огляду: 2/);
  assert.match(root.textContent, /Недостатньо даних у джерелі · 2/);
  assert.ok(!root.textContent.includes('4 із 26'));
  assert.equal(root.all().filter(node => node.tagName === 'summary').length, 3);
});

test('empty, legacy, unavailable and unsupported schema never promote machine memory', () => {
  for (const [view, expected] of [[null, 'unavailable'], [{ schema: 'unknown', status: 'timeline', events: timeline().events }, 'unavailable'],
    [{ schema: 'ig-memory-presentation.v1', status: 'empty' }, 'empty'],
    [{ schema: 'ig-memory-presentation.v1', status: 'unavailable', events: timeline().events }, 'unavailable']]) {
    const { root, api } = fixture(); assert.equal(api.render(root, view), expected); assert.ok(!root.all().some(node => node.tagName === 'blockquote'));
  }
  const { root, api } = fixture();
  api.render(root, { schema: 'ig-memory-presentation.v1', status: 'legacy', legacy_text: 'Попередній <текст>', events: timeline().events });
  assert.match(root.textContent, /немає перевіреної стрічки/); assert.match(root.textContent, /Попередній <текст>/);
  assert.equal(root.all().filter(node => node.tagName === 'summary').length, 1);
});

test('client rerender fully removes prior quote and source', () => {
  const { root, api } = fixture(); api.render(root, timeline(), { sourceHref: () => '/bot/' });
  api.render(root, { schema: 'ig-memory-presentation.v1', status: 'empty' });
  assert.ok(!root.textContent.includes('Ношу M')); assert.ok(!root.all().some(node => node.tagName === 'a'));
});

test('malformed timeline fails closed rather than claiming there is no saved memory', () => {
  for (const events of [null, [], [{ quote: ' ' }], [{ quote: { html: '<script>' } }]]) {
    const { root, api } = fixture(); assert.equal(api.render(root, { ...timeline(), events }), 'unavailable');
    assert.match(root.textContent, /Пам’ять недоступна/);
  }
});

test('verified empty timeline preserves safe omissions, pending updates and date without inventing an event', () => {
  const { root, api } = fixture(), dto = { ...timeline(), status: 'empty', events: [],
    reason_label: 'Огляд завершено; датованих спостережень не відібрано.' };
  assert.equal(api.render(root, dto), 'empty');
  assert.match(root.textContent, /Огляд охоплює 26 повідомлень клієнта\. Показано 0 подій/);
  assert.match(root.textContent, /Зафіксовано пропусків: 3/);
  assert.match(root.textContent, /Недостатньо даних у джерелі · 2/);
  assert.match(root.textContent, /Нових повідомлень після огляду: 2/);
  assert.match(root.textContent, /Оновлено 08\.10\.2026.*14:04.*за Києвом/);
  assert.ok(!root.all().some(node => node.tagName === 'blockquote' || node.tagName === 'ol' || node.tagName === 'a'));
  assert.ok(!root.textContent.includes('Ношу M'));
  for (const coverage of [{ source_count: 2 }, { omitted_count: 2 }, { pending_count: 2 }]) {
    api.render(root, { schema: 'ig-memory-presentation.v1', status: 'empty', coverage });
    assert.ok(root.all().some(node => node.className === 'ig-memory-coverage'));
    assert.ok(!root.textContent.includes('Показано збережені події'));
  }
  for (const coverage of [{}, { source_count: 0, omitted_count: 0, pending_count: 0 },
    { source_count: '26', omitted_count: -1, pending_count: Infinity }]) {
    api.render(root, { schema: 'ig-memory-presentation.v1', status: 'empty', coverage });
    assert.ok(!root.all().some(node => node.className === 'ig-memory-coverage'));
  }
});

test('template consumes projection only and layout has wrapping, shrink and keyboard focus rules', () => {
  const template = fs.readFileSync(path.join(__dirname, '../../twocomms/management/templates/management/bot.html'), 'utf8');
  assert.match(template, /IgMemoryCard\.render\(memoryRoot,c\.memory_view/);
  assert.ok(!template.includes("node('div','bot-mem',c.memory)"));
  const css = fs.readFileSync(path.join(assetDir, 'ig_memory_card.css'), 'utf8');
  assert.match(css, /min-width:0/); assert.match(css, /overflow-wrap:anywhere/);
  assert.match(css, /:focus-visible/); assert.match(css, /max-width:390px/);
  assert.match(css, /white-space:pre-wrap/);
});
