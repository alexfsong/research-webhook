# INFRA — Hetzner box ops guide

Operational doc for the shared Hetzner VPS. Read this **before** deploying a new
app. The box hosts multiple unrelated services; a clash on port, hostname,
firewall, or systemd unit name will break a sibling.

## Box

| Field | Value |
|-------|-------|
| Provider | Hetzner Cloud |
| OS | Ubuntu 24.04 LTS |
| Public IPv4 | `195.201.99.206` |
| Primary user | `researcher` (sudo) |
| Reverse proxy | Caddy 2.8+ (cloudsmith apt repo, **not** Ubuntu universe) |
| Firewall | `ufw` — open: 22, 80, 443 only |
| Persistent data root | `/home/researcher/research-data/` |
| Shared Python venv | `/home/researcher/open_deep_research/.venv` (LangGraph deps) |

## Access

SSH key auth only. Add agent-side public keys to `/home/researcher/.ssh/authorized_keys`
and `/root/.ssh/authorized_keys` (root login still permitted for ops).
No passwords. Never disable key auth or open additional ports without coordination.

```bash
ssh root@195.201.99.206
ssh researcher@195.201.99.206
```

## Conventions (follow these for any new app)

### Filesystem layout

```
/home/researcher/<app-name>/          # the app's repo (git clone)
/home/researcher/<app-name>/.env      # secrets, chmod 600, gitignored
/home/researcher/<app-name>/.venv     # per-app venv (or reuse shared if deps overlap)
/home/researcher/research-data/<app>/ # persistent state (DBs, caches, models)
/etc/systemd/system/<app-name>.service
/etc/systemd/system/<app-name>.timer  # if scheduled
```

Use the app's directory name as the systemd unit name. Avoid generic names
(`api.service`, `worker.service`) — they collide as the box grows.

### Port registry

All HTTP services bind `127.0.0.1` and sit behind Caddy. Pick the next free port.

| Port | App | Unit |
|------|-----|------|
| 22 | ssh | (system) |
| 80, 443 | caddy | `caddy.service` |
| 8000 | research-webhook (FastAPI) | `research-webhook.service` |
| 8001+ | available | — |

**Update this table in the same PR that adds your app.**

### Hostname pattern

