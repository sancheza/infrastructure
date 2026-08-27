#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Populate Pi-hole 6.x local DNS and static DHCP entries from a reservation file.

Source format (required):
MAC,IP,Hostname

Strict validation is enforced. Invalid input aborts the run.

Existing DNS and DHCP entries in pihole.toml that aren't present in the
reservation file are left untouched, including DNS records added through
the Pi-hole web UI (Settings > Local DNS Records), unless they conflict
with an entry from the reservation file. A single IP may have multiple
dns.hosts entries (e.g. several hostname aliases pointing at a reverse
proxy); all of them are preserved, not just one per IP. Any entry a run
removes is logged to stderr.
"""

import argparse
import sys
import os
import shutil
import re
import ipaddress
import subprocess
from datetime import datetime

# Check for toml library availability
try:
    import toml
except ImportError:
    print("Error: 'toml' library not found.", file=sys.stderr)
    print("On Debian/Raspberry Pi OS, install with:", file=sys.stderr)
    print("  sudo apt install python3-toml", file=sys.stderr)
    print("Or install via pip:", file=sys.stderr)
    print("  pip install toml", file=sys.stderr)
    sys.exit(1)

# --- Configuration ---
PIHOLE_TOML_PATH = "/etc/pihole/pihole.toml"
# define your local DNS domain below
DOMAIN = "home.lan"
VERSION = "1.3.0"

# Entries already warned about this run, so a raw TOML line parsed more than
# once internally (e.g. once while building a lookup map, again inside
# get_pihole_entries_as_set()) doesn't print the same warning twice.
_WARNED_UNPARSEABLE = set()

MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
# Regex for simple hostname (alphanumeric and hyphens)
SIMPLE_HOSTNAME_RE = re.compile(
    r"^(?!-)[a-z0-9-]{1,63}(?<!-)$", re.IGNORECASE
)

# Regex for fully qualified domain name (allows dots)
FQDN_RE = re.compile(
    r"^(?!-)([a-z0-9-]{1,63}(?<!-)\.)+[a-z0-9-]{1,63}(?<!-)$", re.IGNORECASE
)


# ---------- Validation ----------

def validate_mac(mac):
    """
    Validate a MAC address format with flexible input handling.
    
    Args:
        mac (str): MAC address to validate
        
    Returns:
        bool: True if valid MAC address format, False otherwise
        
    Format: Accepts various formats (colons, hyphens, dots, or no separators)
    Examples:
        - 00:11:22:33:44:55 (colons)
        - 00-11-22-33-44-55 (hyphens)
        - 0011.2233.4455 (dots)
        - 001122334455 (no separators)
        
    All formats are normalized to colon-separated format internally.
    """
    # Normalize: Remove all common separators (:, -, .) and convert to uppercase
    normalized_mac = mac.replace(":", "").replace("-", "").replace(".", "").upper()
    
    # Validate: Must be exactly 12 hexadecimal characters
    if len(normalized_mac) != 12 or not all(c in "0123456789ABCDEF" for c in normalized_mac):
        return False
        
    # If validation passes, the MAC is valid (format will be standardized later)
    return True


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
        - Simple hostname: 1-63 characters, alphanumeric and hyphens only, cannot start/end with hyphen
        - FQDN: Multiple labels separated by dots, each label follows simple hostname rules
        - Case insensitive
    """
    # Allow either simple hostname or fully qualified domain name
    return bool(SIMPLE_HOSTNAME_RE.match(hostname) or FQDN_RE.match(hostname))


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

            # Normalize MAC address to colon-separated format
            if validate_mac(mac):
                # Remove all separators and convert to uppercase
                clean_mac = mac.replace(":", "").replace("-", "").replace(".", "").upper()
                # Reformat with colons
                mac = ":".join([clean_mac[i:i+2] for i in range(0, 12, 2)])
            else:
                errors.append((line_num, f"Invalid MAC address: {mac}", line))
            if not validate_ip(ip):
                errors.append((line_num, f"Invalid IPv4 address: {ip}", line))
            if not validate_hostname(hostname):
                errors.append(
                    (line_num, f"Invalid hostname (DNS rules): {hostname}", line)
                )

            if any(e[0] == line_num for e in errors):
                continue

            # Create FQDN, but don't double-append the domain if hostname is already fully qualified
            if hostname.endswith(f".{DOMAIN}"):
                fqdn = hostname
            else:
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
        entry_str (str): DNS entry string in Pi-hole's hosts-file form:
            "IP HOSTNAME [HOSTNAME ...]". Entries created by this script use
            three fields (IP, FQDN, short hostname); entries added through
            the Pi-hole web UI ("Settings > Local DNS Records") have only
            two fields (IP, single hostname).

    Returns:
        tuple: (ip, fqdn, hostname) or (None, None, None) if parsing fails.
            For a two-field entry, fqdn and hostname are the same value.
            hostname is always the last field, normalized to lowercase.
    """
    parts = entry_str.split()
    if len(parts) >= 2:
        return parts[0], parts[1], parts[-1].lower()  # Normalize hostname to lowercase
    key = ("dns", entry_str)
    if key not in _WARNED_UNPARSEABLE:
        _WARNED_UNPARSEABLE.add(key)
        print(
            f"⚠️  Skipping unparseable dns.hosts entry: {entry_str!r} "
            f'(expected "IP HOSTNAME [HOSTNAME ...]")',
            file=sys.stderr,
        )
    return None, None, None


def normalize_dns_hosts_entry(entry_str):
    """
    Lowercase the hostname fields of a dns.hosts entry while preserving the
    IP and however many hostname aliases it has.

    Args:
        entry_str (str): DNS entry string in Pi-hole's hosts-file form
            ("IP HOSTNAME [HOSTNAME ...]")

    Returns:
        str: The entry with all hostname fields lowercased, or the original
            string unchanged if it doesn't have at least an IP and a hostname.
    """
    parts = entry_str.split()
    if len(parts) < 2:
        return entry_str
    return parts[0] + " " + " ".join(p.lower() for p in parts[1:])


def parse_dhcp_entry(entry_str):
    """
    Parse a DHCP entry string.
    
    Args:
        entry_str (str): DHCP entry string in format "MAC,IP,HOSTNAME"
        
    Returns:
        tuple: (mac, ip, hostname) or (None, None, None) if parsing fails
        
    Note:
        MAC addresses are preserved in their original case for display/storage,
        but normalized to uppercase for comparison purposes in other functions.
    """
    parts = entry_str.split(",")
    if len(parts) >= 3:
        return parts[0], parts[1], parts[2].lower()  # Preserve MAC case, normalize hostname to lowercase
    key = ("dhcp", entry_str)
    if key not in _WARNED_UNPARSEABLE:
        _WARNED_UNPARSEABLE.add(key)
        print(
            f"⚠️  Skipping unparseable dhcp.hosts entry: {entry_str!r} "
            f'(expected "MAC,IP,HOSTNAME"; Pi-hole also allows leases with no '
            f"hostname or with extra fields like a lease time, which this "
            f"script does not support)",
            file=sys.stderr,
        )
    return None, None, None


def dns_entry_identity(entry_str):
    """Identity of a dns.hosts entry for drop/diff comparisons: (ip, hostname)."""
    ip, _, hostname = parse_dns_entry(entry_str)
    return (ip, hostname)


def dhcp_entry_identity(entry_str):
    """Identity of a dhcp.hosts entry for drop/diff comparisons: (mac, ip, hostname)."""
    mac, ip, hostname = parse_dhcp_entry(entry_str)
    return (mac.upper() if mac else mac, ip, hostname)


def warn_dropped_entries(kind, identity_fn, before, after):
    """
    Print a warning for every entry present in `before` but not `after`.

    Entries are compared by identity_fn rather than raw string equality, so
    a harmless normalization (e.g. lowercasing) isn't mistaken for a drop.
    Intended to be called right before a rewrite is committed, so any
    dns.hosts/dhcp.hosts entry the run is about to remove -- whether from
    an approved conflict resolution or an unexpected cause -- is visible in
    the log instead of silently disappearing.
    """
    after_identities = {identity_fn(e) for e in after}
    for entry in before:
        if identity_fn(entry) not in after_identities:
            print(f"🗑️  {kind} entry removed by this run: {entry!r}", file=sys.stderr)


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


def detect_conflicts(existing_entries, new_entries):
    """
    Detect conflicts between existing and new entries.
    
    Args:
        existing_entries (set): Set of existing entries as tuples (mac, ip, hostname)
        new_entries (set): Set of new entries as tuples (mac, ip, hostname)
        
    Returns:
        dict: Dictionary containing:
            - 'same': Entries that are identical in both sets
            - 'ip_conflicts': IP conflicts (same IP, different details)
            - 'mac_conflicts': MAC conflicts (same MAC, different IP)
            - 'only_in_existing': Entries only in existing set
            - 'only_in_new': Entries only in new set
    """
    # Categorize entries
    same_entries = existing_entries & new_entries
    
    # Find IP conflicts
    ip_conflicts = set()
    existing_by_ip = {entry[1]: entry for entry in existing_entries}
    new_by_ip = {entry[1]: entry for entry in new_entries}
    
    for ip, new_entry in new_by_ip.items():
        if ip in existing_by_ip:
            existing_entry = existing_by_ip[ip]
            if new_entry != existing_entry:
                ip_conflicts.add((new_entry, existing_entry))
    
    # Find MAC conflicts
    mac_conflicts = set()
    existing_by_mac = {entry[0].lower(): entry for entry in existing_entries if entry[0]}
    new_by_mac = {entry[0].lower(): entry for entry in new_entries if entry[0]}
    
    for mac, new_entry in new_by_mac.items():
        if mac in existing_by_mac:
            existing_entry = existing_by_mac[mac]
            if new_entry[1] != existing_entry[1]:  # Different IP with same MAC
                mac_conflicts.add((new_entry, existing_entry))
    
    # Find hostname conflicts (same hostname, different IP or MAC)
    hostname_conflicts = set()
    existing_by_hostname = {}
    new_by_hostname = {}
    
    for entry in existing_entries:
        mac, ip, hostname = entry
        if hostname not in existing_by_hostname:
            existing_by_hostname[hostname] = []
        existing_by_hostname[hostname].append(entry)
        
    for entry in new_entries:
        mac, ip, hostname = entry
        if hostname not in new_by_hostname:
            new_by_hostname[hostname] = []
        new_by_hostname[hostname].append(entry)
    
    # Find conflicts where same hostname exists in both but with different details
    for hostname, new_entries_list in new_by_hostname.items():
        if hostname in existing_by_hostname:
            existing_entries_list = existing_by_hostname[hostname]
            
            # Compare each new entry with each existing entry for the same hostname
            for new_entry in new_entries_list:
                for existing_entry in existing_entries_list:
                    new_mac, new_ip, new_hostname = new_entry
                    existing_mac, existing_ip, existing_hostname = existing_entry
                    
                    # Skip if they're identical (already in same_entries)
                    if new_entry == existing_entry:
                        continue
                    
                    # Skip if this is already detected as an IP or MAC conflict
                    if (new_entry, existing_entry) in ip_conflicts or (new_entry, existing_entry) in mac_conflicts:
                        continue
                    
                    # Check for conflicts: same hostname but different IP or MAC
                    # For hostname conflicts, we want cases where the hostname is the same but the IP/MAC differs
                    if new_ip != existing_ip or new_mac != existing_mac:
                        # But skip if this is already detected as an IP or MAC conflict to avoid duplicates
                        if new_ip == existing_ip:
                            # Same IP, different MAC - this is a MAC conflict, skip hostname conflict
                            continue
                        if new_mac == existing_mac:
                            # Same MAC, different IP - this is a MAC conflict, skip hostname conflict
                            continue
                        # True hostname conflict: same hostname, different IP and MAC
                        hostname_conflicts.add((new_entry, existing_entry))
    
    # Remove conflicts from "only" sets
    conflict_ips = {new_entry[1] for new_entry, _ in ip_conflicts}
    conflict_macs = {new_entry[0] for new_entry, _ in mac_conflicts if new_entry[0]}
    conflict_hostnames = {new_entry[2] for new_entry, _ in hostname_conflicts}
    
    only_in_existing = {
        entry for entry in existing_entries 
        if entry not in same_entries 
        and entry[1] not in conflict_ips 
        and (not entry[0] or entry[0] not in conflict_macs)
        and entry[2] not in conflict_hostnames
    }
    
    only_in_new = {
        entry for entry in new_entries 
        if entry not in same_entries 
        and entry[1] not in conflict_ips 
        and (not entry[0] or entry[0] not in conflict_macs)
        and entry[2] not in conflict_hostnames
    }
    
    return {
        'same': same_entries,
        'ip_conflicts': ip_conflicts,
        'mac_conflicts': mac_conflicts,
        'hostname_conflicts': hostname_conflicts,
        'only_in_existing': only_in_existing,
        'only_in_new': only_in_new
    }


def get_pihole_entries_as_set(data):
    """
    Convert Pi-hole TOML data to a set of (mac, ip, hostname) tuples.
    
    Args:
        data (dict): Parsed TOML data from Pi-hole
        
    Returns:
        set: Set of entries as (mac, ip, hostname) tuples
        
    Note:
        MAC addresses are normalized to uppercase for consistent comparison and storage
        Hostnames are normalized to lowercase for consistent comparison
        The parse_dhcp_entry function preserves MAC case, parse_dns_entry normalizes hostnames
    """
    entries = set()
    dhcp_ips = set()

    # Parse DHCP entries (these have MAC addresses)
    for entry in data.get("dhcp", {}).get("hosts", []):
        mac, ip, hostname = parse_dhcp_entry(entry)
        if mac and ip and hostname:
            # Normalize MAC to uppercase for consistent comparison
            entries.add((mac.upper(), ip, hostname.lower()))
            dhcp_ips.add(ip)

    # Add DNS entries that don't have a DHCP counterpart for their IP. Each
    # dns.hosts line is added independently rather than collapsed into a
    # dict keyed by IP, since Pi-hole allows multiple hostname aliases to
    # share one IP (e.g. several names pointing at a reverse proxy).
    for entry in data.get("dns", {}).get("hosts", []):
        ip, fqdn, hostname = parse_dns_entry(entry)
        if ip and hostname and ip not in dhcp_ips:
            entries.add(("", ip, hostname))  # hostname already normalized

    return entries


def get_csv_entries_as_set(dhcp_hosts):
    """
    Convert CSV DHCP entries to a set of (mac, ip, hostname) tuples.
    
    Args:
        dhcp_hosts (list): List of DHCP entries from CSV
        
    Returns:
        set: Set of entries as (mac, ip, hostname) tuples
        
    Note:
        MAC addresses are normalized to uppercase for consistent comparison and storage
        Hostnames are normalized to lowercase for consistent comparison
        The parse_dhcp_entry function preserves MAC case, parse_dns_entry normalizes hostnames
    """
    entries = set()
    for entry in dhcp_hosts:
        mac, ip, hostname = parse_dhcp_entry(entry)
        if mac and ip and hostname:
            # Normalize MAC to uppercase and hostname to lowercase for consistent comparison
            entries.add((mac.upper(), ip, hostname.lower()))
    return entries


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
            pihole_entries.add((mac.upper(), ip, hostname.lower()))

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
            csv_entries.add((mac.upper(), ip, hostname.lower()))

    # Use common conflict detection
    conflicts = detect_conflicts(pihole_entries, csv_entries)

    # Print results
    print("🔍 AUDIT RESULTS")
    print("=" * 50)
    
    total_pihole = len(pihole_entries)
    total_csv = len(csv_entries)
    
    print(f"Total entries in Pi-hole: {total_pihole}")
    print(f"Total entries in CSV file: {total_csv}")
    print()
    
    print(f"🟢 SAME ENTRIES (all 3 fields match): {len(conflicts['same'])}")
    if conflicts['same']:
        for mac, ip, hostname in sorted(conflicts['same'], key=lambda x: x[1]):
            print(f"{mac.upper() if mac else '(no MAC)'},{ip},{hostname}")
    
    all_conflicts = conflicts['ip_conflicts'] | conflicts['mac_conflicts'] | conflicts['hostname_conflicts']
    if all_conflicts:
        print(f"\n🟡 CONFLICTING ENTRIES: {len(all_conflicts)}")
        print("CSV:")
        
        # Collect and sort all CSV entries
        csv_entries_list = []
        pihole_entries_list = []
        
        for csv_entry, pihole_entry in sorted(all_conflicts, key=lambda x: (x[0][2], x[0][1])):
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
            # Replace "(no MAC)" with "[Missing MAC address]" for consistency
            formatted_entry = pihole_entry.replace("(no MAC)", "[Missing MAC address]")
            print(formatted_entry)
        print()
    
    print(f"\n🔴 ONLY IN PI-HOLE: {len(conflicts['only_in_existing'])}")
    for mac, ip, hostname in sorted(conflicts['only_in_existing'], key=lambda x: x[1]):
        formatted_mac = "[Missing MAC address]" if not mac else mac.upper()
        print(f"{formatted_mac},{ip},{hostname}")
    
    print(f"\n🔵 ONLY IN CSV FILE: {len(conflicts['only_in_new'])}")
    for mac, ip, hostname in sorted(conflicts['only_in_new'], key=lambda x: x[1]):
        formatted_mac = "[Missing MAC address]" if not mac else mac.upper()
        print(f"{formatted_mac},{ip},{hostname}")
    
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

    # Every dns.hosts entry, kept as a list rather than a dict keyed by IP:
    # Pi-hole allows multiple hostname aliases to share one IP, and a dict
    # keyed by IP would silently export only the last one.
    dns_list = []
    for entry in dns_entries:
        ip, fqdn, hostname = parse_dns_entry(entry)
        if ip and hostname:
            dns_list.append((ip, hostname))

    # Build export entries from DHCP (which has MAC addresses)
    export_entries = []
    dhcp_ips = set()
    for entry in dhcp_entries:
        mac, ip, hostname = parse_dhcp_entry(entry)
        if mac and ip:
            dhcp_ips.add(ip)
            # Use hostname from DHCP entry, fall back to a matching DNS hostname
            final_hostname = hostname or next((h for i, h in dns_list if i == ip), "")
            if final_hostname:
                export_entries.append(f"{mac},{ip},{final_hostname}")

    # Add DNS entries/aliases that don't have a corresponding DHCP entry
    for ip, hostname in dns_list:
        if ip not in dhcp_ips:
            # For DNS-only entries, we don't have a MAC, so use empty MAC field
            export_entries.append(f",{ip},{hostname}")

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


def validate_toml_integrity(data):
    """
    Sanity-check an in-memory Pi-hole config before it is written to disk.

    Catches the failure modes that would otherwise only surface when
    pihole-FTL tries to load the file (e.g. duplicate MACs/IPs producing
    conflicting DHCP reservations), plus a round-trip serialize/parse check
    so a structurally broken TOML document is caught before it overwrites
    the live config.

    Raises:
        ValueError: describing the first problem found.
    """
    dhcp_hosts = data.get("dhcp", {}).get("hosts", [])
    dns_hosts = data.get("dns", {}).get("hosts", [])

    seen_macs = set()
    seen_dhcp_ips = set()
    for entry in dhcp_hosts:
        mac, ip, hostname = parse_dhcp_entry(entry)
        if not mac or not ip or not hostname:
            raise ValueError(f"Malformed dhcp.hosts entry: {entry!r}")
        if mac.upper() in seen_macs:
            raise ValueError(f"Duplicate MAC address in dhcp.hosts: {mac}")
        if ip in seen_dhcp_ips:
            raise ValueError(f"Duplicate IP address in dhcp.hosts: {ip}")
        seen_macs.add(mac.upper())
        seen_dhcp_ips.add(ip)

    # A single IP may legitimately appear on multiple dns.hosts lines (e.g.
    # several hostname aliases pointing at one reverse proxy), so only an
    # exact (ip, hostname) repeat -- the same line in substance twice -- is
    # rejected here.
    seen_dns_entries = set()
    for entry in dns_hosts:
        ip, fqdn, hostname = parse_dns_entry(entry)
        if not ip or not fqdn or not hostname:
            raise ValueError(f"Malformed dns.hosts entry: {entry!r}")
        key = (ip, hostname)
        if key in seen_dns_entries:
            raise ValueError(f"Duplicate dns.hosts entry for {ip} {hostname}")
        seen_dns_entries.add(key)

    # Round-trip through the TOML serializer to make sure the result actually parses.
    try:
        toml.loads(toml.dumps(data))
    except Exception as e:
        raise ValueError(f"Generated TOML failed to round-trip parse: {e}")


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

    Existing dns.hosts / dhcp.hosts entries not present in dns_hosts /
    dhcp_hosts are preserved as-is unless they conflict with one of the
    new entries (same IP, MAC, or hostname). This includes DNS records
    added through the Pi-hole web UI, which are two-field entries
    ("IP HOSTNAME") rather than this script's three-field
    ("IP FQDN HOSTNAME") format, and DNS entries that share an IP with
    another entry (e.g. multiple hostname aliases pointing at one reverse
    proxy) -- each is tracked and preserved individually rather than
    collapsed down to one entry per IP. Any entry that can't be parsed at
    all is skipped with a warning rather than silently dropped, and any
    entry actually removed by the run is logged to stderr.

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

    # Snapshot of every existing dns.hosts line, kept as a list rather than
    # a dict keyed by IP: Pi-hole allows multiple independent dns.hosts
    # lines to share one IP (e.g. several hostname aliases pointing at a
    # reverse proxy), and keying by IP would silently drop every entry but
    # the last for any IP that has more than one.
    original_dns_hosts = list(data["dns"]["hosts"])
    existing_dns_entries = list(original_dns_hosts)

    # IP -> one existing entry for that IP, used only to show the "current"
    # DNS record in the conflict prompts below; not used to decide what
    # survives the rewrite.
    existing_dns_map = {}
    for entry in existing_dns_entries:
        ip, _, _ = parse_dns_entry(entry)
        if ip and ip not in existing_dns_map:
            existing_dns_map[ip] = entry

    # --- DHCP Processing ---
    data.setdefault("dhcp", {})
    data["dhcp"].setdefault("hosts", [])
    original_dhcp_hosts = list(data["dhcp"]["hosts"])

    # Map MAC -> full_entry_string for existing DHCP entries (MAC is the true key for DHCP)
    # Keys are normalized to uppercase so lookups/removals agree with the CSV-derived
    # entries, which are always uppercase (see parse_reservations()).
    existing_dhcp_map = {}
    for entry in data["dhcp"]["hosts"]:
        mac, ip, _ = parse_dhcp_entry(entry)
        if mac:
            existing_dhcp_map[mac.upper()] = entry

    # --- Use common conflict detection ---
    pihole_entries = get_pihole_entries_as_set(data)
    csv_entries = get_csv_entries_as_set(dhcp_hosts)
    
    conflicts = detect_conflicts(pihole_entries, csv_entries)
    
    # Convert conflicts back to the format expected by the rest of the function
    dns_conflicts = []
    dhcp_conflicts = []
    
    # Process all conflicts (IP, MAC, and hostname)
    for new_entry, existing_entry in conflicts['ip_conflicts'] | conflicts['mac_conflicts'] | conflicts['hostname_conflicts']:
        new_mac, new_ip, new_hostname = new_entry
        existing_mac, existing_ip, existing_hostname = existing_entry
        
        # Create new entry string
        new_entry_str = f"{new_mac},{new_ip},{new_hostname}"
        
        # Determine conflict type
        if (new_entry, existing_entry) in conflicts['ip_conflicts']:
            conflict_type = "ip_conflict"
        elif (new_entry, existing_entry) in conflicts['mac_conflicts']:
            conflict_type = "mac_conflict"
        else:
            conflict_type = "hostname_conflict"
        
        # Find the existing entry string in the TOML data (look for matching MAC in DHCP entries)
        # existing_mac is normalized uppercase; compare case-insensitively since
        # Pi-hole often stores MACs lowercase on disk.
        existing_entry_str = None
        for entry in data.get("dhcp", {}).get("hosts", []):
            e_mac, e_ip, e_hostname = parse_dhcp_entry(entry)
            if e_mac and e_mac.upper() == existing_mac:
                existing_entry_str = entry
                break
        
        # If no DHCP entry found, look for DNS-only entries
        if not existing_entry_str:
            for entry in data.get("dns", {}).get("hosts", []):
                e_ip, e_fqdn, e_hostname = parse_dns_entry(entry)
                if e_ip == existing_ip and e_hostname == existing_hostname:
                    # Create a synthetic DHCP entry for display purposes
                    existing_entry_str = f"{existing_mac},{existing_ip},{existing_hostname}"
                    break
        
        if existing_entry_str:
            dhcp_conflicts.append((new_ip, "DHCP", existing_entry_str, new_entry_str, conflict_type))
    
    # Prepare entries to add (non-conflicting entries from CSV)
    dns_to_add = {}
    dhcp_to_add = {}
    
    for entry in conflicts['only_in_new']:
        mac, ip, hostname = entry
        if mac:  # Only add entries with MAC addresses (DHCP entries)
            dns_entry = f"{ip} {hostname}.{DOMAIN} {hostname}"
            dhcp_entry = f"{mac},{ip},{hostname}"
            dns_to_add[ip] = dns_entry
            dhcp_to_add[ip] = dhcp_entry

    # --- Handle Conflicts ---
    all_conflicts = dns_conflicts + dhcp_conflicts
    if all_conflicts:
        print("\n⚠️  Found conflicting entries:")
        
        ip_conflicts = []
        mac_conflicts = []
        
        # Separate conflicts by type
        for conflict in all_conflicts:
            if len(conflict) == 5 and conflict[4] == "mac_conflict":
                mac_conflicts.append(conflict)
            else:
                ip_conflicts.append(conflict)
        
        # Show IP conflicts with DNS transparency
        if ip_conflicts:
            print("\nIP Address Conflicts (Current vs. Proposed):")
            for conflict in ip_conflicts:
                if len(conflict) == 4:
                    ip, kind, old, new = conflict
                    conflict_type = "ip_conflict"
                else:
                    ip, kind, old, new, conflict_type = conflict
                
                # Pull current DNS mapping if it exists
                current_dns = existing_dns_map.get(ip, "[No existing DNS]")
                new_mac, new_ip, new_hostname = parse_dhcp_entry(new)
                new_dns = f"{new_ip} {new_hostname}.{DOMAIN} {new_hostname}"
                
                # Parse old entry to get MAC
                old_mac, old_ip, old_hostname = parse_dhcp_entry(old)
                
                # Format old entry with proper MAC display (uppercase)
                if old_mac:
                    formatted_old = f"{old_mac.upper()},{old_ip},{old_hostname}"
                else:
                    formatted_old = f"[Missing MAC address],{old_ip},{old_hostname}"
                
                print(f"   IP: {ip}")
                print(f"      Current: {formatted_old} (DNS: {current_dns})")
                print(f"      Proposed: {new} (DNS: {new_dns})")
        
        # Show MAC conflicts with DNS transparency
        if mac_conflicts:
            print("\nMAC Address Conflicts (Current vs. Proposed):")
            for ip, kind, old, new, conflict_type in mac_conflicts:
                new_mac, new_ip, new_hostname = parse_dhcp_entry(new)
                old_mac, old_ip, old_hostname = parse_dhcp_entry(old)
                
                # Pull current DNS mapping if it exists - use old_ip to find DNS for current entry
                current_dns = existing_dns_map.get(old_ip, "[No existing DNS]")
                new_dns = f"{new_ip} {new_hostname}.{DOMAIN} {new_hostname}"
                
                print(f"   MAC: {new_mac}")
                print(f"      Current: {old_mac} -> {old_ip} (DNS: {current_dns})")
                print(f"      Proposed: {new_mac} -> {new_ip} (DNS: {new_dns})")
        
        # Show hostname conflicts with DNS transparency
        hostname_conflicts_list = [c for c in all_conflicts if len(c) == 5 and c[4] == "hostname_conflict"]
        if hostname_conflicts_list:
            print("\nHostname Conflicts (same hostname, different IP/MAC):")
            for ip, kind, old, new, conflict_type in hostname_conflicts_list:
                new_mac, new_ip, new_hostname = parse_dhcp_entry(new)
                old_mac, old_ip, _ = parse_dhcp_entry(old)
                
                # Pull current DNS mapping if it exists
                current_dns = existing_dns_map.get(ip, "No existing DNS")
                new_dns = f"{new_ip} {new_hostname}.{DOMAIN} {new_hostname}"
                
                # Format old entry with proper MAC display
                if old_mac:
                    formatted_old = f"{old_mac} -> {old_ip}"
                else:
                    formatted_old = f"[Missing MAC address] -> {old_ip}"
                
                print(f"   Hostname: {new_hostname}")
                print(f"      Current: {formatted_old} (DNS: {current_dns})")
                print(f"      Proposed: {new_mac} -> {new_ip} (DNS: {new_dns})")
        
        response = input("\nDo you want to overwrite these entries with the new values? [y/N] ").strip().lower()
        if response != 'y':
            print("Aborting. No changes made.")
            sys.exit(0)
        
        # User confirmed, add conflicts to the "to_add" maps (overwriting logic below will handle it)
        for conflict in all_conflicts:
            # conflicts can be either:
            # - (new_entry, existing_entry) for simple conflicts
            # - (ip, kind, old_entry, new_entry, conflict_type) for more complex ones
            if len(conflict) == 2:
                # Simple conflict: (new_entry, existing_entry)
                new_entry, existing_entry = conflict
                new_mac, new_ip, new_hostname = new_entry
                old_mac, _, _ = existing_entry
            else:
                # Complex conflict: (ip, kind, old_entry, new_entry, conflict_type)
                _, _, old_entry, new_entry, _ = conflict
                new_mac, new_ip, new_hostname = parse_dhcp_entry(new_entry)
                old_mac, _, _ = parse_dhcp_entry(old_entry)

            # For conflicts, we need to update both DNS and DHCP to ensure consistency
            # Create the DNS entry string
            dns_entry = f"{new_ip} {new_hostname}.{DOMAIN} {new_hostname}"
            # Create the DHCP entry string
            dhcp_entry = f"{new_mac},{new_ip},{new_hostname}"

            # Add to both maps to ensure consistency
            dns_to_add[new_ip] = dns_entry
            dhcp_to_add[new_ip] = dhcp_entry

            # If the MAC changed for this IP/hostname, drop the stale entry that is
            # still sitting under the old MAC key so it doesn't survive into the
            # final dhcp.hosts list alongside the new one. This was the cause of
            # duplicate DHCP entries when a MAC address was changed in the source file.
            if old_mac and old_mac.upper() != new_mac.upper():
                existing_dhcp_map.pop(old_mac.upper(), None)

    # --- Apply Updates ---
    
    # 1. Update Maps with new items (this handles both new and overwritten)
    for ip, entry in dns_to_add.items():
        existing_dns_map[ip] = entry
        
    for mac, entry in dhcp_to_add.items():
        # Extract MAC from the entry to use as key
        entry_mac, _, _ = parse_dhcp_entry(entry)
        existing_dhcp_map[entry_mac] = entry
    
    # 1.5. Drop the specific existing DNS entries superseded by a conflict
    # resolution the user approved above. Matched by (ip, hostname) rather
    # than ip alone, so other dns.hosts lines that happen to share the ip
    # (e.g. manually-added aliases unrelated to this reservation) are left
    # untouched instead of being swept away along with the stale entry.
    for conflict in all_conflicts:
        if len(conflict) == 2:
            _, existing_entry = conflict
            old_ip, old_hostname = existing_entry[1], existing_entry[2]
        else:
            _, _, old_entry, _, _ = conflict
            _, old_ip, old_hostname = parse_dhcp_entry(old_entry)

        existing_dns_entries = [
            e for e in existing_dns_entries
            if parse_dns_entry(e)[0] != old_ip or parse_dns_entry(e)[2] != old_hostname
        ]

    # 2. Build the final dns.hosts list, keyed by (ip, hostname) rather than
    # ip alone so multiple aliases sharing one IP all survive.
    #
    # Start with every remaining existing entry (the stale ones superseded
    # by an approved conflict resolution were already removed in step 1.5
    # above), then layer new/changed entries from the CSV on top.
    final_dns_by_identity = {}
    for entry in existing_dns_entries:
        normalized = normalize_dns_hosts_entry(entry)
        ip, _, hostname = parse_dns_entry(normalized)
        if ip:
            final_dns_by_identity[(ip, hostname)] = normalized

    # Add new DNS entries from non-conflicting CSV entries
    for entry in conflicts['only_in_new']:
        mac, ip, hostname = entry
        if mac:  # Only add entries with MAC addresses (DHCP entries)
            hostname = hostname.lower()
            final_dns_by_identity[(ip, hostname)] = f"{ip} {hostname}.{DOMAIN} {hostname}"

    # Add DNS entries from conflict resolutions
    for ip, dns_entry in dns_to_add.items():
        _, _, hostname = parse_dns_entry(dns_entry)
        final_dns_by_identity[(ip, hostname)] = dns_entry

    data["dns"]["hosts"] = list(final_dns_by_identity.values())
    data["dhcp"]["hosts"] = list(existing_dhcp_map.values())

    # Warn about any entry that existed before this run but won't be
    # written back, whether dropped intentionally (an approved conflict
    # resolution) or not, so a future mismatch is visible in the log
    # instead of silently disappearing.
    warn_dropped_entries("dns.hosts", dns_entry_identity, original_dns_hosts, data["dns"]["hosts"])
    warn_dropped_entries("dhcp.hosts", dhcp_entry_identity, original_dhcp_hosts, data["dhcp"]["hosts"])

    try:
        validate_toml_integrity(data)
    except ValueError as e:
        print(f"❌ Integrity check failed: {e}", file=sys.stderr)
        print("   No changes were made to the live configuration.", file=sys.stderr)
        sys.exit(1)

    backup_pihole_toml()

    # Write to a temp file and atomically swap it into place, so a crash or
    # error mid-write can never leave a half-written pihole.toml on disk.
    tmp_path = f"{PIHOLE_TOML_PATH}.tmp"
    try:
        orig_stat = os.stat(PIHOLE_TOML_PATH)
        with open(tmp_path, "w") as f:
            toml.dump(data, f)
        os.chmod(tmp_path, orig_stat.st_mode)
        os.chown(tmp_path, orig_stat.st_uid, orig_stat.st_gid)
        os.replace(tmp_path, PIHOLE_TOML_PATH)
    except Exception as e:
        print(f"Error writing TOML: {e}", file=sys.stderr)
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        sys.exit(1)

    print(f"✅ Updated {PIHOLE_TOML_PATH}")
    print(f"   DNS entries count:  {len(data['dns']['hosts'])}")
    print(f"   DHCP entries count: {len(data['dhcp']['hosts'])}")
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
            "  • Preserves existing DNS/DHCP entries not in the input file,\n"
            "    including DNS records added via the Pi-hole web UI and\n"
            "    multiple hostname aliases sharing one IP\n"
            "  • Warns and skips (instead of silently dropping) any\n"
            "    dns.hosts/dhcp.hosts entry it can't parse, and logs any\n"
            "    entry a run actually removes\n"
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
