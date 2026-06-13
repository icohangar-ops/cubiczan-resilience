import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  verifyWebhook,
  createWebhookHandler,
  resolveWebhookSecret,
  timingSafeEqual,
} from '../src/index.js';

// A deterministic stand-in for the Forge crypto HMAC: expected = `${secret}:${rawBody}`.
const compute = ({ secret, rawBody }) => `${secret}:${rawBody}`;

test('resolveWebhookSecret fails closed on missing / blank secret', () => {
  for (const bad of [undefined, null, '', '   ', 42]) {
    const r = resolveWebhookSecret(bad);
    assert.equal(r.ok, false);
    assert.equal(r.status, 503);
  }
  const good = resolveWebhookSecret('  s3cret  ');
  assert.deepEqual(good, { ok: true, secret: 's3cret' });
});

test('timingSafeEqual matches equal strings and rejects different ones', () => {
  assert.equal(timingSafeEqual('abc123', 'abc123'), true);
  assert.equal(timingSafeEqual('abc123', 'abc124'), false);
  assert.equal(timingSafeEqual('abc', 'abcd'), false);
  assert.equal(timingSafeEqual('', ''), true);
});

test('verifyWebhook FAILS CLOSED when secret is unset (503)', async () => {
  const r = await verifyWebhook({
    secret: undefined,
    signature: 'anything',
    computeExpected: () => 'anything',
  });
  assert.equal(r.ok, false);
  assert.equal(r.status, 503);
});

test('verifyWebhook rejects a missing signature (401)', async () => {
  const r = await verifyWebhook({
    secret: 'topsecret',
    signature: null,
    computeExpected: (s) => `${s}:body`,
  });
  assert.equal(r.ok, false);
  assert.equal(r.status, 401);
  assert.match(r.reason, /Missing/);
});

test('verifyWebhook rejects an invalid signature (401)', async () => {
  const r = await verifyWebhook({
    secret: 'topsecret',
    signature: 'wrong-sig',
    computeExpected: (s) => `${s}:body`,
  });
  assert.equal(r.ok, false);
  assert.equal(r.status, 401);
  assert.match(r.reason, /Invalid/);
});

test('verifyWebhook PASSES on a valid signature', async () => {
  const secret = 'topsecret';
  const expected = `${secret}:body`;
  const r = await verifyWebhook({
    secret,
    signature: expected,
    computeExpected: (s) => `${s}:body`,
  });
  assert.deepEqual(r, { ok: true });
});

// --- handler factory --------------------------------------------------------

function makeRequest({ method = 'POST', body = {}, signature, secretEnv } = {}) {
  if (secretEnv === undefined) delete process.env.WEBHOOK_SECRET;
  else process.env.WEBHOOK_SECRET = secretEnv;
  const headers = new Map();
  if (signature !== undefined) headers.set('x-webhook-signature', signature);
  return {
    method,
    headers: { get: (k) => headers.get(k) ?? null },
    json: async () => body,
  };
}

test('createWebhookHandler rejects non-POST with 405', async () => {
  const handler = createWebhookHandler({ computeExpected: compute, store: async () => {} });
  const res = await handler(makeRequest({ method: 'GET', secretEnv: 's' }));
  assert.equal(res.status, 405);
});

test('createWebhookHandler returns 400 on invalid JSON', async () => {
  const handler = createWebhookHandler({ computeExpected: compute, store: async () => {} });
  const res = await handler({
    method: 'POST',
    headers: { get: () => 'sig' },
    json: async () => { throw new Error('bad json'); },
  });
  assert.equal(res.status, 400);
  assert.equal(res.body.error, 'Invalid JSON body');
});

test('createWebhookHandler fails closed (503) when WEBHOOK_SECRET unset', async () => {
  let stored = false;
  const handler = createWebhookHandler({
    computeExpected: compute,
    store: async () => { stored = true; },
  });
  const res = await handler(makeRequest({ body: { a: 1 }, signature: 'x' }));
  assert.equal(res.status, 503);
  assert.equal(stored, false, 'must not persist on failed auth');
});

test('createWebhookHandler rejects bad signature (401) and does not store', async () => {
  let stored = false;
  const handler = createWebhookHandler({
    computeExpected: compute,
    store: async () => { stored = true; },
  });
  const res = await handler(
    makeRequest({ body: { a: 1 }, signature: 'nope', secretEnv: 'sek' }),
  );
  assert.equal(res.status, 401);
  assert.equal(stored, false);
});

test('createWebhookHandler runs validate -> store -> 200 on a valid request', async () => {
  const body = { decisionId: 'DC-1', title: 'T' };
  const rawBody = JSON.stringify(body);
  const secret = 'sek';
  const stored = [];

  const handler = createWebhookHandler({
    computeExpected: compute, // `${secret}:${rawBody}`
    validate: (b) => (b.decisionId ? { ok: true } : { ok: false, reason: 'Missing decisionId' }),
    store: async (b) => {
      stored.push(b);
      return { message: `Decision ${b.decisionId} updated` };
    },
  });

  const res = await handler(
    makeRequest({ body, signature: `${secret}:${rawBody}`, secretEnv: secret }),
  );

  assert.equal(res.status, 200);
  assert.equal(res.body.success, true);
  assert.equal(res.body.message, 'Decision DC-1 updated');
  assert.deepEqual(stored, [body]);
});

test('createWebhookHandler returns 400 from validate failure (after auth passes)', async () => {
  const body = { wrong: true };
  const rawBody = JSON.stringify(body);
  const secret = 'sek';
  let stored = false;

  const handler = createWebhookHandler({
    computeExpected: compute,
    validate: (b) => (b.decisionId ? { ok: true } : { ok: false, reason: 'Missing decisionId' }),
    store: async () => { stored = true; },
  });

  const res = await handler(
    makeRequest({ body, signature: `${secret}:${rawBody}`, secretEnv: secret }),
  );
  assert.equal(res.status, 400);
  assert.equal(res.body.error, 'Missing decisionId');
  assert.equal(stored, false);
});

test('createWebhookHandler throws on misconfiguration (missing store/computeExpected)', () => {
  assert.throws(() => createWebhookHandler({ computeExpected: compute }), /store/);
  assert.throws(() => createWebhookHandler({ store: async () => {} }), /computeExpected/);
});
