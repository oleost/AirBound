"""Weekly maintenance report: what's pinned, what's newer, what the scans say, how the relay is doing.

Runs in GitHub Actions (.github/workflows/maintenance.yaml) and posts the
report as a comment on the "Weekly maintenance report" issue, which emails the
repository owner. Items that need a decision come first, under "Action needed".
See MAINTENANCE.md for what to do about each.

Standard library only. Environment: GITHUB_TOKEN, GITHUB_REPOSITORY, and
optionally OPS_TOKEN (the relay's /api/v1/ops token) and RELAY_URL.
"""

from __future__ import annotations

import datetime
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = os.environ.get("GITHUB_REPOSITORY", "oleost/AirBound")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
NOW = datetime.datetime.now(datetime.UTC)
ISSUE_TITLE = "Weekly maintenance report"
SCAN_MAX_AGE_DAYS = 7
FRP_BUILD_MAX_AGE_DAYS = 30


def http(url: str, token: str = "", accept: str = "application/vnd.github+json", data: bytes | None = None, method: str | None = None):
    headers = {"Accept": accept, "User-Agent": "airbound-maintenance"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=30) as resp:
        body = resp.read()
    return body if accept.startswith("application/octet") or url.endswith("/zip") else json.loads(body or b"null")


def gh(path: str, **kw):
    return http(f"https://api.github.com/{path.lstrip('/')}", TOKEN, **kw)


def days_since(iso: str) -> float:
    return (NOW - datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds() / 86400


def find(pattern: str, path: str, default: str = "?") -> str:
    match = re.search(pattern, (ROOT / path).read_text(encoding="utf-8"), re.MULTILINE)
    return match.group(1) if match else default


def version_key(v: str) -> list[int]:
    return [int(x) for x in re.findall(r"\d+", v)]


# ---------- what we pin ----------

def pinned() -> dict:
    return {
        "frp": find(r'^\s*FRP_VERSION: "([^"]+)"', ".github/workflows/frp.yaml"),
        "frp_commit": find(r'^\s*FRP_COMMIT: "([0-9a-f]+)"', ".github/workflows/frp.yaml"),
        "frp_release": {
            "airbound/Dockerfile": find(r"^ARG FRP_RELEASE=(\S+)", "airbound/Dockerfile"),
            "airbound/airbound/relay_install.sh": find(r'^RELEASE="([^"]+)"', "airbound/airbound/relay_install.sh"),
            "relay-server/install.sh": find(r'^FRP_RELEASE="([^"]+)"', "relay-server/install.sh"),
            ".github/workflows/govulncheck.yaml": find(r'FRP_RELEASE: "([^"]+)"', ".github/workflows/govulncheck.yaml"),
        },
        "caddy_addon": find(r"FROM caddy:([\d.]+)-builder", "airbound/Dockerfile"),
        "caddy_relay": find(r'^CADDY_VERSION="([^"]+)"', "relay-server/install.sh"),
        "bouncer": find(r"caddy-crowdsec-bouncer/http@(v[\d.]+)", "airbound/Dockerfile"),
        "caddy_dns": find(r"caddy-dns/cloudflare@(v[\d.]+)", "airbound/Dockerfile"),
        "crowdsec": find(r"FROM crowdsecurity/crowdsec:(v[\d.]+)", "airbound/Dockerfile"),
        "python_image": find(r"FROM python:(\S+?)@", "airbound/Dockerfile"),
    }


# ---------- what's out there ----------

def latest_release(repo: str) -> tuple[str, str]:
    data = gh(f"repos/{repo}/releases/latest")
    return data["tag_name"], data["published_at"]


def latest_tag(repo: str) -> str:
    tags = [t["name"] for t in gh(f"repos/{repo}/tags?per_page=50") if re.fullmatch(r"v\d+\.\d+\.\d+", t["name"])]
    return max(tags, key=version_key) if tags else "?"


def go_stable() -> str:
    return http("https://go.dev/dl/?mode=json", accept="application/json")[0]["version"].removeprefix("go")


def newest_python_alpine(series: str) -> str:
    data = http(f"https://hub.docker.com/v2/repositories/library/python/tags?name={series}&page_size=100&ordering=last_updated", accept="application/json")
    tags = [t["name"] for t in data["results"] if re.fullmatch(rf"{re.escape(series)}\.\d+-alpine3\.\d+", t["name"])]
    return max(tags, key=version_key) if tags else "?"


def our_frp_build(tag: str) -> tuple[float | None, str]:
    """(age in days, Go version) of our frp release."""
    try:
        data = gh(f"repos/{REPO}/releases/tags/{tag}")
    except urllib.error.HTTPError:
        return None, "?"
    go = re.search(r"go(\d+\.\d+(?:\.\d+)?)", data.get("body") or "")
    return days_since(data["created_at"]), go.group(1) if go else "?"


def workflow_last_run(file: str) -> dict | None:
    runs = gh(f"repos/{REPO}/actions/workflows/{file}/runs?per_page=1&status=completed").get("workflow_runs") or []
    return runs[0] if runs else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def download_artifact(url: str) -> bytes:
    """GitHub answers with a redirect to a signed storage URL, which must be fetched without our token."""
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}", "User-Agent": "airbound-maintenance"})
    try:
        urllib.request.build_opener(_NoRedirect).open(request, timeout=30)
        raise urllib.error.URLError("no redirect")
    except urllib.error.HTTPError as err:
        if err.code not in (301, 302, 303, 307, 308):
            raise
        location = err.headers["Location"]
    with urllib.request.urlopen(urllib.request.Request(location, headers={"User-Agent": "airbound-maintenance"}), timeout=60) as resp:
        return resp.read()


