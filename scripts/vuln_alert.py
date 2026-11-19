"""Daily: email the maintainer as soon as a scan finds a vulnerability it hasn't reported before.

Runs in GitHub Actions (.github/workflows/vuln-alert.yaml) after the daily
Trivy scan (scan.yaml) and govulncheck (govulncheck.yaml). It reads
- open Trivy findings rated high or critical, from the Security tab, and
- vulnerabilities govulncheck says our Go programs can actually reach
  (frps and Caddy on the relays, the binaries in the add-on image),
and comments on the "Security alerts" issue when one of them is new. GitHub
emails every new comment. Each finding is reported once: the issue body keeps
the list already reported in a hidden marker. The weekly maintenance report
still lists everything that is open.

Standard library only. Environment: GITHUB_TOKEN, GITHUB_REPOSITORY.
"""

from __future__ import annotations

import io
import json
import re
import sys
import urllib.error
import zipfile

from maintenance_report import NOW, REPO, download_artifact, gh, workflow_last_run

ISSUE_TITLE = "Security alerts"
LABEL = "security"
SEVERITIES = ("critical", "high")
MARKER = re.compile(r"<!-- reported: ([^>]*) -->")


def reachable_go_vulns(report: str) -> list[tuple[str, str]]:
    """(GO id, summary) for each vulnerability in govulncheck's symbol results, the reachable ones."""
    symbols = re.split(r"^=== (?:Package|Module) Results ===", report, flags=re.MULTILINE)[0]
    found = []
    for match in re.finditer(r"^Vulnerability #\d+: (GO-\d{4}-\d+)\n((?:    \S.*\n)+)", symbols, re.MULTILINE):
        if match.group(1) not in (f[0] for f in found):
            found.append((match.group(1), " ".join(match.group(2).split())))
    return found


def govulncheck_findings() -> dict[str, str]:
    """key -> one line for the email, from the newest govulncheck run's reports."""
    run = workflow_last_run("govulncheck.yaml")
    if not run or run.get("conclusion") != "success":
        return {}
    artifacts = gh(f"repos/{REPO}/actions/runs/{run['id']}/artifacts").get("artifacts") or []
    report = next((a for a in artifacts if a["name"] == "govulncheck-reports"), None)
    if not report:
        return {}
    by_id: dict[str, tuple[str, list[str]]] = {}
    with zipfile.ZipFile(io.BytesIO(download_artifact(report["archive_download_url"]))) as archive:
        for name in sorted(archive.namelist()):
            binary = name.removeprefix("report-").removesuffix(".txt")
            for vuln_id, summary in reachable_go_vulns(archive.read(name).decode(errors="replace")):
                by_id.setdefault(vuln_id, (summary, []))[1].append(binary)
    return {f"go:{i}": f"[{i}](https://pkg.go.dev/vuln/{i}) (reachable in {', '.join(b)}): {s}" for i, (s, b) in by_id.items()}


def trivy_findings() -> dict[str, str]:
    """key -> one line for the email, from the open code-scanning alerts rated high or critical."""
    by_rule: dict[str, tuple[str, str, set[str]]] = {}
    page = 1
    while True:
        alerts = gh(f"repos/{REPO}/code-scanning/alerts?state=open&per_page=100&page={page}")
        for alert in alerts:
            rule = alert.get("rule") or {}
            severity = rule.get("security_severity_level") or ""
            if severity not in SEVERITIES:
                continue
            path = ((alert.get("most_recent_instance") or {}).get("location") or {}).get("path") or "?"
            component = path.rsplit("/", 1)[-1] if "/" in path else "Alpine packages"
            entry = by_rule.setdefault(rule.get("id") or "?", (severity, rule.get("description") or "", set()))
            entry[2].add(component)
        if len(alerts) < 100:
            break
        page += 1
    return {f"trivy:{r}": f"{r}, {sev} (in {', '.join(sorted(c))}): {d}" for r, (sev, d, c) in by_rule.items()}


def reported(body: str) -> set[str]:
    match = MARKER.search(body or "")
    return set(match.group(1).split()) if match else set()


def issue_body(keys: set[str]) -> str:
    return ("New vulnerabilities in what AirBound ships or runs, found by the daily scans "
            "(`.github/workflows/vuln-alert.yaml`). Keep this issue open: GitHub emails each new comment. "
            "What to do about a finding: MAINTENANCE.md.\n\n"
            f"<!-- reported: {' '.join(sorted(keys))} -->")


def comment(new: dict[str, str], first: bool = False) -> str:
    go = [v for k, v in sorted(new.items()) if k.startswith("go:")]
    trivy = [v for k, v in sorted(new.items()) if k.startswith("trivy:")]
    if first:
        heading = [f"## Watching started, {NOW:%d %b %Y}", "",
                   f"These {len(new)} were already open; from now on only new ones are reported here.", ""]
    else:
        heading = [f"## {len(new)} new vulnerabilit{'y' if len(new) == 1 else 'ies'}, {NOW:%d %b %Y}", ""]
    lines = heading
    if go:
        lines += ["### Reachable in our Go programs (govulncheck)", ""] + [f"- {v}" for v in go] + [""]
    if trivy:
        lines += ["### In the add-on image (Trivy, high or critical, a fix exists)", ""] + [f"- {v}" for v in trivy] + [""]
    lines += [f"Details: [Security tab](https://github.com/{REPO}/security/code-scanning) and the "
              f"[govulncheck runs](https://github.com/{REPO}/actions/workflows/govulncheck.yaml)."]
    return "\n".join(lines)


def main() -> None:
    findings = {**govulncheck_findings(), **trivy_findings()}
    issues = gh(f"repos/{REPO}/issues?state=open&labels={LABEL}&per_page=20")
    issue = next((i for i in issues if i["title"] == ISSUE_TITLE), None)
    known = reported(issue["body"]) if issue else set()
    new = {k: v for k, v in findings.items() if k not in known}
    print(f"{len(findings)} open findings, {len(new)} new")
    if not new:
        return
    text = comment(new, first=issue is None)
    print(text)
    if "--post" not in sys.argv:
        return
    if issue is None:
        issue = gh(f"repos/{REPO}/issues", data=json.dumps({"title": ISSUE_TITLE, "labels": [LABEL], "body": issue_body(set())}).encode(), method="POST")
    gh(f"repos/{REPO}/issues/{issue['number']}/comments", data=json.dumps({"body": text}).encode(), method="POST")
    gh(f"repos/{REPO}/issues/{issue['number']}", data=json.dumps({"body": issue_body(known | set(new))}).encode(), method="PATCH")
    print(f"posted to {issue['html_url']}")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as err:
        sys.exit(f"GitHub API: {err.code} {err.reason} for {err.url}")
