# cubiczan-resilience (Python)

Battle-tested resilience primitives, lifted and generalised from production
services (CFO resilience matrix, Strata AWS-native, Hermes Pi factory guardian,
Valiron advisory AI):

- `@resilient(...)` — mandatory timeout + exponential backoff with **full jitter**
  + pluggable circuit breaker, for sync **and** async callables.
- `CircuitBreaker` — standalone CLOSED / OPEN / HALF_OPEN breaker.
- `IdempotencyStore` — protocol + in-memory and file-backed impls to guard
  money/state operations against double-execution on retry.
- `atomic_write(path, data)` — write-to-temp + `os.replace`, never a partial file.
- FastAPI helpers — fail-closed `require_auth` bearer dependency and a
  `cors_allowlist` factory that forbids wildcard-origin + credentials.

Pure stdlib core. `httpx` and `fastapi` are optional extras.

## Install

```bash
pip install cubiczan-resilience            # core only
pip install 'cubiczan-resilience[fastapi]' # + FastAPI helpers
pip install 'cubiczan-resilience[http]'    # + httpx
```

Requires Python >= 3.10.

## `@resilient`

```python
from cubiczan_resilience import resilient, CircuitBreaker

breaker = CircuitBreaker("payments-api", failure_threshold=5, cooldown_seconds=30)

@resilient(
    timeout=2.0,              # mandatory per-attempt deadline (seconds)
    max_attempts=4,
    base_delay=0.1,           # full-jitter backoff: U(0, base * 2**attempt)
    max_delay=10.0,
    retryable_exceptions=(ConnectionError, TimeoutError),
    circuit_breaker=breaker,  # optional; gates + records outcomes
)
def call_api() -> dict:
    ...

# Async works the same way; the timeout is hard-enforced via asyncio.wait_for.
@resilient(timeout=5.0, max_attempts=3)
async def call_api_async() -> dict:
    ...
```

Retry decisions: if the raised exception exposes an HTTP status (httpx/requests
style `.response.status_code` or a bare `.status_code`), it is retried only when
the code is in `retryable_status` (default `{408, 425, 429, 500, 502, 503, 504}`).
Otherwise the exception type is matched against `retryable_exceptions`.
`CircuitOpenError` is never retried.

## CircuitBreaker (standalone)

```python
from cubiczan_resilience import CircuitBreaker, CircuitOpenError

cb = CircuitBreaker("db", failure_threshold=3, cooldown_seconds=15)

if cb.allow():
    try:
        result = do_query()
    except Exception:
        cb.record_failure()
        raise
    else:
        cb.record_success()
else:
    raise CircuitOpenError(cb.name, cb.retry_after())
```

## Idempotency

```python
from cubiczan_resilience import FileIdempotencyStore

store = FileIdempotencyStore("/var/lib/app/idempotency.json")

def charge(order_id: str, amount: int) -> str:
    if store.already_done(order_id):
        return store.get_result(order_id)          # replay prior result
    if not store.mark_done(order_id, "charged"):    # atomic first-writer-wins claim
        return store.get_result(order_id)
    provider.charge(amount)                          # runs exactly once
    return "charged"
```

`InMemoryIdempotencyStore` has the same interface for tests / single-process use.

## Atomic writes

```python
from cubiczan_resilience import atomic_write

atomic_write("/var/lib/app/state.json", json_payload)   # str or bytes
atomic_write("/var/lib/app/secret", token, mode=0o600)  # set perms atomically
```

A crash before the rename leaves the previous file fully intact — never a
half-written file.

## FastAPI helpers

```python
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from cubiczan_resilience.fastapi_helpers import require_auth, cors_allowlist

app = FastAPI()
app.add_middleware(CORSMiddleware, **cors_allowlist(["https://app.example.com"]))

auth = require_auth(env_var="API_TOKEN")  # fail-closed: 503 if env var unset

@app.get("/secure")
def secure(_: str = Depends(auth)):
    return {"ok": True}
```

## Development

```bash
pip install -e '.[dev]'
pytest
mypy src
```

## License

MIT
