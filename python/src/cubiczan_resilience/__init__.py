"""cubiczan-resilience: battle-tested resilience primitives.

Public API:

* :func:`resilient` — timeout + backoff + circuit-breaker decorator (sync/async)
* :class:`CircuitBreaker`, :class:`CircuitState`, :class:`CircuitOpenError`
* :class:`IdempotencyStore`, :class:`InMemoryIdempotencyStore`,
  :class:`FileIdempotencyStore`
* :func:`atomic_write`
* :class:`AuditLedger`, :func:`verify_ledger` — signed, append-only JSONL audit
  ledger with HMAC-SHA256 signature chaining
* :func:`resolve_tiered` — three-tier live → cache → mock resolution with an
  honest provenance badge (:class:`Cached`, :class:`TieredResult`)
* :class:`RetryPolicy`, :class:`TimeoutExceeded`, :class:`RetriesExhausted`,
  ``DEFAULT_RETRYABLE_STATUS``

The FastAPI helpers (``require_auth``, ``cors_allowlist``) live in
``cubiczan_resilience.fastapi_helpers`` and require the optional ``fastapi``
extra; they are intentionally not imported here so the core package stays
dependency-free.
"""

from __future__ import annotations

from .atomic import atomic_write
from .audit_ledger import (
    AUDIT_LEDGER_KEY_ENV,
    DEFAULT_AUDIT_LEDGER_KEY,
    AuditLedger,
    VerifyResult,
    canonical_json,
    verify_ledger,
)
from .circuit import CircuitBreaker, CircuitOpenError, CircuitState
from .idempotency import (
    FileIdempotencyStore,
    IdempotencyStore,
    InMemoryIdempotencyStore,
)
from .tiered import (
    MISS,
    AllTiersFailed,
    Cached,
    TieredResult,
    TierFailure,
    resolve_tiered,
)
from .verification_gate import (
    CLEAR,
    CONFIDENCE_FLOOR,
    PENALTY_PER_VIOLATION,
    REQUIRES_HUMAN_VERIFICATION,
    VerificationGate,
    build_gate,
)
from .retry import (
    DEFAULT_RETRYABLE_STATUS,
    RetriesExhausted,
    RetryPolicy,
    TimeoutExceeded,
    resilient,
)

__version__ = "0.2.0"

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
    "AuditLedger",
    "VerifyResult",
    "verify_ledger",
    "canonical_json",
    "AUDIT_LEDGER_KEY_ENV",
    "DEFAULT_AUDIT_LEDGER_KEY",
    "resolve_tiered",
    "TieredResult",
    "TierFailure",
    "Cached",
    "AllTiersFailed",
    "MISS",
    "VerificationGate",
    "build_gate",
    "CLEAR",
    "REQUIRES_HUMAN_VERIFICATION",
    "PENALTY_PER_VIOLATION",
    "CONFIDENCE_FLOOR",
    "__version__",
]
