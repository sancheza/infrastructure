#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wifi_ap_survey.py

Multi-Vendor Access Point Interrogator and Wireless Client Surveyor.

Automatically discovers and interrogates heterogeneous access points across your
network to audit connected wireless clients, signal levels, frequency bands,
and PHY link rates. Supports auto-probing banner detection to dynamically identify
router and AP platforms without requiring manual configuration or hardcoded IPs.

Supported Platforms:
    - ASUS WRT / Asuswrt-Merlin (RT-AX series, MediaTek & Broadcom radios via SSH)
    - Netgear (Nighthawk R6700 / Genie series via authenticated HTTP web services)
    - Verizon Fios Router (CR1000A, G3100 via LuCI ArcMD5 SHA-512 challenge-response)
    - Ubiquiti UniFi (UniFi OS / UDM / Network Controller via REST API)
    - OpenWrt (LEDE / OpenWrt Linux APs via ubus / iwinfo over SSH)
    - TP-Link (Omada SDN Controller & Archer web status APIs)
    - MikroTik (RouterOS v7+ REST API & registration table)

Usage Examples:
    # 1. Automatic discovery (scans local subnet, fingerprints APs, prints table):
    wifi_ap_survey.py

    # 2. Interrogate specific routers (auto-fingerprints each IP):
    wifi_ap_survey.py --routers 192.168.1.1,192.168.1.2

    # 3. Explicitly typed routers:
    wifi_ap_survey.py --routers asus:192.168.1.2,netgear:192.168.0.11,verizon:192.168.0.1

    # 4. JSON output for automation / monitoring ingestion:
    wifi_ap_survey.py --json

    # 5. Provide credentials via CLI arguments or .env:
    wifi_ap_survey.py --verizon-password secret --netgear-password secret

ADR Compliance:
    - ADR 0004: Python formatting, type hints, Google-style docstrings.
    - ADR 0005: Command-line interface with argparse, help, and version.
    - ADR 0020: Column-aligned terminal logging with standard color palettes.
