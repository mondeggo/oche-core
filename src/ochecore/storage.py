import json
import os
import tempfile
from pathlib import Path
from typing import Any

from ochecore.autodarts.errors import ConnectionProblem


def write_private_json(path: Path, data: dict[str, Any]) -> None:
    """Atomic replacement; new token files are owner-only on POSIX."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    except OSError as exc:
        raise ConnectionProblem("Cannot write application data. Check the data directory.") from exc


def remove_private_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise ConnectionProblem("Cannot remove the session. Check the data directory.") from exc
