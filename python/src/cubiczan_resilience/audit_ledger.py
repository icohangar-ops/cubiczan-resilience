"""Signed, append-only JSONL audit ledger.

Lifted and generalized from the HMAC-SHA256 audit ledgers in ``cleanmandate``,
``swarmfi-executor``, ``glacier-edge-arm``, and ``compliance-as-code-agent``
(``*-core/src/audit.rs``), which each write one JSON line per decision/event with
a ``content_hash`` and an HMAC signature. This generalizes that scheme and adds
**signature chaining**: every record signs the *previous* record's signature, so
the ledger is tamper-evident and truly append-only — an interior line cannot be
edited, reordered, or deleted without breaking every signature that follows it.

Signing scheme (identical across the TS / Python / Rust ports)::

    canonical = canonical_json({ts, event, actor, inputs, sources,
                                confidence?, rationale?, prev_sig})
    sig       = hex(HMAC-SHA256(key, canonical))

``canonical_json`` emits object keys in sorted order with no insignificant
whitespace, so the signed bytes are stable regardless of insertion order. The
genesis record uses ``prev_sig = ""``.
"""

from __future__ import annotations

import hmac
import json
import os
import threading
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Optional, Union

#: The environment variable read for the signing key.
AUDIT_LEDGER_KEY_ENV = "AUDIT_LEDGER_KEY"

#: The default signing key. Documented and safe ONLY for tests/dev.
DEFAULT_AUDIT_LEDGER_KEY = "cubiczan-resilience-insecure-default-key"


class VerifyResult:
    """Outcome of :meth:`AuditLedger.verify` / :func:`verify_ledger`.

    ``ok`` is ``True`` when the whole chain re-derives correctly. On failure,
    ``tampered_index`` is the zero-based index of the first bad line and
    ``reason`` describes the break.
    """

    __slots__ = ("ok", "count", "tampered_index", "reason")

    def __init__(
        self,
        ok: bool,
        *,
        count: int = 0,
        tampered_index: Optional[int] = None,
        reason: Optional[str] = None,
    ) -> None:
        self.ok = ok
        self.count = count
        self.tampered_index = tampered_index
        self.reason = reason

    def __bool__(self) -> bool:
        return self.ok

    def __repr__(self) -> str:
        if self.ok:
            return f"VerifyResult(ok=True, count={self.count})"
        return (
            f"VerifyResult(ok=False, tampered_index={self.tampered_index}, "
            f"reason={self.reason!r})"
        )