"""

import abc
import argparse
import concurrent.futures
import csv
import hashlib
import http.cookiejar
import json
import os
import platform
import re
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Set, Tuple

VERSION = "2.0.0"
SCRIPT_NAME = "wifi_ap_survey.py"

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


# ==============================================================================
# Abstract Base Collector
# ==============================================================================

class BaseAPCollector(abc.ABC):
    """Abstract base class for all access point collectors."""

    def __init__(self, ip: str, ap_type: str = "Generic AP"):
        self.ip = ip
        self.ap_type = ap_type

    @abc.abstractmethod
    def collect(self) -> List[Dict[str, Any]]:
        """Interrogates the AP and returns a list of connected client records.

        Returns:
            List of dictionaries containing client metrics (mac, ip, hostname,
            band, rssi, rate, ap_type, ap_ip, and optional note).
        """
        raise NotImplementedError


# ==============================================================================
# Vendor Collectors
# ==============================================================================

class AsusCollector(BaseAPCollector):
    """Queries connected stations from ASUS routers via SSH."""

    def __init__(self, ip: str, user: str = "admin", identity: Optional[str] = None):
        super().__init__(ip, "ASUS Router")
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
                "dmesg | grep -E '[0-9A-Fa-f:]{17}.*STA' | tail -n 35; "
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
                    "ap_type": self.ap_type,
                    "ap_ip": self.ip,
                })

        return clients


class NetgearCollector(BaseAPCollector):
    """Queries connected clients from Netgear routers (e.g. Nighthawk / R6700v3)."""

    def __init__(self, ip: str, password: Optional[str] = None):
        super().__init__(ip, "Netgear Router")
        self.password = password or os.getenv("NETGEAR_PASSWORD", "")

    def collect(self) -> List[Dict[str, Any]]:
        """Collects connected wireless stations via Netgear web services."""
        clients: List[Dict[str, Any]] = []
        if not self.password:
            return [{
                "mac": "AUTH_REQUIRED",
                "ip": self.ip,
                "band": "Unknown",
                "rssi": "N/A",
                "rate": "N/A",
                "ap_type": self.ap_type,
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
                        "ap_type": self.ap_type,
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
                    "ap_type": self.ap_type,
                    "ap_ip": self.ip,
                    "note": "Authentication failed: 401 Unauthorized"
                }]
            log_warn(f"Netgear query failed on {self.ip}: HTTP {e.code}")
        except Exception as e:
            log_warn(f"Netgear query failed on {self.ip}: {type(e).__name__} ({e})")

        return clients


class VerizonCollector(BaseAPCollector):
    """Queries connected clients from Verizon Fios Router (e.g. CR1000A / G3100)."""

    def __init__(self, ip: str, password: Optional[str] = None, username: Optional[str] = None):
        super().__init__(ip, "Verizon Fios Router")
        self.password = password or os.getenv("VERIZON_PASSWORD", "")
        self.username = username or os.getenv("VERIZON_USERNAME", "admin")

    @staticmethod
    def _arc_md5(s: str) -> str:
        """Verizon LuCI ArcMD5: SHA-512 of lowercase MD5 hex string."""
        m = hashlib.md5(s.encode("utf-8")).hexdigest().lower()
        return hashlib.sha512(m.encode("ascii")).hexdigest().lower()

    @staticmethod
    def _login_encode(password: str, token: str) -> str:
        """Verizon LuCI login_encode: SHA-512 of token + ArcMD5(password)."""
        arc = VerizonCollector._arc_md5(password)
        return hashlib.sha512((token + arc).encode("ascii")).hexdigest().lower()

    def collect(self) -> List[Dict[str, Any]]:
        """Collects connected wireless devices from the Verizon Fios Router."""
        if not self.password:
            return [{
                "mac": "AUTH_REQUIRED",
                "ip": self.ip,
                "band": "Unknown",
                "rssi": "N/A",
                "rate": "N/A",
                "ap_type": self.ap_type,
                "ap_ip": self.ip,
                "note": "Set VERIZON_PASSWORD or pass --verizon-password to authenticate"
            }]

        clients: List[Dict[str, Any]] = []
        log_info(f"Authenticating with Verizon Router at {self.ip}...")
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
                log_warn(f"Verizon router at {self.ip} did not provide a login token")
                return []

            # Step 2: Try login with configured username (and alternate case if needed)
            pw_hash = self._login_encode(self.password, login_token)
            candidates = [self.username]
            alt = self.username.lower() if self.username != self.username.lower() else self.username.capitalize()
            if alt not in candidates:
                candidates.append(alt)

            login_success = False
            last_err_note = "Authentication failed"

            for user_candidate in candidates:
                user_hash = self._arc_md5(user_candidate)
                form_data = urllib.parse.urlencode({
                    "luci_username": user_hash,
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
                        if resp.status == 200:
                            login_success = True
                            break
                except urllib.error.HTTPError as e:
                    if e.code == 403:
                        try:
                            err_data = json.loads(e.read().decode())
                            flag = err_data.get("flag")
                            if flag == 1:
                                last_err_note = "Authentication failed: invalid password"
                            elif flag == 7:
                                last_err_note = "Authentication failed: temporarily locked (too many attempts)"
                                break
                            else:
                                last_err_note = f"Authentication failed: error flag {flag}"
                        except Exception:
                            last_err_note = "Authentication failed: HTTP 403 Forbidden"
                        continue
                    raise

            if not login_success:
                log_warn(f"Verizon authentication failed on {self.ip}: {last_err_note}")
                return [{
                    "mac": "AUTH_FAILED",
                    "ip": self.ip,
                    "band": "Unknown",
                    "rssi": "N/A",
                    "rate": "N/A",
                    "ap_type": self.ap_type,
                    "ap_ip": self.ip,
                    "note": last_err_note
                }]

            # Step 3: Fetch known devices script
            dev_req = urllib.request.Request(f"https://{self.ip}/cgi/cgi_known_devices.js")
            with opener.open(dev_req, timeout=5) as resp:
                dev_js = resp.read().decode("utf-8", errors="ignore")

            # Extract active station metrics from get_wlan_stat_info
            match = re.search(r'addROD\s*\(\s*["\']get_wlan_stat_info["\']\s*,\s*(\[.*?\])\s*\);', dev_js, re.DOTALL)
            if match:
                raw_list = match.group(1).replace("'", '"')
                raw_list = re.sub(r',\s*\]', ']', raw_list)
                try:
                    stations = json.loads(raw_list)
                    for entry in stations:
                        if len(entry) >= 8:
                            mac, iface, mode, mimo, rssi, snr, pkts, rate_kbps = entry[:8]
                            mac = mac.upper()
                            if "ath0" in iface:
                                band = "5 GHz"
                            elif "ath2" in iface:
                                band = "6 GHz"
                            elif "ath12" in iface:
                                band = "2.4 GHz (IoT)"
                            else:
                                band = "2.4 GHz"

                            rssi_str = f"{rssi} dBm" if rssi else "N/A"
                            try:
                                rate_mbps = f"{round(float(rate_kbps) / 1000)} Mbps"
                            except Exception:
                                rate_mbps = f"{rate_kbps} Kbps"

                            clients.append({
                                "mac": mac,
                                "ip": "Unknown",
                                "band": band,
                                "rssi": rssi_str,
                                "rate": rate_mbps,
                                "ap_type": self.ap_type,
                                "ap_ip": self.ip
                            })
                except Exception as e:
                    log_warn(f"Failed parsing get_wlan_stat_info: {e}")

            # Step 4: Polite logout
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


class UniFiCollector(BaseAPCollector):
    """Queries connected clients from UniFi OS / Network Controller."""

    def __init__(self, ip: str, user: Optional[str] = None, password: Optional[str] = None, site: str = "default"):
        super().__init__(ip, "UniFi AP")
        self.user = user or os.getenv("UNIFI_USER", "admin")
        self.password = password or os.getenv("UNIFI_PASSWORD", "")
        self.site = site or os.getenv("UNIFI_SITE", "default")

    def collect(self) -> List[Dict[str, Any]]:
        """Collects connected stations from UniFi Network application."""
        clients: List[Dict[str, Any]] = []
        if not self.password:
            return [{
                "mac": "AUTH_REQUIRED",
                "ip": self.ip,
                "band": "Unknown",
                "rssi": "N/A",
                "rate": "N/A",
                "ap_type": self.ap_type,
                "ap_ip": self.ip,
                "note": "Set UNIFI_PASSWORD or pass --unifi-password to authenticate"
            }]

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx), urllib.request.HTTPCookieProcessor(cj))

        login_payload = json.dumps({"username": self.user, "password": self.password}).encode("utf-8")
        headers = {"Content-Type": "application/json"}

        # UniFi OS (UDM/CloudKey Gen2) uses /api/auth/login; standalone uses /api/login
        logged_in = False
        base_api = ""
        for login_path in ["/api/auth/login", "/api/login"]:
            try:
                req = urllib.request.Request(f"https://{self.ip}{login_path}", data=login_payload, headers=headers)
                with opener.open(req, timeout=4) as resp:
                    if resp.status == 200:
                        logged_in = True
                        base_api = "/proxy/network" if "auth" in login_path else ""
                        break
            except Exception:
                pass

        if not logged_in:
            log_warn(f"UniFi authentication failed on {self.ip}")
            return [{
                "mac": "AUTH_FAILED",
                "ip": self.ip,
                "band": "Unknown",
                "rssi": "N/A",
                "rate": "N/A",
                "ap_type": self.ap_type,
                "ap_ip": self.ip,
                "note": "Authentication failed against UniFi Controller"
            }]

        try:
            sta_url = f"https://{self.ip}{base_api}/api/s/{self.site}/stat/sta"
            req = urllib.request.Request(sta_url)
            with opener.open(req, timeout=6) as resp:
                data = json.loads(resp.read().decode())
                stations = data.get("data", [])
                for sta in stations:
                    if not sta.get("is_wired", False):
                        band = "5 GHz" if sta.get("radio") == "na" else "2.4 GHz"
                        if sta.get("channel", 0) > 14:
                            band = "5 GHz"
                        rssi = f"{sta.get('rssi', sta.get('signal', 'N/A'))} dBm"
                        rate = f"{sta.get('tx_rate', 0) // 1000} Mbps" if sta.get("tx_rate") else "N/A"
                        clients.append({
                            "mac": sta.get("mac", "").upper(),
                            "ip": sta.get("ip", "Unknown"),
                            "hostname": sta.get("name") or sta.get("hostname"),
                            "band": band,
                            "rssi": rssi,
                            "rate": rate,
                            "ap_type": f"UniFi ({sta.get('ap_mac', self.ip)})",
                            "ap_ip": self.ip
                        })
        except Exception as e:
            log_warn(f"Failed fetching UniFi client stats from {self.ip}: {e}")

        return clients


class OpenWrtCollector(BaseAPCollector):
    """Queries connected clients from OpenWrt / LEDE access points via SSH / ubus."""

    def __init__(self, ip: str, user: str = "root", identity: Optional[str] = None):
        super().__init__(ip, "OpenWrt AP")
        self.user = user or os.getenv("OPENWRT_USER", "root")
        self.identity = identity or os.path.expanduser("~/.ssh/id_rsa")

    def collect(self) -> List[Dict[str, Any]]:
        """Collects connected stations via ubus and iwinfo."""
        clients: List[Dict[str, Any]] = []
        ssh_cmd = [
            "ssh",
            "-o", "ConnectTimeout=4",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            f"{self.user}@{self.ip}",
            (
                "cat /proc/net/arp; echo '===UBUS==='; "
                "for iface in $(ubus list hostapd.* 2>/dev/null); do "
                "echo \"INTERFACE $iface\"; ubus call $iface get_clients 2>/dev/null; "
                "done"
            )
        ]
        if self.identity and os.path.isfile(self.identity):
            ssh_cmd.insert(6, "-i")
            ssh_cmd.insert(7, self.identity)

        try:
            res = subprocess.run(ssh_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=8)
            output = res.stdout
        except Exception as e:
            log_warn(f"OpenWrt SSH query failed on {self.ip}: {e}")
            return clients

        arp_map: Dict[str, str] = {}
        sections = output.split("===UBUS===")
        if len(sections) > 0:
            for line in sections[0].splitlines():
                parts = line.split()
                if len(parts) >= 4 and ":" in parts[3]:
                    arp_map[parts[3].upper()] = parts[0]

        if len(sections) > 1:
            ubus_out = sections[1]
            mac_matches = list(re.finditer(r'["\']?([0-9a-fA-F:]{17})["\']?\s*:\s*\{', ubus_out))
            for match in mac_matches:
                mac = match.group(1).upper()
                start_idx = match.end() - 1
                brace_count = 0
                end_idx = start_idx
                for j in range(start_idx, len(ubus_out)):
                    if ubus_out[j] == '{':
                        brace_count += 1
                    elif ubus_out[j] == '}':
                        brace_count -= 1
                        if brace_count == 0:
                            end_idx = j + 1
                            break
                block = ubus_out[start_idx:end_idx]
                try:
                    data = json.loads(block)
                    signal = data.get("signal", "N/A")
                    tx_rate = data.get("rate", {}).get("tx", 0) if isinstance(data.get("rate"), dict) else data.get("tx", {}).get("rate", 0)
                    rate_mbps = f"{tx_rate // 1000} Mbps" if tx_rate else "N/A"
                    clients.append({
                        "mac": mac,
                        "ip": arp_map.get(mac, "Unknown"),
                        "band": "Wi-Fi",
                        "rssi": f"{signal} dBm" if signal != "N/A" else "N/A",
                        "rate": rate_mbps,
                        "ap_type": self.ap_type,
                        "ap_ip": self.ip
                    })
                except Exception:
                    pass

        return clients


class MikroTikCollector(BaseAPCollector):
    """Queries connected clients from MikroTik RouterOS v7+ via REST API."""

    def __init__(self, ip: str, user: Optional[str] = None, password: Optional[str] = None):
        super().__init__(ip, "MikroTik RouterOS")
        self.user = user or os.getenv("MIKROTIK_USER", "admin")
        self.password = password or os.getenv("MIKROTIK_PASSWORD", "")

    def collect(self) -> List[Dict[str, Any]]:
        """Collects connected wireless stations from MikroTik registration table."""
        clients: List[Dict[str, Any]] = []
        if not self.password:
            return [{
                "mac": "AUTH_REQUIRED",
                "ip": self.ip,
                "band": "Unknown",
                "rssi": "N/A",
                "rate": "N/A",
                "ap_type": self.ap_type,
                "ap_ip": self.ip,
                "note": "Set MIKROTIK_PASSWORD or pass --mikrotik-password to authenticate"
            }]

        try:
            passman = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            passman.add_password(None, f"https://{self.ip}/", self.user, self.password)
            authhandler = urllib.request.HTTPBasicAuthHandler(passman)
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx), authhandler)

            reg_url = f"https://{self.ip}/rest/interface/wireless/registration-table"
            req = urllib.request.Request(reg_url, headers={"User-Agent": "curl/7.68.0"})
            with opener.open(req, timeout=5) as resp:
                entries = json.loads(resp.read().decode())
                for ent in entries:
                    mac = ent.get("mac-address", "").upper()
                    signal = ent.get("signal-strength", "").split("@")[0]
                    tx_rate = ent.get("tx-rate", "N/A")
                    clients.append({
                        "mac": mac,
                        "ip": "Unknown",
                        "band": "Wi-Fi",
                        "rssi": f"{signal} dBm" if signal else "N/A",
                        "rate": tx_rate,
                        "ap_type": self.ap_type,
                        "ap_ip": self.ip
                    })
        except Exception as e:
            log_warn(f"MikroTik query failed on {self.ip}: {e}")

        return clients


class TPLinkCollector(BaseAPCollector):
    """Queries connected clients from TP-Link Omada SDN controllers."""

    def __init__(self, ip: str, user: Optional[str] = None, password: Optional[str] = None, site: str = "default"):
        super().__init__(ip, "TP-Link Omada")
        self.user = user or os.getenv("TPLINK_USER", "admin")
        self.password = password or os.getenv("TPLINK_PASSWORD", "")
        self.site = site or os.getenv("TPLINK_SITE", "Default")

    def collect(self) -> List[Dict[str, Any]]:
        """Collects connected stations from TP-Link Omada controller."""
        clients: List[Dict[str, Any]] = []
        if not self.password:
            return [{
                "mac": "AUTH_REQUIRED",
                "ip": self.ip,
                "band": "Unknown",
                "rssi": "N/A",
                "rate": "N/A",
                "ap_type": self.ap_type,
                "ap_ip": self.ip,
                "note": "Set TPLINK_PASSWORD or pass --tplink-password to authenticate"
            }]

        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            cj = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx), urllib.request.HTTPCookieProcessor(cj))

            login_data = json.dumps({"name": self.user, "password": self.password}).encode("utf-8")
            login_req = urllib.request.Request(
                f"https://{self.ip}:8043/api/v2/login",
                data=login_data,
                headers={"Content-Type": "application/json"}
            )
            with opener.open(login_req, timeout=5) as resp:
                res_data = json.loads(resp.read().decode())
                token = res_data.get("result", {}).get("token", "")

            clients_req = urllib.request.Request(
                f"https://{self.ip}:8043/api/v2/sites/{self.site}/clients?currentPage=1&currentPageSize=100",
                headers={"Csrf-Token": token}
            )
            with opener.open(clients_req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                for c in data.get("result", {}).get("data", []):
                    if not c.get("wireless", True):
                        continue
                    band = "5 GHz" if c.get("radioId") == 1 else "2.4 GHz"
                    clients.append({
                        "mac": c.get("mac", "").upper(),
                        "ip": c.get("ip", "Unknown"),
                        "hostname": c.get("name") or c.get("hostName"),
                        "band": band,
                        "rssi": f"{c.get('rssi', 'N/A')} dBm",
                        "rate": f"{c.get('txRate', 'N/A')} Mbps",
                        "ap_type": f"TP-Link Omada ({c.get('apName', self.ip)})",
                        "ap_ip": self.ip
                    })
        except Exception as e:
            log_warn(f"TP-Link query failed on {self.ip}: {e}")

        return clients


# ==============================================================================
# Auto-Probing & Fingerprinting
# ==============================================================================

def fingerprint_ap(ip: str) -> Optional[str]:
    """Lightweight banner and service fingerprinter to classify AP brand.

    Args:
        ip: Target IP address to probe.

    Returns:
        One of 'asus', 'netgear', 'verizon', 'unifi', 'openwrt', 'mikrotik', 'tplink', or None.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    # Fast HTTP check (ASUS, Netgear, OpenWrt, MikroTik, TP-Link)
    try:
        req = urllib.request.Request(f"http://{ip}/", headers={"User-Agent": "Mozilla/5.0 (compatible; APProbe/2.0)"})
        with urllib.request.urlopen(req, timeout=0.8, context=ctx) as resp:
            body = resp.read(2048).decode("utf-8", errors="ignore")
            auth = resp.headers.get("www-authenticate", "").lower()
            if "netgear" in auth or "netgear" in body.lower():
                return "netgear"
            if "main_login.asp" in body.lower() or "asus" in body.lower():
                return "asus"
            if "openwrt" in body.lower() or "luci" in body.lower():
                return "openwrt"
            if "mikrotik" in body.lower() or "routeros" in body.lower():
                return "mikrotik"
            if "tplink" in body.lower() or "omada" in body.lower():
                return "tplink"
    except urllib.error.HTTPError as e:
        auth = str(e.headers.get("WWW-Authenticate", "")).lower()
        body = e.read(1024).decode("utf-8", errors="ignore")
        if "netgear" in auth or "netgear" in body.lower():
            return "netgear"
        if "openwrt" in body.lower() or "luci" in body.lower():
            return "openwrt"
        if "mikrotik" in auth or "mikrotik" in body.lower():
            return "mikrotik"
    except Exception:
        pass

    # Fast HTTPS check (Verizon CR1000A / G3100)
    try:
        req = urllib.request.Request(f"https://{ip}/loginStatus.cgi")
        with urllib.request.urlopen(req, timeout=0.8, context=ctx) as resp:
            data = resp.read(512).decode("utf-8", errors="ignore")
            if "loginToken" in data or "Fios" in data:
                return "verizon"
    except Exception:
        pass

    # General HTTPS check (UniFi, Netgear HTTPS, Verizon web root)
    try:
        req = urllib.request.Request(f"https://{ip}/", headers={"User-Agent": "Mozilla/5.0 (compatible; APProbe/2.0)"})
        with urllib.request.urlopen(req, timeout=0.8, context=ctx) as resp:
            body = resp.read(2048).decode("utf-8", errors="ignore")
            auth = resp.headers.get("www-authenticate", "").lower()
            if "fios router" in body.lower() or "verizon" in body.lower():
                return "verizon"
            if "netgear" in auth or "netgear" in body.lower():
                return "netgear"
            if "unifi" in body.lower() or "ubiquiti" in body.lower():
                return "unifi"
    except urllib.error.HTTPError as e:
        body = e.read(1024).decode("utf-8", errors="ignore")
        if "fios" in body.lower() or "verizon" in body.lower():
            return "verizon"
    except Exception:
        pass

    # Check UniFi port 8443
    try:
        req = urllib.request.Request(f"https://{ip}:8443/api/login", headers={"User-Agent": "Mozilla/5.0 (compatible; APProbe/2.0)"})
        with urllib.request.urlopen(req, timeout=0.8, context=ctx) as resp:
            body = resp.read(2048).decode("utf-8", errors="ignore").lower()
            headers = str(resp.headers).lower()
            if any(k in body for k in ("unifi", "ubiquiti", "api.err", '"rc"')) or "unificookie" in headers or "ubnt" in headers:
                return "unifi"
    except urllib.error.HTTPError as e:
        body = e.read(2048).decode("utf-8", errors="ignore").lower()
        headers = str(e.headers).lower()
        if any(k in body for k in ("unifi", "ubiquiti", "api.err", '"rc"')) or "unificookie" in headers or "ubnt" in headers:
            return "unifi"
    except Exception:
        pass

    return None


