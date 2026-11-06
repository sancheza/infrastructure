#!/bin/bash
# ==============================================================================
# Script Name: asus_ap_kuma_monitor.sh
# Description:
#   Monitors ASUS RT-AX1800S access points for link speed downshifts (<1 Gbps),
#   hardware switch CRC cable errors, and wireless radio status.
#   Reports status to an Uptime Kuma Push monitor URL.
#
# ADR Compliance:
#   - ADR 0005: Command-line standards (argparse, help, version, dry-run).
#   - ADR 0006: Bash shell standards.
#   - ADR 0020: Terminal logging standards (color-coded prefixes, column-aligned).
# ==============================================================================

set -euo pipefail

SCRIPT_NAME="asus_ap_kuma_monitor.sh"
VERSION="1.1.0"

# Default Configuration
DEFAULT_APS="192.168.0.2:2,192.168.0.3:3"
DEFAULT_SSH_USER="admin"
DEFAULT_SSH_KEY="${HOME}/.ssh/id_ed25519"
DEFAULT_CRC_THRESHOLD=10
DEFAULT_STATE_DIR="/tmp"

# ADR 0020 Color Palette
COLOR_RESET="\033[0m"
COLOR_INFO="\033[32m"         # Green
COLOR_WARN="\033[38;5;214m"   # 256-color Amber
COLOR_ERROR="\033[31m"        # Red

log_info() {
    printf "${COLOR_INFO}%-8s${COLOR_RESET} %s\n" "INFO" "$*"
}

log_warn() {
    printf "${COLOR_WARN}%-8s${COLOR_RESET} %s\n" "WARNING" "$*"
}

log_error() {
    printf "${COLOR_ERROR}%-8s${COLOR_RESET} %s\n" "ERROR" "$*" >&2
}

show_help() {
    cat << EOF
$SCRIPT_NAME - ASUS Access Point Health Monitor for Uptime Kuma

DESCRIPTION:
  Monitors physical Ethernet link negotiation (detects 100 Mbps downshifts),
  switch port CRC corruption counters, and Wi-Fi radio status on ASUS
  RT-AX1800S access points. Reports health status to an Uptime Kuma Push URL.

USAGE:
  $SCRIPT_NAME [OPTIONS]

OPTIONS:
  -h, --help                Show this help message and exit.
  -v, --version             Show script version and exit.
  -k, --kuma-url <URL>      Uptime Kuma Push Monitor URL.
                            (Defaults to KUMA_URL environment variable).
  -a, --aps <IP:PORT,...>   Comma-separated list of AP_IP:BACKHAUL_PORT.
                            (Default: $DEFAULT_APS).
  -u, --user <USER>         SSH username (Default: $DEFAULT_SSH_USER).
  -i, --identity <KEY>      SSH private key path (Default: auto-detect).
  -c, --crc-threshold <NUM> Maximum allowed CRC error delta between runs (Default: $DEFAULT_CRC_THRESHOLD).
  -s, --state-dir <DIR>     Directory for state tracking files (Default: $DEFAULT_STATE_DIR).
  -n, --dry-run             Run health checks without pushing to Uptime Kuma.
  -V, --verbose             Enable detailed diagnostics output.

EXAMPLES:
  # Dry-run health check from terminal
  $SCRIPT_NAME --dry-run --verbose

  # Push to Uptime Kuma
  $SCRIPT_NAME --kuma-url "http://kuma.lan:3001/api/push/TOKEN"

EOF
}

show_version() {
    echo "$SCRIPT_NAME version $VERSION"
}

