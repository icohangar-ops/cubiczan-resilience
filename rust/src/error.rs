//! Typed error surface for the resilience primitives.

use std::time::Duration;
use thiserror::Error;

/// Errors produced by the resilience wrappers themselves.
///
/// `E` is the caller's underlying operation error. It is preserved verbatim so
/// callers can inspect the terminal cause after retries are exhausted or a
/// classifier rejects an error.
#[derive(Debug, Error)]
pub enum ResilienceError<E> {
    /// The operation kept failing with a retryable error until `attempts` was
    /// reached. Carries the last error observed.
    #[error("exhausted {attempts} attempt(s); last error: {source}")]
    Exhausted {
        /// Number of attempts that were made.
        attempts: u32,
        /// The last underlying error observed.
        source: E,
    },

    /// The classifier judged the error terminal (non-retryable); the operation
    /// is not retried and the error is surfaced immediately.
    #[error("terminal (non-retryable) error: {0}")]
    Terminal(E),

    /// A `with_timeout` guard fired before the future resolved.
    #[error("operation timed out after {0:?}")]
    Timeout(Duration),
}

impl<E> ResilienceError<E> {
    /// Extract the underlying operation error, if any. `Timeout` has no
    /// underlying `E` and returns `None`.
    pub fn into_source(self) -> Option<E> {
        match self {
            ResilienceError::Exhausted { source, .. } => Some(source),
            ResilienceError::Terminal(e) => Some(e),
            ResilienceError::Timeout(_) => None,
        }
    }
}