def get_default_gateway() -> Optional[str]:
    """Resolves local network default gateway IP address."""
    system = platform.system()
    try:
        if system == "Darwin":
            res = subprocess.run(["route", "-n", "get", "default"], stdout=subprocess.PIPE, text=True, timeout=2)
            m = re.search(r"gateway:\s+([0-9.]+)", res.stdout)
            if m:
                return m.group(1)
        else:
            res = subprocess.run(["ip", "route", "show", "default"], stdout=subprocess.PIPE, text=True, timeout=2)
            m = re.search(r"default via ([0-9.]+)", res.stdout)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


def check_mgmt_port(ip: str) -> Optional[str]:
    """Tests if an IP has any common router/AP management ports open (80, 443, 22)."""
    for port in (80, 443, 22):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.2)
                if s.connect_ex((ip, port)) == 0:
                    return ip
        except Exception:
            pass
    return None


def get_local_subnet_prefix() -> Optional[str]:
    """Finds the local IPv4 /24 subnet prefix (e.g. '192.168.0.')."""
    gw = get_default_gateway()
    if gw:
        parts = gw.split(".")
        if len(parts) == 4:
            return f"{parts[0]}.{parts[1]}.{parts[2]}."
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            parts = local_ip.split(".")
            if len(parts) == 4:
                return f"{parts[0]}.{parts[1]}.{parts[2]}."
    except Exception:
        pass
    return None


