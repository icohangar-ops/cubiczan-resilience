//! `resilient-call` — small, dependency-light resilience primitives for the
//! cubiczan portfolio.
//!
//! Four building blocks that close the two most common defects found in the
//! architecture audit — external calls with no timeout/retry/backoff, and
//! money/state operations with no idempotency:
//!
//! - [`retry`] — async generic retry with exponential backoff + **full jitter**
//!   and a caller-supplied classifier (retryable vs terminal).
//! - [`with_timeout`] — tokio timeout wrapper returning a typed
//!   [`ResilienceError::Timeout`].
//! - [`crdb_retry`] — CockroachDB serializable retry that retries **only** on
//!   SQLSTATE `40001`, with capped backoff + jitter.
//! - [`IdempotencyLedger`] + [`FileLedger`] — a JSONL-backed guard so retrying
//!   callers never double-execute a money/state operation.
//! - [`AuditLedger`] — a signed, append-only JSONL audit ledger. Each record is
//!   HMAC-SHA256 signed over its canonical JSON **plus the previous record's
//!   signature**, chaining lines together so the ledger is tamper-evident.
//!
//! These were lifted and generalized from proven patterns in
//! `cross-harness-scaffolder` (CRDB retry), `swarmfi-executor` / `cleanmandate`
//! (idempotency ledger), and the HMAC audit ledgers in `cleanmandate`,
//! `swarmfi-executor`, `glacier-edge-arm`, and `compliance-as-code-agent`.

mod audit;
mod crdb;
mod error;
mod ledger;
mod retry;
mod timeout;

pub use audit::{
    verify_ledger, verify_ledger_under, AuditError, AuditLedger, AuditRecord,
    AuditRecordInput, VerifyResult, AUDIT_LEDGER_KEY_ENV, DEFAULT_AUDIT_LEDGER_KEY,
};
pub use crdb::{
    crdb_retry, crdb_retry_with_policy, default_crdb_policy, SqlError, SERIALIZATION_FAILURE,
};
pub use error::ResilienceError;
pub use ledger::{FileLedger, IdempotencyLedger, LedgerError};
pub use retry::{retry, RetryPolicy};
pub use timeout::with_timeout;
