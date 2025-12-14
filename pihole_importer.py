#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Populate Pi-hole v6 local DNS and static DHCP entries from a reservation file.

Source format (required):
MAC,IP,Hostname

Strict validation is enforced. Invalid input aborts the run.
"""

import argparse
import sys
import os
import shutil
import re
import ipaddress
from datetime import datetime

import toml  # pip install toml

# --- Configuration ---
PIHOLE_TOML_PATH = "/etc/pihole/pihole.toml"
# define your local DNS domain here
DOMAIN = "home.lan"
VERSION = "1.0.0"

MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
HOSTNAME_RE = re.compile(
    r"^(?!-)[a-z0-9-]{1,63}(?<!-)$", re.IGNORECASE
)


# ---------- Validation ----------

def validate_mac(mac):
    return bool(MAC_RE.match(mac))


def validate_ip(ip):
    try:
        ipaddress.IPv4Address(ip)
        return True
    except ValueError:
        return False


def validate_hostname(hostname):
    return bool(HOSTNAME_RE.match(hostname))


# ---------- Parsing ----------

def parse_reservations(input_file_path):
    dns_hosts = []
    dhcp_hosts = []
    errors = []

    if not os.path.exists(input_file_path):
        print(f"Error: Input file not found: {input_file_path}", file=sys.stderr)
        sys.exit(1)

    with open(input_file_path, "r") as f:
        for line_num, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue

            parts = [p.strip() for p in line.split(",")]
            if len(parts) != 3:
                errors.append((line_num, "Expected 3 fields", line))
                continue

            mac, ip, hostname = parts
            hostname = hostname.lower()

            if not validate_mac(mac):
                errors.append((line_num, f"Invalid MAC address: {mac}", line))
            if not validate_ip(ip):
                errors.append((line_num, f"Invalid IPv4 address: {ip}", line))
            if not validate_hostname(hostname):
                errors.append(
                    (line_num, f"Invalid hostname (DNS rules): {hostname}", line)
                )

            if any(e[0] == line_num for e in errors):
                continue

            fqdn = f"{hostname}.{DOMAIN}"
            dns_hosts.append(f"{ip} {fqdn} {hostname}")
            dhcp_hosts.append(f"{mac},{ip},{hostname}")

    if errors:
        print("❌ Validation failed. No changes were made.\n", file=sys.stderr)
        for line_num, reason, content in errors:
            print(
                f"Line {line_num}: {reason}\n  → {content}",
                file=sys.stderr,
            )
        sys.exit(1)

    if not dns_hosts:
        print("No valid records found. Nothing to do.", file=sys.stderr)
        sys.exit(0)

    return dns_hosts, dhcp_hosts


# ---------- TOML handling ----------

def backup_pihole_toml():
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = f"{PIHOLE_TOML_PATH}.{ts}"
    shutil.copy2(PIHOLE_TOML_PATH, backup_path)
    print(f"🗄  Backup created: {backup_path}")


def update_pihole_toml(dns_hosts, dhcp_hosts):
    try:
        with open(PIHOLE_TOML_PATH, "r") as f:
            data = toml.load(f)
    except Exception as e:
        print(f"Error reading TOML: {e}", file=sys.stderr)
        sys.exit(1)

    # DNS
    data.setdefault("dns", {})
    data["dns"].setdefault("hosts", [])
    existing_dns = set(data["dns"]["hosts"])

    dns_added = 0
    for entry in dns_hosts:
        if entry not in existing_dns:
            data["dns"]["hosts"].append(entry)
            dns_added += 1

    # DHCP
    data.setdefault("dhcp", {})
    data["dhcp"].setdefault("hosts", [])
    existing_dhcp = set(data["dhcp"]["hosts"])

    dhcp_added = 0
    for entry in dhcp_hosts:
        if entry not in existing_dhcp:
            data["dhcp"]["hosts"].append(entry)
            dhcp_added += 1

    backup_pihole_toml()

    try:
        with open(PIHOLE_TOML_PATH, "w") as f:
            toml.dump(data, f)
    except Exception as e:
        print(f"Error writing TOML: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"✅ Updated {PIHOLE_TOML_PATH}")
    print(f"   DNS:  appended {dns_added}")
    print(f"   DHCP: appended {dhcp_added}")
    print("   Restarting Pi-hole FTL...")

    os.system("systemctl restart pihole-FTL.service")


# ---------- Main ----------

def main():
    parser = argparse.ArgumentParser(
        description="Populate Pi-hole v6 DNS and DHCP static reservations",
        formatter_class=argparse.RawTextHelpFormatter,
    )

    parser.add_argument(
        "-v", "--version", action="version", version=f"%(prog)s {VERSION}"
    )

    parser.add_argument(
        "input_file",
        type=str,
        help="Format: MAC,IP,Hostname",
    )

    args = parser.parse_args()

    dns_hosts, dhcp_hosts = parse_reservations(args.input_file)
    update_pihole_toml(dns_hosts, dhcp_hosts)


if __name__ == "__main__":
    main()
