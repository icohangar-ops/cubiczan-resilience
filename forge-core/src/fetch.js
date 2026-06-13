/**
 * Minimal fetch-with-timeout for Forge handlers.
 *
 * The source apps use the full `safeFetch` from `@cubiczan/resilience` (timeout
 * + retry/backoff + SSRF allowlist) for their proxy calls, and you should keep
 * using that for the proxy path. This helper is the dependency-free common
 * denominator: a per-call AbortController timeout around an injected fetch impl,
 * for the simple cases where retry/SSRF machinery is overkill.
 *
 * Forge has no global `fetch`, so callers MUST pass `fetchImpl` (e.g.
 * `@forge/api`'s fetch). The returned value is whatever `fetchImpl` resolves to
 * (a fetch-style Response).
 */

/**
 * @param {string | URL} url
 * @param {Object} [options]
 * @param {Function} options.fetchImpl   required in Forge (no global fetch)
 * @param {number} [options.timeoutMs=10000]
 * @param {AbortSignal} [options.signal]  optional caller abort signal
 * @param {...*} [options.requestInit]    forwarded to fetchImpl (method, headers, body, ...)
 * @returns {Promise<*>}
 */
export async function fetchWithTimeout(url, options = {}) {
  const {
    fetchImpl = globalThis.fetch,
    timeoutMs = 10_000,
    signal: callerSignal,
    ...requestInit
  } = options;

  if (typeof fetchImpl !== 'function') {
    throw new TypeError(
      'fetchWithTimeout: global fetch is unavailable; pass options.fetchImpl',
    );
  }

  const controller = new AbortController();
  const onCallerAbort = () => controller.abort(callerSignal?.reason);
  if (callerSignal) {
    if (callerSignal.aborted) controller.abort(callerSignal.reason);
    else callerSignal.addEventListener('abort', onCallerAbort, { once: true });
  }

  const timer = setTimeout(() => {
    controller.abort(new Error(`request timed out after ${timeoutMs}ms`));
  }, timeoutMs);
  if (typeof timer === 'object' && timer !== null && 'unref' in timer) {
    timer.unref();
  }

  try {
    return await fetchImpl(url, { ...requestInit, signal: controller.signal });
  } finally {
    clearTimeout(timer);
    if (callerSignal) callerSignal.removeEventListener('abort', onCallerAbort);
  }
}
