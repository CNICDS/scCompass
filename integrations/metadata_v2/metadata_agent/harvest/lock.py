from __future__ import annotations

import fcntl
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


class HarvestLock:
    def __init__(self, lock_path: str | Path):
        self._path = Path(lock_path)
        self._fd: int | None = None

    def acquire(self) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fd = os.open(str(self._path), os.O_CREAT | os.O_WRONLY)
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(self._fd, str(os.getpid()).encode())
            return True
        except OSError:
            logger.warning("Another harvest is running (lock: %s)", self._path)
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            return False

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
            try:
                self._path.unlink()
            except OSError:
                pass

    def __enter__(self) -> HarvestLock:
        if not self.acquire():
            raise RuntimeError(f"Could not acquire harvest lock: {self._path}")
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
