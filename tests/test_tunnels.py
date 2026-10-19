"""Public Odoo links: durable lifecycle, provider isolation and safe cleanup."""

from __future__ import annotations

import io
import json
import tarfile
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
import yaml

import docker
from oduflow import config_store
from oduflow.docker_ops.env_ops import get_env_base_url
from oduflow.errors import ConflictError, FlowError, NotFoundError
from oduflow.locking import LockManager
from oduflow.naming import get_resource_name, get_team_network_name
from oduflow.settings import Settings, TeamSettings, TunnelSettings
from oduflow.tunnels import cloudflare, containers, manager, ngrok, store


@pytest.fixture
def settings(tmp_path):
    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team"))
    return Settings(
        base_data_dir=str(tmp_path),
        teams={"1": team},
        tunnel=TunnelSettings(provider="cloudflare_quick"),
    )


@pytest.fixture
def runtime(settings, monkeypatch):
    team = settings.teams["1"]
    odoo = Mock(
        id="odoo-container-1", status="running", labels={settings.team_label: "1"}
    )
    odoo.name = get_resource_name("feature/x", "odoo", settings.prefix, "1")
    odoo.attrs = {"NetworkSettings": {"Ports": {"8069/tcp": [{"HostPort": "50001"}]}}}
    odoo.exec_run.return_value = SimpleNamespace(
        exit_code=0, output=b'{"changed":true}'
    )
    client = Mock()
    client.containers.get.return_value = odoo
    monkeypatch.setattr(manager, "get_client", lambda: client)
    events = []

    def publish(client, settings, team, record, registry, upstream):
        events.append(("publish", record["provider"], upstream))
        record["connector"] = "connector-" + record["id"]
        return "https://demo.example.com"

    def stop(client, record, registry, **kwargs):
        events.append(("stop", record["provider"], kwargs))
        record.pop("connector", None)

    monkeypatch.setattr(cloudflare, "publish", publish)
    monkeypatch.setattr(cloudflare, "stop", stop)
    monkeypatch.setattr(ngrok, "publish", publish)
    monkeypatch.setattr(ngrok, "stop", stop)
    return SimpleNamespace(client=client, odoo=odoo, team=team, events=events)


def test_publish_targets_odoo_and_prepares_proxy_once(settings, runtime):
    local = get_env_base_url(settings, runtime.team, "feature/x", runtime.odoo)
    result = manager.publish(settings, runtime.team, "feature/x")
    assert result["status"] == "published"
    assert runtime.events == [
        ("publish", "cloudflare_quick", f"http://{runtime.odoo.name}:8069")
    ]
    assert get_env_base_url(settings, runtime.team, "feature/x", runtime.odoo) == local
    runtime.odoo.restart.assert_called_once_with(timeout=10)
    manager.sweep(lambda: settings, LockManager())
    runtime.odoo.exec_run.assert_called_once()
    assert "config" not in manager.status(settings, runtime.team, "feature/x")
    assert (store.root(settings) / "registry.json").stat().st_mode & 0o777 == 0o600


def test_pause_recreate_preserves_identity_but_delete_revokes(settings, runtime):
    manager.publish(settings, runtime.team, "feature/x")
    identity = next(iter(store.read(settings)["publications"].values()))["id"]
    manager.pause_environment(settings, runtime.team, "feature/x")
    assert manager.status(settings, runtime.team, "feature/x")["url"] is None
    assert manager.proxy_overrides(settings, runtime.team, "feature/x") == {
        "proxy_mode": "True"
    }
    runtime.odoo.id = "recreated-odoo"
    manager.sweep(lambda: settings, LockManager())
    assert manager.status(settings, runtime.team, "feature/x")["status"] == "published"
    assert next(iter(store.read(settings)["publications"].values()))["id"] == identity
    assert runtime.odoo.restart.call_count == 2
    manager.pause_environment(settings, runtime.team, "feature/x", delete=True)
    assert store.read(settings)["publications"] == {}
    assert manager.proxy_overrides(settings, runtime.team, "feature/x") == {}
    runtime.events.clear()
    manager.sweep(lambda: settings, LockManager())
    assert runtime.events == []


