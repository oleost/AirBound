"""airbound/apparmor.txt against the code: every program has its profile, and no helper gets another's files.

Whether the profile works on a real Home Assistant is checked by scripts/confinement.py in the test round.
"""

import re
from pathlib import Path

from airbound import caddy, crowdsec, machine, relay, reporting, settings, users
from airbound.ha_bans import BANS_FILE

ADDON = Path(__file__).resolve().parents[2] / "airbound"
RAW = (ADDON / "apparmor.txt").read_bytes()
TEXT = RAW.decode("utf-8")
HELPERS = ("caddy", "frpc", "crowdsec")


def rules_of(name: str) -> str:
    """The rules of one helper's profile, without comments."""
    body = TEXT.split(f"  profile {name} flags=")[1].split("\n  }")[0]
    return re.sub(r"#(?!include).*", "", body)


def main_rules() -> str:
    """The rules of the add-on's own profile, without the helpers' and without comments."""
    return re.sub(r"#(?!include).*", "", TEXT.split("  profile caddy flags=")[0])


def test_the_supervisor_can_load_it():
    # The Supervisor renames the one profile whose line starts with "profile " to the add-on's slug.
    names = [line for line in TEXT.splitlines() if line.startswith("profile ")]
    assert names == ["profile airbound flags=(attach_disconnected,mediate_deleted) {"]
    assert b"\r" not in RAW
    assert TEXT.count("{") == TEXT.count("}")
    assert "complain" not in TEXT and "unconfined" not in TEXT


def test_every_program_the_code_starts_has_a_profile():
    code = "".join(path.read_text(encoding="utf-8") for path in (ADDON / "airbound").glob("*.py"))
    started = set(re.findall(r'create_subprocess_exec\(\s*"([a-z]+)"', code))
    assert started == {"caddy", "frpc", "crowdsec", "cscli"}
    for program in started:
        assert re.search(rf"^  /usr/local/bin/{program} cx -> (caddy|frpc|crowdsec),$", TEXT, re.M), program
    # Nothing else may be started from the add-on, and no rule lets a program leave its profile.
    assert len(re.findall(r" cx -> ", TEXT)) == len(started)
    assert not re.search(r" [mrwlk]*[pPuU]x\b", TEXT)
    inherit = sorted(line.split()[0] for line in TEXT.splitlines() if re.search(r" [mr]*ix,$", line))
    assert inherit == ["/sbin/docker-init", "/usr/local/bin/crowdsec", "/usr/local/bin/cscli", "/usr/local/bin/python3*"]


def test_each_helper_gets_its_own_files():
    assert f"{caddy.CADDYFILE.as_posix()} r," in rules_of("caddy")
    assert "storage file_system /data/caddy\n" in caddy.render_caddyfile(settings.Settings(mode="direct", tls="internal", domain="example.com", hostname="home"), "ha:80")
    assert "/data/caddy/** rwk," in rules_of("caddy")
    assert caddy.ACCESS_LOG.as_posix().startswith("/data/logs/access") and "/data/logs/access* rw," in rules_of("caddy")
    assert "capability net_bind_service," in rules_of("caddy")
    assert f"{relay.FRPC_CONFIG.as_posix()} r," in rules_of("frpc")
    assert f"{relay.PKI_DIR.as_posix()}/** r," in rules_of("frpc")
    assert "/app/airbound/hosted_relay_ca.pem r," in rules_of("frpc") and (ADDON / "airbound" / "hosted_relay_ca.pem").exists()
    assert f"{crowdsec.BASE.as_posix()}/** rwlk," in rules_of("crowdsec")
    assert crowdsec.AUTH_LOG.parent.as_posix() == "/data/logs" and "/data/logs/** r," in rules_of("crowdsec")
    assert f"{crowdsec.DEFAULT_DATA.as_posix()}/** r," in main_rules()
    assert crowdsec.DEFAULT_ETC.as_posix().startswith("/etc/") and "/etc/** r," in main_rules()


def test_the_add_on_reads_only_three_files_from_home_assistants_folder():
    assert users.HA_CONFIG.as_posix() == "/homeassistant"
    allowed = set(re.findall(r"^\s*(/homeassistant\S*) ", TEXT, re.M))
    assert allowed == {f"/homeassistant/.storage/{name}" for name in users.MFA_STORES} | {BANS_FILE.as_posix()}
    assert all(f"{path} r," in main_rules() for path in allowed)


def test_no_helper_can_read_what_is_not_its_own():
    not_theirs = (settings.SETTINGS_FILE, machine.MACHINE_FILE, reporting.STATE_FILE)
    for helper in HELPERS:
        rules = rules_of(helper)
        for path in not_theirs:
            assert path.as_posix() not in rules, f"{helper}: {path}"
        assert "/homeassistant" not in rules and "/run/" not in rules and "/dev/**" not in rules, helper
        assert "/data/** " not in rules and "/data/ " not in rules, helper
        assert not re.search(r"^\s*(file|network|capability|ptrace|signal),", rules, re.M), helper
        assert "network raw" not in rules and "network packet" not in rules, helper
        # The first program's environment holds AirBound's key to Home Assistant.
        assert "deny @{PROC}/@{pid}/environ r," in rules and "deny ptrace," in rules, helper
        assert "signal (receive)," in rules and "signal (send" not in rules, helper
    assert "capability" not in rules_of("frpc") + rules_of("crowdsec")
    assert rules_of("caddy").count("capability") == 1
    assert "/app/" not in rules_of("caddy") + rules_of("crowdsec")
    assert rules_of("frpc").count("/app/") == 1
    assert relay.FRPC_CONFIG.as_posix() not in rules_of("caddy") + rules_of("crowdsec")
    assert "/data/caddy" not in rules_of("frpc") + rules_of("crowdsec")
    assert "/data/crowdsec" not in rules_of("caddy") + rules_of("frpc")


def test_the_add_on_itself_is_limited_too():
    rules = main_rules()
    assert not re.search(r"^\s*(file|capability|network|ptrace|mount),", rules, re.M)
    assert "capability" not in rules and "ptrace" not in rules
    assert "network raw" not in rules and "network packet" not in rules
    assert "deny /app/** w," in rules  # the code can't be changed from inside
    assert "/dev/** " not in rules and "/homeassistant/** " not in rules and "/run/" not in rules
