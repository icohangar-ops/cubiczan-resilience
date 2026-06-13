"""Tests with a fully mocked provider (no live RPC)."""

import pytest

from resilient_onchain import (
    CircuitBreaker,
    CircuitOpenError,
    FeeFields,
    NonRetryableError,
    NormalizedReceipt,
    OnchainWriteError,
    RetryExhaustedError,
    SendOpts,
    StateResult,
    SubmitResult,
    TxContext,
    TxFailedError,
    bump_fee,
    initial_fee,
    is_nonce_too_low,
    is_receipt_success,
    send_with_retry,
)

OK = NormalizedReceipt(status=1, tx_hash="0xok")
BAD = NormalizedReceipt(status=0, tx_hash="0xbad")


def no_sleep(_s):
    return None


def legacy_state(nonce=7, gas_price=100):
    def _f():
        return StateResult(nonce=nonce, fee=FeeFields(gas_price=gas_price))

    return _f


def opts(**kw):
    kw.setdefault("sleep", no_sleep)
    return SendOpts(**kw)


# ─── receipt success classification ──────────────────────────────────


def test_is_receipt_success_forms():
    assert is_receipt_success(NormalizedReceipt(status=1))
    assert is_receipt_success(NormalizedReceipt(status=True))
    assert is_receipt_success(NormalizedReceipt(status="success"))
    assert not is_receipt_success(NormalizedReceipt(status=0))
    assert not is_receipt_success(NormalizedReceipt(status=False))
    assert not is_receipt_success(NormalizedReceipt(status="reverted"))


# ─── fee bump ─────────────────────────────────────────────────────────


def test_legacy_fee_bumps_strictly_each_attempt():
    f0 = initial_fee("legacy", FeeFields(gas_price=100))
    f1 = bump_fee("legacy", f0, 1.125)
    f2 = bump_fee("legacy", f1, 1.125)
    assert f1.gas_price > f0.gas_price
    assert f2.gas_price > f1.gas_price
    assert f1.gas_price == 113  # floor(112.5) + 1


def test_eip1559_bumps_both_fields():
    f0 = initial_fee("eip1559", FeeFields(max_fee_per_gas=200, max_priority_fee_per_gas=10))
    f1 = bump_fee("eip1559", f0, 1.2)
    assert f1.max_fee_per_gas > f0.max_fee_per_gas
    assert f1.max_priority_fee_per_gas > f0.max_priority_fee_per_gas


def test_tiny_fee_bumps_by_at_least_one_wei():
    f1 = bump_fee("legacy", FeeFields(gas_price=1), 1.01)
    assert f1.gas_price > 1


# ─── happy path ───────────────────────────────────────────────────────


def test_first_attempt_success():
    calls = {"n": 0}

    def sign_send(_ctx):
        calls["n"] += 1
        return SubmitResult(tx_hash="0xok", wait=lambda: OK)

    r = send_with_retry(legacy_state(), sign_send, opts())
    assert r is OK
    assert calls["n"] == 1


# ─── retry then success + gas bump per attempt ───────────────────────


def test_retry_then_success_with_gas_bump():
    seen = []
    calls = {"n": 0}

    def sign_send(ctx: TxContext):
        seen.append(ctx.fee.gas_price)
        calls["n"] += 1
        if calls["n"] < 3:
            def w():
                raise RuntimeError("timeout waiting for receipt")

            return SubmitResult(tx_hash=f"0xdrop{calls['n']}", wait=w)
        return SubmitResult(tx_hash="0xok", wait=lambda: OK)

    r = send_with_retry(
        legacy_state(7, 100),
        sign_send,
        opts(max_attempts=5, fee_mode="legacy", bump_factor=1.5),
    )
    assert r is OK
    assert calls["n"] == 3
    assert len(seen) == 3
    assert seen[1] > seen[0]
    assert seen[2] > seen[1]


# ─── nonce-too-low triggers refresh ──────────────────────────────────


def test_nonce_too_low_triggers_refresh():
    states = [
        StateResult(nonce=7, fee=FeeFields(gas_price=100)),
        StateResult(nonce=9, fee=FeeFields(gas_price=150)),
    ]
    fetch_calls = {"n": 0}

    def fetch_state():
        s = states[fetch_calls["n"]]
        fetch_calls["n"] += 1
        return s

    seen_nonces = []
    calls = {"n": 0}

    def sign_send(ctx: TxContext):
        seen_nonces.append(ctx.nonce)
        calls["n"] += 1
        if calls["n"] == 1:
            def w():
                raise RuntimeError("nonce too low")

            return SubmitResult(tx_hash="0xstale", wait=w)
        return SubmitResult(tx_hash="0xok", wait=lambda: OK)

    r = send_with_retry(fetch_state, sign_send, opts())
    assert r is OK
    assert fetch_calls["n"] == 2  # initial + refresh
    assert seen_nonces == [7, 9]


