# Keeping AirBound up to date

How AirBound stays current and safe: what is pinned where, what watches for
updates and vulnerabilities, the regular routine, and the way a change travels
from a commit to users and the live relay.

## What is pinned, and where

| Part | Pinned in | Watched by |
| --- | --- | --- |
| Add-on base image (Python, Alpine) | `airbound/Dockerfile` (tag + digest); `apk upgrade` runs in every build | Dependabot, maintenance report, Trivy |
| Python packages | `airbound/requirements.txt`, `relay-server/controller/requirements.txt`, `requirements-dev.txt` | Dependabot, pip-audit |
| Caddy and its modules | `airbound/Dockerfile` (add-on), `relay-server/install.sh` (relay website, with checksums) | Maintenance report, Trivy, govulncheck |
| CrowdSec | `airbound/Dockerfile` (tag + digest) | Dependabot, maintenance report, Trivy, govulncheck |
| frp | `.github/workflows/frp.yaml` (upstream commit) builds our release; four places pin that release and its checksums (see below) | Maintenance report, govulncheck |
| GitHub Actions | Each workflow, by commit SHA | Dependabot |
| Relay server OS (Debian) | Not pinned: unattended security updates, restart at 04:30 when one needs it | Maintenance report (relay health) |
| Home Assistant | Not ours; changes monthly | Maintenance report, test VM |
| Secrets | `/etc/airbound-controller` on the relay; repository secret `OPS_TOKEN` | Patreon's creator token refreshes itself; the Cloudflare and GitHub tokens have their own expiry |

## What watches automatically (every Monday)

| Time (UTC) | Workflow | What it does |
| --- | --- | --- |
| Mondays | Dependabot | Pull requests against `next` for Actions, Docker base images and Python packages |
| 05:17 | `scan.yaml` | Trivy scans both images; findings go to the Security tab |
| 05:37 | `govulncheck.yaml` | Which Go vulnerabilities the bundled programs can actually reach |
| 06:45 | `maintenance.yaml` | Pinned versions against the newest, the scans, Dependabot, and the relay's health (`/api/v1/ops`), as a comment on the "Weekly maintenance report" issue. GitHub emails it. |

Trivy also runs after every image build. The report puts everything that
needs a decision under **Action needed**.

## The routine

- **Weekly (automatic):** read the report email. Nothing under "Action needed": nothing to do.
- **Monthly (about 30 minutes):** work through the open items:
  - Merge Dependabot pull requests on `next` once its tests pass.
  - Rebuild frp with the newest Go, and a newer frp if there is one (see below).
  - Update Caddy, its modules, CrowdSec and the Python base image when newer ones are out.
  - Update the test VM to the newest Home Assistant and run the test round.
  - Release through the normal path below.
- **Within 48 hours:** a critical vulnerability that the internet can reach. That means frps on the relays, Caddy, Home Assistant's login path, or OpenSSL/Python in the add-on. Fix it, test it, release it.
- **Release gate:** `scripts/release.py` refuses to promote a release when the last scan is older than 7 days, a scan failed, or a critical finding is open, unless the release is the fix for it.

## From change to users

```text
commit on next ──▶ CI: unit + browser tests, images built, Trivy, govulncheck
      │
      ▶ test VM (follows next) ──▶ python scripts/e2e.py: hosted relay, MFA guard and
      │                           bypass probes, MFA enforcement, own relay on a fresh test VPS
      │
      ▶ relay changes: python scripts/relay_staging.py ──▶ the relay installed on the test VPS
      │
      ▶ python scripts/release.py ──▶ main = this commit (users update), then
                                     python scripts/relay_deploy.py ──▶ airbound.dev, with automatic rollback
```

- **Branches:** `next` is development and testing. The test VM follows it (`https://github.com/oleost/HA-AirBound#next`). `main` is what users get: everyone who added the repository without a branch name follows it. Home Assistant keeps the branch it cloned, so `main` must stay the users' branch. A release fast-forwards `main` to a tested commit on `next`. The images for that version already exist, so users never see an update whose image is still building. Work happens on `next`, never directly on `main`.
- **Test round** (`scripts/e2e.py`, about 20 minutes):
  - Deploys `next` to the test VM.
  - Checks the hosted relay end to end, and the MFA guard against 23 path spellings.
  - Runs MFA enforcement with a throwaway user.
  - Installs the add-on's own-relay command on the test VPS rolled back to its `fresh` snapshot, links the test VM through it, goes back to the hosted relay, and stops the VPS.
- **Relay staging** (`scripts/relay_staging.py`): installs `relay-server/` on the test VPS, then checks:
  - that every service starts with its sandbox
  - that the frp plugin refuses fake clients
  - that traffic metering reads the hub
- **Relay deploy** (`scripts/relay_deploy.py`):
  1. Keeps a copy of the running relay.
  2. Installs the new one and checks the services, the website and a member's address.
  3. Puts the copy back if anything fails.
- **Afterwards:** watch the next report, and the relay's alerts.

## How to update each part

**frp** (our own build):

1. In `.github/workflows/frp.yaml`, set `FRP_VERSION`, `FRP_COMMIT` (the commit behind the upstream tag: `gh api repos/fatedier/frp/commits/v<version> --jq .sha`) and raise `BUILD`. Push to `next`, and the release `frp-v<version>-airbound<N>` is built.
2. Take the two checksums from the release's `sha256sums.txt`, then update the release name and checksums in all four places:
   - `airbound/Dockerfile`
   - `airbound/airbound/relay_install.sh`
   - `relay-server/install.sh`
   - `.github/workflows/govulncheck.yaml`
3. Check that govulncheck on the new build reports nothing reachable that can be fixed.

**Caddy (add-on):** change the builder tag and digest and the module versions in `airbound/Dockerfile`. Pin newer dependencies with `--replace`.

**Caddy (relay website):** change `CADDY_VERSION` and both checksums in `relay-server/install.sh`.

**CrowdSec:** change the tag and digest in `airbound/Dockerfile`.

**Base image:** change the tag and digest of both `python:` stages in `airbound/Dockerfile`. Get the digest from Docker Hub's registry API for the multi-arch index.

**Relay server OS:** automatic. A restart that stays pending for more than 3 days, or updates that stop, show up in the report.

## Setting up the report's relay check

The report reads the relay's health with a token that only the relay and
GitHub know. The relay creates it at install time. Copy it into the
repository secret once, from a machine that has SSH access to the relay:

```sh
ssh root@<relay> '. /etc/airbound-controller/controller.env; printf %s "$OPS_TOKEN"' | gh secret set OPS_TOKEN -R oleost/HA-AirBound
```
