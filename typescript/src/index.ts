export {
  ResilienceError,
  isResilienceError,
  type ResilienceErrorKind,
  type ResilienceErrorOptions,
} from "./errors.js";

export { withTimeout } from "./timeout.js";

export { retry, computeBackoff, type RetryOptions } from "./retry.js";

export {
  safeFetch,
  type SafeFetchOptions,
  type AllowlistHook,
} from "./safeFetch.js";

export {
  SlidingWindowRateLimiter,
  type RateLimitOptions,
  type RateLimitResult,
} from "./rateLimit.js";

export {
  requireAuth,
  requireAuthResponse,
  type AuthResult,
  type RequireAuthOptions,
} from "./auth.js";

export {
  checkProxyRequest,
  guardProxyRequest,
  type ProxyGuardResult,
  type GuardProxyOptions,
} from "./proxyGuard.js";

export {
  validateBoundary,
  tryValidateBoundary,
  type SafeParser,
} from "./validate.js";

export {
  AuditLedger,
  verifyLedger,
  canonicalJson,
  DEFAULT_AUDIT_LEDGER_KEY,
  AUDIT_LEDGER_KEY_ENV,
  type AuditRecord,
  type AuditRecordInput,
  type AuditLedgerOptions,
  type VerifyResult,
} from "./auditLedger.js";

export {
  resolveTiered,
  AllTiersFailedError,
  type SourceTier,
  type TieredResult,
  type TierFailure,
  type CachedValue,
  type TieredSourceOptions,
} from "./tieredSource.js";
