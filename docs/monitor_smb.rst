macOS SMB & Wi-Fi Monitor
============================

**File:** ``monitor_smb.sh``

Overview
--------

Polls every 5 seconds and prints, side by side: (1) each mounted SMB
share's mount point, connection status, negotiated protocol version, and
session-reconnect count via ``smbutil statshares -a``; and (2) current
Wi-Fi RSSI, noise floor, and derived SNR via the Apple80211 private
framework (``airport -I``).

SMB drops on macOS are frequently caused by a marginal Wi-Fi link, but
the two symptoms show up in unrelated tools. Watching them together makes
it obvious when a share disconnects (rising reconnect count) at the same
moment SNR craters -- confirming Wi-Fi as the root cause instead of the
file server.

Usage
-----

.. code-block:: bash

    ./monitor_smb.sh

Ctrl+C to stop. Edit ``WIFI_INTERFACE`` (default ``en0``) in the script
if your Wi-Fi adapter has a different BSD device name.

Requires macOS (``smbutil`` and the Apple80211 framework).
