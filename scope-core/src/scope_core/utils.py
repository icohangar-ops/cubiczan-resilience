"""Small shared utilities repeated across the scope-* handlers.

* ``write_object_to_s3`` / ``write_jsonl_to_s3`` — the S3 raw-write pattern.
* ``poll_until_terminal`` — a generic state poller (used by SafeAthenaClient and
  available for any "start then wait" AWS resource).
* ``response_envelope`` / ``success_response`` / ``error_response`` — the Lambda
  ``{"statusCode", "body"}`` envelope all handlers emit.

Domain-agnostic and boto3-injected so tests need no live AWS.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, Iterable, Optional


def write_object_to_s3(
    s3: Any,
    *,
    bucket: str,
    key: str,
    body: Any,
    content_type: Optional[str] = None,
) -> str:
    """Write a single object to S3 and return its ``s3://`` URI."""
    kwargs: Dict[str, Any] = {"Bucket": bucket, "Key": key, "Body": body}
    if content_type:
        kwargs["ContentType"] = content_type
    s3.put_object(**kwargs)
    return f"s3://{bucket}/{key}"


def write_jsonl_to_s3(
    s3: Any, *, bucket: str, key: str, records: Iterable[Dict[str, Any]]
) -> str:
    """Serialize ``records`` as JSON Lines and write to S3. Returns the URI."""
    body = "\n".join(json.dumps(r) for r in records)
    return write_object_to_s3(
        s3, bucket=bucket, key=key, body=body, content_type="application/json"
    )


def poll_until_terminal(
    fetch_state: Callable[[], str],
    *,
    terminal: Iterable[str],
    timeout: float = 120.0,
    interval: float = 1.0,
    time_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> str:
    """Poll ``fetch_state`` until it returns a terminal state or times out.

    Generic version of the Athena/Glue poll loop. ``time_fn``/``sleep_fn`` are
    injectable so tests can run without real wall-clock sleeps. Raises
    :class:`TimeoutError` if no terminal state is reached in ``timeout`` seconds.
    """
    terminal_set = set(terminal)
    deadline = time_fn() + timeout
    while True:
        state = fetch_state()
        if state in terminal_set:
            return state
        if time_fn() >= deadline:
            raise TimeoutError(
                f"State did not reach {sorted(terminal_set)} within {timeout}s "
                f"(last state: {state})"
            )
        sleep_fn(interval)


def response_envelope(status_code: int, body: Dict[str, Any]) -> Dict[str, Any]:
    """Build the standard Lambda ``{"statusCode", "body"}`` envelope."""
    return {"statusCode": status_code, "body": json.dumps(body)}


def success_response(body: Dict[str, Any]) -> Dict[str, Any]:
    """200 envelope."""
    return response_envelope(200, body)


def error_response(error: Any, *, status_code: int = 500) -> Dict[str, Any]:
    """Error envelope with ``{"error": ...}`` body."""
    return response_envelope(status_code, {"error": str(error)})
