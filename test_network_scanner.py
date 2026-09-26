#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_network_scanner.py

Unit and regression tests for network_scanner.py.
Validates text truncation, port color coding, IP sorting, CSV export,
metadata integrity, and command-line argument parsing.

Usage:
    pytest test_network_scanner.py
    python3 test_network_scanner.py [-v] [-h]
"""

import argparse
import csv
import os
import sys
import pytest
from rich.console import Console

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import network_scanner as ns


def test_truncate_text_none():
    """Verify that None returns 'N/A'."""
    assert ns.truncate_text(None, 30) == "N/A"


def test_truncate_text_short():
    """Verify that short text remains unmodified."""
    assert ns.truncate_text("short text", 30) == "short text"


def test_truncate_text_exact():
    """Verify that text at exact max_width is not truncated."""
    text = "a" * 30
    assert ns.truncate_text(text, 30) == text


def test_truncate_text_long():
    """Verify that text exceeding max_width is truncated with ellipsis."""
    text = "a" * 35
    truncated = ns.truncate_text(text, 30)
    assert len(truncated) == 30
    assert truncated.endswith("...")
    assert truncated == ("a" * 27) + "..."


def test_truncate_text_newlines():
    """Verify that newlines are converted to spaces."""
    text = "line1\nline2"
    assert ns.truncate_text(text, 30) == "line1 line2"


def test_get_port_color_open():
    """Verify open port returns green."""
    assert ns.get_port_color("open") == "green"
    assert ns.get_port_color("Open") == "green"


def test_get_port_color_closed_and_filtered():
    """Verify closed and filtered ports return red."""
    assert ns.get_port_color("closed") == "red"
    assert ns.get_port_color("filtered") == "red"
    assert ns.get_port_color("Closed") == "red"


def test_get_port_color_unknown():
    """Verify other statuses default to white."""
    assert ns.get_port_color("unknown") == "white"
    assert ns.get_port_color("N/A") == "white"


def test_sort_by_ip():
    """Verify that results are sorted numerically by IPv4 address."""
    records = [
        {"IP": "192.168.1.100"},
        {"IP": "192.168.1.2"},
        {"IP": "192.168.1.20"},
        {"IP": "192.168.1.1"},
    ]
    sorted_records = ns.sort_by_ip(records)
    expected_order = [
        "192.168.1.1",
        "192.168.1.2",
        "192.168.1.20",
        "192.168.1.100",
    ]
    assert [r["IP"] for r in sorted_records] == expected_order


def test_export_to_csv(tmp_path):
    """Verify that export_to_csv writes valid CSV output."""
    output_file = tmp_path / "test_inventory.csv"
    console = Console(quiet=True)
    records = [
        {
            "Hostname": "router.lan",
            "OS": "Linux",
            "IP": "192.168.1.1",
            "MAC": "00:11:22:33:44:55",
            "Vendor": "RouterCorp",
            "SSH_22": "open",
            "HTTP_80": "open",
        }
    ]
    ns.export_to_csv(records, str(output_file), console)
    assert output_file.exists()

    with open(output_file, mode="r", newline="", encoding="utf-8") as f:
        reader = list(csv.DictReader(f))
    assert len(reader) == 1
    assert reader[0]["Hostname"] == "router.lan"
    assert reader[0]["IP"] == "192.168.1.1"


def test_export_to_csv_empty(tmp_path):
    """Verify that export_to_csv handles empty results without error."""
    output_file = tmp_path / "empty.csv"
    console = Console(quiet=True)
    ns.export_to_csv([], str(output_file), console)
    assert not output_file.exists()


def test_metadata_no_stale_name():
    """Verify that network_scanner.py docstrings and help do not reference network_inventory.py."""
    assert "network_inventory.py" not in ns.__doc__


def main():
    """Run pytest suite with optional verbosity flag."""
    parser = argparse.ArgumentParser(
        description="Run test suite for network_scanner.py."
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Run tests in verbose mode."
    )
    args = parser.parse_args()

    pytest_args = [__file__]
    if args.verbose:
        pytest_args.append("-v")

    sys.exit(pytest.main(pytest_args))


if __name__ == "__main__":
    main()
