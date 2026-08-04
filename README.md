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
| **[check_wifi.sh](#wi-fi-signal-monitor)** | macOS/Linux/Windows | Bash | Live RSSI dashboard with running signal-quality stats. |
| **[monitor_smb.sh](#macos-smb--wi-fi-monitor)** | macOS | Bash | Side-by-side monitoring of SMB and Wi-Fi quality. |

### Infrastructure Configuration
| Script | OS | Language | Description |
|--------|----|----------|-------------|
| **[pihole_importer.py](#pi-hole-v6-reservation-importer)** | Pi-hole | Python | Bulk importer for static DHCP/DNS into Pi-hole v6. |

### Service Alerting & Monitoring
| Script | OS | Language | Description |
|--------|----|----------|-------------|
| **[tvh_kuma_monitor.sh](#tvheadend--uptime-kuma-monitor)** | Linux (systemd) | Bash | Watches Tvheadend logs and pushes up/down status to Uptime Kuma. |

---

## Tool Details & Usage

### Network Discovery & Auditing

#### Network Inventory Scanner
**File:** `network_scanner.py`

What it does: a two-stage device inventory for a local subnet. Stage 1 sends a raw ARP sweep (via Scapy) across the given CIDR range to quickly find every live host's IP and MAC — this works even for devices that block ping. Stage 2 hands those discovered IPs to Nmap (`-O -Pn -sT -p 22,80 -T4`) to fingerprint OS, hostname, and MAC vendor, and to check whether SSH (22) and HTTP (80) are open. Results are printed as a sorted, color-coded table (Rich) and can be dumped to CSV for record-keeping.

Why it's useful: answers "what's actually on my network right now" in one pass — handy for auditing unknown devices, spotting rogue hosts, confirming IoT gear you expect is present, or building a baseline inventory you can diff against later.

- **Usage:** `sudo python3 network_scanner.py [cidr] [--export] [-v]`
  - `cidr` — subnet to scan, e.g. `192.168.1.0/24` (default: `192.168.0.0/24`)
  - `--export` — also write results to `network_inventory.csv` in the current directory
  - Requires `sudo` (Nmap OS detection needs raw-socket access) and Nmap installed and on `PATH`; only works on the local broadcast domain (ARP doesn't route).
  - Example: `sudo python3 network_scanner.py 192.168.1.0/24 --export`

#### Proxmox LXC Network Auditor
**File:** `proxmox_net_check.sh`

What it does: walks a range of Proxmox container IDs, reads each container's `/etc/pve/lxc/<CTID>.conf`, and prints the CTID next to its raw `net0:` line (bridge, VLAN, IP/DHCP mode, firewall flag, etc.) for any CTID whose config file exists.

Why it's useful: on a host running dozens of LXCs, it's the fastest way to eyeball every container's network setup at once — e.g. confirming which CTs are still on the old bridge before a migration, or spotting one with a stale static IP — without opening each config file by hand.

- **Usage:** `./proxmox_net_check.sh [-s START_ID] [-e END_ID]` — must be run on the Proxmox VE host itself.
  - `-s, --start <ID>` — starting CTID (default: 100)
  - `-e, --end <ID>` — ending CTID (default: 130)
  - `-h, --help` / `-v, --version`
  - Example: `./proxmox_net_check.sh -s 200 -e 250`

### Performance & Connectivity Monitoring

#### High-Precision Latency Tester
**File:** `check_latency.py`

What it does: wraps the system `ping` with a fast, configurable interval (as low as fractions of a second) for the whole test duration, then parses the summary line for min/avg/max/stddev round-trip time and exact packet-loss percentage.

Why it's useful: a normal 1-ping-per-second check can miss brief drops. Pinging much more frequently over a longer window surfaces intermittent packet loss and jitter that would otherwise hide between samples — useful for diagnosing flaky Wi-Fi, a marginal ISP line, or VPN instability before escalating to your ISP or ripping out hardware.

- **Usage:** `python3 check_latency.py [-d DURATION] [-t TARGET] [-i INTERVAL]`
  - `-d, --duration` — test length in minutes (default: 10)
  - `-t, --target` — host/IP to ping (default: `8.8.8.8`)
  - `-i, --interval` — seconds between pings (default: 0.2)
  - Examples: `python3 check_latency.py` (default 10-min run against 8.8.8.8) · `python3 check_latency.py -d 5 -t 1.1.1.1` · `python3 check_latency.py -i 0.5`

#### Windows Latency Tester
**File:** `check_latency_win.ps1`

What it does: the Windows-native counterpart to `check_latency.py`, built on `Test-Connection` since Windows has no raw `ping -i <fractional>` equivalent. Pings once per second for the chosen duration, then computes packet loss, min/avg/max latency, and jitter (stddev), and writes a timestamped `NetworkTest_<timestamp>.txt` report to the Desktop (falling back to the current directory if the Desktop path isn't writable).

Why it's useful: gives Windows users the same latency/packet-loss visibility as the macOS/Linux script, plus a saved report file you can attach to a support ticket or compare across days.

- **Usage:** `.\check_latency_win.ps1 [-TargetHost <host>] [-DurationMinutes <n>]`
  - `-TargetHost` — host/IP to ping (default: `8.8.8.8`)
  - `-DurationMinutes` — test length in minutes (default: 10)
  - First run may require: `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`
  - Example: `.\check_latency_win.ps1 -TargetHost "1.1.1.1" -DurationMinutes 15`

#### Wi-Fi Signal Monitor
**File:** `check_wifi.sh`

What it does: a live, in-place (non-scrolling) terminal dashboard of Wi-Fi RSSI. It samples the signal once per second, colors the current reading by quality (Excellent/Good/Fair/Poor/Bad), and keeps a running min/max/mean/median/std-dev over the whole session, plus a printed key explaining the thresholds. It auto-detects the platform: macOS reads real dBm via `wdutil` (needs `sudo`); Linux reads real dBm via `iw`/`iwconfig`, falling back to `nmcli`'s 0-100% quality (converted to an approximate dBm, marked with `~`) if neither is present; Windows (via WSL, Git Bash, MSYS2, or Cygwin) shells out to `netsh.exe`, which likewise only exposes a 0-100% quality and so is also approximate.

Why it's useful: the running stats make it easy to tell a one-off dip from a chronically weak spot — point it at a laptop while walking around a house or office to find dead zones, decide where to place an access point/mesh node, or verify a channel change or antenna adjustment actually helped (falling std-dev = more stable link).

- **Usage:** `sudo ./check_wifi.sh` (macOS, requires root) or `./check_wifi.sh` (Linux/Windows, no sudo needed) — Ctrl+C to stop.

#### macOS SMB & Wi-Fi Monitor
**File:** `monitor_smb.sh`

What it does: polls every 5 seconds and prints, side-by-side, (1) each mounted SMB share's mount point, connection status, negotiated protocol version, and session-reconnect count via `smbutil statshares -a`, and (2) current Wi-Fi RSSI, noise floor, and derived SNR via the Apple80211 private framework (`airport -I`).

Why it's useful: SMB drops on macOS are frequently caused by a marginal Wi-Fi link, but the two symptoms show up in unrelated tools. Watching them together makes it obvious when a share disconnects (rising reconnect count) at the same moment SNR craters — confirming Wi-Fi as the root cause instead of the file server.

- **Usage:** `./monitor_smb.sh` — Ctrl+C to stop. Edit `WIFI_INTERFACE` (default `en0`) in the script if your Wi-Fi adapter has a different BSD device name.

### Infrastructure Configuration

#### Pi-hole v6 Reservation Importer
**File:** `pihole_importer.py`

What it does: bulk-imports static DHCP reservations and local DNS records into Pi-hole v6's `/etc/pihole/pihole.toml` from a plain `MAC,IP,Hostname` CSV/text file. It strictly validates every MAC (any common separator style), IPv4 address, and hostname/FQDN before touching anything, and aborts the whole run on the first invalid line. Before writing, it diffs the incoming entries against what's already in `pihole.toml` and reports four buckets — identical, conflicting (same IP/MAC/hostname but mismatched details), Pi-hole-only, and file-only — so nothing is silently overwritten. It always takes a timestamped backup (`pihole.toml.YYYYMMDD-HHMMSS`) before writing, and restarts the Pi-hole FTL service afterward so the new records take effect. It's idempotent — re-running with the same file is a no-op.

Why it's useful: hand-editing `pihole.toml` for dozens of static reservations is slow and error-prone (one bad MAC breaks DHCP for everyone). This turns "add these 40 devices" into a single reviewable command, with a safety net (backup + conflict report) if something in the file doesn't match reality.

- **Usage:** `sudo python3 pihole_importer.py [input_file] [--export] [--audit] [-v]`
  - `input_file` — path to the `MAC,IP,Hostname` file (default: `macaddr.txt`)
  - `--export` — dump Pi-hole's *current* entries to `macaddr.<timestamp>.csv` (useful for taking a snapshot or seeding a new input file)
  - `--audit` — compare the input file against Pi-hole without writing anything, showing same/conflicting/only-in-Pi-hole/only-in-file
  - Requires the `toml` Python package (`pip install toml` or `sudo apt install python3-toml`) and write access to `/etc/pihole/pihole.toml`, so it needs `sudo` on most systems.
  - Example: `sudo python3 pihole_importer.py ./reservations.txt`

### Service Alerting & Monitoring

#### Tvheadend / Uptime Kuma Monitor
**Files:** `tvh_kuma_monitor.sh`, `tvh-monitor.service`

What it does: runs as a systemd service that (1) tails `journalctl` for the `tvheadend` syslog identifier in real time, filtering out routine noise (`spawn:`/`frame=` lines); (2) on any remaining error/warning/critical log line, strips it down to a clean message and pushes a "down" status plus that message to an Uptime Kuma push monitor, then auto-clears back to "up" after 55 seconds (long enough for Kuma to register the down event, short enough to self-heal without manual intervention); and (3) in parallel, sends an "up" heartbeat every 50 seconds whenever no error is currently active, so Kuma's push monitor (which expects a heartbeat at least every 60s) never times out and flags the host itself as unreachable.

Why it's useful: Tvheadend can fail quietly (a tuner drops, a muxer crashes) with nothing but a log line to show for it. This turns that log line into an actual alert in Uptime Kuma (and whatever notification channels Kuma is wired to — Slack, email, push, etc.) without needing to poll or watch logs manually.

- **Usage:**
  1. Copy `tvh_kuma_monitor.sh` to `/usr/local/bin/` and make it executable.
  2. Create `/usr/local/bin/.env` (not tracked in this repo) with `KUMA_URL=http://<host>:3001/api/push/<key>` — the script refuses to start and logs a critical error via `logger` if this is missing.
  3. Install and start the service:
     ```bash
     sudo cp tvh-monitor.service /etc/systemd/system/
     sudo systemctl enable --now tvh-monitor.service
     ```
  4. Check status/logs with `systemctl status tvh-monitor` / `journalctl -u tvh-monitor -f`.

---

## Dependencies

Install Python dependencies via:
```bash
pip install -r requirements.txt
```

- **Nmap**: Required for `network_scanner.py`.
- **macOS tools**: `wdutil`, `smbutil`, `airport` (all standard on macOS, used by `check_wifi.sh` and `monitor_smb.sh`).
- **Linux Wi-Fi tools** (for `check_wifi.sh`): `iw` or `iwconfig` (wireless-tools) for real dBm, or `nmcli` (NetworkManager) as an approximate fallback.
- **Windows** (for `check_wifi.sh` and `check_latency_win.ps1`): `netsh.exe` (built-in) reachable from WSL/Git Bash/MSYS2/Cygwin; PowerShell execution policy may need `RemoteSigned` for the latency script.

## License & Contributing

- This project is licensed under the **MIT License**. See [LICENSE](LICENSE) for details.
- Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.
