#!/usr/bin/env python3
import argparse
import csv
from pathlib import Path
import re

CSV_FIELDS = ["MAC", "IP", "Interface", "Hostname"]

def import_flat(file_path: Path):
    entries = []
    lines = file_path.read_text().splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if ':' in line and len(line.split(':')) == 6:  # MAC address
            mac = line
            i += 1
            if i < len(lines):
                parts = lines[i].strip().split()
                ip = parts[0]
                iface = parts[1] if len(parts) > 1 else ""
                hostname = parts[2] if len(parts) > 2 else ""
            else:
                ip, iface, hostname = "", "", ""
            entries.append({"MAC": mac, "IP": ip, "Interface": iface, "Hostname": hostname})
            # skip optional "New device" line
            i += 1
            if i < len(lines) and lines[i].strip() == "New device":
                i += 1
        else:
            i += 1
    return entries

def import_csv(file_path: Path):
    entries = []
    with file_path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            entries.append({
                "MAC": row.get("MAC",""),
                "IP": row.get("IP",""),
                "Interface": row.get("Interface",""),
                "Hostname": row.get("Hostname","")
            })
    return entries

def export_for_pihole_dns(entries):
    """IP HOSTNAME #MAC format for Pi-hole DNS copy/paste"""
    lines = []
    for e in entries:
        ip = e["IP"]
        hostname = e["Hostname"] or "unknown"
        mac = e["MAC"]
        if ip:
            lines.append(f"{ip}\t{hostname}\t# {mac}")
    return "\n".join(lines)

def export_for_pihole_dhcp(entries):
    """Pi-hole DHCP format"""
    lines = []
    for idx, e in enumerate(entries, start=1):
        lines.append(f"DHCP_CLIENT_{idx}={e['MAC']}")
        lines.append(f"DHCP_IP_{idx}={e['IP']}")
        lines.append(f"DHCP_HOSTNAME_{idx}={e['Hostname'] or 'unknown'}")
        lines.append("")  # blank line between entries
    return "\n".join(lines)

def save_csv(entries, csv_path: Path):
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for e in entries:
            writer.writerow(e)

def main():
    parser = argparse.ArgumentParser(description="Manage device entries for Pi-hole")
    parser.add_argument("--importflat", type=Path, help="Import flat-format file")
    parser.add_argument("--importcsv", type=Path, help="Import CSV-format file")
    parser.add_argument("--exportdns", type=Path, help="Export in Pi-hole DNS format")
    parser.add_argument("--exportdhcp", type=Path, help="Export in Pi-hole DHCP format")
    
    args = parser.parse_args()

    all_entries = []

    # Determine CSV filename from first source file
    csv_file = None
    if args.importflat:
        prefix = args.importflat.stem
        csv_file = args.importflat.parent / f"{prefix}.csv"
        all_entries.extend(import_flat(args.importflat))
    elif args.importcsv:
        prefix = args.importcsv.stem
        csv_file = args.importcsv.parent / f"{prefix}.csv"
        all_entries.extend(import_csv(args.importcsv))
    else:
        csv_file = Path("structured.csv")  # fallback

    # Save structured CSV
    save_csv(all_entries, csv_file)
    print(f"Saved structured CSV to {csv_file}")

    # Export DNS format
    if args.exportdns:
        args.exportdns.write_text(export_for_pihole_dns(all_entries))
        print(f"Exported Pi-hole DNS entries to {args.exportdns}")

    # Export DHCP format
    if args.exportdhcp:
        args.exportdhcp.write_text(export_for_pihole_dhcp(all_entries))
        print(f"Exported Pi-hole DHCP entries to {args.exportdhcp}")

if __name__ == "__main__":
    main()
