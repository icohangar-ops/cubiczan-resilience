export interface RateLimitOptions {
  /** Max requests permitted per key within the window. */
  readonly limit: number;
  /** Sliding window size in milliseconds. */
  readonly windowMs: number;
  /** Override the clock (used by tests). Default Date.now. */
  readonly now?: () => number;
}

export interface RateLimitResult {
  /** Whether this request is permitted. */
  readonly allowed: boolean;
  /** Requests remaining in the current window after this one. */
  readonly remaining: number;
  /** Unix-ms timestamp when the window resets (oldest hit expires). */
  readonly resetAt: number;
}

/**
 * In-memory sliding-window rate limiter.
 *
 * Tracks per-key request timestamps and drops any older than `windowMs` on
 * each check, giving a true sliding window (not a fixed bucket that resets on
 * a boundary). Suitable for single-process gating; back it with a shared store
 * for multi-instance deployments.
 */
export class SlidingWindowRateLimiter {
  private readonly limit: number;
  private readonly windowMs: number;
  private readonly now: () => number;
  private readonly hits = new Map<string, number[]>();

  constructor(options: RateLimitOptions) {
    if (options.limit < 1) throw new Error("rate limit must be >= 1");
    if (options.windowMs <= 0) throw new Error("windowMs must be > 0");
    this.limit = options.limit;
    this.windowMs = options.windowMs;
    this.now = options.now ?? Date.now;
  }

  /** Record an attempt for `key` and report whether it is allowed. */
  check(key: string): RateLimitResult {
    const ts = this.now();
    const cutoff = ts - this.windowMs;
    const recent = (this.hits.get(key) ?? []).filter((t) => t > cutoff);

    if (recent.length >= this.limit) {
      this.hits.set(key, recent);
      const oldest = recent[0] ?? ts;
      return { allowed: false, remaining: 0, resetAt: oldest + this.windowMs };
    }

    recent.push(ts);
    this.hits.set(key, recent);
    return {
      allowed: true,
      remaining: this.limit - recent.length,
      resetAt: (recent[0] ?? ts) + this.windowMs,
    };
  }

  /** Forget all recorded hits for a key (e.g. after a successful login). */
  reset(key: string): void {
    this.hits.delete(key);
  }

  /** Drop expired entries across all keys to bound memory growth. */
  sweep(): void {
    const cutoff = this.now() - this.windowMs;
    for (const [key, times] of this.hits) {
      const recent = times.filter((t) => t > cutoff);
      if (recent.length === 0) this.hits.delete(key);
      else this.hits.set(key, recent);
    }
  }
}
