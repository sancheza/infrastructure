# ASUS Access Point Health Monitor

Health monitoring script for ASUS RT-AX1800S access points in standalone Access Point (AP) mode, reporting push heartbeats to Uptime Kuma.

---

## 1. Monitored Devices & Network Topology

The monitor validates physical link negotiation, switch hardware CRC error rates, and wireless radio status across both access points over SSH.

| Hostname | IP Address | Hardware Model | Monitored Switch Port | Expected Link Speed | Monitored SSIDs |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `RT-AX1800S-CDA8` | `192.168.1.2` | ASUS RT-AX1800S | Port 2 | 1000baseT Full Duplex | `ExampleNet` (2.4 GHz), `ExampleNet5` (5 GHz) |
| `RT-AX1800S-E198` | `192.168.1.3` | ASUS RT-AX1800S | Port 3 | 1000baseT Full Duplex | `ExampleNet` (2.4 GHz), `ExampleNet5` (5 GHz) |

---

## 2. Health Check Criteria

Every execution inspects three independent layers on each access point:

1. **Physical Link Speed**:
   Executes `swconfig dev switch0 show` and parses link status for the designated backhaul port. The link must report `speed:1000baseT full-duplex`. If a link downshifts to `100baseT` or `10baseT`, or reports `link:down`, the check fails.

2. **CRC Error Rate (Delta Tracking)**:
   Extracts `RxCRC` and `TxCRC` hardware MIB counters for the backhaul port. Because switch hardware MIB counters accumulate continuously until reboot, the script tracks the delta between consecutive runs using local state files:
   * **Within Limits**: Delta $\le 10$ errors per run (configurable via `-c, --crc-threshold`).
   * **Elevated Errors**: Delta $> 10$ errors per run triggers a failure.
   * **Device Reboot**: If cumulative counter is lower than the previous state, baseline resets automatically without triggering false positives.

3. **Wi-Fi Radio Broadcasting**:
   Executes `iwconfig` across interfaces (`ra0`, `rai0`, `ra1`, `rai1`) and verifies the presence of both ESSID `ExampleNet` and ESSID `ExampleNet5`.

---

## 3. Execution Schedule & Intervals

* **Host**: `monitor-host` (`192.168.1.23`)
* **User Account**: `youruser`
* **Schedule Interval**: Every 60 seconds via `cron`
* **Uptime Kuma Monitor**: `ASUS APs Backhaul & Health` (Push monitor token: `REDACTED_PUSH_TOKEN`)

### Crontab Entry

```crontab
* * * * * /home/youruser/.local/bin/asus_ap_kuma_monitor.sh --kuma-url "http://192.168.1.23:3001/api/push/REDACTED_PUSH_TOKEN" > /dev/null 2>&1
```

---

## 4. Files & Paths

| Path | Host | Purpose |
| :--- | :--- | :--- |
| `/home/youruser/infrastructure/asus_ap_kuma_monitor.sh` | `monitor-host` | Git-managed source script |
| `/home/youruser/.local/bin/asus_ap_kuma_monitor.sh` | `monitor-host` | Symlink to git source in PATH |
| `/home/youruser/infrastructure/asus_ap_kuma_monitor.md` | `monitor-host` | Technical documentation |
| `/home/youruser/.local/bin/asus_ap_kuma_monitor.md` | `monitor-host` | Symlink to documentation in `~/.local/bin/` |
| `/tmp/asus_ap_crc_<IP>_port<PORT>.state` | `monitor-host` | CRC counter delta tracking state files |
| `~/.ssh/id_rsa` / `~/.ssh/id_ed25519` | `monitor-host` | SSH keys for `admin@192.168.1.2` and `admin@192.168.1.3` |

---

## 5. Command-Line Options

```
asus_ap_kuma_monitor.sh [OPTIONS]
```