def govulncheck_results(run: dict | None) -> dict[str, str]:
    if not run:
        return {}
    artifacts = gh(f"repos/{REPO}/actions/runs/{run['id']}/artifacts").get("artifacts") or []
    report = next((a for a in artifacts if a["name"] == "govulncheck-reports"), None)
    if not report:
        return {}
    try:
        blob = download_artifact(report["archive_download_url"])
    except (urllib.error.URLError, TimeoutError):
        return {}
    results = {}
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        for name in archive.namelist():
            text = archive.read(name).decode(errors="replace")
            line = next((l for l in text.splitlines() if l.startswith(("Your code is affected", "No vulnerabilities"))), "?")
            results[name.removeprefix("report-").removesuffix(".txt")] = line
    return results


def open_alerts() -> dict[str, dict[str, int]]:
    """component (path in the image) -> severity -> count, for open code-scanning alerts."""
    out: dict[str, dict[str, int]] = {}
    page = 1
    while True:
        try:
            alerts = gh(f"repos/{REPO}/code-scanning/alerts?state=open&per_page=100&page={page}")
        except urllib.error.HTTPError:
            return out
        for alert in alerts:
            path = ((alert.get("most_recent_instance") or {}).get("location") or {}).get("path") or "?"
            component = path.rsplit("/", 1)[-1] if "/" in path else ("Alpine packages" if "addon-airbound" in path else path)
            severity = (alert.get("rule") or {}).get("security_severity_level") or "other"
            out.setdefault(component, {}).setdefault(severity, 0)
            out[component][severity] += 1
        if len(alerts) < 100:
            return out
        page += 1


def relay_ops() -> dict | None:
    token = os.environ.get("OPS_TOKEN", "")
    if not token:
        return None
    try:
        return http(f"{os.environ.get('RELAY_URL', 'https://airbound.dev')}/api/v1/ops", token, accept="application/json")
    except (urllib.error.URLError, TimeoutError) as err:
        return {"error": str(err)}


# ---------- the report ----------

