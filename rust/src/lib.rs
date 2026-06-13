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
//!
//! These were lifted and generalized from proven patterns in
//! `cross-harness-scaffolder` (CRDB retry), and `swarmfi-executor` /
//! `cleanmandate` (idempotency ledger).

mod crdb;
mod error;
mod ledger;
mod retry;
mod timeout;

pub use crdb::{
    crdb_retry, crdb_retry_with_policy, default_crdb_policy, SqlError, SERIALIZATION_FAILURE,
};
pub use error::ResilienceError;
pub use ledger::{FileLedger, IdempotencyLedger, LedgerError};
pub use retry::{retry, RetryPolicy};
pub use timeout::with_timeout;
