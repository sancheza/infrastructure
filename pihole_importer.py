#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Populate Pi-hole 6.x local DNS and static DHCP entries from a reservation file.

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
# define your local DNS domain below
DOMAIN = "home.lan"
VERSION = "1.1.0"

MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
HOSTNAME_RE = re.compile(
    r"^(?!-)[a-z0-9-]{1,63}(?<!-)$", re.IGNORECASE
)


# ---------- Validation ----------

def validate_mac(mac):
    """
    Validate a MAC address format.
    
    Args:
        mac (str): MAC address to validate
        
    Returns:
        bool: True if valid MAC address format, False otherwise
        
    Format: XX:XX:XX:XX:XX:XX (hexadecimal, case insensitive)
    """
    return bool(MAC_RE.match(mac))


def validate_ip(ip):
    """
    Validate an IPv4 address.
    
    Args:
        ip (str): IP address to validate
        
    Returns:
        bool: True if valid IPv4 address, False otherwise
    """
    try:
        ipaddress.IPv4Address(ip)
        return True
    except ValueError:
        return False


def validate_hostname(hostname):
    """
    Validate a hostname according to DNS rules.
    
    Args:
        hostname (str): Hostname to validate
        
    Returns:
        bool: True if valid hostname, False otherwise
        
    Rules:
        - 1-63 characters
        - Alphanumeric and hyphens only
        - Cannot start or end with hyphen
        - Case insensitive
    """
    return bool(HOSTNAME_RE.match(hostname))


# ---------- Parsing ----------

