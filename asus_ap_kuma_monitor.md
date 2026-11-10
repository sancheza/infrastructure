# ASUS Access Point Health Monitor

Health monitoring script for ASUS RT-AX1800S access points in standalone Access Point (AP) mode, reporting push heartbeats to Uptime Kuma.

---

## 1. Monitored Devices

The monitor validates physical link negotiation, switch hardware CRC error rates, and wireless radio status on each access point over SSH.

| Item | Setting | Example |
| :--- | :--- | :--- |
| Access points and the switch port each uses for its wired link | `ASUS_AP_LIST` | `192.168.0.21:2,192.168.0.22:3` |
| SSID expected on 2.4 GHz | `ASUS_AP_SSID_24` | `ExampleNet` |
| SSID expected on 5 GHz | `ASUS_AP_SSID_5` | `ExampleNet5` |
| Expected link speed | Fixed | `1000baseT` full duplex |

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
   Executes `iwconfig` across interfaces (`ra0`, `rai0`, `ra1`, `rai1`) and verifies that the 2.4 GHz SSID (`ASUS_AP_SSID_24`) and the 5 GHz SSID (`ASUS_AP_SSID_5`) are both broadcasting.

---

## 3. Configuration

**Starting Host & Directory:** the monitoring host, in the directory containing `asus_ap_kuma_monitor.sh`.

The script has no built-in access point list or SSIDs. Each value comes from a command-line option, an environment variable, or a `.env` file.

| Setting | `.env` key / environment variable | Option | Required | Default |
| :--- | :--- | :--- | :--- | :--- |
| Access points | `ASUS_AP_LIST` | `-a, --aps` | Yes | None |
| 2.4 GHz SSID | `ASUS_AP_SSID_24` | `--ssid-24` | Yes | None |
| 5 GHz SSID | `ASUS_AP_SSID_5` | `--ssid-5` | Yes | None |
| Uptime Kuma push URL | `KUMA_URL` | `-k, --kuma-url` | For live runs | None |
| SSH user | `ASUS_AP_SSH_USER` | `-u, --user` | No | `admin` |

**Precedence:** command-line option, then environment variable, then `.env` file.

**`.env` lookup:** the first existing file of `<directory of the real script file>/.env`, `/usr/local/bin/.env`, `~/.env`. `-e, --env-file <FILE>` selects a specific file instead. The script follows symlinks to find its own directory, so a `.env` next to the git checkout is found when the script runs through a symlink in `~/.local/bin`.

> [!NOTE]
> The `.env` file is read as `KEY=VALUE` lines and never executed. Only the keys in the table above are used.

### Create the `.env` file

```bash
cp .env.example .env
```

Fill in the monitor section:

```bash
ASUS_AP_LIST=192.168.0.21:2,192.168.0.22:3
ASUS_AP_SSID_24=ExampleNet
ASUS_AP_SSID_5=ExampleNet5
ASUS_AP_SSH_USER=admin
KUMA_URL=http://kuma.lan:3001/api/push/TOKEN
```

> [!IMPORTANT]
> `.env` is listed in `.gitignore`. It holds the push token, so it is never committed.

Expected result when a setting is missing:

```text
ERROR    Missing setting: access points (ASUS_AP_LIST or --aps)
Set them in a .env file (see .env.example) or pass the options. Try 'asus_ap_kuma_monitor.sh --help'.
```

---

## 4. Execution Schedule & Intervals

* **Host**: the monitoring host
* **Schedule Interval**: Every 60 seconds via `cron`
* **Uptime Kuma Monitor**: a Push monitor; its URL is `KUMA_URL`

### Crontab Entry

```crontab
* * * * * /home/<user>/.local/bin/asus_ap_kuma_monitor.sh > /dev/null 2>&1
```

---

## 5. Files & Paths

| Path | Purpose |
| :--- | :--- |
| `/home/<user>/infrastructure/asus_ap_kuma_monitor.sh` | Git-managed source script |
| `/home/<user>/.local/bin/asus_ap_kuma_monitor.sh` | Symlink to git source in PATH |
| `/home/<user>/infrastructure/.env` | Settings for this host (not committed) |
| `/home/<user>/infrastructure/asus_ap_kuma_monitor.md` | Technical documentation |
| `/tmp/asus_ap_crc_<IP>_port<PORT>.state` | CRC counter delta tracking state files |
| `~/.ssh/id_rsa` / `~/.ssh/id_ed25519` | SSH keys for the access points |

---

## 6. Command-Line Options

```
asus_ap_kuma_monitor.sh [OPTIONS]
```

