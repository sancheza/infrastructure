"""
Regression and integrity tests for pihole_importer.py.

Covers: MAC-change DHCP replacement, UI-added DNS record preservation,
multiple DNS aliases sharing one IP, conflict-resolution scoping,
unparseable-entry warnings, and TOML integrity validation.

Run with: pytest test_pihole_importer.py
"""

import os
import sys

import pytest
import toml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pihole_importer as pi

TESTS_FOR_VERSION = pi.VERSION


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


# ---------- UI-added DNS record preservation (the reported bug) ----------

def test_ui_added_dns_record_survives_import(tmp_path, monkeypatch):
    """
    A DNS record added via Settings > Local DNS Records is written by
    Pi-hole as a two-field "IP HOSTNAME" entry (per Pi-hole's dns.hosts
    format docs), not the three-field "IP FQDN HOSTNAME" form this script
    writes for its own entries. Running the importer must not drop it.
    """
    toml_path = tmp_path / "pihole.toml"
    _write_toml(
        toml_path,
        dhcp_hosts=["AA:BB:CC:DD:EE:01,192.168.1.50,myhost"],
        dns_hosts=[
            "192.168.1.50 myhost.home.lan myhost",
            "192.168.1.99 manual.home.lan",  # added by hand via the UI
        ],
    )
    monkeypatch.setattr(pi, "PIHOLE_TOML_PATH", str(toml_path))
    _stub_subprocess(monkeypatch)

    # Re-import the same CSV-derived entries; unrelated to the manual record.
    pi.update_pihole_toml(
        ["192.168.1.50 myhost.home.lan myhost"],
        ["AA:BB:CC:DD:EE:01,192.168.1.50,myhost"],
    )

    data = toml.load(str(toml_path))
    dns_ips = {pi.parse_dns_entry(e)[0] for e in data["dns"]["hosts"]}
    assert "192.168.1.99" in dns_ips


def test_unparseable_dns_entry_is_logged(capsys):
    pi._WARNED_UNPARSEABLE.clear()
    ip, fqdn, hostname = pi.parse_dns_entry("nofieldshere")
    assert (ip, fqdn, hostname) == (None, None, None)
    assert "Skipping unparseable dns.hosts entry" in capsys.readouterr().err


def test_unparseable_dhcp_entry_is_logged(capsys):
    pi._WARNED_UNPARSEABLE.clear()
    mac, ip, hostname = pi.parse_dhcp_entry("AA:BB:CC:DD:EE:FF,192.168.1.50")
    assert (mac, ip, hostname) == (None, None, None)
    assert "Skipping unparseable dhcp.hosts entry" in capsys.readouterr().err


# ---------- Multiple DNS aliases sharing one IP (the reported bug) ----------

def test_multiple_dns_aliases_on_one_ip_all_survive(tmp_path, monkeypatch):
    """
    Pi-hole allows several independent dns.hosts lines to share one IP --
    e.g. multiple hostnames pointing at a reverse proxy. The importer must
    not collapse them down to a single entry per IP on rewrite.
    """
    toml_path = tmp_path / "pihole.toml"
    _write_toml(
        toml_path,
        dhcp_hosts=["BC:24:11:15:0F:7F,192.168.0.16,caddy"],
        dns_hosts=[
            "192.168.0.16 caddy.home.lan caddy",  # the DHCP-driven record
            "192.168.0.16 sonarr",  # manually-added alias
            "192.168.0.16 radarr",  # another manually-added alias
        ],
    )
    monkeypatch.setattr(pi, "PIHOLE_TOML_PATH", str(toml_path))
    _stub_subprocess(monkeypatch)

    # Re-import the same, unchanged CSV-derived caddy entry.
    pi.update_pihole_toml(
        ["192.168.0.16 caddy.home.lan caddy"],
        ["BC:24:11:15:0F:7F,192.168.0.16,caddy"],
    )

    data = toml.load(str(toml_path))
    hostnames = {pi.parse_dns_entry(e)[2] for e in data["dns"]["hosts"]}
    assert hostnames == {"caddy", "sonarr", "radarr"}


def test_conflict_resolution_only_replaces_matching_alias(tmp_path, monkeypatch):
    """
    When a reservation's hostname changes, only the stale entry for that
    exact (ip, hostname) should be removed -- other aliases on the same IP
    must survive.
    """
    toml_path = tmp_path / "pihole.toml"
    _write_toml(
        toml_path,
        dhcp_hosts=["BC:24:11:15:0F:7F,192.168.0.16,caddy"],
        dns_hosts=[
            "192.168.0.16 caddy.home.lan caddy",
            "192.168.0.16 sonarr",  # unrelated manual alias, must survive
        ],
    )
    monkeypatch.setattr(pi, "PIHOLE_TOML_PATH", str(toml_path))
    _stub_subprocess(monkeypatch)

    # Rename the caddy host itself -- this is a hostname change for an
    # existing MAC, which goes through the conflict-confirmation path.
    pi.update_pihole_toml(
        ["192.168.0.16 caddy-proxy.home.lan caddy-proxy"],
        ["BC:24:11:15:0F:7F,192.168.0.16,caddy-proxy"],
    )

    data = toml.load(str(toml_path))
    hostnames = {pi.parse_dns_entry(e)[2] for e in data["dns"]["hosts"]}
    assert hostnames == {"caddy-proxy", "sonarr"}
    assert "caddy" not in hostnames


def test_validate_accepts_multiple_dns_aliases_on_one_ip():
    data = {
        "dhcp": {"hosts": []},
        "dns": {"hosts": [
            "192.168.1.50 myhost.home.lan myhost",
            "192.168.1.50 otherhost",
        ]},
    }
    pi.validate_toml_integrity(data)  # should not raise


def test_warn_dropped_entries_logs_removed_dns_entry(capsys):
    before = ["192.168.1.50 myhost.home.lan myhost", "192.168.1.60 stays.home.lan stays"]
    after = ["192.168.1.60 stays.home.lan stays"]
    pi.warn_dropped_entries("dns.hosts", pi.dns_entry_identity, before, after)
    err = capsys.readouterr().err
    assert "dns.hosts entry removed by this run" in err
    assert "myhost" in err
    assert "stays" not in err


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


def test_validate_rejects_duplicate_dns_entry():
    """
    An exact (ip, hostname) repeat is rejected, but the same IP appearing
    under two different hostnames is not -- Pi-hole allows multiple
    aliases to share one IP (see test_validate_accepts_multiple_dns_aliases_on_one_ip).
    """
    data = {
        "dhcp": {"hosts": []},
        "dns": {"hosts": [
            "192.168.1.50 myhost.home.lan myhost",
            "192.168.1.50 myhost.home.lan myhost",
        ]},
    }
    with pytest.raises(ValueError, match="Duplicate dns.hosts entry"):
        pi.validate_toml_integrity(data)


def test_validate_accepts_clean_config():
    data = {
        "dhcp": {"hosts": ["AA:BB:CC:DD:EE:01,192.168.1.50,myhost"]},
        "dns": {"hosts": ["192.168.1.50 myhost.home.lan myhost"]},
    }
    pi.validate_toml_integrity(data)  # should not raise


# ---------- Direct invocation (pytest never executes this) ----------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            __doc__.strip() + "\n\n"
            "This file is a pytest module, not a standalone CLI -- running it\n"
            "directly (as here) only prints this info and exits; it does not\n"
            "execute the tests."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "-v", "--version", action="version",
        version=f"%(prog)s (tests for pihole_importer.py {TESTS_FOR_VERSION})",
    )
    parser.parse_args()
    parser.print_help()