# --- Argument Parsing ---
APS="$DEFAULT_APS"
SSH_USER="$DEFAULT_SSH_USER"
SSH_KEY=""
CRC_THRESHOLD="$DEFAULT_CRC_THRESHOLD"
STATE_DIR="$DEFAULT_STATE_DIR"
KUMA_URL="${KUMA_URL:-}"
DRY_RUN=false
VERBOSE=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            show_help
            exit 0
            ;;
        -v|--version)
            show_version
            exit 0
            ;;
        -k|--kuma-url)
            KUMA_URL="$2"
            shift 2
            ;;
        -a|--aps)
            APS="$2"
            shift 2
            ;;
        -u|--user)
            SSH_USER="$2"
            shift 2
            ;;
        -i|--identity)
            SSH_KEY="$2"
            shift 2
            ;;
        -c|--crc-threshold)
            CRC_THRESHOLD="$2"
            shift 2
            ;;
        -s|--state-dir)
            STATE_DIR="$2"
            shift 2
            ;;
        -n|--dry-run)
            DRY_RUN=true
            shift
            ;;
        -V|--verbose)
            VERBOSE=true
            shift
            ;;
        *)
            log_error "Unknown option: $1"
            echo "Try '$SCRIPT_NAME --help' for more information." >&2
            exit 1
            ;;
    esac
done

# Try loading KUMA_URL from .env if still empty
if [[ -z "$KUMA_URL" && "$DRY_RUN" = false ]]; then
    if [[ -f "/usr/local/bin/.env" ]]; then
        # shellcheck disable=SC1091
        source /usr/local/bin/.env
    elif [[ -f "${HOME}/.env" ]]; then
        # shellcheck disable=SC1091
        source "${HOME}/.env"
    fi
fi

# Detect SSH key if not explicitly provided
if [[ -z "$SSH_KEY" ]]; then
    if [[ -f "$DEFAULT_SSH_KEY" ]]; then
        SSH_KEY="$DEFAULT_SSH_KEY"
    elif [[ -f "${HOME}/.ssh/id_rsa" ]]; then
        SSH_KEY="${HOME}/.ssh/id_rsa"
    fi
fi

send_kuma() {
    local status="$1"
    local msg="$2"
    local ping_time="${3:-}"

    if [[ "$DRY_RUN" = true ]]; then
        log_info "[DRY-RUN] Would push to Kuma: status=$status msg=\"$msg\" ping=$ping_time"
        return 0
    fi

    if [[ -z "$KUMA_URL" ]]; then
        log_warn "KUMA_URL not configured. Skipping push notification."
        return 0
    fi

    local query="status=${status}&msg=${msg}"
    if [[ -n "$ping_time" ]]; then
        query="${query}&ping=${ping_time}"
    fi

    curl -s -G --data-urlencode "status=${status}" \
               --data-urlencode "msg=${msg}" \
               ${ping_time:+--data-urlencode "ping=${ping_time}"} \
               "$KUMA_URL" > /dev/null || log_warn "Failed to deliver push to Uptime Kuma."
}

# --- Main Inspection Workflow ---
TOTAL_ERRORS=0
FIRST_ERROR_MSG=""
START_TIME=$(date +%s%N 2>/dev/null || date +%s)

IFS="," read -ra AP_ARRAY <<< "$APS"

