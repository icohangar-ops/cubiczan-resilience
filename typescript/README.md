# @cubiczan/resilience

Dependency-light, ESM-first resilience primitives for TypeScript/Node.

Lifted and generalized from proven patterns in the portfolio:

- timeout-via-`Promise.race`/`AbortController` (agent-conductor)
- fail-closed bearer-token auth (AgentPay `require-auth.ts`)
- Zod-validated request boundaries (swarmfi-preps)

**Zero runtime dependencies.** `zod` is an *optional* peer used only by the
validation helper.

```bash
npm install @cubiczan/resilience
# optional, only if you use validateBoundary():
npm install zod
```

Requires Node 18+ (global `fetch` / `AbortController`). Targets ES2022, ships
ESM + `.d.ts`.

## Exports

| Export | Purpose |
|---|---|
| `safeFetch(url, opts)` | fetch with per-attempt timeout, retry+backoff+jitter on 429/5xx & network errors, fail-fast on 4xx, optional SSRF allowlist |
| `requireAuth(req, opts)` | fail-closed bearer check + sliding-window rate limit (generic predicate) |
| `requireAuthResponse(req, opts)` | Next.js-style helper — returns a `Response` to send, or `null` if authorized |
| `withTimeout(promise, ms)` | bound any promise with a typed timeout |
| `retry(fn, opts)` | exponential backoff + full jitter, composable |
| `SlidingWindowRateLimiter` | in-memory sliding-window limiter |
| `validateBoundary(schema, input)` | validate untrusted input via a Zod-compatible schema |
| `ResilienceError` / `isResilienceError` | typed error with `kind`, `attempts`, `status` |

---

## `safeFetch`

```ts
import { safeFetch, isResilienceError } from "@cubiczan/resilience";

try {
  const res = await safeFetch("https://api.example.com/v1/orders", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ sku: "abc" }),
    timeoutMs: 5_000,     // per attempt (AbortController)
    maxAttempts: 3,       // default 3
    baseDelayMs: 250,     // default 250 (exponential w/ full jitter)
    allowlist: ["api.example.com"], // optional SSRF guard
  });
  const data = await res.json();
} catch (err) {
  if (isResilienceError(err)) {
    // err.kind: "timeout" | "network" | "http" | "ssrf" | "exhausted" | "aborted"
    console.error(err.kind, err.status, err.attempts);
  }
}
```

Behavior:

- **Per-attempt timeout** via a fresh `AbortController` each try (also linked to
  a caller-supplied `signal`).
- **Retries** on `408/425/429/500/502/503/504` and network errors, with
  exponential backoff + full jitter.
- **Fail-fast** on other 4xx — a `404`/`400` is returned to you as a `Response`,
  not retried.
- **SSRF allowlist** runs once before any I/O. Pass an array of hostnames or a
  `(url: URL) => boolean` hook; non-allowlisted hosts throw `kind: "ssrf"`.
- On exhausting retries it throws a typed `ResilienceError` (the last retryable
  status is preserved on `.status`).

---

## `requireAuth` / `requireAuthResponse`

Fail-closed: if the expected token is **unset**, the request is **refused**
(503) — it never degrades to open. A missing/mismatched token is `401`.

Generic predicate:

```ts
import { requireAuth, SlidingWindowRateLimiter } from "@cubiczan/resilience";

const limiter = new SlidingWindowRateLimiter({ limit: 60, windowMs: 60_000 });

export async function handler(req: Request) {
  const auth = requireAuth(req, {
    token: process.env.API_TOKEN, // undefined => 503, not allowed
    limiter,
    keyFor: (req) => req.headers.get("x-forwarded-for") ?? "anon",
  });
  if (!auth.ok) {
    return new Response(JSON.stringify({ error: auth.reason }), {
      status: auth.status, // 401 | 503 | 429
    });
  }
  // ...authorized; auth.token available
}
```

Next.js-style helper (mirrors AgentPay's `requireAuth(req): Response | null`):

```ts
import { requireAuthResponse } from "@cubiczan/resilience";

export async function POST(req: Request) {
  const denied = requireAuthResponse(req, {
    token: process.env.API_TOKEN,
    rateLimit: { limit: 10, windowMs: 60_000 }, // limiter auto-created & reused
  });
  if (denied) return denied; // 401/503/429 with JSON body (+ retry-after on 429)

  // ...do the money-moving work
  return Response.json({ ok: true });
}
```

---

## `withTimeout` & `retry` (composable primitives)

```ts
import { withTimeout, retry } from "@cubiczan/resilience";

// Bound any promise:
const rows = await withTimeout(db.query("SELECT ..."), 2_000, "db-query");

// Retry any async fn with backoff + jitter, fail-fast on non-retryable errors:
const result = await retry(
  async (attempt) => callFlakyApi(),
  {
    maxAttempts: 4,
    baseDelayMs: 200,
    shouldRetry: (err) => !(err instanceof FatalError),
    onRetry: ({ attempt, delayMs }) => console.warn(`retry ${attempt} in ${delayMs}ms`),
  },
);
```

---

## `validateBoundary` (optional, needs `zod`)

```ts
import { z } from "zod";
import { validateBoundary } from "@cubiczan/resilience";

const Payment = z.object({ amount: z.number().positive(), to: z.string() });

// Throws a ResilienceError (status 400) on invalid input:
const payment = validateBoundary(Payment, await req.json(), "payment");
```

Any object exposing a Zod-style `safeParse` works — no hard dependency on `zod`.

---

## Scripts

```bash
npm run build      # tsc -> dist/ (ESM + .d.ts)
npm run typecheck  # tsc --noEmit
npm test           # vitest run
```

## License

MIT
