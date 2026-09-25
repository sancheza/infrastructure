Tvheadend / Uptime Kuma Monitor
===================================

**Files:** ``tvh_kuma_monitor.sh``, ``tvh-monitor.service``

Overview
--------

Runs as a systemd service that:

1. Tails ``journalctl`` for the ``tvheadend`` syslog identifier in real
   time, filtering out routine noise (``spawn:``/``frame=`` lines).
2. On any remaining error/warning/critical log line, strips it to a clean
   message and pushes a "down" status plus that message to an Uptime Kuma
   push monitor, then auto-clears back to "up" after 55 seconds -- long
   enough for Kuma to register the down event, short enough to self-heal
   without manual intervention.
3. In parallel, sends an "up" heartbeat every 50 seconds whenever no
   error is currently active, so Kuma's push monitor (which expects a
   heartbeat at least every 60s) never times out and flags the host
   itself as unreachable.

Tvheadend can fail quietly (a tuner drops, a muxer crashes) with nothing
but a log line to show for it. This turns that log line into an actual
alert in Uptime Kuma -- and whatever notification channels Kuma is wired
to (Slack, email, push, etc.) -- without needing to poll or watch logs
manually.

Usage
-----

1. Copy ``tvh_kuma_monitor.sh`` to ``/usr/local/bin/`` and make it
   executable.
2. Create ``/usr/local/bin/.env`` (not tracked in this repo) with
   ``KUMA_URL=http://<host>:3001/api/push/<key>`` -- the script refuses
   to start and logs a critical error via ``logger`` if this is missing.
3. Install and start the service:

   .. code-block:: bash

       sudo cp tvh-monitor.service /etc/systemd/system/
       sudo systemctl enable --now tvh-monitor.service

4. Check status/logs with ``systemctl status tvh-monitor`` /
   ``journalctl -u tvh-monitor -f``.
