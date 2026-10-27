<p align="center"><img src="assets/banner.png" alt="AirBound: secure remote access for Home Assistant" width="820"></p>

<p align="center">
  <a href="https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Foleost%2FHA-AirBound"><img src="https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg" alt="Add the AirBound repository to your Home Assistant" height="40"></a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Home%20Assistant%20OS-add--on-41BDF5?logo=homeassistant&logoColor=white" alt="Home Assistant OS add-on">
  <img src="https://img.shields.io/badge/status-experimental-orange" alt="Status: experimental">
  <img src="https://img.shields.io/badge/arch-amd64%20%7C%20aarch64-blue" alt="amd64 and aarch64">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="MIT license"></a>
</p>

> [!WARNING]
> **Work in progress: not ready for use.** AirBound is under active development and has only been tested on a single test system. Settings, behaviour and the setup wizard can change without notice, and there is no upgrade path between versions yet. Don't rely on it to protect a Home Assistant you care about.

**AirBound gives your Home Assistant its own secure web address.** It works in the Companion app on iPhone and Android and in any browser, at home, at work or on a borrowed computer, with nothing extra to install and no VPN. A step-by-step wizard in the Home Assistant sidebar does the whole setup, so you never need to read documentation.

<p align="center">
  <img src="docs/screenshots/overview.png" alt="The AirBound overview: remote access is on, with all health checks green" width="62%">
  &nbsp;
  <img src="docs/screenshots/overview-phone.png" alt="The same overview on a phone" width="26%">
</p>

## Contents