def test_stopped_odoo_does_not_get_restarted_by_publication(settings, runtime):
    manager.publish(settings, runtime.team, "feature/x")
    runtime.odoo.status = "exited"
    runtime.odoo.restart.reset_mock()
    manager.sweep(lambda: settings, LockManager())
    assert manager.status(settings, runtime.team, "feature/x")["status"] == "stopped"
    runtime.odoo.restart.assert_not_called()
    with pytest.raises(FlowError, match="Start the Odoo"):
        manager.publish(settings, runtime.team, "feature/x")


def test_switch_closes_all_old_connectors_before_any_new_one(settings, runtime):
    manager.publish(settings, runtime.team, "feature/x")
    manager.publish(settings, runtime.team, "second")
    switched = replace(
        settings, tunnel=TunnelSettings(provider="ngrok", ngrok_authtoken="secret")
    )
    assert manager.status(switched, runtime.team, "feature/x")["url"] is None
    runtime.events.clear()
    manager.sweep(lambda: switched, LockManager())
    manager.sweep(lambda: switched, LockManager())
    assert [e[:2] for e in runtime.events[:3]] == [
        ("stop", "cloudflare_quick"),
        ("stop", "cloudflare_quick"),
        ("publish", "ngrok"),
    ]
    disabled = replace(switched, tunnel=TunnelSettings())
    manager.sweep(lambda: disabled, LockManager())
    assert all(
        not r.get("connector") for r in store.read(settings)["publications"].values()
    )
    assert manager.status(disabled, runtime.team, "second")["status"] == "disabled"


def test_busy_environment_is_not_reconciled(settings, runtime):
    manager.publish(settings, runtime.team, "feature/x")
    runtime.events.clear()
    locks = LockManager()
    locks.acquire_env("feature/x", "1", operation="recreate")
    try:
        manager.sweep(lambda: settings, locks)
    finally:
        locks.release_env("feature/x")
    assert runtime.events == []


def test_secret_errors_are_redacted_and_delete_is_durable(
    settings, runtime, monkeypatch
):
    settings = replace(
        settings,
        tunnel=TunnelSettings(provider="ngrok", ngrok_authtoken="secret-token"),
    )
    manager.publish(settings, runtime.team, "feature/x")
    monkeypatch.setattr(
        ngrok, "stop", Mock(side_effect=FlowError("Rejected secret-token"))
    )
    result = manager.unpublish(settings, runtime.team, "feature/x")
    assert result["status"] == "error"
    assert "secret-token" not in json.dumps(result)
    record = next(iter(store.read(settings)["publications"].values()))
    assert record["desired"] is False
    assert record["retry_at"] > 0


def test_worker_mode_refused_before_opening_public_route(settings, runtime):
    runtime.odoo.exec_run.return_value.output = b'{"workers":true}'
    result = manager.publish(settings, runtime.team, "feature/x")
    assert result["status"] == "error" and "workers = 0" in result["error"]
    assert runtime.events == []
    runtime.odoo.restart.assert_not_called()


def test_container_team_is_checked(settings, runtime):
    runtime.odoo.labels = {settings.team_label: "another-team"}
    with pytest.raises(NotFoundError):
        manager.publish(settings, runtime.team, "feature/x")


def test_reusing_default_ngrok_domain_is_refused(settings, runtime):
    settings = replace(
        settings,
        tunnel=TunnelSettings(
            provider="ngrok", ngrok_authtoken="secret", ngrok_domain="demo.ngrok.app"
        ),
    )
    manager.publish(settings, runtime.team, "feature/x")
    with pytest.raises(ConflictError, match="already assigned"):
        manager.publish(settings, runtime.team, "second")
    assert (
        manager.publish(settings, runtime.team, "second", "second.ngrok.app")["status"]
        == "published"
    )


