import { createHash, timingSafeEqual } from "node:crypto";
import {
  SlidingWindowRateLimiter,
  type RateLimitOptions,
} from "./rateLimit.js";

/**
 * Outcome of a proxy-guard check. `ok: false` carries the HTTP status and a
 * reason the caller can turn into a response.
 */
export type ProxyGuardResult =
  | { readonly ok: true; readonly clientIp: string }
  | {
      readonly ok: false;
      readonly status: 401 | 503 | 429;
      readonly reason: string;
      readonly retryAfterMs?: number;
    };

export interface GuardProxyOptions {
  /**
   * The expected proxy secret. If `undefined`/empty, the guard FAILS CLOSED
   * (503 misconfigured) — it never degrades to allowing the request.
   * Defaults to the `PROXY_API_SECRET` environment variable, read at call
   * time (not module load) so tests and multi-tenant runtimes can set it
   * dynamically.
   */
  readonly secret?: string;
  /** Header that must carry the expected secret. Default `x-proxy-secret`. */
  readonly secretHeader?: string;
  /**
   * Optional per-client-IP sliding-window rate limit. Without it the guard
   * only authenticates; supply this so anonymous callers cannot exhaust the
   * upstream API quota the proxy fronts.
   */
  readonly rateLimit?: RateLimitOptions;
  /**
   * Share a limiter instance across call sites (e.g. one quota for every
   * proxy route in the process). When omitted but `rateLimit` is set, an
   * internal limiter is created and reused per options object.
   */
  readonly limiter?: SlidingWindowRateLimiter;
  /**
   * Headers to derive the client IP from, in priority order. Defaults to
   * `x-forwarded-for` (hop list) then `x-real-ip`.
   */
  readonly ipHeaders?: readonly string[];
  /**
   * Number of trusted reverse proxies between the internet and this process.
   * Default 1. For hop-list headers (`x-forwarded-for`), each trusted proxy
   * APPENDS the address it saw, so the address observed by the outermost
   * trusted proxy sits `trustedProxyCount` hops from the right; client- or
   * attacker-supplied entries further left are never selected. Set this to
   * your real proxy depth: too low collapses callers into shared buckets
   * (conservative), too high lets spoofed entries back in. With 0, hop-list
   * headers are never trusted and every caller shares the "unknown" bucket.
   */
  readonly trustedProxyCount?: number;
}

const DEFAULT_SECRET_HEADER = "x-proxy-secret";
const DEFAULT_IP_HEADERS = ["x-forwarded-for", "x-real-ip"] as const;

const limiterRegistry = new WeakMap<object, SlidingWindowRateLimiter>();

function resolveLimiter(
  opts: GuardProxyOptions,
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

function defaultSecret(): string | undefined {
  // Read at call time; a static import-time read freezes misconfigurations
  // into the module and cannot be corrected without a restart.
  if (typeof process === "undefined") return undefined;
  return process.env?.PROXY_API_SECRET;
}

function secretsMatch(provided: string, expected: string): boolean {
  // Hash both sides to a fixed-length digest so timingSafeEqual never throws
  // on length mismatch and the comparison leaks no prefix information.
  const providedDigest = createHash("sha256").update(provided).digest();
  const expectedDigest = createHash("sha256").update(expected).digest();
  return timingSafeEqual(providedDigest, expectedDigest);
}

function clientIp(
  req: Request,
  ipHeaders: readonly string[],
  trustedProxyCount: number,
): string {
  for (const header of ipHeaders) {
    const value = req.headers.get(header);
    if (!value) continue;
    if (trustedProxyCount <= 0) break; // hop headers are attacker-controlled
    const hops = value
      .split(",")
      .map((hop) => hop.trim())
      .filter(Boolean);
    // The client address is the hop observed by the outermost trusted
    // proxy: the rightmost `trustedProxyCount` entries' provenance belongs
    // to the trusted proxies, so hops.length - trustedProxyCount is what
    // the outermost one saw. Spoofed entries sit further left and lose.
    const client = hops[hops.length - trustedProxyCount];
    if (client) return client;
  }
  return "unknown";
}

/**
 * Fail-closed proxy-route guard: caller-secret check + optional per-IP
 * sliding-window rate limit.
 *
 * Generalizes the Next.js proxy guard introduced in metacomp-visionx-dashboard
 * (`proxyGuard.ts`): with no configured secret the request is refused with 503
 * (misconfigured) rather than allowed; a missing or wrong secret header is
 * 401; a rate-limited caller is 429 with the window reset time attached.
 *
 * Framework-agnostic — works with any `Request` subclass (including Next.js
 * `NextRequest`). See {@link guardProxyRequest} for the Response-returning
 * helper.
 */
export function checkProxyRequest(
  req: Request,
  options: GuardProxyOptions = {},
): ProxyGuardResult {
  const expected = options.secret ?? defaultSecret();

  // Fail closed: no configured secret => refuse, never allow.
  if (!expected) {
    return {
      ok: false,
      status: 503,
      reason: "Server misconfigured: proxy secret is not set",
    };
  }

  const provided = req.headers.get(options.secretHeader ?? DEFAULT_SECRET_HEADER);
  if (!provided || !secretsMatch(provided, expected)) {
    return { ok: false, status: 401, reason: "Unauthorized" };
  }

  const limiter = resolveLimiter(options);
  const trustedProxyCount = options.trustedProxyCount ?? 1;
  if (limiter) {
    const ip = clientIp(
      req,
      options.ipHeaders ?? DEFAULT_IP_HEADERS,
      trustedProxyCount,
    );
    const result = limiter.check(ip);
    if (!result.allowed) {
      return {
        ok: false,
        status: 429,
        reason: "Too Many Requests",
        // resetAt is on the limiter's clock — measure against it, not Date.now.
        retryAfterMs: Math.max(0, result.resetAt - limiter.nowMs()),
      };
    }
    return { ok: true, clientIp: ip };
  }

  return {
    ok: true,
    clientIp: clientIp(
      req,
      options.ipHeaders ?? DEFAULT_IP_HEADERS,
      trustedProxyCount,
    ),
  };
}

/**
 * Next.js-style helper. Returns a `Response` to send when the request is
 * rejected, or `null` when the caller is authorized and within rate limits.
 */
export function guardProxyRequest(
  req: Request,
  options: GuardProxyOptions = {},
): Response | null {
  const result = checkProxyRequest(req, options);
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
