import asyncio
import random
import time

import pytest

from cubiczan_resilience import (
    CircuitBreaker,
    CircuitOpenError,
    RetriesExhausted,
    TimeoutExceeded,
    resilient,
)


def _fixed_rng() -> random.Random:
    # Deterministic + zero sleeps so tests are fast.
    r = random.Random()
    r.uniform = lambda a, b: 0.0  # type: ignore[method-assign]
    return r


def test_timeout_sync_raises():
    @resilient(timeout=0.05, max_attempts=1, rng=_fixed_rng())
    def slow():
        time.sleep(0.2)
        return "ok"

    with pytest.raises(TimeoutExceeded):
        slow()


def test_backoff_retry_then_success():
    calls = {"n": 0}

    @resilient(timeout=1.0, max_attempts=4, base_delay=0.001, rng=_fixed_rng())
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ValueError("transient")
        return "recovered"

    assert flaky() == "recovered"
    assert calls["n"] == 3


def test_non_retryable_status_not_retried():
    class HttpErr(Exception):
        def __init__(self, code):
            self.status_code = code

    calls = {"n": 0}

    @resilient(timeout=1.0, max_attempts=5, rng=_fixed_rng())
    def boom():
        calls["n"] += 1
        raise HttpErr(404)  # not in retryable status set

    with pytest.raises(HttpErr):
        boom()
    assert calls["n"] == 1


def test_retryable_status_is_retried():
    class HttpErr(Exception):
        def __init__(self, code):
            self.status_code = code

    calls = {"n": 0}

    @resilient(timeout=1.0, max_attempts=3, base_delay=0.001, rng=_fixed_rng())
    def boom():
        calls["n"] += 1
        raise HttpErr(503)

    with pytest.raises(HttpErr):
        boom()
    assert calls["n"] == 3


def test_exhausted_raises_last_exc():
    @resilient(timeout=1.0, max_attempts=2, base_delay=0.001, rng=_fixed_rng())
    def always():
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError, match="nope"):
        always()


def test_circuit_breaker_integration_blocks():
    cb = CircuitBreaker("svc", failure_threshold=2, cooldown_seconds=60)

    @resilient(timeout=1.0, max_attempts=1, circuit_breaker=cb, rng=_fixed_rng())
    def fail():
        raise ValueError("down")

    with pytest.raises(ValueError):
        fail()
    with pytest.raises(ValueError):
        fail()
    # Circuit now open -> next call short-circuits with CircuitOpenError.
    with pytest.raises(CircuitOpenError):
        fail()


def test_async_timeout():
    @resilient(timeout=0.05, max_attempts=1, rng=_fixed_rng())
    async def slow():
        await asyncio.sleep(0.3)
        return "ok"

    with pytest.raises(TimeoutExceeded):
        asyncio.run(slow())


def test_async_retry_then_success():
    state = {"n": 0}

    @resilient(timeout=1.0, max_attempts=4, base_delay=0.001, rng=_fixed_rng())
    async def flaky():
        state["n"] += 1
        if state["n"] < 2:
            raise ValueError("transient")
        return "ok"

    assert asyncio.run(flaky()) == "ok"
    assert state["n"] == 2


def test_bad_timeout_rejected():
    with pytest.raises(ValueError):

        @resilient(timeout=0)
        def f():
            return 1
