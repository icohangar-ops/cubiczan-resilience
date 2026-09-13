use resilient_call::{
    crdb_retry, retry, verify_ledger_under, with_timeout, AuditError, AuditLedger,
    AuditRecordInput, FileLedger, IdempotencyLedger, ResilienceError, RetryPolicy, SqlError,
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

const AUDIT_KEY: &str = "test-key-0123456789";

fn sample_record(i: u32) -> AuditRecordInput {
    AuditRecordInput {
        event: "decision".into(),
        actor: "agent-1".into(),
        inputs: Some(serde_json::json!({ "i": i })),
        sources: Some(serde_json::json!(["src-a"])),
        confidence: Some(0.9),
        rationale: Some(format!("step {i}")),
        ts: Some(format!("2026-01-01T00:00:0{i}Z")),
    }
}

#[test]
fn audit_append_n_records_and_verify_passes() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("audit.jsonl");
    let ledger = AuditLedger::open_under(&path, Some(AUDIT_KEY.to_string()), dir.path()).unwrap();

    for i in 0..5 {
        ledger.append(sample_record(i)).unwrap();
    }
    let result = ledger.verify().unwrap();
    assert!(result.is_ok());
    assert_eq!(result, resilient_call::VerifyResult::Ok { count: 5 });
}

#[test]
fn audit_records_chain_to_prior_signature() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("audit.jsonl");
    let ledger = AuditLedger::open_under(&path, Some(AUDIT_KEY.to_string()), dir.path()).unwrap();

    let s0 = ledger.append(sample_record(0)).unwrap();
    let s1 = ledger.append(sample_record(1)).unwrap();

    let lines: Vec<serde_json::Value> = std::fs::read_to_string(&path)
        .unwrap()
        .lines()
        .filter(|l| !l.trim().is_empty())
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();

    assert_eq!(lines[0]["prev_sig"], ""); // genesis
    assert_eq!(lines[0]["sig"], s0);
    assert_eq!(lines[1]["prev_sig"], s0); // links to prior sig
    assert_eq!(lines[1]["sig"], s1);
}

#[test]
fn audit_resumes_chain_across_instances() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("audit.jsonl");
    {
        let l1 = AuditLedger::open_under(&path, Some(AUDIT_KEY.to_string()), dir.path()).unwrap();
        l1.append(sample_record(0)).unwrap();
        l1.append(sample_record(1)).unwrap();
    }
    let l2 = AuditLedger::open_under(&path, Some(AUDIT_KEY.to_string()), dir.path()).unwrap();
    l2.append(sample_record(2)).unwrap();

    let result = verify_ledger_under(&path, Some(AUDIT_KEY), dir.path()).unwrap();
    assert_eq!(result, resilient_call::VerifyResult::Ok { count: 3 });
}

#[test]
fn audit_detects_edited_payload_at_right_index() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("audit.jsonl");
    let ledger = AuditLedger::open_under(&path, Some(AUDIT_KEY.to_string()), dir.path()).unwrap();
    for i in 0..4 {
        ledger.append(sample_record(i)).unwrap();
    }

    // Tamper line index 2's payload, leaving its sig untouched.
    let mut lines: Vec<serde_json::Value> = std::fs::read_to_string(&path)
        .unwrap()
        .lines()
        .filter(|l| !l.trim().is_empty())
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    lines[2]["actor"] = serde_json::json!("attacker");
    let rewritten: String = lines
        .iter()
        .map(|l| serde_json::to_string(l).unwrap())
        .collect::<Vec<_>>()
        .join("\n");
    std::fs::write(&path, rewritten + "\n").unwrap();

    let result = verify_ledger_under(&path, Some(AUDIT_KEY), dir.path()).unwrap();
    assert!(!result.is_ok());
    assert_eq!(result.tampered_index(), Some(2));
}

