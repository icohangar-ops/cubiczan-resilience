import { afterEach, describe, it, expect } from "vitest";
import {
  checkProxyRequest,
  guardProxyRequest,
} from "../src/proxyGuard.js";
import { SlidingWindowRateLimiter } from "../src/rateLimit.js";

const SECRET = "proxy-secret-1";

function reqWith(headersIn: Record<string, string>): Request {
  const headers = new Headers();
  for (const [k, v] of Object.entries(headersIn)) headers.set(k, v);
  return new Request("https://app.example.com/api/proxy", {
    method: "POST",
    headers,
  });
}

const envSecret = process.env.PROXY_API_SECRET;
afterEach(() => {
  if (envSecret === undefined) delete process.env.PROXY_API_SECRET;
  else process.env.PROXY_API_SECRET = envSecret;
});

describe("checkProxyRequest fail-closed", () => {
  it("returns 503 when no secret is configured (never open)", () => {
    const result = checkProxyRequest(reqWith({ "x-proxy-secret": SECRET }), {
      secret: "",
    });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.status).toBe(503);
  });

  it("falls back to PROXY_API_SECRET read at call time", () => {
    process.env.PROXY_API_SECRET = SECRET;
    const result = checkProxyRequest(
      reqWith({ "x-proxy-secret": SECRET }),
      {}, // no explicit secret — env supplies it
    );
    expect(result.ok).toBe(true);
  });

  it("prefers an explicit secret over the env default", () => {
    process.env.PROXY_API_SECRET = "env-secret";
    const result = checkProxyRequest(reqWith({ "x-proxy-secret": "opt-secret" }), {
      secret: "opt-secret",
    });
    expect(result.ok).toBe(true);
  });

  it("returns 401 on a wrong secret", () => {
    const result = checkProxyRequest(reqWith({ "x-proxy-secret": "wrong" }), {
      secret: SECRET,
    });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.status).toBe(401);
  });

  it("returns 401 on a prefix of the expected secret (timing-safe compare)", () => {
    const result = checkProxyRequest(
      reqWith({ "x-proxy-secret": SECRET.slice(0, 8) }),
      { secret: SECRET },
    );
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.status).toBe(401);
  });

  it("returns 401 when the secret header is missing", () => {
    const result = checkProxyRequest(reqWith({}), { secret: SECRET });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.status).toBe(401);
  });

  it("honors a custom secret header name", () => {
    const result = checkProxyRequest(
      reqWith({ "x-caller-secret": SECRET }),
      { secret: SECRET, secretHeader: "x-caller-secret" },
    );
    expect(result.ok).toBe(true);
  });
});

describe("checkProxyRequest client IP", () => {
  it("takes the hop observed by the outermost trusted proxy (default 1)", () => {
    // One trusted proxy appended the client address it saw; any client-
    // supplied entry sits further left and must be ignored.
    const result = checkProxyRequest(
      reqWith({
        "x-proxy-secret": SECRET,
        "x-forwarded-for": "203.0.113.7, 70.41.3.18",
      }),
      { secret: SECRET },
    );
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.clientIp).toBe("70.41.3.18");
  });

  it("ignores spoofed leftmost x-forwarded-for entries", () => {
    // Attacker sends `X-Forwarded-For: evil`; the trusted proxy appends the
    // real client address. The chain is [evil, 1.2.3.4] and the observed
    // hop (rightmost) wins.
    const result = checkProxyRequest(
      reqWith({
        "x-proxy-secret": SECRET,
        "x-forwarded-for": "evil.example, 1.2.3.4",
      }),
      { secret: SECRET },
    );
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.clientIp).toBe("1.2.3.4");
  });

  it("respects trustedProxyCount for multi-proxy chains", () => {
    // client -> proxyA -> proxyB -> service: proxyB appended proxyA's address
    // and proxyA appended the client's real address, so trusting both
    // proxies (2) selects the entry proxyA observed.
    const result = checkProxyRequest(
      reqWith({
        "x-proxy-secret": SECRET,
        "x-forwarded-for": "evil.example, 1.2.3.4, 10.0.0.254",
      }),
      { secret: SECRET, trustedProxyCount: 2 },
    );
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.clientIp).toBe("1.2.3.4");
  });

  it("never trusts x-forwarded-for when trustedProxyCount is 0", () => {
    const result = checkProxyRequest(
      reqWith({
        "x-proxy-secret": SECRET,
        "x-forwarded-for": "1.2.3.4",
      }),
      { secret: SECRET, trustedProxyCount: 0 },
    );
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.clientIp).toBe("unknown");
  });

  it("still resolves x-real-ip when trustedProxyCount is 0 (non-hop-list header)", () => {
    const result = checkProxyRequest(
      reqWith({ "x-proxy-secret": SECRET, "x-real-ip": "198.51.100.9" }),
      { secret: SECRET, trustedProxyCount: 0 },
    );
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.clientIp).toBe("198.51.100.9");
  });

  it("skips a spoofed x-forwarded-for and falls through to x-real-ip at count 0", () => {
    const result = checkProxyRequest(
      reqWith({
        "x-proxy-secret": SECRET,
        "x-forwarded-for": "6.6.6.6",
        "x-real-ip": "198.51.100.9",
      }),
      { secret: SECRET, trustedProxyCount: 0 },
    );
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.clientIp).toBe("198.51.100.9");
  });

  it("falls back to x-real-ip", () => {
    const result = checkProxyRequest(
      reqWith({ "x-proxy-secret": SECRET, "x-real-ip": "198.51.100.9" }),
      { secret: SECRET },
    );
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.clientIp).toBe("198.51.100.9");
  });

  it("reports unknown when no IP headers exist", () => {
    const result = checkProxyRequest(reqWith({ "x-proxy-secret": SECRET }), {
      secret: SECRET,
    });
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.clientIp).toBe("unknown");
  });
});

