"""Atomic replacement with unique temporary files and explicit durability."""

from __future__ import annotations

import os
import tempfile
import stat
import time
import random
from pathlib import Path


def write_text(path: Path, text: str, *, durable: bool = True) -> None:
    """Replace a UTF-8 file without exposing a partial write.

The temporary file shares the destination filesystem; concurrent writers
cannot clobber each other's temporary files. Callers still need transaction
locking for read/modify/write operations. A directory fsync is not portable
on Windows, so power-loss durability depends on the filesystem.
    """
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            if durable:
                os.fsync(stream.fileno())
        if path.is_file():
            os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        for attempt in range(4):
            try:
                os.replace(temporary, path)
                break
            except PermissionError as exc:
                # Windows readers/antivirus can briefly deny replacement.
                # Permanent denial remains visible after this bounded retry.
                if getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 3:
                    raise
                time.sleep(random.uniform(0.005, 0.02 * 2**attempt))
    finally:
        Path(temporary).unlink(missing_ok=True)
