Troubleshooting
================

1. Connection refused, times out, or no response at all
-----------------------------------------------------------

**Symptom:** ``curl: (7) Failed to connect`` or the request just hangs.

**Cause:** the caller's source address isn't inside ``--allowed-cidr``
(default ``192.168.0.0/24``). ``TrustedNetworkServer.verify_request()``
drops such connections before any HTTP response is sent -- this is by
design (see :doc:`architecture`), so it looks identical to nothing
listening on that port at all.

**Fix:** confirm the caller's IP is in the allowed network, and check the
server's own log for a matching rejection line:

.. code-block:: bash

    journalctl -u macaddr-reservation-service | grep rejected

.. code-block:: text

    rejected connection from 10.0.0.5 (outside 192.168.0.0/24)

If the caller genuinely should be allowed, restart the service with a
wider ``--allowed-cidr`` in the systemd unit's ``ExecStart`` line.

2. ``401 {"error": "unauthorized"}``
---------------------------------------

**Symptom:** every request to ``/reservations`` gets a 401, even with an
``Authorization`` header set.

**Cause:** the header is missing, isn't the ``Bearer <token>`` form, or
the token doesn't match what the service loaded at startup.

**Fix:** confirm the header format and that the token matches the file
the service is actually reading:

.. code-block:: bash

    cat /root/scripts/macaddr_service_token
    curl -sS -X POST http://192.168.0.8:8600/reservations \
      -H "Authorization: Bearer $(cat /root/scripts/macaddr_service_token)" \
      -H "Content-Type: application/json" -d '{"dry_run": true}'

If ``MACADDR_SERVICE_TOKEN`` is set in the service's environment
(``systemctl show macaddr-reservation-service -p Environment``), it takes
precedence over the token file -- check there first if the file looks
right but auth still fails.

3. ``404 {"error": "unknown host_type: ...", "valid_host_types": [...]}``
---------------------------------------------------------------------------

**Symptom:** a request that used to work now 404s, or a new section isn't
recognized.

**Cause:** ``host_type`` is matched against the live section labels in
``macaddr.txt`` at request time -- there's no separate config listing
them. A typo, or a header line that was edited or removed, changes what's
valid.

**Fix:** the response body already lists every currently valid
``host_type``. Confirm the section header in ``macaddr.txt`` matches the
expected ``# Label .START to .END`` format exactly -- for example
``# Servers .01 to .50`` -- with no extra punctuation.

4. ``409 {"error": "mac already reserved", "existing_ip": "..."}`` (or ``hostname``)
--------------------------------------------------------------------------------------

**Symptom:** adding a reservation for a device fails with a 409.

**Cause:** this is the add-only guarantee working as intended -- the MAC
or hostname already has a reservation somewhere in ``macaddr.txt``
(``existing_ip`` says where), and this service never edits or removes an
existing line.

**Fix:** if the existing reservation is wrong or needs to move, edit
``macaddr.txt`` by hand and run ``pihole_importer.py`` directly --
correcting or removing an entry is intentionally outside this service's
API.

5. ``503 {"error": "no free IP in section '...' (.START to .END)"}``
-------------------------------------------------------------------------

**Symptom:** every request for a given ``host_type`` fails with a 503.

**Cause:** every 4th-octet value in that section's range is already used
somewhere in the file.

**Fix:** widen the section's range in ``macaddr.txt`` (edit the
``# Label .START to .END`` header), or free up IPs by moving stale
reservations to a different section, then retry.

6. ``201 Created`` but ``"importer_ok": false``
--------------------------------------------------

**Symptom:** the response shows ``"status": "created"`` with a real
``"ip"``, but ``"importer_ok"`` is ``false``.

**Cause:** the reservation *was* written to ``macaddr.txt`` -- that part
succeeded -- but running ``pihole_importer.py`` against it failed, so
Pi-hole itself was never updated. Check ``importer.stderr`` in the
response for the reason; a common one is a conflict against the live
``pihole.toml`` that ``pihole_importer.py`` would normally ask about
interactively, which instead surfaces as an ``EOFError`` here (see
:func:`macaddr_reservation_service.run_importer`).

**Fix:** run the importer by hand to see and resolve the interactive
prompt:

.. code-block:: bash

    python3 /root/scripts/pihole_importer.py /root/scripts/macaddr.txt

The reservation is already in ``macaddr.txt``, so re-running the importer
(by hand, or by resubmitting the same request, which will now hit the
409 in section 4) is all that's needed -- nothing needs to be re-added.

7. Service won't start
-------------------------

**Symptom:** ``systemctl status macaddr-reservation-service`` shows
``failed`` or ``inactive`` right after ``start``.

**Cause:** ``journalctl -u macaddr-reservation-service -n 50 --no-pager``
will show one of a few startup checks failing fast, each printed to
stderr:

- ``error: no auth token configured`` -- see section 2 of :doc:`how_to`.
- ``error: reservations file not found: ...`` -- ``--reservations-file``
  (or its default) doesn't exist on this host.
- ``error: pihole_importer.py not found: ...`` -- likewise for
  ``--importer-path``.
- ``No module named 'pihole_importer'`` -- the service isn't running from
  the same directory as ``pihole_importer.py``; both must be installed
  together (step 3 of :doc:`how_to`).

**Fix:** the printed message names the exact missing piece; none of these
require code changes, only fixing the referenced path or file.

8. A write appears to have been lost under concurrent requests
--------------------------------------------------------------------

**Symptom:** two reservations submitted at nearly the same time, and one
seems to have not landed, or both got the same IP.

**Cause:** unlikely by design --
:func:`macaddr_reservation_service.locked_reservations_file` holds an
exclusive ``flock()`` around the whole allocate-insert-write sequence, so
concurrent requests are fully serialized rather than racing. If this is
observed, check first whether something *other* than this service (a
manual edit, a second instance of the service, a different tool) wrote to
``macaddr.txt`` at the same time -- ``flock()`` only serializes cooperating
processes, not an unrelated writer that doesn't take the same lock.

**Fix:** check the timestamped backups
(``macaddr.txt.YYYYMMDD-HHMMSS.bak``, written before every real write) to
recover the pre-write state and confirm what actually happened.
