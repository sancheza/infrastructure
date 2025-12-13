#!/bin/bash

# Define script metadata
SCRIPT_NAME="proxmox_net_check.sh"
VERSION="1.0.0"
DEFAULT_START=100
DEFAULT_END=130

# --- Help Function ---
show_help() {
    cat << EOF
$SCRIPT_NAME - Proxmox Container Network Status Checker

DESCRIPTION:
This utility scans the configuration files of Proxmox LXC containers (CTs)
within a specified ID range. It extracts the details of the 'net0' network 
device (the primary interface) for each container and displays the Container ID (CTID) 
alongside its raw network configuration string.

This script is specifically designed to be run directly on a **Proxmox VE host**,
as it relies on accessing the host's configuration directory: /etc/pve/lxc/.

USAGE:
  $SCRIPT_NAME [OPTIONS]

OPTIONS:
  -h, --help            Show this help message and exit.
  -v, --version         Show script version and exit.
  -s, --start <ID>      Specify the starting CTID for the range (Default: $DEFAULT_START).
  -e, --end <ID>        Specify the ending CTID for the range (Default: $DEFAULT_END).
  
EXAMPLES:
  # Check the default range (100 to 130)
  $SCRIPT_NAME

  # Check a specific range (200 to 250)
  $SCRIPT_NAME -s 200 -e 250
  
EOF
}

# --- Version Function ---
show_version() {
    echo "$SCRIPT_NAME version $VERSION"
}

# --- Argument Parsing ---
START_ID=$DEFAULT_START
END_ID=$DEFAULT_END

while [ "$#" -gt 0 ]; do
    case "$1" in
        -h|--help)
            show_help
            exit 0
            ;;
        -v|--version)
            show_version
            exit 0
            ;;
        -s|--start)
            if [ -n "$2" ] && [[ "$2" =~ ^[0-9]+$ ]]; then
                START_ID="$2"
                shift 2
            else
                echo "Error: Argument for $1 is missing or not a number." >&2
                exit 1
            fi
            ;;
        -e|--end)
            if [ -n "$2" ] && [[ "$2" =~ ^[0-9]+$ ]]; then
                END_ID="$2"
                shift 2
            else
                echo "Error: Argument for $1 is missing or not a number." >&2
                exit 1
            fi
            ;;
        *)
            echo "Error: Unknown option $1" >&2
            show_help
            exit 1
            ;;
    esac
done

# --- Validation ---
if [ "$START_ID" -gt "$END_ID" ]; then
    echo "Error: Start ID ($START_ID) cannot be greater than End ID ($END_ID)." >&2
    exit 1
fi

# --- Main Script Logic ---
# Use seq to generate the range of IDs and ensure proper iteration logic
for ID in $(seq $START_ID $END_ID); do
    CONFIG_FILE="/etc/pve/lxc/${ID}.conf"
    
    # Check if the configuration file for the CTID exists
    if [ -f "$CONFIG_FILE" ]; then
        # Use awk to search for the pattern (^net0:) and prepend the ID in one step.
        awk -v id="$ID" '/^net0:/ {print id " " $0}' "$CONFIG_FILE"
    fi
done

exit 0