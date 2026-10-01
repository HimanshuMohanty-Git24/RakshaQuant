"""
Single-instance guard: at most one RakshaQuant process per state directory.

Two processes on the same ``state_dir`` (say, the CLI and the web console) would overwrite
each other's state. The guard is an OS-level lock on ``<state_dir>/rakshaquant.lock``, so it
is released automatically if the holder crashes. Different environments use different state
directories and so never block each other.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, Timeout

LOCK_FILE_NAME = "rakshaquant.lock"


class InstanceLockHeldError(RuntimeError):
    """Another process already holds the lock for this state directory."""


@contextmanager
def single_instance(state_dir: Path) -> Iterator[Path]:
    """Hold the state directory's lock for the duration of the block.

    Raises :class:`InstanceLockHeldError` at once, without waiting, if another holder exists.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / LOCK_FILE_NAME
    lock = FileLock(lock_path, blocking=False)
    try:
        lock.acquire()
    except Timeout:
        raise InstanceLockHeldError(
            f"Another RakshaQuant instance is already running on {state_dir} "
            f"(lock file {lock_path}). Stop it first, or use a different ENVIRONMENT."
        ) from None
    try:
        yield lock_path
    finally:
        lock.release()
