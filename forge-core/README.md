# @cubiczan/forge-core

Shared plumbing for Atlassian Forge apps. Extracted from the common scaffold
across three Forge apps (`decision-brief`, `finance-cockpit`, `market-radar`)
that all shipped the same webtrigger + resolver boilerplate.

App-specific resolver functions and payload shapes stay in each app. This
package provides the reusable wiring that was duplicated three times:

- **Fail-closed HMAC webhook verification** (`verifyWebhook`, `createWebhookHandler`)
- **Proxy → cache(TTL) → mock resolver chain** (`createResolver`, `readCache`, `writeCache`, `pick`)
- **Response envelopes** (`ok`, `fail`, `methodNotAllowed`, `invalidJson`)
- **Dependency-free fetch + timeout** (`fetchWithTimeout`)

Zero runtime dependencies. ESM only. Node >= 18.

## Why fail-closed matters

An earlier bug left one app's webtrigger with **no signature check at all**,
silently accepting unauthenticated writes to KVS. Centralizing verification here
makes that class of bug impossible to reintroduce per-app: a missing
`WEBHOOK_SECRET` returns **503** (never "allow"), a missing/invalid signature
returns **401**, and only a valid signature proceeds to validation and storage.

## Install

This package lives in the `cubiczan-resilience` monorepo alongside
`@cubiczan/resilience`. Reference it from a Forge app via a workspace/file
dependency, or vendor `src/` into the app's `src/lib/`.

## Webhook handler

```js
// src/webhook.js (in a Forge app)
import { set } from '@forge/kvs';
import crypto from '@forge/crypto';
import { createWebhookHandler } from '@cubiczan/forge-core';

export const handler = createWebhookHandler({
  // Forge has no node:crypto; supply the HMAC via @forge/crypto.
  computeExpected: ({ secret, rawBody }) =>
    crypto.sha256().update(secret + rawBody).digest().then((h) => h.toHex()),

  // App-specific payload validation (runs only after auth passes).
  validate: (body) =>
    body.decisionId
      ? { ok: true }
      : { ok: false, status: 400, reason: 'Missing required field: decisionId' },

  // App-specific persistence. Returns the success message.
  store: async (body) => {
    const { decisionId, ...caseData } = body;
    await set(`decision:${decisionId}`, {
      data: { lastUpdated: new Date().toISOString(), ...caseData },
      timestamp: Date.now(),
    });
    return { message: `Decision brief ${decisionId} updated` };
  },
});
```

The factory wires in, for free: `405` on non-POST, `400` on invalid JSON, the
fail-closed HMAC check (`503` / `401`), and the `200 { success, message }`
envelope.

### Lower-level: `verifyWebhook`

```js
const auth = await verifyWebhook({
  secret: process.env.WEBHOOK_SECRET,
  signature: request.headers.get('x-webhook-signature'),
  computeExpected: (secret) =>
    crypto.sha256().update(secret + rawBody).digest().then((h) => h.toHex()),
});
if (!auth.ok) return { status: auth.status, body: { error: auth.reason } };
```

`timingSafeEqual` is used internally to compare signatures without leaking the
expected value via timing.

## Resolver helper

```js
// src/index.js (resolver function in a Forge app)
import { fetch } from '@forge/api';
import { getAll, set } from '@forge/kvs';
import { safeFetch } from '@cubiczan/resilience';
import { createResolver, readCache, writeCache, pick } from '@cubiczan/forge-core';

const CACHE_KEY = 'market-radar-data';
const TTL = 5 * 60 * 1000;

export const handler = createResolver({
  fromProxy: async () => {
    const res = await safeFetch('https://db-proxy.example.com/api/market-radar', {
      fetchImpl: fetch,
      timeoutMs: 8000,
    });
    return res.ok ? res.json() : null;
  },
  fromCache: () => readCache({ read: () => getAll(CACHE_KEY), ttlMs: TTL }),
  mock: MOCK,
  persist: (data) => writeCache({ write: (rec) => set(CACHE_KEY, rec), data }),
  decorate: (data, { source, request }) => ({
    ...data,
    source,
    projectKey: pick(request, ['context', 'projectKey'], 'UNKNOWN'),
  }),
});
```

`createResolver` runs proxy → cache → mock, swallowing proxy/cache errors so a
degraded upstream never blocks the resolver. Keep using `safeFetch` from
`@cubiczan/resilience` for the proxy path (timeout + retry/backoff + SSRF
allowlist). `fetchWithTimeout` is provided here as a dependency-free fallback for
simple cases.

## API

| Export | Purpose |
| --- | --- |
| `createWebhookHandler(cfg)` | Build a webtrigger handler with shared method/JSON/auth/envelope. |
| `verifyWebhook({secret, signature, computeExpected})` | Fail-closed HMAC check → `{ok}` / `{ok:false,status,reason}`. |
| `resolveWebhookSecret(secret)` | Fail-closed secret resolution (503 if unset). |
| `timingSafeEqual(a, b)` | Constant-time string compare. |
| `createResolver(cfg)` | proxy → cache → mock resolver handler. |
| `readCache({read, ttlMs, now?})` | TTL-bounded KVS cache read. |
| `writeCache({write, data, now?})` | Timestamped KVS cache write (errors swallowed). |
| `pick(request, path, fallback)` | Safe nested context read with default. |
| `ok / fail / methodNotAllowed / invalidJson` | Response envelopes. |
| `fetchWithTimeout(url, {fetchImpl, timeoutMs, ...})` | Dependency-free fetch + AbortController timeout. |

## Tests

```sh
npm test    # node:test, zero dependencies
```
