Latency Testers
=================

**Files:** ``check_latency.py`` (macOS/Linux), ``check_latency_win.ps1``
(Windows)

Overview
--------

High-precision network latency and packet-loss testers. Both wrap the
platform's own ping mechanism at a fast, configurable interval for the
whole test duration, then report min/avg/max/stddev round-trip time and
exact packet-loss percentage. A normal 1-ping-per-second check can miss
brief drops; pinging much more frequently over a longer window surfaces
intermittent packet loss and jitter that would otherwise hide between
samples -- useful for diagnosing flaky Wi-Fi, a marginal ISP line, or VPN
instability before escalating to an ISP or replacing hardware.

macOS / Linux: ``check_latency.py``
--------------------------------------

Wraps the system ``ping`` with a configurable interval (fractions of a
second are supported).

.. code-block:: bash

    python3 check_latency.py [-h] [-d DURATION] [-t TARGET] [-i INTERVAL]

.. list-table::
   :header-rows: 1

   * - Flag
     - Default
     - Purpose
   * - ``-d``, ``--duration``
     - ``10``
     - Test length, in minutes.
   * - ``-t``, ``--target``
     - ``8.8.8.8``
     - Host/IP to ping.
   * - ``-i``, ``--interval``
     - ``0.2``
     - Seconds between pings.

Examples:

.. code-block:: bash

    python3 check_latency.py                 # default 10-min run against 8.8.8.8
    python3 check_latency.py -d 5 -t 1.1.1.1
    python3 check_latency.py -i 0.5

Windows: ``check_latency_win.ps1``
-------------------------------------

The Windows-native counterpart, built on ``Test-Connection`` since
Windows has no raw ``ping -i <fractional>`` equivalent. Pings once per
second for the chosen duration, computes packet loss / min / avg / max
latency / jitter (stddev), and writes a timestamped
``NetworkTest_<timestamp>.txt`` report to the Desktop (falling back to
the current directory if the Desktop path isn't writable).

.. code-block:: powershell

    .\check_latency_win.ps1 [-TargetHost <host>] [-DurationMinutes <n>]

First run may require:

.. code-block:: powershell

    Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser

Example:

.. code-block:: powershell

    .\check_latency_win.ps1 -TargetHost "1.1.1.1" -DurationMinutes 15

Module Reference
-----------------

.. automodule:: check_latency
   :members:
