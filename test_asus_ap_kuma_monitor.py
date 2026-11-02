#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_asus_ap_kuma_monitor.py

Unit tests for asus_ap_kuma_monitor.sh.
Validates CLI argument handling, help output, version flags,
and dry-run execution paths.

Usage:
    pytest test_asus_ap_kuma_monitor.py
    python3 test_asus_ap_kuma_monitor.py
"""

import os
import subprocess
import sys
import pytest

SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "asus_ap_kuma_monitor.sh")


def run_script(*args):
    """Execute the monitor script with the given arguments."""
    cmd = [SCRIPT_PATH] + list(args)
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result


def test_script_exists_and_executable():
    """Verify script file existence and executable permission."""
    assert os.path.isfile(SCRIPT_PATH)
    assert os.access(SCRIPT_PATH, os.X_OK)


def test_help_flag():
    """Verify that -h and --help print usage information and exit 0."""
    res_short = run_script("-h")
    assert res_short.returncode == 0
    assert "asus_ap_kuma_monitor.sh - ASUS Access Point Health Monitor" in res_short.stdout

    res_long = run_script("--help")
    assert res_long.returncode == 0
    assert "OPTIONS:" in res_long.stdout


def test_version_flag():
    """Verify that -v and --version print version information and exit 0."""
    res_short = run_script("-v")
    assert res_short.returncode == 0
    assert "asus_ap_kuma_monitor.sh version 1.0.0" in res_short.stdout

    res_long = run_script("--version")
    assert res_long.returncode == 0
    assert "1.0.0" in res_long.stdout


def test_invalid_argument():
    """Verify that an unknown argument prints an error and exits 1."""
    res = run_script("--invalid-option-xyz")
    assert res.returncode == 1
    assert "Unknown option" in res.stderr


def test_dry_run_unreachable_target():
    """Verify that targeting an unreachable IP fails gracefully with exit code 1."""
    res = run_script("--dry-run", "--aps", "192.0.2.1:1")
    assert res.returncode == 1
    assert "unreachable via SSH" in res.stderr


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))
