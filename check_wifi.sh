#!/bin/bash

# ==============================================================================
# Script Name: check_wifi.sh
# Description:
#   Continuously monitors and displays the Wi-Fi Received Signal Strength 
#   Indicator (RSSI) on macOS systems. This script uses the `wdutil` diagnostic 
#   tool to fetch real-time Wi-Fi metrics.
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

echo "Starting Wi-Fi RSSI monitor (Press Ctrl+C to stop)..."
echo "-----------------------------------------------------"

while true; do
    # Run wdutil info and filter for RSSI values
    # wdutil info outputs detailed wireless diagnostics
    wdutil info | grep -E 'RSSI'
    
    # Wait for 1 second before the next update
    sleep 1
done
