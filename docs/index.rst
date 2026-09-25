Infrastructure & Network Utilities Documentation
===================================================

A collection of independent scripts and tools for network administration,
performance monitoring, and infrastructure configuration, covering macOS,
Linux, and Windows. Each tool below is a standalone entry point -- see its
page for what it does and how to run it. ``macaddr_reservation_service.py``
is the one exception with a multi-page treatment, since it's an HTTP
service (with its own security model) rather than a one-shot command.

Network Discovery & Auditing
-----------------------------

.. toctree::
   :maxdepth: 1

   network_scanner
   proxmox_net_check

Performance & Connectivity
----------------------------

.. toctree::
   :maxdepth: 1

   check_latency
   check_wifi
   monitor_smb

Infrastructure Configuration
-------------------------------

.. toctree::
   :maxdepth: 1

   pihole_importer
   macaddr_reservation_service/index

Service Alerting & Monitoring
--------------------------------

.. toctree::
   :maxdepth: 1

   tvh_kuma_monitor

Indices and tables
=====================

* :ref:`genindex`
* :ref:`search`