def discover_candidate_ips(inventory_file: Optional[str] = None) -> List[str]:
    """Finds active neighboring IP addresses on the local network.

    Combines:
      1. Default gateway.
      2. OS ARP table cache.
      3. Inventory file entries (if present).
      4. Fast parallel port sweep across the local /24 subnet to overcome cold ARP caches.

    Returns:
        Sorted list of candidate IP strings.
    """
    candidates: Set[str] = set()
    gw = get_default_gateway()
    if gw:
        candidates.add(gw)

    # 1. Check existing ARP cache
    system = platform.system()
    if system == "Darwin":
        try:
            res = subprocess.run(["arp", "-an"], stdout=subprocess.PIPE, text=True, timeout=3)
            for line in res.stdout.splitlines():
                m = re.search(r"\(([0-9.]+)\)\s+at\s+([0-9a-fA-F:]+)", line)
                if m:
                    ip = m.group(1)
                    if not ip.endswith(".255") and ip != "255.255.255.255":
                        candidates.add(ip)
        except Exception:
            pass
    else:
        try:
            with open("/proc/net/arp", "r", encoding="utf-8") as f:
                for line in f.readlines()[1:]:
                    parts = line.split()
                    if len(parts) >= 4 and parts[3] != "00:00:00:00:00:00":
                        candidates.add(parts[0])
        except Exception:
            pass

    # 2. Check inventory if provided (filtering for router/AP keywords to avoid adding IoT clients)
    if inventory_file and os.path.isfile(inventory_file):
        try:
            ap_keywords = ("router", "ap", "fios", "ax", "netgear", "asus", "wifi", "gateway", "access", "wrt", "unifi", "omada")
            with open(inventory_file, mode="r", encoding="utf-8", errors="ignore") as f:
                for row in csv.reader(f):
                    if len(row) >= 3:
                        ip = row[1].strip()
                        name = row[2].strip().lower()
                        if re.match(r"^\d+\.\d+\.\d+\.\d+$", ip) and any(k in name for k in ap_keywords):
                            candidates.add(ip)
        except Exception:
            pass

    # 3. Active subnet sweep across the /24 to guarantee cold ARP caches are populated
    prefix = get_local_subnet_prefix()
    if prefix:
        sweep_ips = [f"{prefix}{i}" for i in range(1, 255) if f"{prefix}{i}" not in candidates]
        with concurrent.futures.ThreadPoolExecutor(max_workers=96) as ex:
            for active_ip in ex.map(check_mgmt_port, sweep_ips):
                if active_ip:
                    candidates.add(active_ip)

    return sorted(candidates, key=lambda x: [int(p) if p.isdigit() else 0 for p in x.split(".")])


