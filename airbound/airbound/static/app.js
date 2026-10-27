"use strict";

// The AirBound panel. Runs inside Home Assistant's ingress, so every URL is relative.

const app = document.getElementById("app");
// { settings, user_id, steps: [{ id, title, done, locked, reason, summary }], complete, landing }
let state = null;
let current = null; // the page being shown: "welcome", "overview" or a step id
let timer = null; // the current page's polling timer
let view = {}; // per-page UI state that survives re-renders

// ---------- helpers ----------

const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

async function api(path, body) {
  const options = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const resp = await fetch(`api/${path}`, options);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || "Something went wrong. Try again in a moment.");
  return data;
}

// Polls fn every ms until stopPolling(); fn returns false to stop by itself.
function poll(fn, ms) {
  stopPolling();
  const token = {};
  timer = token;
  const tick = async () => {
    let again = true;
    try {
      again = (await fn()) !== false;
    } catch {
      // Home Assistant may be restarting; keep trying quietly.
    }
    if (timer === token && again) token.id = setTimeout(tick, ms);
    else if (timer === token) timer = null;
  };
  tick();
}

function stopPolling() {
  if (timer) clearTimeout(timer.id);
  timer = null;
}

// Loads what a step needs, once, when it is opened.
async function load(promise, onData) {
  try {
    onData(await promise);
  } catch (e) {
    view.error = e.message;
  }
  render();
}

const enter = {
  cloudflare: () => {
    load(api("hostname/suggest"), (d) => (view.suggested = d.hostname));
    if (state.settings.has_token) load(api("cloudflare/zones"), (d) => (view.zones = d.zones));
  },
  ha_settings: () =>
    poll(async () => {
      view.status = await api("ha-settings");
      view.restarting = false;
      render();
    }, 3000),
  // "Check again" also picks up two-step login turned on elsewhere, which may finish this step.
  users: () => load(api("users").then(async (d) => (await refreshState(), d)), (d) => Object.assign(view, d)),
  relay: () => load(api("relay"), (d) => (view.relay = d)),
  hosted: () => followHosted(),
  go_live: () =>
    load(api("go-live"), (d) => {
      view.progress = d;
      if (d.busy) followGoLive();
    }),
  router: () => load(api("router"), (d) => (view.info = d)),
  devices: () => load(api("router"), (d) => (view.info = d)),
  overview: () =>
    poll(async () => {
      view.status = await api("status");
      view.bans = (await api("bans")).bans;
      render();
    }, 15000),
};

async function setPort(value) {
  view.error = "";
  try {
    state.settings = (await api("router/port", { port: Number(value) })).settings;
    view.portDraft = undefined;
    view.test = null; // the old result was for the old port
  } catch (e) {
    view.error = e.message;
  }
  render();
}

function followGoLive() {
  poll(async () => {
    view.progress = await api("go-live");
    // Going live unlocks the next steps; reload their status before showing the result.
    if (!view.progress.busy) await refreshState();
    render();
    return view.progress.busy;
  }, 1500);
}

// While a link request waits for the Patreon login, check every few seconds.
function followHosted() {
  poll(async () => {
    view.hosted = await api("hosted");
    if (view.hosted.state === "linked") await refreshState();
    render();
    return ["pending", "needs_membership"].includes(view.hosted.state);
  }, 3000);
}

// Reloads step status after anything that may have finished or unlocked a step.
async function refreshState() {
  state = await api("state");
}

async function go(page) {
  stopPolling();
  view = {};
  current = page;
  history.replaceState(null, "", `#${page}`); // a reload stays on this page
  render();
  window.scrollTo(0, 0);
  api("step", { step: page }).catch(() => {});
  await refreshState().catch(() => {});
  render();
  enter[page]?.();
}

const stepById = (id) => state.steps.find((s) => s.id === id);

// The next step that can be opened. When everything later is locked, the first
// step that still needs work (it's what unlocks the rest), or the overview once live.
function nextPage() {
  const index = state.steps.findIndex((s) => s.id === current);
  const later = state.steps.slice(index + 1).find((s) => !s.locked);
  if (later) return later;
  if (state.settings.live) return { id: "overview", title: "Overview" };
  return state.steps.find((s) => !s.locked && !s.done && s.id !== current) || null;
}

function previousPage() {
  const index = state.steps.findIndex((s) => s.id === current);
  return [...state.steps.slice(0, Math.max(index, 0))].reverse().find((s) => !s.locked) || null;
}

// Refresh status first: finishing this step may have unlocked the next one.
async function goNext() {
  await refreshState().catch(() => {});
  const next = nextPage();
  go(next ? next.id : current);
}

// "Continue: Your router" says where the button goes (NN/g: name the next step).
function continueButton({ disabled = false, action = "next" } = {}) {
  const next = nextPage();
  if (!next) return "";
  return `<button class="primary" data-action="${action}" ${disabled ? "disabled" : ""}>Continue: ${esc(next.title)}</button>`;
}

// Not 8443: UniFi and many other routers serve their own settings page there.
const SUGGESTED_PORT = 9443;

const COMMON_HOSTNAMES = ["ha", "hass", "home", "homeassistant", "home-assistant", "smarthome", "smart", "hjem", "huset", "house", "iot", "hub", "remote", "vpn", "www"];

const LOCK = '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><path fill="currentColor" d="M4 7V5a4 4 0 1 1 8 0v2h1v8H3V7h1Zm2 0h4V5a2 2 0 1 0-4 0v2Z"/></svg>';

