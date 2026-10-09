const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.resolve(__dirname, '../../twocomms/twocomms_django_theme/static/js/analytics-startup.js'), 'utf8');

function browser({ search = '', gtmId = 'GTM-OFFLINE' } = {}) {
  const scripts = [], timers = [], listeners = new Map(), emitted = [];
  const head = {
    appendChild(script) { scripts.push(script); script.parentNode = head; },
    removeChild(script) { script.parentNode = null; },
  };
  const document = {
    documentElement: { getAttribute: name => name === 'data-gtm-id' ? gtmId : '' },
    currentScript: { getAttribute: () => '/static/js/analytics-loader.js?v=offline' },
    createElement: () => ({}), head,
  };
  const window = {
    location: { search },
    setTimeout: (fn, delay) => { timers.push({ fn, delay }); return timers.length; },
    addEventListener: (name, fn) => listeners.set(name, fn),
    removeEventListener: (name, fn) => { if (listeners.get(name) === fn) listeners.delete(name); },
    CustomEvent: function (type) { this.type = type; },
    dispatchEvent: event => emitted.push(event.type),
  };
  const context = { window, document, Date, encodeURIComponent };
  const run = () => vm.runInNewContext(source, context);
  const tick = delay => {
    const index = timers.findIndex(timer => timer.delay === delay);
    assert.notEqual(index, -1, 'expected scheduled timer ' + delay);
    timers.splice(index, 1)[0].fn();
  };
  run();
  return { scripts, timers, listeners, emitted, window, run, tick };
}

test('organic visits start GTM without interaction and loader in one second', () => {
  const b = browser();
  assert.equal(b.scripts.length, 0);
  b.tick(0); b.tick(1000);
  assert.equal(b.scripts.length, 2);
  assert.match(b.scripts[0].src, /gtm\.js\?id=GTM-OFFLINE$/);
  assert.match(b.scripts[1].src, /analytics-loader/);
  assert.equal(b.window.dataLayer.filter(x => x.event === 'gtm.js').length, 1);
});

test('paid visit and fast repeated interactions still inject each script once', () => {
  const b = browser({ search: '?utm_source=offline' });
  assert.equal(b.scripts.length, 2);
  b.listeners.get('pointerdown')(); b.listeners.get('click')();
  assert.equal(b.scripts.length, 2);
  b.scripts.forEach(script => script.onload());
  assert.equal(b.listeners.size, 0);
});

test('GTM definite failure retries once and signals fallback only on terminal failure', () => {
  const b = browser(); b.tick(0);
  b.scripts[0].onerror();
  assert.equal(b.window.__twcGtmFailed, undefined);
  b.tick(1500);
  assert.equal(b.scripts.length, 2);
  b.scripts[1].onerror();
  assert.equal(b.window.__twcGtmFailed, true);
  assert.deepEqual(b.emitted, ['twc:gtm-failed']);
  assert.equal(b.window.dataLayer.filter(x => x.event === 'gtm.js').length, 1);
  b.tick(1000);
  assert.equal(b.scripts.length, 3, 'one loader, no third GTM attempt');
});

test('GTM retry success clears fallback and does not reissue initialization event', () => {
  const b = browser(); b.tick(0); b.scripts[0].onerror(); b.tick(1500);
  b.scripts[1].onload(); b.tick(1000); b.scripts[2].onload();
  assert.equal(b.window.__twcGtmFailed, false);
  assert.deepEqual(b.emitted, []);
  assert.equal(b.listeners.size, 0);
});

test('loader definite failure can recover once without duplicating GTM', () => {
  const b = browser({ search: '?fbclid=offline' });
  b.scripts[0].onload(); b.scripts[1].onerror(); b.tick(1500);
  b.scripts[2].onload();
  assert.equal(b.scripts.filter(x => /gtm\.js/.test(x.src)).length, 1);
  assert.equal(b.listeners.size, 0);
});

test('slow scripts stay in flight without timeout reinjection', () => {
  const b = browser({ search: '?gclid=offline' });
  b.listeners.get('scroll')();
  assert.equal(b.scripts.length, 2);
  assert.equal(b.timers.length, 0);
});

test('pages without GTM retain direct loader and an early Google event queue', () => {
  const b = browser({ gtmId: '' });
  b.window.gtag('event', 'offline-event', { event_id: 'early-id' });
  b.tick(0); b.tick(1000);
  assert.equal(b.scripts.length, 1);
  assert.equal(b.window.dataLayer[0][0], 'event');
  assert.equal(b.window.dataLayer[0][2].event_id, 'early-id');
});

test('reexecuting startup preserves queues and does not add timers or listeners', () => {
  const b = browser();
  b.window.dataLayer.push({ event: 'view_item', ecommerce: { value: 123 } });
  b.run();
  assert.equal(b.timers.length, 2);
  assert.equal(b.window.dataLayer[0].ecommerce.value, 123);
});
