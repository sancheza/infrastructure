#!/bin/bash

# script to monitor tvheadend and send alerts to uptime kuma
# Be sure to have a .env file in /usr/local/bin with the entry KUMA_URL=http://[IP_ADDRESS]:3001/api/push/[KEY]

# 1. Manual Environment Load
if [ -f "/usr/local/bin/.env" ]; then
    set -a
    # shellcheck disable=SC1091  # intentionally untracked, host-specific secrets file
    source /usr/local/bin/.env
    set +a
fi

# 2. Safety Check
if [ -z "$KUMA_URL" ]; then
    logger -t tvh-monitor "CRITICAL: KUMA_URL not found in /usr/local/bin/.env"
    exit 1
fi

# 3. Clear old locks on startup
rm -f /tmp/tvh_error_active

# 4. Heartbeat Loop (Background)
(
  while true; do
    if [ ! -f /tmp/tvh_error_active ]; then
        curl -s -G --data-urlencode "status=up" --data-urlencode "msg=Service_Running" "$KUMA_URL" > /dev/null
    fi
    sleep 50
  done
) &

# 5. Monitoring Loop
stdbuf -oL journalctl SYSLOG_IDENTIFIER=tvheadend -p 0..3 -f -n0 --no-pager | while read -r line; do
    
    # Filter noise
    [[ "$line" == *"spawn:"* ]] || [[ "$line" == *"frame="* ]] && continue

    # Clean the message
    CLEAN_MSG=$(echo "$line" | sed 's/^.*tvheadend.*: //; s/[^a-zA-Z0-9 ]//g')

    # Trigger Alert
    touch /tmp/tvh_error_active
    curl -s -G --data-urlencode "status=down" --data-urlencode "msg=${CLEAN_MSG}" "$KUMA_URL" > /dev/null

    # Auto-recover after 55 seconds to ensure Kuma receives heartbeas at least every 60 seconds
    (sleep 55 && rm -f /tmp/tvh_error_active) &
done
