"""
Tests for macaddr_reservation_service.py.

Covers: section parsing, free-IP allocation, sorted insertion, add-only
duplicate rejection, dry-run behavior, token loading, the allowed-network
connection filter, and an end-to-end HTTP round trip.

Run with: pytest test_macaddr_reservation_service.py
"""

import http.client
import ipaddress
import json
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import macaddr_reservation_service as svc
import pihole_importer

SAMPLE_MACADDR_TXT = """# Servers .01 to .50
AC:91:9B:63:4E:B7,192.168.0.1,fios
7C:10:C9:DD:CD:A8,192.168.0.2,router
A8:A1:59:2B:BC:6B,192.168.0.6,proxmox

# IoT-trusted .51 to .53
34:D2:70:7F:75:C6,192.168.0.51,echo-antonio
44:D5:CC:22:1B:31,192.168.0.53,echo-addition

# Endpoints .151 to .151
34:5A:60:3E:70:1E,192.168.0.151,aarondesktop
"""


def _write_sample(path):
    path.write_text(SAMPLE_MACADDR_TXT)
    return path


# ---------- Parsing ----------

def test_parse_sections_matches_real_format(tmp_path):
    path = _write_sample(tmp_path / "macaddr.txt")
    lines, trailing = svc.read_reservations(str(path))
    assert trailing is True

    sections = svc.parse_sections(lines)
    labels = [(s.label, s.start, s.end) for s in sections]
    assert labels == [
        ("Servers", 1, 50),
        ("IoT-trusted", 51, 53),
        ("Endpoints", 151, 151),
    ]
    assert [e.ip for e in sections[0].entries] == [
        "192.168.0.1", "192.168.0.2", "192.168.0.6",
    ]


def test_find_section_case_insensitive(tmp_path):
    path = _write_sample(tmp_path / "macaddr.txt")
    lines, _ = svc.read_reservations(str(path))
    sections = svc.parse_sections(lines)

    assert svc.find_section(sections, "servers").label == "Servers"
    assert svc.find_section(sections, "SERVERS").label == "Servers"
    assert svc.find_section(sections, "iot-trusted").label == "IoT-trusted"
    assert svc.find_section(sections, "nonexistent") is None


def test_find_existing_detects_mac_and_hostname(tmp_path):
    path = _write_sample(tmp_path / "macaddr.txt")
    lines, _ = svc.read_reservations(str(path))
    sections = svc.parse_sections(lines)

    assert svc.find_existing(sections, "AC:91:9B:63:4E:B7", "newname") == (
        "mac", "192.168.0.1")
    assert svc.find_existing(sections, "00:00:00:00:00:00", "proxmox") == (
        "hostname", "192.168.0.6")
    assert svc.find_existing(sections, "00:00:00:00:00:00", "newname") is None


# ---------- IP allocation ----------

def test_find_free_ip_skips_used(tmp_path):
    path = _write_sample(tmp_path / "macaddr.txt")
    lines, _ = svc.read_reservations(str(path))
    sections = svc.parse_sections(lines)
    used = {e.ip for s in sections for e in s.entries}

    servers = svc.find_section(sections, "servers")
    assert svc.find_free_ip(servers, used, "192.168.0.") == "192.168.0.3"


def test_find_free_ip_returns_none_when_full(tmp_path):
    path = _write_sample(tmp_path / "macaddr.txt")
    lines, _ = svc.read_reservations(str(path))
    sections = svc.parse_sections(lines)
    used = {e.ip for s in sections for e in s.entries}

    endpoints = svc.find_section(sections, "endpoints")  # range is .151 to .151, and .151 is used
    assert svc.find_free_ip(endpoints, used, "192.168.0.") is None


# ---------- Sorted insertion ----------

