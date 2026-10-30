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
| Home Assistant | Not ours; changes monthly, with a beta about a week before | Maintenance report, test VM, beta VM |
| Secrets | `/etc/airbound-controller` on the relay; repository secret `OPS_TOKEN` | Patreon's creator token refreshes itself; the Cloudflare token and the GitHub token for the sponsor list (`read:user`, in `github.env`) have their own expiry |

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
- **When Home Assistant has a beta out** (the report says so, about a week before each monthly release): run `python scripts/beta.py`. A failure doesn't block other releases, but fix it and release before HA's release day, or report it to Home Assistant if the fault is theirs.
  - Release through the normal path below.
- **Within 48 hours:** a critical vulnerability that the internet can reach. That means frps on the relays, Caddy, Home Assistant's login path, or OpenSSL/Python in the add-on. Fix it, test it, release it.
- **Release gate:** `scripts/release.py` refuses to promote a release when the last scan is older than 7 days, a scan failed, or a critical finding is open, unless the release is the fix for it. Test results count only for the exact commit they ran on; commits that change nothing but documentation (`*.md`, `docs/`, `assets/`) may reuse the last result.

## From change to users

```text
push to next ──▶ pre-push hook: unit, relay and browser tests (a failure stops the push)
      │
      ▶ CI: unit and relay tests, images built, Trivy, govulncheck
      │
      ▶ test VM (follows next) ──▶ python scripts/e2e.py: hosted relay, MFA guard and
      │                           bypass probes, MFA enforcement, own relay on a fresh test VPS
      │
      ▶ relay changes: python scripts/relay_staging.py ──▶ the relay installed on the test VPS
      │
      ▶ python scripts/release.py ──▶ main = this commit (users update), then
                                     python scripts/relay_deploy.py ──▶ airbound.dev, with automatic rollback
```

- **Branches:** `next` is development and testing. The test VM follows it (`https://github.com/oleost/AirBound#next`). `main` is what users get: everyone who added the repository without a branch name follows it. Home Assistant keeps the branch it cloned, so `main` must stay the users' branch. A release fast-forwards `main` to a tested commit on `next`. The images for that version already exist, so users never see an update whose image is still building. Work happens on `next`, never directly on `main`.
- **Pushes:** `.githooks/pre-push` runs the unit, relay and browser tests and refuses the push when one fails. A clone enables it once with `git config core.hooksPath .githooks`.
- **A result belongs to one commit:** `e2e.py`, `beta.py` and `relay_staging.py` note the commit when they start and refuse to start with uncommitted or unpushed changes (the VMs install from GitHub). A run during which HEAD or the files changed is saved as failed, so `release.py` can't take it for a commit it didn't test. Commit and push first, then start the run, and don't commit while it runs.
- **Test round** (`scripts/e2e.py`, about 20 minutes; the test environment's private addresses go in `.env`, listed in `scripts/testenv.py`):
  - Deploys `next` to the test VM.
  - Checks the hosted relay end to end, and from outside that none of 11 spellings of the login path finishes a login without MFA.
  - Runs MFA enforcement with a throwaway user.
  - Installs the add-on's own-relay command on the test VPS rolled back to its `fresh` snapshot, links the test VM through it, goes back to the hosted relay, and stops the VPS.
- **Beta check** (`scripts/beta.py`, about 15 minutes): starts the beta VM (Home Assistant OS on the beta channel, stopped otherwise), updates the Supervisor, HA OS and Home Assistant to the newest beta, installs or updates AirBound from `next`, and runs all integration tests with a self-signed address. They include the restore test (`tests/integration/test_restore.py`): a backup of AirBound must leave out the machine key, `frpc.toml` and the logs; after restoring it the address stays off, the panel and a notification ask first, and confirming brings remote access back. The test VM skips that test, since a restore there would need a Patreon login. Then the VM is stopped.
- **Hosted restore** (`scripts/hosted_restore.py`, about 5 minutes, needs the maintainer's Patreon login): restores a backup of AirBound on the test VM, checks that the address stays off until confirmed, links again and checks that the same address comes back. Run it when the restore or linking flow changes.
- **Copied backup** (`scripts/copied_backup.py`, about 20 minutes, needs the maintainer's Patreon login): restores the test VM's AirBound backup on the beta VM, tries the copied key with a real frpc from the test VPS, checks that airbound.dev refuses it and the original warns, then links the test VM again. Run it when the machine check or linking changes.
- **Relay staging** (`scripts/relay_staging.py`): installs `relay-server/` on the test VPS, then checks:
  - that every service starts with its sandbox
  - that the frp plugin refuses fake clients
  - that a real device key is accepted from the machine that claimed it (read from frpc's environment, as the add-on does) and refused from another
  - that traffic metering reads the hub
- **Relay deploy** (`scripts/relay_deploy.py`):
  1. Keeps a copy of the running relay.
  2. Installs the new one and checks the services, the website and a member's address.
  3. Puts the copy back if anything fails.
- **Afterwards:** watch the next report, and the relay's alerts.

## Known Home Assistant behaviour we work around

Not reported upstream; we handle them ourselves. Check after each Home Assistant release whether they still apply.

- **Ingress after restoring an app from another machine** (seen with Supervisor 2026.10.4). The backup brings the other machine's ingress token, and `restore()` in `supervisor/apps/app.py` doesn't reload the Supervisor's token list (update and rebuild do). The panel answers `503: Service Unavailable` until the Supervisor restarts. Home Assistant's UI has no Supervisor restart; *Reboot system* (Settings → System → power button → More options) fixes it. AirBound's restore notification (`checks.py`, the "restored" check) tells people so, and `scripts/copied_backup.py` restarts the Supervisor through the API. A restore on the same machine isn't affected. If a Supervisor release adds the reload, drop the hint and the restart in the script.

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
ssh root@<relay> '. /etc/airbound-controller/controller.env; printf %s "$OPS_TOKEN"' | gh secret set OPS_TOKEN -R oleost/AirBound
```
