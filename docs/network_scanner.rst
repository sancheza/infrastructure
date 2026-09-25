Network Inventory Scanner
============================

**File:** ``network_scanner.py``

Overview
--------

A two-stage device inventory for a local subnet. Stage 1 sends a raw ARP
sweep (via Scapy) across the given CIDR range to quickly find every live
host's IP and MAC -- this works even for devices that block ping. Stage 2
hands those discovered IPs to Nmap (``-O -Pn -sT -p 22,80 -T4``) to
fingerprint OS, hostname, and MAC vendor, and to check whether SSH (22)
and HTTP (80) are open. Results print as a sorted, color-coded table
(Rich) and can be written to CSV.

Answers "what's actually on my network right now" in one pass -- useful
for auditing unknown devices, spotting rogue hosts, confirming IoT gear
is present, or building a baseline inventory to diff against later.

Usage
-----

.. code-block:: bash

    sudo python3 network_scanner.py [cidr] [--export] [-v]

.. list-table::
   :header-rows: 1

   * - Argument
     - Default
     - Purpose
   * - ``cidr``
     - ``192.168.0.0/24``
     - Subnet to scan, e.g. ``192.168.1.0/24``.
   * - ``--export``
     - off
     - Also write results to ``network_inventory.csv`` in the current
       directory.

Requires ``sudo`` (Nmap OS detection needs raw-socket access) and Nmap
installed and on ``PATH``. Only works on the local broadcast domain --
ARP doesn't route.

Example:

.. code-block:: bash

    sudo python3 network_scanner.py 192.168.1.0/24 --export

Module Reference
-----------------

.. automodule:: network_scanner
   :members:
