"""Is AirBound's AppArmor profile (airbound/apparmor.txt) in force on a Home Assistant OS VM, and does it hold?

    python scripts/confinement.py            the test VM (HA_VMID in .env)
    python scripts/confinement.py <vmid>

Three checks, as root on the VM through Proxmox's guest agent:

1. Every program of the add-on runs under its own profile, enforcing.
2. Nothing the add-on needed was refused since it started (or since a given time): a refusal
   means the profile is too tight, and some feature quietly broke.
3. What a taken-over helper program could read. Caddy's own file server stands in for the
   attacker: it serves the whole container, with the add-on's real folders, under each helper's
   profile. Only that helper's own files may come out. The same server under Docker's general
   profile is the control: there everything must come out, or the check proves nothing.

The test round (scripts/e2e.py) and the beta check (scripts/beta.py) run this at the end.
"""

from __future__ import annotations

import base64
import json
import shlex
import sys
import time

from testenv import env, ssh

# What each helper may read of the files that matter; everything else in PATHS must be refused.
PATHS = (
    "/proc/1/environ",  # in the real add-on: the first program's environment, with the key to Home Assistant
    "/data/settings.json",
    "/data/machine_key",
    "/data/frpc.toml",
    "/data/Caddyfile",
    "/data/caddy/instance.uuid",
    "/data/crowdsec/bouncer.key",
    "/data/logs/access.log",
    "/homeassistant/secrets.yaml",
    "/homeassistant/.storage/auth",
    "/homeassistant/.storage/auth_module.totp",
    "/app/airbound/web.py",
)
MAY_READ = {
    "caddy": {"/data/Caddyfile", "/data/caddy/instance.uuid", "/data/logs/access.log"},
    "frpc": {"/data/frpc.toml"},
    "crowdsec": {"/data/crowdsec/bouncer.key", "/data/logs/access.log"},
}
HELPERS = {"caddy": "caddy", "frpc": "frpc", "crowdsec": "crowdsec", "cscli": "crowdsec"}
PROBE_PORT = 18080

LOOK = r"""
N=$(docker ps --format '{{.Names}}' | grep '_airbound$' | head -1)
[ -n "$N" ] || { echo "ERROR the AirBound container isn't running"; exit 0; }
echo "CONTAINER $N $(docker inspect "$N" --format '{{.AppArmorProfile}}')"
for p in $(docker top "$N" -o pid | tail -n +2); do echo "PROCESS $(cat /proc/$p/comm) $(cat /proc/$p/attr/current)"; done
# The probe below is refused on purpose; what it left in the log is not the add-on's.
S=SINCE; P=$(cat /run/airbound_probe_end 2>/dev/null || echo 0); [ "$P" -gt "$S" ] && S=$P
journalctl --since "@$S" --no-pager -o cat | grep 'apparmor="DENIED"' | grep '_airbound' \
  | sed -E 's/.*apparmor=/apparmor=/; s/ (pid|fsuid|ouid)=[^ ]*//g; s#/proc/[0-9]+#/proc/PID#g' | sort | uniq -c | sort -rn | head -40 | sed 's/^/REFUSED /'
"""

PROBE = r"""
N=$(docker ps --format '{{.Names}}' | grep '_airbound$' | head -1)
IMG=$(docker inspect "$N" --format '{{.Config.Image}}')
MAIN=$(docker inspect "$N" --format '{{.AppArmorProfile}}')
DATA=$(docker inspect "$N" --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}')
CONFIG=$(docker inspect "$N" --format '{{range .Mounts}}{{if eq .Destination "/homeassistant"}}{{.Source}}{{end}}{{end}}')
for PROFILE in docker-default HELPERS; do
  [ "$PROFILE" = docker-default ] && FULL=docker-default || FULL="$MAIN//$PROFILE"
  docker rm -f airbound_probe >/dev/null 2>&1
  docker run -d --name airbound_probe --network host --security-opt "apparmor=$FULL" -e SUPERVISOR_TOKEN=stand-in \
    -v "$DATA:/data:ro" -v "$CONFIG:/homeassistant:ro" \
    --entrypoint /usr/local/bin/caddy "$IMG" file-server --root / --listen 127.0.0.1:PORT >/dev/null 2>&1 || echo "ERROR the probe didn't start under $FULL"
  for i in 1 2 3 4 5 6 7 8 9 10; do
    [ "$(curl -s -o /dev/null -m 3 -w '%{http_code}' http://127.0.0.1:PORT/etc/hostname)" = 200 ] && break; sleep 1
  done
  for path in PATHS; do
    echo "READ $PROFILE $(curl -s -o /dev/null -m 5 -w '%{http_code}' "http://127.0.0.1:PORT$path") $path"
  done
done
docker rm -f airbound_probe >/dev/null 2>&1
sleep 2; date +%s > /run/airbound_probe_end
"""


def guest(vmid: str, script: str, timeout: int = 240) -> str:
    """Run a shell script as root on the VM; its output."""
    host, key = f"root@{env('PROXMOX_HOST')}", env("PROXMOX_KEY")

    def call(command: str) -> dict:
        out = ssh(host, key, f"qm guest exec {vmid} --timeout {timeout} -- sh -c {shlex.quote(command)}", check=False, timeout=timeout + 60)
        try:
            return json.loads(out)
        except ValueError:
            raise RuntimeError(f"the guest agent on VM {vmid} gave no answer: {out[-200:]!r}") from None

    blob = base64.b64encode(script.encode()).decode()
    call("rm -f /tmp/airbound_check.b64")
    for start in range(0, len(blob), 2500):  # the guest agent takes only short commands
        if call(f"printf %s {blob[start:start + 2500]} >> /tmp/airbound_check.b64").get("exitcode") != 0:
            raise RuntimeError("couldn't send the check to the VM")
    result = call("base64 -d /tmp/airbound_check.b64 > /tmp/airbound_check.sh; rm -f /tmp/airbound_check.b64; sh /tmp/airbound_check.sh 2>&1; rm -f /tmp/airbound_check.sh")
    return result.get("out-data", "")


def check(vmid: str, since: float | None = None) -> str:
    """Raises RuntimeError with what is wrong; otherwise a one-line summary."""
    since = since if since is not None else time.time() - 24 * 3600
    seen = guest(vmid, LOOK.replace("SINCE", str(int(since)))).splitlines()
    problems = [line for line in seen if line.startswith("ERROR")]
    container = next((line.split() for line in seen if line.startswith("CONTAINER")), None)
    if problems or not container:
        raise RuntimeError(f"couldn't look at the add-on: {problems or seen}")
    main = container[2]
    if not main.endswith("_airbound"):
        raise RuntimeError(f"the add-on runs under {main}, not its own profile")
    processes = [line.split(None, 2)[1:] for line in seen if line.startswith("PROCESS")]
    wrong = []
    for name, profile in processes:
        want = f"{main}//{HELPERS[name]} (enforce)" if name in HELPERS else f"{main} (enforce)"
        if profile != want:
            wrong.append(f"{name} runs under {profile}, expected {want}")
    if wrong or not {"python", "caddy", "crowdsec"} <= {name for name, _ in processes}:
        raise RuntimeError(f"profiles not as expected: {wrong or processes}")
    refused = [line.removeprefix("REFUSED ").strip() for line in seen if line.startswith("REFUSED")]
    if refused:
        raise RuntimeError(f"the profile refused something the add-on wanted ({len(refused)} kinds): " + " ;; ".join(refused[:8]))

    script = PROBE.replace("HELPERS", " ".join(MAY_READ)).replace("PORT", str(PROBE_PORT)).replace("PATHS", " ".join(PATHS))
    out = guest(vmid, script).splitlines()
    if errors := [line for line in out if line.startswith("ERROR")]:
        raise RuntimeError("; ".join(errors))
    read = {(profile, path): code for _, profile, code, path in (line.split() for line in out if line.startswith("READ"))}
    there = [path for path in PATHS if read.get(("docker-default", path)) == "200"]
    if len(there) < 8:
        raise RuntimeError(f"the control could only read {there}: the check itself is broken")
    leaks, blocked = [], 0
    for helper, allowed in MAY_READ.items():
        for path in there:
            code = read.get((helper, path))
            if path in allowed:
                if code != "200":
                    leaks.append(f"{helper} can't read its own {path} ({code})")
            elif code != "403":
                leaks.append(f"{helper} can read {path} ({code})")
            else:
                blocked += 1
    if leaks:
        raise RuntimeError("; ".join(leaks))
    return (
        f"{len(processes)} programs each under their own enforcing profile, nothing they needed was refused; "
        f"a taken-over helper was refused {blocked} of {blocked} reads of other programs' files "
        f"(all {len(there)} readable under Docker's general profile)"
    )


if __name__ == "__main__":
    print(check(sys.argv[1] if len(sys.argv) > 1 else env("HA_VMID")))
