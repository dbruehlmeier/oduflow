"""A dashboard deploy that queues behind another operation on the production
must be visible while it waits, and leave a trace if it gives up (#309)."""

import time
from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow import production_registry, web_ui
from oduflow.docker_ops import production_ops
from oduflow.locking import LockManager, current_waiters, prod_lock_key
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui

KEY = prod_lock_key("1", "erp")


@pytest.fixture
def team(tmp_path):
    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team_1"))
    production_registry.create_production(
        team, "erp", {"domain": "erp.example.com", "branch": "main"}
    )
    return team


@pytest.fixture
def app(tmp_path, team):
    settings = Settings(
        base_data_dir=str(tmp_path), prod_enabled=True, teams={"1": team}
    )
    app = Starlette()
    locks = LockManager()
    mount_web_ui(app, lambda: settings, locks)
    return TestClient(app), locks


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_free_production_deploys_at_once(app):
    client, _locks = app
    with patch.object(production_ops, "update_production") as update:
        response = client.post("/api/productions/erp/update")
        assert _wait_for(lambda: update.called)

    assert response.status_code == 202
    assert response.json()["queued"] is False


def test_busy_production_queues_visibly_then_records_the_give_up(app, team):
    client, locks = app
    locks.acquire_env(KEY, operation="scheduled backup")
    try:
        with (
            patch.object(web_ui, "_UI_DEPLOY_WAIT_SECONDS", 0.5),
            patch.object(production_ops, "update_production") as update,
        ):
            response = client.post("/api/productions/erp/update")
            body = response.json()
            assert response.status_code == 202
            assert body["queued"] is True
            assert "scheduled backup" in body["busy"]

            # Listed as waiting while it waits...
            assert _wait_for(
                lambda: (
                    [w["operation"] for w in current_waiters(KEY)]
                    == ["dashboard deploy"]
                )
            )
            # ...and a second click does not stack another one behind it.
            again = client.post("/api/productions/erp/update")
            assert again.status_code == 409
            assert "already queued" in again.json()["error"]

            assert _wait_for(lambda: production_ops.read_deploys(team, "erp"))
        update.assert_not_called()
    finally:
        locks.release_env(KEY)

    assert current_waiters(KEY) == []
    [entry] = production_ops.read_deploys(team, "erp")
    assert entry["status"] == "skipped"
    assert entry["trigger"] == "ui"
    assert "scheduled backup" in entry["error"]
    # No commits: a skipped entry must never become a rollback target.
    assert entry["from_commit"] == ""


def test_productions_list_names_the_lock_holder(app):
    client, locks = app
    locks.acquire_env(KEY, operation="restart_production")
    try:
        with patch.object(
            production_ops, "list_productions", return_value=[{"name": "erp"}]
        ):
            prods = client.get("/api/productions").json()["productions"]
    finally:
        locks.release_env(KEY)

    assert prods[0]["busy"].startswith("restart_production, running for")
