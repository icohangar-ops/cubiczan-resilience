import { describe, it, expect } from "vitest";
import { z } from "zod";
import { validateBoundary, tryValidateBoundary } from "../src/validate.js";
import { ResilienceError } from "../src/errors.js";

const Schema = z.object({ amount: z.number().positive(), to: z.string() });

describe("validateBoundary", () => {
  it("returns parsed data on valid input", () => {
    const data = validateBoundary(Schema, { amount: 10, to: "alice" });
    expect(data).toEqual({ amount: 10, to: "alice" });
  });

  it("throws a typed 400 ResilienceError on invalid input", () => {
    try {
      validateBoundary(Schema, { amount: -1, to: 5 }, "payment");
      throw new Error("should have thrown");
    } catch (err) {
      expect(err).toBeInstanceOf(ResilienceError);
      expect((err as ResilienceError).status).toBe(400);
      expect((err as ResilienceError).message).toContain("payment");
    }
  });

  it("tryValidateBoundary returns a discriminated result", () => {
    expect(tryValidateBoundary(Schema, { amount: 1, to: "x" }).ok).toBe(true);
    expect(tryValidateBoundary(Schema, {}).ok).toBe(false);
  });
});