describe("checkProxyRequest rate limiting", () => {
  it("trips 429 after the limit within the window, with retryAfterMs", () => {
    let now = 1_000_000;
    const limiter = new SlidingWindowRateLimiter({
      limit: 2,
      windowMs: 60_000,
      now: () => now,
    });
    const req = () => reqWith({ "x-proxy-secret": SECRET, "x-real-ip": "10.0.0.1" });
    const opts = { secret: SECRET, limiter };

    expect(checkProxyRequest(req(), opts).ok).toBe(true);
    expect(checkProxyRequest(req(), opts).ok).toBe(true);

    const third = checkProxyRequest(req(), opts);
    expect(third.ok).toBe(false);
    if (!third.ok) {
      expect(third.status).toBe(429);
      expect(third.retryAfterMs).toBeGreaterThan(0);
    }

    // A different IP is not penalized for the exhausted one.
    const other = checkProxyRequest(
      reqWith({ "x-proxy-secret": SECRET, "x-real-ip": "10.0.0.2" }),
      opts,
    );
    expect(other.ok).toBe(true);

    now += 60_001; // window slides
    expect(checkProxyRequest(req(), opts).ok).toBe(true);
  });

  it("shares one limiter across repeated calls with the same options object", () => {
    const opts = {
      secret: SECRET,
      rateLimit: { limit: 1, windowMs: 60_000 },
    };
    expect(
      checkProxyRequest(reqWith({ "x-proxy-secret": SECRET }), opts).ok,
    ).toBe(true);
    const second = checkProxyRequest(reqWith({ "x-proxy-secret": SECRET }), opts);
    expect(second.ok).toBe(false);
    if (!second.ok) expect(second.status).toBe(429);
  });

  it("skips rate limiting when no limiter is configured", () => {
    for (let i = 0; i < 50; i++) {
      const result = checkProxyRequest(reqWith({ "x-proxy-secret": SECRET }), {
        secret: SECRET,
      });
      expect(result.ok).toBe(true);
    }
  });
});

describe("guardProxyRequest response ergonomics", () => {
  it("returns null when authorized", () => {
    const denied = guardProxyRequest(reqWith({ "x-proxy-secret": SECRET }), {
      secret: SECRET,
    });
    expect(denied).toBeNull();
  });

  it("returns a JSON 503 response when misconfigured", async () => {
    const denied = guardProxyRequest(reqWith({ "x-proxy-secret": SECRET }), {
      secret: "",
    });
    expect(denied).not.toBeNull();
    expect(denied?.status).toBe(503);
    expect(await denied?.json()).toEqual({
      error: "Server misconfigured: proxy secret is not set",
    });
  });

  it("sets retry-after (seconds) on a 429 response", async () => {
    let now = 5_000_000;
    const limiter = new SlidingWindowRateLimiter({
      limit: 1,
      windowMs: 30_000,
      now: () => now,
    });
    const req = () => reqWith({ "x-proxy-secret": SECRET });
    const opts = { secret: SECRET, limiter };
    expect(guardProxyRequest(req(), opts)).toBeNull();

    now += 20_000; // 10s of window left
    const denied = guardProxyRequest(req(), opts);
    expect(denied?.status).toBe(429);
    expect(denied?.headers.get("retry-after")).toBe("10");
    expect(await denied?.json()).toEqual({ error: "Too Many Requests" });
  });
});
