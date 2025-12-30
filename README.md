# Infrastructure & Network Utilities

A collection of scripts and tools designed for network administration, performance monitoring, and infrastructure configuration. These utilities cover macOS, Linux, and Windows environments.

## Repository Contents

### Network Discovery & Auditing
| Script | OS | Language | Description |
|--------|----|----------|-------------|
| **[network_scanner.py](#network-inventory-scanner)** | Linux/macOS | Python | Subnet scanner with ARP & Nmap integration. |
| **[proxmox_net_check.sh](#proxmox-lxc-network-auditor)** | Proxmox | Bash | Rapidly audit network interfaces for LXC containers. |

### Performance & Connectivity Monitoring
| Script | OS | Language | Description |
|--------|----|----------|-------------|
| **[check_latency.py](#high-precision-latency-tester)** | Linux/macOS | Python | Detailed latency and packet loss analysis tool. |
| **[check_latency_win.ps1](#windows-latency-tester)** | Windows | PowerShell | Windows-native latency monitor with reports. |
| **[check_wifi.sh](#macos-wi-fi-signal-monitor)** | macOS | Bash | Real-time RSSI signal strength monitor. |
| **[monitor_smb.sh](#macos-smb--wi-fi-monitor)** | macOS | Bash | Side-by-side monitoring of SMB and Wi-Fi quality. |

### Infrastructure Configuration
| Script | OS | Language | Description |
|--------|----|----------|-------------|
| **[pihole_importer.py](#pi-hole-v6-reservation-importer)** | Pi-hole | Python | Bulk importer for static DHCP/DNS into Pi-hole v6. |

---

## Tool Details & Usage

### Network Discovery & Auditing

#### Network Inventory Scanner
**File:** `network_scanner.py`
- **Stage 1:** Fast ARP sweep to identify live hosts (IP & MAC).
- **Stage 2:** Nmap integration for Hostnames, OS, Vendors, and Ports (SSH/HTTP).
- **Usage:** `sudo python3 network_scanner.py 192.168.1.0/24 --export`

#### Proxmox LXC Network Auditor
**File:** `proxmox_net_check.sh`
- Iterates through LXC configuration files to report primary interface settings.
- **Usage:** `./proxmox_net_check.sh -s 100 -e 150`

### Performance & Connectivity Monitoring

#### High-Precision Latency Tester
**File:** `check_latency.py`
- Diagnoses intermittent issues with continuous high-frequency pings.
- **Usage:** `python3 check_latency.py -d 5 -t 8.8.8.8`

#### Windows Latency Tester
**File:** `check_latency_win.ps1`
- Generates a detailed network quality report (Excellent/Good/Fair/Poor).
- **Usage:** `.\check_latency_win.ps1 -TargetHost "1.1.1.1" -DurationMinutes 15`

#### macOS Wi-Fi Signal Monitor
**File:** `check_wifi.sh`
- Real-time RSSI signal tracking via `wdutil`.
- **Usage:** `sudo ./check_wifi.sh`

#### macOS SMB & Wi-Fi Monitor
**File:** `monitor_smb.sh`
- Side-by-side status tracking of SMB mounts and Wi-Fi SNR.
- **Usage:** `./monitor_smb.sh`

### Infrastructure Configuration

#### Pi-hole v6 Reservation Importer
**File:** `pihole_importer.py`
- Bulk adds static DHCP/DNS records to `pihole.toml`.
- Includes auto-backup and conflict detection.
- **Usage:** `sudo python3 pihole_importer.py ./reservations.txt`

---

## Dependencies

Install Python dependencies via:
```bash
pip install -r requirements.txt
```

- **Nmap**: Required for `network_scanner.py`.
- **System Tools**: `wdutil`, `smbutil`, `airport` (standard on macOS).

## License & Contributing

- This project is licensed under the **MIT License**. See [LICENSE](LICENSE) for details.
- Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.
