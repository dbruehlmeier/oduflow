"""Only the authenticated team operator may publish an Odoo service."""

from unittest.mock import Mock

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow import web_ui
from oduflow.errors import FlowError
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings


@pytest.fixture
def web(tmp_path, monkeypatch):
    settings = Settings(
        base_data_dir=str(tmp_path),
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
