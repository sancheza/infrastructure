#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_wifi_ap_survey.py

Unit tests for wifi_ap_survey.py.
Validates CLI parsing, fingerprinters, inventory loaders, and collectors across
all supported router brands (ASUS, Netgear, Verizon, UniFi, OpenWrt, MikroTik, TP-Link).

Usage:
    pytest test_wifi_ap_survey.py
    python3 test_wifi_ap_survey.py
"""

import json
import os
import subprocess
import sys
import tempfile
from unittest.mock import MagicMock, patch
import pytest

import wifi_ap_survey as was

SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wifi_ap_survey.py")


def test_script_exists_and_executable():
    """Verify script file exists and is executable."""
    assert os.path.isfile(SCRIPT_PATH)
    assert os.access(SCRIPT_PATH, os.X_OK)


def test_help_flag():
    """Verify --help flag provides rich details and supported brand information."""
    res = subprocess.run([SCRIPT_PATH, "--help"], stdout=subprocess.PIPE, text=True)
    assert res.returncode == 0
    assert "Multi-Vendor Access Point Interrogator & Wireless Client Surveyor" in res.stdout
    assert "ASUS" in res.stdout
    assert "Netgear" in res.stdout
    assert "Verizon" in res.stdout
    assert "UniFi" in res.stdout
    assert "OpenWrt" in res.stdout
    assert "MikroTik" in res.stdout
    assert "TP-Link" in res.stdout


def test_version_flag():
    """Verify --version flag outputs version."""
    res = subprocess.run([SCRIPT_PATH, "--version"], stdout=subprocess.PIPE, text=True)
    assert res.returncode == 0
    assert f"wifi_ap_survey.py {was.VERSION}" in res.stdout


def test_load_network_inventory():
    """Verify inventory loading with temporary CSV content."""
    with tempfile.NamedTemporaryFile(mode="w+", delete=False) as tf:
        tf.write("# MAC,IP,NAME\n")
        tf.write("AA:BB:CC:DD:EE:FF,192.168.1.99,test-device\n")
        tf_name = tf.name

    try:
        mac_inv, ip_inv = was.load_network_inventory(tf_name)
        assert "AA:BB:CC:DD:EE:FF" in mac_inv
        assert mac_inv["AA:BB:CC:DD:EE:FF"] == ("192.168.1.99", "test-device")
        assert ip_inv.get("192.168.1.99") == "test-device"
    finally:
        os.remove(tf_name)


def test_fingerprint_ap_detection():
    """Verify fingerprinter matches signature responses."""
    with patch("urllib.request.urlopen") as mock_url:
        # Mock Netgear 401 header
        mock_resp = MagicMock()
        mock_resp.headers = {"www-authenticate": 'Basic realm="NETGEAR R6700v3"'}
        mock_resp.read.return_value = b""
        mock_url.return_value.__enter__.return_value = mock_resp
        assert was.fingerprint_ap("192.168.1.1") == "netgear"

        # Mock ASUS Login redirection
        mock_resp.headers = {"server": "httpd/2.0"}
        mock_resp.read.return_value = b"<script>window.top.location.href='/Main_Login.asp';</script>"
        assert was.fingerprint_ap("192.168.1.2") == "asus"

        # Mock Verizon loginStatus
        mock_resp.headers = {}
        mock_resp.read.return_value = b'{"loginToken": "abc123token"}'
        assert was.fingerprint_ap("192.168.1.3") == "verizon"


def test_fingerprint_unifi_vs_generic_404():
    """Verify UniFi fingerprinting matches authentic controller responses and ignores generic 404s (e.g. Chromecast)."""
    import urllib.error

    # Case 1: Real UniFi controller returns JSON error on port 8443
    err_body = b'{"data":[],"meta":{"rc":"error","msg":"api.err.LoginRequired"}}'
    http_err = urllib.error.HTTPError("https://192.168.1.10:8443/api/login", 400, "Bad Request", {"Set-Cookie": "unificookie=123"}, None)
    http_err.read = MagicMock(return_value=err_body)

    with patch("urllib.request.urlopen", side_effect=http_err):
        assert was.fingerprint_ap("192.168.1.10") == "unifi"

    # Case 2: Generic 404 device on port 8443 (e.g. Google Chromecast Cast TLS service)
    chromecast_err = urllib.error.HTTPError("https://192.168.1.63:8443/api/login", 404, "Not Found", {}, None)
    chromecast_err.read = MagicMock(return_value=b"")

    with patch("urllib.request.urlopen", side_effect=chromecast_err):
        assert was.fingerprint_ap("192.168.1.63") is None


def test_asus_collector_parsing():
    """Verify AsusCollector parsing of ARP and driver dmesg station lines."""
    fake_output = (
        "192.168.1.55 0x1 0x2 AA:BB:CC:DD:EE:FF * br0\n"
        "===STATIONS===\n"
        "AA:BB:CC:DD:EE:FF STA 0 0 0 0 0 0 -45/-45 120/120 0 0 80M\n"
        "===CLIENTLIST===\n"
        "{}"
    )
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout=fake_output, returncode=0)
        collector = was.AsusCollector("192.168.1.2")
        clients = collector.collect()
        assert len(clients) == 1
        assert clients[0]["mac"] == "AA:BB:CC:DD:EE:FF"
        assert clients[0]["ip"] == "192.168.1.55"
        assert clients[0]["rssi"] == "-45 dBm"
        assert clients[0]["rate"] == "120 Mbps"
        assert clients[0]["band"] == "5 GHz"


def test_netgear_collector_parsing():
    """Verify NetgearCollector parsing of DEV_device.htm wireless table."""
    html_sample = (
        "<b>Wireless Devices</b>"
        '<tr><td align="center"><span class="thead">1</span></td>'
        '<td align="center" noWrap><span class="ttext">192.168.1.75</span></td>'
        '<td align="center" noWrap><span class="ttext">printer</span></td>'
        '<td align="center" noWrap><span class="ttext">00:80:92:00:00:01</span></td></tr>'
    )
    with patch("urllib.request.build_opener") as mock_opener:
        mock_inst = MagicMock()
        mock_resp = MagicMock()
        mock_resp.read.return_value = html_sample.encode("utf-8")
        mock_inst.open.return_value.__enter__.return_value = mock_resp
        mock_opener.return_value = mock_inst

        collector = was.NetgearCollector("192.168.1.11", password="secret")
        clients = collector.collect()
        assert len(clients) == 1
        assert clients[0]["mac"] == "00:80:92:00:00:01"
        assert clients[0]["ip"] == "192.168.1.75"
        assert clients[0]["hostname"] == "printer"


def test_verizon_collector_arcmd5_and_parsing():
    """Verify VerizonCollector ArcMD5 encoding and station info parsing."""
    # Verify ArcMD5 logic
    token = "testtoken123"
    pw = "mypassword"
    arc = was.VerizonCollector._arc_md5(pw)
    assert len(arc) == 128  # SHA-512 hex length
    encoded = was.VerizonCollector._login_encode(pw, token)
    assert len(encoded) == 128

    # Verify Station Parsing
    js_sample = (
        'addROD("get_wlan_stat_info", ['
        "['4C:17:44:00:00:03','ath0','AC','2 x 2 : 2','-71','20','285262','520000'],"
        "['50:F2:65:00:00:04','ath2','AX','2 x 2 : 2','-78','17','119341','1200000']"
        "]);"
    )
    with patch("urllib.request.build_opener") as mock_opener:
        mock_inst = MagicMock()
        login_status_resp = MagicMock()
        login_status_resp.read.return_value = b'{"loginToken": "fake123"}'

        login_resp = MagicMock(status=200)

        dev_resp = MagicMock()
        dev_resp.read.return_value = js_sample.encode("utf-8")

        mock_inst.open.return_value.__enter__.side_effect = [
            login_status_resp,
            login_resp,
            dev_resp,
            MagicMock()  # logout
        ]
        mock_opener.return_value = mock_inst

        collector = was.VerizonCollector("192.168.1.1", password="secret")
        clients = collector.collect()
        assert len(clients) == 2
        assert clients[0]["mac"] == "4C:17:44:00:00:03"
        assert clients[0]["band"] == "5 GHz"
        assert clients[0]["rssi"] == "-71 dBm"
        assert clients[0]["rate"] == "520 Mbps"
        assert clients[1]["mac"] == "50:F2:65:00:00:04"
        assert clients[1]["band"] == "6 GHz"


def test_verizon_collector_flag2_max_sessions():
    """Verify VerizonCollector provides clear diagnostic when router returns flag 2 (max sessions)."""
    import urllib.error

    with patch("urllib.request.build_opener") as mock_opener:
        mock_inst = MagicMock()
        login_status_cm = MagicMock()
        login_status_resp = MagicMock()
        login_status_resp.read.return_value = b'{"loginToken": "tok123"}'
        login_status_cm.__enter__.return_value = login_status_resp

        flag2_err = urllib.error.HTTPError(
            "https://192.168.1.1/login.cgi",
            403,
            "Forbidden",
            {},
            None
        )
        flag2_err.read = MagicMock(return_value=b'{"flag": 2, "maxsession": 10, "timeout": 0}')

        mock_inst.open.side_effect = [
            login_status_cm,
            flag2_err
        ]
        mock_opener.return_value = mock_inst

        collector = was.VerizonCollector("192.168.1.1", password="secret")
        res = collector.collect()
        assert len(res) == 1
        assert res[0]["mac"] == "AUTH_FAILED"
        assert "maximum open sessions (10) reached" in res[0]["note"]


def test_unifi_collector_parsing():
    """Verify UniFiCollector parsing of stat/sta response."""
    sta_json = {
        "data": [
            {
                "mac": "11:22:33:44:55:66",
                "ip": "192.168.1.40",
                "name": "unifi-client",
                "is_wired": False,
                "radio": "na",
                "channel": 36,
                "signal": -52,
                "tx_rate": 866000,
                "ap_mac": "e0:63:da:11:22:33"
            }
        ]
    }
    with patch("urllib.request.build_opener") as mock_opener:
        mock_inst = MagicMock()
        login_resp = MagicMock(status=200)
        data_resp = MagicMock()
        data_resp.read.return_value = json.dumps(sta_json).encode("utf-8")
        mock_inst.open.return_value.__enter__.side_effect = [login_resp, data_resp]
        mock_opener.return_value = mock_inst

        collector = was.UniFiCollector("192.168.1.10", user="admin", password="secret")
        clients = collector.collect()
        assert len(clients) == 1
        assert clients[0]["mac"] == "11:22:33:44:55:66"
        assert clients[0]["band"] == "5 GHz"
        assert clients[0]["rate"] == "866 Mbps"


def test_openwrt_collector_parsing():
    """Verify OpenWrtCollector parsing of ubus json output."""
    output = (
        "192.168.1.80 0x1 0x2 33:44:55:66:77:88 * br-lan\n"
        "===UBUS===\n"
        '"33:44:55:66:77:88": {"signal": -48, "tx": {"rate": 300000}}\n'
    )
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout=output, returncode=0)
        collector = was.OpenWrtCollector("192.168.1.20")
        clients = collector.collect()
        assert len(clients) == 1
        assert clients[0]["mac"] == "33:44:55:66:77:88"
        assert clients[0]["ip"] == "192.168.1.80"
        assert clients[0]["rssi"] == "-48 dBm"
        assert clients[0]["rate"] == "300 Mbps"


def test_mikrotik_collector_parsing():
    """Verify MikroTikCollector parsing of registration table."""
    reg_data = [
        {
            "mac-address": "55:66:77:88:99:AA",
            "signal-strength": "-58dBm@6Mbps",
            "tx-rate": "400Mbps-80MHz/2S/SGI"
        }
    ]
    with patch("urllib.request.build_opener") as mock_opener:
        mock_inst = MagicMock()
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(reg_data).encode("utf-8")
        mock_inst.open.return_value.__enter__.return_value = mock_resp
        mock_opener.return_value = mock_inst

        collector = was.MikroTikCollector("192.168.1.30", user="admin", password="secret")
        clients = collector.collect()
        assert len(clients) == 1
        assert clients[0]["mac"] == "55:66:77:88:99:AA"
        assert clients[0]["rssi"] == "-58dBm dBm"
        assert clients[0]["rate"] == "400Mbps-80MHz/2S/SGI"


def test_tplink_collector_parsing():
    """Verify TPLinkCollector parsing of Omada client list."""
    omada_login = {"result": {"token": "omada-token-123"}}
    omada_clients = {
        "result": {
            "data": [
                {
                    "mac": "66:77:88:99:AA:BB",
                    "ip": "192.168.1.150",
                    "name": "omada-laptop",
                    "wireless": True,
                    "radioId": 1,
                    "rssi": -55,
                    "txRate": 866,
                    "apName": "EAP610-Office"
                }
            ]
        }
    }
    with patch("urllib.request.build_opener") as mock_opener:
        mock_inst = MagicMock()
        login_resp = MagicMock()
        login_resp.read.return_value = json.dumps(omada_login).encode("utf-8")
        data_resp = MagicMock()
        data_resp.read.return_value = json.dumps(omada_clients).encode("utf-8")
        mock_inst.open.return_value.__enter__.side_effect = [login_resp, data_resp]
        mock_opener.return_value = mock_inst

        collector = was.TPLinkCollector("192.168.1.40", user="admin", password="secret")
        clients = collector.collect()
        assert len(clients) == 1
        assert clients[0]["mac"] == "66:77:88:99:AA:BB"
        assert clients[0]["band"] == "5 GHz"
        assert clients[0]["rssi"] == "-55 dBm"
        assert clients[0]["rate"] == "866 Mbps"


def test_ip_sort_key_and_ordering():
    """Verify ip_sort_key orders IPv4 addresses numerically and handles unknowns."""
    assert was.ip_sort_key("192.168.0.1") == [192, 168, 0, 1]
    assert was.ip_sort_key("192.168.0.11") == [192, 168, 0, 11]
    assert was.ip_sort_key("Unknown") == [999, 999, 999, 999]
    assert was.ip_sort_key(None) == [999, 999, 999, 999]

    # Test numerical ordering (e.g. .1 < .2 < .3 < .11)
    ips = ["192.168.0.11", "192.168.0.3", "192.168.0.1", "192.168.0.2", "Unknown"]
    sorted_ips = sorted(ips, key=was.ip_sort_key)
    assert sorted_ips == ["192.168.0.1", "192.168.0.2", "192.168.0.3", "192.168.0.11", "Unknown"]

    # Test sorting records by AP IP address
    records = [
        {"ap_ip": "192.168.0.11", "ip": "192.168.0.50", "device_name": "device-netgear"},
        {"ap_ip": "192.168.0.2", "ip": "192.168.0.15", "device_name": "device-asus2"},
        {"ap_ip": "192.168.0.1", "ip": "192.168.0.60", "device_name": "device-verizon"},
        {"ap_ip": "192.168.0.3", "ip": "192.168.0.96", "device_name": "device-asus3"},
    ]
    records.sort(key=lambda r: (was.ip_sort_key(r.get("ap_ip")), was.ip_sort_key(r.get("ip")), str(r.get("device_name", ""))))
    assert [r["ap_ip"] for r in records] == ["192.168.0.1", "192.168.0.2", "192.168.0.3", "192.168.0.11"]


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))
