const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.resolve(__dirname, '../../twocomms/twocomms_django_theme/static/js/analytics-loader.js'), 'utf8');

function browser(attrs = {}, initial = {}) {
  const scripts = [];
  const listeners = new Map();
  const timers = [];
  const idle = [];
  const intervals = [];
  const cookies = new Map(Object.entries(initial.cookies || {}));
  const doc = {
    documentElement: { getAttribute: name => attrs[name] || '', dataset: {} },
    readyState: 'complete', title: 'Offline analytics check',
    getElementById: () => null, querySelector: () => null,
    getElementsByTagName: name => name === 'script' ? scripts : [],
    createElement: () => ({ dataset: {} }),
    addEventListener: () => {},
    head: { appendChild(script) { script.parentNode = this; scripts.push(script); },
      insertBefore(script) { this.appendChild(script); } },
  };
  Object.defineProperty(doc, 'cookie', {
    get: () => [...cookies].map(([key, value]) => key + '=' + value).join('; '),
    set: value => { const pair = value.split(';')[0]; const split = pair.indexOf('='); cookies.set(pair.slice(0, split), pair.slice(split + 1)); },
  });
  const win = {
    location: { search: initial.search || '', href: 'https://example.invalid/product/', protocol: 'https:' },
    navigator: {},
    requestIdleCallback: fn => { idle.push(fn); },
    addEventListener(name, fn) { const callbacks = listeners.get(name) || new Set(); callbacks.add(fn); listeners.set(name, callbacks); },
    removeEventListener(name, fn) { listeners.get(name)?.delete(fn); },
    ...initial.window,
  };
  const context = vm.createContext({
    window: win, document: doc, URLSearchParams,
    setTimeout: (fn, delay) => { timers.push({ fn, delay }); return timers.length; }, clearTimeout: () => {},
    setInterval: fn => { intervals.push(fn); return intervals.length; }, clearInterval: () => {},
    console: { log() {}, debug() {}, warn() {} },
  });
  const run = () => vm.runInContext(source, context);
  const interact = () => [...(listeners.get('pointerdown') || [])].forEach(fn => fn());
  const runIdle = () => { let limit = 50; while (idle.length && limit--) idle.shift()(); assert.ok(limit > 0); };
  run();
  const emit = name => [...(listeners.get(name) || [])].forEach(fn => fn());
  return { win, doc, scripts, run, interact, runIdle, timers, idle, intervals, emit };
}

test('early Meta calls survive missing inline pixel, preserving CAPI ID and custom method', () => {
  const b = browser({ 'data-meta-pixel-id': 'offline-meta' }, {
    window: { _trackEventQueue: [['SelectShippingPoint', { event_id: 'source-event-1' }]] },
  });
  b.interact();
  const calls = b.win.fbq.queue.map(args => Array.from(args));
  const tracked = calls.filter(args => args[1] === 'SelectShippingPoint');
  assert.equal(tracked.length, 1);
  assert.equal(tracked[0][0], 'trackCustom');
  assert.equal(tracked[0][3].eventID, 'source-event-1');
});

test('loading orchestrator twice does not disable an already active Meta bridge', () => {
  const b = browser({ 'data-meta-pixel-id': 'offline-meta' });
  b.interact();
  const before = b.win.fbq.queue.length;
  b.run();
  b.win.trackEvent('AddToCart', { event_id: 'after-repeat', value: 100, currency: 'UAH' });
  assert.equal(b.win.fbq.queue.length, before + 1);
  assert.equal(b.win._fbqBuffer.length, 0);
});

test('GTM owns matching GA and Clarity without duplicate scripts, config or pageview', () => {
  const b = browser({ 'data-ga-id': 'G-OFFLINE', 'data-gtm-ga-id': 'G-OFFLINE',
    'data-clarity-id': 'offline-clarity', 'data-gtm-clarity-id': 'offline-clarity' });
  b.win.trackEvent('Contact', { method: 'form_submit' });
  b.interact();
  b.runIdle();
  assert.equal(b.scripts.length, 0);
  const calls = b.win.dataLayer.map(args => Array.from(args));
  assert.equal(calls.filter(args => args[0] === 'event' && args[1] === 'Contact').length, 1);
  assert.equal(calls.filter(args => args[0] === 'config' || args[1] === 'page_view').length, 0);
});

