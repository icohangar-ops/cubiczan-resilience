import { ResilienceError } from "./errors.js";

/**
 * Race a promise against a timeout.
 *
 * Generalized from the `Promise.race` timeout pattern used in agent-conductor:
 * the original promise keeps running (JS cannot cancel it), but the caller is
 * released after `ms` with a typed {@link ResilienceError} of kind `"timeout"`.
 *
 * The internal timer is always cleared so a fast-resolving promise does not
 * keep the event loop alive.
 *
 * @param promise the work to bound
 * @param ms      timeout budget in milliseconds (<= 0 disables the timeout)
 * @param label   optional label included in the timeout error message
 */
export function withTimeout<T>(
  promise: PromiseLike<T>,
  ms: number,
  label = "operation",
): Promise<T> {
  if (!Number.isFinite(ms) || ms <= 0) {
    return Promise.resolve(promise);
  }

  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => {
      reject(
        new ResilienceError(
          "timeout",
          `${label} timed out after ${ms}ms`,
          { attempts: 1 },
        ),
      );
    }, ms);

    // Do not let the timer hold the process open in Node.
    if (typeof timer === "object" && timer !== null && "unref" in timer) {
      (timer as { unref: () => void }).unref();
    }

    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (err) => {
        clearTimeout(timer);
        reject(err);
      },
    );
  });
}
