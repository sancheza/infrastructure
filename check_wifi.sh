#!/bin/bash

# ==============================================================================
# Script Name: check_wifi.sh
# Description:
#   Continuously monitors and displays Wi-Fi signal strength (RSSI, in dBm)
#   as a live, in-place dashboard (not scrolling rows), with running session
#   stats (min / max / mean / median / std dev) and a color-coded quality key.
#
#   Works on:
#     - macOS   : via `wdutil info` (requires sudo). Real dBm.
#     - Linux   : via `iw` or `iwconfig` (no sudo needed). Real dBm.
#                 Falls back to `nmcli` (no sudo needed) if neither is
#                 installed, which only reports a 0-100% signal quality;
#                 that gets converted to an *approximate* dBm.
#     - Windows : via Git Bash, MSYS2, Cygwin, or WSL, by shelling out to
#                 `netsh.exe wlan show interfaces`. Native cmd.exe/PowerShell
#                 cannot run this script directly - bash has to come from
#                 one of those environments. Windows only exposes a 0-100%
#                 signal quality (no true dBm), so the value shown is an
#                 *approximate* conversion. Assumes an English-language
#                 Windows install (parses the "Signal" label in netsh output).
#
#   Approximate readings (Windows, and the Linux nmcli fallback) are marked
#   with "~" next to the value, since they're derived from a 0-100% signal
#   quality reading rather than a direct radio dBm measurement.
#
#   Signal quality reference (RSSI is in dBm; values closer to 0 are better):
#     Excellent : >= -50 dBm         - as strong as it gets
#     Good      : -50 to -60 dBm     - strong, reliable connection
#     Fair      : -60 to -67 dBm     - usable, but may see slowdowns
#     Poor      : -67 to -75 dBm     - noticeable drops / slow speeds
#     Bad       : <  -75 dBm         - barely usable / frequent drops
#   (Std Dev is colored on stability instead: <3 dBm green, 3-7 dBm yellow, >7 dBm red)
#
#   Useful for:
#   - Troubleshooting Wi-Fi dead zones.
#   - Aligning antennas or positioning routers.
#   - Monitoring signal stability over time.
#
# Usage:
#   macOS:         sudo ./check_wifi.sh
#   Linux/Windows: ./check_wifi.sh   (no sudo needed)
#
# Requirements:
#   - macOS (root) with wdutil, OR
#   - Linux with iw, iwconfig, or nmcli, OR
#   - Windows, run from WSL, Git Bash, MSYS2, or Cygwin, with netsh.exe
#     reachable (either on PATH or at /mnt/c/Windows/System32/netsh.exe).
#
# ==============================================================================

detect_platform() {
    case "$(uname -s)" in
        Darwin) echo "macos" ;;
        Linux)
            if grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null; then
                echo "wsl"
            else
                echo "linux"
            fi
            ;;
        MINGW*|MSYS*|CYGWIN*) echo "windows" ;;
        *) echo "unknown" ;;
    esac
}

PLATFORM=$(detect_platform)
WIFI_IFACE=""
NETSH_BIN=""
PLATFORM_LABEL=""
SOURCE_IS_APPROX=0

