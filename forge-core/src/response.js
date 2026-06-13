/**
 * Shared response helpers for Forge webtrigger / function handlers.
 *
 * Forge handlers return a plain `{ status, body }` object. All three apps
 * (decision-brief, finance-cockpit, market-radar) hand-rolled the same shapes:
 *   - 200 { success: true, message }
 *   - 4xx/5xx { error }
 *
 * These helpers centralize those shapes so every app emits a consistent
 * envelope and so the webhook factory can build responses uniformly.
 */

/**
 * Build a success response.
 *
 * @param {string} message      human-readable success message
 * @param {object} [extra]      additional fields merged into the body
 * @param {number} [status=200] HTTP status
 * @returns {{ status: number, body: object }}
 */
export function ok(message, extra = {}, status = 200) {
  return { status, body: { success: true, message, ...extra } };
}

/**
 * Build an error response.
 *
 * @param {number} status  HTTP status (e.g. 400, 401, 405, 503)
 * @param {string} error   human-readable error reason
 * @returns {{ status: number, body: { error: string } }}
 */
export function fail(status, error) {
  return { status, body: { error } };
}

/** 405 Method Not Allowed. */
export function methodNotAllowed() {
  return fail(405, 'Method not allowed');
}

/** 400 Invalid JSON body. */
export function invalidJson() {
  return fail(400, 'Invalid JSON body');
}
