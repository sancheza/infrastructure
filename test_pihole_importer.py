"""
Regression and integrity tests for pihole_importer.py.

Run with: pytest test_pihole_importer.py
"""

import os
import sys

import pytest
import toml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pihole_importer as pi


class _FakeCompletedProcess:
    stdout = "Active: active (running)"


def _write_toml(path, dhcp_hosts, dns_hosts):
    path.write_text(toml.dumps({
        "dhcp": {"hosts": dhcp_hosts},
        "dns": {"hosts": dns_hosts},
    }))


def _stub_subprocess(monkeypatch):
    monkeypatch.setattr(pi.subprocess, "run", lambda *a, **k: _FakeCompletedProcess())
    monkeypatch.setattr("builtins.input", lambda *_: "y")


@pytest.fixture
def pihole_toml(tmp_path, monkeypatch):
    toml_path = tmp_path / "pihole.toml"
    _write_toml(
        toml_path,
        dhcp_hosts=["AA:BB:CC:DD:EE:01,192.168.1.50,myhost"],
        dns_hosts=["192.168.1.50 myhost.home.lan myhost"],
    )
    monkeypatch.setattr(pi, "PIHOLE_TOML_PATH", str(toml_path))
    _stub_subprocess(monkeypatch)
    return toml_path


# ---------- MAC-change regression (the reported bug) ----------

def test_mac_change_replaces_not_duplicates(pihole_toml):
    """
    Editing macaddr.txt to change the MAC address of an existing IP/hostname
    must replace the dhcp.hosts entry, not add a second one. Previously the
    stale entry under the old MAC was never removed, producing two entries
    for the same IP and breaking pihole-FTL on restart.
    """
    dns_hosts = ["192.168.1.50 myhost.home.lan myhost"]
    dhcp_hosts = ["AA:BB:CC:DD:EE:02,192.168.1.50,myhost"]  # MAC changed

    pi.update_pihole_toml(dns_hosts, dhcp_hosts)

    data = toml.load(str(pihole_toml))
    assert len(data["dhcp"]["hosts"]) == 1
    mac, ip, hostname = pi.parse_dhcp_entry(data["dhcp"]["hosts"][0])
    assert mac.upper() == "AA:BB:CC:DD:EE:02"
    assert ip == "192.168.1.50"
    assert len(data["dns"]["hosts"]) == 1


def test_mac_change_is_case_insensitive(tmp_path, monkeypatch):
    """
    Same as above, but with the on-disk MAC stored lowercase (as Pi-hole
    itself often writes it), to make sure the old-entry removal isn't
    skipped due to a case mismatch between the CSV (uppercase) and the
    file on disk.
    """
    toml_path = tmp_path / "pihole.toml"
    _write_toml(
        toml_path,
        dhcp_hosts=["aa:bb:cc:dd:ee:01,192.168.1.50,myhost"],
        dns_hosts=["192.168.1.50 myhost.home.lan myhost"],
    )
    monkeypatch.setattr(pi, "PIHOLE_TOML_PATH", str(toml_path))
    _stub_subprocess(monkeypatch)

    pi.update_pihole_toml(
        ["192.168.1.50 myhost.home.lan myhost"],
        ["AA:BB:CC:DD:EE:02,192.168.1.50,myhost"],
    )

    data = toml.load(str(toml_path))
    assert len(data["dhcp"]["hosts"]) == 1


def test_new_host_is_added_without_touching_existing(pihole_toml):
    dns_hosts = ["192.168.1.51 other.home.lan other"]
    dhcp_hosts = ["11:22:33:44:55:66,192.168.1.51,other"]

    pi.update_pihole_toml(dns_hosts, dhcp_hosts)

    data = toml.load(str(pihole_toml))
    assert len(data["dhcp"]["hosts"]) == 2
    macs = {pi.parse_dhcp_entry(e)[0].upper() for e in data["dhcp"]["hosts"]}
    assert macs == {"AA:BB:CC:DD:EE:01", "11:22:33:44:55:66"}


def test_idempotent_rerun_does_not_duplicate(pihole_toml):
    dns_hosts = ["192.168.1.50 myhost.home.lan myhost"]
    dhcp_hosts = ["AA:BB:CC:DD:EE:01,192.168.1.50,myhost"]  # unchanged

    pi.update_pihole_toml(dns_hosts, dhcp_hosts)

    data = toml.load(str(pihole_toml))
    assert len(data["dhcp"]["hosts"]) == 1
    assert len(data["dns"]["hosts"]) == 1


# ---------- Integrity validation ----------

def test_validate_rejects_duplicate_mac():
    data = {
        "dhcp": {"hosts": [
            "AA:BB:CC:DD:EE:01,192.168.1.50,myhost",
            "AA:BB:CC:DD:EE:01,192.168.1.51,otherhost",
        ]},
        "dns": {"hosts": []},
    }
    with pytest.raises(ValueError, match="Duplicate MAC"):
        pi.validate_toml_integrity(data)


def test_validate_rejects_duplicate_dhcp_ip():
    data = {
        "dhcp": {"hosts": [
            "AA:BB:CC:DD:EE:01,192.168.1.50,myhost",
            "AA:BB:CC:DD:EE:02,192.168.1.50,otherhost",
        ]},
        "dns": {"hosts": []},
    }
    with pytest.raises(ValueError, match="Duplicate IP"):
        pi.validate_toml_integrity(data)


def test_validate_rejects_duplicate_dns_ip():
    data = {
        "dhcp": {"hosts": []},
        "dns": {"hosts": [
            "192.168.1.50 myhost.home.lan myhost",
            "192.168.1.50 otherhost.home.lan otherhost",
        ]},
    }
    with pytest.raises(ValueError, match="Duplicate IP"):
        pi.validate_toml_integrity(data)


def test_validate_accepts_clean_config():
    data = {
        "dhcp": {"hosts": ["AA:BB:CC:DD:EE:01,192.168.1.50,myhost"]},
        "dns": {"hosts": ["192.168.1.50 myhost.home.lan myhost"]},
    }
    pi.validate_toml_integrity(data)  # should not raise
