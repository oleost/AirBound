# AirBound hosted relay

The server side of AirBound's hosted mode: one shared relay that many Home
Assistants connect out to, with Patreon membership deciding who may use it. It
runs [airbound.dev](https://airbound.dev), and you can run your own for family and
friends.

## How it works

```text
phone â”€â”€TLSâ”€â”€â–¶ :443 hub (frps) â”€â”€routes by name (SNI), no decryptionâ”€â”€â–¶ tunnel â”€â”€â–¶ Home Assistant
                     â”‚
                     â””â”€â”€ asks the controller on every login, new proxy and heartbeat
```

| Service | What it does |
| --- | --- |
| `airbound-hub` | [frp](https://github.com/fatedier/frp)'s server. Port 443 routes each connection by the name in the TLS handshake to that member's Home Assistant; port 7000 takes the tunnels, over TLS pinned to the hub's own CA. |
| `airbound-controller` | The hub's server plugin and the website's API. A tunnel may log in only with a valid device key and an active membership, may only publish its own hostname, gets a server-side bandwidth limit, and is dropped at the next heartbeat once the membership has lapsed (after a grace period). It also handles linking with Patreon and keeps the member list in sync (webhook plus an hourly check). |
| `airbound-web` | Caddy, serving the website on the domain itself. It gets its certificate the same way members do: through the hub (TLS-ALPN). |
| `airbound-site` | A local tunnel that publishes the website through the hub. |

Each Home Assistant gets its certificate from Let's Encrypt through the hub, so
the private key never leaves the home and the relay can't decrypt anything.

## Install

You need a small Debian or Ubuntu server with a public IPv4 address, a domain on
Cloudflare, and a Patreon creator page with a tier for the relay.

1. **Cloudflare:** create an API token with *Zone → DNS → Edit* and *Zone → Zone → Read* for the domain only, and lock it to the server's IP under *Client IP address filtering*. Turn off *Universal SSL* for the domain (SSL/TLS → Edge Certificates); nothing is proxied through Cloudflare, and its automatic certificate would cover every member's address.
2. **Patreon:** in the [developer portal](https://www.patreon.com/portal/registration/register-clients), create a client (API version 2) with the redirect URI `https://<your domain>/patreon/callback`.
3. **Secrets on the server**, readable by root only. They never go in this repository:

   ```sh
   mkdir -p /etc/airbound-controller
   echo '<cloudflare token>' > /etc/airbound-controller/cloudflare_token
   cat > /etc/airbound-controller/patreon.env <<'EOF'
   PATREON_CLIENT_ID=...
   PATREON_CLIENT_SECRET=...
   PATREON_CREATOR_ACCESS_TOKEN=...
   PATREON_CREATOR_REFRESH_TOKEN=...
   EOF
   chmod 600 /etc/airbound-controller/*
   ```

   Optional, for GitHub Sponsors as a second way in: a GitHub OAuth app (callback `https://<your domain>/github/callback`), a classic token with `read:user` from the sponsored account (to read the sponsor list), and a sponsorship webhook to `https://<your domain>/github/webhook` with a secret, in `/etc/airbound-controller/github.env`:

   ```sh
   GITHUB_CLIENT_ID=...
   GITHUB_CLIENT_SECRET=...
   GITHUB_TOKEN=...
   GITHUB_WEBHOOK_SECRET=...
   GITHUB_SPONSORABLE=<account>
   ```

4. **Run the installer** from this directory: `sudo sh install.sh`. The first run creates `/etc/airbound-controller/controller.env`; fill in `DOMAIN`, `CAMPAIGN_ID` and `RELAY_TIER_ID` (shown by the Patreon API), then run it again.
5. **Point the domain at the server:** `cd /opt/airbound-controller && sudo -u airbound-controller venv/bin/python -m airbound_relay dns`.

Running the installer again updates everything and keeps the settings, the database and the hub's certificate.

The add-on pins the hub's CA (`airbound/airbound/hosted_relay_ca.pem`). A relay of
your own has its own CA, at `https://<your domain>/relay-ca.pem`; build the add-on
with that file and with `AIRBOUND_HOSTED_SERVICE` and `AIRBOUND_HOSTED_RELAY` set
to your domain.

## Settings (`controller.env`)

| Setting | Default | Meaning |
| --- | --- | --- |
| `DOMAIN` | `airbound.dev` | Members get addresses under this domain |
| `RELAY_HOST` | `relay.<domain>` | Where tunnels connect (port 7000) |
| `CAMPAIGN_ID`, `RELAY_TIER_ID` | | The Patreon campaign and the tier that gives access |
| `FREE_PATREON_USERS` | | Patreon user ids with free access, comma-separated (for example the operator) |
| `MAX_DEVICES` | `2` | Home Assistants per membership; linking a third unlinks the one seen least recently |
| `BANDWIDTH` | `4MB` | Per Home Assistant and direction, in frp's notation |
| `GRACE_DAYS` | `3` | Access after a membership lapses, to fix a failed payment |
| `DEVICE_MONTHLY_GB` | `300` | Fair use per Home Assistant and calendar month (in + out) |
| `THROTTLED_BANDWIDTH` | `1MB` | Speed past that allowance, for the rest of the month |
| `SERVER_MONTHLY_GB` | `15000` | What the server may send in a month (stay under your provider's included traffic) |
| `EMERGENCY_BANDWIDTH` | `512KB` | Speed for everyone past the server's allowance |
| `GITHUB_SPONSORABLE` | | Optional: the GitHub account people sponsor; turns on "Continue with GitHub Sponsors" |
| `GITHUB_MIN_CENTS` | `100` | Monthly sponsorship that gives access ($1; GitHub tiers are whole dollars) |
| `ALERT_URL` | | Optional [ntfy](https://ntfy.sh)-style URL; alerts always go to the journal too |

## Keeping one member from taking the whole server

| Guard | How |
| --- | --- |
| Speed | Every Home Assistant gets a server-side bandwidth limit when its tunnel is created; the client can't change it. |
| Monthly traffic | The controller reads each tunnel's traffic from the hub's local admin API every 5 minutes. Past the per-device allowance that Home Assistant is slowed down for the rest of the month; past the server's allowance, everyone is. The next heartbeat (every 30 seconds) makes the tunnel reconnect at the new speed. |
| Membership | Checked at login and at every heartbeat, so a lapsed member is dropped within a minute after the grace period. |
| Connections | nftables limits each address to 256 open and 60 new connections a second on port 443, and 32 open and 20 new a minute on port 7000 (`airbound-limits`). |
| Memory and CPU | Each service has a systemd `MemoryMax`, `CPUQuota` and `TasksMax`, so one can't starve the others. |
| Firewall | `airbound-firewall` (nftables) lets in only SSH, 443, 7000 and anything else that already listened publicly when the installer ran; everything else is dropped. A server run by ufw or firewalld keeps that firewall instead. |
| SSH | Password logins off and root by key only, once the person running the installer has an SSH key. |
| Updates | Security updates install automatically (unattended-upgrades). |
| Alerts | Crossed allowances, 80% of the server's traffic, high load, low memory and a full disk. |

## Terms

`/terms` shows the terms for airbound.dev: a hobby project, the relay as a
best-effort thank-you for Patreon supporters, with no warranty or liability. If you
run a relay of your own, edit `terms()` in `controller/airbound_relay/pages.py` so
it fits you.

## Privacy

The controller keeps the Patreon user id, whether the membership gives access, and
for each linked Home Assistant its address, a hash of its key, when it was last
connected and how much data it used each month (for fair use, kept for three
months). Connection logs are kept for 14 days (journald). Nothing about the
traffic itself is stored or readable.
