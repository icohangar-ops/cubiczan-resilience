"""Idempotency stores.

Generalised from the DynamoDB conditional-put pattern in ``valiron-advisory-ai``
(``ConditionExpression="attribute_not_exists(...)"``): guard money / state
mutations so a retried or duplicated request executes exactly once.

A store answers two questions:

* :meth:`already_done` — has ``key`` been completed before?
* :meth:`mark_done` — atomically claim ``key`` and persist its result.

:meth:`mark_done` returns ``False`` (and does not overwrite) if the key was
already recorded, making it safe to use as a claim primitive.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

from .atomic import atomic_write


@runtime_checkable
class IdempotencyStore(Protocol):
    """Protocol for idempotency-key stores."""

    def already_done(self, key: str) -> bool:
        """Return ``True`` if ``key`` has already been marked done."""
        ...

    def get_result(self, key: str) -> Optional[Any]:
        """Return the stored result for ``key``, or ``None`` if absent."""
        ...

    def mark_done(self, key: str, result: Any = None) -> bool:
        """Atomically record ``key`` with ``result``.

        Returns ``True`` if this call recorded the key (first writer wins),
        ``False`` if it was already present (no overwrite).
        """
        ...


class InMemoryIdempotencyStore:
    """Thread-safe, process-local idempotency store."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {}

    def already_done(self, key: str) -> bool:
        with self._lock:
            return key in self._data

    def get_result(self, key: str) -> Optional[Any]:
        with self._lock:
            return self._data.get(key)

    def mark_done(self, key: str, result: Any = None) -> bool:
        with self._lock:
            if key in self._data:
                return False
            self._data[key] = result
            return True

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


class FileIdempotencyStore:
    """File-backed idempotency store using an atomically rewritten JSON map.

    State is persisted to a single JSON file via :func:`atomic_write`, so the
    file is never left partially written. Suitable for single-host, low-volume
    money/state guards (CLIs, batch jobs, single-process services). For
    multi-process use, layer your own file locking.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                raw = self._path.read_text(encoding="utf-8")
                loaded = json.loads(raw) if raw.strip() else {}
                if isinstance(loaded, dict):
                    self._data = loaded
            except (json.JSONDecodeError, OSError):
                # Corrupt/unreadable state file: start empty rather than crash.
                self._data = {}

    def _flush(self) -> None:
        atomic_write(
            self._path,
            json.dumps(self._data, sort_keys=True, separators=(",", ":")),
        )

    def already_done(self, key: str) -> bool:
        with self._lock:
            return key in self._data

    def get_result(self, key: str) -> Optional[Any]:
        with self._lock:
            return self._data.get(key)

    def mark_done(self, key: str, result: Any = None) -> bool:
        with self._lock:
            if key in self._data:
                return False
            self._data[key] = result
            self._flush()
            return True
