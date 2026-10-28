"""The few HTML pages people see on the relay's website. Plain HTML, no scripts."""

from __future__ import annotations

from html import escape

from .config import Config

HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; form-action 'self' https://www.patreon.com; frame-ancestors 'none'; base-uri 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}

LOGO = """<svg viewBox="0 0 512 512" width="44" height="44" aria-hidden="true"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#1A9BF0"/><stop offset="1" stop-color="#0A5FA3"/></linearGradient></defs><rect width="512" height="512" rx="112" fill="url(#g)"/><g fill="none" stroke="#fff" stroke-linecap="round" stroke-width="26"><path d="M207 150 A70 70 0 0 1 305 150"/><path d="M168 112 A124 124 0 0 1 344 112" opacity=".6"/></g><path d="M132 304 L256 200 L380 304 V384 a24 24 0 0 1 -24 24 H156 a24 24 0 0 1 -24 -24 Z" fill="none" stroke="#fff" stroke-width="28" stroke-linejoin="round"/><path d="M236 330 v-16 a20 20 0 0 1 40 0 v16" fill="none" stroke="#fff" stroke-width="13" stroke-linecap="round"/><rect x="222" y="328" width="68" height="54" rx="11" fill="#fff"/></svg>"""

# The browser tab icon: the logo from assets/logo.svg, served at /favicon.svg and /favicon.ico.
ICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#1A9BF0"/><stop offset="1" stop-color="#0A5FA3"/></linearGradient></defs><rect width="512" height="512" rx="112" fill="url(#g)"/><g fill="none" stroke="#fff" stroke-linecap="round" stroke-width="26"><path d="M207 150 A70 70 0 0 1 305 150"/><path d="M168 112 A124 124 0 0 1 344 112" opacity=".6"/></g><path d="M132 304 L256 200 L380 304 V384 a24 24 0 0 1 -24 24 H156 a24 24 0 0 1 -24 -24 Z" fill="none" stroke="#fff" stroke-width="28" stroke-linejoin="round"/><path d="M236 330 v-16 a20 20 0 0 1 40 0 v16" fill="none" stroke="#fff" stroke-width="13" stroke-linecap="round"/><rect x="222" y="328" width="68" height="54" rx="11" fill="#fff"/></svg>"""

STYLE = """
:root{--bg:#f4f7fb;--card:#fff;--ink:#14324d;--muted:#55697d;--accent:#0A72C4;--accent-ink:#fff;--line:#dfe6ee}
@media (prefers-color-scheme:dark){:root{--bg:#0f1720;--card:#16212d;--ink:#e6eef6;--muted:#9fb0c1;--accent:#3aa5f2;--accent-ink:#06121d;--line:#26384a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:640px;margin:0 auto;padding:32px 16px 48px}
header{display:flex;align-items:center;gap:12px;margin-bottom:24px}header b{font-size:22px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:24px;margin-bottom:16px}
h1{font-size:24px;margin:0 0 12px}h2{font-size:18px;margin:20px 0 8px}p{margin:0 0 12px}.muted{color:var(--muted)}
.code{font:600 32px/1.2 ui-monospace,Consolas,monospace;letter-spacing:4px;text-align:center;padding:16px;border:2px dashed var(--line);border-radius:10px;margin:16px 0}
.button{display:inline-block;background:var(--accent);color:var(--accent-ink);text-decoration:none;font-weight:600;padding:12px 20px;border-radius:10px;margin:4px 8px 4px 0}
.button.secondary{background:transparent;color:var(--accent);border:1px solid var(--accent)}
footer{font-size:14px;color:var(--muted);text-align:center}footer a{color:inherit}
.card a:not(.button){color:var(--accent)}
ul{padding-left:20px}li{margin-bottom:6px}code{font-family:ui-monospace,Consolas,monospace}
"""


def page(title: str, body: str, config: Config) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)} · AirBound</title><link rel="icon" href="/favicon.svg" type="image/svg+xml"><style>{STYLE}</style></head>
<body><main><header>{LOGO}<b>AirBound</b></header>{body}
<footer><a href="/">Home</a> · <a href="/terms">Terms</a> · <a href="/privacy">Privacy</a> ·<a href="https://github.com/oleost/AirBound">Source code</a> · <a href="{escape(config.manage_url)}">Patreon</a></footer>
</main></body></html>"""


def home(config: Config) -> str:
    supporters = "Supporters on Patreon or GitHub Sponsors" if config.github_sponsors else "Supporters on Patreon"
    return f"""<div class="card"><h1>Secure remote access for Home Assistant</h1>
