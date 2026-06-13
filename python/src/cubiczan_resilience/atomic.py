"""Atomic file writes.

Generalised from the durable-write pattern used by ``hermes-pi-factory-guardian``:
write to a temporary file in the *same directory* as the destination, flush and
``fsync`` it, then ``os.replace`` it into place. ``os.replace`` is atomic on
POSIX and Windows for same-filesystem renames, so a reader never observes a
partially written file, and a crash mid-write leaves the original intact.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Union

BytesOrStr = Union[bytes, str]


def atomic_write(
    path: Union[str, os.PathLike[str]],
    data: BytesOrStr,
    *,
    encoding: str = "utf-8",
    mode: int | None = None,
    fsync: bool = True,
) -> None:
    """Atomically write ``data`` to ``path``.

    The write goes to a uniquely named temp file in the destination's parent
    directory and is moved into place with :func:`os.replace`. If the process
    crashes before the replace, the destination keeps its previous contents and
    only an orphan temp file may remain.

    Parameters
    ----------
    path:
        Destination file path.
    data:
        ``bytes`` written verbatim, or ``str`` encoded with ``encoding``.
    encoding:
        Encoding used when ``data`` is a ``str``.
    mode:
        Optional octal permission bits applied to the destination (e.g. ``0o600``).
    fsync:
        If ``True`` (default), flush and ``fsync`` the temp file before the
        rename for crash durability. Set ``False`` for speed when durability
        across power loss is not required.
    """
    dest = Path(path)
    parent = dest.parent
    parent.mkdir(parents=True, exist_ok=True)

    payload: bytes = data.encode(encoding) if isinstance(data, str) else data

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{dest.name}.", suffix=".tmp", dir=str(parent)
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            if fsync:
                os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp_name, mode)
        os.replace(tmp_name, dest)
    except BaseException:
        # Clean up the temp file on any failure; the destination is untouched.
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