def test_insert_line_in_section_numeric_order(tmp_path):
    path = _write_sample(tmp_path / "macaddr.txt")
    lines, _ = svc.read_reservations(str(path))
    sections = svc.parse_sections(lines)
    servers = svc.find_section(sections, "servers")

    new_line = "AA:BB:CC:DD:EE:FF,192.168.0.3,newhost"
    result = svc.insert_line_in_section(lines, servers, "192.168.0.3", new_line)

    # Must land between .2 and .6, i.e. right after the .2 row.
    idx_of_2 = next(i for i, l in enumerate(result) if l.startswith("7C:10:C9"))
    assert result[idx_of_2 + 1] == new_line


def test_insert_line_in_section_at_start_of_empty_range(tmp_path):
    text = (
        "# Servers .01 to .50\n"
        "\n"
        "# IoT-trusted .51 to .100\n"
        "34:D2:70:7F:75:C6,192.168.0.51,echo\n"
    )
    path = tmp_path / "macaddr.txt"
    path.write_text(text)
    lines, _ = svc.read_reservations(str(path))
    sections = svc.parse_sections(lines)
    servers = svc.find_section(sections, "servers")

    new_line = "AA:BB:CC:DD:EE:FF,192.168.0.1,newhost"
    result = svc.insert_line_in_section(lines, servers, "192.168.0.1", new_line)
    assert result[servers.header_index + 1] == new_line


# ---------- MAC normalization ----------

@pytest.mark.parametrize("raw", [
    "aa:bb:cc:dd:ee:ff", "AA-BB-CC-DD-EE-FF", "aabb.ccdd.eeff", "AABBCCDDEEFF",
])
def test_normalize_mac(raw):
    assert svc._normalize_mac(raw) == "AA:BB:CC:DD:EE:FF"


# ---------- process_reservation ----------

def _config(tmp_path, monkeypatch, importer_result=None):
    reservations_file = str(_write_sample(tmp_path / "macaddr.txt"))
    importer_path = str(tmp_path / "pihole_importer.py")
    with open(importer_path, "w", encoding="utf-8"):
        pass

    if importer_result is None:
        importer_result = {"returncode": 0, "stdout": "ok", "stderr": ""}
    monkeypatch.setattr(svc, "run_importer", lambda config: importer_result)

    return svc.ServiceConfig(
        reservations_file=reservations_file,
        importer_path=importer_path,
        python_bin=sys.executable,
        network_prefix="192.168.0.",
        token="secret",
    )


