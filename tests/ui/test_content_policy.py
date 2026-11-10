"""The panel under its Content-Security-Policy in a real browser: its own pages work, an injected script doesn't run."""

from test_allowed_apps import fake_api
from test_users_step import playwright, server  # noqa: F401


def test_the_panel_works_under_its_policy_and_an_injected_script_does_not_run(server):  # noqa: F811
    fake = {"enabled": True, "allowed": [], "blocked": []}
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        tab = browser.new_page()
        problems = []
        tab.on("console", lambda message: problems.append(message.text) if "Content Security Policy" in message.text else None)
        tab.route("**/api/**", fake_api(fake))
        tab.goto(server + "#overview")
        tab.get_by_role("heading", name="Allowed apps").wait_for()
        assert tab.locator("[style]").count() > 0  # the pages use style attributes, which the policy must allow
        assert problems == [], problems
        # What a hole in the panel's escaping would give an attacker: markup with a script in it.
        tab.evaluate(
            """() => {
                const box = document.createElement("div");
                box.innerHTML = '<img src="static/missing.png" onerror="window.injected = 1">';
                document.body.append(box);
                const script = document.createElement("script");
                script.textContent = "window.injected = 2";
                document.body.append(script);
                const outside = document.createElement("script");
                outside.src = "https://example.invalid/x.js";
                document.body.append(outside);
            }"""
        )
        tab.wait_for_timeout(500)
        assert tab.evaluate("window.injected") is None
        assert len(problems) >= 3, problems
        browser.close()