def auto_discover_aps(inventory_file: Optional[str] = None) -> List[Tuple[str, str]]:
    """Scans local subnet neighbors in parallel and returns detected APs.

    Returns:
        List of (ip, brand) tuples.
    """
    candidates = discover_candidate_ips(inventory_file)
    log_info(f"Auto-probing {len(candidates)} local network candidates for AP signatures...")

    detected: List[Tuple[str, str]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(32, len(candidates) or 1)) as executor:
        future_to_ip = {executor.submit(fingerprint_ap, ip): ip for ip in candidates}
        for future in concurrent.futures.as_completed(future_to_ip):
            ip = future_to_ip[future]
            try:
                brand = future.result()
                if brand:
                    detected.append((ip, brand))
            except Exception:
                pass

    return sorted(detected, key=lambda x: x[0])


# ==============================================================================
# Main Orchestration & Output
# ==============================================================================

def create_collector(brand: str, ip: str, args: argparse.Namespace) -> BaseAPCollector:
    """Factory creating appropriate BaseAPCollector for given brand and IP."""
    brand = brand.lower()
    if brand == "asus":
        return AsusCollector(ip, user=args.asus_user, identity=args.asus_key)
    elif brand == "netgear":
        return NetgearCollector(ip, password=args.netgear_password)
    elif brand == "verizon":
        return VerizonCollector(ip, password=args.verizon_password, username=args.verizon_username)
    elif brand == "unifi":
        return UniFiCollector(ip, user=args.unifi_user, password=args.unifi_password, site=args.unifi_site)
    elif brand == "openwrt":
        return OpenWrtCollector(ip, user=args.openwrt_user, identity=args.openwrt_key)
    elif brand == "mikrotik":
        return MikroTikCollector(ip, user=args.mikrotik_user, password=args.mikrotik_password)
    elif brand == "tplink":
        return TPLinkCollector(ip, user=args.tplink_user, password=args.tplink_password, site=args.tplink_site)
    else:
        # Fallback to fingerprinter or generic
        probed = fingerprint_ap(ip)
        if probed and probed != brand:
            return create_collector(probed, ip, args)
        return AsusCollector(ip, user=args.asus_user, identity=args.asus_key)