for ap_entry in "${AP_ARRAY[@]}"; do
    IFS=":" read -r ap_ip backhaul_port <<< "$ap_entry"

    if [[ "$VERBOSE" = true ]]; then
        log_info "Inspecting AP at ${ap_ip} (monitored backhaul port: ${backhaul_port})..."
    fi

    # 1. SSH Reachability & Switch Status
    SSH_OPTS=(-o ConnectTimeout=5 -o BatchMode=yes -o StrictHostKeyChecking=accept-new)
    if [[ -n "$SSH_KEY" ]]; then
        SSH_OPTS+=(-i "$SSH_KEY")
    fi

    REMOTE_SCRIPT="swconfig dev switch0 show; echo '===WIFI==='; iwconfig ra0 2>/dev/null; iwconfig rai0 2>/dev/null; iwconfig ra1 2>/dev/null; iwconfig rai1 2>/dev/null; echo '===STATS==='; iwpriv rai0 stat 2>/dev/null; iwpriv rai1 stat 2>/dev/null"
    
    # shellcheck disable=SC2029  # REMOTE_SCRIPT is intended to expand on the client side
    if ! raw_output=$(ssh "${SSH_OPTS[@]}" "${SSH_USER}@${ap_ip}" "$REMOTE_SCRIPT" 2>&1); then
        log_error "AP ${ap_ip} is unreachable via SSH."
        TOTAL_ERRORS=$((TOTAL_ERRORS + 1))
        [[ -z "$FIRST_ERROR_MSG" ]] && FIRST_ERROR_MSG="AP_${ap_ip}_SSH_Unreachable"
        continue
    fi

    # 2. Check Backhaul Port Link Speed
    port_link=$(echo "$raw_output" | grep "link: port:${backhaul_port}" | head -n 1 || true)
    if [[ -z "$port_link" ]]; then
        log_error "AP ${ap_ip}: Port ${backhaul_port} not found in switch output."
        TOTAL_ERRORS=$((TOTAL_ERRORS + 1))
        [[ -z "$FIRST_ERROR_MSG" ]] && FIRST_ERROR_MSG="AP_${ap_ip}_Port${backhaul_port}_NotFound"
    elif [[ "$port_link" == *"link:down"* ]]; then
        log_error "AP ${ap_ip}: Backhaul port ${backhaul_port} is DOWN."
        TOTAL_ERRORS=$((TOTAL_ERRORS + 1))
        [[ -z "$FIRST_ERROR_MSG" ]] && FIRST_ERROR_MSG="AP_${ap_ip}_Port${backhaul_port}_Down"
    elif [[ "$port_link" != *"speed:1000baseT"* ]]; then
        # Capture negotiated speed (e.g. 100baseT or 10baseT)
        negotiated_speed=$(echo "$port_link" | sed -n 's/.*speed:\([^ ]*\).*/\1/p')
        log_error "AP ${ap_ip}: Port ${backhaul_port} downshifted to ${negotiated_speed} (expected 1000baseT)!"
        TOTAL_ERRORS=$((TOTAL_ERRORS + 1))
        [[ -z "$FIRST_ERROR_MSG" ]] && FIRST_ERROR_MSG="AP_${ap_ip}_Port${backhaul_port}_${negotiated_speed}"
    else
        if [[ "$VERBOSE" = true ]]; then
            log_info "AP ${ap_ip}: Port ${backhaul_port} verified at 1000baseT full-duplex."
        fi
    fi

    # 3. Check Hardware MIB CRC Counters (Delta Tracking)
    rx_crc=$(echo "$raw_output" | awk "/Port ${backhaul_port} MIB counters/,/Port $((backhaul_port + 1)) MIB counters/" | grep -E '^RxCRC' | awk '{print $3}' | head -n 1 || echo 0)
    tx_crc=$(echo "$raw_output" | awk "/Port ${backhaul_port} MIB counters/,/Port $((backhaul_port + 1)) MIB counters/" | grep -E '^TxCRC' | awk '{print $3}' | head -n 1 || echo 0)
    rx_crc=${rx_crc:-0}
    tx_crc=${tx_crc:-0}

    state_file="${STATE_DIR}/asus_ap_crc_${ap_ip//./_}_port${backhaul_port}.state"
    prev_rx_crc=0
    prev_tx_crc=0

    if [[ -f "$state_file" ]]; then
        read -r prev_rx_crc prev_tx_crc < "$state_file" 2>/dev/null || { prev_rx_crc=0; prev_tx_crc=0; }
        [[ "$prev_rx_crc" =~ ^[0-9]+$ ]] || prev_rx_crc=0
        [[ "$prev_tx_crc" =~ ^[0-9]+$ ]] || prev_tx_crc=0
    else
        # Initialize baseline on first observation
        prev_rx_crc="$rx_crc"
        prev_tx_crc="$tx_crc"
    fi

    # Detect counter resets (e.g. AP reboot)
    if [[ "$rx_crc" -lt "$prev_rx_crc" ]]; then
        delta_rx="$rx_crc"
    else
        delta_rx=$((rx_crc - prev_rx_crc))
    fi

    if [[ "$tx_crc" -lt "$prev_tx_crc" ]]; then
        delta_tx="$tx_crc"
    else
        delta_tx=$((tx_crc - prev_tx_crc))
    fi

    # Update state file when not in dry-run mode
    if [[ "$DRY_RUN" = false ]]; then
        mkdir -p "$STATE_DIR"
        printf "%s %s\n" "$rx_crc" "$tx_crc" > "$state_file"
    fi

    if [[ "$delta_rx" -gt "$CRC_THRESHOLD" || "$delta_tx" -gt "$CRC_THRESHOLD" ]]; then
        log_warn "AP ${ap_ip}: Elevated switch CRC errors on port ${backhaul_port} (Delta RxCRC: ${delta_rx}, Delta TxCRC: ${delta_tx}, Cumulative Rx: ${rx_crc}, Cumulative Tx: ${tx_crc})."
        TOTAL_ERRORS=$((TOTAL_ERRORS + 1))
        [[ -z "$FIRST_ERROR_MSG" ]] && FIRST_ERROR_MSG="AP_${ap_ip}_Port${backhaul_port}_CRC_Delta_Rx${delta_rx}_Tx${delta_tx}"
    elif [[ "$VERBOSE" = true ]]; then
        log_info "AP ${ap_ip}: Port ${backhaul_port} CRC errors within limits (Delta Rx: ${delta_rx}, Delta Tx: ${delta_tx}, Cumulative Rx: ${rx_crc}, Cumulative Tx: ${tx_crc})."
    fi

    # 4. Check Wi-Fi SSID Presence
    if ! echo "$raw_output" | grep -q 'ESSID:"Cooper"'; then
        log_error "AP ${ap_ip}: 2.4 GHz SSID 'Cooper' not broadcasting."
        TOTAL_ERRORS=$((TOTAL_ERRORS + 1))
        [[ -z "$FIRST_ERROR_MSG" ]] && FIRST_ERROR_MSG="AP_${ap_ip}_SSID_Cooper_Down"
    elif [[ "$VERBOSE" = true ]]; then
        log_info "AP ${ap_ip}: 2.4 GHz SSID 'Cooper' broadcasting."
    fi

    if ! echo "$raw_output" | grep -q 'ESSID:"Cooper5"'; then
        log_error "AP ${ap_ip}: 5 GHz SSID 'Cooper5' not broadcasting."
        TOTAL_ERRORS=$((TOTAL_ERRORS + 1))
        [[ -z "$FIRST_ERROR_MSG" ]] && FIRST_ERROR_MSG="AP_${ap_ip}_SSID_Cooper5_Down"
    elif [[ "$VERBOSE" = true ]]; then
        log_info "AP ${ap_ip}: 5 GHz SSID 'Cooper5' broadcasting."
    fi
done

# Calculate execution duration (in ms) for Uptime Kuma ping metric
END_TIME=$(date +%s%N 2>/dev/null || date +%s)
DURATION_MS=0
if [[ "$START_TIME" =~ ^[0-9]{19}$ && "$END_TIME" =~ ^[0-9]{19}$ ]]; then
    DURATION_MS=$(( (END_TIME - START_TIME) / 1000000 ))
fi

# --- Report Outcome ---
if [[ "$TOTAL_ERRORS" -gt 0 ]]; then
    log_error "Health check completed with ${TOTAL_ERRORS} issue(s). Reason: ${FIRST_ERROR_MSG}"
    send_kuma "down" "$FIRST_ERROR_MSG" "$DURATION_MS"
    exit 1
else
    log_info "All APs healthy: all backhaul links 1 Gbps, 0 critical CRC errors, all SSIDs broadcasting."
    send_kuma "up" "OK_All_APs_1Gbps" "$DURATION_MS"
    exit 0
fi
