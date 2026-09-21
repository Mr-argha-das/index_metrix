// Run with: node --test tests/frontend_auth.test.js
// Exercise the real frontend with denied browser storage, without npm dependencies.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/js/app.js', 'utf8');

function browser({ bootstrapToken, fetcher } = {}) {
  const listeners = {};
  const calls = [];
  let removed = false;
  let cleaned = '';
  let destination = '';
  const context = {
    URL, FormData, setTimeout,
    location: {
      href: 'https://preview.e2b.app/dashboard?st=once',
      origin: 'https://preview.e2b.app',
      replace(value) { destination = value; },
    },
    history: { replaceState(_a, _b, value) { cleaned = value; } },
    document: {
      cookie: '',
      getElementById(id) {
        if (id === 'session-bootstrap' && bootstrapToken) return {
          textContent: JSON.stringify(bootstrapToken), remove() { removed = true; },
        };
        return null;
      },
      addEventListener(name, fn) { listeners[name] = fn; },
    },
    fetch: async (path, options) => {
      calls.push({ path, options });
      if (fetcher) return fetcher(path, options);
      return { ok: true, headers: { get: () => 'application/json' }, json: async () => ({ st: 'page-grant' }) };
    },
  };
  for (const name of ['localStorage', 'sessionStorage']) {
    Object.defineProperty(context, name, { get() { throw new Error('Storage blocked'); } });
  }
  context.window = context;
  vm.createContext(context);
  vm.runInContext(source + '\nglobalThis.app = App;', context);
  return { app: context.app, calls, listeners, state: () => ({ removed, cleaned, destination }) };
}

test('denied localStorage AND sessionStorage do not lose the login token', async () => {
  const b = browser();
  b.app.setSession('test-session');
  await b.app.gotoWithSession('/dashboard');
  assert.equal(b.calls[0].options.headers.Authorization, 'Bearer test-session');
  assert.equal(b.state().destination, '/dashboard?st=page-grant');
  assert.ok(!b.state().destination.includes('test-session'));
});

test('next page bootstraps bearer API access without storage or cookies', async () => {
  const b = browser({ bootstrapToken: 'continued-session' });
  await b.app.api('/api/dashboard/stats');
  assert.equal(b.calls[0].options.headers.Authorization, 'Bearer continued-session');
  assert.equal(b.state().removed, true);
  assert.equal(b.state().cleaned, '/dashboard');
});

test('theme storage failure does not abort navigation/logout setup', () => {
  const b = browser();
  assert.doesNotThrow(() => b.listeners.DOMContentLoaded());
  assert.equal(typeof b.listeners.click, 'function');
});

test('logout clears in-memory session even with denied storage', () => {
  const b = browser({ bootstrapToken: 'continued-session' });
  b.app.clearSession();
  assert.equal(b.app.sessionToken(), '');
});

test('handoff error is surfaced rather than navigating into a login loop', async () => {
  const b = browser({ fetcher: async () => ({ ok: false, status: 401,
    headers: { get: () => 'application/json' }, json: async () => ({ detail: 'Session expired' }) }) });
  b.app.setSession('expired-session');
  await assert.rejects(b.app.gotoWithSession('/dashboard'), /Session expired/);
  assert.equal(b.state().destination, '');
  assert.equal(b.app.sessionToken(), '');
});

test('external next URLs cannot receive authentication handoffs', async () => {
  const b = browser();
  for (const next of ['//evil.example', '/\\evil.example', 'https://evil.example', 'javascript:alert(1)']) {
    await assert.rejects(b.app.gotoWithSession(next), /Invalid redirect/);
  }
  assert.equal(b.calls.length, 0);
});

test('navigation keeps parameters/hash, replaces old grants, never appends after hash', async () => {
  const b = browser();
  b.app.setSession('test-session');
  await b.app.gotoWithSession('/urls?filter=ready&st=old#result');
  assert.equal(b.state().destination, '/urls?filter=ready&st=page-grant#result');
});

test('protected sidebar navigation sends a handoff with denied storage', async () => {
  const b = browser({ bootstrapToken: 'continued-session' });
  b.listeners.DOMContentLoaded();
  let prevented = false;
  b.listeners.click({ button: 0, target: { closest: () => ({ href: 'https://preview.e2b.app/urls',
    hasAttribute: () => false }) }, preventDefault() { prevented = true; } });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(prevented, true);
  assert.equal(b.state().destination, '/urls?st=page-grant');
});
