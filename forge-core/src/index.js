/**
 * @cubiczan/forge-core — shared plumbing for Atlassian Forge apps.
 *
 * Extracted from the common scaffold across decision-brief, finance-cockpit,
 * and market-radar. App-specific resolver functions and payload shapes stay in
 * each app; this package provides the reusable wiring:
 *
 *   - verifyWebhook / createWebhookHandler : fail-closed HMAC webtrigger auth
 *   - createResolver / readCache / writeCache / pick : proxy->cache->mock chain
 *   - ok / fail / methodNotAllowed / invalidJson : response envelopes
 *   - fetchWithTimeout : dependency-free fetch + AbortController timeout
 */

export {
  verifyWebhook,
  createWebhookHandler,
  resolveWebhookSecret,
  timingSafeEqual,
} from './webhook.js';

export {
  createResolver,
  readCache,
  writeCache,
  pick,
} from './resolver.js';

export { ok, fail, methodNotAllowed, invalidJson } from './response.js';

export { fetchWithTimeout } from './fetch.js';
