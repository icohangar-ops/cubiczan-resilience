import {
  SlidingWindowRateLimiter,
  type RateLimitOptions,
} from "./rateLimit.js";

/**
 * Outcome of an auth check. `ok: false` carries the HTTP status and a reason
 * the caller can turn into a response.
 */
export type AuthResult =
  | { readonly ok: true; readonly token: string }
  | {
      readonly ok: false;
      readonly status: 401 | 503 | 429;
      readonly reason: string;
      readonly retryAfterMs?: number;
    };

export interface RequireAuthOptions {
  /**
   * The expected bearer token. If `undefined`/empty, the check FAILS CLOSED
   * (503 misconfigured) — it never degrades to allowing the request.
   */
  readonly token: string | undefined;
  /** Optional per-key rate limit applied to authenticated callers. */
  readonly rateLimit?: RateLimitOptions;
  /**
   * Derive the rate-limit key. Defaults to the bearer token; supply this to
   * key by client IP instead (e.g. from an `x-forwarded-for` header).
   */
  readonly keyFor?: (req: Request, token: string) => string;
  /**
   * Share a limiter instance across calls. When omitted but `rateLimit` is
   * set, an internal limiter is created and reused per options object.
   */
  readonly limiter?: SlidingWindowRateLimiter;
}

const limiterRegistry = new WeakMap<object, SlidingWindowRateLimiter>();

function resolveLimiter(
  opts: RequireAuthOptions,
): SlidingWindowRateLimiter | undefined {
  if (opts.limiter) return opts.limiter;
  if (!opts.rateLimit) return undefined;
  let limiter = limiterRegistry.get(opts);
  if (!limiter) {
    limiter = new SlidingWindowRateLimiter(opts.rateLimit);
    limiterRegistry.set(opts, limiter);
  }
  return limiter;
}

function extractBearer(header: string | null): string | undefined {
  const match = (header ?? "").match(/^Bearer\s+(.+)$/i);
  return match?.[1]?.trim() || undefined;
}

/**
 * Fail-closed bearer-token check + optional sliding-window rate limit.
 *
 * Generalizes AgentPay's `require-auth.ts`: when the expected token is unset
 * we return 503 (misconfigured) rather than allowing the request; a mismatch
 * or missing header is 401. This is the generic predicate form — see
 * {@link requireAuthResponse} for the Next.js-style helper.
 */
export function requireAuth(
  req: Request,
  options: RequireAuthOptions,
): AuthResult {
  const expected = options.token;

  // Fail closed: no configured token => refuse, never allow.
  if (!expected) {
    return {
      ok: false,
      status: 503,
      reason: "Server misconfigured: auth token is not set",
    };
  }

  const provided = extractBearer(req.headers.get("authorization"));
  if (!provided || provided !== expected) {
    return { ok: false, status: 401, reason: "Unauthorized" };
  }

  const limiter = resolveLimiter(options);
  if (limiter) {
    const key = options.keyFor ? options.keyFor(req, provided) : provided;
    const result = limiter.check(key);
    if (!result.allowed) {
      return {
        ok: false,
        status: 429,
        reason: "Too Many Requests",
        retryAfterMs: Math.max(0, result.resetAt - Date.now()),
      };
    }
  }

  return { ok: true, token: provided };
}

/**
 * Next.js-style helper. Returns a `Response` to send when the request is
 * rejected, or `null` when the caller is authorized (mirroring AgentPay's
 * `requireAuth(req): NextResponse | null` ergonomics, but framework-agnostic).
 */
export function requireAuthResponse(
  req: Request,
  options: RequireAuthOptions,
): Response | null {
  const result = requireAuth(req, options);
  if (result.ok) return null;

  const headers: Record<string, string> = {
    "content-type": "application/json",
  };
  if (result.status === 429 && result.retryAfterMs !== undefined) {
    headers["retry-after"] = String(Math.ceil(result.retryAfterMs / 1000));
  }
  return new Response(JSON.stringify({ error: result.reason }), {
    status: result.status,
    headers,
  });
}
