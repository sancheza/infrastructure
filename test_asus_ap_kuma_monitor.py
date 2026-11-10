#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_asus_ap_kuma_monitor.py

Unit tests for asus_ap_kuma_monitor.sh.
Validates CLI argument handling, help output, version flags,
dry-run execution paths, and switch CRC delta tracking logic.

Usage:
    pytest test_asus_ap_kuma_monitor.py
    python3 test_asus_ap_kuma_monitor.py
"""

import os
import subprocess
import sys
import pytest

SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "asus_ap_kuma_monitor.sh")


SETTINGS = ["--ssid-24", "ExampleNet", "--ssid-5", "ExampleNet5"]
CONFIG_KEYS = ["KUMA_URL", "ASUS_AP_LIST", "ASUS_AP_SSID_24", "ASUS_AP_SSID_5", "ASUS_AP_SSH_USER"]


@pytest.fixture
def empty_env(tmp_path):
    """Path of an empty env file, so no real .env is read during a test."""
    path = tmp_path / "empty.env"
    path.write_text("")
    return str(path)


def run_script(*args, env=None):
    """Execute the monitor script with the given arguments.

    The caller's own configuration variables are removed so a real .env or
    environment never leaks into a test.
    """
    cmd = [SCRIPT_PATH] + list(args)
    proc_env = {k: v for k, v in os.environ.items() if k not in CONFIG_KEYS}
    if env:
        proc_env.update(env)
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=proc_env,
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
    assert "--state-dir" in res_short.stdout

    res_long = run_script("--help")
    assert res_long.returncode == 0
    assert "OPTIONS:" in res_long.stdout


def test_version_flag():
    """Verify that -v and --version print version information and exit 0."""
    res_short = run_script("-v")
    assert res_short.returncode == 0
    assert "asus_ap_kuma_monitor.sh version 1.2.0" in res_short.stdout

    res_long = run_script("--version")
    assert res_long.returncode == 0
    assert "1.2.0" in res_long.stdout


def test_invalid_argument():
    """Verify that an unknown argument prints an error and exits 1."""
    res = run_script("--invalid-option-xyz")
    assert res.returncode == 1
    assert "Unknown option" in res.stderr


def test_dry_run_unreachable_target(empty_env):
    """Verify that targeting an unreachable IP fails gracefully with exit code 1."""
    res = run_script("--dry-run", "--env-file", empty_env, "--aps", "192.0.2.1:1", *SETTINGS)
    assert res.returncode == 1
    assert "unreachable via SSH" in res.stderr


def test_crc_delta_tracking_workflow(tmp_path, empty_env):
    """Verify CRC delta tracking across consecutive monitor runs."""
    fake_bin_dir = tmp_path / "bin"
    fake_bin_dir.mkdir()
    fake_ssh = fake_bin_dir / "ssh"
    crc_file = tmp_path / "mock_crc.txt"
    crc_file.write_text("500")

    # Mock SSH script that responds with simulated swconfig and iwconfig
    fake_ssh.write_text(f"""#!/bin/bash
RX_CRC=$(cat "{crc_file}")
cat << EOF
Global attributes:
	enable_vlan: 0
Port 2:
	mib: Port 2 MIB counters
TxCRC      : 0
RxCRC      : $RX_CRC
	link: port:2 link:up speed:1000baseT full-duplex
