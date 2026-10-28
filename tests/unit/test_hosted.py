from airbound import hosted
from airbound.caddy import render_caddyfile
from airbound.certwatch import unexpected_certificates
from airbound.relay import render_frpc
from airbound.settings import Settings, wizard_steps

HOSTED = Settings(
    mode="hosted",
    tls="hosted",
    domain="airbound.dev",
    hostname="x7k2abcdefgh",
    hosted_device="d0123456789abcdef",
    hosted_key="secret-key",
)


def test_hosted_frpc_publishes_only_its_own_hostname_over_pinned_tls():
    config = render_frpc(HOSTED)
    assert f'serverAddr = "{hosted.RELAY_HOST}"' in config
    assert 'user = "d0123456789abcdef"' in config
    assert 'metadatas.key = "secret-key"' in config
    # The machine key comes from frpc's environment, so the file (which a backup may hold) never has it.
    assert 'metadatas.machine = "{{ .Envs.AIRBOUND_MACHINE }}"' in config
    assert 'type = "https"' in config
    assert 'customDomains = ["x7k2abcdefgh.airbound.dev"]' in config
    assert f'transport.tls.trustedCaFile = "{hosted.CA_FILE.as_posix()}"' in config
    assert 'transport.tls.serverName = "airbound-hosted-relay"' in config
    assert 'transport.proxyProtocolVersion = "v2"' in config
    assert "remotePort" not in config


def test_pinned_hosted_ca_ships_with_the_add_on():
    assert hosted.CA_FILE.read_text().startswith("-----BEGIN CERTIFICATE-----")


def test_hosted_caddy_gets_an_exact_certificate_through_the_relay():
    config = render_caddyfile(HOSTED, "homeassistant:80")
    assert "acme_dns" not in config and "CF_API_TOKEN" not in config
    assert "disable_http_challenge" in config
    assert "x7k2abcdefgh.airbound.dev:443 {" in config
    assert "x7k2abcdefgh.airbound.dev:8444 {" in config
    assert "*.airbound.dev" not in config  # never a wildcard on the relay's domain
    assert "proxy_protocol" in config and "@forged_local remote_ip private_ranges" in config


def test_hosted_settings_hide_the_key():
    public = HOSTED.public()
    assert "hosted_key" not in public and "hosted_poll" not in public
    assert public["has_hosted_key"] is True
    assert HOSTED.is_configured


def test_wizard_steps_for_hosted_mode():
    assert wizard_steps("hosted")[:3] == ["welcome", "connection", "hosted"]
    assert "cloudflare" not in wizard_steps("hosted") and "router" not in wizard_steps("hosted")


def cert(names, pubkey, cert_id="c1"):
    return {"id": cert_id, "names": names, "issuer": "Let's Encrypt", "not_before": "", "pubkey": pubkey}


def test_hosted_ct_watch_flags_any_certificate_for_the_name_with_a_foreign_key():
    fqdn = "x7k2abcdefgh.airbound.dev"
    own = {"aaa"}
    assert unexpected_certificates([cert([fqdn], "aaa")], fqdn, own) == []  # ours
    assert len(unexpected_certificates([cert([fqdn], "bbb")], fqdn, own)) == 1  # someone else's
    assert len(unexpected_certificates([cert(["*.airbound.dev", "airbound.dev"], "ccc")], fqdn, own)) == 1  # a wildcard covering it
    assert unexpected_certificates([cert(["other.airbound.dev"], "ddd")], fqdn, own) == []  # another member's
    assert unexpected_certificates([cert([fqdn], "")], fqdn, own) == []  # no key to judge by (crt.sh)


def test_hosted_ct_watch_ignores_certificates_from_before_the_link():
    fqdn = "x7k2abcdefgh.airbound.dev"
    old_wildcard = {**cert(["*.airbound.dev", "airbound.dev"], "cf"), "not_before": "2026-10-26T18:18:11Z"}
    new_wildcard = {**old_wildcard, "id": "c2", "not_before": "2026-10-27T09:00:00Z"}
    found = unexpected_certificates([old_wildcard, new_wildcard], fqdn, {"aaa"}, since="2026-10-26T20:00:00Z")
    assert [c["id"] for c in found] == ["c2"]


def test_ct_results_belong_to_one_address():
    from airbound.certwatch import CertWatch

    watch = CertWatch()
    watch.checked, watch.unexpected, watch.fqdn = True, [{"id": "x"}], "old.example.com"
    assert watch.results_for("new.airbound.dev") == (False, [])
    assert watch.results_for("old.example.com") == (True, [{"id": "x"}])


def test_own_domain_ct_watch_still_ignores_wildcards():
    fqdn = "h.example.com"
    assert unexpected_certificates([cert(["*.example.com"], "x")], fqdn) == []
    assert len(unexpected_certificates([cert([fqdn], "x")], fqdn)) == 1
