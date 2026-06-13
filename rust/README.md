# resilient-call

Small, dependency-light **resilience primitives** for the cubiczan portfolio.

Closes the two most common defects from the architecture audit:

1. external calls with no **timeout / retry / backoff**, and
2. money/state operations with no **idempotency** guard.

Lifted and generalized from proven patterns in `cross-harness-scaffolder`
(CockroachDB serializable-retry) and `swarmfi-executor` / `cleanmandate`
(idempotency ledger). The CRDB retry in the scaffolder hard-coded its delay
schedule and had **no jitter**; this crate adds full jitter and a backoff cap.

## What you get

| API | Purpose |
|---|---|
| `retry(op, &policy, classify)` | async generic retry: exponential backoff + **full jitter**, max attempts, classifier closure deciding retryable vs terminal |
| `with_timeout(fut, dur)` | tokio timeout wrapper → typed `ResilienceError::Timeout` |
| `crdb_retry(op)` | CockroachDB serializable retry; retries **only** on SQLSTATE `40001`, capped backoff + jitter |
| `IdempotencyLedger` / `FileLedger` | JSONL-backed `contains(key)` / `record(key)` guard for money/state ops |

Minimal deps: `tokio`, `rand`, `thiserror`, `serde`/`serde_json`. No network deps.

## Add it

```toml
[dependencies]
resilient-call = { path = "../cubiczan-resilience/rust" }
```

## Examples

### Retry with backoff + jitter and a classifier

```rust
use resilient_call::{retry, RetryPolicy, ResilienceError};

let policy = RetryPolicy::with_max_attempts(5); // base 50ms, cap 2s, full jitter

let result: Result<Bytes, ResilienceError<HttpError>> = retry(
    || async { http_client.get(url).await },     // re-runnable per attempt
    &policy,
    |e: &HttpError| e.is_transient(),             // 5xx/timeout retryable, 4xx terminal
).await;

match result {
    Ok(body) => { /* ... */ }
    Err(ResilienceError::Exhausted { attempts, source }) => { /* gave up */ }
    Err(ResilienceError::Terminal(e)) => { /* non-retryable, surfaced at once */ }
    Err(ResilienceError::Timeout(_)) => unreachable!(),
}
```

### Timeout wrapper

```rust
use resilient_call::{with_timeout, ResilienceError};
use std::time::Duration;

match with_timeout(slow_call(), Duration::from_secs(2)).await {
    Ok(v) => { /* ... */ }
    Err(ResilienceError::Timeout(d)) => eprintln!("timed out after {d:?}"),
    Err(other) => { /* underlying error */ }
}
```

### CockroachDB serializable retry (SQLSTATE 40001 only)

```rust
use resilient_call::{crdb_retry, SqlError};

let row = crdb_retry(|| async {
    // open + run + commit a SERIALIZABLE transaction.
    // Map driver errors into SqlError { sqlstate, message }.
    run_txn().await.map_err(|e| SqlError::new(e.sqlstate(), e.to_string()))
}).await?;
// Retried on 40001 (serialization_failure) with capped backoff + jitter.
// Any other SQLSTATE (e.g. 23505 unique_violation) is terminal immediately.
```

### Idempotency ledger (guard money/state ops)

```rust
use resilient_call::{FileLedger, IdempotencyLedger};

let ledger = FileLedger::open(".state/idempotency.jsonl")?;
let key = format!("payment:{}", idempotency_key);

if ledger.contains(&key)? {
    return Ok(prior_result()); // idempotent replay: do NOT re-charge
}
charge_card(amount)?;          // the side effect
ledger.record(&key)?;          // mark done so retries are blocked
```

## Tests

```sh
cargo check
cargo test
```

Covered: retry succeeds after N transient failures; gives up after max
attempts; terminal errors short-circuit; timeout fires (and passes fast
successes through); `crdb_retry` retries on `40001` but not other SQLSTATEs;
ledger blocks duplicate keys and persists across reopen.

## License

MIT OR Apache-2.0
