const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const main = fs.readFileSync(path.resolve(__dirname, '../../twocomms/twocomms_django_theme/static/js/main.js'), 'utf8');
const source = main.slice(main.indexOf('// The published GTM container owns'), main.indexOf('// Опрос монтируется page-scoped'));

function listing({ ga = 'G-OFFLINE', failed = false, consent = true, storage = new Map(), url = 'https://example.com/catalog/', storageDenied = false } = {}) {
  const handlers = {}, window = { __twcGtmFailed: failed, __twcAnalyticsConsent: consent };
  const attrs = {
    'data-product-id': '12', 'data-default-offer-id': 'offer12',
    'data-product-title': 'Test tee', 'data-product-price': '740', 'data-product-category': 'T-shirts',
  };
  window.location = { href: url, origin: new URL(url).origin };
  window.sessionStorage = { getItem: key => storage.get(key), removeItem: key => storage.delete(key), setItem: (key, value) => { if (storageDenied) throw Error('Storage denied'); storage.set(key, value); } };
  const link = { href: 'https://example.com/product/test/', target: '', hasAttribute: () => false };
  const card = { getAttribute: name => attrs[name], contains: node => node === link };
  const document = {
    readyState: 'complete',
    documentElement: { getAttribute: name => ({ 'data-ga-id': ga, 'data-route-name': 'catalog' })[name] || '' },
    querySelectorAll: () => [card], addEventListener: (name, fn) => { handlers[name] = fn; },
  };
  let counter = 0;
  const context = { window, document, URL, ANALYTICS_BRAND_NAME: 'TwoComms',
    safeGenerateAnalyticsEventId: () => 'offline-' + (++counter), scheduleIdle: fn => fn() };
  const run = () => vm.runInNewContext('(function () {\n' + source + '\n})();', context);
  run();
  return { window, run, storage, link, click: (isLink, options = {}) => handlers.click({ ...options, target: {
    closest: selector => selector === '.card.product' ? card : (isLink ? link : null),
  } }) };
}

test('actual list and link handlers queue one GA event and retain GTM object with shared ID', () => {
  const b = listing(); b.click(true);
  for (const name of ['view_item_list', 'select_item']) {
    const commands = b.window.dataLayer.filter(x => x[0] === 'event' && x[1] === name);
    const records = b.window.dataLayer.filter(x => x.event === name);
    assert.equal(commands.length, 1); assert.equal(records.length, 1);
    assert.equal(commands[0][2].event_id, records[0].event_id);
    assert.equal(commands[0][2].send_to, 'G-OFFLINE');
    assert.equal(records[0].ga_direct_sent, true);
    assert.equal(commands[0][2].items[0].item_id, 'offer12');
    assert.equal(commands[0][2].items[0].price, 740);
  }
});

test('favorite and color button clicks are not product selections', () => {
  const b = listing(); b.click(false);
  assert.equal(b.window.dataLayer.filter(x => x.event === 'select_item').length, 0);
});

test('a repeated list initializer emits only one list impression', () => {
  const b = listing(); b.run();
  assert.equal(b.window.dataLayer.filter(x => x.event === 'view_item_list').length, 1);
});

test('terminal GTM failure leaves canonical entries for the loader relay', () => {
  const b = listing({ failed: true }); b.click(true);
  assert.equal(b.window.dataLayer.filter(x => x[0] === 'event').length, 0);
  assert.equal(b.window.dataLayer.filter(x => x.event).length, 2);
  assert.ok(b.window.dataLayer.every(x => !x.ga_direct_sent));
});

test('disabled GA or denied consent does not queue direct Google commands', () => {
  for (const options of [{ ga: '' }, { consent: false }]) {
    const b = listing(options); b.click(true);
    assert.equal(b.window.dataLayer.filter(x => x[0] === 'event').length, 0);
  }
});

test('same-tab selection continues once at its destination without delaying navigation', () => {
  const b = listing();
  b.click(true, { button: 0, preventDefault: () => assert.fail('navigation delayed') });
  assert.equal(b.window.dataLayer.filter(x => x[1] === 'select_item').length, 0);
  assert.equal(b.storage.size, 1);
  const destination = listing({ storage: b.storage, url: b.link.href });
  const command = destination.window.dataLayer.find(x => x[1] === 'select_item');
  assert.equal(command[2].items[0].item_id, 'offer12');
  assert.equal(command[2].event_id, 'offline-2');
  assert.equal(command[2].page_location, 'https://example.com/catalog/');
  assert.equal(b.storage.size, 0);
  assert.equal(listing({ storage: b.storage, url: b.link.href }).window.dataLayer.filter(x => x[1] === 'select_item').length, 0);
});

test('unavailable storage falls back to direct tracking and native navigation', () => {
  const b = listing({ storageDenied: true });
  b.click(true, { button: 0, preventDefault: () => assert.fail('navigation delayed') });
  assert.equal(b.window.dataLayer.filter(x => x[1] === 'select_item').length, 1);
});

test('modified, new-tab, downloaded, external and unavailable analytics do not stage selection', () => {
  for (const options of [{ ctrlKey: true }, { metaKey: true }, { shiftKey: true }, { altKey: true }, { button: 1 }, { defaultPrevented: true }]) {
    const b = listing(); b.click(true, { button: 0, ...options });
    assert.equal(b.storage.size, 0);
  }
  for (const settings of [{ failed: true }, { ga: '' }, { consent: false }]) {
    const b = listing(settings); b.click(true, { button: 0 });
    assert.equal(b.storage.size, 0);
  }
  for (const kind of ['target', 'download', 'external']) {
    const b = listing();
    if (kind === 'target') b.link.target = '_blank';
    if (kind === 'download') b.link.hasAttribute = () => true;
    if (kind === 'external') b.link.href = 'https://other.example/product/';
    b.click(true, { button: 0 });
    assert.equal(b.storage.size, 0);
  }
});

test('expired, mismatched, malformed and denied-consent pending events are discarded', () => {
  for (const mode of ['expired', 'destination', 'malformed', 'consent']) {
    const b = listing(); b.click(true, { button: 0 });
    const key = [...b.storage.keys()][0];
    const pending = JSON.parse(b.storage.get(key));
    if (mode === 'expired') pending.expires = 1;
    if (mode === 'destination') pending.destination = 'https://example.com/other/';
    b.storage.set(key, mode === 'malformed' ? '{bad' : JSON.stringify(pending));
    const next = listing({ storage: b.storage, url: b.link.href, consent: mode !== 'consent' });
    assert.equal(next.window.dataLayer.filter(x => x[1] === 'select_item').length, 0);
    assert.equal(b.storage.size, 0);
  }
});
