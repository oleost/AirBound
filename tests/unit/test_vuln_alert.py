"""scripts/vuln_alert.py: which govulncheck findings count, and that each finding is reported once."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import vuln_alert  # noqa: E402

REPORT = """=== Symbol Results ===

Vulnerability #1: GO-2026-0001
    HTTP/1 server crash due to a header race in
    net/http
  More info: https://pkg.go.dev/vuln/GO-2026-0001
  Standard library
    Found in: net/http@go1.27.3
    Fixed in: net/http@go1.27.4

Vulnerability #2: GO-2026-0002
    Panic on crafted frames
  More info: https://pkg.go.dev/vuln/GO-2026-0002

=== Package Results ===

Vulnerability #1: GO-2026-0003
    Imported but never called
"""


def test_only_reachable_vulnerabilities_count():
    assert vuln_alert.reachable_go_vulns(REPORT) == [
        ("GO-2026-0001", "HTTP/1 server crash due to a header race in net/http"),
        ("GO-2026-0002", "Panic on crafted frames"),
    ]


def test_clean_report_has_nothing():
    assert vuln_alert.reachable_go_vulns("No vulnerabilities found.\n") == []


def test_reported_list_survives_the_issue_body():
    keys = {"go:GO-2026-0001", "trivy:CVE-2026-1234"}
    assert vuln_alert.reported(vuln_alert.issue_body(keys)) == keys
    assert vuln_alert.reported(vuln_alert.issue_body(set())) == set()
    assert vuln_alert.reported("") == set()


def test_first_comment_says_watching_started():
    new = {"go:GO-2026-0001": "a", "trivy:CVE-2026-1234": "b"}
    assert "Watching started" in vuln_alert.comment(new, first=True)
    later = vuln_alert.comment({"go:GO-2026-0001": "a"})
    assert "1 new vulnerability," in later and "Trivy" not in later
