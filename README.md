# Infrastructure & Network Utilities

A collection of scripts and tools designed for network administration, performance monitoring, and infrastructure configuration. These utilities cover macOS, Linux, and Windows environments, providing solutions for tasks ranging from detailed network inventory scanning to Pi-hole configuration management.

## Repository Contents

| Script | OS | Language | Description |
|--------|----|----------|-------------|
| **[network_scanner.py](#1-network-inventory-scanner)** | Linux/macOS | Python | Comprehensive subnet scanner with ARP & Nmap integration. |
| **[check_latency.py](#2-high-precision-latency-tester)** | Linux/macOS | Python | Detailed network latency and packet loss analysis tool. |
| **[check_latency_win.ps1](#3-windows-latency-tester)** | Windows | PowerShell | Windows-native latency monitor with report generation. |
| **[pihole_importer.py](#4-pi-hole-v6-reservation-importer)** | Linux (Pi-hole) | Python | Bulk importer for static DHCP/DNS into Pi-hole v6. |
| **[check_wifi.sh](#5-macos-wi-fi-signal-monitor)** | macOS | Bash | Real-time RSSI signal strength monitor. |
| **[monitor_smb.sh](#6-smb--wi-fi-monitor)** | macOS | Bash | Side-by-side monitoring of SMB mount status and Wi-Fi quality. |
| **[proxmox_net_check.sh](#7-proxmox-lxc-network-auditor)** | Proxmox (Debian) | Bash | Rapidly audit network interfaces for LXC containers. |

---

## Tool Details & Usage

### 1. Network Inventory Scanner
**File:** `network_scanner.py`

A robust discovery tool that maps out devices on a local network.
- **Features:** 
  - **Stage 1:** Fast ARP sweep to identify live hosts (IP & MAC).
  - **Stage 2:** Nmap integration to determine Hostnames, OS, MAC Vendors, and Open Ports (specifically SSH/22 and HTTP/80).
  - Outputs a beautiful, color-coded terminal table.
  - Supports exporting results to CSV.

**Usage:**
```bash
# Basic scan of a subnet
sudo python3 network_scanner.py 192.168.1.0/24

# Scan and export results to CSV
sudo python3 network_scanner.py 192.168.1.0/24 --export
```
*Note: Root privileges (`sudo`) are often required for OS fingerprinting.*

### 2. High-Precision Latency Tester
**File:** `check_latency.py`

Designed for diagnosing intermittent network issues where simple pings aren't enough. It runs a continuous stream of pings and calculates precise statistics.
- **Features:** Calculates Min/Avg/Max/StdDev RTT and exact packet loss percentages.

**Usage:**
```bash
# Run for 5 minutes targeting Google DNS
python3 check_latency.py -d 5 -t 8.8.8.8

# Run with a faster interval (every 0.1s)
python3 check_latency.py -i 0.1
```

### 3. Windows Latency Tester
**File:** `check_latency_win.ps1`

A PowerShell equivalent of the latency tester for Windows admins.
- **Features:** Real-time console visualization, generates a detailed text report upon completion, and performs a network quality assessment (Excellent/Good/Fair/Poor).

**Usage:**
```powershell
.\check_latency_win.ps1 -TargetHost "1.1.1.1" -DurationMinutes 15
```

### 4. Pi-hole v6 Reservation Importer
**File:** `pihole_importer.py`

Automates the often tedious task of adding static DHCP and DNS records to Pi-hole v6 (`pihole.toml`).
- **Features:** 
  - Idempotent: safe to run multiple times; it won't create duplicates.
  - Smart Conflict Resolution: Detects if an IP is already assigned to a different host and prompts for action.
  - Validates MAC addresses, IPs, and Hostnames before applying.
  - **Auto-Backup:** Creates a backup of your config before editing.

**Input Format (`reservations.txt`):**
```csv
AA:BB:CC:DD:EE:FF,192.168.1.10,printer-main
00:11:22:33:44:55,192.168.1.20,iot-hub
```

**Usage:**
```bash
sudo python3 pihole_importer.py ./dhcp_reservations.txt
```

### 5. macOS Wi-Fi Signal Monitor
**File:** `check_wifi.sh`

A lightweight diagnostic tool for macOS users to troubleshoot Wi-Fi dead zones or antenna positioning.
- **Features:** Leverages the native `wdutil` diagnostic tool for real-time RSSI readings.

**Usage:**
```bash
sudo ./check_wifi.sh
```

### 6. SMB & Wi-Fi Monitor
**File:** `monitor_smb.sh`

Useful for troubleshooting NAS connection drops. It monitors the status of mounted SMB shares while simultaneously logging Wi-Fi noise and signal-to-noise ratio (SNR).
- **Features:** Tracks session reconnect counts to identify if the mount is silently dropping and reconnecting.

**Usage:**
```bash
./monitor_smb.sh
```

### 7. Proxmox LXC Network Auditor
**File:** `proxmox_net_check.sh`

For use on a Proxmox VE host. It iterates through LXC container configuration files to report the network settings for a range of containers.

**Usage:**
```bash
# Check containers 100 through 150
./proxmox_net_check.sh -s 100 -e 150
```

---

## Dependencies

- **Python Scripts:** May require `pip install -r requirements.txt` (if provided) or individual packages like `scapy`, `python-nmap`, `rich`, `toml`.
- **System Tools:** 
  - `nmap` (for `network_scanner.py`)
  - `wdutil` / `smbutil` (standard on macOS)
