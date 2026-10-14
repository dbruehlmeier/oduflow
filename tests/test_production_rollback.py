"""Update engine tests: auto code rollback on failed production deploys."""

import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from oduflow import production_registry
from oduflow.docker_ops import production_ops
from oduflow.git_ops import rev_parse
from oduflow.settings import Settings, TeamSettings


@pytest.fixture
def team(tmp_path):
    data_dir = tmp_path / "team_1"
    data_dir.mkdir()
    return TeamSettings(team_id="1", data_dir=str(data_dir))


@pytest.fixture
def settings(team, tmp_path):
    return Settings(
        routing_mode="traefik",
        acme_email="a@b.co",
        base_data_dir=str(tmp_path),
        teams={"1": team},
    )


def _git(cwd, *args):
    subprocess.run(
        ["git", "-C", cwd, *args],
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


@pytest.fixture
def prod_repo(team, tmp_path):
    """A production 'erp' with a two-commit git checkout in its workspace."""
    production_registry.create_production(
        team, "erp", {"domain": "erp.example.com", "branch": "main"}
    )
    repo_path = os.path.join(team.workspaces_dir, "prod-erp", "repo")
    os.makedirs(repo_path)
    subprocess.run(
        ["git", "init", "-q", "-b", "main", repo_path], check=True, capture_output=True
    )
    with open(os.path.join(repo_path, "a.py"), "w") as f:
        f.write("v1\n")
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-qm", "c1")
    old_head = rev_parse(repo_path)
    with open(os.path.join(repo_path, "a.py"), "w") as f:
        f.write("v2\n")
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-qm", "c2")
    new_head = rev_parse(repo_path)
    return repo_path, old_head, new_head


def _container():
    container = MagicMock()
    container.labels = {"oduflow.team": "1", "oduflow.prod": "true"}
    container.status = "running"
    return container


def _run_update(settings, team, pull_result, healthy_sequence, container):
    """Run update_production with pull/health mocked; return (result, health_mock)."""
    client = MagicMock()
    health = MagicMock(side_effect=healthy_sequence)
    with (
        patch.object(production_ops, "get_client", return_value=client),
        patch.object(production_ops, "_require_container", return_value=container),
        patch("oduflow.docker_ops.env_ops.pull_environment", return_value=pull_result),
        patch.object(production_ops, "wait_production_healthy", health),
        patch.object(production_ops, "reapply_prod_odoo_conf", return_value=True),
    ):
        result = production_ops.update_production(settings, team, "erp", trigger="test")
    return result, health


class TestUpdateSuccess:
    def test_successful_deploy_recorded(self, settings, team, prod_repo):
        repo_path, old_head, new_head = prod_repo
        container = _container()
        result, _ = _run_update(
            settings,
            team,
            {
                "action": "upgrade",
                "exit_code": 0,
                "changed_files": ["a.py"],
                "modules_upgraded": ["m"],
                "message": "ok",
            },
            [True],
            container,
        )
        assert result["action"] == "upgrade"
        assert result["commit"] == new_head
        deploys = production_ops.read_deploys(team, "erp")
        assert deploys[-1]["status"] == "success"
        record = production_registry.get_production(team, "erp")
        assert record["deploy_in_progress"] is False
        assert record["unhealthy"] is False

    def test_refresh_promoted_to_restart(self, settings, team, prod_repo):
        container = _container()
        result, _ = _run_update(
            settings,
            team,
            {
                "action": "refresh",
                "exit_code": 0,
                "changed_files": ["v.xml"],
                "message": "refresh",
            },
            [True],
            container,
        )
        assert result["action"] == "restart"
        container.restart.assert_called_once()

    def test_no_changes_is_not_a_deploy(self, settings, team, prod_repo):
        container = _container()
        before = len(production_ops.read_deploys(team, "erp", limit=0))
        result, health = _run_update(
            settings,
            team,
            {"action": "none", "message": "up to date"},
            [True],
            container,
        )
        assert result["action"] == "none"
        assert len(production_ops.read_deploys(team, "erp", limit=0)) == before
        health.assert_not_called()


class TestUpdateRollback:
    def test_failed_upgrade_rolls_code_back(self, settings, team, prod_repo):
        repo_path, old_head, new_head = prod_repo
        # Simulate: pull moved HEAD to new_head (it already is), upgrade failed.
        container = _container()
        result, _ = _run_update(
            settings,
            team,
            {
                "action": "upgrade",
                "exit_code": 1,
                "changed_files": ["a.py"],
                "output": "Traceback ...",
                "message": "failed",
            },
            [True],  # rollback health check succeeds
            container,
        )
        assert result["action"] == "rolled_back"
        # Code is back at... wait: from_commit was HEAD before pull. In this
        # test HEAD never moved (pull is mocked), so rollback resets to the
        # recorded old head == current head == new_head.
        assert rev_parse(repo_path) == new_head
        deploys = production_ops.read_deploys(team, "erp")
        assert deploys[-1]["status"] == "rolled_back"
        assert "NOT rolled back" in result["message"]  # DB warning present
        record = production_registry.get_production(team, "erp")
        assert record["unhealthy"] is False
        assert record["deploy_in_progress"] is False

    def test_health_failure_triggers_rollback(self, settings, team, prod_repo):
        container = _container()
        result, health = _run_update(
            settings,
            team,
            {
                "action": "restart",
                "exit_code": 0,
                "changed_files": ["a.py"],
                "message": "restarted",
            },
            [False, True],  # deploy verify fails, rollback verify succeeds
            container,
        )
        assert result["action"] == "rolled_back"
        assert health.call_count == 2

    def test_rollback_failure_marks_unhealthy(self, settings, team, prod_repo):
        container = _container()
        result, _ = _run_update(
            settings,
            team,
            {
                "action": "upgrade",
                "exit_code": 1,
                "changed_files": ["a.py"],
                "message": "failed",
            },
            [False, False],
            container,
        )
        assert result["action"] == "rollback_failed"
        deploys = production_ops.read_deploys(team, "erp")
        assert deploys[-1]["status"] == "rollback_failed"
        assert production_registry.get_production(team, "erp")["unhealthy"] is True

    def test_real_code_reset(self, settings, team, prod_repo):
        """End-to-end git behaviour: HEAD actually moves back on rollback."""
        repo_path, old_head, new_head = prod_repo
        container = _container()

        # Reset the checkout to old_head, then a fake pull that moves it to
        # new_head and fails — rollback must land back on old_head.
        from oduflow.git_ops import reset_hard

        reset_hard(repo_path, old_head)

        def fake_pull(settings_, team_, env_name, **kw):
            reset_hard(repo_path, new_head)
            return {
                "action": "upgrade",
                "exit_code": 1,
                "changed_files": ["a.py"],
                "message": "failed",
            }

        client = MagicMock()
        with (
            patch.object(production_ops, "get_client", return_value=client),
            patch.object(production_ops, "_require_container", return_value=container),
            patch("oduflow.docker_ops.env_ops.pull_environment", fake_pull),
            patch.object(production_ops, "wait_production_healthy", side_effect=[True]),
            patch.object(production_ops, "reapply_prod_odoo_conf", return_value=True),
        ):
            result = production_ops.update_production(settings, team, "erp")

        assert result["action"] == "rolled_back"
        assert rev_parse(repo_path) == old_head
        assert result["failed_commit"] == new_head


class TestManualRollback:
    def test_rollback_to_explicit_commit(self, settings, team, prod_repo):
        repo_path, old_head, new_head = prod_repo
        container = _container()
        client = MagicMock()
        with (
            patch.object(production_ops, "get_client", return_value=client),
            patch.object(production_ops, "_require_container", return_value=container),
            patch.object(production_ops, "wait_production_healthy", return_value=True),
            patch.object(production_ops, "reapply_prod_odoo_conf", return_value=True),
        ):
            result = production_ops.rollback_production(settings, team, "erp", old_head)
        assert result["healthy"] is True
        assert rev_parse(repo_path) == old_head
        deploys = production_ops.read_deploys(team, "erp")
        assert deploys[-1]["action"] == "rollback"

    def test_rollback_unknown_commit_raises(self, settings, team, prod_repo):
        from oduflow.errors import NotFoundError

        container = _container()
        client = MagicMock()
        with (
            patch.object(production_ops, "get_client", return_value=client),
            patch.object(production_ops, "_require_container", return_value=container),
        ):
            with pytest.raises(NotFoundError, match="not found"):
                production_ops.rollback_production(settings, team, "erp", "0" * 40)


class TestProductionDatabaseRouting:
    def test_upgrade_preflight_uses_production_cluster(self, settings, team, prod_repo):
        from oduflow.docker_ops import odoo_ops

        client = MagicMock()
        database = MagicMock()
        database.exec_run.return_value = (0, b"name,state\noduflow,installed\n")
        client.containers.get.return_value = database
        container = _container()

        def fake_pull(scoped, selected_team, env_name, **kwargs):
            assert scoped is not settings
            assert scoped.shared_db_container == settings.prod_db_container
            assert selected_team is team
            assert env_name == "prod-erp"
            assert kwargs["upgrade"] == ["oduflow"]
            odoo_ops._require_upgradeable_modules(
                scoped, selected_team, env_name, ("oduflow",)
            )
            return {
                "action": "upgrade",
                "exit_code": 0,
                "modules_upgraded": ["oduflow"],
            }

        with (
            patch.object(production_ops, "get_client", return_value=client),
            patch.object(production_ops, "_require_container", return_value=container),
            patch.object(production_ops, "wait_production_healthy", return_value=True),
            patch.object(production_ops, "_has_routing_drift", return_value=False),
            patch("oduflow.docker_ops.env_ops.pull_environment", side_effect=fake_pull),
            patch.object(odoo_ops, "get_client", return_value=client),
            patch.object(
                odoo_ops, "load_credentials", return_value={"pg_user": "u_1_prod-erp"}
            ),
        ):
            result = production_ops.update_production(
                settings, team, "erp", upgrade=["oduflow"]
            )

        assert result["action"] == "upgrade"
        client.containers.get.assert_called_once_with(settings.prod_db_container)
        command = database.exec_run.call_args.args[0]
        assert command[command.index("-U") + 1] == "u_1_prod-erp"
        assert command[command.index("-d") + 1] == "oduflow_1_prod-erp"
        assert settings.shared_db_container == "oduflow-db"

    @pytest.mark.parametrize("healthy", [True, False])
    def test_exception_after_pull_rolls_back_code(
        self, settings, team, prod_repo, healthy
    ):
        from oduflow.errors import ExternalCommandError
        from oduflow.git_ops import reset_hard

        repo_path, old_head, new_head = prod_repo
        reset_hard(repo_path, old_head)
        container = _container()

        def fake_pull(*args, **kwargs):
            reset_hard(repo_path, new_head)
            raise ExternalCommandError("psql", 2, "fixture preflight failure")

        with (
            patch.object(production_ops, "get_client", return_value=MagicMock()),
            patch.object(production_ops, "_require_container", return_value=container),
            patch("oduflow.docker_ops.env_ops.pull_environment", side_effect=fake_pull),
            patch.object(
                production_ops, "wait_production_healthy", return_value=healthy
            ),
            patch.object(production_ops, "reapply_prod_odoo_conf", return_value=True),
        ):
            result = production_ops.update_production(
                settings, team, "erp", upgrade=["oduflow"]
            )

        assert result["action"] == ("rolled_back" if healthy else "rollback_failed")
        assert rev_parse(repo_path) == old_head
        assert result["failed_commit"] == new_head
        assert result["exit_code"] == 1
        assert "fixture preflight failure" in result["output"]
        record = production_registry.get_production(team, "erp")
        assert record["deploy_in_progress"] is False
        assert record["unhealthy"] is not healthy
        deploy = production_ops.read_deploys(team, "erp")[-1]
        assert deploy["from_commit"] == old_head
        assert deploy["to_commit"] == new_head
        container.restart.assert_called_once()


def _deploy(settings, team, fake_pull, hook, **kwargs):
    """Run update_production with *fake_pull* standing in for the pull engine."""
    with (
        patch.object(production_ops, "pre_update_hooks", [hook]),
        patch.object(production_ops, "get_client", return_value=MagicMock()),
        patch.object(production_ops, "_require_container", return_value=_container()),
        patch("oduflow.docker_ops.env_ops.pull_environment", side_effect=fake_pull),
        patch.object(production_ops, "wait_production_healthy", return_value=True),
        patch.object(production_ops, "_has_routing_drift", return_value=False),
    ):
        return production_ops.update_production(settings, team, "erp", **kwargs)


class TestPreUpdateHooks:
    """Issue #309: the pre-update snapshot (minutes on a large filestore) runs
    only when the deploy has something to apply, against the pre-deploy code."""

    def test_nothing_to_apply_skips_hooks_and_clears_stale_flag(
        self, settings, team, prod_repo
    ):
        _repo, _old, new_head = prod_repo
        # A deploy thread that died without cleanup left the flag behind; a
        # no-op deploy is what clears it.
        production_registry.update_production(team, "erp", {"deploy_in_progress": True})
        hook = MagicMock()

        def fake_pull(*args, before_apply=None, **kw):
            return {"action": "none", "message": "Already up to date."}

        result = _deploy(settings, team, fake_pull, hook)

        hook.assert_not_called()
        assert result["action"] == "none"
        assert result["commit"] == new_head
        assert production_ops.read_deploys(team, "erp", limit=0) == []
        assert not production_registry.get_production(team, "erp").get(
            "deploy_in_progress"
        )

    def test_hooks_see_the_pre_deploy_commit(self, settings, team, prod_repo):
        from oduflow.git_ops import reset_hard

        repo_path, old_head, new_head = prod_repo
        reset_hard(repo_path, old_head)
        seen: list[str] = []

        def hook(*args):
            seen.append(rev_parse(repo_path))

        def fake_pull(*args, before_apply=None, **kw):
            reset_hard(repo_path, new_head)  # the pull moved the checkout
            before_apply()
            # The apply step must find the pulled code back in place.
            assert rev_parse(repo_path) == new_head
            return {"action": "upgrade", "exit_code": 0, "changed_files": ["a.py"]}

        result = _deploy(settings, team, fake_pull, hook)

        # The snapshot records the commit its database ran against, and the
        # running server never sees the new code while it is taken.
        assert seen == [old_head]
        assert result["commit"] == new_head
        assert result["deploy"]["to_commit"] == new_head

    def test_failing_hook_still_restores_the_pulled_code(
        self, settings, team, prod_repo
    ):
        from oduflow.git_ops import reset_hard

        repo_path, old_head, new_head = prod_repo
        reset_hard(repo_path, old_head)

        def fake_pull(*args, before_apply=None, **kw):
            reset_hard(repo_path, new_head)
            before_apply()
            return {"action": "restart", "exit_code": 0, "changed_files": ["a.py"]}

        result = _deploy(
            settings, team, fake_pull, MagicMock(side_effect=RuntimeError("s3 down"))
        )

        assert result["commit"] == new_head
        assert rev_parse(repo_path) == new_head

    def test_progress_is_reported_and_cleared(self, settings, team, prod_repo):
        from oduflow.locking import current_progress, prod_lock_key

        key = prod_lock_key(team.team_id, "erp")
        phases: list[str] = []

        def hook(*args):
            phases.append(str(current_progress(key)["phase"]))

        def fake_pull(*args, before_apply=None, **kw):
            phases.append(str(current_progress(key)["phase"]))
            before_apply()
            phases.append(str(current_progress(key)["phase"]))
            return {"action": "restart", "exit_code": 0, "changed_files": ["a.py"]}

        _deploy(settings, team, fake_pull, hook)

        assert phases == ["fetching code", "fetching code", "applying changes"]
        assert current_progress(key) is None
