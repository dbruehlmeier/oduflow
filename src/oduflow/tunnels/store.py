"""Atomic, private publication registry; independent of dashboard share keys."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any

from oduflow.errors import FlowError
from oduflow.settings import Settings

_mutex = threading.RLock()


def root(settings: Settings) -> Path:
    if not settings.base_data_dir:
        raise FlowError("A data directory is required for public Odoo links.")
    return Path(settings.base_data_dir) / ".tunnels"


def exists(settings: Settings) -> bool:
    return bool(settings.base_data_dir) and (root(settings) / "registry.json").is_file()


def key(team_id: str, env_name: str) -> str:
    return json.dumps([team_id, env_name], separators=(",", ":"))


def read(settings: Settings) -> dict[str, Any]:
    path = root(settings) / "registry.json"
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {"version": 1, "instance": uuid.uuid4().hex, "publications": {}}
    except (ValueError, OSError) as exc:
        raise FlowError("Cannot read the public Odoo link registry.") from exc
    if (
        not isinstance(data, dict)
        or data.get("version") != 1
        or not isinstance(data.get("publications"), dict)
        or not isinstance(data.get("instance"), str)
    ):
        raise FlowError("Invalid public Odoo link registry; restore it from backup.")
    return data


def atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tunnel-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class Registry:
    """Serialize provider side effects with their durable checkpoints.

    File locks also cover sibling CLI processes. Saving after each remote
    allocation lets a failed operation be resumed or revoked on the next sweep.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.data: dict[str, Any] = {}
        self.fd: int | None = None

    def __enter__(self) -> Registry:
        _mutex.acquire()
        try:
            directory = root(self.settings)
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.fd = os.open(directory / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(self.fd, fcntl.LOCK_EX)
            self.data = read(self.settings)
            return self
        except BaseException:
            if self.fd is not None:
                os.close(self.fd)
            _mutex.release()
            raise

    @property
    def publications(self) -> dict[str, Any]:
        return self.data["publications"]  # type: ignore[no-any-return]

    def save(self) -> None:
        atomic_write(
            root(self.settings) / "registry.json",
            json.dumps(self.data, indent=2) + "\n",
        )

    def __exit__(self, *args: object) -> None:
        try:
            self.save()
        finally:
            if self.fd is not None:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
                os.close(self.fd)
            _mutex.release()
