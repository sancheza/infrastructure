#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
network_inventory.py

Comprehensive network scanner tool for local networks.
It performs an ARP scan for fast discovery and uses Nmap for detailed interrogation
(Hostname, OS, MAC Vendor, and Port Status), presenting the results in a 
numerically sorted terminal table and optionally exporting them to CSV.
"""
import argparse
import ipaddress
import nmap
import csv
from scapy.all import Ether, ARP, srp
from rich.console import Console
from rich.table import Table

# --- Configuration Constants ---
__version__ = "1.0.0"
DEFAULT_CIDR = "192.168.0.0/24"
SCAN_TIMEOUT_ARP = 1.0
NMAP_PROCESS_TIMEOUT = 90.0
TARGET_PORTS = "22,80" 
NMAP_ARGS_FULL = f"-O -Pn -sT -p {TARGET_PORTS} -T4" 
MAX_CELL_WIDTH = 30 # Max characters allowed in a table cell before truncation

def truncate_text(text: str, max_width: int) -> str:
    """
    Truncates text to ensure it fits within a single line. Handles None/newlines.
    """
    if text is None:
        return 'N/A'
    
    # Ensure text is string and replace newlines with space for single-line enforcement
    text = str(text).replace('\n', ' ') 
    
    if len(text) > max_width:
        return text[:max_width-3] + '...'
    return text

def get_port_color(status: str) -> str:
    """Returns the color based on port status."""
    status_lower = status.lower()
    if 'open' in status_lower:
        return "green"
    elif 'closed' in status_lower or 'filtered' in status_lower:
        return "red"
    return "white"

def get_mac_ip_table(cidr_range: str, console: Console) -> list[dict]:
    """
    Performs a fast ARP scan on the local subnet to discover live devices (IP and MAC).

    Args:
        cidr_range (str): The subnet in CIDR notation (e.g., '192.168.0.0/24').
        console (Console): Rich console object for printing.

    Returns:
        list[dict]: A list of dictionaries containing 'ip' and 'mac' for discovered devices.
    """
    console.print(f"📡 [bold yellow]Stage 1:[/bold yellow] Performing ARP scan on {cidr_range}...")
    
    arp_request = ARP(pdst=cidr_range)
    ether_frame = Ether(dst="ff:ff:ff:ff:ff:ff")
    packet = ether_frame / arp_request

    answered_list, _ = srp(packet, timeout=SCAN_TIMEOUT_ARP, verbose=0)
    
    devices = []
    for sent, received in answered_list:
        devices.append({
            "ip": received.psrc,
            "mac": received.hwsrc
        })
        
    return devices

def get_nmap_info(ip_list: list[str], console: Console) -> dict:
    """
    Uses python-nmap to perform Hostname, OS, Port Status, and MAC Vendor detection.

    Args:
        ip_list (list[str]): List of IP addresses found in the ARP scan.
        console (Console): Rich console object for printing.

    Returns:
        dict: A dictionary mapping IP addresses to detailed scan results.
    """
    console.print(f"🔎 [bold yellow]Stage 2:[/bold yellow] Running Nmap ({NMAP_ARGS_FULL}) on {len(ip_list)} discovered hosts...")
    
    nm = nmap.PortScanner()
    targets = ' '.join(ip_list)
    
    try:
        nm.scan(hosts=targets, arguments=NMAP_ARGS_FULL, sudo=True, timeout=NMAP_PROCESS_TIMEOUT) 
    except nmap.PortScannerError as e:
        console.print(f"[bold red]❌ Nmap Scan Error:[/bold red] Details: {e}")
        return {}

    nmap_results = {}
    for host in nm.all_hosts():
        # --- 1. Hostname Fix: Look through all hostnames provided by Nmap for the first non-empty name ---
        hostname = 'N/A'
        if nm[host]['hostnames']:
            for h in nm[host]['hostnames']:
                if h.get('name') and h.get('name').strip():
                    hostname = h['name'].strip()
                    break
                elif h.get('type') and h.get('type').strip():
                    hostname = h['type'].strip()
                    break

        # --- 2. OS ---
        os_info = 'N/A'
        if 'osmatch' in nm[host] and nm[host]['osmatch']:
            os_info = nm[host]['osmatch'][0].get('name', 'N/A')

        # --- 3. MAC Vendor & Port Status ---
        mac = nm[host].get('addresses', {}).get('mac')
        vendor = 'N/A'
        if mac and 'vendor' in nm[host] and mac in nm[host]['vendor']:
            vendor = nm[host]['vendor'][mac]

        ssh_status = nm[host].get('tcp', {}).get(22, {}).get('state', 'N/A')
        http_status = nm[host].get('tcp', {}).get(80, {}).get('state', 'N/A')

        nmap_results[host] = {
            'Hostname': hostname,
            'OS': os_info,
            'Vendor': vendor,
            'SSH_22': ssh_status.capitalize(),
            'HTTP_80': http_status.capitalize(),
        }
    
    return nmap_results

def sort_by_ip(results: list[dict]) -> list[dict]:
    """
    Sorts the final results list numerically by IP address.

    Args:
        results (list[dict]): The list of discovered devices.

    Returns:
        list[dict]: The sorted list.
    """
    return sorted(
        results,
        key=lambda x: ipaddress.IPv4Address(x['IP'])
    )

def export_to_csv(results: list[dict], filename: str, console: Console):
    """
    Exports the final scan results to a CSV file.

    Args:
        results (list[dict]): The list of discovered devices.
        filename (str): The name of the CSV file.
        console (Console): Rich console object for printing.
    """
    if not results:
        console.print("[bold red]⚠️ Export failed: No results to export.[/bold red]")
        return

    keys = results[0].keys()
    try:
        with open(filename, 'w', newline='', encoding='utf-8') as output_file:
            dict_writer = csv.DictWriter(output_file, fieldnames=keys)
            dict_writer.writeheader()
            dict_writer.writerows(results)
        console.print(f"💾 [bold white]Results successfully exported to:[/bold white] {filename}")
    except Exception as e:
        console.print(f"[bold red]❌ CSV Export Error:[/bold red] {e}")


def main():
    """Main function to parse arguments and execute the scan."""
    parser = argparse.ArgumentParser(
        description=(
            "Network Inventory Scanner\n"
            "\n"
            "Performs a two-stage device discovery on a local subnet:\n"
            "  1) ARP sweep for fast detection of live hosts (IP + MAC)\n"
            "  2) Nmap interrogation for Hostname, OS fingerprint, MAC vendor,\n"
            "     and port states for SSH (22) and HTTP (80)\n"
            "\n"
            "Results are presented in a sorted Rich table with truncated columns\n"
            "and color-coded port status indicators. Optional CSV export available."
        ),
        epilog=(
            "Notes:\n"
            "  • Requires root/sudo privileges for Nmap OS detection.\n"
            "  • ARP scanning works only on local networks.\n"
            "  • Nmap must be installed and accessible in PATH.\n"
            "\n"
            "Example:\n"
            "  sudo ./network_inventory.py 192.168.1.0/24 --export\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument(
        "cidr",
        nargs="?",
        default=DEFAULT_CIDR,
        help=(
            "CIDR subnet to scan (e.g., 192.168.1.0/24). "
            f"Defaults to {DEFAULT_CIDR} if omitted."
        )
    )

    parser.add_argument(
        "--export",
        action="store_true",
        help=(
            "Export scan results to a CSV file named 'network_inventory.csv' "
            "in the current directory."
        )
    )

    parser.add_argument(
        "-v", "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="Show program version and exit."
    )
    
    args = parser.parse_args()
    console = Console()
    
    # 1. Perform ARP Scan
    discovered_devices = get_mac_ip_table(args.cidr, console)
    
    if not discovered_devices:
        console.print(f"\n[bold red]❌ No devices found in {args.cidr}.[/bold red]")
        return

    ip_list = [d["ip"] for d in discovered_devices]

    # 2. Perform Nmap Scan
    nmap_data = get_nmap_info(ip_list, console)
    
    # 3. Combine Data and Finalize Results
    final_results = []
    for device in discovered_devices:
        ip = device["ip"]
        mac = device["mac"].upper()
        
        host_info = nmap_data.get(ip, {})
        
        final_results.append({
            "Hostname": host_info.get("Hostname", "N/A"),
            "OS": host_info.get("OS", "N/A"),
            "IP": ip,
            "MAC": mac,
            "Vendor": host_info.get("Vendor", "N/A"),
            "SSH_22": host_info.get("SSH_22", "N/A"),
            "HTTP_80": host_info.get("HTTP_80", "N/A"),
        })
        
    # 4. Sort Results by IP Address
    sorted_results = sort_by_ip(final_results)
        
    # 5. Display Results in a Rich Table
    table = Table(title=f"✅ Network Inventory: {args.cidr}", show_header=True, header_style="bold white", border_style="white")
    
    # Define columns with default white color
    table.add_column("Hostname", style="white", justify="left", max_width=MAX_CELL_WIDTH)
    table.add_column("OS", style="white", max_width=MAX_CELL_WIDTH)
    table.add_column("Vendor", style="white", max_width=MAX_CELL_WIDTH)
    table.add_column("IP Address", style="white", max_width=MAX_CELL_WIDTH)
    table.add_column("MAC Address", style="white", max_width=MAX_CELL_WIDTH)
    table.add_column("SSH (22)", justify="center")
    table.add_column("HTTP (80)", justify="center")
    
    # Add rows with truncation and conditional coloring
    for row in sorted_results:
        table.add_row(
            truncate_text(row["Hostname"], MAX_CELL_WIDTH),
            truncate_text(row["OS"], MAX_CELL_WIDTH),
            truncate_text(row["Vendor"], MAX_CELL_WIDTH),
            truncate_text(row["IP"], MAX_CELL_WIDTH),
            truncate_text(row["MAC"], MAX_CELL_WIDTH),
            f"[{get_port_color(row['SSH_22'])}]{row['SSH_22']}[/]",
            f"[{get_port_color(row['HTTP_80'])}]{row['HTTP_80']}[/]",
        )

    console.print("\n--- Network Scan Complete ---")
    console.print(table)
    console.print(f"Total Devices Found: [bold white]{len(sorted_results)}[/bold white]")

    # 6. Export to CSV if requested
    if args.export:
        export_to_csv(sorted_results, "network_inventory.csv", console)

if __name__ == "__main__":
    main()