// The step list: names and status, completed steps show what's set up, steps that
// depend on something unfinished are greyed out with the reason (PatternFly wizard).
function stepNav() {
  const items = [];
  if (state.settings.live) {
    items.push(`<li><button class="nav-item ${current === "overview" ? "current" : ""}" data-action="goto" data-step="overview" ${current === "overview" ? 'aria-current="step"' : ""}>
      <span class="nav-icon overview" aria-hidden="true">◎</span><span class="nav-text"><span class="nav-title">Overview</span>
      <span class="nav-sub">${state.complete ? "Everything is set up" : "Status and settings"}</span></span></button></li>`);
  }
  state.steps.forEach((s, i) => {
    const isCurrent = s.id === current;
    const icon = s.locked ? LOCK : s.done ? "✓" : String(i + 1);
    const cls = ["nav-item", isCurrent && "current", s.done && "done", s.locked && "locked"].filter(Boolean).join(" ");
    const sub = s.locked ? s.reason : s.done ? s.summary : isCurrent ? "You are here" : "Not done yet";
    items.push(`<li><button class="${cls}" ${s.locked ? "disabled" : `data-action="goto" data-step="${s.id}"`} ${isCurrent ? 'aria-current="step"' : ""}>
      <span class="nav-icon" aria-hidden="true">${icon}</span><span class="nav-text"><span class="nav-title">${esc(s.title)}</span>
      <span class="nav-sub">${esc(sub)}</span></span>${s.locked ? `<span class="sr-only">Locked. ${esc(s.reason)}</span>` : ""}</button></li>`);
  });
  return `<ol class="nav-list">${items.join("")}</ol>`;
}

function frame(stepId, body) {
  const index = state.steps.findIndex((s) => s.id === stepId);
  const step = state.steps[index];
  const label = step ? `Step ${index + 1} of ${state.steps.length} · ${step.title}` : "Overview";
  const prev = previousPage();
  const back = prev ? `<button class="link back" data-action="goto" data-step="${prev.id}">← ${esc(prev.title)}</button>` : "";
  return `
    <div class="shell">
      <nav class="side-nav" aria-label="Setup steps">${stepNav()}</nav>
      <details class="mobile-nav"><summary>${esc(label)}</summary>${stepNav()}</details>
      <section class="content">
        <div class="step-head"><span class="step-label">${esc(label)}</span>${back}</div>
        ${body}
      </section>
    </div>`;
}

function errorBox(message) {
  return message ? `<div class="notice bad" role="alert"><p>${esc(message)}</p></div>` : "";
}

