//! Tokio timeout wrapper producing a typed [`ResilienceError::Timeout`].

use crate::error::ResilienceError;
use std::future::Future;
use std::time::Duration;

/// Run `fut` with a deadline of `dur`.
///
/// On success the future's `Result<T, E>` is returned unchanged (wrapped so the
/// outer error type is uniform). If the deadline elapses first, returns
/// [`ResilienceError::Timeout`] carrying `dur`.
///
/// # Example
/// ```
/// # use resilient_call::{with_timeout, ResilienceError};
/// # use std::time::Duration;
/// # tokio_test_block(async {
/// let res: Result<u8, ResilienceError<std::convert::Infallible>> =
///     with_timeout(async { Ok(7u8) }, Duration::from_millis(50)).await;
/// assert_eq!(res.unwrap(), 7);
/// # });
/// # fn tokio_test_block<F: std::future::Future>(f: F) -> F::Output {
/// #     tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap().block_on(f)
/// # }
/// ```
pub async fn with_timeout<T, E, Fut>(
    fut: Fut,
    dur: Duration,
) -> Result<T, ResilienceError<E>>
where
    Fut: Future<Output = Result<T, E>>,
{
    match tokio::time::timeout(dur, fut).await {
        Ok(Ok(v)) => Ok(v),
        Ok(Err(e)) => Err(ResilienceError::Terminal(e)),
        Err(_elapsed) => Err(ResilienceError::Timeout(dur)),
    }
}
