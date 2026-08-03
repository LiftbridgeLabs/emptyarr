import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterator, Optional


_condition = threading.Condition()
_active_operations: dict[str, str] = {}
_recovery_check: Optional[Callable[[str, str], bool]] = None


def set_recovery_check(check: Callable[[str, str], bool]) -> None:
    global _recovery_check
    _recovery_check = check


def recovery_required(instance_name: str, operation: str) -> bool:
    return bool(_recovery_check and _recovery_check(instance_name, operation))


@contextmanager
def lease(instance_name: str, allow_recovery: bool = False,
          operation: str = "maintenance", queue_empty_trash: bool = False,
          wait_timeout: float = 1800) -> Iterator[tuple[bool, str]]:
    """Acquire the global Plex maintenance lease.

    Empty Trash runs wait for active maintenance so schedules are serialized
    instead of reported as safety failures. Timestamp repair and recovery fail
    immediately on conflicts, keeping filesystem changes mutually exclusive
    with destructive work.
    """
    if not allow_recovery and recovery_required(instance_name, operation):
        yield False, "timestamp repair recovery is required"
        return

    acquired = False
    failure_reason = ""
    deadline = time.monotonic() + max(0, wait_timeout)
    with _condition:
        while instance_name in _active_operations:
            can_wait = queue_empty_trash and operation == "empty_trash"
            if not can_wait:
                failure_reason = (
                    "another maintenance operation is active on this Plex server"
                )
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure_reason = "timed out waiting for another Empty Trash run"
                break
            _condition.wait(remaining)
        if not failure_reason:
            _active_operations[instance_name] = operation
            acquired = True

    if failure_reason:
        yield False, failure_reason
        return

    try:
        if not allow_recovery and recovery_required(instance_name, operation):
            yield False, "timestamp repair recovery is required"
        else:
            yield True, ""
    finally:
        if acquired:
            with _condition:
                _active_operations.pop(instance_name, None)
                _condition.notify_all()