- [Install](#install)
- [Three ways to connect](#three-ways-to-connect)
- [How it keeps your home safe](#how-it-keeps-your-home-safe)
- [The hosted relay](#the-hosted-relay)
- [Requirements](#requirements)
- [Development](#development)
- [Status](#status)

## Install

1. **Add the repository.** Select the button below. It opens your Home Assistant and asks to add the AirBound add-on repository.

   <a href="https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Foleost%2FHA-AirBound"><img src="https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg" alt="Add the AirBound repository to your Home Assistant" height="32"></a>

   <details>
   <summary>Or add it by hand</summary>

   In Home Assistant, go to **Settings â†’ Add-ons â†’ Add-on store**, open the **â‹®** menu at the top right, choose **Repositories**, and add:

   ```text
   https://github.com/oleost/HA-AirBound
   ```

   </details>

2. **Install AirBound.** Find **AirBound** in the add-on store (reload the page if it isn't there yet), select it and choose **Install**.
3. **Start it** and turn on **Show in sidebar**.
4. **Open AirBound in the sidebar.** The wizard takes it from there: it explains every step, checks it for you, and only moves on when each part works.

<p align="center"><img src="docs/screenshots/welcome.png" alt="The AirBound welcome page" width="80%"></p>

## Three ways to connect

The first step asks how your phone should reach Home Assistant. You can change it later.

| | **Hosted relay** | **Your own server** | **Your router** |
| --- | --- | --- | --- |
| Setup | Log in with Patreon, about 5 minutes | One command on a small VPS | Forward one port |
| Router changes | None | None | One port forward (or automatic, with UPnP/NAT-PMP) |
| Your own domain | Not needed: you get `something.airbound.dev` | A domain on Cloudflare (free account) | A domain on Cloudflare (free account) |
| Cost | Support on Patreon, 7-day free trial | A VPS, about â‚¬4â€“5 a month | Free |
| Home address hidden | Yes | Yes | No |

<p align="center"><img src="docs/screenshots/connection.png" alt="Choosing how to connect: hosted relay, your own server or your router" width="80%"></p>

**Linking to the hosted relay** works like signing in on a TV. AirBound shows a code, you open the link page, check that the code is the same, and log in with Patreon. AirBound picks up its address by itself.

<p align="center">
  <img src="docs/screenshots/link-code.png" alt="The link page on airbound.dev showing the code to compare" width="44%">
  &nbsp;
  <img src="docs/screenshots/hosted-linked.png" alt="AirBound in Home Assistant, linked with its new address" width="50%">
</p>

## How it keeps your home safe

- **Two-step login is enforced.** Every login from outside needs a password *and* a code from an authenticator app. Logins without it are refused at the door, even though Home Assistant itself has no setting to require it. Logins at home are unaffected.
- **Encrypted all the way home.** The certificate is made and kept by your own Home Assistant (Let's Encrypt). Relays and DNS providers only move encrypted traffic and can't read it.
- **A hidden address.** A random name nobody can guess. On your own domain it's covered by a wildcard certificate, so the name never shows up in public certificate logs, and requests for any other name are dropped.
- **Certificate watch.** AirBound searches the public Certificate Transparency logs and warns you if anyone else gets a certificate for your address. On your own domain a CAA record also forbids it outright.
- **Attack protection.** CrowdSec watches the logs, blocks addresses that probe or guess passwords, and uses the community blocklist. Home Assistant's own login ban is turned on too.
- **A locked-down relay server.** The one command that sets up your own server also turns on a firewall (only SSH and the relay's ports get in), SSH logins with keys only, and automatic security updates. The hosted relay runs the same setup.
- **Status in Home Assistant.** Health checks in the panel, plus sensors, events and notifications, for example when someone logs in from a new address.

## The hosted relay

The hosted relay is for people who don't want to deal with domains or servers. It runs on a server in Germany, at [airbound.dev](https://airbound.dev).

> [!NOTE]
> **AirBound is a hobby project, made by one person in their spare time.** Support on [Patreon](https://www.patreon.com/c/airbound) pays for its development, and access to the hosted relay comes with it as a thank-you. You're supporting a project, not buying a service: the relay is run on a best-effort basis, with no guarantee of uptime, no support agreement and no liability if it's down. Don't rely on it for alarms or anything safety-related, and keep in mind that you can switch to your own server or router at any time. See the [terms](https://airbound.dev/terms).

New supporters get a 7-day free trial, and Patreon shows the price in your own currency.

**What the relay can and can't see**

- It routes each connection by the address name and passes it on **without decrypting it**. It never sees passwords, pages or camera images.
- It sees which addresses connect, when, and how much data passes. Connection logs are kept for at most 14 days, and monthly data totals for three months (fair use: up to 4 MB/s, and slower for the rest of the month past 300 GB).
- It stores your Patreon member id and whether your membership includes the relay, not your name, email address or card. See the [privacy page](https://airbound.dev/privacy).

**Being honest about trust.** Whoever runs a domain can, in principle, get a certificate for any name under it. That includes the operator of airbound.dev. It can't be done quietly, though: every certificate is public, and AirBound warns you if one appears for your address that your Home Assistant didn't make. If you'd rather not depend on that, use your own domain with your own server or your router. The add-on works the same way.

**Run your own.** The relay is open source too. [`relay-server/`](relay-server) has the controller and an installer for Debian or Ubuntu, for anyone who wants to run a relay for family and friends.

## Requirements

- **Home Assistant OS**, 2026.8 or later (AirBound uses the HTTP settings API added then)
- **Hosted relay:** the Relay tier on Patreon (7-day free trial; best effort, see the [terms](https://airbound.dev/terms))
- **Your own server or router:** a domain managed by Cloudflare (a free account is enough), plus a small Linux VPS (Debian or Ubuntu) or access to your router's settings

## Development

The add-on lives in [`airbound/`](airbound), the hosted relay in [`relay-server/`](relay-server). Unit tests run anywhere:

```sh
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/unit tests/relay_server
```

Integration and browser tests run against a Home Assistant OS test system. Put its address and a long-lived token in a `.env` file (never committed):

```sh
HA_URL=http://<test-vm>
HA_WS=ws://<test-vm>/api/websocket
HA_TOKEN=<long-lived access token>
```

Then deploy with `python scripts/deploy.py` (it installs the pushed version from GitHub on the test system) and run `python -m pytest tests/integration`. `python scripts/readme_screenshots.py` retakes the screenshots above, with the test system's address, names and IP addresses replaced by examples.

## Status

| Part | State |
| --- | --- |
| Setup wizard, MFA enforcement, CrowdSec, sensors and notifications | Working on the test system, confirmed with the Companion app |
| Hosted relay (airbound.dev, Patreon login, shared SNI routing) | Working end to end on the test system; waiting for the first outside members |
| Your own server (relay mode) | Working end to end with a real VPS (Hetzner, Debian 13) |
| Direct through the router | Working; automatic port opening (UPnP/NAT-PMP) is unit-tested only |

AirBound is not affiliated with Home Assistant or Nabu Casa. If you can, consider supporting Home Assistant's development through [Home Assistant Link](https://www.nabucasa.com/), Nabu Casa's own remote access service, which funds the project.

## License

[MIT](LICENSE). AirBound is provided as is, without warranty of any kind; see the license for the full text.
