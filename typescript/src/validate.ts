import { ResilienceError } from "./errors.js";

/**
 * Minimal structural type for a Zod-like schema. We avoid a hard dependency on
 * `zod` (it is an optional peer) and accept anything with a `safeParse` that
 * returns the standard discriminated result. Generalized from swarmfi-preps'
 * Zod-validated request boundaries.
 */
export interface SafeParser<T> {
  safeParse(input: unknown):
    | { success: true; data: T }
    | { success: false; error: unknown };
}

/**
 * Validate untrusted input at a boundary using a Zod-compatible schema.
 *
 * On success returns the parsed value; on failure throws a typed
 * {@link ResilienceError} (`kind: "network"` is not appropriate here, so we
 * surface it as a generic validation failure carrying the Zod error as cause).
 */
export function validateBoundary<T>(
  schema: SafeParser<T>,
  input: unknown,
  label = "input",
): T {
  const result = schema.safeParse(input);
  if (result.success) return result.data;
  throw new ResilienceError("http", `invalid ${label}`, {
    status: 400,
    cause: result.error,
  });
}

/** Non-throwing variant returning a discriminated result. */
export function tryValidateBoundary<T>(
  schema: SafeParser<T>,
  input: unknown,
): { ok: true; data: T } | { ok: false; error: unknown } {
  const result = schema.safeParse(input);
  return result.success
    ? { ok: true, data: result.data }
    : { ok: false, error: result.error };
}