function minutesLeft(iso) {
  if (!iso) return "";
  const seconds = Math.max(0, Math.round((new Date(iso) - Date.now()) / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

// ---------- steps ----------

const steps = {};

steps.welcome = () =>
  solo(
    `<h1>Reach your Home Assistant from anywhere</h1>
    <p class="lead">AirBound gives Home Assistant its own secure web address. It works in the Home Assistant app and in any browser, at home, at work or on a borrowed computer, with no VPN app.</p>
    <div class="card">
      <h2>You'll need one of these</h2>
      <ul class="plain">
        <li><b>The easiest:</b> support AirBound on Patreon and use the hosted relay as a thank-you. Nothing to set up, and it starts with a 7-day free trial.</li>
        <li><b>Or do it yourself, for free:</b> your own domain on Cloudflare, plus either a small rented server or access to your router's settings.</li>
      </ul>
      <p class="muted small">Setting up takes about 5 minutes with the hosted relay, 10–15 on your own.</p>
      <h2>How it keeps your home safe</h2>
      <ul class="plain">
        <li>Everyone who logs in from outside needs a password <em>and</em> a code from an authenticator app.</li>
        <li>The address is random, and nothing else on your network is opened.</li>
        <li>Your traffic is encrypted all the way to your home. Nobody in between can read it.</li>
        <li>Addresses that keep guessing passwords are blocked.</li>
      </ul>
    </div>
    <div class="actions"><button class="primary" data-action="goto" data-step="${state.steps[0].id}">Get started: ${esc(state.steps[0].title)}</button></div>`
  );

steps.cloudflare = () => {
  const s = state.settings;
  const zones = view.zones;
  const tokenForm = `
    <div class="card">
      <h2>Create a token in Cloudflare</h2>
      <p>The token lets AirBound create the address and a certificate for one domain. It can't change anything else.</p>
      <ol class="steps">
        <li><a href="https://dash.cloudflare.com/profile/api-tokens" target="_blank" rel="noopener">Open API tokens in Cloudflare</a> (opens in a new tab).</li>
        <li>Select <b>Create Token</b>, then <b>Use template</b> next to <b>Edit zone DNS</b>.</li>
        <li>Under <b>Permissions</b>, select <b>+ Add more</b> and choose <b>Zone</b> → <b>Zone</b> → <b>Read</b>.</li>
        <li>Under <b>Zone Resources</b>, pick your domain.</li>
        <li>Leave <b>Token expiration</b> and <b>Client IP address filtering</b> as they are. AirBound needs the token for as long as it runs, and your home address can change.</li>
        <li>Select <b>Continue to summary</b>, then <b>Create Token</b>, and copy the token.</li>
      </ol>
      <label for="token">Paste the token here</label>
      <input id="token" type="password" autocomplete="off" spellcheck="false" placeholder="Cloudflare API token">
      ${errorBox(view.error)}
      <div class="actions">
        <button class="primary" data-action="check-token" ${view.busy ? "disabled" : ""}>${view.busy ? '<span class="spinner"></span> Checking…' : "Check token"}</button>
      </div>
    </div>`;
  let zonePicker = "";
  if (zones) {
    view.zoneId ??= (zones.find((z) => z.name === s.domain) || zones[0]).id;
    view.hostMode ??= s.hostname && s.tls === "cloudflare" && !view.suggestedOnly ? "keep" : "random";
    const zone = zones.find((z) => z.id === view.zoneId) || zones[0];
    const own = (view.ownName ?? "").trim().toLowerCase();
    const common = COMMON_HOSTNAMES.includes(own);
    const option = (value, label, detail) => `
      <label class="row" style="font-weight:normal;margin:0">
        <span class="grow"><input type="radio" name="hostmode" value="${value}" data-action="pick-hostmode" ${view.hostMode === value ? "checked" : ""}> ${label}
        ${detail ? `<div class="muted small" style="margin-left:24px">${detail}</div>` : ""}</span>
      </label>`;
    zonePicker = `<div class="card">
        <div class="notice good"><p>Cloudflare accepted the token.</p></div>
        <h2>Domain</h2>
        <div class="list">
          ${zones
            .map(
              (z) => `<label class="row" style="font-weight:normal;margin:0">
                <span class="grow"><input type="radio" name="zone" value="${esc(z.id)}" data-action="pick-zone" ${z.id === zone.id ? "checked" : ""}> ${esc(z.name)}</span>
              </label>`
            )
            .join("")}
        </div>
        <h2>Address</h2>
        <div class="list">
          ${s.hostname && s.tls === "cloudflare" && s.domain === zone.name ? option("keep", "Keep the current address", `<span class="mono">${esc(s.hostname)}.${esc(zone.name)}</span>`) : ""}
          ${option("random", "Random name <b>(recommended)</b>", `<span class="mono">${esc(view.suggested || "…")}.${esc(zone.name)}</span><br>A random name is very hard for strangers to find.`)}
          ${option("own", "Choose my own name", "")}
        </div>
        ${
          view.hostMode === "own"
            ? `<label for="hostname">Name</label>
               <div style="display:flex;gap:8px;align-items:center">
                 <input id="hostname" type="text" autocomplete="off" spellcheck="false" placeholder="for example kitchen-42" value="${esc(view.ownName ?? "")}" data-action="type-hostname">
                 <span class="mono nowrap">.${esc(zone.name)}</span>
               </div>
               ${common ? `<div class="notice warn"><p>"${esc(own)}" is one of the first names people try when they look for a Home Assistant. It works, but a less obvious name keeps you out of sight.</p></div>` : ""}`
            : ""
        }
        ${errorBox(view.error)}
        <div class="actions">
          <button class="primary" data-action="use-zone">Use this address</button>
          <button class="link" data-action="change-token">Use a different token</button>
        </div>
      </div>`;
  }
  return frame(
    "cloudflare",
    `<h1>Connect your domain</h1>
    <p class="lead">AirBound uses your domain on Cloudflare for the address and the certificate. Your traffic doesn't go through Cloudflare.</p>
    ${zones ? zonePicker : s.has_token && !view.changeToken && !view.error ? '<p><span class="spinner"></span> Checking your saved token…</p>' : tokenForm}`
  );
};

steps.connection = () => {
  const mode = state.settings.mode;
  const option = (value, title, pill, points, button) => `
    <div class="card choice ${mode === value ? "chosen" : ""}">
      <div class="choice-head"><h2>${title}</h2><span class="pill ${value === "hosted" ? "good" : "muted"}">${pill}</span></div>
      <ul class="plain">${points.map((p) => `<li>${p}</li>`).join("")}</ul>
      <div class="actions"><button class="${mode === value || (!mode && value === "hosted") ? "primary" : ""}" data-action="choose-mode" data-mode="${value}">${button}</button>
      ${mode === value ? '<span class="muted small">Your current choice</span>' : ""}</div>
    </div>`;
  return frame(
    "connection",
    `<h1>How should your phone reach Home Assistant?</h1>
    <p class="lead">Pick one. You can change it later.</p>
    ${errorBox(view.error)}
    ${option(
      "hosted",
      "Through AirBound's hosted relay",
      "Easiest",
      [
        'Nothing to set up: no domain, no server and no router changes. You get an address like <span class="mono">x7k2….airbound.dev</span>.',
        "Comes with supporting AirBound on Patreon. It starts with a 7-day free trial, and you can cancel any time.",
        'AirBound is a hobby project: the relay is best effort, with no guarantee of uptime. <a href="https://airbound.dev/terms" target="_blank" rel="noopener">Terms</a>',
        "The relay can't read anything. The traffic stays encrypted all the way to your home, and the certificate is made by your own Home Assistant.",
      ],
      "Use the hosted relay"
    )}
    ${option(
      "relay",
      "Through your own small server",
      "No router changes",
      [
        "A small rented server (a VPS) passes the traffic on to your home. Your router needs no open ports, and your home's internet address stays hidden.",
        "Costs about €4–5 a month from providers such as Hetzner, DigitalOcean or OVH. Setting it up is one command that AirBound gives you.",
        "The server can't read anything. The traffic stays encrypted all the way to your home.",
      ],
      "Use my own server"
    )}
    ${option(
      "direct",
      "Directly through your router",
      "Free",
      [
        "Your phone connects straight to your home.",
        "You forward one port on your router, and your home's internet address is part of the setup.",
      ],
      "Use my router"
    )}`
  );
};

steps.relay = () => {
  const r = view.relay;
  let check = "";
  if (view.checking) check = '<p><span class="spinner"></span> Checking…</p>';
  else if (view.check && !view.check.public)
    check = `<div class="notice warn">
      <p><b><span class="mono">${esc(view.check.ip)}</span> is an address on a private network.</b> Your phone can't reach it from outside, so AirBound can't point your domain at it.</p>
      <p>Enter the server's <b>public</b> IP address instead. The install command prints it at the end ("enter this address").</p>
    </div>`;
  else if (view.check?.reachable)
    check = `<div class="notice good"><p>AirBound can reach the relay at <span class="mono">${esc(view.check.host)}</span>.</p></div>`;
  else if (view.check)
    check = `<div class="notice warn">
      <p><b>AirBound can't reach the relay yet.</b> Check that the command finished with "AirBound relay is running".</p>
      <p class="small">If your provider has a firewall in its web console, allow TCP ports ${r?.control_port ?? 7000} and ${r?.public_port ?? 443} there too. On Oracle Cloud that's an ingress rule in the network's <b>Security List</b> (source 0.0.0.0/0).</p>
    </div>`;
  return frame(
    "relay",
    `<h1>Set up your relay server</h1>
    <p class="lead">This takes about 5 minutes, and you only do it once.</p>
    <div class="card">
      <h2>1. Get a server</h2>
      <p>Rent the smallest Linux server (Ubuntu or Debian) from any provider. One processor and 512 MB of memory is plenty. Free tiers such as Oracle Cloud's Always Free work too.</p>
      <p class="muted small">In the provider's own firewall (on Oracle Cloud: the <b>Security List</b>), allow incoming TCP on ports 443 and 7000.</p>
    </div>
    <div class="card">
      <h2>2. Run this command on the server</h2>
      <p>Log in to the server with SSH (your provider shows how) and paste this command:</p>
      ${r ? `<textarea class="command" readonly rows="3" aria-label="Install command" data-action="select-all">${esc(r.command)}</textarea>` : '<p><span class="spinner"></span></p>'}
      <div class="actions" style="margin-top:8px">
        <button data-action="copy" data-text="${esc(r?.command ?? "")}">${view.copied ? "Copied" : "Copy command"}</button>
        <button class="link" data-action="relay-new-key">Make a new key</button>
      </div>
      <p class="small"><b>Set up a relay before?</b> Run the command again on the same server. It updates the relay to this key.</p>
      <p class="muted small">It installs the relay and secures the server: a firewall that only lets in SSH, ports ${r?.public_port ?? 443} and ${r?.control_port ?? 7000} and anything already running there, SSH logins with keys only (if you log in with a key), and automatic security updates. Then it prints the server's address. The command contains a secret key, so don't share it.</p>
    </div>
    <div class="card">
      <h2>3. Enter the server's address</h2>
      <label for="relay-host">IP address</label>
      <input id="relay-host" type="text" autocomplete="off" spellcheck="false" placeholder="for example 203.0.113.25" value="${esc(view.hostDraft ?? r?.host ?? "")}" data-action="type-relay-host">
      ${errorBox(view.error)}
      ${check}
      <div class="actions">
        <button class="${view.check?.reachable ? "" : "primary"}" data-action="check-relay" ${view.checking ? "disabled" : ""}>Check connection</button>
        ${view.check?.reachable && view.check.public ? continueButton() : view.check?.public ? '<button data-action="next">Continue anyway</button>' : ""}
      </div>
    </div>`
  );
};

steps.hosted = () => {
  const h = view.hosted;
  let body;
  if (!h) body = '<p><span class="spinner"></span> Checking…</p>';
  else if (h.state === "pending" || h.state === "needs_membership")
    body = `
      <div class="card">
        <h2>1. Open the link page</h2>
        <p>It opened in a new tab. If it didn't, use this button:</p>
        <div class="actions"><a class="button primary" href="${esc(h.link_url)}" target="_blank" rel="noopener">Open the link page</a></div>
        <h2>2. Check the code</h2>
        <p>The page shows a code. It must be the same as this one:</p>
        <div class="code-box">${esc(h.code)}</div>
        <h2>3. Log in with Patreon</h2>
        ${
          h.state === "needs_membership"
            ? `<div class="notice warn"><p><b>You're logged in, but the membership is missing.</b> Start the 7-day free trial on Patreon, then select <b>I've joined: check again</b> on the link page.</p>
               <div class="actions"><a class="button" href="${esc(h.join_url)}" target="_blank" rel="noopener">Start the free trial on Patreon</a></div></div>`
            : "<p>Log in on the link page. AirBound continues here by itself.</p>"
        }
        <p class="muted small"><span class="spinner"></span> Waiting for the login…</p>
        <div class="actions"><button class="link" data-action="hosted-start">Start over with a new link</button></div>
      </div>`;
  else if (h.state === "linked")
    body = `
      <div class="card">
        <div class="notice good"><p>Linked. This Home Assistant's address is:</p></div>
        <div class="address">https://${esc(h.fqdn)}</div>
        <div class="actions">${continueButton()}<button class="link" data-action="hosted-start">Link again</button></div>
      </div>`;
  else
    body = `
      <div class="card">
        ${h.state === "expired" ? '<div class="notice warn"><p>The link expired before the login finished. Start again.</p></div>' : ""}
        <p>You log in with Patreon on AirBound's website. New members start with a 7-day free trial on Patreon, and you can cancel any time.</p>
        <div class="notice info"><p><b>A hobby project, not a company.</b> Patreon support pays for AirBound's development, and the relay comes with it as a thank-you. It's run on a best-effort basis, with no guarantee of uptime and no liability if it's down, so don't rely on it for alarms or anything safety-related. You can switch to your own server or router at any time. <a href="https://airbound.dev/terms" target="_blank" rel="noopener">Read the terms</a></p></div>
        <p class="muted small">AirBound only learns your Patreon member id and whether the membership includes the relay, never your name, email address or card.</p>
        ${errorBox(view.error)}
        <div class="actions"><button class="primary" data-action="hosted-start" ${view.busy ? "disabled" : ""}>${view.busy ? '<span class="spinner"></span> Starting…' : "Log in with Patreon"}</button></div>
      </div>`;
  return frame(
    "hosted",
    `<h1>Get your AirBound address</h1>
    <p class="lead">Link this Home Assistant to your AirBound membership. You get a private address on airbound.dev.</p>
    ${body}`
  );
};

steps.ha_settings = () => {
  const st = view.status;
  let body;
  if (!st) {
    body = view.restarting
      ? '<div class="notice info"><p><span class="spinner"></span> Home Assistant is restarting with the new settings. This takes about a minute.</p></div>'
      : '<p><span class="spinner"></span> Checking Home Assistant…</p>';
  } else if (st.pending && st.proxy_trusted) {
    body = `
      <div class="notice good"><p><b>Checked:</b> Home Assistant now sees who is connecting through AirBound.</p></div>
      <div class="notice info">
        <p><b>One more click:</b> Home Assistant shows a box called <b>Confirm new HTTP server configuration</b>. Select <b>Confirm</b> there to keep the settings.</p>
        <p class="small">If nobody confirms, Home Assistant puts the old settings back in ${esc(minutesLeft(st.revert_at))}.</p>
      </div>
      <div class="actions"><button class="link" data-action="confirm-here">I don't see that box, confirm here</button></div>`;
  } else if (st.pending) {
    body = `
      <div class="notice bad">
        <p><b>Something isn't right, so don't confirm the change.</b> Home Assistant still can't see who is connecting.</p>
        <p class="small">It will undo the change by itself in ${esc(minutesLeft(st.revert_at))}. ${esc(st.pending_error || "")}</p>
      </div>`;
  } else if (!st.changes.length && st.proxy_trusted) {
    body = `
      <div class="notice good"><p>Home Assistant is set up the way AirBound needs.</p></div>
      <div class="actions">${continueButton()}</div>`;
  } else {
    body = `
      <div class="card">
        <h2>What changes</h2>
        <div class="list">
          ${st.changes
            .map((c) => `<div class="row"><span class="grow">${esc(c.label)}</span><span class="change"><span class="from">${esc(c.current)}</span>→<b>${esc(c.new)}</b></span></div>`)
            .join("")}
        </div>
        <p class="muted small">Home Assistant restarts to try the new settings. AirBound checks that they work before you confirm them. If something goes wrong, Home Assistant undoes the change by itself after 5 minutes.</p>
        ${errorBox(view.error)}
        <div class="actions"><button class="primary" data-action="apply-ha" ${view.busy ? "disabled" : ""}>Change settings and restart</button></div>
      </div>`;
  }
  return frame(
    "ha_settings",
    `<h1>Let Home Assistant see who is connecting</h1>
    <p class="lead">AirBound sits in front of Home Assistant. With these settings Home Assistant sees each visitor's real address, so it can block an address after 10 failed logins.</p>
    ${body}`
  );
};

steps.users = () => {
  const people = view.users;
  const rows = (people || [])
    .map((p) => {
      const status = p.mfa
        ? '<span class="pill good">Two-step login on</span>'
        : p.local_only
          ? '<span class="pill muted">Home network only</span>'
          : '<span class="pill warn">Needs two-step login</span>';
      let action = "";
      if (!p.mfa && !p.local_only && p.you) {
        action = `<button class="primary" data-action="mfa-me">Turn it on for me</button>
                  <button class="link" data-action="local-only" data-id="${esc(p.id)}" data-value="true">Home network only</button>`;
      } else if (!p.mfa && !p.local_only) {
        action = `<button data-action="local-only" data-id="${esc(p.id)}" data-value="true">Home network only</button>`;
      } else if (p.local_only) {
        action = `<button class="link" data-action="local-only" data-id="${esc(p.id)}" data-value="false">Allow from outside</button>`;
      }
      const hint =
        !p.mfa && !p.local_only && !p.you
          ? `<div class="muted small">Or ask ${esc(p.name)} to turn on two-step login at home: their profile → Security → Multi-factor authentication.</div>`
          : "";
      return `<div class="row"><div class="grow"><b>${esc(p.name)}</b>${p.you ? ' <span class="muted">(you)</span>' : ""} ${status}${hint}</div><div class="actions" style="margin:0">${action}</div></div>`;
    })
    .join("");
  const blocking = view.blocking || [];
  return frame(
    "users",
    `<h1>Two-step login for everyone who logs in from outside</h1>
    <p class="lead">From outside, logging in takes a password and a code from an authenticator app on your phone. People who only use Home Assistant at home can be limited to the home network instead.</p>
    <div class="card">
      ${people ? `<div class="list">${rows}</div>` : '<p><span class="spinner"></span> Loading users…</p>'}
      ${errorBox(view.error)}
      ${
        people && blocking.length
          ? `<div class="notice warn"><p>To continue, turn on two-step login for ${esc(blocking.join(", "))}, or limit them to the home network. AirBound never lets anyone log in from outside without it.</p></div>`
          : ""
      }
      <div class="actions">
        ${continueButton({ disabled: !people || blocking.length })}
        <button class="link" data-action="refresh-users">Check again</button>
      </div>
    </div>`
  );
};

steps.go_live = () => {
  const s = state.settings;
  const progress = view.progress;
  const tasks = (progress?.tasks || [])
    .map((t) => {
      const icon = t.state === "done" ? "✓" : t.state === "error" ? "!" : t.state === "running" ? '<span class="spinner"></span>' : "·";
      return `<div class="task ${t.state}"><span class="icon">${icon}</span><div><div>${esc(t.label)}</div>${t.detail ? `<div class="small ${t.state === "error" ? "" : "muted"}">${esc(t.detail)}</div>` : ""}</div></div>`;
    })
    .join("");
  const failed = progress?.tasks?.some((t) => t.state === "error");
  const done = progress && !progress.busy && !failed && progress.live;
  let actions;
  if (done) actions = `${continueButton()}<button class="link" data-action="go-live">Run again</button>`;
  else if (failed) actions = '<button class="primary" data-action="go-live">Try again</button>';
  else if (progress?.busy) actions = "";
  else actions = '<button class="primary" data-action="go-live">Go live</button>';
  return frame(
    "go_live",
    `<h1>Go live</h1>
    <p class="lead">AirBound now creates your address, starts the secure proxy and gets a certificate from Let's Encrypt. This usually takes under a minute.</p>
    <div class="card">
      <div class="muted small">Your Home Assistant address</div>
      <div class="address">${esc(s.url)}</div>
      ${tasks ? `<div style="margin-top:16px">${tasks}</div>` : ""}
      ${
        done
          ? s.mode === "hosted"
            ? '<div class="notice good"><p>AirBound is live. Your phone reaches Home Assistant through AirBound&#39;s hosted relay, and your router needs no changes.</p></div>'
            : s.mode === "relay"
            ? '<div class="notice good"><p>AirBound is live. Your phone reaches Home Assistant through your relay server, and your router needs no changes.</p></div>'
            : '<div class="notice good"><p>AirBound is running. One step left: your router has to let the traffic in.</p></div>'
          : ""
      }
      <div class="actions">${actions}</div>
    </div>`
  );
};

steps.router = () => {
  const info = view.info;
  const s = state.settings;
  const port = s.external_port;
  const where = info?.ha_ip || "your Home Assistant's address";
  let result = "";
  if (view.testing) result = '<p><span class="spinner"></span> Testing…</p>';
  else if (view.test?.answer === "airbound")
    result = '<div class="notice good"><p>Your Home Assistant can be reached from the internet.</p></div>';
  else if (view.test?.answer === "other")
    result = `<div class="notice bad">
      <p><b>Something else answers on port ${port}${view.test.owner ? `: ${esc(view.test.owner)}` : ""}.</b> It isn't Home Assistant.</p>
      <p>If that's another server, either point its port ${port} rule to <span class="mono">${esc(where)}</span>, or give AirBound another port below.</p>
      <p class="small">If it's your router, it may use port ${port} for its own settings page. Seen from inside your home, the router then answers itself, even when the forward works from outside. Pick a port the router doesn't use, or check from your phone on mobile data.</p>
    </div>`;
  else if (view.test)
    result = `<div class="notice warn">
      <p><b>Couldn't confirm it from here.</b> Many routers can't test this from inside the home, so this doesn't have to mean anything is wrong.</p>
      <p>To check: turn off Wi-Fi on your phone and open <span class="mono">${esc(s.url)}</span>. If you see the Home Assistant login page, it works.</p>
    </div>`;
  const showPort = view.portOpen || port !== 443 || view.test?.answer === "other";
  const portChooser = showPort
    ? `<div class="card">
        <h2>Use another port</h2>
        <p>If port 443 is already used for something else, the router can use another outside port for AirBound. The address then ends with that port, for example <span class="mono">${esc(s.url.replace(/:\d+$/, ""))}:${SUGGESTED_PORT}</span>.</p>
        <p class="muted small">Some work and guest networks only allow port 443, so the address may not open there. Avoid 8443 and 8080: many routers use them for their own settings page.</p>
        <label for="port">Outside port for AirBound</label>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <input id="port" type="text" inputmode="numeric" style="max-width:140px" value="${esc(view.portDraft ?? (port === 443 ? SUGGESTED_PORT : port))}" data-action="type-port">
          <button data-action="save-port">Use this port</button>
          ${port !== 443 ? '<button class="link" data-action="reset-port">Go back to 443</button>' : ""}
        </div>
      </div>`
    : `<div class="actions" style="margin-top:0"><button class="link" data-action="open-port">Port 443 is already used on my network</button></div>`;
  let auto = "";
  if (view.autoBusy) auto = '<p><span class="spinner"></span> Asking your router…</p>';
  else if (view.auto?.opened)
    auto = `<div class="notice good"><p>Your router opened port ${port} for Home Assistant. AirBound keeps it open by itself. Select <b>Test connection</b> below to check.</p></div>`;
  else if (view.auto)
    auto = `<div class="notice warn"><p><b>Your router doesn't allow automatic port opening.</b> That's the safer setting (UPnP off), so we don't recommend turning it on. Add the rule by hand below, or go back and use a relay server instead.</p></div>`;
  const autoCard = `<div class="card">
      <h2>Let AirBound ask the router</h2>
      <p>Some routers can open the port themselves (UPnP or NAT-PMP). It only works if that's turned on in the router.</p>
      ${auto}
      ${view.auto?.opened ? "" : `<div class="actions"><button data-action="auto-port" ${view.autoBusy ? "disabled" : ""}>Open the port automatically</button></div>`}
    </div>`;
  return frame(
    "router",
    `<h1>Let the traffic in through your router</h1>
    ${autoCard}
    <p class="lead">Or add it by hand: your router has to pass secure web traffic on to Home Assistant. This is called <b>port forwarding</b>. Log in to your router and add one <b>TCP</b> rule:</p>
    <div class="card">
      ${
        info
          ? `<table class="values">
              <tr><td>Protocol</td><td><b>TCP</b> only (no UDP)</td></tr>
              <tr><td>External (outside) port</td><td><b>${port}</b></td></tr>
              <tr><td>Internal (local) IP address</td><td><b class="mono">${esc(where)}</b></td></tr>
              <tr><td>Internal port</td><td><b>443</b></td></tr>
            </table>
            <p class="muted small">Routers call this <i>Port forwarding</i>, <i>Virtual server</i>, <i>NAT</i> or <i>Applications</i>. Forward only this one port.</p>`
          : '<p><span class="spinner"></span> Looking up your addresses…</p>'
      }
      ${errorBox(view.error)}
      ${result}
      <div class="actions">
        <button class="primary" data-action="test-router" ${view.testing ? "disabled" : ""}>${view.test ? "Test again" : "Test connection"}</button>
        ${view.test?.answer === "airbound" || view.auto?.opened ? continueButton({ action: "router-done" }) : view.test?.answer === "unknown" ? '<button data-action="router-done">It works on my phone, continue</button>' : ""}
      </div>
    </div>
    ${portChooser}`
  );
};

steps.devices = () => {
  const s = state.settings;
  const url = s.url;
  const internal = view.info?.ha_ip ? `http://${view.info.ha_ip}${view.info.ha_port === 80 ? "" : ":" + view.info.ha_port}` : "";
  return frame(
    "devices",
    `<h1>Connect your phone</h1>
    <p class="lead">Use this address in the Home Assistant app and in any browser.</p>
    <div class="card">
      <div class="address">${esc(url)}</div>
      <div class="actions" style="margin-top:8px"><button data-action="copy" data-text="${esc(url)}">${view.copied ? "Copied" : "Copy address"}</button></div>
      <div style="margin-top:16px" class="qr" role="img" aria-label="QR code for ${esc(url)}"><img src="api/qr.svg" alt="" width="180" height="180"></div>
      <p class="muted small">Scan with your phone's camera to open the address.</p>
    </div>
    <div class="card">
      <h2>In the Home Assistant app</h2>
      <ol class="steps">
        <li>Open <b>Settings</b> → <b>Companion app</b> → your home → <b>Connection</b>.</li>
        <li>Set <b>External URL</b> (Home Assistant URL) to:<br><span class="mono">${esc(url)}</span></li>
        ${internal ? `<li>Set <b>Internal URL</b> to:<br><span class="mono">${esc(internal)}</span><br>and add your home Wi-Fi under <b>Home network</b>. The app then connects directly while you're at home.</li>` : ""}
      </ol>
      <p class="muted small">On a new phone, choose <b>Enter address manually</b> when the app asks for your server, and use the address above.</p>
    </div>
    <div class="actions"><button class="primary" data-action="devices-done">Done: go to the overview</button></div>`
  );
};

// After setup: one place that shows how everything is set up, with a way to change each part.
steps.overview = () => {
  const s = state.settings;
  const st = view.status;
  const setup = state.steps
    .map(
      (step) => `<div class="row"><div class="grow"><b>${esc(step.title)}</b>
        <div class="muted small">${esc(step.done ? step.summary : step.locked ? step.reason : "Not done yet")}</div></div>
        ${step.locked ? "" : `<button class="link" data-action="goto" data-step="${step.id}">${step.done ? "Change" : "Set up"}</button>`}</div>`
    )
    .join("");
  const allOk = st && st.checks.every((c) => c.ok);
  const checks = (st?.checks || [])
    .map(
      (c) => `<div class="check ${c.ok ? "ok" : "bad"}"><span class="dot" aria-hidden="true"></span>
        <div class="grow"><b>${esc(c.title)}</b> <span class="sr-only">${c.ok ? "OK" : "Needs attention"}</span><div class="muted">${esc(c.detail)}</div>
        ${c.fix_step ? `<div class="actions" style="margin-top:8px"><button data-action="goto" data-step="${esc(c.fix_step)}">${esc(c.fix_label)}</button></div>` : ""}</div></div>`
    )
    .join("");
  return frame(
    "overview",
    `<h1>${s.live ? (st ? (allOk ? "Remote access is on" : "Remote access needs attention") : "Remote access") : "Remote access is off"}</h1>
    <div class="card">
      <div class="muted small">Your Home Assistant address</div>
      <div class="address">${esc(s.url)}</div>
      <div class="actions" style="margin-top:8px">
        <button data-action="copy" data-text="${esc(s.url)}">${view.copied ? "Copied" : "Copy address"}</button>
        <button class="link" data-action="goto" data-step="devices">Connect a phone</button>
      </div>
    </div>
    <div class="card">
      <h2 style="margin-top:0">Health</h2>
      ${st ? checks : '<p><span class="spinner"></span> Checking…</p>'}
    </div>
    <div class="card">
      <h2 style="margin-top:0">Your setup</h2>
      <div class="list">${setup}</div>
    </div>
    ${
      view.bans?.length
        ? `<div class="card">
            <h2 style="margin-top:0">Blocked addresses</h2>
            <p class="muted small">CrowdSec blocked these after repeated failed logins or attacks. Blocks end by themselves; unblock one only if you know it's yours.</p>
            <div class="list">
              ${view.bans
                .map(
                  (b) => `<div class="row"><div class="grow"><span class="mono">${esc(b.ip)}</span>
                    <div class="muted small">${esc(b.reason.replaceAll("-", " "))}${b.expires_in ? ` · ends in ${esc(b.expires_in)}` : ""}</div></div>
                    <button class="link" data-action="unban" data-ip="${esc(b.ip)}">Unblock</button></div>`
                )
                .join("")}
            </div>
          </div>`
        : ""
    }
    <div class="card">
      <div class="toggle-row">
        <div>
          <b>Require two-step login from outside</b>
          <div class="muted small">Strongly recommended. Turning this off lets people log in from outside with only a password.</div>
        </div>
        <label class="switch" style="margin:0"><input type="checkbox" data-action="toggle-mfa" ${s.require_mfa ? "checked" : ""} aria-label="Require two-step login from outside"><span></span></label>
      </div>
      ${view.confirmMfaOff ? `<div class="notice bad"><p>Are you sure? Anyone who has or guesses a password could then log in from anywhere.</p><div class="actions"><button data-action="mfa-off-yes">Turn it off</button><button class="primary" data-action="mfa-off-no">Keep it on</button></div></div>` : ""}
      ${st ? `<p class="muted small" style="margin-top:12px">Logins refused because two-step login was missing (since AirBound started): <b>${st.refused_logins}</b></p>` : ""}
    </div>`
  );
};

// The welcome page stands alone: no step list before the user has started.
function solo(body) {
  return `<div class="shell solo"><section class="content">${body}</section></div>`;
}

// ---------- actions ----------

const actions = {
  next: () => goNext(),
  goto: (el) => go(el.dataset.step),
  async "router-done"() {
    state = await api("step-done", { step: "router" });
    goNext();
  },
  async "devices-done"() {
    state = await api("step-done", { step: "devices" });
    go("overview");
  },

  async "check-token"() {
    const token = document.getElementById("token").value.trim();
    view.busy = true;
    view.error = "";
    render();
    try {
      const d = await api("cloudflare", { token });
      view.zones = d.zones;
      view.changeToken = false;
      state.settings.has_token = true;
    } catch (e) {
      view.error = e.message;
    }
    view.busy = false;
    render();
  },
  "change-token"() {
    view.zones = null;
    view.changeToken = true;
    view.error = "";
    render();
  },
  "pick-zone"(el) {
    view.zoneId = el.value;
    if (view.hostMode === "keep") view.hostMode = "random";
    render();
  },
  "pick-hostmode"(el) {
    view.hostMode = el.value;
    view.error = "";
    render();
  },
  "type-hostname"(el) {
    view.ownName = el.value;
    render();
  },
  async "use-zone"() {
    const zone = view.zones.find((z) => z.id === view.zoneId);
    const body = { id: zone.id, name: zone.name };
    if (view.hostMode === "random") body.hostname = view.suggested;
    if (view.hostMode === "own") {
      body.hostname = (view.ownName || "").trim().toLowerCase();
      if (!body.hostname) {
        view.error = "Type a name first.";
        return render();
      }
    }
    try {
      const d = await api("domain", body);
      state.settings = d.settings;
      goNext();
    } catch (e) {
      view.error = e.message;
      render();
    }
  },
  async "choose-mode"(el) {
    state = await api("connection", { mode: el.dataset.mode });
    goNext();
  },
  async "hosted-start"() {
    // Open the tab right away: browsers block tabs opened after waiting for the network.
    const tab = window.open("", "_blank");
    view.busy = true;
    view.error = "";
    render();
    try {
      view.hosted = await api("hosted/start", {});
      if (tab) tab.location.href = view.hosted.link_url;
      followHosted();
    } catch (e) {
      tab?.close();
      view.error = e.message;
    }
    view.busy = false;
    render();
  },
  "select-all"(el) {
    el.select();
  },
  async "relay-new-key"() {
    view.relay = await api("relay/new-key", {});
    view.check = null;
    render();
  },
  "type-relay-host"(el) {
    view.hostDraft = el.value;
  },
  async "check-relay"() {
    view.checking = true;
    view.error = "";
    render();
    try {
      view.check = await api("relay", { host: document.getElementById("relay-host").value });
    } catch (e) {
      view.error = e.message;
      view.check = null;
    }
    view.checking = false;
    render();
  },

  async "apply-ha"() {
    view.busy = true;
    render();
    try {
      await api("ha-settings/apply", {});
      view.status = null;
      view.restarting = true;
    } catch (e) {
      view.error = e.message;
    }
    view.busy = false;
    render();
  },
  async "confirm-here"() {
    await api("ha-settings/confirm", {});
  },

  "mfa-me"() {
    // The panel runs in an iframe inside Home Assistant; open the profile page in the main window.
    window.top.location.href = "/profile/security";
  },
  async "local-only"(el) {
    const d = await api(`users/${el.dataset.id}/local-only`, { local_only: el.dataset.value === "true" });
    Object.assign(view, d);
    // The last person sorted out finishes this step and unlocks the next; the Continue button needs to know.
    await refreshState();
    render();
  },
  "refresh-users"() {
    view.users = null;
    render();
    enter.users();
  },

  async "go-live"() {
    view.progress = await api("go-live", {});
    render();
    followGoLive();
  },

  async "auto-port"() {
    view.autoBusy = true;
    render();
    try {
      view.auto = await api("router/auto", {});
    } catch (e) {
      view.error = e.message;
    }
    view.autoBusy = false;
    render();
  },
  "open-port"() {
    view.portOpen = true;
    render();
  },
  "type-port"(el) {
    view.portDraft = el.value;
  },
  async "save-port"() {
    await setPort(document.getElementById("port").value);
  },
  async "reset-port"() {
    view.portOpen = false;
    await setPort(443);
  },

  async "test-router"() {
    view.testing = true;
    render();
    try {
      view.test = await api("router/test", {});
    } catch (e) {
      view.error = e.message;
    }
    view.testing = false;
    render();
  },

  async copy(el) {
    await navigator.clipboard?.writeText(el.dataset.text);
    view.copied = true;
    render();
    setTimeout(() => { view.copied = false; render(); }, 2000);
  },

  async "toggle-mfa"(el) {
    if (!el.checked) {
      el.checked = true;
      view.confirmMfaOff = true;
      return render();
    }
    state.settings = (await api("settings", { require_mfa: true })).settings;
    render();
  },
  async "mfa-off-yes"() {
    state.settings = (await api("settings", { require_mfa: false })).settings;
    view.confirmMfaOff = false;
    render();
  },
  async unban(el) {
    view.bans = (await api("bans/unban", { ip: el.dataset.ip })).bans;
    render();
  },
  "mfa-off-no"() {
    view.confirmMfaOff = false;
    render();
  },
};

app.addEventListener("click", async (event) => {
  const el = event.target.closest("[data-action]");
  if (!el || ["checkbox", "radio", "text"].includes(el.type)) return;
  if (el.tagName === "A") return;
  event.preventDefault();
  try {
    await actions[el.dataset.action]?.(el);
  } catch (e) {
    view.error = e.message;
    render();
  }
});

app.addEventListener("change", async (event) => {
  const el = event.target.closest("input[type=checkbox][data-action], input[type=radio][data-action]");
  if (el) await actions[el.dataset.action]?.(el);
});

app.addEventListener("input", (event) => {
  const el = event.target.closest("input[type=text][data-action]");
  if (el) actions[el.dataset.action]?.(el);
});

app.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && event.target.id === "token") actions["check-token"]();
});

// ---------- render ----------

function render() {
  const step = current;
  // Keep typing undisturbed: restore focus, value and caret of the active field.
  const active = document.activeElement;
  const focused = active?.id && active.tagName === "INPUT" ? active : null;
  const saved = focused && { id: focused.id, value: focused.value, start: focused.selectionStart, end: focused.selectionEnd };
  app.innerHTML = (steps[step] || steps.welcome)();
  const el = saved && document.getElementById(saved.id);
  if (el) {
    el.value = saved.value;
    el.focus();
    if (saved.start !== null) el.setSelectionRange(saved.start, saved.end);
  }
}

(async function start() {
  try {
    state = await api("state");
    // Open where attention is needed: the overview once set up, else the first
    // unfinished step, not wherever the user happened to be last time.
    const pages = ["welcome", "overview", ...state.steps.filter((s) => !s.locked).map((s) => s.id)];
    const linked = location.hash.slice(1);
    current = pages.includes(linked) ? linked : state.landing;
    render();
    enter[current]?.();
  } catch (e) {
    app.innerHTML = errorBox("AirBound couldn't load. Reload the page in a moment.");
  }
})();
