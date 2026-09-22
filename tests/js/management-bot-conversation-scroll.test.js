const test = require('node:test');
const assert = require('node:assert/strict');

const { pinToBottom } = require(
  '../../twocomms/management/static/management/bot_conversation_scroll.js',
);

class FakeImage {
  constructor() {
    this.complete = false;
    this.listeners = {};
  }

  decode() {
    return new Promise(() => {});
  }

  addEventListener(type, callback) {
    this.listeners[type] = callback;
  }
}

class FakeMessages {
  constructor(image) {
    this.isConnected = true;
    this.scrollHeight = 100;
    this.scrollTop = 0;
    this.clientHeight = 100;
    this.image = image;
    this.listeners = {};
    this.lastElementChild = { scrollIntoView: () => { this.scrollIntoViewCalls += 1; } };
    this.scrollIntoViewCalls = 0;
  }

  querySelectorAll(selector) {
    return selector === 'img' ? [this.image] : [];
  }

  addEventListener(type, callback) {
    this.listeners[type] = callback;
  }

  removeEventListener(type, callback) {
    if (this.listeners[type] === callback) delete this.listeners[type];
  }

  dispatch(type) {
    this.listeners[type]?.();
  }
}

test('keeps a desktop conversation pinned after a late image resize', () => {
  const image = new FakeImage();
  const messages = new FakeMessages(image);
  const frames = [];
  let resizeCallback;
  const observer = {
    observe() {},
    disconnect() {},
  };
  class FakeResizeObserver {
    constructor(callback) {
      resizeCallback = callback;
      Object.assign(this, observer);
    }
  }
  const cleanup = pinToBottom(messages, {
    force: true,
    requestAnimationFrame: (callback) => { frames.push(callback); return frames.length; },
    cancelAnimationFrame: () => {},
    ResizeObserver: FakeResizeObserver,
    setTimeout: () => 1,
    clearTimeout: () => {},
  });

  while (frames.length) frames.shift()();
  assert.equal(messages.scrollTop, 100);

  // Simulate the browser discovering the intrinsic image size well after the
  // initial two animation frames. ResizeObserver must still repin the tail.
  messages.scrollHeight = 360;
  resizeCallback();
  while (frames.length) frames.shift()();
  assert.equal(messages.scrollTop, 360);

  cleanup();
});

test('stops repinning once the operator scrolls away from the bottom', () => {
  const image = new FakeImage();
  const messages = new FakeMessages(image);
  const frames = [];
  let resizeCallback;
  class FakeResizeObserver {
    constructor(callback) { resizeCallback = callback; }
    observe() {}
    disconnect() {}
  }
  pinToBottom(messages, {
    force: true,
    requestAnimationFrame: (callback) => { frames.push(callback); return frames.length; },
    cancelAnimationFrame: () => {},
    ResizeObserver: FakeResizeObserver,
    setTimeout: () => 1,
    clearTimeout: () => {},
  });
  while (frames.length) frames.shift()();

  messages.scrollHeight = 300;
  messages.scrollTop = 0;
  messages.dispatch('scroll');
  messages.scrollHeight = 420;
  resizeCallback();
  while (frames.length) frames.shift()();
  assert.equal(messages.scrollTop, 0);
});
