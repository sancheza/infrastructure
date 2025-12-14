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
import subprocess
from datetime import datetime

import toml  # pip install toml

# --- Configuration ---
PIHOLE_TOML_PATH = "/etc/pihole/pihole.toml"
# define your local DNS domain here
DOMAIN = "home.lan"
VERSION = "1.0.1"

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


# ---------- Helpers ----------

def parse_dns_entry(entry_str):
    """
    Expects format: "IP FQDN HOSTNAME"
    Returns: (ip, fqdn, hostname)
    """
    parts = entry_str.split()
    if len(parts) >= 3:
        return parts[0], parts[1], parts[2]
    return None, None, None


def parse_dhcp_entry(entry_str):
    """
    Expects format: "MAC,IP,HOSTNAME"
    Returns: (mac, ip, hostname)
    """
    parts = entry_str.split(",")
    if len(parts) >= 3:
        return parts[0], parts[1], parts[2]
    return None, None, None


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

    # --- DNS Processing ---
    data.setdefault("dns", {})
    data["dns"].setdefault("hosts", [])
    
    # Map IP -> full_entry_string
    existing_dns_map = {}
    for entry in data["dns"]["hosts"]:
        ip, _, _ = parse_dns_entry(entry)
        if ip:
            existing_dns_map[ip] = entry

    dns_to_add = {}
    dns_conflicts = []

    for new_entry in dns_hosts:
        new_ip, _, new_host = parse_dns_entry(new_entry)
        if not new_ip:
            continue

        if new_ip in existing_dns_map:
            old_entry = existing_dns_map[new_ip]
            if old_entry != new_entry:
                dns_conflicts.append((new_ip, "DNS", old_entry, new_entry))
            # If identical, do nothing (idempotent)
        else:
            dns_to_add[new_ip] = new_entry

    # --- DHCP Processing ---
    data.setdefault("dhcp", {})
    data["dhcp"].setdefault("hosts", [])

    # Map IP -> full_entry_string
    existing_dhcp_map = {}
    for entry in data["dhcp"]["hosts"]:
        _, ip, _ = parse_dhcp_entry(entry)
        if ip:
            existing_dhcp_map[ip] = entry

    dhcp_to_add = {}
    dhcp_conflicts = []

    for new_entry in dhcp_hosts:
        _, new_ip, _ = parse_dhcp_entry(new_entry)
        if not new_ip:
            continue

        if new_ip in existing_dhcp_map:
            old_entry = existing_dhcp_map[new_ip]
            if old_entry != new_entry:
                dhcp_conflicts.append((new_ip, "DHCP", old_entry, new_entry))
        else:
            dhcp_to_add[new_ip] = new_entry

    # --- Handle Conflicts ---
    all_conflicts = dns_conflicts + dhcp_conflicts
    if all_conflicts:
        print("\n⚠️  Found conflicting entries for existing IP addresses:")
        for ip, kind, old, new in all_conflicts:
            print(f"   [{kind}] IP: {ip}")
            print(f"      Current: {old}")
            print(f"      New:     {new}")
        
        response = input("\nDo you want to overwrite these entries with the new values? [y/N] ").strip().lower()
        if response != 'y':
            print("Aborting. No changes made.")
            sys.exit(0)
        
        # User confirmed, add conflicts to the "to_add" maps (overwriting logic below will handle it)
        for ip, kind, _, new_entry in all_conflicts:
            if kind == "DNS":
                dns_to_add[ip] = new_entry
            elif kind == "DHCP":
                dhcp_to_add[ip] = new_entry

    # --- Apply Updates ---
    
    # 1. Update Maps with new items (this handles both new and overwritten)
    for ip, entry in dns_to_add.items():
        existing_dns_map[ip] = entry
        
    for ip, entry in dhcp_to_add.items():
        existing_dhcp_map[ip] = entry

    # 2. Convert back to lists
    # We want to preserve order somewhat, or just dump values. 
    # To keep it loosely stable, we could carry over the original list, 
    # but since we might be overwriting, rebuilding from the map is safer to ensure uniqueness by IP.
    # However, to be nice to the file structure, maybe we just append new ones and find/replace existing logic? 
    # Rebuilding from map is easiest to guarantee idempotency and no duplicates.
    
    data["dns"]["hosts"] = list(existing_dns_map.values())
    data["dhcp"]["hosts"] = list(existing_dhcp_map.values())

    backup_pihole_toml()

    try:
        with open(PIHOLE_TOML_PATH, "w") as f:
            toml.dump(data, f)
    except Exception as e:
        print(f"Error writing TOML: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"✅ Updated {PIHOLE_TOML_PATH}")
    print(f"   DNS entries count:  {len(data['dns']['hosts'])}")
    print(f"   DHCP entries count: {len(data['dhcp']['hosts'])}")
    print("   Restarting Pi-hole FTL...")

    print("   Restarting Pi-hole FTL...")

    subprocess.run(["systemctl", "restart", "pihole-FTL.service"], check=False)
    
    # Check status
    try:
        result = subprocess.run(
            ["systemctl", "status", "pihole-FTL", "--no-pager"], 
            capture_output=True, 
            text=True
        )
        if "Active: active (running)" in result.stdout:
            print("✅ Pi-hole FTL is active and running.")
        else:
            print("⚠️  Warning: Pi-hole FTL did not report 'active (running)'. Check 'systemctl status pihole-FTL'.")
    except FileNotFoundError:
        print("⚠️  Could not run systemctl. Is this a systemd system?")


# ---------- Main ----------

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Populate Pi-hole v6 DNS and DHCP static reservations.\n"
            "Features:\n"
            "  - Validates MAC, IP, and Hostnames.\n"
            "  - Checks for existing entries in pihole.toml.\n"
            "  - Detects conflicts (same IP, different details) and prompts user.\n"
            "  - Idempotent: Replaces differing entries if confirmed, ignores identicals."
        ),
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
