"""Only the authenticated team operator may publish an Odoo service."""

from unittest.mock import Mock

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow import env_share, web_ui
from oduflow.errors import FlowError
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings, TunnelSettings
from oduflow.tunnels import manager, store


@pytest.fixture
def web(tmp_path, monkeypatch):
    settings = Settings(
        base_data_dir=str(tmp_path),
        tunnel=TunnelSettings(provider="ngrok", ngrok_authtoken="agent-secret"),
        teams={
            "1": TeamSettings(
                team_id="1", ui_password="password", data_dir=str(tmp_path / "1")
            )
        },
    )
    locks = LockManager()
    app = Starlette()
    web_ui.mount_web_ui(app, lambda: settings, locks)
    monkeypatch.setattr(web_ui.env_ops, "require_environment", lambda *a: None)
    client = TestClient(app)
    assert (
        client.post(
            "/login", data={"password": "password"}, follow_redirects=False
        ).status_code
        == 303
    )
    return settings, locks, app, client


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
def test_publication_requires_operator_session(web, method):
    _, _, app, _ = web
    assert (
        TestClient(app)
        .request(method, "/api/environments/feature/x/public-odoo", json={})
        .status_code
        == 401
    )


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
def test_shared_dashboard_cannot_manage_or_read_publication(web, method):
    _, _, app, client = web
    share = client.post("/api/environments/feature/x/share").json()["result"]["url"]
    visitor = TestClient(app)
    assert visitor.get("/" + share.split("/", 3)[3]).status_code == 200
    response = visitor.request(
        method, "/api/environments/feature/x/public-odoo", json={}
    )
    assert response.status_code == 403


def test_publish_uses_authenticated_team_and_environment_lock(web, monkeypatch):
    settings, locks, _, client = web

    def publish(s, team, name, hostname):
        assert s is settings and team is settings.teams["1"]
        assert name == "feature/x" and hostname == "demo.example.com"
        assert locks.active_operations()
        return {"status": "starting", "url": None}

    monkeypatch.setattr(web_ui.tunnel_manager, "publish", publish)
    response = client.post(
        "/api/environments/feature/x/public-odoo", json={"hostname": "demo.example.com"}
    )
    assert (
        response.status_code == 200
        and response.json()["result"]["status"] == "starting"
    )
    assert response.headers["cache-control"] == "no-store"
    assert not locks.active_operations()


@pytest.mark.parametrize(
    "body",
    [[], {"hostname": 2}, {"upstream": "http://dashboard:8000"}, {"team_id": "other"}],
)
def test_operator_cannot_choose_arbitrary_target_or_team(web, body, monkeypatch):
    _, locks, _, client = web
    publish = Mock()
    monkeypatch.setattr(web_ui.tunnel_manager, "publish", publish)
    assert (
        client.post("/api/environments/feature/x/public-odoo", json=body).status_code
        == 400
    )
    publish.assert_not_called()
    assert not locks.active_operations()


def test_error_releases_lock_and_is_retryable(web, monkeypatch):
    _, locks, _, client = web
    monkeypatch.setattr(
        web_ui.tunnel_manager,
        "publish",
        Mock(side_effect=FlowError("Start Odoo first")),
    )
    response = client.post("/api/environments/feature/x/public-odoo", json={})
    assert response.status_code >= 400
    assert response.json()["error"] == "Start Odoo first"
    assert not locks.active_operations()


def test_cross_origin_mutation_is_rejected(web, monkeypatch):
    _, _, _, client = web
    publish = Mock()
    monkeypatch.setattr(web_ui.tunnel_manager, "publish", publish)
    response = client.post(
        "/api/environments/feature/x/public-odoo",
        json={},
        headers={"Origin": "https://another.example"},
    )
    assert response.status_code == 403
    publish.assert_not_called()


def test_busy_environment_returns_conflict(web, monkeypatch):
    _, locks, _, client = web
    publish = Mock()
    monkeypatch.setattr(web_ui.tunnel_manager, "publish", publish)
    locks.acquire_env("feature/x", "1", operation="recreate")
    try:
        response = client.post("/api/environments/feature/x/public-odoo", json={})
        assert response.status_code == 409
        publish.assert_not_called()
    finally:
        locks.release_env("feature/x")


def _list_cards(web, monkeypatch):
    _, _, _, client = web
    monkeypatch.setattr(
        web_ui.env_ops,
        "list_environments",
        lambda *args: [
            {"env_name": name, "branch": name, "containers": []}
            for name in ("feature/x", "other")
        ],
    )
    return client


def test_card_indicators_are_team_scoped_and_exclude_credentials(web, monkeypatch):
    settings, _, _, _ = web
    client = _list_cards(web, monkeypatch)
    with store.Registry(settings) as registry:
        for team_id, name in (("1", "feature/x"), ("2", "other")):
            registry.publications[store.key(team_id, name)] = {
                "team_id": team_id,
                "env_name": name,
                "config": manager._config(settings),
                "desired": True,
                "status": "published",
                "url": "https://private-publication.example.com",
                "remote": {"token": "connector-secret"},
            }
    share_key = env_share.create_or_get(settings.teams["1"], "feature/x")
    other_team = TeamSettings(team_id="2", data_dir=settings.base_data_dir + "/2")
    other_key = env_share.create_or_get(other_team, "other")
    read = Mock(wraps=store.read)
    monkeypatch.setattr(store, "read", read)

    response = client.get("/api/environments")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    cards = response.json()["environments"]
    assert cards[0]["public_odoo"] == {"enabled": True, "status": "published"}
    assert cards[0]["ui_shared"] is True
    assert cards[1]["public_odoo"] == {"enabled": False, "status": "disabled"}
    assert cards[1]["ui_shared"] is False
    assert read.call_count == 1
    for private in (
        share_key,
        other_key,
        "agent-secret",
        "connector-secret",
        "https://",
    ):
        assert private not in response.text


def test_card_share_indicator_follows_create_and_revoke(web, monkeypatch):
    client = _list_cards(web, monkeypatch)

    def shared():
        return client.get("/api/environments").json()["environments"][0]["ui_shared"]

    assert shared() is False
    assert client.post("/api/environments/feature/x/share").status_code == 200
    assert shared() is True
    assert client.post("/api/environments/feature/x/share/revoke").status_code == 200
    assert shared() is False


def test_unreadable_tunnel_registry_keeps_cards_with_unknown_status(web, monkeypatch):
    settings, _, _, _ = web
    client = _list_cards(web, monkeypatch)
    store.atomic_write(store.root(settings) / "registry.json", "broken json")

    response = client.get("/api/environments")

    assert response.status_code == 200
    assert all(
        env["public_odoo"] == {"enabled": None, "status": "unknown"}
        for env in response.json()["environments"]
    )


def test_shared_cards_do_not_expose_operator_access_indicators(web, monkeypatch):
    _, _, app, _ = web
    client = _list_cards(web, monkeypatch)
    share = client.post("/api/environments/feature/x/share").json()["result"]["url"]
    visitor = TestClient(app)
    assert visitor.get("/" + share.split("/", 3)[3]).status_code == 200
    summaries = Mock(side_effect=AssertionError("Scoped visitors cannot read tunnels"))
    monkeypatch.setattr(manager, "summaries", summaries)

    response = visitor.get("/api/environments")

    assert response.status_code == 200
    assert response.json()["environments"] == [
        {"env_name": "feature/x", "branch": "feature/x", "containers": []}
    ]
    summaries.assert_not_called()
