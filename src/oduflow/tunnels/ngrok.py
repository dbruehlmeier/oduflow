"""One official ngrok agent per team, with dynamically managed endpoints."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, urlsplit

import yaml

from oduflow.errors import FlowError
from oduflow.settings import Settings, TeamSettings
from oduflow.tunnels import containers
from oduflow.tunnels.store import Registry

# Official ngrok images use Nix-store symlinks for passwd/group. Docker 29's
# exec user lookup rejects these with "path escapes from parent". Materialize
# the same contents before the agent starts; retain the official binary.
ENTRYPOINT = [
    "sh",
    "-ec",
    "for file in /etc/passwd /etc/group; do "
    'if [ -L "$file" ]; then cp -L "$file" "$file.oduflow"; '
    'mv -f "$file.oduflow" "$file"; fi; done; exec ngrok "$@"',
    "oduflow-ngrok",
]


def _error(data: dict[str, Any]) -> FlowError:
    # Never echo the agent's full response (it can contain account details).
    code = re.search(r"ERR_NGROK_\d+", str(data))
    suffix = f" ({code.group()})" if code else ""
    return FlowError(
        f"ngrok rejected the publication{suffix}. Check the authtoken, domain and account limits."
    )


def publish(
    client: Any,
    settings: Settings,
    team: TeamSettings,
    record: dict[str, Any],
    registry: Registry,
    upstream: str,
) -> str:
    instance = registry.data["instance"]
    team_key = containers.fingerprint(team.team_id)[:10]
    name = f"{settings.prefix}ngrok-{instance[:8]}-{team_key}"
    record["connector"] = name
    registry.save()
    config = yaml.safe_dump(
        {
            "version": "3",
            "agent": {
                "authtoken": settings.tunnel.ngrok_authtoken,
                "web_addr": "127.0.0.1:4040",
                "update_check": False,
                "log": "stdout",
                "log_format": "json",
            },
            "endpoints": [],
        }
    )
    container = containers.ensure(
        client,
        settings,
        team,
        instance,
        name,
        settings.tunnel.ngrok_image,
        ["start", "--none", "--config", "/run/oduflow/config"],
        config,
        entrypoint=ENTRYPOINT,
    )
    path = "/api/endpoints/" + quote(record["id"], safe="")
    status, data = containers.ngrok_request(container, "GET", path)
    if status == 200:
        if data.get("upstream", {}).get("url") != upstream:
            status, data = containers.ngrok_request(container, "DELETE", path)
            if status not in (200, 204, 404):
                raise _error(data)
            status = 404
    if status == 404:
        body: dict[str, Any] = {
            "name": record["id"],
            "upstream": {"url": upstream},
            "inspect": False,
        }
        domain = record.get("hostname") or settings.tunnel.ngrok_domain
        if domain:
            body["url"] = f"https://{domain}"
        status, data = containers.ngrok_request(
            container, "POST", "/api/endpoints", body
        )
    if status not in (200, 201):
        raise _error(data)
    url = str(data.get("url", ""))
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.port
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        raise FlowError("ngrok did not return a public HTTPS address.")
    return url.rstrip("/")


def stop(client: Any, record: dict[str, Any], registry: Registry) -> None:
    name = record.get("connector")
    if not name:
        return
    container = containers.get(client, name)
    if container is not None:
        if container.labels.get(containers.INSTANCE) != registry.data["instance"]:
            raise FlowError("The ngrok agent belongs to another installation.")
        if container.status == "running":
            try:
                status, data = containers.ngrok_request(
                    container,
                    "DELETE",
                    "/api/endpoints/" + quote(record["id"], safe=""),
                )
                if status not in (200, 204, 404):
                    raise _error(data)
            except Exception:
                # Revocation must still work if the agent API is broken. Other
                # desired endpoints are restored on the next reconciliation.
                containers.remove(client, name, registry.data["instance"])
                for other in registry.publications.values():
                    if other.get("connector") == name:
                        other.pop("connector", None)
                        other.update(url=None, status="starting", retry_at=0)
    record.pop("connector", None)
    # Removing the last endpoint also stops the idle agent session.
    if not any(
        other.get("connector") == name for other in registry.publications.values()
    ):
        containers.remove(client, name, registry.data["instance"])
