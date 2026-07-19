import json
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml


def _atomic_replace(path: str, content: str) -> None:
    """Write content beside the destination, fsync it, then atomically replace."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=str(destination.parent),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def atomic_write_yaml(path: str, value: Any) -> None:
    content = yaml.safe_dump(
        value,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )
    _atomic_replace(path, content)


def atomic_write_json(path: str, value: Any) -> None:
    _atomic_replace(path, json.dumps(value, indent=2, sort_keys=True) + "\n")