def build_report() -> str:
    actions: list[str] = []
    rows: list[str] = []
    p = pinned()

    releases = set(p["frp_release"].values())
    if len(releases) != 1:
        actions.append(f"frp release isn't the same everywhere: {p['frp_release']}")
    frp_release = next(iter(releases))
    frp_tag, frp_date = latest_release("fatedier/frp")
    age, built_with = our_frp_build(frp_release)
    go = go_stable()
    if version_key(frp_tag) > version_key(p["frp"]):
        actions.append(f"**frp {frp_tag}** is out (we build {p['frp']}). Update FRP_VERSION/FRP_COMMIT in frp.yaml.")
    if age is None:
        actions.append(f"Our frp release {frp_release} wasn't found.")
    elif age > FRP_BUILD_MAX_AGE_DAYS or version_key(built_with) < version_key(go):
        actions.append(f"Rebuild frp: our build is {age:.0f} days old, made with Go {built_with}; Go {go} is current.")
    rows.append(f"| frp | {p['frp']} ({frp_release}, Go {built_with}, {age or 0:.0f} days old) | {frp_tag} ({frp_date[:10]}), Go {go} |")

    for name, pin, repo in (("Caddy (add-on)", p["caddy_addon"], "caddyserver/caddy"), ("Caddy (relay website)", p["caddy_relay"], "caddyserver/caddy"), ("CrowdSec", p["crowdsec"], "crowdsecurity/crowdsec")):
        tag, date = latest_release(repo)
        if version_key(tag) > version_key(pin):
            actions.append(f"**{name}**: {tag} is out (we use {pin}).")
        rows.append(f"| {name} | {pin} | {tag} ({date[:10]}) |")
    for name, pin, repo in (("Caddy CrowdSec bouncer", p["bouncer"], "hslatman/caddy-crowdsec-bouncer"), ("Caddy Cloudflare DNS", p["caddy_dns"], "caddy-dns/cloudflare")):
        tag = latest_tag(repo)
        if tag != "?" and version_key(tag) > version_key(pin):
            actions.append(f"**{name}**: {tag} is out (we use {pin}).")
        rows.append(f"| {name} | {pin} | {tag} |")
    series = ".".join(p["python_image"].split(".")[:2])
    newest = newest_python_alpine(series)
    if newest != "?" and version_key(newest) > version_key(p["python_image"]):
        actions.append(f"**Python base image**: {newest} is out (we use {p['python_image']}); update the tag and digest in the Dockerfile.")
    rows.append(f"| Python base image | {p['python_image']} | {newest} |")
    ha_tag, ha_date = latest_release("home-assistant/core")
    rows.append(f"| Home Assistant | (tested on the test VM) | {ha_tag} ({ha_date[:10]}) |")
    if days_since(ha_date) < 8:
        actions.append(f"Home Assistant {ha_tag} came out this week: update the test VM to it, then run `python scripts/e2e.py`.")
    ha_beta = http("https://version.home-assistant.io/beta.json", accept="application/json")["homeassistant"]["default"]
    if "b" in ha_beta:  # between releases the beta channel carries the stable version
        rows.append(f"| Home Assistant beta | (tested on the beta VM) | {ha_beta} |")
        actions.append(f"Home Assistant {ha_beta} is in beta: run `python scripts/beta.py` before it's released.")

    scan_lines = []
    for file, label in (("scan.yaml", "Trivy image scan"), ("govulncheck.yaml", "govulncheck"), ("build.yaml", "Image build")):
        run = workflow_last_run(file)
        if not run:
            actions.append(f"{label} has never run.")
            continue
        age_days = days_since(run["created_at"])
        if run["conclusion"] != "success":
            actions.append(f"{label}: last run {run['conclusion']} ({run['html_url']}).")
        elif file != "build.yaml" and age_days > SCAN_MAX_AGE_DAYS:
            actions.append(f"{label} is {age_days:.0f} days old; run it again.")
        scan_lines.append(f"- {label}: {run['conclusion']}, {age_days:.1f} days ago")
    reach = govulncheck_results(workflow_last_run("govulncheck.yaml"))
    alerts = open_alerts()
    critical = sum(v.get("critical", 0) for v in alerts.values())
    if critical:
        actions.append(f"{critical} critical image finding(s) are open in the Security tab.")

    dependabot = [pr for pr in gh(f"repos/{REPO}/pulls?state=open&per_page=50") if pr["user"]["login"].startswith("dependabot")]
    if dependabot:
        actions.append(f"{len(dependabot)} Dependabot pull request(s) wait for review: " + ", ".join(f"#{pr['number']}" for pr in dependabot))

    ops = relay_ops()
    relay_lines = []
    if ops is None:
        relay_lines.append("- Not checked: the OPS_TOKEN secret isn't set (see MAINTENANCE.md).")
    elif "error" in ops:
        actions.append(f"Couldn't reach the relay's health endpoint: {ops['error']}")
    else:
        if ops.get("reboot_required") and (ops.get("uptime_days") or 0) > 3:
            actions.append(f"The relay needs a restart and has been up {ops['uptime_days']} days (the nightly restart isn't happening).")
        if (ops.get("days_since_unattended_upgrade") or 99) > 3:
            actions.append(f"The relay's automatic updates last ran {ops.get('days_since_unattended_upgrade')} days ago.")
        if (ops.get("disk_used_percent") or 0) > 80:
            actions.append(f"The relay's disk is {ops['disk_used_percent']}% full.")
        if ops.get("website_certificate_days_left") is not None and ops["website_certificate_days_left"] < 20:
            actions.append(f"airbound.dev's certificate expires in {ops['website_certificate_days_left']} days.")
        if ops.get("frp_build") != frp_release:
            actions.append(f"The relay runs {ops.get('frp_build') or 'unknown frp'}, not {frp_release}: deploy the relay.")
        for problem in ops.get("problems") or []:
            actions.append(f"Relay: {problem}")
        relay_lines += [f"- {k.replace('_', ' ')}: {v}" for k, v in ops.items() if k != "problems"]

    lines = [f"## Maintenance report, {NOW:%d %b %Y}", ""]
    lines += ["### Action needed", ""] + ([f"- [ ] {a}" for a in actions] or ["Nothing. Everything is current."]) + [""]
    lines += ["### Versions", "", "| Part | We use | Newest |", "| --- | --- | --- |"] + rows + [""]
    lines += ["### Scans", ""] + scan_lines + [""]
    if reach:
        lines += ["Reachable Go vulnerabilities (govulncheck):", ""] + [f"- {k}: {v}" for k, v in sorted(reach.items())] + [""]
    if alerts:
        lines += ["Open image findings (Trivy, Security tab):", ""] + [f"- {k}: " + ", ".join(f"{n} {s}" for s, n in sorted(v.items())) for k, v in sorted(alerts.items())] + [""]
    lines += ["### Relay (airbound.dev)", ""] + relay_lines + ["", "_What to do about each item: MAINTENANCE.md._"]
    return "\n".join(lines)


def post(report: str) -> None:
    issues = gh(f"repos/{REPO}/issues?state=open&labels=maintenance&per_page=20")
    issue = next((i for i in issues if i["title"] == ISSUE_TITLE), None)
    if issue is None:
        issue = gh(f"repos/{REPO}/issues", data=json.dumps({"title": ISSUE_TITLE, "labels": ["maintenance"],
                                                               "body": "One comment a week from .github/workflows/maintenance.yaml. Keep this issue open; GitHub emails each new comment."}).encode(), method="POST")
    gh(f"repos/{REPO}/issues/{issue['number']}/comments", data=json.dumps({"body": report}).encode(), method="POST")
    print(f"posted to {issue['html_url']}")


if __name__ == "__main__":
    text = build_report()
    print(text)
    if "--post" in sys.argv:
        post(text)