test('standalone GA initializes queue before early events and requests only one script', () => {
  const b = browser({ 'data-ga-id': 'G-OFFLINE' });
  b.win.trackEvent('Contact', { method: 'form_submit' });
  b.runIdle();
  b.run();
  b.runIdle();
  const ga = b.scripts.filter(script => script.src.includes('/gtag/js'));
  assert.equal(ga.length, 1);
  ga[0].onload();
  const calls = b.win.dataLayer.map(args => Array.from(args));
  const config = calls.findIndex(args => args[0] === 'config');
  const contact = calls.findIndex(args => args[0] === 'event' && args[1] === 'Contact');
  assert.ok(config >= 0 && config < contact, 'destination must be configured before queued events');
  assert.equal(calls.filter(args => args[0] === 'event' && args[1] === 'page_view').length, 1);
});

test('TikTok flushes original conversion ID once without readiness polling', () => {
  const b = browser({ 'data-tiktok-pixel-id': 'offline-tiktok' });
  b.win.trackEvent('Purchase', { event_id: 'server-purchase', value: 100, currency: 'UAH' });
  const tt = b.scripts.find(script => script.src.includes('analytics.tiktok.com'));
  assert.ok(tt);
  tt.onload();
  assert.equal(b.intervals.length, 0);
  const events = b.win.ttq.filter(call => call[0] === 'track');
  assert.equal(events.length, 1);
  assert.equal(events[0][1], 'CompletePayment');
  assert.equal(events[0][2].event_id, 'server-purchase');
  assert.equal(b.win._ttqBuffer.length, 0);
  b.run();
  b.win.trackEvent('AddToCart', { event_id: 'later-cart' });
  assert.equal(b.win.ttq.filter(call => call[0] === 'track').length, 2);
});

test('Instagram checkout consent denial leaves all SDKs and identifiers inactive', () => {
  const b = browser({ 'data-route-name': 'ig_checkout_proposal', 'data-ga-id': 'G-OFFLINE',
    'data-meta-pixel-id': 'offline-meta', 'data-tiktok-pixel-id': 'offline-tiktok' });
  b.win.trackEvent('Purchase', { event_id: 'not-authorized' });
  b.interact(); b.runIdle();
  assert.equal(b.scripts.length, 0);
  assert.equal(b.doc.cookie, '');
  assert.equal(b.win.__twcAnalyticsConsent, false);
});

test('ownership suppresses only exact destination matches', () => {
  const b = browser({ 'data-ga-id': 'G-NEW', 'data-gtm-ga-id': 'G-OLD',
    'data-clarity-id': 'new-clarity', 'data-gtm-clarity-id': 'old-clarity' });
  b.interact(); b.runIdle();
  assert.equal(b.scripts.filter(s => s.src.includes('/gtag/js?id=G-NEW')).length, 1);
  assert.equal(b.scripts.filter(s => s.src.includes('/tag/new-clarity')).length, 1);
});

test('terminal GTM failure activates standalone destinations once, also when failure preceded loader', () => {
  const attrs = { 'data-ga-id': 'G-OFFLINE', 'data-gtm-ga-id': 'G-OFFLINE',
    'data-clarity-id': 'offline-clarity', 'data-gtm-clarity-id': 'offline-clarity' };
  for (const priorFailure of [false, true]) {
    const b = browser(attrs, { window: { __twcGtmFailed: priorFailure } });
    if (!priorFailure) b.emit('twc:gtm-failed');
    b.emit('twc:gtm-failed'); b.runIdle();
    assert.equal(b.scripts.filter(s => s.src.includes('/gtag/js')).length, 1);
    assert.equal(b.scripts.filter(s => s.src.includes('/tag/offline-clarity')).length, 1);
    const calls = b.win.dataLayer.map(args => Array.from(args));
    assert.equal(calls.filter(args => args[0] === 'config').length, 1);
    assert.equal(calls.filter(args => args[0] === 'event' && args[1] === 'page_view').length, 1);
  }
});

test('existing inline Meta PageView is retained without a duplicate PageView', () => {
  const sent = [['init', 'offline-meta'], ['track', 'PageView']];
  const b = browser({ 'data-meta-pixel-id': 'offline-meta' }, {
    window: { __fbPixelInline: true, fbq: (...args) => sent.push(args) },
  });
  b.interact(); b.runIdle(); b.run(); b.interact();
  assert.equal(sent.filter(args => args[0] === 'track' && args[1] === 'PageView').length, 1);
  assert.equal(b.scripts.filter(s => s.src.includes('fbevents.js')).length, 0);
});

