import { describe, it, expect, vi } from "vitest";
import { retry, computeBackoff } from "../src/retry.js";
import { ResilienceError } from "../src/errors.js";

describe("computeBackoff", () => {
  it("grows exponentially and is bounded by maxDelayMs", () => {
    // random()=1 (just under) would exceed cap; force random=1-eps via 0.999...
    const r = () => 0.9999999;
    expect(computeBackoff(1, 250, 30_000, r)).toBeLessThan(250);
    expect(computeBackoff(2, 250, 30_000, r)).toBeLessThan(500);
    expect(computeBackoff(3, 250, 30_000, r)).toBeLessThan(1_000);
    // capped
    expect(computeBackoff(20, 250, 1_000, r)).toBeLessThan(1_000);
  });

  it("applies full jitter (0 when random()=0)", () => {
    expect(computeBackoff(5, 250, 30_000, () => 0)).toBe(0);
  });
});

describe("retry", () => {
  it("retries with backoff and eventually succeeds", async () => {
    const sleep = vi.fn(async () => {});
    let calls = 0;
    const result = await retry(
      async () => {
        calls++;
        if (calls < 3) throw new Error("transient");
        return "ok";
      },
      { maxAttempts: 3, baseDelayMs: 10, sleep, random: () => 0.5 },
    );
    expect(result).toBe("ok");
    expect(calls).toBe(3);
    expect(sleep).toHaveBeenCalledTimes(2); // backoff between the 3 attempts
  });

  it("stops immediately when shouldRetry returns false (fail fast)", async () => {
    const sleep = vi.fn(async () => {});
    let calls = 0;
    await expect(
      retry(
        async () => {
          calls++;
          throw new Error("fatal");
        },
        { maxAttempts: 5, sleep, shouldRetry: () => false },
      ),
    ).rejects.toBeInstanceOf(ResilienceError);
    expect(calls).toBe(1);
    expect(sleep).not.toHaveBeenCalled();
  });

  it("throws exhausted after maxAttempts and preserves cause", async () => {
    const sleep = vi.fn(async () => {});
    const cause = new Error("still broken");
    try {
      await retry(
        async () => {
          throw cause;
        },
        { maxAttempts: 2, sleep },
      );
      throw new Error("should have thrown");
    } catch (err) {
      expect(err).toBeInstanceOf(ResilienceError);
      expect((err as ResilienceError).kind).toBe("exhausted");
      expect((err as ResilienceError).attempts).toBe(2);
      expect((err as ResilienceError & { cause?: unknown }).cause).toBe(cause);
    }
  });
});