def main() -> int:
    """Main execution entry point."""
    parser = argparse.ArgumentParser(
        prog=SCRIPT_NAME,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""
Multi-Vendor Access Point Interrogator & Wireless Client Surveyor (v2.0)
=======================================================================
Interrogates heterogeneous AP controllers (ASUS, Netgear, Verizon, UniFi,
OpenWrt, TP-Link, MikroTik) to extract real-time station metrics (frequency
band, signal RSSI, PHY link rate) and maps devices against DHCP reservations.

Runs fully automated out of the box via active local auto-probing.
""",
        epilog="""
Examples:
  %(prog)s
      Auto-discovers APs on the local network and prints unified survey.

  %(prog)s --routers 192.168.1.1,192.168.1.2
      Scans specified routers, auto-fingerprinting each device platform.

  %(prog)s --routers asus:192.168.1.2,netgear:192.168.0.11,verizon:192.168.0.1
      Interrogates explicitly typed APs directly without probing.

  %(prog)s --json
      Emits JSON array containing full station and AP telemetry.
"""
    )
    parser.add_argument("-v", "--version", action="version", version=f"{SCRIPT_NAME} {VERSION}")
    parser.add_argument(
        "-r", "--routers", type=str, default=None,
        help="Comma-separated router specifications: '[brand:]<ip>'. If omitted, APs are auto-discovered."
    )
    parser.add_argument(
        "--auto", action="store_true",
        help="Force automatic discovery of local APs even if configuration defaults exist."
    )
    parser.add_argument("--json", action="store_true", help="Output results in JSON format.")
    parser.add_argument(
        "--inventory", type=str,
        default=os.path.join(os.path.dirname(os.path.realpath(__file__)), "dhcp_reservations.csv"),
        help="Path to CSV inventory (MAC,IP,Name) for friendly hostname mapping."
    )

    # Vendor Authentication Flags
    auth_group = parser.add_argument_group("Vendor Authentication Options")
    auth_group.add_argument("--netgear-password", type=str, default=None, help="Password for Netgear routers.")
    auth_group.add_argument("--verizon-password", type=str, default=None, help="Password for Verizon Fios routers.")
    auth_group.add_argument("--verizon-username", type=str, default="admin", help="Username for Verizon router (Default: admin).")
    auth_group.add_argument("--asus-user", type=str, default="admin", help="SSH username for ASUS routers (Default: admin).")
    auth_group.add_argument("--asus-key", type=str, default=None, help="Path to SSH private key for ASUS routers.")
    auth_group.add_argument("--unifi-user", type=str, default=None, help="Username for UniFi controller.")
    auth_group.add_argument("--unifi-password", type=str, default=None, help="Password for UniFi controller.")
    auth_group.add_argument("--unifi-site", type=str, default="default", help="UniFi site name (Default: default).")
    auth_group.add_argument("--openwrt-user", type=str, default="root", help="SSH username for OpenWrt APs.")
    auth_group.add_argument("--openwrt-key", type=str, default=None, help="Path to SSH private key for OpenWrt APs.")
    auth_group.add_argument("--mikrotik-user", type=str, default=None, help="Username for MikroTik RouterOS API.")
    auth_group.add_argument("--mikrotik-password", type=str, default=None, help="Password for MikroTik RouterOS API.")
    auth_group.add_argument("--tplink-user", type=str, default=None, help="Username for TP-Link Omada controller.")
    auth_group.add_argument("--tplink-password", type=str, default=None, help="Password for TP-Link Omada controller.")
    auth_group.add_argument("--tplink-site", type=str, default="Default", help="TP-Link Omada site name.")

    args = parser.parse_args()

    load_dotenv_file()

    # Determine AP target list
    targets: List[Tuple[str, str]] = []  # List of (ip, brand)
    if args.routers and not args.auto:
        raw_list = [r.strip() for r in args.routers.split(",") if r.strip()]
        for item in raw_list:
            if ":" in item:
                parts = item.split(":", 1)
                targets.append((parts[1].strip(), parts[0].strip().lower()))
            else:
                probed = fingerprint_ap(item) or "unknown"
                targets.append((item, probed))
    else:
        # Automated discovery mode
        targets = auto_discover_aps(inventory_file=args.inventory)
        if not targets:
            log_warn("No compatible AP controllers detected automatically on the local subnet.")
            log_info("Tip: specify APs explicitly using --routers [brand:]<ip> (e.g. --routers asus:192.168.1.2)")
            return 0

    log_info(f"Interrogating {len(targets)} AP(s): {', '.join(f'{b}:{ip}' for ip, b in targets)}")

    # Construct collectors
    collectors = [create_collector(brand, ip, args) for ip, brand in targets]

    # Concurrent Execution
    all_records: List[Dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(collectors) or 1)) as executor:
        future_to_col = {executor.submit(col.collect): col for col in collectors}
        for future in concurrent.futures.as_completed(future_to_col):
            try:
                records = future.result()
                all_records.extend(records)
            except Exception as e:
                col = future_to_col[future]
                log_error(f"Collector error on {col.ip}: {e}")

    # Inventory & DNS Enrichment
    mac_inv, ip_inv = load_network_inventory(args.inventory)
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
    print(f"\n{COLOR_BOLD}{'CONNECTED AP':<24} {'DEVICE NAME':<20} {'IP ADDRESS':<16} {'MAC ADDRESS':<18} {'BAND':<14} {'RSSI':<10} {'LINK RATE':<10}{COLOR_RESET}")
    print("-" * 114)

    for rec in all_records:
        if rec.get("mac") in ("AUTH_REQUIRED", "AUTH_FAILED"):
            ap_label = f"{rec.get('ap_type')} ({rec.get('ap_ip')})"
            tag = "[AUTH FAILED]" if rec.get("mac") == "AUTH_FAILED" else "[AUTH REQUIRED]"
            print(f"{ap_label:<24} {COLOR_WARN}{tag:<20}{COLOR_RESET} {rec.get('ip'):<16} {'--':<18} {'--':<14} {'--':<10} {rec.get('note', ''):<10}")
            continue

        ap_label = f"{rec.get('ap_type', '').replace('RT-AX1800S', '').strip()} ({rec.get('ap_ip')})"
        dev_name = str(rec.get("device_name", "Unknown"))[:19]
        ip = str(rec.get("ip", "Unknown"))
        mac = str(rec.get("mac", "Unknown"))
        band = str(rec.get("band", "Unknown"))
        rssi = str(rec.get("rssi", "N/A"))
        rate = str(rec.get("rate", "N/A"))

        print(f"{ap_label:<24} {dev_name:<20} {ip:<16} {mac:<18} {band:<14} {rssi:<10} {rate:<10}")

    print("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
