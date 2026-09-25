Wi-Fi Signal Monitor
=======================

**File:** ``check_wifi.sh``

Overview
--------

A live, in-place (non-scrolling) terminal dashboard of Wi-Fi RSSI.
Samples the signal once per second, colors the current reading by
quality, and keeps a running min/max/mean/median/stddev over the whole
session. Auto-detects the platform:

- **macOS** -- real dBm via ``wdutil info`` (requires ``sudo``).
- **Linux** -- real dBm via ``iw``/``iwconfig`` (no ``sudo`` needed),
  falling back to ``nmcli``'s 0-100% quality (converted to an
  *approximate* dBm, marked with ``~``) if neither is present.
- **Windows** -- via WSL, Git Bash, MSYS2, or Cygwin, shelling out to
  ``netsh.exe wlan show interfaces``. Native cmd.exe/PowerShell can't run
  this script directly. Windows only exposes a 0-100% quality (no true
  dBm), so the value shown is likewise an approximate ``~`` conversion,
  and parsing assumes an English-language Windows install.

The running stats make a one-off dip easy to tell apart from a
chronically weak spot -- point it at a laptop while walking around a
building to find dead zones, decide where to place an access point, or
verify a channel change or antenna adjustment actually helped (falling
stddev = a more stable link).

Signal quality key (RSSI in dBm; closer to 0 is better):

.. list-table::
   :header-rows: 1

   * - Rating
     - Range
   * - Excellent
     - >= -50 dBm
   * - Good
     - -50 to -60 dBm
   * - Fair
     - -60 to -67 dBm
   * - Poor
     - -67 to -75 dBm
   * - Bad
     - < -75 dBm

Stddev is colored on stability instead: <3 dBm green, 3-7 dBm yellow,
>7 dBm red.

Usage
-----

.. code-block:: bash

    sudo ./check_wifi.sh   # macOS, requires root
    ./check_wifi.sh        # Linux/Windows, no sudo needed

Ctrl+C to stop.
