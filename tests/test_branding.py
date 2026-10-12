"""White label under a custom license (oduflow.branding)."""

from __future__ import annotations

import base64
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow import branding, git_ops, licensing
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui
from tests.test_licensing import TestVerifyLicenseText

_PW = "s3cret"
_DATA_DIR = tempfile.mkdtemp(prefix="oduflow-branding-test-")
_PNG = b"\x89PNG\r\n\x1a\ncustom-logo"


@pytest.fixture(autouse=True)
def _reset_branding():
    yield
    branding._get_settings = None
    branding._cached = None


@pytest.fixture
def sign(monkeypatch):
    private, pem = TestVerifyLicenseText._keypair()
    monkeypatch.setattr(licensing, "_PUBLIC_KEY_PEM", pem)
    return lambda payload: TestVerifyLicenseText._sign(private, payload)


def _custom(**changes):
    return {
        "version": 4,
        "type": "custom",
        "plan": "custom",
        "license_id": "11111111-2222-4333-8444-555555555555",
        "name": "Acme Cloud",
        "email": "ops@acme.example",
        "brand_name": "AcmeFlow",
        "domains": ["acme-cloud.com"],
        "valid_from": "2026-01-01T00:00:00Z",
        "expires": "2099-01-01T00:00:00Z",
        **changes,
    }


def _settings(tmp_path, hostname="dev.acme-cloud.com") -> Settings:
    return Settings(
        base_data_dir=_DATA_DIR,
        etc_dir=str(tmp_path),
        teams={"1": TeamSettings(team_id="1", hostname=hostname, ui_password=_PW)},
    )


def _install(tmp_path, sign, **changes) -> None:
    (tmp_path / "license.key").write_text(sign(_custom(**changes)))


def test_no_custom_license_keeps_the_stock_brand(tmp_path, sign):
    (tmp_path / "license.key").write_text(
        sign({"type": "business", "name": "Acme", "email": "a@b.example"})
    )
    brand = branding.compute(_settings(tmp_path))
    assert brand == branding.Brand()
    assert brand.rebrand("Restart Oduflow.") == "Restart Oduflow."


def test_custom_license_on_a_licensed_domain_turns_white_label_on(tmp_path, sign):
    _install(tmp_path, sign)
    brand = branding.compute(_settings(tmp_path))
    assert brand.white_label and brand.name == "AcmeFlow" and brand.slug == "acmeflow"
    assert brand.rebrand("Restart Oduflow. See oduflow.toml.") == (
        "Restart AcmeFlow. See oduflow.toml."
    )


@pytest.mark.parametrize(
    "hostname", ["localhost", "acme-cloud.com.evil.example", "notacme-cloud.com"]
)
def test_a_key_copied_to_another_domain_brands_nothing(tmp_path, sign, hostname):
    _install(tmp_path, sign)
    brand = branding.compute(_settings(tmp_path, hostname=hostname))
    assert not brand.white_label and brand.name == "Oduflow"
    assert "not covered" in brand.reason


def test_every_team_hostname_must_be_covered(tmp_path, sign):
    _install(tmp_path, sign)
    settings = _settings(tmp_path)
    settings.teams["2"] = TeamSettings(team_id="2", hostname="other.example")
    assert not branding.compute(settings).white_label


def test_white_label_survives_the_grace_period_only(tmp_path, sign):
    expires = datetime(2030, 1, 1, tzinfo=timezone.utc)
    _install(tmp_path, sign, expires=expires.isoformat())
    settings = _settings(tmp_path)
    inside = expires + timedelta(days=branding.GRACE_DAYS - 1)
    after = expires + timedelta(days=branding.GRACE_DAYS + 1)
    assert branding.compute(settings, now=inside).white_label
    late = branding.compute(settings, now=after)
    assert not late.white_label and "expired" in late.reason


def test_icon_falls_back_to_the_logo(tmp_path, sign):
    _install(tmp_path, sign)
    (tmp_path / "branding").mkdir()
    (tmp_path / "branding" / "logo.png").write_bytes(_PNG)
    brand = branding.compute(_settings(tmp_path))
    assert brand.logo_path.endswith("logo.png") and brand.icon_path == brand.logo_path


def test_new_ssh_keys_carry_the_brand(tmp_path, sign):
    _install(tmp_path, sign)
    settings = _settings(tmp_path)
    assert git_ops.ssh_key_comment("1") == "oduflow-1"
    branding.configure(lambda: settings)
    assert git_ops.ssh_key_comment("1") == "acmeflow-1"


def _client(settings: Settings) -> TestClient:
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    client = TestClient(app, base_url="https://dev.acme-cloud.com")
    client.post("/login", data={"password": _PW})
    return client


_VENDOR_MARKERS = (
    "a product by",
    "docs.oduflow.dev",
    'id="license-modal"',
    'id="feedback-modal"',
    'id="version-modal"',
    'id="license-badge"',
    'onclick="openVersionModal()"',
    "oduflow.dev/odoo-sh-import-notes",
)


def test_white_label_dashboard_has_the_brand_and_no_vendor_surfaces(tmp_path, sign):
    _install(tmp_path, sign)
    client = _client(_settings(tmp_path))
    page = client.get("/").text
    assert "<title>AcmeFlow</title>" in page
    assert 'data-white-label="1"' in page
    assert '<span class="brand-version" id="brand-version">v' in page
    assert "IF-STOCK" not in page and "IF-WHITE-LABEL" not in page
    for marker in _VENDOR_MARKERS:
        assert marker not in page, marker
    assert client.get("/api/license").status_code == 404
    assert client.post("/api/license/refresh").status_code == 404


def test_stock_dashboard_keeps_vendor_surfaces(tmp_path):
    client = _client(_settings(tmp_path))
    page = client.get("/").text
    assert "<title>Oduflow</title>" in page
    assert 'data-white-label=""' in page
    for marker in _VENDOR_MARKERS:
        assert marker in page, marker
    assert "IF-STOCK" not in page and "IF-WHITE-LABEL" not in page
    assert client.get("/api/license").status_code == 200


def test_admin_console_login_follows_the_brand(tmp_path, sign):
    from oduflow import admin_ui

    page = admin_ui._render_login()
    assert "<title>Oduflow — Server settings</title>" in page
    assert "__" not in page.split("<body", 1)[1].split("<script", 1)[0]

    _install(tmp_path, sign)
    branding.configure(lambda: _settings(tmp_path))
    page = admin_ui._render_login()
    assert "<title>AcmeFlow — Server settings</title>" in page
    assert 'alt="AcmeFlow logo"' in page
    assert "__BRAND_NAME__" not in page and "__LOGO_CLASS__" not in page


def test_white_label_images_come_from_the_config_dir(tmp_path, sign):
    _install(tmp_path, sign)
    (tmp_path / "branding").mkdir()
    (tmp_path / "branding" / "logo.png").write_bytes(_PNG)
    client = _client(_settings(tmp_path))
    login = TestClient(client.app, base_url="https://dev.acme-cloud.com").get("/login")
    assert "AcmeFlow — Sign in" in login.text and "custom-logo" in login.text
    for path in (
        "/logo.png",
        "/favicon.ico",
        "/static/icon.png",
        "/static/apple-icon.png",
    ):
        response = client.get(path)
        assert response.content == _PNG, path
    assert 'class="custom-logo"' in client.get("/").text


def test_handle_errors_rebrands_results_and_errors(tmp_path, sign):
    import asyncio

    from fastmcp.exceptions import ToolError

    from oduflow.errors import FlowError
    from oduflow.server import handle_errors

    _install(tmp_path, sign)
    settings = _settings(tmp_path)
    branding.configure(lambda: settings)

    @handle_errors
    def ok() -> str:
        return "Oduflow created the environment."

    @handle_errors
    def flow_error() -> str:
        raise FlowError("Restart Oduflow.")

    @handle_errors
    def tool_error() -> str:
        raise ToolError("Ask the Oduflow operator.")

    assert asyncio.run(ok()) == "AcmeFlow created the environment."
    with pytest.raises(ToolError, match="Restart AcmeFlow"):
        asyncio.run(flow_error())
    with pytest.raises(ToolError, match="Ask the AcmeFlow operator"):
        asyncio.run(tool_error())


def test_rest_errors_are_rebranded(tmp_path, sign, monkeypatch):
    """Dashboard errors are rebranded at the one place they pass through.

    _public_flow_error is to the REST and WebSocket surface what handle_errors
    is to MCP, so messages raised anywhere in the codebase stay written as
    "Oduflow".
    """
    from oduflow import web_ui
    from oduflow.errors import ProtectedError

    def _protected(*args, **kwargs):
        raise ProtectedError("Unprotect it in the Oduflow dashboard first.")

    _install(tmp_path, sign)
    client = _client(_settings(tmp_path))
    monkeypatch.setattr(web_ui.env_ops, "list_environments", _protected)

    response = client.get("/api/environments")

    assert response.status_code == 400
    assert response.json()["error"] == "Unprotect it in the AcmeFlow dashboard first."


def test_terminal_errors_are_rebranded(tmp_path, sign):
    from oduflow import web_ui
    from oduflow.errors import FlowError

    _install(tmp_path, sign)
    branding.configure(lambda: _settings(tmp_path))

    line = web_ui._term_error(FlowError("Restart Oduflow and retry."))

    assert "Restart AcmeFlow and retry." in line
    assert "Oduflow" not in line


@pytest.mark.parametrize(
    "agent_type,provider_error,marker",
    [
        ("claude", "401 failed to authenticate", "AcmeFlow authentication guidance:"),
        (
            "opencode",
            "provider authentication required",
            "AcmeFlow OpenCode authentication guidance:",
        ),
    ],
)
def test_acp_auth_guidance_is_rebranded_and_appended_once(
    tmp_path, sign, agent_type, provider_error, marker
):
    """The marker is both what the client reads and the idempotency check.

    Rebranding the guidance but not the marker would stop matching the frame it
    already annotated, appending the advice again on every error frame.
    """
    import json

    from oduflow import web_ui

    _install(tmp_path, sign)
    branding.configure(lambda: _settings(tmp_path))
    frame = json.dumps({"error": {"code": -32603, "message": provider_error}})

    once = web_ui._annotate_acp_auth_error(frame, agent_type, "setup_token", "1")
    twice = web_ui._annotate_acp_auth_error(once, agent_type, "setup_token", "1")

    assert marker in once
    assert "Oduflow" not in once
    assert twice == once


def test_seed_commits_carry_the_brand(tmp_path, sign):
    # The seed commit of a local extra repo lands in the client's own history.
    from oduflow import extra_addons

    _install(tmp_path, sign)
    assert extra_addons._seed_commit_env()["GIT_AUTHOR_NAME"] == "Oduflow"

    branding.configure(lambda: _settings(tmp_path))
    seed_env = extra_addons._seed_commit_env()

    assert seed_env["GIT_AUTHOR_NAME"] == "AcmeFlow"
    assert seed_env["GIT_COMMITTER_EMAIL"] == "import@acmeflow.local"


def test_mcp_surface_is_rebranded_and_vendor_tools_hidden(tmp_path, sign):
    from oduflow import server

    _install(tmp_path, sign)
    settings = _settings(tmp_path)
    branding.configure(lambda: settings)
    tools = server.mcp._tool_manager._tools
    saved = (
        server.mcp._mcp_server.name,
        server.mcp.instructions,
        {key: tool.description for key, tool in tools.items()},
        tools["report_issue"].enabled,
    )
    try:
        server._apply_branding()
        assert server.mcp.name == "AcmeFlow"
        assert "Oduflow" not in (server.mcp.instructions or "")
        assert not any("Oduflow" in (t.description or "") for t in tools.values())
        assert not tools["report_issue"].enabled
        assert not tools["submit_agent_feedback"].enabled
    finally:
        server.mcp._mcp_server.name = saved[0]
        server.mcp.instructions = saved[1]
        for key, description in saved[2].items():
            tools[key].description = description
        if saved[3]:
            tools["report_issue"].enable()


