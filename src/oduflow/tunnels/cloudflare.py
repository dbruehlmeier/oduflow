"""Quick URLs and API-provisioned named tunnels using cloudflared in Docker."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import httpx

from oduflow.errors import ConflictError, FlowError
from oduflow.naming import slugify_branch, validate_domain
from oduflow.settings import Settings, TeamSettings
from oduflow.tunnels import containers
from oduflow.tunnels.store import Registry

API = "https://api.cloudflare.com/client/v4"
_QUICK_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com\b")


def _connection(container: Any, record: dict[str, Any]) -> tuple[bool, str]:
    container.reload()
    started_at = container.attrs["State"]["StartedAt"]
    run = container.id + ":" + started_at
    if record.get("connector_run") != run:
        record.update(connector_run=run, connected=False, quick_url=None)
    if container.status != "running":
        raise FlowError(
            "cloudflared stopped. Check the provider credentials and outbound connectivity."
        )
    # Logs survive restarts. Cache a URL only for this particular process run,
    # so log rotation does not lose it and a restart cannot resurrect it.
    started = datetime.fromisoformat(started_at[:19]).replace(tzinfo=timezone.utc)
    logs = container.logs(tail=200, since=int(started.timestamp())).decode(
        "utf-8", errors="replace"
    )
    if "Registered tunnel connection" in logs:
        record["connected"] = True
    urls = _QUICK_URL.findall(logs)
    if urls:
        record["quick_url"] = urls[-1]
    return bool(record.get("connected")), str(record.get("quick_url") or "")


def request(config: dict[str, Any], method: str, path: str, **kwargs: Any) -> Any:
    try:
        response = httpx.request(
            method,
            API + path,
            headers={"Authorization": "Bearer " + config["cloudflare_api_token"]},
            timeout=20,
            **kwargs,
        )
        if method == "DELETE" and response.status_code == 404:
            return None
        data = response.json()
        if not response.is_success or not data.get("success"):
            raise FlowError(
                f"Cloudflare request failed (HTTP {response.status_code}). Check the API token permissions, account, zone and domain."
            )
        return data.get("result")
    except (httpx.HTTPError, ValueError) as exc:
        raise FlowError(
            "Cannot reach the Cloudflare API. Retry when the connection is available."
        ) from exc


def publish(
    client: Any,
    settings: Settings,
    team: TeamSettings,
    record: dict[str, Any],
    registry: Registry,
    upstream: str,
) -> str | None:
    name = f"{settings.prefix}tunnel-{record['id']}"
    record["connector"] = name
    registry.save()
    if record["provider"] == "cloudflare_quick":
        container = containers.ensure(
            client,
            settings,
            team,
            registry.data["instance"],
            name,
            settings.tunnel.cloudflared_image,
            ["tunnel", "--no-autoupdate", "--url", upstream],
        )
        connected, url = _connection(container, record)
        return url if connected and url else None

    config = record["config"]
    remote = record.setdefault("remote", {})
    account = f"/accounts/{config['cloudflare_account_id']}/cfd_tunnel"
    zone = f"/zones/{config['cloudflare_zone_id']}/dns_records"
    slug = (
        slugify_branch(record["env_name"]).replace("_", "-")[:30].strip("-") or "odoo"
    )
    hostname = validate_domain(
        record.get("hostname")
        or f"{slug}-{record['id'][:8]}.{config['cloudflare_domain']}"
    )
    # Persist a deterministic remote name before allocation, so a retry can
    # recover a create whose response was lost without making a second tunnel.
    tunnel_name = f"oduflow-{registry.data['instance'][:8]}-{record['id']}"
    remote.update(tunnel_name=tunnel_name, hostname=hostname)
    registry.save()
    if not remote.get("tunnel_id"):
        matches = request(
            config, "GET", account, params={"name": tunnel_name, "is_deleted": "false"}
        )
        exact = [item for item in matches if item.get("name") == tunnel_name]
        if exact:
            remote["tunnel_id"] = exact[0]["id"]
        else:
            created = request(
                config,
                "POST",
                account,
                json={"name": tunnel_name, "config_src": "cloudflare"},
            )
            remote["tunnel_id"] = created["id"]
        registry.save()
    tunnel_id = remote["tunnel_id"]
    route = {
        "config": {
            "ingress": [
                {"hostname": hostname, "service": upstream},
                {"service": "http_status:404"},
            ]
        }
    }
    signature = containers.fingerprint(route)
    if remote.get("route_signature") != signature:
        request(config, "PUT", f"{account}/{tunnel_id}/configurations", json=route)
        remote["route_signature"] = signature
        registry.save()
    if not remote.get("dns_id"):
        comment = f"oduflow:{registry.data['instance']}:{record['id']}"
        matches = request(config, "GET", zone, params={"name": hostname})
        owned = [
            item
            for item in matches
            if item.get("comment") == comment
            and item.get("content") == f"{tunnel_id}.cfargotunnel.com"
        ]
        if owned:
            remote["dns_id"] = owned[0]["id"]
        elif matches:
            raise ConflictError(
                "This Cloudflare hostname already has a DNS record. Choose another hostname."
            )
        else:
            created = request(
                config,
                "POST",
                zone,
                json={
                    "type": "CNAME",
                    "name": hostname,
                    "content": f"{tunnel_id}.cfargotunnel.com",
                    "proxied": True,
                    "comment": comment,
                },
            )
            remote["dns_id"] = created["id"]
        registry.save()
    if not remote.get("token"):
        remote["token"] = request(config, "GET", f"{account}/{tunnel_id}/token")
        registry.save()
    container = containers.ensure(
        client,
        settings,
        team,
        registry.data["instance"],
        name,
        settings.tunnel.cloudflared_image,
        ["tunnel", "--no-autoupdate", "run", "--token-file", "/run/oduflow/config"],
        remote["token"],
    )
    connected, _ = _connection(container, record)
    return f"https://{hostname}" if connected else None


def stop(
    client: Any, record: dict[str, Any], registry: Registry, *, delete: bool
) -> None:
    name = record.get("connector")
    if name:
        containers.remove(client, name, registry.data["instance"])
        record.pop("connector", None)
    if not delete or record["provider"] != "cloudflare":
        return
    config = record["config"]
    remote = record.get("remote", {})
    account = f"/accounts/{config['cloudflare_account_id']}/cfd_tunnel"
    zone = f"/zones/{config['cloudflare_zone_id']}/dns_records"
    # Recover allocations even when a create succeeded but its response was
    # lost. Never remove a DNS record which an operator has since repurposed.
    if not remote.get("tunnel_id") and remote.get("tunnel_name"):
        matches = request(
            config,
            "GET",
            account,
            params={"name": remote["tunnel_name"], "is_deleted": "false"},
        )
        for item in matches:
            if item.get("name") == remote["tunnel_name"]:
                remote["tunnel_id"] = item["id"]
                registry.save()
                break
    if remote.get("hostname"):
        matches = request(config, "GET", zone, params={"name": remote["hostname"]})
        comment = f"oduflow:{registry.data['instance']}:{record['id']}"
        for item in matches:
            if (
                item.get("comment") == comment
                and item.get("content") == f"{remote.get('tunnel_id')}.cfargotunnel.com"
            ):
                request(config, "DELETE", f"{zone}/{item['id']}")
        remote.pop("dns_id", None)
        remote.pop("hostname", None)
        registry.save()
    if remote.get("tunnel_id"):
        request(
            config,
            "DELETE",
            f"{account}/{remote['tunnel_id']}",
        )
        remote.pop("tunnel_id")
        remote.pop("tunnel_name", None)
        registry.save()
    record.pop("remote", None)