| Option | Argument | Description | Default |
| :--- | :--- | :--- | :--- |
| `-h, --help` | None | Show usage information and exit | N/A |
| `-v, --version` | None | Show version string and exit | `1.1.0` |
| `-k, --kuma-url` | `<URL>` | Uptime Kuma Push Monitor endpoint URL | `$KUMA_URL` or `/usr/local/bin/.env` |
| `-a, --aps` | `<IP:PORT,...>` | Comma-separated list of `AP_IP:BACKHAUL_PORT` | `192.168.1.2:2,192.168.1.3:3` |
| `-u, --user` | `<USER>` | SSH username for access points | `admin` |
| `-i, --identity` | `<KEY>` | SSH private key file path | Auto-detect (`~/.ssh/id_ed25519`, `~/.ssh/id_rsa`) |
| `-c, --crc-threshold` | `<NUM>` | Maximum allowed CRC error delta between runs | `10` |
| `-s, --state-dir` | `<DIR>` | Directory for state tracking files | `/tmp` |
| `-n, --dry-run` | None | Perform inspection without pushing to Uptime Kuma | `false` |
| `-V, --verbose` | None | Output detailed diagnostic logging to stdout | `false` |

---

## 6. Manual Execution & Verification

### Dry-Run Verification

Run diagnostic check without updating persistent state or pushing metrics to Uptime Kuma:

```bash
/home/youruser/.local/bin/asus_ap_kuma_monitor.sh --dry-run --verbose
```

Expected output:
```text
INFO     Inspecting AP at 192.168.1.2 (monitored backhaul port: 2)...
INFO     AP 192.168.1.2: Port 2 verified at 1000baseT full-duplex.
INFO     AP 192.168.1.2: Port 2 CRC errors within limits (Delta Rx: 0, Delta Tx: 0, Cumulative Rx: 0, Cumulative Tx: 0).
INFO     AP 192.168.1.2: 2.4 GHz SSID 'ExampleNet' broadcasting.
INFO     AP 192.168.1.2: 5 GHz SSID 'ExampleNet5' broadcasting.
INFO     Inspecting AP at 192.168.1.3 (monitored backhaul port: 3)...
INFO     AP 192.168.1.3: Port 3 verified at 1000baseT full-duplex.
INFO     AP 192.168.1.3: Port 3 CRC errors within limits (Delta Rx: 0, Delta Tx: 0, Cumulative Rx: 0, Cumulative Tx: 0).
INFO     AP 192.168.1.3: 2.4 GHz SSID 'ExampleNet' broadcasting.
INFO     AP 192.168.1.3: 5 GHz SSID 'ExampleNet5' broadcasting.
INFO     All APs healthy: all backhaul links 1 Gbps, 0 critical CRC errors, all SSIDs broadcasting.
INFO     [DRY-RUN] Would push to Kuma: status=up msg="OK_All_APs_1Gbps" ping=...
```

### Live Run

```bash
/home/youruser/.local/bin/asus_ap_kuma_monitor.sh --kuma-url "http://192.168.1.23:3001/api/push/REDACTED_PUSH_TOKEN"
```

---

## 7. Troubleshooting

### Port Negotiation Downshift (100 Mbps or 10 Mbps)

* **Symptom**: Monitor pushes `status=down` with message `AP_<IP>_Port<NUM>_100baseT` or `AP_<IP>_Port<NUM>_10baseT`.
* **Cause**: Cable termination degradation, bent RJ-45 pins, or intermediate switch port negotiation mismatch.
* **Resolution**:
  1. Inspect physical cable run between access point and upstream switch.
  2. Query live port status directly on the access point:
     ```bash
     ssh admin@<AP_IP> "swconfig dev switch0 port <PORT> get link"
     ```
  3. Re-seat or replace Ethernet cable.

### Active Switch CRC Frame Corruption

* **Symptom**: Monitor pushes `status=down` with message `AP_<IP>_Port<NUM>_CRC_Delta_Rx<COUNT>_Tx0`.
* **Cause**: Active electromagnetic interference, marginal cable crimp, or damaged physical patch cable causing real-time frame checksum errors.
* **Resolution**:
  1. Inspect state file on `monitor-host` to see delta progression:
     ```bash
     cat /tmp/asus_ap_crc_<IP_UNDERSCORES>_port<PORT>.state
     ```
  2. Query switch MIB counter directly:
     ```bash
     ssh admin@<AP_IP> "swconfig dev switch0 port <PORT> get mib"
     ```
  3. If delta continues increasing on every check, replace the Ethernet cable connected to that port.
