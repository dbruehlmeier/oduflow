"""Publication lifecycle shared by HTTP/CLI actions and the reconciliation loop."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from oduflow.docker_ops.client import get_client
from oduflow.errors import BusyError, ConflictError, FlowError, NotFoundError
from oduflow.locking import LockManager
from oduflow.naming import PROD_ENV_PREFIX, get_resource_name, validate_domain
from oduflow.settings import Settings, TeamSettings
from oduflow.tunnels import cloudflare, containers, ngrok, store

logger = logging.getLogger("oduflow")

# Only a network publication is granted. The Odoo database/user permissions
# and the dashboard's independent Share UI credentials are never touched.
_PREPARE_ODOO = """
import configparser, json
path = '/etc/odoo/odoo.conf'
config = configparser.RawConfigParser()
config.optionxform = str
if not config.read(path) or not config.has_section('options'):
    raise RuntimeError('Missing Odoo configuration')
if config.getint('options', 'workers', fallback=0) != 0:
    print(json.dumps({'workers': True}))
else:
    changed = not config.getboolean('options', 'proxy_mode', fallback=False)
    if changed:
        config.set('options', 'proxy_mode', 'True')
        with open(path, 'w') as stream:
            config.write(stream)
    print(json.dumps({'changed': changed}))
"""


def _config(settings: Settings) -> dict[str, Any]:
    config = asdict(settings.tunnel)
    provider = config["provider"]
    keys = {
        "none": (),
        "cloudflare_quick": ("cloudflared_image",),
        "cloudflare": (
            "cloudflared_image",
            "cloudflare_api_token",
            "cloudflare_account_id",
            "cloudflare_zone_id",
            "cloudflare_domain",
        ),
        "ngrok": ("ngrok_image", "ngrok_authtoken", "ngrok_domain"),
    }[provider]
    return {"provider": provider, **{key: config[key] for key in keys}}


def _payload(settings: Settings, record: dict[str, Any] | None) -> dict[str, Any]:
    record = record or {}
    current = not record or record.get("config") == _config(settings)
    state = record.get("status", "disabled")
    if not current and state != "error":
        state = "disabled" if settings.tunnel.provider == "none" else "starting"
    return {
        "provider": settings.tunnel.provider,
        "enabled": bool(record.get("desired")),
        "status": state,
        "url": record.get("url") if current and state == "published" else None,
        "hostname": record.get("hostname", ""),
        "error": record.get("error", ""),
    }


def status(settings: Settings, team: TeamSettings, env_name: str) -> dict[str, Any]:
    if not store.exists(settings):
        return _payload(settings, None)
    record = store.read(settings)["publications"].get(store.key(team.team_id, env_name))
    return _payload(settings, record)


def proxy_overrides(
    settings: Settings, team: TeamSettings, env_name: str
) -> dict[str, str]:
    """Keep HTTPS proxy support when a published environment is recreated."""
    if not store.exists(settings):
        return {}
    record = store.read(settings)["publications"].get(
        store.key(team.team_id, env_name), {}
    )
    return {"proxy_mode": "True"} if record.get("desired") else {}


def _odoo(client: Any, settings: Settings, team: TeamSettings, env_name: str) -> Any:
    if env_name.startswith(PROD_ENV_PREFIX):
        raise FlowError(
            "Public Odoo links are available for development environments only."
        )
    container = containers.get(
        client, get_resource_name(env_name, "odoo", settings.prefix, team.team_id)
    )
    if container is None or container.labels.get(settings.team_label) != team.team_id:
        raise NotFoundError(f"Environment '{env_name}' does not exist.")
    return container


def _prepare(container: Any, record: dict[str, Any]) -> None:
    if record.get("prepared_container") == container.id:
        return
    result = container.exec_run(["python3", "-c", _PREPARE_ODOO], user="0:0")
    if result.exit_code:
        raise FlowError("Cannot enable HTTPS proxy support in this Odoo container.")
    try:
        result_data = json.loads(result.output)
    except (ValueError, TypeError) as exc:
        raise FlowError("Cannot read the Odoo proxy configuration.") from exc
    if result_data.get("workers"):
        raise FlowError(
            "Public Odoo links require development worker mode (workers = 0). Update the environment configuration first."
        )
    if result_data.get("changed"):
        container.restart(timeout=10)
    record["prepared_container"] = container.id


def _stop(
    client: Any,
    record: dict[str, Any],
    registry: store.Registry,
    *,
    delete: bool = False,
) -> None:
    record["url"] = None
    record["status"] = "stopped" if record.get("desired") else "disabled"
    if record["provider"] == "ngrok":
        ngrok.stop(client, record, registry)
    else:
        cloudflare.stop(client, record, registry, delete=delete)


def _reconcile(
    client: Any,
    settings: Settings,
    team: TeamSettings,
    record: dict[str, Any],
    registry: store.Registry,
) -> None:
    record["error"] = ""
    active = _config(settings)
    # A token/image rotation in the same account can reuse the owned tunnel.
    # In particular, a revoked previous API token must not prevent recovery.
    if record["provider"] == active["provider"] == "cloudflare" and all(
        record["config"].get(key) == active.get(key)
        for key in ("cloudflare_account_id", "cloudflare_zone_id", "cloudflare_domain")
    ):
        record["config"] = active
    changed = record["config"] != active
    if changed or not record.get("desired") or settings.tunnel.provider == "none":
        _stop(client, record, registry, delete=True)
        if not record.get("desired"):
            registry.publications.pop(store.key(team.team_id, record["env_name"]), None)
            return
        if record.get("hostname_provider", record["provider"]) != active["provider"]:
            record["hostname"] = ""
        record.update(
            provider=active["provider"], config=active, status="disabled", url=None
        )
        if settings.tunnel.provider == "none":
            return
    # Close all old providers before opening any connector for the new one.
    if any(
        other.get("connector") and other["provider"] != active["provider"]
        for other in registry.publications.values()
    ):
        record["status"] = "starting"
        return
    try:
        container = _odoo(client, settings, team, record["env_name"])
    except NotFoundError:
        # Recreates hold the environment lock. A missing container observed
        # here is an abandoned/deleted environment, not an in-flight rebuild.
        record["desired"] = False
        registry.save()
        _stop(client, record, registry, delete=True)
        registry.publications.pop(store.key(team.team_id, record["env_name"]), None)
        return
    if container.status != "running":
        _stop(client, record, registry)
        return
    _prepare(container, record)
    upstream = f"http://{container.name}:8069"
    url: str | None
    if settings.tunnel.provider == "ngrok":
        url = ngrok.publish(client, settings, team, record, registry, upstream)
    else:
        url = cloudflare.publish(client, settings, team, record, registry, upstream)
    record.update(
        url=url, status="published" if url else "starting", failures=0, retry_at=0
    )


def _attempt(
    client: Any,
    settings: Settings,
    team: TeamSettings,
    record: dict[str, Any],
    registry: store.Registry,
) -> None:
    try:
        _reconcile(client, settings, team, record, registry)
    except Exception as exc:
        # Provider messages are deliberately bounded/scrubbed. Docker errors
        # can contain a complete create request, including credentials.
        message = (
            str(exc)
            if isinstance(exc, FlowError)
            else "Tunnel operation failed. Check Docker and the provider settings, then retry."
        )
        for config in (record.get("config", {}), asdict(settings.tunnel)):
            for key, value in config.items():
                if "token" in key and value:
                    message = message.replace(value, "[redacted]")
        failures = record.get("failures", 0) + 1
        record.update(
            status="error",
            error=message,
            url=None,
            failures=failures,
            retry_at=time.time() + min(60, 2 ** min(failures, 6)),
        )
        logger.warning(
            "Public Odoo link for team %s environment %s: %s",
            team.team_id,
            record["env_name"],
            message,
        )


def publish(
    settings: Settings, team: TeamSettings, env_name: str, hostname: str = ""
) -> dict[str, Any]:
    settings.tunnel.validate()
    if settings.tunnel.provider == "none":
        raise FlowError("Select a tunnel provider in Server settings → Tunnels first.")
    hostname = hostname.strip().lower()
    if hostname:
        try:
            hostname = validate_domain(hostname)
        except ValueError as exc:
            raise FlowError("Enter a hostname without a scheme, path or port.") from exc
        if settings.tunnel.provider == "cloudflare_quick":
            raise FlowError("Quick Tunnel assigns its own temporary hostname.")
        if settings.tunnel.provider == "cloudflare" and not hostname.endswith(
            "." + settings.tunnel.cloudflare_domain
        ):
            raise FlowError(
                "The hostname must be below the configured Cloudflare domain."
            )
    client = get_client()
    container = _odoo(client, settings, team, env_name)
    if container.status != "running":
        raise FlowError("Start the Odoo environment before publishing it.")
    with store.Registry(settings) as registry:
        key = store.key(team.team_id, env_name)
        requested_domain = hostname or (
            settings.tunnel.ngrok_domain if settings.tunnel.provider == "ngrok" else ""
        )
        if requested_domain:
            for other_key, other in registry.publications.items():
                other_domain = other.get("hostname") or other.get("config", {}).get(
                    "ngrok_domain"
                )
                if (
                    other_key != key
                    and other.get("desired")
                    and requested_domain == other_domain
                ):
                    raise ConflictError(
                        "This hostname is already assigned to another environment. Unpublish it or choose another hostname."
                    )
        record = registry.publications.get(key)
        if record is None:
            record = {
                "id": uuid.uuid4().hex,
                "team_id": team.team_id,
                "env_name": env_name,
                "provider": settings.tunnel.provider,
                "config": _config(settings),
            }
            registry.publications[key] = record
        elif record.get("hostname", "") != hostname:
            _stop(client, record, registry, delete=True)
        record.update(
            desired=True,
            hostname=hostname,
            hostname_provider=settings.tunnel.provider,
            status="starting",
            error="",
            retry_at=0,
        )
        registry.save()
        _attempt(client, settings, team, record, registry)
        return _payload(settings, record)


def unpublish(settings: Settings, team: TeamSettings, env_name: str) -> dict[str, Any]:
    if not store.exists(settings):
        return _payload(settings, None)
    with store.Registry(settings) as registry:
        record = registry.publications.get(store.key(team.team_id, env_name))
        if record is not None:
            record["desired"] = False
            registry.save()
            _attempt(get_client(), settings, team, record, registry)
            if record.get("status") == "error":
                return _payload(settings, record)
    return _payload(settings, None)


def pause_environment(
    settings: Settings, team: TeamSettings, env_name: str, *, delete: bool = False
) -> None:
    """Called under the environment lock, before removing/stopping Odoo.

    A true deletion revokes the desired publication before any local teardown:
    a later unrelated environment with the same name must never inherit it.
    A recreate keeps the desired state and the named Cloudflare resources.
    """
    if not store.exists(settings):
        return
    with store.Registry(settings) as registry:
        record = registry.publications.get(store.key(team.team_id, env_name))
        if record is None:
            return
        if delete:
            record["desired"] = False
            registry.save()
        _stop(get_client(), record, registry, delete=delete)
        if delete:
            registry.publications.pop(store.key(team.team_id, env_name), None)


def sweep(get_settings: Callable[[], Settings], locks: LockManager) -> None:
    settings = get_settings()
    if not store.exists(settings):
        return
    snapshot = store.read(settings)
    for key, item in snapshot["publications"].items():
        settings = get_settings()
        team = settings.teams.get(item["team_id"])
        if team is None:
            # A removed team still owns remote resources until they are cleaned.
            try:
                with store.Registry(settings) as registry:
                    record = registry.publications.get(key)
                    if record:
                        record["desired"] = False
                        _stop(get_client(), record, registry, delete=True)
                        registry.publications.pop(key, None)
            except Exception:
                logger.warning("Could not clean up a removed team's public Odoo link.")
            continue
        try:
            locks.acquire_env(item["env_name"], team.team_id, operation="tunnel")
        except BusyError:
            continue
        try:
            with store.Registry(settings) as registry:
                record = registry.publications.get(key)
                if record is None:
                    continue
                if record.get("retry_at", 0) > time.time() and record[
                    "config"
                ] == _config(settings):
                    continue
                _attempt(get_client(), settings, team, record, registry)
        finally:
            locks.release_env(item["env_name"])


class Worker:
    def __init__(self, get_settings: Callable[[], Settings], locks: LockManager):
        self.get_settings = get_settings
        self.locks = locks
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._run, name="oduflow-tunnels", daemon=True
        )

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=25)

    def _run(self) -> None:
        settings = self.get_settings()
        if not settings.base_data_dir:
            return
        directory = store.root(settings)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(directory / ".worker.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            while not self.stop_event.is_set():
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    self.stop_event.wait(5)
            else:
                return
            while not self.stop_event.is_set():
                try:
                    sweep(self.get_settings, self.locks)
                except Exception:
                    # Never log an external exception containing credentials.
                    logger.warning("Could not reconcile public Odoo links; will retry.")
                self.stop_event.wait(5)
            settings = self.get_settings()
            if store.exists(settings):
                with store.Registry(settings) as registry:
                    for record in registry.publications.values():
                        try:
                            _stop(get_client(), record, registry)
                        except Exception:
                            logger.warning(
                                "Could not stop a tunnel connector during shutdown."
                            )
        finally:
            os.close(fd)
