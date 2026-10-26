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
ul{padding-left:20px}li{margin-bottom:6px}code{font-family:ui-monospace,Consolas,monospace}
"""


def page(title: str, body: str, config: Config) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)} · AirBound</title><style>{STYLE}</style></head>
<body><main><header>{LOGO}<b>AirBound</b></header>{body}
<footer><a href="/">Home</a> · <a href="/privacy">Privacy</a> · <a href="https://github.com/oleost/HA-AirBound">Source code</a> · <a href="{escape(config.manage_url)}">Patreon</a></footer>
</main></body></html>"""


def home(config: Config) -> str:
    return f"""<div class="card"><h1>Secure remote access for Home Assistant</h1>
<p>AirBound gives your Home Assistant its own secure web address that works in the Companion app and any browser, without a VPN and without opening ports on your router.</p>
<p>This relay is for AirBound members. Install the AirBound add-on in Home Assistant, choose <b>AirBound hosted relay</b>, and it walks you through the rest. New members get a 7-day free trial.</p>
<a class="button" href="https://github.com/oleost/HA-AirBound">Get the add-on</a>
<a class="button secondary" href="{escape(config.join_url)}">Become a member</a></div>
<div class="card"><h2>What the relay can and can't see</h2><ul>
<li>Traffic is encrypted all the way to your Home Assistant. The relay passes it on by the address name and can't read it.</li>
<li>The certificate for your address is made by your own Home Assistant. The relay never has its key.</li>
<li>The relay knows which addresses connect and when, and how much data passes. It never sees passwords, pages or camera images.</li>
</ul></div>"""


def privacy(config: Config) -> str:
    return f"""<div class="card"><h1>Privacy</h1>
<p>The hosted relay keeps as little as possible:</p><ul>
<li><b>Your Patreon user id</b> and whether your membership includes the relay. Not your name or email address.</li>
<li><b>For each linked Home Assistant:</b> its random address on {escape(config.domain)}, a hash of its key, and when it was last connected.</li>
<li><b>Connection logs</b> (address and time) are kept for at most 14 days to stop abuse.</li>
</ul>
<p>Traffic to your Home Assistant is end-to-end encrypted. The relay can't read it, and nothing about it is stored.</p>
<p>Unlinking a Home Assistant or ending your membership removes access; ask on GitHub to have your data deleted sooner.</p></div>"""


def confirm_code(claim_id: str, code: str) -> str:
    return f"""<div class="card"><h1>Link your Home Assistant</h1>
<p>Check that AirBound in your Home Assistant shows this code:</p>
<div class="code">{escape(code)}</div>
<p class="muted">If you didn't start this yourself, or the code is different, close this page. Someone may be trying to use your membership.</p>
<a class="button" href="/link/{escape(claim_id)}/login">The code matches: continue with Patreon</a></div>"""


def needs_membership(config: Config, claim_id: str) -> str:
    return f"""<div class="card"><h1>One more step: become a member</h1>
<p>You're logged in, but your Patreon account doesn't have the <b>Relay</b> membership yet. It starts with a 7-day free trial, and you can cancel any time.</p>
<a class="button" href="{escape(config.join_url)}" target="_blank" rel="noopener">Start the free trial on Patreon</a>
<a class="button secondary" href="/link/{escape(claim_id)}/login">I've joined: check again</a>
<p class="muted">Joining on Patreon's iPhone app doesn't offer the trial. Use the browser.</p></div>"""


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