===WIFI===
ra0       RTWIFI SoftAP  ESSID:"ExampleNet"
rai0      RTWIFI SoftAP  ESSID:"ExampleNet5"
===STATS===
EOF
exit 0
""")
    fake_ssh.chmod(0o755)

    test_env = {"PATH": f"{fake_bin_dir}:{os.environ.get('PATH', '')}"}
    state_dir = str(tmp_path / "state")

    # Run 1: Baseline established (500 cumulative errors). Delta is 0. Expected: Pass (exit 0)
    res1 = run_script("--env-file", empty_env, *SETTINGS, "--aps", "127.0.0.1:2", "--state-dir", state_dir, "--dry-run", "--verbose", env=test_env)
    assert res1.returncode == 0
    assert "Port 2 CRC errors within limits" in res1.stdout

    # Now run without dry-run to persist state file
    res1_save = run_script("--env-file", empty_env, *SETTINGS, "--aps", "127.0.0.1:2", "--state-dir", state_dir, env=test_env)
    assert res1_save.returncode == 0

    # Run 2: RxCRC increments to 505 (+5 errors, below threshold 10). Expected: Pass (exit 0)
    crc_file.write_text("505")
    res2 = run_script("--env-file", empty_env, *SETTINGS, "--aps", "127.0.0.1:2", "--state-dir", state_dir, "--verbose", env=test_env)
    assert res2.returncode == 0
    assert "Delta Rx: 5" in res2.stdout

    # Run 3: RxCRC increments to 525 (+20 errors, above threshold 10). Expected: Fail (exit 1)
    crc_file.write_text("525")
    res3 = run_script("--env-file", empty_env, *SETTINGS, "--aps", "127.0.0.1:2", "--state-dir", state_dir, "--verbose", env=test_env)
    assert res3.returncode == 1
    assert "Elevated switch CRC errors" in res3.stdout or "CRC_Delta" in res3.stderr or "CRC_Delta" in res3.stdout

    # Run 4: Counter reset (AP reboot). RxCRC drops to 3. Expected: Pass (exit 0)
    crc_file.write_text("3")
    res4 = run_script("--env-file", empty_env, *SETTINGS, "--aps", "127.0.0.1:2", "--state-dir", state_dir, "--verbose", env=test_env)
    assert res4.returncode == 0
    assert "Delta Rx: 3" in res4.stdout


def test_missing_settings_reported(empty_env):
    """Verify that missing access points and SSIDs are reported and exit 1."""
    res = run_script("--env-file", empty_env, "--dry-run")
    assert res.returncode == 1
    assert "ASUS_AP_LIST" in res.stderr
    assert "ASUS_AP_SSID_24" in res.stderr
    assert "ASUS_AP_SSID_5" in res.stderr


def test_missing_env_file_is_an_error(tmp_path):
    """Verify that an explicit --env-file that does not exist exits 1."""
    res = run_script("--env-file", str(tmp_path / "absent.env"), "--dry-run")
    assert res.returncode == 1
    assert "Env file not found" in res.stderr


def _fake_ssh(tmp_path, ssids):
    """Create a fake ssh on PATH that reports a healthy AP with the given SSIDs."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_ssh = bin_dir / "ssh"
    wifi = "\n".join(f'ra{i}       RTWIFI SoftAP  ESSID:"{name}"' for i, name in enumerate(ssids))
    fake_ssh.write_text(f"""#!/bin/bash
cat << EOF
Port 2:
	mib: Port 2 MIB counters
TxCRC      : 0
RxCRC      : 0
	link: port:2 link:up speed:1000baseT full-duplex
===WIFI===
{wifi}
===STATS===
EOF
""")
    fake_ssh.chmod(0o755)
    return {"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"}


def test_settings_loaded_from_env_file(tmp_path):
    """Verify that access points and SSIDs are read from the env file."""
    env_file = tmp_path / "monitor.env"
    env_file.write_text(
        "# comment\n"
        "ASUS_AP_LIST=127.0.0.1:2\n"
        'ASUS_AP_SSID_24="Home Net"\n'
        "export ASUS_AP_SSID_5='Home Net 5'\n"
        "UNRELATED_KEY=ignored\n"
    )
    env = _fake_ssh(tmp_path, ["Home Net", "Home Net 5"])
    res = run_script("--env-file", str(env_file), "--dry-run", "--verbose", env=env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "AP 127.0.0.1: 2.4 GHz SSID 'Home Net' broadcasting." in res.stdout
    assert "AP 127.0.0.1: 5 GHz SSID 'Home Net 5' broadcasting." in res.stdout


def test_missing_ssid_fails_check(tmp_path, empty_env):
    """Verify that an SSID not broadcast by the AP fails the check."""
    env = _fake_ssh(tmp_path, ["ExampleNet"])
    res = run_script("--env-file", empty_env, *SETTINGS, "--aps", "127.0.0.1:2",
                     "--dry-run", env=env)
    assert res.returncode == 1
    assert "5 GHz SSID 'ExampleNet5' not broadcasting" in res.stderr


def test_option_overrides_environment_and_env_file(tmp_path):
    """Verify precedence: command-line option, then environment, then env file."""
    env_file = tmp_path / "monitor.env"
    env_file.write_text(
        "ASUS_AP_LIST=192.0.2.1:2\nASUS_AP_SSID_24=FromFile\nASUS_AP_SSID_5=FromFile5\n"
    )
    env = _fake_ssh(tmp_path, ["FromOption", "FromEnv5"])
    env["ASUS_AP_SSID_5"] = "FromEnv5"
    res = run_script("--env-file", str(env_file), "--aps", "127.0.0.1:2",
                     "--ssid-24", "FromOption", "--dry-run", "--verbose", env=env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "SSID 'FromOption' broadcasting" in res.stdout
    assert "SSID 'FromEnv5' broadcasting" in res.stdout


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))
