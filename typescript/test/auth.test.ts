import { describe, it, expect } from "vitest";
import {
  requireAuth,
  requireAuthResponse,
} from "../src/auth.js";
import { SlidingWindowRateLimiter } from "../src/rateLimit.js";

function reqWith(token?: string): Request {
  const headers = new Headers();
  if (token !== undefined) headers.set("authorization", `Bearer ${token}`);
  return new Request("https://app.example.com/pay", {
    method: "POST",
    headers,
  });
}

describe("requireAuth fail-closed", () => {
  it("returns 503 when the expected token is unset (never open)", () => {
    const result = requireAuth(reqWith("anything"), { token: undefined });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.status).toBe(503);
  });

  it("returns 503 when the expected token is empty string", () => {
    const result = requireAuth(reqWith("anything"), { token: "" });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.status).toBe(503);
  });

  it("returns 401 on a mismatched token", () => {
    const result = requireAuth(reqWith("wrong"), { token: "secret" });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.status).toBe(401);
  });

  it("returns 401 when no header is provided", () => {
    const result = requireAuth(reqWith(undefined), { token: "secret" });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.status).toBe(401);
  });

  it("authorizes a matching token", () => {
    const result = requireAuth(reqWith("secret"), { token: "secret" });
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.token).toBe("secret");
  });
});

describe("requireAuth rate limiting", () => {
  it("trips 429 after the limit within the window", () => {
    const limiter = new SlidingWindowRateLimiter({ limit: 2, windowMs: 60_000 });
    const opts = { token: "secret", limiter };

    expect(requireAuth(reqWith("secret"), opts).ok).toBe(true);
    expect(requireAuth(reqWith("secret"), opts).ok).toBe(true);

    const third = requireAuth(reqWith("secret"), opts);
    expect(third.ok).toBe(false);
    if (!third.ok) expect(third.status).toBe(429);
  });

  it("keys by IP when keyFor is supplied", () => {
    const limiter = new SlidingWindowRateLimiter({ limit: 1, windowMs: 60_000 });
    const keyFor = (req: Request): string =>
      req.headers.get("x-forwarded-for") ?? "anon";

    const make = (ip: string): Request =>
      new Request("https://app.example.com/pay", {
        headers: { authorization: "Bearer secret", "x-forwarded-for": ip },
      });

    const opts = { token: "secret", limiter, keyFor };
    expect(requireAuth(make("1.1.1.1"), opts).ok).toBe(true);
    expect(requireAuth(make("2.2.2.2"), opts).ok).toBe(true); // different key
    expect(requireAuth(make("1.1.1.1"), opts).ok).toBe(false); // first key tripped
  });
});

describe("requireAuthResponse (Next.js-style helper)", () => {
  it("returns null when authorized", () => {
    const res = requireAuthResponse(reqWith("secret"), { token: "secret" });
    expect(res).toBeNull();
  });

  it("returns a 401 Response when unauthorized", async () => {
    const res = requireAuthResponse(reqWith("nope"), { token: "secret" });
    expect(res).not.toBeNull();
    expect(res?.status).toBe(401);
    const body = await res?.json();
    expect(body).toEqual({ error: "Unauthorized" });
  });

  it("sets retry-after on a 429 Response", () => {
    const limiter = new SlidingWindowRateLimiter({ limit: 1, windowMs: 60_000 });
    const opts = { token: "secret", limiter };
    requireAuthResponse(reqWith("secret"), opts); // consume the one allowed
    const res = requireAuthResponse(reqWith("secret"), opts);
    expect(res?.status).toBe(429);
    expect(res?.headers.get("retry-after")).toBeTruthy();
  });
});