def test_process_reservation_creates_and_runs_importer(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    result = svc.process_reservation(
        {"mac": "AA:BB:CC:DD:EE:FF", "hostname": "newhost", "host_type": "servers"},
        config, pihole_importer,
    )

    assert result["status"] == "created"
    assert result["ip"] == "192.168.0.3"
    assert result["importer_ok"] is True

    with open(config.reservations_file, encoding="utf-8") as f:
        written = f.read()
    assert "AA:BB:CC:DD:EE:FF,192.168.0.3,newhost" in written
    # Backup was created and the section stayed add-only (nothing removed).
    assert "AC:91:9B:63:4E:B7,192.168.0.1,fios" in written


def test_process_reservation_dry_run_does_not_write(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    with open(config.reservations_file, encoding="utf-8") as f:
        before = f.read()

    result = svc.process_reservation(
        {
            "mac": "AA:BB:CC:DD:EE:FF", "hostname": "newhost",
            "host_type": "servers", "dry_run": True,
        },
        config, pihole_importer,
    )

    assert result["status"] == "dry-run"
    assert result["ip"] == "192.168.0.3"
    with open(config.reservations_file, encoding="utf-8") as f:
        assert f.read() == before


def test_process_reservation_duplicate_mac_is_conflict(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    with pytest.raises(svc.ReservationError) as excinfo:
        svc.process_reservation(
            {"mac": "AC:91:9B:63:4E:B7", "hostname": "somethingnew", "host_type": "servers"},
            config, pihole_importer,
        )
    assert excinfo.value.status == 409
    assert excinfo.value.extra["existing_ip"] == "192.168.0.1"


def test_process_reservation_duplicate_hostname_is_conflict(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    with pytest.raises(svc.ReservationError) as excinfo:
        svc.process_reservation(
            {"mac": "AA:BB:CC:DD:EE:FF", "hostname": "proxmox", "host_type": "servers"},
            config, pihole_importer,
        )
    assert excinfo.value.status == 409
    assert excinfo.value.extra["existing_ip"] == "192.168.0.6"


def test_process_reservation_unknown_host_type(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    with pytest.raises(svc.ReservationError) as excinfo:
        svc.process_reservation(
            {"mac": "AA:BB:CC:DD:EE:FF", "hostname": "newhost", "host_type": "guests"},
            config, pihole_importer,
        )
    assert excinfo.value.status == 404
    assert "Servers" in excinfo.value.extra["valid_host_types"]


def test_process_reservation_section_full(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    with pytest.raises(svc.ReservationError) as excinfo:
        svc.process_reservation(
            {"mac": "AA:BB:CC:DD:EE:FF", "hostname": "newhost", "host_type": "endpoints"},
            config, pihole_importer,
        )
    assert excinfo.value.status == 503


def test_process_reservation_invalid_mac(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    with pytest.raises(svc.ReservationError) as excinfo:
        svc.process_reservation(
            {"mac": "not-a-mac", "hostname": "newhost", "host_type": "servers"},
            config, pihole_importer,
        )
    assert excinfo.value.status == 400


def test_process_reservation_invalid_hostname(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    with pytest.raises(svc.ReservationError) as excinfo:
        svc.process_reservation(
            {"mac": "AA:BB:CC:DD:EE:FF", "hostname": "not valid!", "host_type": "servers"},
            config, pihole_importer,
        )
    assert excinfo.value.status == 400


# ---------- Token loading ----------

def test_load_token_prefers_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("MACADDR_SERVICE_TOKEN", "from-env")
    assert svc.load_token(str(tmp_path / "missing")) == "from-env"


def test_load_token_missing_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.delenv("MACADDR_SERVICE_TOKEN", raising=False)
    assert svc.load_token(str(tmp_path / "missing")) is None


def test_load_token_reads_file(tmp_path, monkeypatch):
    monkeypatch.delenv("MACADDR_SERVICE_TOKEN", raising=False)
    token_file = tmp_path / "token"
    token_file.write_text("from-file\n")
    os.chmod(token_file, 0o600)
    assert svc.load_token(str(token_file)) == "from-file"


# ---------- Trusted-network connection filter ----------

def test_verify_request_allows_only_configured_network():
    handler_cls = svc.make_handler(
        svc.ServiceConfig("", "", "", "", "secret"), pihole_importer
    )
    server = svc.TrustedNetworkServer(
        ("127.0.0.1", 0), handler_cls, ipaddress.ip_network("127.0.0.0/8")
    )
    try:
        assert server.verify_request(None, ("127.0.0.1", 5000)) is True
        assert server.verify_request(None, ("10.0.0.5", 5000)) is False
    finally:
        server.server_close()


# ---------- End-to-end HTTP round trip ----------

def test_end_to_end_http_roundtrip(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    handler_cls = svc.make_handler(config, pihole_importer)
    server = svc.TrustedNetworkServer(
        ("127.0.0.1", 0), handler_cls, ipaddress.ip_network("127.0.0.0/8")
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)

        # Missing token -> 401.
        conn.request("POST", "/reservations", body=json.dumps({}), headers={
            "Content-Type": "application/json",
        })
        resp = conn.getresponse()
        assert resp.status == 401
        resp.read()

        # Valid request -> 201, with the reservation actually written.
        body = json.dumps({
            "mac": "AA:BB:CC:DD:EE:FF", "hostname": "newhost", "host_type": "servers",
        })
        conn.request("POST", "/reservations", body=body, headers={
            "Authorization": "Bearer secret",
            "Content-Type": "application/json",
        })
        resp = conn.getresponse()
        payload = json.loads(resp.read())
        assert resp.status == 201
        assert payload["ip"] == "192.168.0.3"

        conn.request("GET", "/healthz")
        resp = conn.getresponse()
        assert resp.status == 200
        resp.read()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