test('inline Meta queue receives early ViewContent immediately without interaction or idle callbacks', () => {
  const sent = [['init', 'offline-meta'], ['track', 'PageView']];
  const b = browser({ 'data-meta-pixel-id': 'offline-meta' }, {
    window: { __fbPixelInline: true, fbq: (...args) => sent.push(args),
      _trackEventQueue: [['ViewContent', { event_id: 'early-product-id', value: 123, currency: 'UAH' }]],
    },
  });
  const view = sent.filter(args => args[1] === 'ViewContent');
  assert.equal(view.length, 1);
  assert.equal(view[0][0], 'track');
  assert.equal(view[0][3].eventID, 'early-product-id');
  assert.equal(b.win._fbqBuffer.length, 0);
  assert.equal(sent.filter(args => args[1] === 'PageView').length, 1);
  assert.equal(b.scripts.length, 0, 'reuse the already injected inline SDK queue');
});

test('temporary Meta handoff failure preserves queued event and retries once SDK recovers', () => {
  const sent = [];
  let throws = true;
  const b = browser({ 'data-meta-pixel-id': 'offline-meta' }, {
    window: { __fbPixelInline: true,
      _trackEventQueue: [['SelectShippingPoint', { event_id: 'kept-id' }]],
      fbq: (...args) => { if (args[1] === 'SelectShippingPoint' && throws) throw new Error('SDK unavailable'); sent.push(args); },
    },
  });
  b.interact();
  assert.equal(b.win._fbqBuffer.length, 1);
  throws = false;
  b.win.trackEvent('Contact', { event_id: 'next-id' });
  const retried = sent.filter(args => args[1] === 'SelectShippingPoint');
  assert.equal(retried.length, 1);
  assert.equal(retried[0][0], 'trackCustom');
  assert.equal(retried[0][3].eventID, 'kept-id');
  assert.equal(b.win._fbqBuffer.length, 0);
});

test('temporary TikTok handoff failure preserves conversion and retries after SDK recovers', () => {
  const b = browser({ 'data-tiktok-pixel-id': 'offline-tiktok' });
  b.win.trackEvent('Purchase', { event_id: 'kept-purchase' });
  const tt = b.scripts.find(s => s.src.includes('analytics.tiktok.com'));
  b.win.ttq.track = () => { throw new Error('SDK unavailable'); };
  tt.onload();
  assert.equal(b.win._ttqBuffer.length, 1);
  const sent = [];
  b.win.ttq.track = (...args) => sent.push(args);
  b.win.trackEvent('Contact', { event_id: 'next-contact' });
  assert.equal(sent.filter(args => args[0] === 'CompletePayment').length, 1);
  assert.equal(sent[0][1].event_id, 'kept-purchase');
  assert.equal(b.win._ttqBuffer.length, 0);
});

function ecommerce(event = 'purchase', eventId = 'source-id') {
  return { event, event_id: eventId,
    ecommerce: { transaction_id: 'order-1', value: 123, currency: 'UAH',
      items: [{ item_id: 'TC-OFFLINE-S', quantity: 2, price: 61.5 }] } };
}

function eventCommands(layer, name) {
  return layer.filter(entry => entry[0] === 'event' && (!name || entry[1] === name));
}

test('terminal GTM failure replays all six canonical events after config, retaining values and source IDs', () => {
  const names = ['view_item', 'add_to_cart', 'begin_checkout', 'purchase', 'view_item_list', 'select_item'];
  const pending = names.map(name => ecommerce(name, 'id-' + name));
  const layer = pending.slice();
  const b = browser({ 'data-ga-id': 'G-OFFLINE', 'data-gtm-ga-id': 'G-OFFLINE' },
    { window: { dataLayer: layer } });
  assert.equal(eventCommands(layer, 'purchase').length, 0, 'healthy GTM retains sole covered-event ownership');
  b.emit('twc:gtm-failed');
  assert.equal(b.win.dataLayer, layer, 'do not replace the original queue');
  const configIndex = layer.findIndex(entry => entry[0] === 'config');
  for (const name of names) {
    const calls = eventCommands(layer, name);
    assert.equal(calls.length, 1);
    assert.ok(layer.indexOf(calls[0]) > configIndex);
    assert.equal(calls[0][2].event_id, 'id-' + name);
    assert.equal(calls[0][2].send_to, 'G-OFFLINE');
    assert.equal(calls[0][2].value, 123);
    assert.equal(calls[0][2].items[0].item_id, 'TC-OFFLINE-S');
    assert.equal(pending.find(entry => entry.event === name).ecommerce.send_to, undefined);
  }
});

