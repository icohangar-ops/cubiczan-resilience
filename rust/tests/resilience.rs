use resilient_call::{
    crdb_retry, retry, with_timeout, FileLedger, IdempotencyLedger, ResilienceError, RetryPolicy,
    SqlError,
};
use std::sync::atomic::{AtomicU32, Ordering};
use std::time::Duration;

/// A fast policy so retry tests don't spend real wall-clock time on backoff.
fn fast_policy(max_attempts: u32) -> RetryPolicy {
    RetryPolicy {
        max_attempts,
        base_delay: Duration::from_millis(0),
        max_delay: Duration::from_millis(0),
    }
}

#[tokio::test]
async fn retry_succeeds_after_n_transient_failures() {
    let calls = AtomicU32::new(0);
    let result: Result<&str, ResilienceError<&str>> = retry(
        || async {
            let n = calls.fetch_add(1, Ordering::SeqCst);
            if n < 3 {
                Err("transient")
            } else {
                Ok("ok")
            }
        },
        &fast_policy(10),
        |_e| true, // all retryable
    )
    .await;

    assert_eq!(result.unwrap(), "ok");
    // 3 failures + 1 success
    assert_eq!(calls.load(Ordering::SeqCst), 4);
}

#[tokio::test]
async fn retry_gives_up_after_max_attempts() {
    let calls = AtomicU32::new(0);
    let result: Result<(), ResilienceError<&str>> = retry(
        || async {
            calls.fetch_add(1, Ordering::SeqCst);
            Err::<(), _>("always fails")
        },
        &fast_policy(4),
        |_e| true,
    )
    .await;

    match result {
        Err(ResilienceError::Exhausted { attempts, source }) => {
            assert_eq!(attempts, 4);
            assert_eq!(source, "always fails");
        }
        other => panic!("expected Exhausted, got {other:?}"),
    }
    assert_eq!(calls.load(Ordering::SeqCst), 4);
}

#[tokio::test]
async fn retry_stops_immediately_on_terminal_error() {
    let calls = AtomicU32::new(0);
    let result: Result<(), ResilienceError<&str>> = retry(
        || async {
            calls.fetch_add(1, Ordering::SeqCst);
            Err::<(), _>("terminal")
        },
        &fast_policy(10),
        |e| *e != "terminal", // terminal is NOT retryable
    )
    .await;

    assert!(matches!(result, Err(ResilienceError::Terminal("terminal"))));
    assert_eq!(calls.load(Ordering::SeqCst), 1, "must not retry a terminal error");
}

#[tokio::test]
async fn timeout_fires_on_slow_future() {
    let result: Result<(), ResilienceError<std::convert::Infallible>> = with_timeout(
        async {
            tokio::time::sleep(Duration::from_secs(30)).await;
            Ok(())
        },
        Duration::from_millis(20),
    )
    .await;

    match result {
        Err(ResilienceError::Timeout(d)) => assert_eq!(d, Duration::from_millis(20)),
        other => panic!("expected Timeout, got {other:?}"),
    }
}

#[tokio::test]
async fn timeout_passes_through_fast_success() {
    let result: Result<u32, ResilienceError<std::convert::Infallible>> =
        with_timeout(async { Ok(99) }, Duration::from_secs(5)).await;
    assert_eq!(result.unwrap(), 99);
}

#[tokio::test]
async fn crdb_retry_retries_on_40001() {
    let calls = AtomicU32::new(0);
    let result = crdb_retry(|| async {
        let n = calls.fetch_add(1, Ordering::SeqCst);
        if n < 2 {
            Err(SqlError::new("40001", "restart transaction: retry txn"))
        } else {
            Ok("committed")
        }
    })
    .await;

    assert_eq!(result.unwrap(), "committed");
    assert_eq!(calls.load(Ordering::SeqCst), 3);
}

#[tokio::test]
async fn crdb_retry_does_not_retry_other_sqlstates() {
    let calls = AtomicU32::new(0);
    let result: Result<(), ResilienceError<SqlError>> = crdb_retry(|| async {
        calls.fetch_add(1, Ordering::SeqCst);
        // 23505 = unique_violation: a real, terminal error — must NOT retry.
        Err::<(), _>(SqlError::new("23505", "duplicate key value"))
    })
    .await;

    match result {
        Err(ResilienceError::Terminal(e)) => assert_eq!(e.sqlstate, "23505"),
        other => panic!("expected Terminal, got {other:?}"),
    }
    assert_eq!(
        calls.load(Ordering::SeqCst),
        1,
        "non-40001 SQLSTATE must not be retried"
    );
}

#[test]
fn ledger_blocks_duplicate_keys() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("idempotency.jsonl");
    let ledger = FileLedger::open(&path).unwrap();

    let key = "payment:txn-abc-123";
    assert!(!ledger.contains(key).unwrap(), "key absent before first run");

    // First execution records the key.
    ledger.record(key).unwrap();
    assert!(ledger.contains(key).unwrap(), "key present after record");

    // A retrying caller checks again and is blocked from re-executing.
    assert!(
        ledger.contains(key).unwrap(),
        "duplicate key must be detected on replay"
    );

    // Recording the same key twice is an idempotent no-op (no second line).
    ledger.record(key).unwrap();
    assert_eq!(ledger.len().unwrap(), 1);
}

#[test]
fn ledger_persists_across_reopen() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("nested").join("ledger.jsonl");

    {
        let ledger = FileLedger::open(&path).unwrap();
        ledger.record("mandate:m-1").unwrap();
        ledger.record("mandate:m-2").unwrap();
    }

    // Reopen: previously recorded keys must still block.
    let reopened = FileLedger::open(&path).unwrap();
    assert!(reopened.contains("mandate:m-1").unwrap());
    assert!(reopened.contains("mandate:m-2").unwrap());
    assert!(!reopened.contains("mandate:m-3").unwrap());
    assert_eq!(reopened.len().unwrap(), 2);
}
