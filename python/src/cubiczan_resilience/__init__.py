"""cubiczan-resilience: battle-tested resilience primitives.

Public API:

* :func:`resilient` — timeout + backoff + circuit-breaker decorator (sync/async)
* :class:`CircuitBreaker`, :class:`CircuitState`, :class:`CircuitOpenError`
* :class:`IdempotencyStore`, :class:`InMemoryIdempotencyStore`,
  :class:`FileIdempotencyStore`
* :func:`atomic_write`
* :class:`RetryPolicy`, :class:`TimeoutExceeded`, :class:`RetriesExhausted`,
  ``DEFAULT_RETRYABLE_STATUS``

The FastAPI helpers (``require_auth``, ``cors_allowlist``) live in
``cubiczan_resilience.fastapi_helpers`` and require the optional ``fastapi``
extra; they are intentionally not imported here so the core package stays
dependency-free.
"""

from __future__ import annotations

from .atomic import atomic_write
from .circuit import CircuitBreaker, CircuitOpenError, CircuitState
from .idempotency import (
    FileIdempotencyStore,
    IdempotencyStore,
    InMemoryIdempotencyStore,
)
from .retry import (
    DEFAULT_RETRYABLE_STATUS,
    RetriesExhausted,
    RetryPolicy,
    TimeoutExceeded,
    resilient,
)

__version__ = "0.1.0"

__all__ = [
    "resilient",
    "RetryPolicy",
    "TimeoutExceeded",
    "RetriesExhausted",
    "DEFAULT_RETRYABLE_STATUS",
    "CircuitBreaker",
    "CircuitState",
    "CircuitOpenError",
    "IdempotencyStore",
    "InMemoryIdempotencyStore",
    "FileIdempotencyStore",
    "atomic_write",
    "__version__",
]
