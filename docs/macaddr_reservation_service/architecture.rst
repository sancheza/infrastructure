Architecture
============

Overview
--------

The macaddr reservation service is a companion HTTP microservice to
``pihole_importer.py``, both running on the Pi-hole host itself. It exists
so a caller can reserve the next free IP for a new device over an
authenticated API call, instead of SSHing in and hand-editing
``macaddr.txt``. It never removes or edits an existing line in that file
-- every request either adds exactly one new reservation or is rejected.

**Components**

- **Service** (``macaddr_reservation_service.py``) -- a single-file,
  standard-library-only Python module. Runs as a systemd unit
  (``macaddr-reservation-service.service``), listening on TCP 8600.
- **Reservations file** (``macaddr.txt``, default
  ``/root/scripts/macaddr.txt``) -- the same file ``pihole_importer.py``
  reads. Plain text, grouped into ``# Label .START to .END`` sections
  (for example ``# Servers .01 to .50``), each holding
  ``MAC,IP,HOSTNAME`` rows sorted by IP.
- **Importer** (``pihole_importer.py``) -- invoked as a subprocess after
  every successful write, to push the new reservation into
  ``/etc/pihole/pihole.toml`` and restart ``pihole-FTL``. See
  :doc:`/pihole_importer` for what it does beyond this point.

Request Flow
------------

::

    ┌──────────┐     ┌────────────────┐     ┌───────────────────────────┐
    │  Caller  │────▶│ TrustedNetwork │────▶│         Handler            │
    │ (curl,   │     │ Server         │     │  do_GET / do_POST          │
    │ script)  │     │ (verify_request)│     │                            │
    └──────────┘     └───────┬────────┘     └──────────────┬─────────────┘
                              │ outside                     │
                              │ --allowed-cidr               │ bad/missing
                              ▼                              ▼ Bearer token
                      connection dropped              401 Unauthorized
                              │                              │
                              │                              ▼ authorized
                              │                   ┌────────────────────────┐
                              │                   │   process_reservation   │
                              │                   │  (validate -> lock ->   │
                              │                   │   allocate -> insert -> │
                              │                   │   write -> import)      │
                              │                   └───────────┬─────────────┘
                              │                                 │
                              │                                 ▼
                              │                   201 Created / 200 dry-run
                              │                   400 / 404 / 409 / 503 / 500

Each step of ``process_reservation()``:

1. **Validate** -- ``mac``, ``hostname``, and ``host_type`` are required;
   ``mac``/``hostname`` are checked with ``pihole_importer.py``'s own
   ``validate_mac()`` / ``validate_hostname()`` (dev ADR 0003 code reuse:
   the same rules a manually-edited ``macaddr.txt`` line must satisfy
   before ``pihole_importer.py`` will accept it).
2. **Lock** -- an advisory ``flock()`` on ``macaddr.txt`` (see
   :func:`macaddr_reservation_service.locked_reservations_file`)
   serializes concurrent requests for the rest of this list.
3. **Parse** -- :func:`~macaddr_reservation_service.parse_sections` reads
   every ``# Label .START to .END`` section and its
   ``MAC,IP,HOSTNAME`` rows.
4. **Match section** -- ``host_type`` is matched case-insensitively
   against a section label; no match is a 404 listing the valid labels.
5. **Reject duplicates** -- a MAC or hostname already present under a
   *different* hostname or MAC anywhere in the file (any section) is a
   409, naming the IP it already holds. A request that exactly repeats an
   existing MAC+hostname pair is instead a 200, returning that entry's IP
   unchanged -- an idempotent repeat, not a conflict, so retrying (or
   replaying) the same registration is always safe. This is what keeps
   the file add-only: the same device can never end up with two
   reservations.
6. **Allocate** -- the first 4th-octet value in the section's range that
   isn't already used *anywhere in the file* becomes the new IP; an
   exhausted range is a 503.
7. **Insert** -- :func:`~macaddr_reservation_service.insert_line_in_section`
   places the new row immediately after the entry with the next-lower IP
   in that section, preserving numeric order.
8. **Write** -- :func:`~macaddr_reservation_service.write_reservations`
   takes a timestamped backup, then atomically replaces ``macaddr.txt``
   (temp file + ``os.replace()``), so a crash mid-write can't corrupt it.
   Skipped entirely when the request set ``"dry_run": true``.
9. **Import** -- :func:`~macaddr_reservation_service.run_importer` runs
   ``pihole_importer.py`` against the updated file and returns its
   ``returncode``/``stdout``/``stderr`` to the caller. A non-zero result
   here does **not** undo the write in step 8 -- the reservation is
   already committed to ``macaddr.txt``, and only the push to Pi-hole
   failed; see :doc:`troubleshooting`.

Security Model
---------------

The service must run as root: writing ``macaddr.txt``, running
``pihole_importer.py`` (which writes ``/etc/pihole/pihole.toml`` and
restarts ``pihole-FTL``), and preserving file ownership all require it.
Running an HTTP listener as root on a trusted-but-shared LAN is an
accepted tradeoff of this deployment, not an oversight -- it's mitigated
two ways rather than by dropping privileges (which the importer's own
requirements make impractical):

- **Network boundary** -- ``TrustedNetworkServer.verify_request()``
  checks every incoming connection's source address against
  ``--allowed-cidr`` (default ``192.168.0.0/24``) *before* any HTTP
  parsing happens; a connection from outside it is dropped silently, with
  no response and no information disclosed.
- **Bearer token** -- every ``POST /reservations`` also requires
  ``Authorization: Bearer <token>``, compared with
  :func:`hmac.compare_digest` to avoid a timing side-channel. The token
  lives outside version control, in a root-only file (or the
  ``MACADDR_SERVICE_TOKEN`` environment variable) -- see :doc:`how_to`.

``GET /healthz`` is the one unauthenticated route (still subject to the
network filter above), by design: it reveals nothing beyond process
liveness.

Module Reference
-----------------

.. automodule:: macaddr_reservation_service
   :members:
