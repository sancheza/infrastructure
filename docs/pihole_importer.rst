Pi-hole v6 Reservation Importer
===================================

**File:** ``pihole_importer.py``

Overview
--------

Bulk-imports static DHCP reservations and local DNS records into Pi-hole
v6's ``/etc/pihole/pihole.toml`` from a plain ``MAC,IP,Hostname`` CSV/text
file (``macaddr.txt`` by default). Strictly validates every MAC (any
common separator style), IPv4 address, and hostname/FQDN before touching
anything, and aborts the whole run on the first invalid line.

Before writing, it diffs the incoming entries against what's already in
``pihole.toml`` and reports four buckets -- identical, conflicting (same
IP/MAC/hostname but mismatched details), Pi-hole-only, and file-only --
so nothing is silently overwritten. It always takes a timestamped backup
(``pihole.toml.YYYYMMDD-HHMMSS``) before writing, and restarts the
``pihole-FTL`` service afterward. Re-running with the same file is a
no-op (idempotent).

:doc:`macaddr_reservation_service/index` is a companion HTTP service that
adds one reservation at a time to ``macaddr.txt`` and runs this importer
automatically afterward; use this script directly for bulk edits or a
manual audit instead.

Usage
-----

.. code-block:: bash

    sudo python3 pihole_importer.py [input_file] [--export] [--audit] [-v]

.. list-table::
   :header-rows: 1

   * - Argument / Flag
     - Default
     - Purpose
   * - ``input_file``
     - ``macaddr.txt``
     - Path to the ``MAC,IP,Hostname`` file.
   * - ``--export``
     - off
     - Dump Pi-hole's *current* entries to ``macaddr.<timestamp>.csv`` --
       useful for a snapshot or seeding a new input file.
   * - ``--audit``
     - off
     - Compare the input file against Pi-hole without writing anything:
       same / conflicting / only-in-Pi-hole / only-in-file.

Requires the ``toml`` Python package (``pip install toml`` or
``sudo apt install python3-toml``) and write access to
``/etc/pihole/pihole.toml``, so it needs ``sudo`` on most systems.

Example:

.. code-block:: bash

    sudo python3 pihole_importer.py ./reservations.txt

Module Reference
-----------------

.. automodule:: pihole_importer
   :members:
