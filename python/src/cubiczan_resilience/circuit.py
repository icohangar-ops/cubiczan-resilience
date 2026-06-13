"""In-memory circuit breaker.

Generalised from the DynamoDB-backed breaker in ``strata-aws-native`` and the
provider-failover logic in ``cfo-resilience-matrix``.  This implementation is
process-local (no external store) and thread-safe.

State machine::

    CLOSED    --[failures >= threshold]--> OPEN
    OPEN      --[cooldown elapsed]-------> HALF_OPEN
    HALF_OPEN --[success]---------------> CLOSED
    HALF_OPEN --[failure]---------------> OPEN
"""

from __future__ import annotations

import threading
import time
from enum import Enum
from typing import Callable


class CircuitState(str, Enum):
    """The three circuit-breaker states."""

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitOpenError(RuntimeError):
    """Raised when a call is attempted while the circuit is OPEN."""

    def __init__(self, name: str, retry_after: float) -> None:
        self.name = name
        self.retry_after = retry_after
        super().__init__(
            f"circuit '{name}' is OPEN; retry in {retry_after:.2f}s"
        )


class CircuitBreaker:
    """A thread-safe, in-memory circuit breaker.

    Parameters
    ----------
    name:
        Human-readable identifier used in error messages and logs.
    failure_threshold:
        Number of consecutive failures in CLOSED state before the circuit
        trips to OPEN.
    cooldown_seconds:
        How long the circuit stays OPEN before allowing a single trial call
        (HALF_OPEN).
    half_open_max_calls:
        Maximum number of concurrent trial calls permitted in HALF_OPEN. The
        first success closes the circuit; a failure re-opens it.
    clock:
        Injectable monotonic clock (defaults to :func:`time.monotonic`) for
        deterministic testing.
    """

    def __init__(
        self,
        name: str = "default",
        *,
        failure_threshold: int = 5,
        cooldown_seconds: float = 30.0,
        half_open_max_calls: int = 1,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must be >= 0")
        if half_open_max_calls < 1:
            raise ValueError("half_open_max_calls must be >= 1")

        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.half_open_max_calls = half_open_max_calls
        self._clock = clock

        self._lock = threading.RLock()
        self._state: CircuitState = CircuitState.CLOSED
        self._failure_count: int = 0
        self._opened_at: float | None = None
        self._half_open_calls: int = 0

    # -- introspection -----------------------------------------------------

    @property
    def state(self) -> CircuitState:
        """Return the current state, applying time-based OPEN -> HALF_OPEN."""
        with self._lock:
            self._maybe_half_open()
            return self._state

    @property
    def failure_count(self) -> int:
        with self._lock:
            return self._failure_count

    def _maybe_half_open(self) -> None:
        """Transition OPEN -> HALF_OPEN if the cooldown has elapsed."""
        if self._state is CircuitState.OPEN and self._opened_at is not None:
            if self._clock() - self._opened_at >= self.cooldown_seconds:
                self._state = CircuitState.HALF_OPEN
                self._half_open_calls = 0

    # -- gate --------------------------------------------------------------

    def allow(self) -> bool:
        """Return ``True`` if a call may proceed, reserving a HALF_OPEN slot."""
        with self._lock:
            self._maybe_half_open()
            if self._state is CircuitState.OPEN:
                return False
            if self._state is CircuitState.HALF_OPEN:
                if self._half_open_calls >= self.half_open_max_calls:
                    return False
                self._half_open_calls += 1
            return True

    def retry_after(self) -> float:
        """Seconds until the circuit will permit a trial call (0 if allowed)."""
        with self._lock:
            self._maybe_half_open()
            if self._state is not CircuitState.OPEN or self._opened_at is None:
                return 0.0
            remaining = self.cooldown_seconds - (self._clock() - self._opened_at)
            return max(0.0, remaining)

    # -- outcome reporting -------------------------------------------------

    def record_success(self) -> None:
        """Record a successful call; closes the circuit from HALF_OPEN."""
        with self._lock:
            self._failure_count = 0
            self._half_open_calls = 0
            self._opened_at = None
            self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        """Record a failed call; may trip CLOSED -> OPEN or HALF_OPEN -> OPEN."""
        with self._lock:
            if self._state is CircuitState.HALF_OPEN:
                self._trip_open()
                return
            self._failure_count += 1
            if self._failure_count >= self.failure_threshold:
                self._trip_open()

    def _trip_open(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = self._clock()
        self._half_open_calls = 0

    def reset(self) -> None:
        """Force the circuit back to CLOSED and clear all counters."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._opened_at = None
            self._half_open_calls = 0

    # -- convenience -------------------------------------------------------

    def guard(self) -> None:
        """Raise :class:`CircuitOpenError` if the circuit will not allow a call."""
        if not self.allow():
            raise CircuitOpenError(self.name, self.retry_after())