<p>AirBound gives your Home Assistant its own secure web address that works in the Companion app and any browser, without a VPN and without opening ports on your router.</p>
<p>AirBound is a free, open-source hobby project, made by one person in their spare time. {supporters} help pay for its development and get access to this hosted relay as a thank-you. Install the AirBound add-on in Home Assistant, choose <b>AirBound hosted relay</b>, and it walks you through the rest. New supporters get a 7-day free trial.</p>
<a class="button" href="https://github.com/oleost/AirBound">Get the add-on</a>
<a class="button secondary" href="{escape(config.join_url)}">Support on Patreon</a></div>
<div class="card"><h2>Please read before you join</h2>
<p>The relay is offered as is, on a best-effort basis, with no guarantee that it's always up and no liability if it isn't. Don't rely on it for anything where an outage could cause harm, such as alarms or safety equipment. The add-on also works with your own server or router, so you can always move. <a href="/terms">Read the terms</a>.</p></div>
<div class="card"><h2>What the relay can and can't see</h2><ul>
<li>Traffic is encrypted all the way to your Home Assistant. The relay passes it on by the address name and can't read it.</li>
<li>The certificate for your address is made by your own Home Assistant. The relay never has its key.</li>
<li>The relay knows which addresses connect and when, and how much data passes. It never sees passwords, pages or camera images.</li>
</ul></div>"""


def privacy(config: Config) -> str:
    account = (
        "<b>Your Patreon or GitHub user id</b> and whether your membership or sponsorship includes the relay."
        if config.github_sponsors
        else "<b>Your Patreon user id</b> and whether your membership includes the relay."
    )
    return f"""<div class="card"><h1>Privacy</h1>
<p>The hosted relay keeps as little as possible:</p><ul>
<li>{account} Not your name or email address.</li>
<li><b>For each linked Home Assistant:</b> its random address on {escape(config.domain)}, a hash of its key and of a random key for the machine it runs on, when it was last connected, and how much data it used each month (for the fair-use limit, kept for three months).</li>
<li><b>Connection logs</b> (address and time) are kept for at most 14 days to stop abuse.</li>
</ul>
<p>Traffic to your Home Assistant is end-to-end encrypted. The relay can't read it, and nothing about it is stored.</p>
<p>Unlinking a Home Assistant or ending your membership removes access; ask on GitHub to have your data deleted sooner.</p></div>"""


TERMS_UPDATED = "28 October 2026"


def terms(config: Config) -> str:
    if config.github_sponsors:
        support = (
            "<h2>What Patreon and GitHub support is for</h2>\n"
            "<p>A membership on Patreon or a monthly sponsorship on GitHub supports the development of AirBound. "
            "As a thank-you, members of the <b>Relay</b> tier on Patreon and monthly GitHub sponsors may use the hosted relay"
        )
        payments = (
            'Patreon and GitHub handle all payments, cancelling and refunds under their own terms '
            '(<a href="https://www.patreon.com/policy/legal" target="_blank" rel="noopener">Patreon</a>, '
            '<a href="https://docs.github.com/en/site-policy/github-terms/github-sponsors-additional-terms" target="_blank" rel="noopener">GitHub Sponsors</a>)'
        )
        ends = "Patreon or GitHub reports"
    else:
        support = (
            "<h2>What Patreon support is for</h2>\n"
            "<p>A membership on Patreon supports the development of AirBound. "
            "As a thank-you, members of the <b>Relay</b> tier may use the hosted relay"
        )
        payments = (
            'Patreon handles all payments, cancelling and refunds under its own terms '
            '(<a href="https://www.patreon.com/policy/legal" target="_blank" rel="noopener">Patreon</a>)'
        )
        ends = "Patreon reports"
    return f"""<div class="card"><h1>Terms</h1>
