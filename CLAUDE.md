# AirBound: notes for Claude Code

AirBound is a Home Assistant OS add-on for secure remote access (`airbound/`),
plus the relay server behind the hosted service at airbound.dev
(`relay-server/`). How releases, tests and updates work is in
[MAINTENANCE.md](MAINTENANCE.md); read it before releasing or bumping versions.

## Rules

- **English everywhere** in the repository: code, comments, docs, commit messages, UI text.
- **Branches:** work on `next` only. `main` is what users' Home Assistants follow and moves only through `scripts/release.py`, which Claude runs when the maintainer asks for a release. Never push to `main` directly.
- **Privacy:** this repository is public. Before every push, scan the staged diff for private addresses, hostnames, tokens and names. Private values belong in `.env` (gitignored, listed in `scripts/testenv.py`) or on the relay under `/etc/airbound-controller`, never in the repository. `test-results/` is gitignored because screenshots show real addresses.
- **Commits** use the repository's GitHub noreply identity (set in the repo-local git config).
- **Files:** LF line endings (enforced by `.gitattributes`; `relay_install.sh` breaks with CRLF). Read and write text with `encoding="utf-8"` explicitly, since the Windows default is cp1252.
- **The add-on UI is the documentation.** The panel is a guided wizard: every step says what to do and why, locked steps say what they wait for. A feature that needs reading docs to set up isn't finished.
- **Claude runs everything.** The maintainer works only through Claude Code and never runs scripts by hand. Run the tests, the test round, staging and the release yourself, and when a step is due (a test round before a release, the beta check), run it or say it is due; never leave it as a step for the maintainer.
- **Test it yourself** with the scripts below instead of handing manual testing to the maintainer.
- **Test VMs stay stopped** except while a script uses them; the scripts stop them on exit.
- **Pushes are gated.** `.githooks/pre-push` runs the unit, relay and browser tests and refuses the push if any fail (a clone enables it with `git config core.hooksPath .githooks`). Never bypass it with `--no-verify`, and judge test runs by their exit code, never by output piped through `tail`.
- **A test result belongs to one commit.** `e2e.py`, `beta.py` and `relay_staging.py` refuse to start with uncommitted or unpushed changes, and a run during which HEAD or the files changed is saved as failed. Don't commit while one of them runs; commit, push, then start the run.

## Layout

- `airbound/airbound/`: the add-on (aiohttp panel in `web.py`, wizard steps in `steps.py`, front end in `static/`).
- `relay-server/`: `install.sh` for a relay server and the controller (`controller/airbound_relay/`: Patreon/GitHub linking, frp plugin, ops endpoint, pages).
- `scripts/`: test round, deploy and release tooling (see below).
- `tests/unit`, `tests/ui` (Playwright), `tests/relay_server`: run in CI and locally. `tests/integration`: against a real Home Assistant, needs `.env`.

## Commands

```sh
.venv/Scripts/python -m pytest                       # unit tests
.venv/Scripts/python -m pytest tests/ui tests/relay_server
.venv/Scripts/python scripts/deploy.py               # next onto the test VM
.venv/Scripts/python scripts/e2e.py                  # full test round, required before a release
.venv/Scripts/python scripts/relay_staging.py        # relay changes, on the test VPS
.venv/Scripts/python scripts/beta.py                 # Home Assistant's beta channel
```

`scripts/release.py` (on the maintainer's request) refuses to release a commit without fresh
scans and a passing test round for that exact commit; relay changes also need
relay staging, and it then runs `scripts/relay_deploy.py`.
Without flags it releases at once, with no prompt: run `--check` first.