@pytest.mark.parametrize(
    "hostname",
    [
        "http://evil.example",
        "demo.example/a",
        "*.example.com",
        "a.example:443",
        "a.example\r\nInjected: x",
    ],
)
def test_publication_rejects_non_hostnames(settings, runtime, hostname):
    with pytest.raises(FlowError):
        manager.publish(settings, runtime.team, "feature/x", hostname)
    assert runtime.events == []


@pytest.mark.parametrize(
    "section,match",
    [
        ({"provider": "invalid"}, "provider"),
        ({"provider": "ngrok"}, "ngrok_authtoken"),
        ({"provider": "cloudflare"}, "cloudflare_api_token"),
        ({"ngrok_domain": "https://foo.example"}, "domain"),
        ({"provider": 42}, "string"),
        ([], "table"),
    ],
)
def test_invalid_settings(section, match):
    with pytest.raises(ValueError, match=match):
        Settings.from_raw({"tunnel": section, "team": {"1": {}}}, "oduflow.toml")


def test_quick_settings_need_no_credentials():
    settings = Settings.from_raw(
        {"tunnel": {"provider": "cloudflare_quick"}, "team": {"1": {}}}, "oduflow.toml"
    )
    assert settings.tunnel.provider == "cloudflare_quick"
    assert "secret-value" not in repr(
        TunnelSettings(
            ngrok_authtoken="secret-value", cloudflare_api_token="secret-value"
        )
    )


def test_provider_and_credentials_apply_live_but_are_masked(settings):
    raw = {
        "tunnel": {
            "provider": "ngrok",
            "ngrok_authtoken": "private-ngrok",
            "cloudflare_api_token": "private-cloudflare",
        }
    }
    masked = json.dumps(config_store.mask_raw(raw))
    assert "private-ngrok" not in masked and "private-cloudflare" not in masked
    overlaid, changes = config_store.live_overlay({}, raw, settings)
    assert changes
    live = Settings.from_raw({**overlaid, "team": {"1": {}}}, "oduflow.toml")
    assert live.tunnel.provider == "ngrok"
    assert live.tunnel.ngrok_authtoken == "private-ngrok"


def test_corrupt_registry_is_not_overwritten(settings):
    path = store.root(settings) / "registry.json"
    path.parent.mkdir()
    path.write_text("broken")
    with pytest.raises(FlowError):
        with store.Registry(settings):
            pass
    assert path.read_text() == "broken"


def _record(settings, registry, **overrides):
    record = {
        "id": "a" * 32,
        "team_id": "1",
        "env_name": "feature_under/score",
        "provider": settings.tunnel.provider,
        "config": manager._config(settings),
        "desired": True,
        **overrides,
    }
    registry.publications[store.key("1", record["env_name"])] = record
    return record


def test_ngrok_loopback_transport_parses_chunked_and_avoids_shell_injection():
    container = Mock()
    container.exec_run.return_value = SimpleNamespace(
        exit_code=0,
        output=b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n",
    )
    malicious = "$(touch /tmp/never); `id`"
    assert containers.ngrok_request(
        container, "POST", "/api/endpoints", {"name": malicious}
    ) == (200, {})
    args = container.exec_run.call_args.args[0]
    assert malicious not in args[2]
    assert malicious in args[-1]
    assert "127.0.0.1 4040" in args[2]


def test_connector_has_no_host_ports_or_host_mounts(settings, monkeypatch):
    client = Mock()
    client.containers.get.side_effect = docker.errors.NotFound("missing")
    containers.ensure(
        client,
        settings,
        settings.teams["1"],
        "instance",
        "connector",
        "ngrok/image",
        ["start"],
        "private-token",
    )
    kwargs = client.containers.create.call_args.kwargs
    assert "ports" not in kwargs and "network_mode" not in kwargs
    assert kwargs["network"] == get_team_network_name("1", settings.prefix)
    assert "volumes" not in kwargs
    archive = client.containers.create.return_value.put_archive.call_args.args[1]
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        entry = tar.getmember("run/oduflow/config")
        assert entry.mode == 0o600
        assert tar.extractfile(entry).read() == b"private-token"
    assert "private-token" not in str(kwargs)