def test_license_cli_status_explains_white_label(tmp_path, sign, capsys):
    import argparse

    from oduflow.server import _run_license_cli

    _install(tmp_path, sign)
    _run_license_cli(
        _settings(tmp_path, hostname="localhost"),
        argparse.Namespace(license_action="status"),
    )
    out = capsys.readouterr().out
    assert "Brand: AcmeFlow" in out and "White label: off" in out
    assert "localhost" in out


def test_grace_renewal_installs_a_renewed_key(tmp_path, sign, monkeypatch):
    now = datetime.now(timezone.utc)
    _install(
        tmp_path,
        sign,
        valid_from="2025-01-01T00:00:00Z",
        expires=(now - timedelta(days=2)).isoformat(),
    )
    renewed = sign(
        _custom(
            valid_from=(now - timedelta(days=2)).isoformat(),
            expires=(now + timedelta(days=363)).isoformat(),
        )
    )
    import httpx

    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kw: httpx.Response(
            200,
            json={"renewed": True, "license_key": renewed},
            request=httpx.Request("POST", url),
        ),
    )
    settings = _settings(tmp_path)
    assert branding.grace_days_left(settings) == branding.GRACE_DAYS - 2
    assert branding._renew_in_grace(settings)
    assert (tmp_path / "license.key").read_text() == renewed
    assert branding.grace_days_left(settings) is None
    assert not branding._renew_in_grace(settings)


def test_white_label_withholds_the_agent_feedback_surface(tmp_path, sign):
    """A hidden tool must not be advertised in the MCP instructions.

    White label hides submit_agent_feedback, so publishing its hint would tell
    every client to call a tool that answers "Unknown tool".
    """
    import dataclasses

    from oduflow import agent_feedback, server

    _install(tmp_path, sign)
    settings = dataclasses.replace(_settings(tmp_path), agent_feedback=True)
    branding.configure(lambda: settings)
    saved = server.mcp.instructions
    try:
        server._apply_agent_feedback(settings)

        assert agent_feedback.MCP_HINT not in (server.mcp.instructions or "")
        assert not server.mcp._tool_manager._tools["submit_agent_feedback"].enabled
    finally:
        server.mcp.instructions = saved
        server.submit_agent_feedback.disable()


def test_grace_renewal_attempts_once_at_startup(tmp_path, monkeypatch):
    """The daily gate must not be measured against the host's uptime.

    time.monotonic() counts from boot, so starting the gate at zero made the
    first attempt wait for a day of uptime; a server restarted more often than
    that never renewed at all, and white label lapsed after the grace period
    despite a paid renewal. Here the host has been up for five minutes.
    """
    import threading

    attempts: list[Settings] = []
    parked = threading.Event()
    forever = threading.Event()

    class _JustBooted:
        """Stands in for the ``time`` module inside oduflow.branding."""

        @staticmethod
        def monotonic() -> float:
            return 300.0

        @staticmethod
        def sleep(seconds: float) -> None:
            # One iteration is enough; park the daemon thread for good.
            parked.set()
            forever.wait()

    def _attempt(settings: Settings) -> bool:
        attempts.append(settings)
        return True

    monkeypatch.setattr(branding, "time", _JustBooted)
    monkeypatch.setattr(branding, "_renew_in_grace", _attempt)
    settings = _settings(tmp_path)

    branding.start_grace_renewal(lambda: settings)

    assert parked.wait(timeout=10)
    assert attempts == [settings]


def test_payload_helpers_are_base64():
    # Guard for the helper the tests rely on: a key is "<sig>.<payload>".
    private, _ = TestVerifyLicenseText._keypair()
    key = TestVerifyLicenseText._sign(private, {"type": "custom"})
    assert base64.b64decode(key.split(".")[1]) == b'{"type": "custom"}'
