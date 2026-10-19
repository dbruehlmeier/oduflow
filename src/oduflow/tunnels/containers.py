"""Docker-only connector runtime, with no exposed management ports."""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import tarfile
from typing import Any

import docker
from oduflow.errors import FlowError
from oduflow.naming import get_team_network_name
from oduflow.settings import Settings, TeamSettings

ROLE = "oduflow.tunnel"
INSTANCE = "oduflow.tunnel.instance"
FINGERPRINT = "oduflow.tunnel.fingerprint"


def fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def get(client: Any, name: str) -> Any:
    try:
        return client.containers.get(name)
    except docker.errors.NotFound:
        return None


def remove(client: Any, name: str, instance: str) -> None:
    container = get(client, name)
    if container is None:
        return
    if container.labels.get(INSTANCE) != instance:
        raise FlowError(
            "A tunnel container name is already in use by another installation."
        )
    container.remove(force=True)


def ensure(
    client: Any,
    settings: Settings,
    team: TeamSettings,
    instance: str,
    name: str,
    image: str,
    command: list[str],
    config: str = "",
    *,
    entrypoint: list[str] | None = None,
) -> Any:
    signature = fingerprint([image, command, config, entrypoint])
    container = get(client, name)
    if container is not None:
        if container.labels.get(INSTANCE) != instance:
            raise FlowError(
                "A tunnel container name is already in use by another installation."
            )
        if container.labels.get(FINGERPRINT) != signature:
            container.remove(force=True)
            container = None
        elif container.status != "running":
            container.start()
    if container is not None:
        return container
    # Copy through the Docker API: host bind paths are not portable when
    # Oduflow itself runs in a container or uses a remote Docker daemon.
    try:
        client.images.get(image)
    except docker.errors.ImageNotFound:
        client.images.pull(image)
    container = client.containers.create(
        image,
        name=name,
        command=command,
        entrypoint=entrypoint,
        detach=True,
        network=get_team_network_name(team.team_id, settings.prefix),
        labels={
            settings.managed_label: "true",
            settings.team_label: team.team_id,
            ROLE: "true",
            INSTANCE: instance,
            FINGERPRINT: signature,
        },
        user="0:0",
        read_only=not bool(config),
        tmpfs={"/tmp": "rw,noexec,nosuid,size=16m"},
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"],
        restart_policy={"Name": "unless-stopped"},
        log_config={"type": "json-file", "config": {"max-size": "2m", "max-file": "2"}},
    )
    try:
        if config:
            archive = io.BytesIO()
            payload = config.encode()
            with tarfile.open(fileobj=archive, mode="w") as tar:
                directory = tarfile.TarInfo("run/oduflow")
                directory.type = tarfile.DIRTYPE
                directory.mode = 0o700
                tar.addfile(directory)
                entry = tarfile.TarInfo("run/oduflow/config")
                entry.size = len(payload)
                entry.mode = 0o600
                tar.addfile(entry, io.BytesIO(payload))
            if not container.put_archive("/", archive.getvalue()):
                raise FlowError("Cannot install the private tunnel configuration.")
        container.start()
        return container
    except Exception:
        container.remove(force=True)
        raise


class _ResponseSocket:
    def __init__(self, data: bytes):
        self.data = data

    def makefile(self, *args: object, **kwargs: object) -> io.BytesIO:
        return io.BytesIO(self.data)


def ngrok_request(
    container: Any, method: str, path: str, body: dict[str, Any] | None = None
) -> tuple[int, dict[str, Any]]:
    """Reach the agent's loopback API through Docker exec.

    The pinned official Alpine image includes BusyBox nc. Keeping web_addr on
    loopback avoids exposing ngrok's unauthenticated API to Odoo containers or
    the LAN. The HTTP request is a positional argument, never shell source.
    HTTPResponse handles both Content-Length and chunked agent responses.
    """
    payload = json.dumps(body).encode() if body is not None else b""
    request = (
        f"{method} {path} HTTP/1.1\r\nHost: localhost:4040\r\n"
        "Connection: close\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n\r\n"
    ).encode() + payload
    result = container.exec_run(
        [
            "sh",
            "-c",
            'printf "%s" "$1" | nc -w 10 127.0.0.1 4040',
            "oduflow-agent-api",
            request.decode(),
        ]
    )
    if result.exit_code:
        raise FlowError(
            "ngrok agent is not ready. Check the authtoken and connection, then retry."
        )
    try:
        response = http.client.HTTPResponse(_ResponseSocket(result.output))  # type: ignore[arg-type]
        response.begin()
        raw = response.read()
        data = json.loads(raw) if raw else {}
        if not isinstance(data, dict):
            raise ValueError("Expected an object")
        return response.status, data
    except (ValueError, http.client.HTTPException, OSError) as exc:
        raise FlowError("Cannot read the ngrok agent response.") from exc
