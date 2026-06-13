import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  createResolver,
  readCache,
  writeCache,
  pick,
} from '../src/index.js';

test('createResolver requires a mock fallback', () => {
  assert.throws(() => createResolver({}), /mock/);
});

test('createResolver uses proxy data when available and persists + decorates', async () => {
  const persisted = [];
  const handler = createResolver({
    fromProxy: async () => ({ value: 'fresh' }),
    fromCache: async () => ({ value: 'cached' }),
    mock: { value: 'mock' },
    persist: async (data) => persisted.push(data),
    decorate: (data, { source }) => ({ ...data, source }),
  });
  const out = await handler({});
  assert.deepEqual(out, { value: 'fresh', source: 'proxy' });
  assert.deepEqual(persisted, [{ value: 'fresh' }]);
});

test('createResolver falls back to cache when proxy returns null', async () => {
  const handler = createResolver({
    fromProxy: async () => null,
    fromCache: async () => ({ value: 'cached' }),
    mock: { value: 'mock' },
    decorate: (data, { source }) => ({ ...data, source }),
  });
  assert.deepEqual(await handler({}), { value: 'cached', source: 'cache' });
});

test('createResolver falls back to cache when proxy THROWS (swallowed)', async () => {
  const handler = createResolver({
    fromProxy: async () => { throw new Error('proxy down'); },
    fromCache: async () => ({ value: 'cached' }),
    mock: { value: 'mock' },
    decorate: (data, { source }) => ({ ...data, source }),
  });
  assert.deepEqual(await handler({}), { value: 'cached', source: 'cache' });
});

test('createResolver falls back to mock when proxy and cache miss', async () => {
  const handler = createResolver({
    fromProxy: async () => null,
    fromCache: async () => null,
    mock: (req) => ({ value: 'mock', key: req.k }),
    decorate: (data, { source }) => ({ ...data, source }),
  });
  assert.deepEqual(await handler({ k: 'X' }), { value: 'mock', key: 'X', source: 'mock' });
});

test('readCache honors TTL and missing/expired entries', async () => {
  const ttlMs = 1000;
  let nowVal = 10_000;
  const now = () => nowVal;

  // fresh
  assert.deepEqual(
    await readCache({ read: async () => ({ data: { a: 1 }, timestamp: 9500 }), ttlMs, now }),
    { a: 1 },
  );
  // expired
  assert.equal(
    await readCache({ read: async () => ({ data: { a: 1 }, timestamp: 8000 }), ttlMs, now }),
    null,
  );
  // missing
  assert.equal(await readCache({ read: async () => null, ttlMs, now }), null);
  // malformed (no timestamp)
  assert.equal(await readCache({ read: async () => ({ data: { a: 1 } }), ttlMs, now }), null);
  // read throws
  assert.equal(
    await readCache({ read: async () => { throw new Error('kvs down'); }, ttlMs, now }),
    null,
  );
});

test('writeCache stamps a timestamp and swallows write errors', async () => {
  const writes = [];
  await writeCache({ write: async (rec) => writes.push(rec), data: { a: 1 }, now: () => 42 });
  assert.deepEqual(writes, [{ data: { a: 1 }, timestamp: 42 }]);

  // must not throw on failure
  await assert.doesNotReject(
    writeCache({ write: async () => { throw new Error('kvs down'); }, data: {} }),
  );
});

test('pick reads nested context with a fallback', () => {
  const req = { context: { projectKey: 'ABC' }, extension: {} };
  assert.equal(pick(req, ['context', 'projectKey'], 'UNKNOWN'), 'ABC');
  assert.equal(pick(req, ['extension', 'decisionId'], 'DC-1'), 'DC-1');
  assert.equal(pick(req, ['missing', 'deep'], 'DEF'), 'DEF');
  assert.equal(pick({}, ['context', 'projectKey'], 'UNKNOWN'), 'UNKNOWN');
});
