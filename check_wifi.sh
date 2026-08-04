#!/bin/bash

# ==============================================================================
# Script Name: check_wifi.sh
# Description:
#   Continuously monitors and displays the Wi-Fi Received Signal Strength
#   Indicator (RSSI) on macOS systems, updating a live dashboard in place
#   (rather than scrolling), along with running session statistics
#   (min / max / mean / median / std dev). This script uses the `wdutil`
#   diagnostic tool to fetch real-time Wi-Fi metrics.
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
#   sudo ./check_wifi.sh
#
#   Note: This script requires `sudo` privileges because `wdutil` interacts
#   directly with the wireless diagnostics subsystem.
#
# Requirements:
#   - macOS
#   - Root/Sudo privileges
#
# ==============================================================================

# Ensure the script is run with sudo
if [ "$EUID" -ne 0 ]; then
  echo "Error: This script must be run as root. Please use 'sudo $0'."
  exit 1
fi

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
CAT_NAME=("Excellent" "Good" "Fair" "Poor" "Bad")
CAT_RANGE=(">= -50 dBm" "-50 to -60 dBm" "-60 to -67 dBm" "-67 to -75 dBm" "<  -75 dBm")
CAT_DESC=("as strong as it gets" "strong, reliable" "usable, may see slowdowns" "noticeable drops" "barely usable")
CAT_COLOR=("$BRIGHT_GREEN" "$GREEN" "$YELLOW" "$RED" "$BRIGHT_RED")

KEY_LINES=()
for i in "${!CAT_NAME[@]}"; do
    printf -v line "  %-10s %-15s - %s" "${CAT_NAME[$i]}" "${CAT_RANGE[$i]}" "${CAT_DESC[$i]}"
    KEY_LINES+=("$line")
done

# Worst-case renderings of the live dashboard lines below (3-digit dBm, decimals),
# so the separators are wide enough even before real readings come in.
SAMPLE_CURRENT_LINE="Current RSSI: -100 dBm (Excellent)"
SAMPLE_STATS_LINE="Min: -100 dBm  Max: -100 dBm  Mean: -100.0 dBm  Median: -100.0 dBm  StdDev: 100.0 dBm"

# Size the separator lines to the widest line of actual content, rather than a fixed width.
WIDTH=${#TITLE}
for line in "${DESC_LINES[@]}" "$KEY_HEADING" "${KEY_LINES[@]}" "$SAMPLE_CURRENT_LINE" "$SAMPLE_STATS_LINE"; do
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
    rssi_line=$(wdutil info | grep -m1 -E 'RSSI')
    rssi=$(printf '%s' "$rssi_line" | grep -oE -- '-?[0-9]+' | head -1)

    if [[ -n "$rssi" ]]; then
        readings+=("$rssi")
    fi

    read -r min max mean median stddev <<< "$(compute_stats)"

    tput cup "$DASHBOARD_ROW" 0
    tput el
    if [[ -n "$rssi" ]]; then
        color=$(color_for_rssi "$rssi")
        rating=$(rating_for_rssi "$rssi")
        printf "Current RSSI: ${color}${BOLD}%d dBm (%s)${RESET}\n" "$rssi" "$rating"
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