test('failed-GTM observer preserves patched push receiver, variadic arguments and return value', () => {
  const layer = [];
  let calls = 0;
  layer.push = function (...entries) {
    assert.equal(this, layer);
    calls++;
    Array.prototype.push.apply(this, entries);
    return 'vendor-result';
  };
  const b = browser({ 'data-ga-id': 'G-OFFLINE', 'data-gtm-ga-id': 'G-OFFLINE' },
    { window: { dataLayer: layer, __twcGtmFailed: true } });
  const before = calls;
  const result = layer.push(ecommerce('purchase', 'future-1'), ecommerce('add_to_cart', 'future-2'));
  assert.equal(result, 'vendor-result');
  assert.equal(calls, before + 3, 'original object push plus two non-recursive gtag command pushes');
  assert.equal(eventCommands(layer, 'purchase').length, 1);
  assert.equal(eventCommands(layer, 'add_to_cart').length, 1);
  b.win.gtag('event', 'purchase', { event_id: 'manual-command', send_to: 'G-OFFLINE' });
  assert.equal(eventCommands(layer, 'purchase').length, 2, 'commands themselves are not relayed');
});

test('fallback deduplicates recreated events by name and ID and skips supplemental listing handoffs', () => {
  const directListing = ecommerce('select_item', 'already-direct');
  directListing.ga_direct_sent = true;
  const layer = [ecommerce('purchase', 'same-id'), ecommerce('purchase', 'same-id'), directListing];
  const b = browser({ 'data-ga-id': 'G-OFFLINE', 'data-gtm-ga-id': 'G-OFFLINE' },
    { window: { dataLayer: layer, __twcGtmFailed: true } });
  layer.push(ecommerce('purchase', 'same-id'), ecommerce('add_to_cart', 'same-id'));
  b.emit('twc:gtm-failed');
  assert.equal(eventCommands(layer, 'purchase').length, 1);
  assert.equal(eventCommands(layer, 'add_to_cart').length, 1, 'same ID on a different event remains distinct');
  assert.equal(eventCommands(layer, 'select_item').length, 0);
});

test('fallback becomes dormant if GTM recovers, without replacing the live vendor queue', () => {
  const b = browser({ 'data-ga-id': 'G-OFFLINE', 'data-gtm-ga-id': 'G-OFFLINE' },
    { window: { __twcGtmFailed: true } });
  const push = b.win.dataLayer.push;
  b.win.__twcGtmFailed = false;
  b.win.dataLayer.push(ecommerce('purchase', 'after-recovery'));
  assert.equal(b.win.dataLayer.push, push);
  assert.equal(eventCommands(b.win.dataLayer, 'purchase').length, 0);
});

test('late GTM failure can install relay even while an independent GA script is already in flight', () => {
  const b = browser({ 'data-ga-id': 'G-OFFLINE' });
  b.runIdle();
  b.win.dataLayer.push(ecommerce('purchase', 'late-failure'));
  b.emit('twc:gtm-failed');
  assert.equal(eventCommands(b.win.dataLayer, 'purchase').length, 1);
  assert.equal(b.scripts.filter(script => script.src.includes('/gtag/js')).length, 1);
});

test('consent denial prevents fallback from relaying buffered or future ecommerce', () => {
  const layer = [ecommerce('purchase', 'denied-before')];
  const b = browser({ 'data-route-name': 'ig_checkout_proposal', 'data-ga-id': 'G-OFFLINE',
    'data-gtm-ga-id': 'G-OFFLINE' }, { window: { dataLayer: layer, __twcGtmFailed: true } });
  b.emit('twc:gtm-failed');
  layer.push(ecommerce('purchase', 'denied-after'));
  assert.equal(eventCommands(layer, 'purchase').length, 0);
  assert.equal(b.scripts.length, 0);
});
