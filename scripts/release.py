"""Release gate: promote the tested commit on next to main (what users get), then deploy the relay.

    python scripts/release.py                 check everything, then release
    python scripts/release.py --check         only check
    python scripts/release.py --accept-critical "why"   release despite open critical findings (e.g. this is the fix)

Refuses unless:
- next is checked out, clean and pushed, and its image build succeeded, with images for this version;
- this version has no tag yet;
- the Trivy and govulncheck runs are successful and less than 7 days old;
- no critical image finding is open (unless accepted, with a reason);
- scripts/e2e.py passed for this exact commit, and, when relay-server/ or the
  hardening changed since the last release, scripts/relay_staging.py too.
Then moves main to this commit (fast-forward only), tags it, creates a GitHub
release, and runs scripts/relay_deploy.py when the relay changed. Users'
Home Assistants follow main; the test VM follows next.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys

from testenv import RESULTS, ROOT, head_commit, run

from deploy import image_published  # noqa: E402

REPO = "oleost/HA-AirBound"
SCAN_MAX_AGE_DAYS = 7
RELAY_PATHS = ["relay-server", "airbound/airbound/vps_harden.sh"]


def gh_api(path: str):
    return json.loads(run(["gh", "api", path], quiet=True) or "null")


def version() -> str:
    return re.search(r'^version: "([^"]+)"', (ROOT / "airbound/config.yaml").read_text(encoding="utf-8"), re.M).group(1)


def passed(kind: str, commit: str) -> tuple[bool, str]:
    path = RESULTS / f"{kind}-{commit[:12]}.json"
    if not path.exists():
        return False, f"no {kind} result for {commit[:12]}; run scripts/{kind.replace('-', '_')}.py"
    results = json.loads(path.read_text())["results"]
    failed = [k for k, v in results.items() if v["ok"] is False]
    skipped = [k for k, v in results.items() if v["ok"] is None]
    if failed:
        return False, f"{kind} failed: {failed}"
    return True, f"{kind} passed" + (f" (skipped: {skipped})" if skipped else "")


def checks(accept_critical: str) -> tuple[list[str], list[str], bool]:
    problems, notes = [], []
    commit, ver = head_commit(), version()
    if run(["git", "rev-parse", "--abbrev-ref", "HEAD"], quiet=True).strip() != "next":
        problems.append("not on next (releases come from the test branch)")
    if run(["git", "status", "--porcelain", "airbound", "relay-server", "scripts", ".github"], quiet=True).strip():
        problems.append("uncommitted changes")
    run(["git", "fetch", "-q", "origin"], quiet=True)
    if run(["git", "rev-parse", "origin/next"], quiet=True).strip() != commit:
        problems.append("next isn't pushed (or is behind origin)")
    if run(["git", "tag", "-l", f"v{ver}"], quiet=True).strip():
        problems.append(f"v{ver} is already released; bump the version in airbound/config.yaml")

    builds = gh_api(f"repos/{REPO}/actions/workflows/build.yaml/runs?head_sha={commit}&per_page=5").get("workflow_runs") or []
    if not any(r["conclusion"] == "success" for r in builds):
        problems.append(f"no successful image build for {commit[:12]}")
    missing = [a for a in ("amd64", "aarch64") if not image_published(a, ver)]
    if missing:
        problems.append(f"images for {ver} missing: {missing}")

    from datetime import UTC, datetime
    for file, label in (("scan.yaml", "Trivy"), ("govulncheck.yaml", "govulncheck")):
        runs = gh_api(f"repos/{REPO}/actions/workflows/{file}/runs?status=completed&per_page=1").get("workflow_runs") or []
        if not runs:
            problems.append(f"{label} has never run")
            continue
        age = (datetime.now(UTC) - datetime.fromisoformat(runs[0]["created_at"].replace("Z", "+00:00"))).days
        if runs[0]["conclusion"] != "success":
            problems.append(f"{label}'s last run {runs[0]['conclusion']}")
        elif age > SCAN_MAX_AGE_DAYS:
            problems.append(f"{label} is {age} days old; run it (gh workflow run {file})")
        else:
            notes.append(f"{label}: {runs[0]['conclusion']}, {age} days old")

    alerts = gh_api(f"repos/{REPO}/code-scanning/alerts?state=open&severity=critical&per_page=100") or []
    if alerts and not accept_critical:
        problems.append(f"{len(alerts)} critical image finding(s) open; fix them, or --accept-critical \"reason\"")
    elif alerts:
        notes.append(f"{len(alerts)} critical finding(s) accepted: {accept_critical}")

    ok, note = passed("e2e", commit)
    (notes if ok else problems).append(note)

    relay_changed = bool(run(["git", "diff", "--name-only", "origin/main", commit, "--", *RELAY_PATHS], quiet=True).strip())
    if relay_changed:
        ok, note = passed("relay-staging", commit)
        (notes if ok else problems).append(note + " (the relay changed)")
    else:
        notes.append("relay unchanged since the last release")
    return problems, notes, relay_changed


def main() -> None:
    accept = sys.argv[sys.argv.index("--accept-critical") + 1] if "--accept-critical" in sys.argv else ""
    problems, notes, relay_changed = checks(accept)
    for note in notes:
        print(f"✓ {note}")
    for problem in problems:
        print(f"✗ {problem}")
    if problems:
        raise SystemExit("\nNot released.")
    if "--check" in sys.argv:
        print("\nReady to release.")
        return

    ver, commit = version(), head_commit()
    run(["git", "push", "origin", f"{commit}:refs/heads/main"])  # fast-forward only: git refuses anything else
    run(["git", "tag", "-a", f"v{ver}", commit, "-m", f"AirBound {ver}"])
    run(["git", "push", "origin", f"v{ver}"])
    previous = run(["git", "describe", "--tags", "--abbrev=0", f"v{ver}^"], check=False, quiet=True).strip()
    log = run(["git", "log", "--no-merges", "--format=- %s", f"{previous}..{commit}" if previous else commit, "-30"], quiet=True)
    run(["gh", "release", "create", f"v{ver}", "-R", REPO, "--title", f"AirBound {ver}", "--notes", log or f"AirBound {ver}"], quiet=True)
    print(f"Released {ver}: main -> {commit[:12]}")
    if relay_changed:
        subprocess.run([sys.executable, str(ROOT / "scripts/relay_deploy.py"), *[a for a in sys.argv[1:] if a.startswith("https://")]], check=True)


if __name__ == "__main__":
    main()
