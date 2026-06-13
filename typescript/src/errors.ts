/**
 * Discriminated reasons a resilient operation can ultimately fail.
 *
 * - `timeout`     — an attempt exceeded its allotted time budget.
 * - `network`     — the underlying transport (e.g. fetch) threw / connection failed.
 * - `http`        — the server returned a non-OK status that we treat as a failure.
 * - `ssrf`        — the target host was rejected by the SSRF allowlist hook.
 * - `exhausted`   — all retry attempts were used up.
 * - `aborted`     — the caller's own AbortSignal aborted the operation.
 */
export type ResilienceErrorKind =
  | "timeout"
  | "network"
  | "http"
  | "ssrf"
  | "exhausted"
  | "aborted";

export interface ResilienceErrorOptions {
  /** Number of attempts made before giving up (1-based). */
  readonly attempts?: number;
  /** HTTP status code, when the failure originated from an HTTP response. */
  readonly status?: number;
  /** Underlying error that triggered this failure, if any. */
  readonly cause?: unknown;
}

/**
 * Typed error thrown by the resilience primitives. Carries a machine-readable
 * `kind`, the number of attempts made, and (for HTTP failures) the status code.
 */
export class ResilienceError extends Error {
  readonly kind: ResilienceErrorKind;
  readonly attempts: number;
  readonly status: number | undefined;

  constructor(
    kind: ResilienceErrorKind,
    message: string,
    options: ResilienceErrorOptions = {},
  ) {
    super(message);
    this.name = "ResilienceError";
    this.kind = kind;
    this.attempts = options.attempts ?? 1;
    this.status = options.status;
    if (options.cause !== undefined) {
      // Preserve the cause chain without requiring lib.es2022.error in older targets.
      (this as { cause?: unknown }).cause = options.cause;
    }
    Object.setPrototypeOf(this, ResilienceError.prototype);
  }
}

/** Type guard for {@link ResilienceError}. */
export function isResilienceError(value: unknown): value is ResilienceError {
  return value instanceof ResilienceError;
}
