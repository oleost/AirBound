from airbound.certwatch import from_certspotter, from_crtsh, unexpected_certificates

FQDN = "k7f3q9.example.com"


def test_wildcard_and_other_names_are_expected():
    certs = from_certspotter(
        [
            {"id": 1, "dns_names": ["*.example.com", "example.com"], "issuer": {"friendly_name": "Let's Encrypt"}},
            {"id": 2, "dns_names": ["www.example.com"], "issuer": {"friendly_name": "Sectigo"}},
        ]
    )
    assert unexpected_certificates(certs, FQDN) == []


def test_certificate_for_exact_hostname_is_flagged():
    certs = from_certspotter([{"id": 3, "dns_names": ["K7F3Q9.example.com"], "issuer": {"friendly_name": "Evil CA"}}])
    found = unexpected_certificates(certs, FQDN)
    assert [c["issuer"] for c in found] == ["Evil CA"]


def test_crtsh_entries_with_newline_separated_names():
    certs = from_crtsh([{"id": 9, "name_value": f"*.example.com\n{FQDN}", "issuer_name": "CN=R11"}])
    assert [c["id"] for c in unexpected_certificates(certs, FQDN)] == ["crtsh:9"]


def test_duplicate_log_entries_are_reported_once():
    certs = from_crtsh([{"id": 4, "name_value": FQDN}, {"id": 4, "name_value": FQDN}])
    assert len(unexpected_certificates(certs, FQDN)) == 1
