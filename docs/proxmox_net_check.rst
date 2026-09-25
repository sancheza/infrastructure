Proxmox LXC Network Auditor
==============================

**File:** ``proxmox_net_check.sh``

Overview
--------

Walks a range of Proxmox container IDs, reads each container's
``/etc/pve/lxc/<CTID>.conf``, and prints the CTID next to its raw
``net0:`` line (bridge, VLAN, IP/DHCP mode, firewall flag, etc.) for
every CTID whose config file exists. Must run on the Proxmox VE host
itself -- it reads directly from ``/etc/pve/lxc/``.

On a host running dozens of LXCs, this is the fastest way to eyeball
every container's network setup at once: confirming which containers are
still on an old bridge before a migration, or spotting one with a stale
static IP, without opening each config file by hand.

Usage
-----

.. code-block:: bash

    ./proxmox_net_check.sh [OPTIONS]

.. list-table::
   :header-rows: 1

   * - Flag
     - Default
     - Purpose
   * - ``-s``, ``--start <ID>``
     - ``100``
     - Starting CTID for the range.
   * - ``-e``, ``--end <ID>``
     - ``130``
     - Ending CTID for the range.
   * - ``-h``, ``--help``
     -
     - Show usage and exit.
   * - ``-v``, ``--version``
     -
     - Show script version and exit.

Example:

.. code-block:: bash

    ./proxmox_net_check.sh -s 200 -e 250
