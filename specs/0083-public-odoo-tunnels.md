# 0083 — Public Odoo links through Docker-managed tunnels

**Status:** Adopted
**Type:** Architecture / delivery and network access
**First introduced:** 2026-10-19
**Key code today:** `tunnels/`, `settings.py`, `config_schema.py`, `docker_ops/env_ops.py`, `web_ui.py`, `server.py`

## Context

A developer running Oduflow on a laptop needs to send a client a link to the
result in Odoo. The existing Share UI capability
([[0055-scoped-environment-ui-sharing]]) grants dashboard access but does not
make the local Odoo service reachable from the internet. Requiring inbound
ports, public IPs, or a host tunnel installation would undermine easy local
setup. Some users want a zero-account temporary URL; others already have
Cloudflare domains or an ngrok account and want stable addresses.

The discussion considered ngrok's Python SDK, then settled on running both
ngrok and cloudflared in containers, using the Docker runtime Oduflow already
requires. Providers are alternatives selected in settings, not simultaneous
publication channels.

## Decision

Add an installation-wide tunnel provider setting and an explicit per-environment
**Publish Odoo** action. Support Disabled, Cloudflare Quick Tunnel, managed
Cloudflare Tunnel, and ngrok through pinned official Docker images. Publication
grants network reachability to Odoo; Odoo's accounts and permissions remain the
access policy. Local URLs and dashboard sharing retain their own identities.

Use Cloudflare's API to provision a named tunnel and owned DNS record for each
managed publication. Use one ngrok agent per team and dynamically manage its
endpoints through the local agent API. Account plans determine how many distinct
ngrok domains can be published; Oduflow does not hard-code a free/paid limit.

## How it works (macro)

The connector joins the selected environment's team network and targets only
its Odoo HTTP service. No router configuration is needed. The ngrok management
API stays on container loopback and is accessed through Docker exec, without
opening a management port to the host or the team's other containers.

A private, atomically written registry records publication intent and remote
resource ownership. A single elected reconciliation worker restores desired
publications, respects environment locks, retries failures, and closes previous
provider connectors before starting a replacement provider. Durable allocation
checkpoints and deterministic remote names allow cleanup after partial failures.
Remote DNS cleanup checks ownership before deleting records.

Environment stop pauses publication, and same-name recreate retains intent,
following the replacement-versus-retirement distinction in
[[0080-stable-environment-identity-across-recreate]]. Delete, rename and failed
full recreate revoke it so a later environment cannot inherit an old public
link. Settings use the existing console's validation, secret masking and live
application machinery ([[0077-server-settings-console]]).

## Consequences

- Quick Tunnel needs no account, but its URL is ephemeral and it cannot carry
  SSE. Managed Cloudflare provides owned domain names and automatic DNS cleanup;
  ngrok uses the user's domains and account limits.
- The laptop, Docker, Odoo and reconciliation server must remain running.
  Provider/account failures remain visible and recoverable in the dialog.
- The first publication may restart Odoo to enable HTTPS proxy handling.
  This capability targets development worker mode (`workers = 0`); production
  and separate gevent routing remain outside its scope.
- Provider credentials are stored locally, including credentials retained long
  enough to clean up a previous provider. Container configuration travels over
  the Docker API, avoiding host-path assumptions in containerized installs.
- A failed ngrok endpoint revoke removes the agent as a fallback. Other desired
  endpoints on that agent briefly disconnect and are restored by reconciliation.

## History

- 2026-10-19 — Initial implementation following the local client-review and
  interchangeable Docker tunnel provider design; commit pointer pending merge.
