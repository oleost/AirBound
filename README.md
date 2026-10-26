# AirBound

> [!WARNING]
> **Work in progress — not ready for use.** AirBound is under active development and has only been tested on a single test system. Settings, behaviour and the setup wizard can change without notice, and there is no upgrade path between versions yet. Don't rely on it to protect a Home Assistant you care about.

Secure, self-hosted remote access to Home Assistant, without a subscription.

AirBound is a Home Assistant OS add-on that gives your Home Assistant its own secure web address. It works in the Companion app and in any browser, including on a borrowed computer, with no VPN app. A step-by-step wizard in the Home Assistant sidebar does the setup, so you don't need to read documentation.

## How it works

- **Two ways to connect**
  - **Relay (recommended):** a small server you rent (a VPS) passes the traffic on to your home. Home Assistant connects *out* to it, so your router needs no open ports and your home's address stays hidden. The wizard gives you one command to set up the server.
  - **Direct:** your phone connects straight to your home through one forwarded port on your router (set by hand, or automatically with UPnP/NAT-PMP if your router allows it).
- **Encrypted to your home.** The certificate lives in Home Assistant (Let's Encrypt via Cloudflare DNS). The relay and Cloudflare only move encrypted traffic and can't read it.
- **Two-step login is enforced.** Logins from outside without MFA are refused at the door, even though Home Assistant itself has no setting to require MFA. Logins at home are unaffected.
- **A hidden address.** A random hostname under your domain and a wildcard certificate, so the name never shows up in public certificate logs. Requests for any other name are dropped.
- **Attack protection.** CrowdSec watches the logs, blocks addresses that probe or guess passwords, and uses the community blocklist. Private addresses are never blocked.
- **Status in Home Assistant.** Sensors, events and notifications, for example when someone logs in from a new address.

## Requirements

- Home Assistant OS (the HTTP settings API from 2026.8 or later)
- A domain managed by Cloudflare (a free account is enough)
- For relay mode: a small Linux VPS (Debian or Ubuntu)

## Development

The add-on lives in [`airbound/`](airbound). Unit tests run anywhere:

```sh
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

Integration and browser tests run against a Home Assistant OS test system. Put its address and a long-lived token in a `.env` file (never committed):

```sh
HA_URL=http://<test-vm>
HA_WS=ws://<test-vm>/api/websocket
HA_TOKEN=<long-lived access token>
```

Then deploy with `python scripts/deploy.py` and run `python -m pytest tests/integration`.

## Status

| Part | State |
| --- | --- |
| Setup wizard, direct mode, MFA enforcement | Working on the test system, confirmed with the Companion app |
| Relay mode | Working end to end with a test relay; not yet tested on a real VPS |
| CrowdSec | Running with a startup self-test |
| Home Assistant sensors and notifications | Working |
| Automatic port opening (UPnP/NAT-PMP) | Unit-tested only |
