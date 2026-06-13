"""The ``@resilient`` decorator: timeout + exponential backoff + circuit breaker.

Generalised from the gateway retry loops in ``cfo-resilience-matrix`` and
``valiron-advisory-ai``: mandatory per-attempt timeout, exponential backoff
with *full jitter* (``sleep = U(0, base * multiplier**attempt)``), configurable
retryable exceptions / HTTP status codes, and an optional pluggable
:class:`CircuitBreaker`.

Both synchronous and ``async def`` callables are supported; the decorator
detects coroutine functions and dispatches to the matching driver.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import random
import time
from dataclasses import dataclass, field
from typing import (
    Any,
    Awaitable,
    Callable,
    Sequence,
    TypeVar,
    cast,
    overload,
)

from .circuit import CircuitBreaker, CircuitOpenError

logger = logging.getLogger("cubiczan_resilience.retry")

F = TypeVar("F", bound=Callable[..., Any])

# Status codes that are safe to retry by default (transient server / throttle).
DEFAULT_RETRYABLE_STATUS: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})


class TimeoutExceeded(TimeoutError):
    """Raised when a single attempt exceeds the configured timeout."""


class RetriesExhausted(RuntimeError):
    """Raised when all retry attempts have been used without success."""

    def __init__(self, attempts: int, last_exc: BaseException) -> None:
        self.attempts = attempts
        self.last_exc = last_exc
        super().__init__(f"all {attempts} attempt(s) failed; last error: {last_exc!r}")


def _status_of(exc: BaseException) -> int | None:
    """Best-effort extraction of an HTTP status code from an exception.

    Recognises httpx/requests-style ``.response.status_code`` and a bare
    ``.status_code`` attribute, without importing those libraries.
    """
    resp = getattr(exc, "response", None)
    if resp is not None:
        code = getattr(resp, "status_code", None)
        if isinstance(code, int):
            return code
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    return None


@dataclass
class RetryPolicy:
    """Resolved, validated retry configuration."""

    timeout: float
    max_attempts: int = 3
    base_delay: float = 0.1
    max_delay: float = 30.0
    multiplier: float = 2.0
    retryable_exceptions: tuple[type[BaseException], ...] = (Exception,)
    retryable_status: frozenset[int] = DEFAULT_RETRYABLE_STATUS
    circuit_breaker: CircuitBreaker | None = None
    rng: random.Random = field(default_factory=random.Random)

    def __post_init__(self) -> None:
        if self.timeout <= 0:
            raise ValueError("timeout must be > 0 (a mandatory deadline is required)")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.base_delay < 0 or self.max_delay < 0:
            raise ValueError("delays must be >= 0")
        if self.multiplier < 1:
            raise ValueError("multiplier must be >= 1")

    def backoff(self, attempt: int) -> float:
        """Full-jitter backoff for a zero-based ``attempt`` index."""
        ceiling = min(self.base_delay * (self.multiplier ** attempt), self.max_delay)
        return self.rng.uniform(0.0, ceiling)

    def is_retryable(self, exc: BaseException) -> bool:
        if isinstance(exc, CircuitOpenError):
            return False
        status = _status_of(exc)
        if status is not None:
            # When the error carries an HTTP status, retry only on listed codes.
            return status in self.retryable_status
        return isinstance(exc, self.retryable_exceptions)


def _build_policy(
    *,
    timeout: float,
    max_attempts: int,
    base_delay: float,
    max_delay: float,
    multiplier: float,
    retryable_exceptions: Sequence[type[BaseException]] | None,
    retryable_status: frozenset[int],
    circuit_breaker: CircuitBreaker | None,
    rng: random.Random | None,
) -> RetryPolicy:
    excs: tuple[type[BaseException], ...] = (
        tuple(retryable_exceptions) if retryable_exceptions is not None else (Exception,)
    )
    return RetryPolicy(
        timeout=timeout,
        max_attempts=max_attempts,
        base_delay=base_delay,
        max_delay=max_delay,
        multiplier=multiplier,
        retryable_exceptions=excs,
        retryable_status=retryable_status,
        circuit_breaker=circuit_breaker,
        rng=rng or random.Random(),
    )


@overload
def resilient(func: F) -> F: ...
@overload
def resilient(
    *,
    timeout: float,
    max_attempts: int = ...,
    base_delay: float = ...,
    max_delay: float = ...,
    multiplier: float = ...,
    retryable_exceptions: Sequence[type[BaseException]] | None = ...,
    retryable_status: frozenset[int] = ...,
    circuit_breaker: CircuitBreaker | None = ...,
    rng: random.Random | None = ...,
) -> Callable[[F], F]: ...


def resilient(
    func: F | None = None,
    *,
    timeout: float = 30.0,
    max_attempts: int = 3,
    base_delay: float = 0.1,
    max_delay: float = 30.0,
    multiplier: float = 2.0,
    retryable_exceptions: Sequence[type[BaseException]] | None = None,
    retryable_status: frozenset[int] = DEFAULT_RETRYABLE_STATUS,
    circuit_breaker: CircuitBreaker | None = None,
    rng: random.Random | None = None,
) -> F | Callable[[F], F]:
    """Wrap a callable with timeout, backoff retries, and an optional breaker.

    A mandatory per-attempt ``timeout`` (seconds) bounds every call. Failures
    that match ``retryable_exceptions`` (or carry a status code in
    ``retryable_status``) are retried up to ``max_attempts`` times with
    full-jitter exponential backoff. When a ``circuit_breaker`` is supplied,
    calls are gated through it and outcomes recorded.

    Works on both ``def`` and ``async def`` callables. For synchronous
    functions the timeout is enforced cooperatively: it is checked *after* the
    call returns (a sync function cannot be interrupted mid-flight without
    threads), so it primarily guards slow-but-returning work and feeds the
    retry decision. For ``async def`` callables the timeout is hard-enforced
    via :func:`asyncio.wait_for`.
    """

    def decorate(fn: F) -> F:
        policy = _build_policy(
            timeout=timeout,
            max_attempts=max_attempts,
            base_delay=base_delay,
            max_delay=max_delay,
            multiplier=multiplier,
            retryable_exceptions=retryable_exceptions,
            retryable_status=retryable_status,
            circuit_breaker=circuit_breaker,
            rng=rng,
        )

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                return await _run_async(fn, policy, args, kwargs)

            return cast(F, async_wrapper)

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            return _run_sync(fn, policy, args, kwargs)

        return cast(F, sync_wrapper)

    if func is not None:
        return decorate(func)
    return decorate


def _sleep_sync(policy: RetryPolicy, attempt: int) -> None:
    delay = policy.backoff(attempt)
    if delay > 0:
        time.sleep(delay)


def _run_sync(
    fn: Callable[..., Any],
    policy: RetryPolicy,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    cb = policy.circuit_breaker
    last_exc: BaseException | None = None
    for attempt in range(policy.max_attempts):
        if cb is not None:
            cb.guard()
        start = time.monotonic()
        try:
            result = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - we re-raise below
            if cb is not None:
                cb.record_failure()
            last_exc = exc
            if not policy.is_retryable(exc) or attempt == policy.max_attempts - 1:
                raise
            logger.debug(
                "retry %s attempt %d failed: %r", getattr(fn, "__name__", fn), attempt + 1, exc
            )
            _sleep_sync(policy, attempt)
            continue

        elapsed = time.monotonic() - start
        if elapsed > policy.timeout:
            exc = TimeoutExceeded(
                f"call took {elapsed:.3f}s, exceeding timeout {policy.timeout:.3f}s"
            )
            if cb is not None:
                cb.record_failure()
            last_exc = exc
            if attempt == policy.max_attempts - 1:
                raise exc
            _sleep_sync(policy, attempt)
            continue

        if cb is not None:
            cb.record_success()
        return result

    assert last_exc is not None
    raise RetriesExhausted(policy.max_attempts, last_exc)


async def _run_async(
    fn: Callable[..., Awaitable[Any]],
    policy: RetryPolicy,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    cb = policy.circuit_breaker
    last_exc: BaseException | None = None
    for attempt in range(policy.max_attempts):
        if cb is not None:
            cb.guard()
        try:
            result = await asyncio.wait_for(fn(*args, **kwargs), timeout=policy.timeout)
        except asyncio.TimeoutError as exc:
            wrapped: BaseException = TimeoutExceeded(
                f"call exceeded timeout {policy.timeout:.3f}s"
            )
            wrapped.__cause__ = exc
            if cb is not None:
                cb.record_failure()
            last_exc = wrapped
            if attempt == policy.max_attempts - 1:
                raise wrapped
            await asyncio.sleep(policy.backoff(attempt))
            continue
        except BaseException as exc:  # noqa: BLE001
            if cb is not None:
                cb.record_failure()
            last_exc = exc
            if not policy.is_retryable(exc) or attempt == policy.max_attempts - 1:
                raise
            await asyncio.sleep(policy.backoff(attempt))
            continue

        if cb is not None:
            cb.record_success()
        return result

    assert last_exc is not None
    raise RetriesExhausted(policy.max_attempts, last_exc)
