/**
 * Shared resolver plumbing for Forge function/resolver handlers.
 *
 * All three apps' resolvers (decision-brief, finance-cockpit, market-radar) run
 * the same tiered-fallback chain:
 *
 *   1. fetch from the CockroachDB REST proxy (via safeFetch w/ timeout)
 *   2. fall back to the Forge KVS cache, honoring a TTL
 *   3. fall back to baked-in mock data
 *   4. (optionally) write the chosen result back to the cache
 *
 * Each step swallows its own errors and falls through, so a degraded proxy or
 * storage layer never blocks the resolver. `createResolver` captures that chain;
 * the app supplies the proxy fetch, the mock, and the KVS accessors.
 */

/**
 * Read a value from a TTL-bounded KVS cache entry.
 *
 * Source apps store `{ data, timestamp }` under a key and treat the entry as
 * expired once `Date.now() - timestamp > ttlMs`.
 *
 * @param {Object} params
 * @param {() => Promise<{ data: unknown, timestamp: number } | null | undefined>} params.read
 *        Reads the raw cache record (e.g. `() => getAll(key)`).
 * @param {number} params.ttlMs   max age in ms before the entry is considered stale
 * @param {() => number} [params.now=Date.now]
 * @returns {Promise<unknown|null>} the cached data, or null if missing/expired/error
 */
export async function readCache({ read, ttlMs, now = Date.now }) {
  try {
    const cached = await read();
    if (!cached || typeof cached.timestamp !== 'number') return null;
    if (now() - cached.timestamp > ttlMs) return null;
    return cached.data ?? null;
  } catch {
    return null;
  }
}

/**
 * Write a value into the KVS cache with a fresh timestamp. Errors are swallowed
 * (a cache-write failure is non-critical), mirroring the source apps.
 *
 * @param {Object} params
 * @param {(record: { data: unknown, timestamp: number }) => Promise<unknown>} params.write
 *        Persists the record (e.g. `(rec) => set(key, rec)`).
 * @param {unknown} params.data
 * @param {() => number} [params.now=Date.now]
 * @returns {Promise<void>}
 */
export async function writeCache({ write, data, now = Date.now }) {
  try {
    await write({ data, timestamp: now() });
  } catch {
    /* non-critical: still return data to the caller */
  }
}

/**
 * Build a Forge resolver handler that tries proxy -> cache(TTL) -> mock.
 *
 * @param {Object} cfg
 * @param {(request: object) => Promise<unknown|null>} [cfg.fromProxy]
 *        Fetch fresh data. Return null/throw to fall through. (Throws are caught.)
 * @param {(request: object) => Promise<unknown|null>} [cfg.fromCache]
 *        Read cached data (e.g. via `readCache`). Return null to fall through.
 * @param {unknown | ((request: object) => unknown)} cfg.mock
 *        Mock data (or a factory) used when proxy and cache both miss.
 * @param {(data: unknown, request: object) => Promise<void>} [cfg.persist]
 *        Optional write-back of the resolved data into the cache.
 * @param {(data: unknown, meta: { source: 'proxy'|'cache'|'mock', request: object }) => unknown} [cfg.decorate]
 *        Optional final transform (e.g. attach `source`/`projectKey`).
 * @returns {(request: object) => Promise<unknown>}
 */
export function createResolver(cfg) {
  const { fromProxy, fromCache, mock, persist, decorate } = cfg ?? {};
  if (mock === undefined) {
    throw new TypeError('createResolver: a `mock` fallback is required');
  }

  return async function handler(request = {}) {
    let data = null;
    let source = 'mock';

    if (typeof fromProxy === 'function') {
      try {
        data = await fromProxy(request);
      } catch {
        data = null;
      }
      if (data) source = 'proxy';
    }

    if (!data && typeof fromCache === 'function') {
      data = await fromCache(request);
      if (data) source = 'cache';
    }

    if (!data) {
      data = typeof mock === 'function' ? mock(request) : mock;
      source = 'mock';
    }

    if (typeof persist === 'function') {
      await persist(data, request);
    }

    return typeof decorate === 'function'
      ? decorate(data, { source, request })
      : data;
  };
}

/**
 * Safely read a value out of the Forge resolver `request` (context/extension),
 * with a default. Mirrors the `request.context?.projectKey || 'UNKNOWN'` and
 * `request.extension?.decisionId || 'DC-CFO-001'` patterns in the source apps.
 *
 * @param {object} request
 * @param {string[]} path     property path, e.g. ['context', 'projectKey']
 * @param {*} fallback
 * @returns {*}
 */
export function pick(request, path, fallback) {
  let cur = request;
  for (const key of path) {
    if (cur == null) return fallback;
    cur = cur[key];
  }
  return cur == null ? fallback : cur;
}
