/**
 * Fail-closed HMAC webhook verification + handler factory for Forge webtriggers.
 *
 * All three source apps (decision-brief, finance-cockpit, market-radar) shipped
 * the same webtrigger scaffold:
 *
 *   1. reject non-POST                       -> 405
 *   2. parse JSON body                       -> 400 on failure
 *   3. fail-closed HMAC signature check      -> 503 (no secret) / 401 (bad sig)
 *   4. app-specific field validation         -> 400
 *   5. write to KVS with a timestamp
 *   6. 200 { success: true, message }
 *
 * Steps 1-3 and 6 are identical plumbing; only 4-5 are app-specific. An earlier
 * bug left market-radar's webtrigger (src/webhook-fn/index.js) with NO signature
 * check at all, silently accepting unauthenticated writes. Centralizing step 3
 * here makes that class of bug impossible to reintroduce per-app.
 *
 * `verifyWebhook` is fail-closed by construction:
 *   - secret unset/blank  => { ok: false, status: 503 }  (never "allow")
 *   - signature missing   => { ok: false, status: 401 }
 *   - signature mismatch  => { ok: false, status: 401 }
 *   - valid signature     => { ok: true }
 */

import { fail } from './response.js';

/**
 * Constant-time string comparison to avoid leaking the expected signature via
 * timing. Pure JS so it works in the Forge runtime without node:crypto.
 *
 * @param {string} a
 * @param {string} b
 * @returns {boolean}
 */
export function timingSafeEqual(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string') return false;
  // Compare against a fixed-length view so the loop count does not depend on
  // whether the lengths match. Length mismatch still fails.
  const len = Math.max(a.length, b.length);
  let mismatch = a.length ^ b.length;
  for (let i = 0; i < len; i++) {
    mismatch |= a.charCodeAt(i % a.length || 0) ^ b.charCodeAt(i % b.length || 0);
  }
  return mismatch === 0;
}

/**
 * Resolve a webhook secret, failing closed when it is missing/blank.
 *
 * Unlike `process.env.WEBHOOK_SECRET || ''` (which silently disables auth),
 * a missing secret is treated as a server misconfiguration (503).
 *
 * @param {string|undefined|null} secret
 * @returns {{ ok: true, secret: string } | { ok: false, status: 503, reason: string }}
 */
export function resolveWebhookSecret(secret) {
  const value = typeof secret === 'string' ? secret.trim() : '';
  if (!value) {
    return {
      ok: false,
      status: 503,
      reason: 'Server misconfigured: WEBHOOK_SECRET is not set',
    };
  }
  return { ok: true, secret: value };
}

/**
 * Verify an HMAC-style webhook signature. Fail-closed.
 *
 * The caller supplies `computeExpected(secret) => Promise<string>` (or sync)
 * so this stays agnostic to the digest implementation. In Forge that is
 * typically `(s) => crypto.sha256().update(s + rawBody).digest().then(h => h.toHex())`.
 *
 * @param {Object} params
 * @param {string|undefined|null} params.secret             configured secret
 * @param {string|null|undefined} params.signature          signature header from the caller
 * @param {(secret: string) => string | Promise<string>} params.computeExpected
 * @returns {Promise<{ ok: true } | { ok: false, status: 401|503, reason: string }>}
 */
export async function verifyWebhook({ secret, signature, computeExpected }) {
  const resolved = resolveWebhookSecret(secret);
  if (!resolved.ok) return resolved;

  if (!signature) {
    return { ok: false, status: 401, reason: 'Missing X-Webhook-Signature header' };
  }

  if (typeof computeExpected !== 'function') {
    throw new TypeError('verifyWebhook: computeExpected must be a function');
  }

  const expected = await computeExpected(resolved.secret);
  if (!timingSafeEqual(String(signature), String(expected))) {
    return { ok: false, status: 401, reason: 'Invalid webhook signature' };
  }

  return { ok: true };
}

/**
 * Build a Forge webtrigger handler from app-specific pieces, wiring in the
 * shared method check, JSON parse, fail-closed HMAC verification, and response
 * envelope.
 *
 * @param {Object} cfg
 * @param {() => string|undefined} cfg.getSecret
 *        Returns the configured secret. Defaults to reading
 *        `process.env.WEBHOOK_SECRET` at request time.
 * @param {(args: { secret: string, rawBody: string, body: object }) => string | Promise<string>} cfg.computeExpected
 *        Computes the expected signature. Receives the resolved secret, the raw
 *        stringified body, and the parsed body.
 * @param {(body: object) => ({ ok: true } | { ok: false, status?: number, reason?: string })} [cfg.validate]
 *        App-specific payload validation. Return `{ ok: false }` to reject with
 *        a 400 (or a custom status/reason).
 * @param {(body: object) => Promise<{ message?: string } | void>} cfg.store
 *        App-specific persistence (e.g. write to KVS). May return a `{ message }`
 *        used in the 200 response.
 * @param {string} [cfg.signatureHeader='x-webhook-signature']
 * @param {string} [cfg.successMessage='Webhook processed']
 * @returns {(request: object) => Promise<{ status: number, body: object }>}
 */
export function createWebhookHandler(cfg) {
  const {
    getSecret = () => process.env.WEBHOOK_SECRET,
    computeExpected,
    validate,
    store,
    signatureHeader = 'x-webhook-signature',
    successMessage = 'Webhook processed',
  } = cfg ?? {};

  if (typeof computeExpected !== 'function') {
    throw new TypeError('createWebhookHandler: computeExpected must be a function');
  }
  if (typeof store !== 'function') {
    throw new TypeError('createWebhookHandler: store must be a function');
  }

  return async function handler(request) {
    if (request.method !== 'POST') {
      return fail(405, 'Method not allowed');
    }

    let body;
    let rawBody;
    try {
      body = await request.json();
      rawBody = JSON.stringify(body);
    } catch {
      return fail(400, 'Invalid JSON body');
    }

    const secret = getSecret();
    const signature = request.headers?.get
      ? request.headers.get(signatureHeader)
      : request.headers?.[signatureHeader];

    const auth = await verifyWebhook({
      secret,
      signature,
      computeExpected: (s) => computeExpected({ secret: s, rawBody, body }),
    });
    if (!auth.ok) {
      return fail(auth.status, auth.reason);
    }

    if (typeof validate === 'function') {
      const v = validate(body);
      if (!v.ok) {
        return fail(v.status ?? 400, v.reason ?? 'Invalid payload');
      }
    }

    const result = (await store(body)) || {};
    return {
      status: 200,
      body: { success: true, message: result.message ?? successMessage },
    };
  };
}
