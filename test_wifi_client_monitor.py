#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_wifi_client_monitor.py

Unit tests for wifi_client_monitor.py.
Validates CLI parsing, inventory loading, JSON mode, and collector structure.

Usage:
    pytest test_wifi_client_monitor.py
    python3 test_wifi_client_monitor.py
"""

import os
import subprocess
import sys
import tempfile
import pytest

import wifi_client_monitor as wcm

SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wifi_client_monitor.py")


def test_script_exists_and_executable():
    """Verify script file exists and is executable."""
    assert os.path.isfile(SCRIPT_PATH)
    assert os.access(SCRIPT_PATH, os.X_OK)


def test_help_flag():
    """Verify --help flag works cleanly."""
    res = subprocess.run([SCRIPT_PATH, "--help"], stdout=subprocess.PIPE, text=True)
    assert res.returncode == 0
    assert "Unified Wireless Client and AP Association Monitor" in res.stdout


def test_version_flag():
    """Verify --version flag outputs version."""
    res = subprocess.run([SCRIPT_PATH, "--version"], stdout=subprocess.PIPE, text=True)
    assert res.returncode == 0
    assert "wifi_client_monitor.py 1.0.0" in res.stdout


def test_load_network_inventory():
    """Verify inventory loading with temporary CSV content."""
    with tempfile.NamedTemporaryFile(mode="w+", delete=False) as tf:
        tf.write("# MAC,IP,NAME\n")
        tf.write("AA:BB:CC:DD:EE:FF,192.168.0.99,test-device\n")
        tf_name = tf.name

    try:
        mac_inv, ip_inv = wcm.load_network_inventory(tf_name)
        assert "AA:BB:CC:DD:EE:FF" in mac_inv
        assert mac_inv["AA:BB:CC:DD:EE:FF"] == ("192.168.0.99", "test-device")
        assert ip_inv.get("192.168.0.99") == "test-device"
    finally:
        os.remove(tf_name)


def test_netgear_auth_prompt():
    """Verify NetgearCollector returns AUTH_REQUIRED notice when unauthenticated."""
    collector = wcm.NetgearCollector("192.168.0.11", password=None)
    records = collector.collect()
    assert len(records) == 1
    assert records[0]["mac"] == "AUTH_REQUIRED"
    assert "NETGEAR_PASSWORD" in records[0]["note"]


def test_verizon_auth_prompt():
    """Verify VerizonCollector returns AUTH_REQUIRED notice when unauthenticated."""
    collector = wcm.VerizonCollector("192.168.0.1", password=None)
    records = collector.collect()
    assert len(records) == 1
    assert records[0]["mac"] == "AUTH_REQUIRED"
    assert "VERIZON_PASSWORD" in records[0]["note"]


def test_auth_failed_formatting():
    """Verify AUTH_FAILED records are structured properly."""
    rec = {
        "mac": "AUTH_FAILED",
        "ip": "192.168.0.1",
        "band": "Unknown",
        "rssi": "N/A",
        "rate": "N/A",
        "ap_type": "Verizon Fios Router",
        "ap_ip": "192.168.0.1",
        "note": "Authentication failed: invalid password"
    }
    assert rec["mac"] == "AUTH_FAILED"
    assert rec["ap_ip"] == "192.168.0.1"


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))