| Option | Argument | Description | Default |
| :--- | :--- | :--- | :--- |
| `-h, --help` | None | Show usage information and exit | N/A |
| `-v, --version` | None | Show version string and exit | `1.2.0` |
| `-e, --env-file` | `<FILE>` | Read settings from this file | First existing `.env` (see section 3) |
| `-k, --kuma-url` | `<URL>` | Uptime Kuma Push Monitor endpoint URL | `KUMA_URL` |
| `-a, --aps` | `<IP:PORT,...>` | Comma-separated list of `AP_IP:BACKHAUL_PORT` | `ASUS_AP_LIST` |
| `--ssid-24` | `<NAME>` | 2.4 GHz SSID expected on every access point | `ASUS_AP_SSID_24` |
| `--ssid-5` | `<NAME>` | 5 GHz SSID expected on every access point | `ASUS_AP_SSID_5` |
| `-u, --user` | `<USER>` | SSH username for access points | `ASUS_AP_SSH_USER`, else `admin` |
| `-i, --identity` | `<KEY>` | SSH private key file path | Auto-detect (`~/.ssh/id_ed25519`, `~/.ssh/id_rsa`) |
| `-c, --crc-threshold` | `<NUM>` | Maximum allowed CRC error delta between runs | `10` |
| `-s, --state-dir` | `<DIR>` | Directory for state tracking files | `/tmp` |
| `-n, --dry-run` | None | Perform inspection without pushing to Uptime Kuma | `false` |
| `-V, --verbose` | None | Output detailed diagnostic logging to stdout | `false` |

---

## 7. Manual Execution & Verification

### Dry-Run Verification

Run diagnostic check without updating persistent state or pushing metrics to Uptime Kuma:

```bash
/home/<user>/.local/bin/asus_ap_kuma_monitor.sh --dry-run --verbose
```

Expected output:
```text
INFO     Inspecting AP at 192.168.0.21 (monitored backhaul port: 2)...
INFO     AP 192.168.0.21: Port 2 verified at 1000baseT full-duplex.
INFO     AP 192.168.0.21: Port 2 CRC errors within limits (Delta Rx: 0, Delta Tx: 0, Cumulative Rx: 0, Cumulative Tx: 0).
INFO     AP 192.168.0.21: 2.4 GHz SSID 'ExampleNet' broadcasting.
INFO     AP 192.168.0.21: 5 GHz SSID 'ExampleNet5' broadcasting.
INFO     Inspecting AP at 192.168.0.22 (monitored backhaul port: 3)...
INFO     AP 192.168.0.22: Port 3 verified at 1000baseT full-duplex.
INFO     AP 192.168.0.22: Port 3 CRC errors within limits (Delta Rx: 0, Delta Tx: 0, Cumulative Rx: 0, Cumulative Tx: 0).
INFO     AP 192.168.0.22: 2.4 GHz SSID 'ExampleNet' broadcasting.
INFO     AP 192.168.0.22: 5 GHz SSID 'ExampleNet5' broadcasting.
INFO     All APs healthy: all backhaul links 1 Gbps, 0 critical CRC errors, all SSIDs broadcasting.
INFO     [DRY-RUN] Would push to Kuma: status=up msg="OK_All_APs_1Gbps" ping=...
```

### Live Run

```bash
/home/<user>/.local/bin/asus_ap_kuma_monitor.sh
```

---

## 8. Troubleshooting

| Symptom | Cause | Fix |
| :--- | :--- | :--- |
| `Missing setting: ...` and exit 1 | `ASUS_AP_LIST`, `ASUS_AP_SSID_24`, or `ASUS_AP_SSID_5` is empty in every source | Set the value in `.env` or pass the option (section 3) |
| `Env file not found: <path>` | `-e, --env-file` points to a missing file | Correct the path |
| `KUMA_URL not configured. Skipping push notification.` | No push URL in `.env`, environment, or `--kuma-url` | Set `KUMA_URL` |
| Push message `AP_<IP>_SSID_2.4GHz_Down` or `AP_<IP>_SSID_5GHz_Down` | The access point is not broadcasting the configured SSID, or the configured SSID is spelled differently from the broadcast name | Compare `ASUS_AP_SSID_24` / `ASUS_AP_SSID_5` with the SSID shown in the access point's admin page |

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
  1. Inspect state file on the monitoring host to see delta progression:
     ```bash
     cat /tmp/asus_ap_crc_<IP_UNDERSCORES>_port<PORT>.state
     ```
  2. Query switch MIB counter directly:
     ```bash
     ssh admin@<AP_IP> "swconfig dev switch0 port <PORT> get mib"
     ```
  3. If delta continues increasing on every check, replace the Ethernet cable connected to that port.
