# cubiczan-resilience

Shared resilience primitives for the portfolio — built once, adopted everywhere.
Closes the two most common defects found in the architecture audit: external calls
with no timeout/retry/backoff, and money/state operations with no idempotency.

| Package | Language | Provides |
|---|---|---|
| `typescript/` | TypeScript | `safeFetch()` (timeout + retry/backoff + SSRF allowlist), `requireAuth()` (fail-closed bearer + in-memory rate limit) |
| `python/`     | Python (pip: `cubiczan-resilience`) | `@resilient` (timeout + backoff-with-jitter + circuit breaker), idempotency key store, atomic file write, FastAPI auth dependency + CORS allowlist factory |
| `rust/`       | Rust crate `resilient-call` | timeout, backoff+jitter, CockroachDB serializable-retry (SQLSTATE 40001), idempotency ledger check |
| `onchain/`    | TS + Python | bounded retry + gas/fee bump, nonce management, tx-success assertion, off-chain circuit breaker |

Lifted and generalized from: `cfo-resilience-matrix`, `strata-aws-native`, `hermes-pi-factory-guardian`, `cross-harness-scaffolder`, `valiron-advisory-ai`, `agent-conductor`, `critmin-oracle`.
