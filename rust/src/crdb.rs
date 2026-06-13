//! CockroachDB serializable-transaction retry.
//!
//! Lifted and fixed from `cross-harness-scaffolder`
//! (`storage/cockroach.py::run_with_retry`). That implementation matched on the
//! substring `"40001"` / `"restart transaction"` and used a fixed delay
//! schedule with **no jitter** and an attempt-derived (uncapped-feeling)
//! backoff. Here we:
//!
//! 1. Classify strictly on SQLSTATE `40001` (`serialization_failure`) — the
//!    only code CockroachDB asks you to retry the transaction for.
//! 2. Reuse the shared [`retry`](crate::retry) engine, which applies **capped**
//!    exponential backoff with **full jitter**.

use crate::error::ResilienceError;
use crate::retry::{retry, RetryPolicy};
use std::future::Future;

/// SQLSTATE for `serialization_failure` — the retryable transaction conflict.
pub const SERIALIZATION_FAILURE: &str = "40001";

/// Error carrying a SQLSTATE, as classified for CRDB retry purposes.
///
/// Callers map their database driver's error into this so `crdb_retry` can
/// inspect the code. The original `message` is preserved for diagnostics.
#[derive(Debug, Clone, thiserror::Error)]
#[error("sqlstate {sqlstate}: {message}")]
pub struct SqlError {
    /// The five-character SQLSTATE returned by the server.
    pub sqlstate: String,
    /// Human-readable detail.
    pub message: String,
}

impl SqlError {
    /// Convenience constructor.
    pub fn new(sqlstate: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            sqlstate: sqlstate.into(),
            message: message.into(),
        }
    }

    /// True only for SQLSTATE `40001` (serialization failure).
    pub fn is_serialization_failure(&self) -> bool {
        self.sqlstate == SERIALIZATION_FAILURE
    }
}

/// Retry a CockroachDB transaction body, retrying **only** on SQLSTATE `40001`.
///
/// Any other SQLSTATE is treated as terminal and surfaced immediately as
/// [`ResilienceError::Terminal`]. Uses a CRDB-tuned default policy (capped
/// backoff + full jitter). For custom timing use
/// [`crdb_retry_with_policy`].
///
/// `op` must be re-runnable: it is invoked once per attempt and should open,
/// run, and commit the serializable transaction.
pub async fn crdb_retry<T, Op, Fut>(op: Op) -> Result<T, ResilienceError<SqlError>>
where
    Op: FnMut() -> Fut,
    Fut: Future<Output = Result<T, SqlError>>,
{
    crdb_retry_with_policy(op, &default_crdb_policy()).await
}

/// Like [`crdb_retry`] but with a caller-supplied [`RetryPolicy`].
pub async fn crdb_retry_with_policy<T, Op, Fut>(
    op: Op,
    policy: &RetryPolicy,
) -> Result<T, ResilienceError<SqlError>>
where
    Op: FnMut() -> Fut,
    Fut: Future<Output = Result<T, SqlError>>,
{
    retry(op, policy, |e: &SqlError| e.is_serialization_failure()).await
}

/// Default policy for CRDB retries: more attempts than the generic default,
/// with a sensible cap so backoff never runs away under sustained contention.
pub fn default_crdb_policy() -> RetryPolicy {
    RetryPolicy {
        max_attempts: 6,
        base_delay: std::time::Duration::from_millis(50),
        max_delay: std::time::Duration::from_secs(3),
    }
}