#[test]
fn audit_detects_deleted_interior_line() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("audit.jsonl");
    let ledger = AuditLedger::open_under(&path, Some(AUDIT_KEY.to_string()), dir.path()).unwrap();
    for i in 0..4 {
        ledger.append(sample_record(i)).unwrap();
    }

    let mut lines: Vec<String> = std::fs::read_to_string(&path)
        .unwrap()
        .lines()
        .filter(|l| !l.trim().is_empty())
        .map(str::to_string)
        .collect();
    lines.remove(1); // drop line index 1
    std::fs::write(&path, lines.join("\n") + "\n").unwrap();

    let result = verify_ledger_under(&path, Some(AUDIT_KEY), dir.path()).unwrap();
    assert!(!result.is_ok());
    // Former line 2 (now at index 1) has a prev_sig that no longer matches.
    assert_eq!(result.tampered_index(), Some(1));
}

#[test]
fn audit_fails_under_wrong_key() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("audit.jsonl");
    let ledger = AuditLedger::open_under(&path, Some(AUDIT_KEY.to_string()), dir.path()).unwrap();
    ledger.append(sample_record(0)).unwrap();

    let result = verify_ledger_under(&path, Some("the-wrong-key"), dir.path()).unwrap();
    assert!(!result.is_ok());
    assert_eq!(result.tampered_index(), Some(0));
}

#[test]
fn audit_verify_passes_on_absent_ledger() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("missing.jsonl");
    let result = verify_ledger_under(&path, Some(AUDIT_KEY), dir.path()).unwrap();
    assert_eq!(result, resilient_call::VerifyResult::Ok { count: 0 });
}

/// Golden vector locking the wire format: this exact signature is also asserted
/// by the TypeScript and Python ports, so the three implementations can never
/// silently drift on canonicalization or the signing scheme.
#[test]
fn audit_cross_language_signature_matches() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("x.jsonl");
    let ledger = AuditLedger::open_under(&path, Some("k".to_string()), dir.path()).unwrap();
    let sig = ledger
        .append(AuditRecordInput {
            event: "e".into(),
            actor: "a".into(),
            inputs: Some(serde_json::json!({ "x": 1 })),
            sources: Some(serde_json::json!(["s"])),
            confidence: None,
            rationale: None,
            ts: Some("2026-01-01T00:00:00Z".into()),
        })
        .unwrap();
    assert_eq!(sig, "d379966f5be33822aa1091efa18034e67e679fbadb168bb73c3f42ef712a46fc");
}

#[test]
fn audit_accepts_relative_path_under_base() {
    let dir = tempfile::tempdir().unwrap();
    let ledger = AuditLedger::open_under("audit.jsonl", Some(AUDIT_KEY.to_string()), dir.path())
        .unwrap();
    ledger.append(sample_record(0)).unwrap();
    let result =
        verify_ledger_under(&dir.path().join("audit.jsonl"), Some(AUDIT_KEY), dir.path()).unwrap();
    assert!(result.is_ok());
}

#[test]
fn audit_rejects_relative_traversal() {
    let dir = tempfile::tempdir().unwrap();
    let err = AuditLedger::open_under(
        "../escaped.jsonl",
        Some(AUDIT_KEY.to_string()),
        dir.path(),
    )
    .unwrap_err();
    assert!(matches!(err, AuditError::PathEscape));
}

#[test]
fn audit_rejects_nested_traversal() {
    let dir = tempfile::tempdir().unwrap();
    let err = AuditLedger::open_under(
        "nested/../../escaped.jsonl",
        Some(AUDIT_KEY.to_string()),
        dir.path(),
    )
    .unwrap_err();
    assert!(matches!(err, AuditError::PathEscape));
    let err = verify_ledger_under(
        std::path::Path::new("../escaped.jsonl"),
        Some(AUDIT_KEY),
        dir.path(),
    )
    .unwrap_err();
    assert!(matches!(err, AuditError::PathEscape));
}

#[test]
fn audit_rejects_absolute_path_outside_base() {
    let dir = tempfile::tempdir().unwrap();
    let outside = tempfile::tempdir().unwrap();
    let path = outside.path().join("audit.jsonl");
    let err = AuditLedger::open_under(&path, Some(AUDIT_KEY.to_string()), dir.path()).unwrap_err();
    assert!(matches!(err, AuditError::PathEscape));
}
