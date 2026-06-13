"""Core resilient on-chain write helper. Synchronous, dependency-free.

Fee amounts are kept as ``int`` (wei) to avoid float drift.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, Union

# ─── Errors ──────────────────────────────────────────────────────────


class OnchainWriteError(Exception):
    """Base error. ``code`` mirrors the TS client for cross-language parity."""

    def __init__(self, message: str, code: str, cause: Optional[BaseException] = None):
        super().__init__(message)
        self.code = code
        self.cause = cause


class TxFailedError(OnchainWriteError):
    """Receipt confirmed but reports a reverted/failed tx."""

    def __init__(self, message: str, receipt: object = None):
        super().__init__(message, "TX_FAILED")
        self.receipt = receipt


class CircuitOpenError(OnchainWriteError):
    """Circuit breaker is open and refuses to submit."""

    def __init__(self, message: str):
        super().__init__(message, "CIRCUIT_OPEN")


class RetryExhaustedError(OnchainWriteError):
    """Retries exhausted without a successful confirmation."""

    def __init__(self, message: str, cause: Optional[BaseException] = None):
        super().__init__(message, "RETRY_EXHAUSTED", cause)


class NonRetryableError(OnchainWriteError):
    """Deterministic error that will never succeed on retry."""

    def __init__(self, message: str, cause: Optional[BaseException] = None):
        super().__init__(message, "NON_RETRYABLE", cause)


# ─── Fee fields ──────────────────────────────────────────────────────

FeeMode = str  # "legacy" | "eip1559"


@dataclass
class FeeFields:
    """Fee fields injected into a tx. ``legacy`` uses ``gas_price``;
    ``eip1559`` uses ``max_fee_per_gas`` + ``max_priority_fee_per_gas``.
    """

    gas_price: Optional[int] = None
    max_fee_per_gas: Optional[int] = None
    max_priority_fee_per_gas: Optional[int] = None


@dataclass
class NormalizedReceipt:
    """Minimal receipt contract. ``status`` 1/True/'success' means success."""

    status: Union[int, bool, str]
    tx_hash: Optional[str] = None
    raw: object = None


def is_receipt_success(r: NormalizedReceipt) -> bool:
    s = r.status
    if isinstance(s, bool):
        return s
    if isinstance(s, int):
        return s == 1
    norm = str(s).lower()
    return norm in ("success", "1", "true", "ok")


@dataclass
class TxContext:
    """Everything a caller needs to build+sign+send one attempt."""

    nonce: int
    fee: FeeFields
    attempt: int


@dataclass
class SubmitResult:
    """Result of one sign+send: tx hash + a callable awaiting the receipt."""

    tx_hash: str
    wait: Callable[[], NormalizedReceipt]


# ─── Circuit breaker ─────────────────────────────────────────────────


class CircuitBreaker:
    """Off-chain breaker: after ``threshold`` consecutive failures it opens and
    rejects submissions for ``cooldown_s`` (prevents draining gas into a
    failing chain/RPC). A success resets the failure count.
    """

    def __init__(
        self,
        threshold: int,
        cooldown_s: float = 0.0,
        now: Callable[[], float] = time.monotonic,
    ):
        if threshold < 1:
            raise OnchainWriteError("threshold must be >= 1", "BAD_CONFIG")
        self._threshold = threshold
        self._cooldown_s = cooldown_s
        self._now = now
        self._consecutive_failures = 0
        self._opened_at: Optional[float] = None

    def assert_closed(self) -> None:
        if self._opened_at is None:
            return
        if self._cooldown_s > 0 and self._now() - self._opened_at >= self._cooldown_s:
            return  # half-open: allow a trial through
        raise CircuitOpenError(
            f"circuit open after {self._consecutive_failures} consecutive failures"
        )

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._threshold:
            self._opened_at = self._now()

    @property
    def is_open(self) -> bool:
        try:
            self.assert_closed()
            return False
        except CircuitOpenError:
            return True

    @property
    def failures(self) -> int:
        return self._consecutive_failures


# ─── Fee init + bump ─────────────────────────────────────────────────


def initial_fee(mode: FeeMode, base: FeeFields) -> FeeFields:
    if mode == "legacy":
        if base.gas_price is None:
            raise OnchainWriteError("legacy mode requires gas_price", "BAD_FEE")
        return FeeFields(gas_price=base.gas_price)
    if base.max_fee_per_gas is None:
        raise OnchainWriteError("eip1559 mode requires max_fee_per_gas", "BAD_FEE")
    return FeeFields(
        max_fee_per_gas=base.max_fee_per_gas,
        max_priority_fee_per_gas=(
            base.max_priority_fee_per_gas
            if base.max_priority_fee_per_gas is not None
            else base.max_fee_per_gas
        ),
    )


def bump_fee(mode: FeeMode, fee: FeeFields, factor: float) -> FeeFields:
    """Bump fee for the next attempt. Mirrors critmin's ``int(gas*factor)+1``:
    floor then +1 wei so a replacement tx ALWAYS strictly exceeds the prior fee.
    """

    def bump(v: int) -> int:
        return (v * round(factor * 1000)) // 1000 + 1

    if mode == "legacy":
        assert fee.gas_price is not None
        return FeeFields(gas_price=bump(fee.gas_price))
    assert fee.max_fee_per_gas is not None
    prio = (
        fee.max_priority_fee_per_gas
        if fee.max_priority_fee_per_gas is not None
        else fee.max_fee_per_gas
    )
    return FeeFields(
        max_fee_per_gas=bump(fee.max_fee_per_gas),
        max_priority_fee_per_gas=bump(prio),
    )


# ─── Error heuristics ────────────────────────────────────────────────


def is_nonce_too_low(err: BaseException) -> bool:
    msg = str(err).lower()
    return any(
        s in msg
        for s in (
            "nonce too low",
            "nonce-too-low",
            "nonce is too low",
            "already known",
            "invalid nonce",
        )
    )


def is_retryable(err: BaseException) -> bool:
    msg = str(err).lower()
    non_retryable = (
        "insufficient funds",
        "invalid signature",
        "execution reverted",
        "unauthorized",
    )
    return not any(s in msg for s in non_retryable)


# ─── Callbacks ───────────────────────────────────────────────────────

FetchState = Callable[[], "StateResult"]
SignSend = Callable[[TxContext], SubmitResult]
AlreadySent = Callable[[str], bool]


@dataclass
class StateResult:
    nonce: int
    fee: FeeFields


@dataclass
class SendOpts:
    max_attempts: int = 5
    fee_mode: FeeMode = "legacy"
    bump_factor: float = 1.125
    timeout_s: float = 120.0
    backoff_base_s: float = 1.0
    idempotency_key: Optional[str] = None
    already_sent: Optional[AlreadySent] = None
    breaker: Optional[CircuitBreaker] = None
    logger: Callable[[str], None] = lambda _m: None
    sleep: Callable[[float], None] = time.sleep


# ─── Per-attempt timeout wrapper (deepbook pattern) ──────────────────


def _with_timeout(op: Callable[[], NormalizedReceipt], timeout_s: float, label: str) -> NormalizedReceipt:
    """Synchronous timeout guard. Records the deadline and asserts the operation
    did not exceed it. (Synchronous code can't preempt a blocking call mid-flight,
    so this bounds total elapsed time and surfaces a TIMEOUT error after the fact;
    adapters should also pass a native timeout to their RPC wait call.)
    """
    start = time.monotonic()
    result = op()
    if time.monotonic() - start > timeout_s:
        raise OnchainWriteError(f"{label} exceeded timeout of {timeout_s}s", "TIMEOUT")
    return result


# ─── Core: send_with_retry ───────────────────────────────────────────


def send_with_retry(
    fetch_state: FetchState,
    sign_send: SignSend,
    opts: Optional[SendOpts] = None,
) -> NormalizedReceipt:
    """Submit an on-chain write with bounded retry, per-attempt fee bump, pinned
    nonce (refreshed on nonce-too-low), explicit success assertion, an optional
    circuit breaker, and an optional idempotency guard.

    Returns the successful NormalizedReceipt; raises on exhaustion, open circuit,
    deterministic error, or confirmed-but-reverted tx.
    """
    o = opts or SendOpts()

    if o.bump_factor <= 1:
        raise OnchainWriteError("bump_factor must be > 1", "BAD_CONFIG")
    if o.max_attempts < 1:
        raise OnchainWriteError("max_attempts must be >= 1", "BAD_CONFIG")

    # Idempotency guard (cleanmandate pattern): bail BEFORE spending any gas.
    if o.idempotency_key is not None and o.already_sent is not None:
        if o.already_sent(o.idempotency_key):
            raise OnchainWriteError(
                f"idempotent: tx for key '{o.idempotency_key}' already submitted",
                "ALREADY_SENT",
            )

    # Circuit breaker check (off-chain).
    if o.breaker is not None:
        o.breaker.assert_closed()

    # Pin nonce + fee once (critmin pattern). Same nonce across replacements.
    state = fetch_state()
    nonce = state.nonce
    fee = initial_fee(o.fee_mode, state.fee)

    last_err: Optional[BaseException] = None
    for attempt in range(1, o.max_attempts + 1):
        try:
            def _submit_and_wait(n=nonce, f=fee, a=attempt) -> NormalizedReceipt:
                submitted = sign_send(TxContext(nonce=n, fee=f, attempt=a))
                o.logger(f"attempt {a}/{o.max_attempts} sent nonce={n} hash={submitted.tx_hash}")
                return submitted.wait()

            receipt = _with_timeout(_submit_and_wait, o.timeout_s, f"attempt {attempt}")

            # Explicit success assertion (deepbook pattern).
            if not is_receipt_success(receipt):
                if o.breaker is not None:
                    o.breaker.record_failure()
                raise TxFailedError(
                    f"tx confirmed but reverted (status={receipt.status})", receipt
                )

            if o.breaker is not None:
                o.breaker.record_success()
            o.logger(f"attempt {attempt} confirmed success hash={receipt.tx_hash or ''}")
            return receipt

        except TxFailedError:
            raise  # confirmed revert is terminal
        except BaseException as err:  # noqa: BLE001 - we classify below
            last_err = err

            # Nonce drifted: refresh nonce + fee and retry.
            if is_nonce_too_low(err):
                o.logger(f"attempt {attempt} nonce-too-low: refreshing nonce")
                refreshed = fetch_state()
                nonce = refreshed.nonce
                fee = initial_fee(o.fee_mode, refreshed.fee)
                if attempt < o.max_attempts:
                    o.sleep(o.backoff_base_s)
                    continue

            if o.breaker is not None:
                o.breaker.record_failure()

            if not is_retryable(err):
                raise NonRetryableError(
                    f"non-retryable on-chain error on attempt {attempt}: {err}", err
                )

            if attempt < o.max_attempts:
                # Bump fee + resend with SAME nonce (critmin pattern).
                fee = bump_fee(o.fee_mode, fee, o.bump_factor)
                delay = o.backoff_base_s * 2 ** (attempt - 1)
                o.logger(
                    f"attempt {attempt} failed ({err}); bumping fee + retrying in {delay}s"
                )
                o.sleep(delay)

    raise RetryExhaustedError(
        f"on-chain write failed after {o.max_attempts} attempts: {last_err}", last_err
    )
