import threading
from contextlib import contextmanager
from typing import Callable, Iterator, Optional


_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}
_recovery_check: Optional[Callable[[str], bool]] = None


def set_recovery_check(check: Callable[[str], bool]) -> None:
    global _recovery_check
    _recovery_check = check


def recovery_required(instance_name: str) -> bool:
    return bool(_recovery_check and _recovery_check(instance_name))


@contextmanager
def lease(instance_name: str, allow_recovery: bool = False) -> Iterator[tuple[bool, str]]:
    """Acquire the shared global Plex maintenance lock without waiting."""
    if not allow_recovery and recovery_required(instance_name):
        yield False, "timestamp repair recovery is required"
        return
    with _guard:
        # Phase 1 intentionally serializes maintenance across every Plex server.
        lock = _locks.setdefault("global", threading.Lock())
    if not lock.acquire(blocking=False):
        yield False, "another Plex maintenance operation is active"
        return
    try:
        if not allow_recovery and recovery_required(instance_name):
            yield False, "timestamp repair recovery is required"
        else:
            yield True, ""
    finally:
        lock.release()
