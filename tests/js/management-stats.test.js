const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const scriptPath = path.join(
  __dirname,
  '..',
  '..',
  'twocomms',
  'twocomms_django_theme',
  'static',
  'js',
  'management-stats.js',
);

const scriptSource = fs.readFileSync(scriptPath, 'utf8');

async function runManagementStats({
  cookie = '',
  hiddenCsrfToken = '',
  dismissUrl = '/stats/advice/dismiss/',
} = {}) {
  const fetchCalls = [];
  const listeners = {};
  const item = {
    dataset: { adviceKey: 'focus', expiresAt: '2026-03-15T12:00:00' },
    remove: () => {},
  };
  const button = {
    closest: (selector) => {
      if (selector === '.dismiss-advice') return button;
      if (selector === '.advice-item') return item;
      return null;
    },
  };
  const dataEl = { textContent: '{}' };
  const pageEl = { dataset: { dismissUrl } };

  const document = {
    cookie,
    addEventListener: (type, handler) => {
      listeners[type] = handler;
    },
    createElement: () => ({ style: {}, appendChild: () => {}, innerHTML: '', className: '' }),
    createElementNS: () => ({
      setAttribute: () => {},
      classList: { add: () => {} },
      dataset: {},
    }),
    querySelector: (selector) => {
      if (selector === '[name="csrfmiddlewaretoken"]' && hiddenCsrfToken) {
        return { value: hiddenCsrfToken };
      }
      if (selector === '.open-profile-modal') {
        return { click: () => {} };
      }
      if (selector === '#profile-modal .bot-block') {
        return { scrollIntoView: () => {} };
      }
      return null;
    },
    // This CSRF fixture has no source bars, counters, tabs or drawer nodes.
    // Let the real renderer initialize against the same empty DOM collections.
    querySelectorAll: () => [],
    getElementById: (id) => {
      if (id === 'mgmt-stats-data') return dataEl;
      if (id === 'stats-page') return pageEl;
      return null;
    },
  };

  const sandbox = {
    document,
    window: { document },
    fetch: async (requestUrl, options = {}) => {
      fetchCalls.push({ requestUrl, options });
      return { ok: true, status: 200 };
    },
    setTimeout: (fn) => {
      fn();
      return 1;
    },
    console,
    JSON,
    Math,
    Map,
    Array,
    Number,
    String,
  };

  vm.runInNewContext(scriptSource, sandbox, { filename: 'management-stats.js' });
  listeners.click?.({ target: button });
  await Promise.resolve();

  return fetchCalls;
}

test('stats dismiss advice uses DOM csrf token when duplicate cookies exist', async () => {
  const domToken = 'A'.repeat(64);
  const cookie = `foo=1; csrftoken=${'b'.repeat(32)}; theme=dark; csrftoken=${'c'.repeat(32)}`;

  const fetchCalls = await runManagementStats({
    cookie,
    hiddenCsrfToken: domToken,
  });

  assert.equal(fetchCalls.length, 1);
  assert.equal(fetchCalls[0].requestUrl, '/stats/advice/dismiss/');
  assert.equal(fetchCalls[0].options.headers['X-CSRFToken'], domToken);
});
