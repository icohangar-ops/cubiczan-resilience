import { describe, it, expect, vi } from "vitest";
import { safeFetch } from "../src/safeFetch.js";
import { ResilienceError } from "../src/errors.js";

function jsonResponse(status: number, body: unknown = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("safeFetch", () => {
  it("retries on 5xx with backoff and then succeeds", async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(503))
      .mockResolvedValueOnce(jsonResponse(500))
      .mockResolvedValueOnce(jsonResponse(200, { ok: true }));

    const res = await safeFetch("https://api.example.com/x", {
      fetchImpl,
      maxAttempts: 3,
      baseDelayMs: 0, // no real sleeping
      random: () => 0,
    });

    expect(res.status).toBe(200);
    expect(fetchImpl).toHaveBeenCalledTimes(3);
  });

  it("fails fast on 404 (no retry)", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(404));

    const res = await safeFetch("https://api.example.com/missing", {
      fetchImpl,
      maxAttempts: 5,
      baseDelayMs: 0,
    });

    // 404 is a non-retryable client error => returned to caller, called once.
    expect(res.status).toBe(404);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("retries on 429 (rate limited) up to maxAttempts then throws", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(429));

    await expect(
      safeFetch("https://api.example.com/x", {
        fetchImpl,
        maxAttempts: 3,
        baseDelayMs: 0,
        random: () => 0,
      }),
    ).rejects.toMatchObject({ kind: "http", status: 429 });

    expect(fetchImpl).toHaveBeenCalledTimes(3);
  });

  it("rejects non-allowlisted hosts before any fetch (SSRF guard)", async () => {
    const fetchImpl = vi.fn<typeof fetch>();

    await expect(
      safeFetch("https://evil.internal/x", {
        fetchImpl,
        allowlist: ["api.example.com"],
      }),
    ).rejects.toMatchObject({ kind: "ssrf" });

    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("allows allowlisted hosts", async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValue(jsonResponse(200));

    const res = await safeFetch("https://api.example.com/x", {
      fetchImpl,
      allowlist: ["api.example.com"],
    });
    expect(res.status).toBe(200);
  });

  it("retries on network errors then surfaces a typed error", async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockRejectedValue(new TypeError("fetch failed"));

    await expect(
      safeFetch("https://api.example.com/x", {
        fetchImpl,
        maxAttempts: 2,
        baseDelayMs: 0,
        random: () => 0,
      }),
    ).rejects.toBeInstanceOf(ResilienceError);

    expect(fetchImpl).toHaveBeenCalledTimes(2);
  });

  it("times out a slow attempt via AbortController", async () => {
    const fetchImpl = vi.fn<typeof fetch>((_, init) => {
      return new Promise<Response>((resolve, reject) => {
        const signal = (init as RequestInit | undefined)?.signal;
        const t = setTimeout(() => resolve(jsonResponse(200)), 1_000);
        signal?.addEventListener("abort", () => {
          clearTimeout(t);
          reject(signal.reason ?? new Error("aborted"));
        });
      });
    });

    await expect(
      safeFetch("https://api.example.com/slow", {
        fetchImpl,
        timeoutMs: 20,
        maxAttempts: 1,
        baseDelayMs: 0,
      }),
    ).rejects.toMatchObject({ kind: "timeout" });
  });
});