def test_ngrok_multiple_endpoints_use_one_agent_without_exposing_its_api(
    settings, monkeypatch
):
    settings = replace(
        settings, tunnel=TunnelSettings(provider="ngrok", ngrok_authtoken="token")
    )
    ensure = Mock(return_value=Mock())
    monkeypatch.setattr(containers, "ensure", ensure)
    api = Mock(
        side_effect=[
            (404, {}),
            (201, {"url": "https://one.ngrok.app"}),
            (404, {}),
            (201, {"url": "https://two.ngrok.app"}),
        ]
    )
    monkeypatch.setattr(containers, "ngrok_request", api)
    with store.Registry(settings) as registry:
        for name in ("one", "two"):
            record = _record(
                settings, registry, id=name, env_name=name, hostname=f"{name}.ngrok.app"
            )
            assert (
                ngrok.publish(
                    Mock(),
                    settings,
                    settings.teams["1"],
                    record,
                    registry,
                    f"http://odoo-{name}:8069",
                )
                == f"https://{name}.ngrok.app"
            )
        assert ensure.call_args_list[0].args[4] == ensure.call_args_list[1].args[4]
        config = yaml.safe_load(ensure.call_args.args[-1])
        assert config["agent"]["web_addr"] == "127.0.0.1:4040"
        posts = [call.args[3] for call in api.call_args_list if call.args[1] == "POST"]
        assert [p["upstream"]["url"] for p in posts] == [
            "http://odoo-one:8069",
            "http://odoo-two:8069",
        ]


def test_ngrok_revoke_removes_agent_when_api_fails(settings, monkeypatch):
    settings = replace(
        settings, tunnel=TunnelSettings(provider="ngrok", ngrok_authtoken="token")
    )
    with store.Registry(settings) as registry:
        first = _record(settings, registry, connector="agent", env_name="one")
        second = _record(settings, registry, connector="agent", env_name="two")
        agent = Mock(
            status="running", labels={containers.INSTANCE: registry.data["instance"]}
        )
        monkeypatch.setattr(containers, "get", lambda *args: agent)
        monkeypatch.setattr(
            containers, "ngrok_request", Mock(side_effect=FlowError("API unavailable"))
        )
        remove = Mock()
        monkeypatch.setattr(containers, "remove", remove)
        ngrok.stop(Mock(), first, registry)
        assert remove.called
        assert not first.get("connector") and not second.get("connector")
        assert second["desired"] and second["status"] == "starting"


def _managed(settings):
    return replace(
        settings,
        tunnel=TunnelSettings(
            provider="cloudflare",
            cloudflare_api_token="token",
            cloudflare_account_id="a" * 32,
            cloudflare_zone_id="b" * 32,
            cloudflare_domain="demo.example.com",
        ),
    )


def test_managed_cloudflare_creates_specific_odoo_route_and_owned_dns(
    settings, monkeypatch
):
    settings = _managed(settings)
    requests = Mock(
        side_effect=[
            [],
            {"id": "tunnel-id"},
            {},
            [],
            {"id": "dns-id"},
            "connector-token",
        ]
    )
    monkeypatch.setattr(cloudflare, "request", requests)
    connector = Mock(
        id="connector-id",
        status="running",
        attrs={"State": {"StartedAt": "2026-10-19T12:00:00Z"}},
    )
    connector.logs.return_value = b"Registered tunnel connection"
    ensure = Mock(return_value=connector)
    monkeypatch.setattr(containers, "ensure", ensure)
    with store.Registry(settings) as registry:
        record = _record(settings, registry)
        url = cloudflare.publish(
            Mock(), settings, settings.teams["1"], record, registry, "http://odoo:8069"
        )
        assert url == "https://feature-under-score-aaaaaaaa.demo.example.com"
        route = requests.call_args_list[2].kwargs["json"]["config"]["ingress"]
        assert route == [
            {"hostname": url[8:], "service": "http://odoo:8069"},
            {"service": "http_status:404"},
        ]
        dns = requests.call_args_list[4].kwargs["json"]
        assert dns["comment"] == f"oduflow:{registry.data['instance']}:{record['id']}"
        assert dns["content"] == "tunnel-id.cfargotunnel.com"
        assert "connector-token" not in str(ensure.call_args.args[6])
        assert ensure.call_args.args[-1] == "connector-token"


