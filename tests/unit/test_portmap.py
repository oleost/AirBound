import struct

import pytest

from airbound.portmap import PortMapError, find_control_url, natpmp_parse, natpmp_request, soap_body, ssdp_location

ROOT_DESC = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device><deviceList><device><deviceList><device>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType>
        <controlURL>/ctl/IPConn</controlURL>
      </service>
    </serviceList>
  </device></deviceList></device></deviceList></device>
</root>"""


def test_natpmp_request_layout():
    assert natpmp_request(9443, 443, 3600) == struct.pack("!BBHHHI", 0, 2, 0, 443, 9443, 3600)


def test_natpmp_success_and_refusal():
    ok = struct.pack("!BBHIHHI", 0, 130, 0, 1234, 443, 9443, 3600)
    assert natpmp_parse(ok) == 9443
    refused = struct.pack("!BBHIHHI", 0, 130, 2, 1234, 443, 0, 0)
    with pytest.raises(PortMapError, match="refused"):
        natpmp_parse(refused)
    with pytest.raises(PortMapError):
        natpmp_parse(b"\x00")


def test_ssdp_location():
    answer = "HTTP/1.1 200 OK\r\nST: urn:x\r\nLOCATION: http://10.0.0.1:5000/rootDesc.xml\r\n\r\n"
    assert ssdp_location(answer) == "http://10.0.0.1:5000/rootDesc.xml"
    assert ssdp_location("HTTP/1.1 200 OK\r\n\r\n") is None


def test_find_control_url_in_nested_devices():
    assert find_control_url(ROOT_DESC, "http://10.0.0.1:5000") == (
        "http://10.0.0.1:5000/ctl/IPConn",
        "urn:schemas-upnp-org:service:WANIPConnection:1",
    )


def test_soap_body():
    body = soap_body("urn:svc", "AddPortMapping", {"NewExternalPort": 9443, "NewProtocol": "TCP"})
    assert '<u:AddPortMapping xmlns:u="urn:svc"><NewExternalPort>9443</NewExternalPort><NewProtocol>TCP</NewProtocol>' in body