We use [sslip.io](https://sslip.io) for free TLS-eligible hostnames keyed off
the box IP — Anthropic's Routines sandbox proxy blocks DuckDNS / dynamic-DNS
providers, but sslip.io passes. Pattern:

```
<app-name>.195-201-99-206.sslip.io
```

Existing:
- `lisearch.195-201-99-206.sslip.io` → research-webhook

Caddy auto-issues a Let's Encrypt cert via http-01 on first request. No manual
cert ops.

### ufw policy

Already configured: deny incoming except 22, 80, 443. **Do not add app ports to
ufw.** All app traffic must go through Caddy. To verify:

```bash
sudo ufw status
```

### Secrets

- Per-app `.env`, chmod 600, owned by `researcher`.
- Loaded by systemd via `EnvironmentFile=` in the unit.
- Never `Environment=` in the unit file (leaks via `systemctl cat`).
- `.env.example` committed; real `.env` gitignored.
- Rotate any secret that has been pasted into a chat / log / PR.

## Standard systemd unit (HTTP service)

```ini
[Unit]
Description=<App description>
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=researcher
Group=researcher
WorkingDirectory=/home/researcher/<app>
EnvironmentFile=/home/researcher/<app>/.env
ExecStart=/home/researcher/<app>/.venv/bin/<entrypoint>
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Install:
```bash
sudo cp deploy/<app>.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now <app>.service
sudo systemctl status <app>.service
```

## Standard scheduled job (timer + oneshot)

Prefer systemd timers over cron — better logging, dependency control, env
handling matches services.

```ini
# /etc/systemd/system/<job>.service
[Unit]
Description=<Job description>
After=network-online.target

[Service]
Type=oneshot
User=researcher
WorkingDirectory=/home/researcher/<app>
EnvironmentFile=/home/researcher/<app>/.env
ExecStart=/home/researcher/<app>/.venv/bin/python <script>.py
```

```ini
# /etc/systemd/system/<job>.timer
[Unit]
Description=<schedule description>

[Timer]
OnCalendar=*-*-* 06:00:00   # daily 06:00 UTC
Persistent=true             # catches up missed runs after reboot

[Install]
WantedBy=timers.target
```

Enable: `sudo systemctl enable --now <job>.timer`.
Logs: `journalctl -u <job>.service -n 200`.
List timers: `systemctl list-timers`.

## Caddy

Config: `/etc/caddy/Caddyfile`. Each app contributes a block:

```
<app>.195-201-99-206.sslip.io {
    reverse_proxy 127.0.0.1:<port>
}
```

Reload after edits:
```bash
sudo systemctl reload caddy
sudo journalctl -u caddy -f      # watch ACME / cert flow
```

If Caddy is ever reinstalled, use the cloudsmith repo (Ubuntu universe ships
2.6.2 with an acmez nil-pointer bug on ZeroSSL fallback):

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install caddy
```

If certs misbehave, check ACME state:

```bash
sudo ls /var/lib/caddy/.local/share/caddy/certificates/
# nuke poisoned state if needed (forces re-issuance):
# sudo rm -rf /var/lib/caddy/.local/share/caddy/{acme,certificates}/*
sudo systemctl reload caddy
```

## Persistent state

`/home/researcher/research-data/` is the convention. Subdirectory per app:

| Path | Owner app |
|------|-----------|
| `~/research-data/courses.db` | research-webhook |
| `~/research-data/chroma/` | research-webhook |
| `~/research-data/reports/` | research-webhook |
| `~/research-data/hf-cache/` | research-webhook (HuggingFace models) |

Back up this directory if data matters. Nothing in `/home/researcher/<app>/` is
intended to be persistent beyond a `git pull`.

## Add a new app — checklist

1. Pick a unit name (`<app>`) and a port (next free in registry above).
2. `sudo -u researcher git clone <repo> /home/researcher/<app>`.
3. Per-app venv: `python3.11 -m venv /home/researcher/<app>/.venv` and install.
4. Copy `.env.example` → `.env`, fill in, `chmod 600`.
5. Drop systemd unit (and timer if scheduled) into `/etc/systemd/system/`.
6. `daemon-reload && enable --now`.
7. Add Caddy block in `/etc/caddy/Caddyfile`, reload Caddy.
8. Smoke test: `curl https://<app>.195-201-99-206.sslip.io/health`.
9. Update the port registry table in this file. Open a PR.

## Update flow (per app)

```bash
# locally
git push

# on the box
sudo -u researcher bash -lc 'cd /home/researcher/<app> && git pull'
sudo systemctl restart <app>.service
```

For static-only changes (e.g. PWA assets served by Caddy from disk), `git pull`
alone is enough.

## Operational quick reference

```bash
# service health
systemctl status <app>.service
journalctl -u <app>.service -n 200 --no-pager
journalctl -u <app>.service -f          # tail

# ports actually listening
ss -tlnp

# disk / memory
df -h /home
free -h

# what's enabled
systemctl list-unit-files --state=enabled | grep -v '@'
systemctl list-timers
```

## Existing apps

| App | Repo | Unit | Port | Hostname | Persistent state |
|-----|------|------|------|----------|------------------|
| research-webhook | https://github.com/alexfsong/research-webhook | `research-webhook.service` | 8000 | `lisearch.195-201-99-206.sslip.io` | `~/research-data/{courses.db,chroma/,reports/,hf-cache/}` |

Companion (off-box, fires `/ingest`): https://github.com/alexfsong/agentic-research-play

## Hard rules

- **Never** open additional ufw ports. All HTTP behind Caddy.
- **Never** bind `0.0.0.0` for an app port. Always `127.0.0.1`.
- **Never** commit `.env` or any file containing a real bearer token / OAT / API key.
- **Never** rename or delete another app's systemd unit, Caddyfile block, or
  `~/research-data/<app>/` dir.
- **Always** update the port registry and existing-apps tables in this file when
  adding or removing an app.