case "$PLATFORM" in
    macos)
        if [ "$EUID" -ne 0 ]; then
            echo "Error: This script must be run as root on macOS (wdutil requires it)."
            echo "Please use: sudo $0"
            exit 1
        fi
        if ! command -v wdutil >/dev/null 2>&1; then
            echo "Error: 'wdutil' not found (expected on macOS)."
            exit 1
        fi
        PLATFORM_LABEL="macOS (wdutil)"
        ;;
    linux)
        if command -v iw >/dev/null 2>&1; then
            WIFI_IFACE=$(iw dev 2>/dev/null | awk '$1=="Interface"{print $2; exit}')
        fi
        if [[ -z "$WIFI_IFACE" ]]; then
            for d in /sys/class/net/*/wireless; do
                [[ -d "$d" ]] || continue
                WIFI_IFACE=$(basename "$(dirname "$d")")
                break
            done
        fi
        if command -v iw >/dev/null 2>&1 && [[ -n "$WIFI_IFACE" ]]; then
            PLATFORM_LABEL="Linux (iw, $WIFI_IFACE)"
        elif command -v iwconfig >/dev/null 2>&1 && [[ -n "$WIFI_IFACE" ]]; then
            PLATFORM_LABEL="Linux (iwconfig, $WIFI_IFACE)"
        elif command -v nmcli >/dev/null 2>&1; then
            PLATFORM_LABEL="Linux (nmcli, ~approx. from signal %)"
            SOURCE_IS_APPROX=1
        else
            echo "Error: No Wi-Fi tool found. Install one of: iw, iwconfig (wireless-tools), or nmcli (NetworkManager)."
            echo "  Debian/Ubuntu: sudo apt install iw"
            echo "  Fedora:        sudo dnf install iw"
            exit 1
        fi
        ;;
    wsl|windows)
        if command -v netsh.exe >/dev/null 2>&1; then
            NETSH_BIN="netsh.exe"
        elif [[ -x /mnt/c/Windows/System32/netsh.exe ]]; then
            NETSH_BIN="/mnt/c/Windows/System32/netsh.exe"
        else
            echo "Error: 'netsh.exe' not found. This script needs Windows' netsh to read Wi-Fi signal."
            exit 1
        fi
        if [[ "$PLATFORM" == "wsl" ]]; then
            PLATFORM_LABEL="Windows via WSL (netsh, ~approx. from signal %)"
        else
            PLATFORM_LABEL="Windows via Git Bash/MSYS/Cygwin (netsh, ~approx. from signal %)"
        fi
        SOURCE_IS_APPROX=1
        ;;
    *)
        echo "Error: Unsupported platform ($(uname -s))."
        echo "This script supports macOS, Linux, and Windows (via WSL, Git Bash, MSYS2, or Cygwin)."
        exit 1
        ;;
esac

# --- Signal thresholds (dBm) - tune to taste ---
RSSI_EXCELLENT=-50   # >= this is "Excellent"
RSSI_GOOD=-60        # >= this (and < EXCELLENT) is "Good"
RSSI_FAIR=-67         # >= this (and < GOOD) is "Fair"
RSSI_POOR=-75         # >= this (and < FAIR) is "Poor"; below this is "Bad"

# --- Stability thresholds (std dev, dBm) ---
STDDEV_GOOD=3   # std dev < this is stable (green)
STDDEV_FAIR=7   # std dev < this (and >= STDDEV_GOOD) is somewhat stable (yellow); above is unstable (red)

# --- Colors ---
BRIGHT_GREEN='\033[1;32m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
BRIGHT_RED='\033[1;31m'
BOLD='\033[1m'
RESET='\033[0m'

readings=()

# Prints the ANSI color code for a given RSSI value (higher/less negative = better)
color_for_rssi() {
    local rssi=$1
    if (( rssi >= RSSI_EXCELLENT )); then
        printf '%s' "$BRIGHT_GREEN"
    elif (( rssi >= RSSI_GOOD )); then
        printf '%s' "$GREEN"
    elif (( rssi >= RSSI_FAIR )); then
        printf '%s' "$YELLOW"
    elif (( rssi >= RSSI_POOR )); then
        printf '%s' "$RED"
    else
        printf '%s' "$BRIGHT_RED"
    fi
}

# Prints the ANSI color code for a std dev value (lower = more stable = better)
color_for_stddev() {
    local sd=$1
    local sd_int=${sd%.*}
    if (( sd_int < STDDEV_GOOD )); then
        printf '%s' "$GREEN"
    elif (( sd_int < STDDEV_FAIR )); then
        printf '%s' "$YELLOW"
    else
        printf '%s' "$RED"
    fi
}

rating_for_rssi() {
    local rssi=$1
    if (( rssi >= RSSI_EXCELLENT )); then
        printf 'Excellent'
    elif (( rssi >= RSSI_GOOD )); then
        printf 'Good'
    elif (( rssi >= RSSI_FAIR )); then
        printf 'Fair'
    elif (( rssi >= RSSI_POOR )); then
        printf 'Poor'
    else
        printf 'Bad'
    fi
}

# Fetches one RSSI reading (dBm, integer) for the detected platform.
# Whether the value is a direct radio dBm reading or an approximation from a
# 0-100% signal quality reading is fixed per-platform in SOURCE_IS_APPROX
# (set once during preflight) - not tracked here, since this function is
# always invoked as `rssi=$(get_rssi)`, and a command-substitution subshell
# can't hand a variable assignment back to the caller.
get_rssi() {
    local val pct

    case "$PLATFORM" in
        macos)
            val=$(wdutil info 2>/dev/null | grep -m1 -E 'RSSI' | grep -oE -- '-?[0-9]+' | head -1)
            ;;
        linux)
            if command -v iw >/dev/null 2>&1 && [[ -n "$WIFI_IFACE" ]]; then
                val=$(iw dev "$WIFI_IFACE" link 2>/dev/null | awk '/signal:/{print $2; exit}')
            fi
            if [[ -z "$val" ]] && command -v iwconfig >/dev/null 2>&1 && [[ -n "$WIFI_IFACE" ]]; then
                val=$(iwconfig "$WIFI_IFACE" 2>/dev/null | grep -oE 'Signal level=-?[0-9]+' | grep -oE -- '-?[0-9]+')
            fi
            if [[ -z "$val" ]] && command -v nmcli >/dev/null 2>&1; then
                pct=$(nmcli -t -f active,signal dev wifi 2>/dev/null | awk -F: '$1=="yes"{print $2; exit}')
                [[ -n "$pct" ]] && val=$(( pct / 2 - 100 ))
            fi
            ;;
        wsl|windows)
            pct=$("$NETSH_BIN" wlan show interfaces 2>/dev/null | grep -m1 -E 'Signal' | grep -oE '[0-9]+%' | tr -d '%')
            [[ -n "$pct" ]] && val=$(( pct / 2 - 100 ))
            ;;
    esac

    printf '%s' "$val"
}

# Computes "min max mean median stddev" from the readings array.
# Sorts externally with `sort -n` since the default macOS /usr/bin/awk
# has no built-in sort function (that's a gawk-only extension).
compute_stats() {
    printf '%s\n' "${readings[@]}" | sort -n | awk '
        { vals[NR] = $1; sum += $1; sumsq += $1 * $1; n++ }
        END {
            if (n == 0) { print "0 0 0 0 0"; exit }
            min = vals[1]; max = vals[n]
            mean = sum / n
            if (n % 2 == 1) { median = vals[(n + 1) / 2] }
            else { median = (vals[n / 2] + vals[n / 2 + 1]) / 2 }
            variance = (sumsq / n) - (mean * mean)
            if (variance < 0) variance = 0
            stddev = sqrt(variance)
            printf "%d %d %.1f %.1f %.1f\n", min, max, mean, median, stddev
        }'
}

cleanup() {
    tput cnorm  # restore cursor
    echo
    exit 0
}
trap cleanup INT TERM

tput civis  # hide cursor
clear

# --- Static content (plain text, used both to print and to size the separators) ---
TITLE="Wi-Fi RSSI Monitor (Press Ctrl+C to stop)"
PLATFORM_LINE="Platform: $PLATFORM_LABEL"
KEY_HEADING="Key (signal quality by RSSI):"
DESC_LINES=(
    "RSSI (Received Signal Strength Indicator) measures how strong the Wi-Fi"
    "signal is at this device, in dBm. It is a negative number, and closer to"
    "zero is better (e.g. -40 is stronger than -80). It gets worse with:"
    "  distance from the router/AP, walls/floors/metal or concrete obstructions,"
    "  interference from other Wi-Fi networks, microwaves, Bluetooth, and"
    "  cordless phones, and a weak/older antenna or crowded Wi-Fi channel."
    "To improve it: move closer, reduce obstructions, switch channels/bands,"
    "or add an access point/mesh node."
)
if [[ "$SOURCE_IS_APPROX" -eq 1 ]]; then
    DESC_LINES+=(
        "Note: this platform only exposes signal quality as a percentage, so"
        "values marked with '~' are an approximate dBm, not a direct radio reading."
    )
fi
CAT_NAME=("Excellent" "Good" "Fair" "Poor" "Bad")
CAT_RANGE=(">= -50 dBm" "-50 to -60 dBm" "-60 to -67 dBm" "-67 to -75 dBm" "<  -75 dBm")
CAT_DESC=("as strong as it gets" "strong, reliable" "usable, may see slowdowns" "noticeable drops" "barely usable")
CAT_COLOR=("$BRIGHT_GREEN" "$GREEN" "$YELLOW" "$RED" "$BRIGHT_RED")

KEY_LINES=()
for i in "${!CAT_NAME[@]}"; do
    printf -v line "  %-10s %-15s - %s" "${CAT_NAME[$i]}" "${CAT_RANGE[$i]}" "${CAT_DESC[$i]}"
    KEY_LINES+=("$line")
done

# Worst-case renderings of the live dashboard lines below (3-digit dBm, decimals,
# approx marker), so the separators are wide enough even before real readings come in.
SAMPLE_CURRENT_LINE="Current RSSI: ~-100 dBm (Excellent, approx.)"
SAMPLE_STATS_LINE="Min: -100 dBm  Max: -100 dBm  Mean: -100.0 dBm  Median: -100.0 dBm  StdDev: 100.0 dBm"

# Size the separator lines to the widest line of actual content, rather than a fixed width.
WIDTH=${#TITLE}
for line in "$PLATFORM_LINE" "${DESC_LINES[@]}" "$KEY_HEADING" "${KEY_LINES[@]}" "$SAMPLE_CURRENT_LINE" "$SAMPLE_STATS_LINE"; do
    (( ${#line} > WIDTH )) && WIDTH=${#line}
done
SEP_EQ=$(printf '=%.0s' $(seq 1 "$WIDTH"))
SEP_DASH=$(printf -- '-%.0s' $(seq 1 "$WIDTH"))

# --- Print the static header + Key once; the live dashboard redraws below it ---
STATIC_LINE_COUNT=0
print_static_line() {
    echo "$1"
    STATIC_LINE_COUNT=$((STATIC_LINE_COUNT + 1))
}

print_static_line "$TITLE"
print_static_line "$PLATFORM_LINE"
print_static_line "$SEP_EQ"
for line in "${DESC_LINES[@]}"; do
    print_static_line "$line"
done
print_static_line "$SEP_EQ"
print_static_line "$KEY_HEADING"
for i in "${!CAT_NAME[@]}"; do
    printf "  ${CAT_COLOR[$i]}%-10s${RESET} %-15s - %s\n" "${CAT_NAME[$i]}" "${CAT_RANGE[$i]}" "${CAT_DESC[$i]}"
    STATIC_LINE_COUNT=$((STATIC_LINE_COUNT + 1))
done
print_static_line "$SEP_EQ"
DASHBOARD_ROW=$STATIC_LINE_COUNT  # live region starts right after the static header

while true; do
    rssi=$(get_rssi)
    approx=$SOURCE_IS_APPROX

    if [[ -n "$rssi" ]]; then
        readings+=("$rssi")
    fi

    read -r min max mean median stddev <<< "$(compute_stats)"

    tput cup "$DASHBOARD_ROW" 0
    tput el
    if [[ -n "$rssi" ]]; then
        color=$(color_for_rssi "$rssi")
        rating=$(rating_for_rssi "$rssi")
        if [[ "$approx" -eq 1 ]]; then
            printf "Current RSSI: ${color}${BOLD}~%d dBm (%s, approx.)${RESET}\n" "$rssi" "$rating"
        else
            printf "Current RSSI: ${color}${BOLD}%d dBm (%s)${RESET}\n" "$rssi" "$rating"
        fi
    else
        printf "Current RSSI: n/a (waiting for data...)\n"
    fi
    tput el
    echo "$SEP_DASH"
    tput el
    printf "Samples: %d\n" "${#readings[@]}"
    tput el

    if [[ -n "$rssi" ]]; then
        min_c=$(color_for_rssi "$min")
        max_c=$(color_for_rssi "$max")
        mean_c=$(color_for_rssi "${mean%.*}")
        median_c=$(color_for_rssi "${median%.*}")
        sd_c=$(color_for_stddev "$stddev")
        printf "Min: ${min_c}%d dBm${RESET}  Max: ${max_c}%d dBm${RESET}  Mean: ${mean_c}%s dBm${RESET}  Median: ${median_c}%s dBm${RESET}  StdDev: ${sd_c}%s dBm${RESET}\n" \
            "$min" "$max" "$mean" "$median" "$stddev"
    else
        printf "Min: --  Max: --  Mean: --  Median: --  StdDev: --\n"
    fi
    tput el
    echo "$SEP_EQ"
    tput el

    sleep 1
done
