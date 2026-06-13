import { describe, it, expect } from "vitest";
import { withTimeout } from "../src/timeout.js";
import { ResilienceError } from "../src/errors.js";

describe("withTimeout", () => {
  it("fires a timeout error when the promise is too slow", async () => {
    const slow = new Promise((resolve) => setTimeout(resolve, 1_000));
    await expect(withTimeout(slow, 20, "slow-op")).rejects.toMatchObject({
      name: "ResilienceError",
      kind: "timeout",
    });
  });

  it("resolves when the promise beats the deadline", async () => {
    const fast = Promise.resolve(42);
    await expect(withTimeout(fast, 1_000)).resolves.toBe(42);
  });

  it("propagates the original rejection (not a timeout)", async () => {
    const boom = Promise.reject(new Error("boom"));
    await expect(withTimeout(boom, 1_000)).rejects.toThrow("boom");
  });

  it("includes the label and duration in the message", async () => {
    const slow = new Promise((resolve) => setTimeout(resolve, 1_000));
    try {
      await withTimeout(slow, 15, "db-query");
      throw new Error("should have thrown");
    } catch (err) {
      expect(err).toBeInstanceOf(ResilienceError);
      expect((err as ResilienceError).message).toContain("db-query");
      expect((err as ResilienceError).message).toContain("15ms");
    }
  });
});
