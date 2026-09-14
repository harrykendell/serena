# Cloudflare Tunnel deployment

The hosted Serena deployment uses a remotely managed Cloudflare Tunnel to expose the local Serena, Orchestrator, and dashboard services at `https://mcp.kendell.uk`.

Cloudflare DNS, Tunnel ingress/public-hostname routing, and Access policies are managed in Cloudflare and are deliberately not stored in this repository. The tunnel connector token is also a secret and must never be committed.

## Local service topology

The current deployment expects these loopback services:

| Service | Local endpoint | Public endpoint |
| --- | --- | --- |
| Serena MCP | `http://127.0.0.1:9121/serena` | `https://mcp.kendell.uk/serena` |
| Orchestrator MCP | `http://127.0.0.1:8100/orchestrator` | `https://mcp.kendell.uk/orchestrator` |
| Kendell dashboard | `http://127.0.0.1:24282/dashboard/` | `https://mcp.kendell.uk/dashboard/` |

The dashboard service also redirects its local `/` route to `/dashboard/`.

The Serena runtime is configured with:

```text
SERENA_PUBLIC_BASE_URL=https://mcp.kendell.uk
```

Do not bind these application services to a public interface. Cloudflare Tunnel connects to the loopback listeners.

## 1. Prepare Serena and Orchestrator

Clone or restore this repository, then recreate its environment rather than copying an existing `.venv`:

```bash
cd ~/Desktop/serena
uv sync --extra dev --locked
```

The deployment currently runs the equivalent of:

```bash
~/Desktop/serena/.venv/bin/serena start-mcp-server \
  --transport streamable-http \
  --host 127.0.0.1 \
  --port 9121 \
  --streamable-http-path /serena \
  --enable-web-dashboard true \
  --web-dashboard-port 24282 \
  --open-web-dashboard false

~/Desktop/serena/.venv/bin/orchestrator \
  --transport streamable-http \
  --host 127.0.0.1 \
  --port 8100 \
  --streamable-http-path /orchestrator
```

On the workstation these are supervised by user systemd services. If restoring a previous home-directory backup, verify that these files are present and still refer to the correct checkout path:

```text
~/.config/systemd/user/serena-mcp.service
~/.config/systemd/user/serena-mcp.service.d/10-local-fork.conf
~/.config/systemd/user/serena-mcp.service.d/20-file-transfer.conf
~/.config/systemd/user/orchestrator-mcp.service
```

Then reload and start them:

```bash
systemctl --user daemon-reload
systemctl --user enable --now serena-mcp.service orchestrator-mcp.service
```

Confirm the listeners before configuring Cloudflare:

```bash
ss -ltnp | grep -E ':(8100|9121|24282)\\b'
curl -I http://127.0.0.1:24282/dashboard/
curl -i http://127.0.0.1:9121/serena
curl -i http://127.0.0.1:8100/orchestrator
```

A plain `curl` to an MCP endpoint is expected to return an MCP protocol error such as HTTP 406 because it has not supplied the required MCP `Accept` headers. That still confirms that the endpoint is reachable.

## 2. Install cloudflared

Install `cloudflared` from Cloudflare's supported package repository for the workstation distribution. Do not copy the old binary between machines.

Verify it is available:

```bash
cloudflared --version
```

The September 2026 workstation deployment used `cloudflared 2026.8.2`; matching that exact version is not required unless a later version introduces an incompatibility.

## 3. Obtain a connector token

Use the existing remotely managed tunnel in the Cloudflare dashboard. Generate a connector/install token for that tunnel rather than creating a new DNS hostname or a second unrelated tunnel.

Prefer issuing a new connector token during a machine replacement. Do not paste the token into shell history, documentation, Git, issue trackers, or chat transcripts.

Store the token as a root-only file:

```text
/etc/cloudflared/token
```

with ownership and mode:

```text
root:root 0600
```

For example, write it using a root-owned editor or another mechanism that does not expose the token in shell history, then verify only its metadata:

```bash
sudo chown root:root /etc/cloudflared/token
sudo chmod 600 /etc/cloudflared/token
sudo stat /etc/cloudflared/token
```

Never commit this file.

## 4. Install the system service

The connector is run as a system service so it is available independently of the interactive user session.

Create `/etc/systemd/system/cloudflared.service`:

```ini
[Unit]
Description=Cloudflare Tunnel client
After=network-online.target
Wants=network-online.target

[Service]
TimeoutStartSec=15
Type=notify
ExecStart=/usr/bin/cloudflared --no-autoupdate tunnel run --token-file /etc/cloudflared/token
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
```

The current workstation also uses an optional daily package self-update check. `/etc/systemd/system/cloudflared-update.service`:

```ini
[Unit]
Description=Update cloudflared
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/bin/bash -c '/usr/bin/cloudflared update; code=$?; if [ $code -eq 11 ]; then systemctl restart cloudflared; exit 0; fi; exit $code'
```

and `/etc/systemd/system/cloudflared-update.timer`:

```ini
[Unit]
Description=Update cloudflared

[Timer]
OnCalendar=daily

[Install]
WantedBy=timers.target
```

If `cloudflared` is managed exclusively by the system package manager, the update timer is optional and may be omitted in favour of normal package upgrades.

Load and enable the connector:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cloudflared.service
sudo systemctl enable --now cloudflared-update.timer
```

Inspect it with:

```bash
systemctl status cloudflared.service --no-pager
journalctl -u cloudflared.service -n 100 --no-pager
```

## 5. Cloudflare-side configuration

The following configuration is intentionally maintained in Cloudflare rather than in this repository:

- the `mcp.kendell.uk` DNS/tunnel association;
- public-hostname or ingress rules mapping the public paths to the three local services;
- Cloudflare Access applications and policies;
- OAuth/protected-resource behaviour for the MCP endpoints;
- authentication policy for the dashboard;
- the tunnel connector token.

For a replacement workstation using the existing tunnel, these settings should not need to be recreated. Confirm in Cloudflare that the tunnel has a healthy connector after starting `cloudflared`.

If the Cloudflare-side configuration itself has been lost, recreate the routing from the local service topology table above and restore the appropriate Access policies before exposing the endpoints.

## 6. End-to-end verification

First verify the local services, then the tunnel, then Access.

Local:

```bash
curl -I http://127.0.0.1:24282/dashboard/
curl -i http://127.0.0.1:9121/serena
curl -i http://127.0.0.1:8100/orchestrator
```

Public, without an authenticated browser/session:

```bash
curl -I https://mcp.kendell.uk/
curl -I https://mcp.kendell.uk/dashboard/
curl -i https://mcp.kendell.uk/serena
curl -i https://mcp.kendell.uk/orchestrator
```

Expected behaviour is that the dashboard routes are intercepted by Cloudflare Access and unauthenticated MCP requests are rejected by the configured Access/OAuth layer. A successful public `200` response to an endpoint that is supposed to be protected should be treated as a deployment error.

Finally verify with the actual authenticated clients:

1. open `https://mcp.kendell.uk/dashboard/` and authenticate through Cloudflare Access;
2. connect ChatGPT to the Serena MCP endpoint;
3. execute a harmless Serena read-only tool call;
4. verify Orchestrator separately;
5. confirm that dashboard activity updates arrive as expected.

## Machine replacement checklist

For a workstation replacement:

1. Restore or clone the Serena repository and restore the user configuration/state that should survive the migration.
2. Run `uv sync --extra dev --locked` to recreate `.venv`.
3. Restore/verify the Serena and Orchestrator user systemd services and `SERENA_PUBLIC_BASE_URL`.
4. Install `cloudflared` normally.
5. Issue a fresh connector token for the existing Cloudflare tunnel and install it as `/etc/cloudflared/token` with mode `0600`.
6. Install/restore `cloudflared.service` and enable it.
7. Start Serena and Orchestrator and verify ports `9121`, `8100`, and `24282` locally.
8. Confirm the Cloudflare tunnel reports a healthy connector.
9. Verify Cloudflare Access and all three public routes end to end.
10. Once the replacement is known-good, remove/revoke the old connector if the old machine will no longer be used.

No Cloudflare secret is required in this repository, and no secret should be added to make this process more automatic.
