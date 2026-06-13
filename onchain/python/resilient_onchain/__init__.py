"""Resilient on-chain write client (chain-library-agnostic).

Lifts and generalizes three proven patterns from production repos:
  - critmin-oracle  : bounded retry + gas/fee bump per attempt, pinned nonce,
                      refresh on nonce-too-low, propagate after exhaustion.
  - deepbook-agent  : per-attempt wall-clock timeout, explicit tx-success
                      assertion (raise if receipt status != success).
  - cleanmandate    : idempotency guard before submit (no double-send on a
                      crashed-then-retried job).

The signer/provider is INJECTED. Nothing here imports web3 / eth-account;
callers supply thin adapter callbacks (see README for a web3.py adapter).
"""

from .client import (
    CircuitBreaker,
    CircuitOpenError,
    FeeFields,
    NormalizedReceipt,
    NonRetryableError,
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
    is_retryable,
    send_with_retry,
)

__all__ = [
    "CircuitBreaker",
    "CircuitOpenError",
    "FeeFields",
    "NormalizedReceipt",
    "NonRetryableError",
    "OnchainWriteError",
    "RetryExhaustedError",
    "SendOpts",
    "StateResult",
    "SubmitResult",
    "TxContext",
    "TxFailedError",
    "bump_fee",
    "initial_fee",
    "is_nonce_too_low",
    "is_receipt_success",
    "is_retryable",
    "send_with_retry",
]
