#!/bin/bash
# ==============================================================================
# Script Name: monitor_smb.sh
# Description:
#   A real-time monitoring tool for macOS that tracks the status of mounted 
#   SMB shares and Wi-Fi signal quality side-by-side.
#
#   It displays:
#   - SMB Mount Status: Mount point, connection status, protocol version, 
#     and session reconnect counts (useful for spotting drops).
#   - Wi-Fi Signal: RSSI (Signal Strength), Noise level, and SNR (Signal-to-Noise Ratio).
#
# Usage:
#   ./monitor_smb.sh
#
#   Press Ctrl+C to stop the monitoring loop.
#
# Requirements:
#   - macOS (relies on `smbutil` and the Apple80211 framework)
# ==============================================================================


INTERVAL=5  # seconds between polls
WIFI_INTERFACE="en0"  # adjust if your Wi-Fi interface is different

echo "Monitoring SMB shares and Wi-Fi signal (Ctrl+C to stop)..."

# Trap Ctrl+C to exit cleanly
trap "echo; echo 'Stopped monitoring.'; exit 0" SIGINT

while true; do
    TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
    
    # --- SMB status ---
    echo "[$TIMESTAMP] SMB status:"
    smbutil statshares -a | awk -v ts="$TIMESTAMP" '
        BEGIN {print "  Mount | Status | Protocol | Session reconnect count"}
        /^\\s*Mount Point/ {mount=$3}
        /Status/ {status=$2}
        /SMB_VERSION/ {proto=$2}
        /SESSION_RECONNECT_COUNT/ {reconnect=$2; print "  " mount " | " status " | " proto " | " reconnect}
    '

    # --- Wi-Fi info ---
    WIFI_INFO=$( /System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport -I | grep -E "agrCtlRSSI|agrCtlNoise" )
    RSSI=$(echo "$WIFI_INFO" | awk '/agrCtlRSSI/ {print $2}')
    NOISE=$(echo "$WIFI_INFO" | awk '/agrCtlNoise/ {print $2}')
    echo "[$TIMESTAMP] Wi-Fi: RSSI=${RSSI} dBm, Noise=${NOISE} dBm, SNR=$((RSSI-NOISE)) dB"
    
    echo "------------------------------------------------------------"
    sleep $INTERVAL
done
