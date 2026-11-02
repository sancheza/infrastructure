#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wifi_client_monitor.py

Gathers and displays connected wireless clients across heterogeneous access points
(ASUS RT-AX1800S, Netgear R6700v3/Genie, and Verizon Fios Gateway).
Correlates MAC addresses with DHCP reservations to report device names,
IPs, frequency bands (2.4G/5G), signal strength (RSSI), and PHY link rates.

Usage:
    python3 wifi_client_monitor.py [--help] [--version] [--json]
    python3 wifi_client_monitor.py --routers 192.168.1.2,192.168.1.3
    python3 wifi_client_monitor.py --all

ADR Compliance:
    - ADR 0004: Python formatting, type hints, Google-style docstrings.
    - ADR 0005: Command-line interface with argparse, help, and version.
    - ADR 0020: Column-aligned terminal logging with standard color palettes.
"""

import argparse
import csv
import hashlib
import http.cookiejar
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

VERSION = "1.0.0"
SCRIPT_NAME = "wifi_client_monitor.py"

# ADR 0020 ANSI Colors
COLOR_RESET = "\033[0m"
COLOR_INFO = "\033[32m"         # Green
COLOR_WARN = "\033[38;5;214m"   # 256-color Amber
COLOR_ERROR = "\033[31m"        # Red
COLOR_BOLD = "\033[1m"


def log_info(msg: str) -> None:
    """Print an aligned informational message in green."""
    print(f"{COLOR_INFO}{'INFO':<8}{COLOR_RESET} {msg}")


def log_warn(msg: str) -> None:
    """Print an aligned warning message in amber."""
    print(f"{COLOR_WARN}{'WARNING':<8}{COLOR_RESET} {msg}")


def log_error(msg: str) -> None:
    """Print an aligned error message in red to stderr."""
    print(f"{COLOR_ERROR}{'ERROR':<8}{COLOR_RESET} {msg}", file=sys.stderr)


def load_dotenv_file(filepath: Optional[str] = None) -> None:
    """Loads environment variables from a .env file into os.environ if present.

    Args:
        filepath: Optional path to .env file. Defaults to script directory .env
                  or user home directory ~/.env.
    """
    candidates = []
    if filepath:
        candidates.append(filepath)
    else:
        script_dir = os.path.dirname(os.path.realpath(__file__))
        candidates.append(os.path.join(script_dir, ".env"))
        candidates.append(os.path.expanduser("~/.env"))
        candidates.append("/usr/local/bin/.env")

    for env_path in candidates:
        if os.path.isfile(env_path):
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        key, val = line.split("=", 1)
                        key = key.strip().removeprefix("export ").strip()
                        val = val.strip().strip('"').strip("'")
                        if key and key not in os.environ:
                            os.environ[key] = val
                break
            except Exception:
                pass


def load_network_inventory(inventory_file: str) -> Tuple[Dict[str, Tuple[str, str]], Dict[str, str]]:
    """Loads MAC-to-(IP, Hostname) and IP-to-Hostname mappings from inventory.

    Args:
        inventory_file: Absolute or relative path to CSV reservations file.

    Returns:
        Tuple of (mac_map, ip_map).
    """
    mac_map: Dict[str, Tuple[str, str]] = {}
    ip_map: Dict[str, str] = {}
    if not os.path.isfile(inventory_file):
        return mac_map, ip_map

    try:
        with open(inventory_file, mode="r", encoding="utf-8", errors="replace") as f:
            reader = csv.reader(f)
            for row in reader:
                if not row or len(row) < 3:
                    continue
                mac, ip, name = row[0].strip().upper(), row[1].strip(), row[2].strip()
                if mac.startswith("#") or ":" not in mac:
                    continue
                mac_map[mac] = (ip, name)
                if ip and ip != "N/A":
                    ip_map[ip] = name
    except Exception as err:
        log_warn(f"Failed to read inventory file {inventory_file}: {err}")

    return mac_map, ip_map


class AsusCollector:
    """Queries connected stations from ASUS routers via SSH."""

    def __init__(self, ip: str, user: str = "admin", identity: Optional[str] = None):
        self.ip = ip
        self.user = user
        self.identity = identity or os.path.expanduser("~/.ssh/id_rsa")
        if not os.path.isfile(self.identity):
            alt_ed25519 = os.path.expanduser("~/.ssh/id_ed25519")
            if os.path.isfile(alt_ed25519):
                self.identity = alt_ed25519

    def collect(self) -> List[Dict[str, Any]]:
        """Collects connected wireless stations from the ASUS router."""
        clients: List[Dict[str, Any]] = []
        ssh_cmd = [
            "ssh",
            "-o", "ConnectTimeout=4",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-i", self.identity,
            f"{self.user}@{self.ip}",
            (
                "cat /proc/net/arp; echo '===STATIONS==='; "
                "iwpriv rai0 show stainfo >/dev/null 2>&1; "
                "iwpriv ra0 show stainfo >/dev/null 2>&1; "
                "iwpriv rai1 show stainfo >/dev/null 2>&1; "
                "iwpriv ra1 show stainfo >/dev/null 2>&1; "
                "dmesg | grep -E '[0-9A-Fa-f:]{17}.*STA' | tail -n 30; "
                "echo '===CLIENTLIST==='; cat /tmp/clientlist.json 2>/dev/null"
            )
        ]

        try:
            res = subprocess.run(ssh_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=8)
            output = res.stdout
        except Exception as e:
            log_error(f"Failed connecting to ASUS AP {self.ip}: {e}")
            return clients

        # 1. Parse ARP table for local IP lookups
        arp_map: Dict[str, str] = {}
        sections = output.split("===STATIONS===")
        if len(sections) > 1:
            for line in sections[0].splitlines():
                parts = line.split()
                if len(parts) >= 4 and ":" in parts[3]:
                    arp_map[parts[3].upper()] = parts[0]

        # 2. Parse driver dmesg output
        remaining = sections[1] if len(sections) > 1 else output
        subsections = remaining.split("===CLIENTLIST===")
        station_lines = subsections[0].splitlines()

        seen_macs = set()
        for line in station_lines:
            # Pattern matches: MAC STA ... RSSI ... RATE
            match = re.search(
                r"([0-9A-Fa-f:]{17})\s+STA\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+([-\d]+/[-\d]+).*?(\d+/\d+)\s+0\s+0",
                line
            )
            if match:
                mac = match.group(1).upper()
                if mac in seen_macs:
                    continue
                seen_macs.add(mac)

                rssi_val = match.group(2).split("/")[0] + " dBm"
                rate_val = match.group(3).split("/")[0] + " Mbps"
                band = "5 GHz" if ("80M" in line or "VHT" in line or "HE" in line) else "2.4 GHz"
                clients.append({
                    "mac": mac,
                    "ip": arp_map.get(mac, "Unknown"),
                    "band": band,
                    "rssi": rssi_val,
                    "rate": rate_val,
                    "ap_type": "ASUS RT-AX1800S",
                    "ap_ip": self.ip,
                })

        return clients


class NetgearCollector:
    """Queries connected clients from Netgear routers (e.g. R6700v3)."""

    def __init__(self, ip: str, password: Optional[str] = None):
        self.ip = ip
        self.password = password or os.getenv("NETGEAR_PASSWORD", "")

    def collect(self) -> List[Dict[str, Any]]:
        """Collects connected wireless stations via Netgear web services."""
        clients: List[Dict[str, Any]] = []
        if not self.password:
            # Report status with helpful prompt without breaking the full output
            return [{
                "mac": "AUTH_REQUIRED",
                "ip": self.ip,
                "band": "Unknown",
                "rssi": "N/A",
                "rate": "N/A",
                "ap_type": "Netgear R6700v3",
                "ap_ip": self.ip,
                "note": "Set NETGEAR_PASSWORD or pass --netgear-password to authenticate"
            }]

        try:
            passman = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            passman.add_password(None, f"http://{self.ip}/", "admin", self.password)
            authhandler = urllib.request.HTTPBasicAuthHandler(passman)
            cj = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), authhandler)

            req = urllib.request.Request(
                f"http://{self.ip}/DEV_device.htm",
                headers={"User-Agent": "curl/7.68.0"}
            )
            with opener.open(req, timeout=6) as resp:
                html = resp.read().decode("utf-8", errors="ignore")

            if "Wireless Devices" in html:
                wireless_part = html.split("Wireless Devices", 1)[1]
                rows = re.findall(
                    r'<td[^>]*><span class="ttext">([0-9.]+)</span></td>\s*<td[^>]*><span class="ttext">([^<]*)</span></td>\s*<td[^>]*><span class="ttext">([0-9A-Fa-f:]{17})</span></td>',
                    wireless_part
                )
                for ip, name, mac in rows:
                    name_clean = name.strip() if name.strip() not in ("--", "") else None
                    clients.append({
                        "ip": ip,
                        "mac": mac.upper(),
                        "hostname": name_clean,
                        "band": "2.4/5 GHz",
                        "rssi": "N/A",
                        "rate": "N/A",
                        "ap_type": "Netgear R6700v3",
                        "ap_ip": self.ip
                    })
        except urllib.error.HTTPError as e:
            if e.code == 401:
                log_warn(f"Netgear authentication failed on {self.ip}: invalid password")
                return [{
                    "mac": "AUTH_FAILED",
                    "ip": self.ip,
                    "band": "Unknown",
                    "rssi": "N/A",
                    "rate": "N/A",
                    "ap_type": "Netgear R6700v3",
                    "ap_ip": self.ip,
                    "note": "Authentication failed: 401 Unauthorized"
                }]
            log_warn(f"Netgear query failed on {self.ip}: HTTP {e.code}")
        except Exception as e:
            log_warn(f"Netgear query failed on {self.ip}: {type(e).__name__} ({e})")

        return clients


class VerizonCollector:
    """Queries connected clients from Verizon Fios Gateway routers (e.g. CR1000A / G3100)."""

    def __init__(self, ip: str, password: Optional[str] = None):
        self.ip = ip
        self.password = password or os.getenv("VERIZON_PASSWORD", "")

    def collect(self) -> List[Dict[str, Any]]:
        """Collects connected wireless devices from the Verizon Fios Gateway."""
        if not self.password:
            return [{
                "mac": "AUTH_REQUIRED",
                "ip": self.ip,
                "band": "Unknown",
                "rssi": "N/A",
                "rate": "N/A",
                "ap_type": "Verizon Fios Gateway",
                "ap_ip": self.ip,
                "note": "Set VERIZON_PASSWORD or pass --verizon-password to authenticate"
            }]

        clients: List[Dict[str, Any]] = []
        log_info(f"Authenticating with Verizon Gateway at {self.ip}...")
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            cj = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=ctx),
                urllib.request.HTTPCookieProcessor(cj)
            )

            # Step 1: Obtain login token
            req = urllib.request.Request(f"https://{self.ip}/loginStatus.cgi")
            with opener.open(req, timeout=5) as resp:
                status_data = json.loads(resp.read().decode())
                login_token = status_data.get("loginToken", "")

            if not login_token:
                log_warn(f"Verizon gateway at {self.ip} did not provide a login token")
                return []

            # Step 2: Hash credentials according to Luci admin auth
            user_md5 = hashlib.md5(b"admin").hexdigest().lower()
            pw_md5 = hashlib.md5(self.password.encode()).hexdigest().lower()
            combined = (login_token + pw_md5).encode("ascii")
            pw_hash = hashlib.sha512(combined).hexdigest().lower()

            form_data = urllib.parse.urlencode({
                "luci_username": user_md5,
                "luci_password": pw_hash,
                "luci_view": "Desktop",
                "luci_token": login_token,
                "luci_keep_login": "0"
            }).encode("utf-8")

            login_req = urllib.request.Request(
                f"https://{self.ip}/login.cgi",
                data=form_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            try:
                with opener.open(login_req, timeout=5) as resp:
                    pass
            except urllib.error.HTTPError as e:
                if e.code == 403:
                    try:
                        err_data = json.loads(e.read().decode())
                        flag = err_data.get("flag")
                        if flag == 1:
                            note = "Authentication failed: invalid password"
                        elif flag == 7:
                            note = "Authentication failed: temporarily locked (too many attempts)"
                        else:
                            note = f"Authentication failed: error flag {flag}"
                    except Exception:
                        note = "Authentication failed: HTTP 403 Forbidden"
                    log_warn(f"Verizon authentication failed on {self.ip}: {note}")
                    return [{
                        "mac": "AUTH_FAILED",
                        "ip": self.ip,
                        "band": "Unknown",
                        "rssi": "N/A",
                        "rate": "N/A",
                        "ap_type": "Verizon Fios Gateway",
                        "ap_ip": self.ip,
                        "note": note
                    }]
                raise

            # Step 3: Fetch known devices
            dev_req = urllib.request.Request(f"https://{self.ip}/cgi/cgi_known_devices.js")
            with opener.open(dev_req, timeout=5) as resp:
                dev_js = resp.read().decode("utf-8", errors="ignore")

            match = re.search(r'addROD\s*\(\s*["\']known_device_list["\']\s*,\s*(\{.*?\})\s*\);', dev_js, re.DOTALL)
            if match:
                data = json.loads(match.group(1))
                dev_list = data.get("known_devices", [])
                for dev in dev_list:
                    conn = dev.get("conn_type", "").lower()
                    status = str(dev.get("status", "")).lower()
                    if "wifi" in conn or "wireless" in conn or dev.get("is_wireless", False):
                        if status in ("1", "true", "connected", "active"):
                            clients.append({
                                "mac": dev.get("mac", "").upper(),
                                "ip": dev.get("ip", "Unknown"),
                                "hostname": dev.get("name") or dev.get("host_name"),
                                "band": dev.get("band", "Wi-Fi"),
                                "rssi": f"{dev.get('rssi')} dBm" if dev.get("rssi") else "N/A",
                                "rate": f"{dev.get('tx_rate')} Mbps" if dev.get("tx_rate") else "N/A",
                                "ap_type": "Verizon Fios Gateway",
                                "ap_ip": self.ip
                            })

            # Logout politely
            try:
                logout_req = urllib.request.Request(
                    f"https://{self.ip}/logout.cgi",
                    data=b"",
                    headers={"Content-Type": "application/x-www-form-urlencoded"}
                )
                opener.open(logout_req, timeout=3)
            except Exception:
                pass

        except Exception as e:
            log_warn(f"Verizon query failed on {self.ip}: {type(e).__name__} ({e})")

        return clients


def main() -> int:
    """Main execution entry point."""
    parser = argparse.ArgumentParser(
        prog=SCRIPT_NAME,
        description="Unified Wireless Client and AP Association Monitor."
    )
    parser.add_argument("-v", "--version", action="version", version=f"{SCRIPT_NAME} {VERSION}")
    parser.add_argument(
        "-a", "--all", action="store_true",
        help="Scan all known APs (ASUS 192.168.1.2 & 192.168.1.3, Netgear 192.168.0.11, Verizon 192.168.0.1)"
    )
    parser.add_argument(
        "-r", "--routers", type=str, default="192.168.1.2,192.168.1.3",
        help="Comma-separated router IPs to query (Default: 192.168.1.2,192.168.1.3)"
    )
    parser.add_argument("--netgear-password", type=str, default=None, help="Admin password for Netgear router")
    parser.add_argument("--verizon-password", type=str, default=None, help="Admin password for Verizon router")
    parser.add_argument("--json", action="store_true", help="Output results in JSON format")
    parser.add_argument(
        "--inventory", type=str,
        default=os.path.join(os.path.dirname(os.path.realpath(__file__)), "dhcp_reservations.csv"),
        help="Path to dhcp_reservations.csv for friendly name mapping"
    )

    args = parser.parse_args()

    load_dotenv_file()

    mac_inv, ip_inv = load_network_inventory(args.inventory)
    router_list = [r.strip() for r in args.routers.split(",") if r.strip()]
    if args.all:
        for extra in ["192.168.0.1", "192.168.0.11"]:
            if extra not in router_list:
                router_list.append(extra)

    all_records: List[Dict[str, Any]] = []

    for router_ip in router_list:
        if router_ip in ("192.168.1.2", "192.168.1.3"):
            collector = AsusCollector(router_ip)
            all_records.extend(collector.collect())
        elif router_ip == "192.168.0.11":
            collector = NetgearCollector(router_ip, password=args.netgear_password or os.getenv("NETGEAR_PASSWORD"))
            all_records.extend(collector.collect())
        elif router_ip == "192.168.0.1":
            collector = VerizonCollector(router_ip, password=args.verizon_password or os.getenv("VERIZON_PASSWORD"))
            all_records.extend(collector.collect())

    # Enrich records with inventory names (matching by MAC, then IP, then reverse DNS)
    import socket

    for rec in all_records:
        mac = rec.get("mac", "")
        ip = rec.get("ip", "")
        if mac in mac_inv:
            inv_ip, inv_name = mac_inv[mac]
            rec["device_name"] = inv_name
            if ip in ("Unknown", "", None):
                rec["ip"] = inv_ip
        elif ip in ip_inv:
            rec["device_name"] = ip_inv[ip]
        elif ip and ip != "Unknown":
            try:
                host_info = socket.gethostbyaddr(ip)[0]
                rec["device_name"] = host_info.split(".")[0]
            except Exception:
                rec["device_name"] = rec.get("hostname", "Unknown")
        else:
            rec["device_name"] = rec.get("hostname", "Unknown")

    if args.json:
        print(json.dumps(all_records, indent=2))
        return 0

    # Print Table Format
    print(f"\n{COLOR_BOLD}{'CONNECTED AP':<22} {'DEVICE NAME':<18} {'IP ADDRESS':<16} {'MAC ADDRESS':<18} {'BAND':<8} {'RSSI':<10} {'LINK RATE':<10}{COLOR_RESET}")
    print("-" * 106)

    for rec in all_records:
        if rec.get("mac") in ("AUTH_REQUIRED", "AUTH_FAILED"):
            ap_label = f"{rec.get('ap_type')} ({rec.get('ap_ip')})"
            tag = "[AUTH FAILED]" if rec.get("mac") == "AUTH_FAILED" else "[AUTH REQUIRED]"
            print(f"{ap_label:<22} {COLOR_WARN}{tag:<18}{COLOR_RESET} {rec.get('ip'):<16} {'--':<18} {'--':<8} {'--':<10} {rec.get('note', ''):<10}")
            continue

        ap_label = f"{rec.get('ap_type', '').replace('RT-AX1800S', '')} ({rec.get('ap_ip')})"
        dev_name = str(rec.get("device_name", "Unknown"))[:17]
        ip = str(rec.get("ip", "Unknown"))
        mac = str(rec.get("mac", "Unknown"))
        band = str(rec.get("band", "Unknown"))
        rssi = str(rec.get("rssi", "N/A"))
        rate = str(rec.get("rate", "N/A"))

        print(f"{ap_label:<22} {dev_name:<18} {ip:<16} {mac:<18} {band:<8} {rssi:<10} {rate:<10}")

    print("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
