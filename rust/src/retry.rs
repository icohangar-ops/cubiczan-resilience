//! Async retry with exponential backoff + full jitter.
//!
//! Generalized from the CockroachDB serializable-retry helper in
//! `cross-harness-scaffolder` (`storage/cockroach.py`), which hard-coded a
//! delay schedule and notably had **no jitter** — a classic thundering-herd
//! risk under contention. This implementation adds full jitter (AWS
//! "Exponential Backoff And Jitter" strategy) and a capped backoff.

use crate::error::ResilienceError;
use rand::Rng;
use std::future::Future;
use std::time::Duration;

/// Policy controlling retry attempts and backoff timing.
#[derive(Debug, Clone)]
pub struct RetryPolicy {
    /// Maximum number of attempts (the initial try counts as attempt 1).
    pub max_attempts: u32,
    /// Base delay used for the exponential schedule.
    pub base_delay: Duration,
    /// Upper bound on any single backoff delay (cap). Prevents unbounded
    /// growth on high attempt counts.
    pub max_delay: Duration,
}

impl Default for RetryPolicy {
    fn default() -> Self {
        Self {
            max_attempts: 4,
            base_delay: Duration::from_millis(50),
            max_delay: Duration::from_secs(2),
        }
    }
}

impl RetryPolicy {
    /// Construct a policy with the given attempt budget, keeping default
    /// timing.
    pub fn with_max_attempts(max_attempts: u32) -> Self {
        Self {
            max_attempts,
            ..Self::default()
        }
    }

    /// Builder-style override of the base delay.
    pub fn base_delay(mut self, d: Duration) -> Self {
        self.base_delay = d;
        self
    }

    /// Builder-style override of the cap.
    pub fn max_delay(mut self, d: Duration) -> Self {
        self.max_delay = d;
        self
    }

    /// Compute the *capped* exponential ceiling for a zero-based attempt index,
    /// before jitter is applied: `min(base * 2^attempt, max_delay)`.
    ///
    /// Saturating arithmetic guards against overflow on large attempt counts.
    pub(crate) fn capped_ceiling(&self, attempt: u32) -> Duration {
        let factor = 1u64.checked_shl(attempt).unwrap_or(u64::MAX);
        let base_ms = self.base_delay.as_millis() as u64;
        let raw_ms = base_ms.saturating_mul(factor);
        let capped_ms = raw_ms.min(self.max_delay.as_millis() as u64);
        Duration::from_millis(capped_ms)
    }

    /// Full-jitter delay for the given attempt: a uniform random value in
    /// `[0, capped_ceiling(attempt)]`.
    pub(crate) fn jittered_delay(&self, attempt: u32) -> Duration {
        let ceiling_ms = self.capped_ceiling(attempt).as_millis() as u64;
        if ceiling_ms == 0 {
            return Duration::ZERO;
        }
        let jittered = rand::thread_rng().gen_range(0..=ceiling_ms);
        Duration::from_millis(jittered)
    }
}

/// Retry an async operation according to `policy`.
///
/// - `op` is a closure returning a fresh `Future` on each attempt (it is called
///   again per retry, so it must be re-runnable).
/// - `classify` decides, for a given error, whether it is retryable (`true`) or
///   terminal (`false`). A terminal error short-circuits immediately as
///   [`ResilienceError::Terminal`]. Exhausting the attempt budget yields
///   [`ResilienceError::Exhausted`].
///
/// Backoff between attempts uses exponential growth with **full jitter**,
/// capped at `policy.max_delay`.
///
/// # Example
/// ```
/// # use resilient_call::{retry, RetryPolicy};
/// # use std::sync::atomic::{AtomicU32, Ordering};
/// # tokio_test_block(async {
/// let attempts = AtomicU32::new(0);
/// let result: Result<u32, _> = retry(
///     || async {
///         let n = attempts.fetch_add(1, Ordering::SeqCst);
///         if n < 2 { Err("transient") } else { Ok(42) }
///     },
///     &RetryPolicy::with_max_attempts(5),
///     |_e: &&str| true, // everything is retryable
/// ).await;
/// assert_eq!(result.unwrap(), 42);
/// # });
/// # fn tokio_test_block<F: std::future::Future>(f: F) -> F::Output {
/// #     tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap().block_on(f)
/// # }
/// ```
pub async fn retry<T, E, Op, Fut, C>(
    mut op: Op,
    policy: &RetryPolicy,
    classify: C,
) -> Result<T, ResilienceError<E>>
where
    Op: FnMut() -> Fut,
    Fut: Future<Output = Result<T, E>>,
    C: Fn(&E) -> bool,
{
    let max = policy.max_attempts.max(1);
    let mut attempt: u32 = 0;
    loop {
        match op().await {
            Ok(v) => return Ok(v),
            Err(e) => {
                if !classify(&e) {
                    return Err(ResilienceError::Terminal(e));
                }
                attempt += 1;
                if attempt >= max {
                    return Err(ResilienceError::Exhausted {
                        attempts: attempt,
                        source: e,
                    });
                }
                // Backoff index is zero-based on the just-finished attempt.
                let delay = policy.jittered_delay(attempt - 1);
                if !delay.is_zero() {
                    tokio::time::sleep(delay).await;
                }
            }
        }
    }
}