def canonical_json(value: Any) -> str:
    """Stable, whitespace-free JSON with recursively sorted object keys."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _resolve_key(key: Optional[str]) -> str:
    if key is not None:
        return key
    return os.environ.get(AUDIT_LEDGER_KEY_ENV, DEFAULT_AUDIT_LEDGER_KEY)


def _confine_path(
    path: Union[str, Path],
    base_dir: Union[str, Path, None] = None,
) -> Path:
    """Resolve ``path`` and reject it if it escapes ``base_dir``.

    ``base_dir`` defaults to the process cwd. Relative ``..`` traversal and
    absolute paths outside the base are rejected so a caller-supplied ledger
    path cannot read or write files outside the intended directory.
    """
    base = Path(base_dir) if base_dir is not None else Path.cwd()
    base = base.resolve()
    candidate = Path(path)
    resolved = (candidate if candidate.is_absolute() else base / candidate).resolve()
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise ValueError(
            f"audit ledger path escapes allowed base directory: {path}"
        ) from exc
    return resolved


def _signing_payload(record: dict[str, Any]) -> dict[str, Any]:
    """The fields that get signed: content + ``prev_sig``, never ``sig``.

    Optional fields are included only when present so the canonical form matches
    on both append and verify.
    """
    payload: dict[str, Any] = {
        "ts": record["ts"],
        "event": record["event"],
        "actor": record["actor"],
        "inputs": record["inputs"],
        "sources": record["sources"],
        "prev_sig": record["prev_sig"],
    }
    if record.get("confidence") is not None:
        payload["confidence"] = record["confidence"]
    if record.get("rationale") is not None:
        payload["rationale"] = record["rationale"]
    return payload


def _sign(key: str, record: dict[str, Any]) -> str:
    msg = canonical_json(_signing_payload(record)).encode("utf-8")
    return hmac.new(key.encode("utf-8"), msg, sha256).hexdigest()


def _parse_lines(raw: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        records.append(json.loads(line))
    return records


class AuditLedger:
    """File-backed, HMAC-signed, append-only JSONL audit ledger.

    Each :meth:`append` writes one JSON line and chains it to the previous
    line's signature. :meth:`verify` re-walks the file and reports the first
    tampered line.

    The signing key is resolved (in order): the ``key`` argument, then the
    ``AUDIT_LEDGER_KEY`` environment variable, then
    :data:`DEFAULT_AUDIT_LEDGER_KEY` (test-only).
    """

    def __init__(
        self,
        path: Union[str, Path],
        *,
        key: Optional[str] = None,
        base_dir: Union[str, Path, None] = None,
    ) -> None:
        self._base_dir = (
            Path(base_dir).resolve() if base_dir is not None else Path.cwd().resolve()
        )
        self._path = _confine_path(path, self._base_dir)
        self._key = _resolve_key(key)
        self._lock = threading.RLock()
        # Resume the chain from an existing file so appends stay linked.
        self._last_sig = ""
        if self._path.exists():
            records = _parse_lines(self._path.read_text(encoding="utf-8"))
            if records:
                self._last_sig = records[-1]["sig"]

    def append(
        self,
        event: str,
        actor: str,
        *,
        inputs: Any = None,
        sources: Any = None,
        confidence: Optional[float] = None,
        rationale: Optional[str] = None,
        ts: Optional[str] = None,
    ) -> str:
        """Append one record, chained to the prior signature; return its ``sig``.

        ``ts`` defaults to the current UTC time in RFC 3339 form. Supply it to
        make records deterministic in tests.
        """
        record: dict[str, Any] = {
            "ts": ts or datetime.now(timezone.utc).isoformat(),
            "event": event,
            "actor": actor,
            "inputs": inputs,
            "sources": sources,
        }
        if confidence is not None:
            record["confidence"] = confidence
        if rationale is not None:
            record["rationale"] = rationale

        with self._lock:
            record["prev_sig"] = self._last_sig
            sig = _sign(self._key, record)
            record["sig"] = sig

            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")

            self._last_sig = sig
            return sig

    def verify(self) -> VerifyResult:
        """Re-walk the ledger and recompute every signature in-chain."""
        return verify_ledger(self._path, key=self._key, base_dir=self._base_dir)


def verify_ledger(
    path: Union[str, Path],
    *,
    key: Optional[str] = None,
    base_dir: Union[str, Path, None] = None,
) -> VerifyResult:
    """Verify a ledger file without constructing an :class:`AuditLedger`.

    Re-derives each signature from the stored content plus the running
    ``prev_sig`` and returns the index of the first broken line.

    ``path`` is resolved and must stay under ``base_dir`` (default cwd).
    """
    resolved_key = _resolve_key(key)
    p = _confine_path(path, base_dir)
    raw = p.read_text(encoding="utf-8") if p.exists() else ""
    records = _parse_lines(raw)

    prev_sig = ""
    for i, record in enumerate(records):
        if record.get("prev_sig") != prev_sig:
            return VerifyResult(
                False,
                tampered_index=i,
                reason=f"prev_sig mismatch: chain broken at line {i}",
            )
        expected = _sign(resolved_key, record)
        if expected != record.get("sig"):
            return VerifyResult(
                False,
                tampered_index=i,
                reason=f"signature mismatch at line {i}",
            )
        prev_sig = record["sig"]

    return VerifyResult(True, count=len(records))