def parse_reservations(input_file_path):
    """
    Parse and validate reservations from input file.
    
    Args:
        input_file_path (str): Path to CSV file containing reservations
        
    Returns:
        tuple: (dns_hosts, dhcp_hosts) where:
            - dns_hosts: List of DNS entries in format "IP FQDN HOSTNAME"
            - dhcp_hosts: List of DHCP entries in format "MAC,IP,HOSTNAME"
            
    Raises:
        SystemExit: If validation fails or no valid records found
        
    File format: MAC,IP,Hostname (one per line)
    Skips empty lines and comments (lines starting with #)
    """
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
    Parse a DNS entry string.
    
    Args:
        entry_str (str): DNS entry string in format "IP FQDN HOSTNAME"
        
    Returns:
        tuple: (ip, fqdn, hostname) or (None, None, None) if parsing fails
    """
    parts = entry_str.split()
    if len(parts) >= 3:
        return parts[0], parts[1], parts[2]
    return None, None, None


def parse_dhcp_entry(entry_str):
    """
    Parse a DHCP entry string.
    
    Args:
        entry_str (str): DHCP entry string in format "MAC,IP,HOSTNAME"
        
    Returns:
        tuple: (mac, ip, hostname) or (None, None, None) if parsing fails
    """
    parts = entry_str.split(",")
    if len(parts) >= 3:
        return parts[0], parts[1], parts[2]
    return None, None, None


# ---------- TOML handling ----------

def backup_pihole_toml():
    """
    Create a timestamped backup of the Pi-hole TOML configuration file.
    
    Creates backup in format: /etc/pihole/pihole.toml.YYYYMMDD-HHMMSS
    """
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = f"{PIHOLE_TOML_PATH}.{ts}"
    shutil.copy2(PIHOLE_TOML_PATH, backup_path)
    print(f"🗄  Backup created: {backup_path}")


def audit_entries(input_file_path):
    """
    Audit entries between Pi-hole TOML and input CSV file.
    
    Compares all entries and categorizes them as:
    - Same: All 3 fields match (MAC, IP, Hostname)
    - Different: Same IP but different MAC or Hostname
    - Only in Pi-hole: Entries only in TOML file
    - Only in CSV: Entries only in input file
    
    Args:
        input_file_path (str): Path to CSV file to compare against Pi-hole
        
    Returns:
        None (prints results to console and exits)
        
    Raises:
        SystemExit: Always exits after showing audit results
    """
    # Parse the input file
    try:
        if os.path.exists(input_file_path):
            dns_hosts, dhcp_hosts = parse_reservations(input_file_path)
        else:
            print(f"⚠️  Input file not found: {input_file_path}", file=sys.stderr)
            print("   Only Pi-hole entries will be shown.", file=sys.stderr)
            dns_hosts, dhcp_hosts = [], []
    except SystemExit:
        # If parse_reservations calls sys.exit(), we'll catch it and continue
        # This handles the case where the file exists but has validation errors
        print("⚠️  Input file has validation errors, skipping CSV comparison.", file=sys.stderr)
        dns_hosts, dhcp_hosts = [], []

    # Parse Pi-hole TOML entries
    try:
        with open(PIHOLE_TOML_PATH, "r") as f:
            data = toml.load(f)
    except Exception as e:
        print(f"Error reading TOML: {e}", file=sys.stderr)
        sys.exit(1)

    # Extract and parse Pi-hole entries
    pihole_dns_entries = data.get("dns", {}).get("hosts", [])
    pihole_dhcp_entries = data.get("dhcp", {}).get("hosts", [])

    # Create sets for comparison
    # Format: (mac, ip, hostname)
    pihole_entries = set()
    csv_entries = set()

    # Parse Pi-hole DHCP entries (these have MAC addresses)
    for entry in pihole_dhcp_entries:
        mac, ip, hostname = parse_dhcp_entry(entry)
        if mac and ip and hostname:
            pihole_entries.add((mac.lower(), ip, hostname.lower()))

    # Parse Pi-hole DNS entries that don't have DHCP counterparts
    pihole_dns_map = {}
    for entry in pihole_dns_entries:
        ip, fqdn, hostname = parse_dns_entry(entry)
        if ip and hostname:
            pihole_dns_map[ip] = hostname.lower()

    # Add DNS-only entries to pihole_entries (without MAC)
    for ip, hostname in pihole_dns_map.items():
        # Check if this IP already exists in DHCP entries
        ip_in_dhcp = any(entry[1] == ip for entry in pihole_entries)
        if not ip_in_dhcp:
            # For DNS-only entries, we use empty MAC
            pihole_entries.add(("", ip, hostname))

    # Parse CSV entries
    for i in range(min(len(dns_hosts), len(dhcp_hosts))):
        # Parse DHCP format: MAC,IP,Hostname
        mac, ip, hostname = parse_dhcp_entry(dhcp_hosts[i])
        if mac and ip and hostname:
            csv_entries.add((mac.lower(), ip, hostname.lower()))

    # Categorize entries
    same_entries = pihole_entries & csv_entries
    conflicting_entries = set()
    
    # Find entries with same IP but different details
    csv_by_ip = {entry[1]: entry for entry in csv_entries}
    pihole_by_ip = {entry[1]: entry for entry in pihole_entries}
    
    for ip, csv_entry in csv_by_ip.items():
        if ip in pihole_by_ip:
            pihole_entry = pihole_by_ip[ip]
            if csv_entry != pihole_entry:
                conflicting_entries.add((csv_entry, pihole_entry, "same_ip"))

    # Find entries with same hostname but different IPs or MACs
    csv_by_hostname = {}
    pihole_by_hostname = {}
    
    for entry in csv_entries:
        mac, ip, hostname = entry
        if hostname not in csv_by_hostname:
            csv_by_hostname[hostname] = []
        csv_by_hostname[hostname].append(entry)
        
    for entry in pihole_entries:
        mac, ip, hostname = entry
        if hostname not in pihole_by_hostname:
            pihole_by_hostname[hostname] = []
        pihole_by_hostname[hostname].append(entry)
    
    # Find hostname conflicts
    for hostname, csv_entries_list in csv_by_hostname.items():
        if hostname in pihole_by_hostname:
            pihole_entries_list = pihole_by_hostname[hostname]
            
            # Compare each CSV entry with each Pi-hole entry for the same hostname
            for csv_entry in csv_entries_list:
                for pihole_entry in pihole_entries_list:
                    csv_mac, csv_ip, csv_hostname = csv_entry
                    pihole_mac, pihole_ip, pihole_hostname = pihole_entry
                    
                    # Skip if they're the same entry (already in same_entries)
                    if csv_entry == pihole_entry:
                        continue
                    
                    # Skip if this is already detected as an IP conflict
                    if (csv_entry, pihole_entry, "same_ip") in conflicting_entries:
                        continue
                    
                    # Check for conflicts: same hostname but different IP or MAC
                    if csv_ip != pihole_ip or csv_mac != pihole_mac:
                        conflicting_entries.add((csv_entry, pihole_entry, "same_hostname"))

    only_in_pihole = pihole_entries - csv_entries
    only_in_csv = csv_entries - pihole_entries

    # Remove entries that are in conflicting_entries from the "only" sets
    conflicting_ips = {csv_entry[1] for csv_entry, _, _ in conflicting_entries}
    conflicting_hostnames = {csv_entry[2] for csv_entry, _, _ in conflicting_entries}
    only_in_pihole = {entry for entry in only_in_pihole 
                     if entry[1] not in conflicting_ips and entry[2] not in conflicting_hostnames}
    only_in_csv = {entry for entry in only_in_csv 
                  if entry[1] not in conflicting_ips and entry[2] not in conflicting_hostnames}

    # Print results
    print("🔍 AUDIT RESULTS")
    print("=" * 50)
    
    total_pihole = len(pihole_entries)
    total_csv = len(csv_entries)
    
    print(f"Total entries in Pi-hole: {total_pihole}")
    print(f"Total entries in CSV file: {total_csv}")
    print()
    
    print(f"🟢 SAME ENTRIES (all 3 fields match): {len(same_entries)}")
    if same_entries:
        for mac, ip, hostname in sorted(same_entries, key=lambda x: x[1]):
            print(f"{mac.upper() if mac else '(no MAC)'},{ip},{hostname}")
    
    if conflicting_entries:
        print(f"\n🟡 CONFLICTING ENTRIES: {len(conflicting_entries)}")
        print("CSV:")
        
        # Collect and sort all CSV entries
        csv_entries_list = []
        pihole_entries_list = []
        
        for csv_entry, pihole_entry, _ in sorted(conflicting_entries, key=lambda x: (x[0][2], x[0][1])):
            csv_mac, csv_ip, csv_hostname = csv_entry
            csv_entries_list.append(f"{csv_mac.upper() if csv_mac else '(no MAC)'},{csv_ip},{csv_hostname}")
            
            pihole_mac, pihole_ip, pihole_hostname = pihole_entry
            pihole_entries_list.append(f"{pihole_mac.upper() if pihole_mac else '(no MAC)'},{pihole_ip},{pihole_hostname}")
        
        # Print all CSV entries
        for csv_entry in csv_entries_list:
            print(csv_entry)
        
        print("\nPI-HOLE:")
        # Print all Pi-hole entries
        for pihole_entry in pihole_entries_list:
            print(pihole_entry)
        print()
    
    print(f"\n🔴 ONLY IN PI-HOLE: {len(only_in_pihole)}")
    for mac, ip, hostname in sorted(only_in_pihole, key=lambda x: x[1]):
        print(f"{mac.upper() if mac else '(no MAC)'},{ip},{hostname}")
    
    print(f"\n🔵 ONLY IN CSV FILE: {len(only_in_csv)}")
    for mac, ip, hostname in sorted(only_in_csv, key=lambda x: x[1]):
        print(f"{mac.upper() if mac else '(no MAC)'},{ip},{hostname}")
    
    print("\n" + "=" * 50)
    print("AUDIT COMPLETE")
    
    sys.exit(0)


def export_pihole_entries():
    """
    Export existing Pi-hole DNS and DHCP entries to a CSV file.
    
    Exports entries in format: MAC,IP,Hostname
    Creates file in format: macaddr.YYYYMMDD-HHMMSS.csv
    
    Returns:
        str: Path to the exported file
        
    Raises:
        SystemExit: If there are errors reading the TOML file
    """
    try:
        with open(PIHOLE_TOML_PATH, "r") as f:
            data = toml.load(f)
    except Exception as e:
        print(f"Error reading TOML: {e}", file=sys.stderr)
        sys.exit(1)

    # Extract DNS and DHCP entries
    dns_entries = data.get("dns", {}).get("hosts", [])
    dhcp_entries = data.get("dhcp", {}).get("hosts", [])

    # Create a mapping from IP to hostname for DNS entries
    dns_map = {}
    for entry in dns_entries:
        ip, fqdn, hostname = parse_dns_entry(entry)
        if ip and hostname:
            dns_map[ip] = hostname

    # Build export entries from DHCP (which has MAC addresses)
    export_entries = []
    for entry in dhcp_entries:
        mac, ip, hostname = parse_dhcp_entry(entry)
        if mac and ip:
            # Use hostname from DHCP entry, fall back to DNS hostname if available
            final_hostname = hostname or dns_map.get(ip, "")
            if final_hostname:
                export_entries.append(f"{mac},{ip},{final_hostname}")

    # Add DNS entries that don't have corresponding DHCP entries
    for ip, hostname in dns_map.items():
        # Check if this IP already exists in our export (from DHCP)
        ip_exists = any(entry.startswith(f",{ip},") or entry.split(",")[1] == ip for entry in export_entries)
        if not ip_exists:
            # For DNS-only entries, we don't have a MAC, so skip them
            continue

    if not export_entries:
        print("No entries found to export.", file=sys.stderr)
        return None

    # Generate timestamped filename
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    export_filename = f"macaddr.{ts}.csv"
    
    # Write export file
    try:
        with open(export_filename, "w") as f:
            f.write("# Pi-hole DNS and DHCP entries export\n")
            f.write("# Format: MAC,IP,Hostname\n")
            f.write("# Generated: {}\n\n".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            for entry in sorted(export_entries):
                f.write(f"{entry}\n")
        
        print(f"📤 Exported {len(export_entries)} entries to {export_filename}")
        return export_filename
        
    except Exception as e:
        print(f"Error writing export file: {e}", file=sys.stderr)
        sys.exit(1)


def update_pihole_toml(dns_hosts, dhcp_hosts):
    """
    Update Pi-hole TOML configuration with new DNS and DHCP entries.
    
    Args:
        dns_hosts (list): List of DNS entries in format "IP FQDN HOSTNAME"
        dhcp_hosts (list): List of DHCP entries in format "MAC,IP,HOSTNAME"
        
    Process:
        1. Loads existing configuration
        2. Checks for conflicts with existing entries
        3. Prompts user for conflict resolution
        4. Updates configuration
        5. Creates backup
        6. Writes updated configuration
        7. Restarts Pi-hole FTL service
        
    Raises:
        SystemExit: If there are errors reading/writing TOML or restarting service
    """
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
            "Pi-hole Importer - Import DNS and DHCP reservations from CSV file\n"
            "\n"
            "OVERVIEW:\n"
            "  This script imports static DNS and DHCP reservations into Pi-hole 6.x\n"
            "  from a CSV file. It validates entries, checks for conflicts, and safely\n"
            "  updates your Pi-hole configuration.\n"
            "\n"
            "USAGE:\n"
            "  pihole_importer.py [reservations.csv]\n"
            "\n"
            "  If no input file is specified, defaults to 'macaddr.txt'\n"
            "\n"
            "FEATURES:\n"
            "  • Validates MAC addresses, IP addresses, and hostnames\n"
            "  • Checks for existing entries in pihole.toml\n"
            "  • Detects conflicts (same IP, different details) and prompts user\n"
            "  • Creates automatic backups before making changes\n"
            "  • Idempotent operation (safe to run multiple times)\n"
            "  • Automatically restarts Pi-hole FTL service\n"
            "\n"
            "INPUT FILE FORMAT:\n"
            "  MAC,IP,Hostname\n"
            "\n"
            "EXAMPLE:\n"
            "  00:11:22:33:44:55,192.168.1.100,mydevice\n"
            "  AA:BB:CC:DD:EE:FF,192.168.1.101,anotherdevice\n"
            "\n"
            "DEFAULT FILE:\n"
            "  If no file is specified, the script looks for 'macaddr.txt'\n"
            "\n"
            "EXPORT FUNCTIONALITY:\n"
            "  Use --export flag to export existing Pi-hole entries\n"
            "  Creates macaddr.YYYYMMDD-HHMMSS.csv with current entries\n"
            "\n"
            "AUDIT FUNCTIONALITY:\n"
            "  Use --audit flag to compare Pi-hole entries with input file\n"
            "  Shows detailed comparison: same, different, only in Pi-hole, only in file\n"
            "  Uses default macaddr.txt if no file specified\n"
            "\n"
            "NOTES:\n"
            "  • Strict validation is enforced\n"
            "  • Invalid input will abort the operation\n"
            "  • Requires toml package (pip install toml)"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )

    parser.add_argument(
        "-v", "--version", action="version", version=f"%(prog)s {VERSION}"
    )

    parser.add_argument(
        "input_file",
        type=str,
        nargs="?",
        default="macaddr.txt",
        help="Format: MAC,IP,Hostname (default: macaddr.txt)",
    )

    parser.add_argument(
        "--export",
        action="store_true",
        help="Export existing Pi-hole entries to macaddr.[timestamp].csv file",
    )

    parser.add_argument(
        "--audit",
        action="store_true",
        help="Audit entries between Pi-hole and input file, showing matches and differences",
    )

    args = parser.parse_args()

    if args.export:
        export_pihole_entries()
    elif args.audit:
        audit_entries(args.input_file)
    else:
        dns_hosts, dhcp_hosts = parse_reservations(args.input_file)
        update_pihole_toml(dns_hosts, dhcp_hosts)


if __name__ == "__main__":
    main()
