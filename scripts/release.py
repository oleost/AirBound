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


def tested_commit(kind: str, commit: str) -> tuple[str | None, str]:
    """The commit whose result counts: this one, or an earlier one when only documentation changed since."""
    if (RESULTS / f"{kind}-{commit[:12]}.json").exists():
        return commit, ""
    for path in sorted(RESULTS.glob(f"{kind}-*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        earlier = json.loads(path.read_text())["commit"]
        if subprocess.run(["git", "merge-base", "--is-ancestor", earlier, commit], cwd=ROOT).returncode != 0:
            continue
        changed = run(["git", "diff", "--name-only", earlier, commit], quiet=True).split()
        if all(f.endswith(".md") or f.startswith(("docs/", "assets/")) for f in changed):
            return earlier, f" (from {earlier[:12]}; only documentation changed since)"
    return None, ""


def passed(kind: str, commit: str) -> tuple[bool, str]:
    commit, note = tested_commit(kind, commit)
    if commit is None:
        return False, f"no {kind} result for this commit; run scripts/{kind.replace('-', '_')}.py"
    path = RESULTS / f"{kind}-{commit[:12]}.json"
    results = json.loads(path.read_text())["results"]
    failed = [k for k, v in results.items() if v["ok"] is False]
    skipped = [k for k, v in results.items() if v["ok"] is None]
    if failed:
        return False, f"{kind} failed: {failed}"
    return True, f"{kind} passed{note}" + (f" (skipped: {skipped})" if skipped else "")


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
    last_tag = run(["git", "describe", "--tags", "--abbrev=0", "--match", "v*", "origin/main"], check=False, quiet=True).strip()
    addon_changed = not last_tag or bool(run(["git", "diff", "--name-only", last_tag, commit, "--", "airbound"], quiet=True).strip())
    if addon_changed and run(["git", "tag", "-l", f"v{ver}"], quiet=True).strip():
        problems.append(f"the add-on changed but v{ver} is already released; bump the version in airbound/config.yaml")
    elif not addon_changed:
        notes.append(f"add-on unchanged since {last_tag}: main moves, without a new version")

    # The images come from the last commit that changed the add-on; later commits may only touch scripts.
    image_commit = run(["git", "log", "-1", "--format=%H", commit, "--", "airbound", ".github/workflows/build.yaml"], quiet=True).strip()
    builds = gh_api(f"repos/{REPO}/actions/workflows/build.yaml/runs?head_sha={image_commit}&per_page=5").get("workflow_runs") or []
    if not any(r["conclusion"] == "success" for r in builds):
        problems.append(f"no successful image build for {image_commit[:12]}, the last commit that changed the add-on")
    else:
        notes.append(f"images built from {image_commit[:12]}")
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

    relay_files = run(["git", "diff", "--name-only", "origin/main", commit, "--", *RELAY_PATHS], quiet=True).split()
    relay_changed = any(not f.endswith(".md") for f in relay_files)  # a README alone doesn't need a deploy
    if relay_changed:
        ok, note = passed("relay-staging", commit)
        (notes if ok else problems).append(note + " (the relay changed)")
    else:
        notes.append("relay unchanged since the last release")
    return problems, notes, relay_changed, addon_changed


def main() -> None:
    accept = sys.argv[sys.argv.index("--accept-critical") + 1] if "--accept-critical" in sys.argv else ""
    problems, notes, relay_changed, addon_changed = checks(accept)
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
    if not addon_changed:
        print(f"main -> {commit[:12]} (no add-on change, so no new version or GitHub release)")
        if relay_changed:
            subprocess.run([sys.executable, str(ROOT / "scripts/relay_deploy.py"), *[a for a in sys.argv[1:] if a.startswith("https://")]], check=True)
        return
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
