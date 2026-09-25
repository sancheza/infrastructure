How To
======

This guide covers installing, configuring, and calling the macaddr
reservation service. All commands run on the Pi-hole host itself.

1. Prerequisites
-----------------

- Python 3.9+ and ``pihole_importer.py`` already working on this host
  (its ``toml`` dependency and ``/etc/pihole/pihole.toml`` access apply
  here too, since this service invokes it).
- Root access -- both installation and the running service need it.

2. Generate the auth token
----------------------------

Every ``POST /reservations`` request needs a bearer token. Generate one
and lock the file down to root:

.. code-block:: bash

    openssl rand -hex 32 > /root/scripts/macaddr_service_token
    chmod 600 /root/scripts/macaddr_service_token

Expected output: no output on success; ``ls -la`` on the file shows
``-rw-------``.

Setting the ``MACADDR_SERVICE_TOKEN`` environment variable overrides the
token file -- useful for the systemd unit's ``Environment=`` directive
instead of a file on disk, if preferred.

3. Install
-----------

.. code-block:: bash

    cp macaddr_reservation_service.py pihole_importer.py /root/scripts/
    chmod +x /root/scripts/macaddr_reservation_service.py
    cp macaddr-reservation-service.service /etc/systemd/system/
    systemctl daemon-reload

4. Configure
-------------

All configuration is via CLI flags (``macaddr_reservation_service.py -h``
for the full list); the systemd unit's ``ExecStart`` line is the place to
add any of these:

.. list-table::
   :header-rows: 1

   * - Flag
     - Default
     - Purpose
   * - ``--port``
     - ``8600``
     - TCP port to listen on.
   * - ``--allowed-cidr``
     - ``192.168.0.0/24``
     - Only connections from this network are accepted; everything else
       is silently dropped.
   * - ``--network``
     - ``192.168.0.0/24``
     - The network reservations are allocated from. Usually matches
       ``--allowed-cidr``.
   * - ``--reservations-file``
     - ``/root/scripts/macaddr.txt``
     - Path to the file this service edits.
   * - ``--importer-path``
     - ``/root/scripts/pihole_importer.py``
     - Path to the importer this service runs after each write.
   * - ``--token-file``
     - ``/root/scripts/macaddr_service_token``
     - Where to read the bearer token from, if
       ``MACADDR_SERVICE_TOKEN`` isn't set.
   * - ``--bind-host``
     - ``0.0.0.0``
     - Socket bind address. ``--allowed-cidr`` is the real trust
       boundary; narrow this too for defense in depth if the host has
       more than one interface.

5. Deploy as a systemd service
--------------------------------

.. code-block:: bash

    systemctl enable --now macaddr-reservation-service.service

Expected output:

.. code-block:: text

    Created symlink /etc/systemd/system/multi-user.target.wants/macaddr-reservation-service.service → ...

6. Verify it's running
------------------------

.. code-block:: bash

    systemctl status macaddr-reservation-service.service --no-pager
    curl -s http://192.168.0.8:8600/healthz

Expected output:

.. code-block:: text

    Active: active (running) since ...
    {"status": "ok"}

Watch logs live with ``journalctl -u macaddr-reservation-service -f``.

7. Call the API
-----------------

**Preview an allocation without changing anything** (``dry_run``):

.. code-block:: bash

    TOKEN=$(cat /root/scripts/macaddr_service_token)
    curl -sS -X POST http://192.168.0.8:8600/reservations \
      -H "Authorization: Bearer $TOKEN" \
      -H "Content-Type: application/json" \
      -d '{"mac": "AA:BB:CC:DD:EE:FF", "hostname": "newhost", "host_type": "Servers", "dry_run": true}'

Expected output:

.. code-block:: json

    {"status": "dry-run", "mac": "AA:BB:CC:DD:EE:FF", "hostname": "newhost", "host_type": "Servers", "ip": "192.168.0.3", "importer": null}

**Add a real reservation** (drop ``dry_run``):

.. code-block:: bash

    curl -sS -X POST http://192.168.0.8:8600/reservations \
      -H "Authorization: Bearer $TOKEN" \
      -H "Content-Type: application/json" \
      -d '{"mac": "AA:BB:CC:DD:EE:FF", "hostname": "newhost", "host_type": "Servers"}'

Expected output:

.. code-block:: json

    {"status": "created", "mac": "AA:BB:CC:DD:EE:FF", "hostname": "newhost", "host_type": "Servers", "ip": "192.168.0.3", "reservations_file": "/root/scripts/macaddr.txt", "importer_ok": true, "importer": {"returncode": 0, "stdout": "...", "stderr": ""}}

Submitting that exact same request again is safe -- the same MAC and
hostname together are treated as a repeat of an existing reservation,
not a new one, and return ``200 {"status": "exists", ..., "ip":
"192.168.0.3"}`` with no file write. This makes the endpoint safe to
call from idempotent automation (e.g. an OpenTofu ``local-exec``
provisioner that may re-run on every ``apply``).

``host_type`` is matched case-insensitively against whatever
``# Label .START to .END`` sections currently exist in ``macaddr.txt`` --
there's no fixed list in the service itself. An unknown ``host_type``
returns the valid ones:

.. code-block:: json

    {"error": "unknown host_type: guests", "valid_host_types": ["Endpoints", "IoT-trusted", "IoT-untrusted", "Servers"]}

See :doc:`troubleshooting` for every other error response.