def test_cloudflare_revoke_recovers_lost_create_response_and_preserves_foreign_dns(
    settings, monkeypatch
):
    settings = _managed(settings)
    events = []
    monkeypatch.setattr(
        containers, "remove", lambda *args: events.append("connector-removed")
    )
    with store.Registry(settings) as registry:
        record = _record(
            settings,
            registry,
            connector="cf-agent",
            remote={"tunnel_name": "owned-name", "hostname": "demo.example.com"},
        )
        comment = f"oduflow:{registry.data['instance']}:{record['id']}"

        def request(config, method, path, **kwargs):
            events.append((method, path))
            if method == "GET" and path.endswith("cfd_tunnel"):
                return [{"id": "tunnel-id", "name": "owned-name"}]
            if method == "GET":
                return [
                    {
                        "id": "ours",
                        "comment": comment,
                        "content": "tunnel-id.cfargotunnel.com",
                    },
                    {
                        "id": "foreign",
                        "comment": "operator",
                        "content": "other.example.com",
                    },
                ]
            return None

        monkeypatch.setattr(cloudflare, "request", request)
        cloudflare.stop(Mock(), record, registry, delete=True)
        assert events[0] == "connector-removed"
        deletes = [e[1] for e in events[1:] if e[0] == "DELETE"]
        assert len(deletes) == 2
        assert deletes[0].endswith("/ours") and deletes[1].endswith("/tunnel-id")
        assert "remote" not in record


def test_cloudflare_error_does_not_echo_token_or_remote_body(monkeypatch):
    monkeypatch.setattr(
        httpx,
        "request",
        lambda *a, **k: httpx.Response(
            403, json={"success": False, "errors": [{"message": "private-token"}]}
        ),
    )
    with pytest.raises(FlowError) as exc:
        cloudflare.request(
            {"cloudflare_api_token": "private-token"}, "GET", "/accounts/test"
        )
    assert "private-token" not in str(exc.value)


def test_quick_url_read_is_limited_to_current_container_start(settings, monkeypatch):
    connector = Mock(
        id="connector-id",
        status="running",
        attrs={"State": {"StartedAt": "2026-10-19T12:00:00.123456789Z"}},
    )
    connector.logs.return_value = (
        b"https://new-url.trycloudflare.com\nRegistered tunnel connection"
    )
    monkeypatch.setattr(containers, "ensure", Mock(return_value=connector))
    with store.Registry(settings) as registry:
        record = _record(settings, registry)
        assert (
            cloudflare.publish(
                Mock(),
                settings,
                settings.teams["1"],
                record,
                registry,
                "http://odoo:8069",
            )
            == "https://new-url.trycloudflare.com"
        )
        assert connector.logs.call_args.kwargs["since"] > 0
        connector.logs.return_value = b""
        assert cloudflare._connection(connector, record) == (
            True,
            "https://new-url.trycloudflare.com",
        )
        connector.attrs["State"]["StartedAt"] = "2026-10-19T12:05:00Z"
        assert cloudflare._connection(connector, record) == (False, "")


def test_ngrok_entrypoint_materializes_symlinks_without_changing_account_contents(
    tmp_path,
):
    """Regression for Docker 29 refusing exec on the official Nix-built image."""
    import subprocess

    etc = tmp_path / "etc"
    etc.mkdir()
    for name in ("passwd", "group"):
        source = tmp_path / name
        source.write_text("original-" + name)
        (etc / name).symlink_to(source)
    script = (
        ngrok.ENTRYPOINT[2]
        .replace("/etc/", str(etc) + "/")
        .replace('exec ngrok "$@"', "true")
    )
    subprocess.run(["sh", "-ec", script], check=True)
    for name in ("passwd", "group"):
        assert not (etc / name).is_symlink()
        assert (etc / name).read_text() == "original-" + name