<p class="muted">Last updated {TERMS_UPDATED}. In short: AirBound is a hobby project. Patreon support pays for its development, the relay is a thank-you that comes with no guarantee, and you can always move to your own server or router.</p>
<h2>A hobby project</h2>
<p>AirBound is free, open-source software (MIT license), made by one person in their spare time. It is not a company, and it is not affiliated with Home Assistant or Nabu Casa.</p>
{support} on {escape(config.domain)}. You're supporting a project, not buying a service: there is no service level, no promised uptime and no support agreement.</p>
<h2>No guarantee</h2>
<ul>
<li>The relay and the add-on are provided <b>as is</b>, without warranty of any kind.</li>
<li>The relay may be slow, down, changed or shut down at any time, also without notice, for example for maintenance or if the server fails.</li>
<li>Don't rely on it for anything where an outage could cause harm or loss, such as alarms, safety equipment, medical devices or business use. Keep another way to reach your home.</li>
</ul>
<h2>Liability</h2>
<p>As far as the law allows, AirBound and the person running it are not liable for any damage or loss from using, or not being able to use, the relay or the add-on. That includes outages, lost data and anything that happens in your Home Assistant. Nothing here limits rights you have by law that can't be waived.</p>
<h2>Your part</h2>
<ul>
<li>You're responsible for your Home Assistant and who can log in to it. AirBound requires two-step login, but keep Home Assistant updated and use strong passwords.</li>
<li>Use the relay for your own Home Assistant only, not for anything illegal, for sharing large amounts of data or for attacking others. Access can be removed for abuse.</li>
<li>Each membership covers up to {config.max_devices} Home Assistant installations. Fair use: each runs at up to {escape(config.bandwidth)}/s, and past {config.device_monthly_gb} GB in a calendar month at {escape(config.throttled_bandwidth)}/s for the rest of that month. If the whole server nears its own monthly limit, everyone may be slowed down until the month ends.</li>
</ul>
<h2>Payments, cancelling and refunds</h2>
<p>{payments}; AirBound never sees your card. You can cancel any time where you signed up. Access ends when {ends} that the support has ended, after a grace period of {config.grace_days} days. Because the membership supports development rather than buying uptime, outages don't give a refund.</p>
<h2>If the relay closes</h2>
<p>If the hosted relay is ever shut down for good, the plan is to say so on Patreon and GitHub at least 30 days ahead when possible. The add-on keeps working with your own server or router.</p>
<h2>Changes and contact</h2>
<p>These terms can change; the date above shows the latest version, and bigger changes are posted on Patreon. Questions go to <a href="https://github.com/oleost/AirBound/issues">GitHub issues</a>. To report abuse of the relay, such as attacks or illegal content coming from its address, write to <a href="mailto:abuse@{escape(config.domain)}">abuse@{escape(config.domain)}</a>. See also the <a href="/privacy">privacy page</a>.</p></div>"""


def confirm_code(claim_id: str, code: str, github: bool = False) -> str:
    github_button = f'<a class="button" href="/link/{escape(claim_id)}/github">Continue with GitHub Sponsors</a>' if github else ""
    return f"""<div class="card"><h1>Link your Home Assistant</h1>
<p>Check that AirBound in your Home Assistant shows this code:</p>
<div class="code">{escape(code)}</div>
<p class="muted">If you didn't start this yourself, or the code is different, close this page. Someone may be trying to use your membership.</p>
<p>The code matches? Log in where you support AirBound:</p>
<a class="button" href="/link/{escape(claim_id)}/login">Continue with Patreon</a>
{github_button}
<p class="muted">By continuing you accept the <a href="/terms">terms</a>: AirBound is a hobby project, and the relay comes with no guarantee.</p></div>"""


def needs_membership(config: Config, claim_id: str) -> str:
    return f"""<div class="card"><h1>One more step: support AirBound on Patreon</h1>
<p>You're logged in, but your Patreon account doesn't have the <b>Relay</b> membership yet. It supports AirBound's development, and access to the relay comes with it as a thank-you. It starts with a 7-day free trial, and you can cancel any time.</p>
<p class="muted">The relay is a best-effort hobby service with no guarantee of uptime. See the <a href="/terms">terms</a>.</p>
<a class="button" href="{escape(config.join_url)}" target="_blank" rel="noopener">Start the free trial on Patreon</a>
<a class="button secondary" href="/link/{escape(claim_id)}/login">I've joined: check again</a>
<p class="muted">Joining on Patreon's iPhone app doesn't offer the trial. Use the browser.</p></div>"""


def needs_sponsorship(config: Config, claim_id: str) -> str:
    dollars = config.github_min_cents / 100
    amount = f"${dollars:g}"
    return f"""<div class="card"><h1>One more step: sponsor AirBound on GitHub</h1>
<p>You're logged in, but your GitHub account doesn't sponsor <b>{escape(config.github_sponsorable)}</b> yet. A monthly sponsorship of {escape(amount)} or more supports AirBound's development, and access to the relay comes with it as a thank-you. You can cancel any time.</p>
<a class="button" href="{escape(config.github_sponsor_url)}" target="_blank" rel="noopener">Sponsor on GitHub</a>
<a class="button secondary" href="/link/{escape(claim_id)}/github">I've sponsored: check again</a>
<p class="muted">Choose a monthly tier; a one-time sponsorship doesn't give access. The relay is a best-effort hobby service with no guarantee of uptime. See the <a href="/terms">terms</a>.</p></div>"""


def linked(fqdn: str) -> str:
    return f"""<div class="card"><h1>Done! Your Home Assistant is linked</h1>
<p>Its new address is <b>{escape(fqdn)}</b>.</p>
<p>Go back to AirBound in Home Assistant. It continues on its own; you can close this page.</p></div>"""


def expired() -> str:
    return """<div class="card"><h1>This link has expired</h1>
<p>Links work for 15 minutes. Go back to AirBound in Home Assistant and start again.</p></div>"""


def error(message: str) -> str:
    return f"""<div class="card"><h1>Something went wrong</h1><p>{escape(message)}</p>
<p>Go back to AirBound in Home Assistant and try again.</p></div>"""