def test_nonce_too_low_phrasings():
    assert is_nonce_too_low(RuntimeError("nonce too low"))
    assert is_nonce_too_low(RuntimeError("Nonce is too low"))
    assert is_nonce_too_low(RuntimeError("invalid nonce"))
    assert not is_nonce_too_low(RuntimeError("some other error"))


# ─── success assertion throws on reverted receipt ────────────────────


def test_success_assertion_raises_on_reverted_receipt():
    def sign_send(_ctx):
        return SubmitResult(tx_hash="0xbad", wait=lambda: BAD)

    with pytest.raises(TxFailedError):
        send_with_retry(legacy_state(), sign_send, opts())


# ─── retry exhaustion / fail-fast ────────────────────────────────────


def test_retry_exhaustion():
    def sign_send(_ctx):
        def w():
            raise RuntimeError("timeout")

        return SubmitResult(tx_hash="0xnope", wait=w)

    with pytest.raises(RetryExhaustedError):
        send_with_retry(legacy_state(), sign_send, opts(max_attempts=3))


def test_fail_fast_on_insufficient_funds():
    calls = {"n": 0}

    def sign_send(_ctx):
        calls["n"] += 1

        def w():
            raise RuntimeError("insufficient funds for gas")

        return SubmitResult(tx_hash="0x", wait=w)

    with pytest.raises(NonRetryableError):
        send_with_retry(legacy_state(), sign_send, opts(max_attempts=5))
    assert calls["n"] == 1


# ─── circuit breaker ──────────────────────────────────────────────────


def test_circuit_breaker_opens_and_blocks():
    breaker = CircuitBreaker(threshold=2)

    def failing(_ctx):
        def w():
            raise RuntimeError("timeout")

        return SubmitResult(tx_hash="0x", wait=w)

    with pytest.raises(RetryExhaustedError):
        send_with_retry(legacy_state(), failing, opts(max_attempts=3, breaker=breaker))
    assert breaker.is_open

    calls = {"n": 0}

    def good(_ctx):
        calls["n"] += 1
        return SubmitResult(tx_hash="0x", wait=lambda: OK)

    with pytest.raises(CircuitOpenError):
        send_with_retry(legacy_state(), good, opts(breaker=breaker))
    assert calls["n"] == 0  # never submitted


def test_circuit_breaker_resets_on_success():
    breaker = CircuitBreaker(threshold=2)
    breaker.record_failure()
    assert breaker.failures == 1
    breaker.record_success()
    assert breaker.failures == 0
    assert not breaker.is_open


def test_circuit_breaker_half_opens_after_cooldown():
    t = {"v": 1000.0}
    breaker = CircuitBreaker(threshold=1, cooldown_s=5.0, now=lambda: t["v"])
    breaker.record_failure()
    assert breaker.is_open
    t["v"] += 6.0
    assert not breaker.is_open


# ─── idempotency guard ────────────────────────────────────────────────


def test_idempotency_guard_blocks_duplicate():
    calls = {"n": 0}

    def sign_send(_ctx):
        calls["n"] += 1
        return SubmitResult(tx_hash="0x", wait=lambda: OK)

    with pytest.raises(OnchainWriteError) as ei:
        send_with_retry(
            legacy_state(),
            sign_send,
            opts(idempotency_key="job-42", already_sent=lambda k: k == "job-42"),
        )
    assert ei.value.code == "ALREADY_SENT"
    assert calls["n"] == 0


def test_idempotency_guard_proceeds_when_not_sent():
    r = send_with_retry(
        legacy_state(),
        lambda _ctx: SubmitResult(tx_hash="0xok", wait=lambda: OK),
        opts(idempotency_key="job-99", already_sent=lambda _k: False),
    )
    assert r is OK


# ─── config validation ────────────────────────────────────────────────


def test_rejects_bump_factor_le_one():
    with pytest.raises(OnchainWriteError):
        send_with_retry(
            legacy_state(),
            lambda _ctx: SubmitResult(tx_hash="0x", wait=lambda: OK),
            opts(bump_factor=1.0),
        )
