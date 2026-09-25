#!/usr/bin/env python3
"""macaddr_reservation_service.py

Authenticated HTTP microservice that adds host reservations to a
macaddr.txt file (companion to pihole_importer.py) and then runs
pihole_importer.py to push the change into Pi-hole.

Reservations are add-only: an existing MAC, IP, or hostname is never
removed or edited. A request supplies a MAC, a hostname, and a host_type
(matched against one of the "# Label .START to .END" section headers in
macaddr.txt); the service allocates the first free IP in that section's
4th-octet range, inserts "MAC,IP,HOSTNAME" immediately after the previous
IP in numeric order, and runs pihole_importer.py against the updated file.

Usage:
    macaddr_reservation_service.py
    macaddr_reservation_service.py --port 8600 --allowed-cidr 192.168.0.0/24
    macaddr_reservation_service.py --reservations-file /root/scripts/macaddr.txt

Request example:
    curl -sS -X POST http://192.168.0.8:8600/reservations \\
        -H "Authorization: Bearer $TOKEN" \\
        -H "Content-Type: application/json" \\
        -d '{"mac": "AA:BB:CC:DD:EE:FF", "hostname": "newhost", "host_type": "Servers"}'

Add ``"dry_run": true`` to the request body to compute and return the
allocated IP without writing macaddr.txt or running pihole_importer.py.
``GET /healthz`` is an unauthenticated liveness check.
"""

import argparse
import contextlib
import dataclasses
import fcntl
import hmac
import http.server
import ipaddress
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import traceback
from datetime import datetime
from http import HTTPStatus

VERSION = "1.0.2"

DEFAULT_RESERVATIONS_FILE = "/root/scripts/macaddr.txt"
DEFAULT_IMPORTER_PATH = "/root/scripts/pihole_importer.py"
DEFAULT_TOKEN_FILE = "/root/scripts/macaddr_service_token"
DEFAULT_NETWORK = "192.168.0.0/24"
DEFAULT_PORT = 8600

MAX_BODY_BYTES = 8192
MAX_IMPORTER_OUTPUT = 4000
IMPORTER_TIMEOUT_SECONDS = 60

SECTION_HEADER_RE = re.compile(
    r"^#\s*(?P<label>.+?)\s+\.(?P<start>\d{1,3})\s+to\s+\.(?P<end>\d{1,3})\s*$"
)
ROW_RE = re.compile(
    r"^(?P<mac>[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}),"
    r"(?P<ip>(?:\d{1,3}\.){3}\d{1,3}),"
    r"(?P<hostname>\S+)$"
)


# ---------- Data model ----------

class _Entry:
    __slots__ = ("line_index", "mac", "ip", "octet", "hostname")

    def __init__(self, line_index, mac, ip, hostname):
        self.line_index = line_index
        self.mac = mac
        self.ip = ip
        self.octet = int(ip.rsplit(".", 1)[1])
        self.hostname = hostname


class Section:
    """One '# Label .START to .END' block of macaddr.txt.

    Args:
        label: Section name, e.g. "Servers".
        start: First 4th-octet value in the section's range.
        end: Last 4th-octet value in the section's range.
        header_index: Index of the header line in the file's line list.
    """

    def __init__(self, label, start, end, header_index):
        self.label = label
        self.start = start
        self.end = end
        self.header_index = header_index
        self.entries = []


@dataclasses.dataclass
class ServiceConfig:
    """Runtime configuration for one running instance of the service."""

    reservations_file: str
    importer_path: str
    python_bin: str
    network_prefix: str
    token: str


class ReservationError(Exception):
    """Carries the HTTP status and JSON body for a rejected request."""

    def __init__(self, status, message, extra=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra or {}

    def to_json(self):
        """Render as the JSON error body sent to the client."""
        payload = {"error": self.message}
        payload.update(self.extra)
        return payload


# ---------- macaddr.txt parsing ----------

def read_reservations(path):
    """Read macaddr.txt.

    Args:
        path: Path to macaddr.txt.

    Returns:
        tuple: (lines, had_trailing_newline) where lines is the file's
        content split on newlines with no trailing "\\n" on each entry,
        and had_trailing_newline records whether the file itself ended in
        one, so write_reservations() can reproduce it exactly.
    """
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    had_trailing_newline = content.endswith("\n")
    lines = content.split("\n")
    if had_trailing_newline:
        lines = lines[:-1]
    return lines, had_trailing_newline


def write_reservations(path, lines, had_trailing_newline):
    """Back up and atomically rewrite macaddr.txt with the given lines.

    Takes a timestamped ``path.YYYYMMDD-HHMMSS.bak`` copy first, then
    writes to a temp file in the same directory and atomically swaps it
    into place with ``os.replace()``, so a crash mid-write never leaves a
    half-written macaddr.txt on disk. The replacement file's
    owner/group/mode are copied from the original.

    Args:
        path: Path to macaddr.txt.
        lines: Full new content, one entry per line (no trailing "\\n").
        had_trailing_newline: Whether to end the written file with "\\n",
            as returned by read_reservations().

    Raises:
        OSError: If the backup or the atomic replace fails; the partial
            temp file is removed before the error propagates.
    """
    content = "\n".join(lines)
    if had_trailing_newline:
        content += "\n"

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(path, f"{path}.{ts}.bak")

    orig_stat = os.stat(path)
    tmp_path = f"{path}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp_path, orig_stat.st_mode)
        os.chown(tmp_path, orig_stat.st_uid, orig_stat.st_gid)
        os.replace(tmp_path, path)
    except OSError:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


@contextlib.contextmanager
def locked_reservations_file(path):
    """Serialize concurrent requests with an advisory exclusive lock.

    Holds a POSIX ``flock()`` on ``path`` for the duration of the ``with``
    block, so two requests racing to allocate a free IP in the same
    section can't both win it. Only guards macaddr.txt itself; the lock is
    released before pihole_importer.py runs.

    Args:
        path: Path to macaddr.txt.
    """
    with open(path, "r+", encoding="utf-8") as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)


def parse_sections(lines):
    """Parse macaddr.txt lines into an ordered list of Section objects.

    A line outside any section, or one that matches neither the header nor
    the row pattern (blank lines, stray comments), is skipped rather than
    raising -- it stays in the file untouched by insert_line_in_section()
    but isn't tracked as an entry.

    Args:
        lines: File content as returned by read_reservations().

    Returns:
        list[Section]: Sections in file order, each with its own entries
        in file order.
    """
    sections = []
    current = None
    for idx, raw in enumerate(lines):
        line = raw.strip()
        header_match = SECTION_HEADER_RE.match(line)
        if header_match:
            current = Section(
                header_match.group("label").strip(),
                int(header_match.group("start")),
                int(header_match.group("end")),
                idx,
            )
            sections.append(current)
            continue
        if current is None or not line:
            continue
        row_match = ROW_RE.match(line)
        if row_match:
            current.entries.append(_Entry(
                idx,
                row_match.group("mac"),
                row_match.group("ip"),
                row_match.group("hostname"),
            ))
    return sections


def find_section(sections, host_type):
    """Case-insensitive exact match of host_type against a section label."""
    target = host_type.strip().lower()
    for section in sections:
        if section.label.lower() == target:
            return section
    return None


def find_existing(sections, mac, hostname):
    """Look up whether mac/hostname already appear in the reservations file.

    Distinguishes a request that exactly repeats an existing entry (same
    mac AND same hostname, on the same row) from one that collides with a
    *different* entry (mac reused under another hostname, or vice versa),
    so callers can treat the former as an idempotent repeat and the latter
    as a genuine conflict.

    Returns:
        tuple[str, str, Section] | None: (kind, ip, section) where kind is
        "exact" (same mac and hostname already reserved together), "mac"
        (mac reserved under a different hostname), or "hostname" (hostname
        reserved under a different mac). None if neither appears at all.
    """
    mac_norm = mac.upper()
    hostname_norm = hostname.lower()
    for section in sections:
        for entry in section.entries:
            mac_match = entry.mac.upper() == mac_norm
            hostname_match = entry.hostname.lower() == hostname_norm
            if mac_match and hostname_match:
                return "exact", entry.ip, section
            if mac_match:
                return "mac", entry.ip, section
            if hostname_match:
                return "hostname", entry.ip, section
    return None


def find_free_ip(section, used_ips, network_prefix):
    """First unused 4th-octet IP within the section's range, or None."""
    for octet in range(section.start, section.end + 1):
        candidate = f"{network_prefix}{octet}"
        if candidate not in used_ips:
            return candidate
    return None


def build_reservation_line(mac, ip, hostname):
    """Format one macaddr.txt row: MACADDR,IP_ADDR,HOSTNAME."""
    return f"{mac},{ip},{hostname}"


def insert_line_in_section(lines, section, new_ip, new_line):
    """Insert new_line immediately after the previous IP in numeric order.

    Scans section.entries (already in file order) for the last one whose
    4th octet is below new_ip's, and inserts right after it. If new_ip is
    lower than every existing entry -- including when the section is
    empty -- new_line lands immediately after the section's header line.

    Args:
        lines: Full file content, as returned by read_reservations().
        section: The Section new_line belongs in.
        new_ip: The IP being reserved, e.g. "192.168.0.46".
        new_line: The formatted row to insert, from build_reservation_line().

    Returns:
        list[str]: A new lines list with new_line inserted; lines itself
        is not modified.
    """
    new_octet = int(new_ip.rsplit(".", 1)[1])
    insert_at = section.header_index + 1
    for entry in section.entries:
        if entry.octet < new_octet:
            insert_at = entry.line_index + 1
        else:
            break
    result = list(lines)
    result.insert(insert_at, new_line)
    return result


# ---------- Request handling ----------

def _normalize_mac(mac):
    clean = re.sub(r"[:\-.]", "", mac).upper()
    return ":".join(clean[i:i + 2] for i in range(0, 12, 2))


def run_importer(config):
    """Run pihole_importer.py against the updated reservations file.

    stdin is redirected from /dev/null rather than inherited. Our own
    allocation logic only ever picks an IP that's free in macaddr.txt, but
    pihole_importer.py's conflict check runs against the live pihole.toml,
    which can diverge from macaddr.txt (e.g. a reservation added by hand
    through the Pi-hole web UI). If that happens, pihole_importer.py
    prompts for confirmation via input(); with no stdin to read, that
    raises EOFError immediately instead of hanging the request forever.

    Args:
        config: The running ServiceConfig.

    Returns:
        dict: {"returncode", "stdout", "stderr"}. returncode is None (with
        stderr explaining why) if the subprocess couldn't be started, or
        timed out after IMPORTER_TIMEOUT_SECONDS. stdout/stderr are each
        truncated to MAX_IMPORTER_OUTPUT characters.
    """
    try:
        completed = subprocess.run(
            [config.python_bin, config.importer_path, config.reservations_file],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=IMPORTER_TIMEOUT_SECONDS,
            check=False,
        )
        return {
            "returncode": completed.returncode,
            "stdout": completed.stdout[:MAX_IMPORTER_OUTPUT],
            "stderr": completed.stderr[:MAX_IMPORTER_OUTPUT],
        }
    except subprocess.TimeoutExpired:
        return {
            "returncode": None,
            "stdout": "",
            "stderr": f"pihole_importer.py timed out after {IMPORTER_TIMEOUT_SECONDS}s",
        }
    except OSError as error:
        return {
            "returncode": None,
            "stdout": "",
            "stderr": f"failed to run pihole_importer.py: {error}",
        }


def process_reservation(payload, config, importer_module):
    """Validate, allocate, write, and import one reservation request.

    The single entry point do_POST() calls for ``POST /reservations``;
    also covers the ``dry_run`` path, which stops after allocation and
    never writes macaddr.txt or runs the importer.

    Args:
        payload: Parsed JSON request body. Required keys: "mac" (str),
            "hostname" (str), "host_type" (str, matched case-insensitively
            against a macaddr.txt section label). Optional: "dry_run"
            (bool, default False).
        config: The running ServiceConfig.
        importer_module: The imported pihole_importer module, used for its
            validate_mac() and validate_hostname() functions -- passed in
            rather than imported at module scope per dev ADR 0004 section 2.7.

    Returns:
        dict: On success, {"status", "mac", "hostname", "host_type", "ip",
        ...}. "status" is "dry-run" (no file write, "importer": None),
        "exists" (mac and hostname already reserved together -- an
        idempotent repeat of a prior request, no file write) or "created"
        (file written; "importer_ok" and "importer" carry the
        pihole_importer.py run's result).

    Raises:
        ReservationError: mac/hostname/host_type missing or fails
            validation (400); host_type doesn't match any section (404,
            with "valid_host_types"); the mac or hostname is already
            reserved under a *different* hostname or mac (409, with
            "existing_ip"); or the target section has no free IP left
            (503).
    """
    mac_raw = payload.get("mac")
    hostname_raw = payload.get("hostname")
    host_type = payload.get("host_type")
    dry_run = bool(payload.get("dry_run", False))

    if not isinstance(mac_raw, str) or not mac_raw.strip():
        raise ReservationError(HTTPStatus.BAD_REQUEST, "mac is required")
    if not isinstance(hostname_raw, str) or not hostname_raw.strip():
        raise ReservationError(HTTPStatus.BAD_REQUEST, "hostname is required")
    if not isinstance(host_type, str) or not host_type.strip():
        raise ReservationError(HTTPStatus.BAD_REQUEST, "host_type is required")

    if not importer_module.validate_mac(mac_raw):
        raise ReservationError(HTTPStatus.BAD_REQUEST, f"invalid MAC address: {mac_raw}")
    mac = _normalize_mac(mac_raw)

    hostname = hostname_raw.strip().lower()
    if not importer_module.validate_hostname(hostname):
        raise ReservationError(HTTPStatus.BAD_REQUEST, f"invalid hostname: {hostname_raw}")

    with locked_reservations_file(config.reservations_file):
        lines, had_trailing_newline = read_reservations(config.reservations_file)
        sections = parse_sections(lines)

        section = find_section(sections, host_type)
        if section is None:
            valid = sorted({s.label for s in sections})
            raise ReservationError(
                HTTPStatus.NOT_FOUND,
                f"unknown host_type: {host_type}",
                {"valid_host_types": valid},
            )

        existing = find_existing(sections, mac, hostname)
        if existing is not None:
            kind, ip, existing_section = existing
            if kind == "exact":
                return {
                    "status": "exists",
                    "mac": mac,
                    "hostname": hostname,
                    "host_type": existing_section.label,
                    "ip": ip,
                }
            raise ReservationError(
                HTTPStatus.CONFLICT,
                f"{kind} already reserved",
                {"existing_ip": ip},
            )

        used_ips = {entry.ip for s in sections for entry in s.entries}
        ip = find_free_ip(section, used_ips, config.network_prefix)
        if ip is None:
            raise ReservationError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                f"no free IP in section '{section.label}' "
                f"(.{section.start:02d} to .{section.end:02d})",
            )

        if dry_run:
            return {
                "status": "dry-run",
                "mac": mac,
                "hostname": hostname,
                "host_type": section.label,
                "ip": ip,
                "importer": None,
            }

        new_line = build_reservation_line(mac, ip, hostname)
        new_lines = insert_line_in_section(lines, section, ip, new_line)
        write_reservations(config.reservations_file, new_lines, had_trailing_newline)

    importer_result = run_importer(config)
    return {
        "status": "created",
        "mac": mac,
        "hostname": hostname,
        "host_type": section.label,
        "ip": ip,
        "reservations_file": config.reservations_file,
        "importer_ok": importer_result["returncode"] == 0,
        "importer": importer_result,
    }


# ---------- HTTP layer ----------

def make_handler(config, importer_module):
    """Build a BaseHTTPRequestHandler bound to config/importer_module."""

    class Handler(http.server.BaseHTTPRequestHandler):
        """Handles GET /healthz and POST /reservations."""

        server_version = f"macaddr-reservation-service/{VERSION}"

        def _send_json(self, status, payload, extra_headers=None):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self):
            header = self.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                return False
            provided = header[len("Bearer "):].strip()
            return hmac.compare_digest(provided, config.token)

        def do_GET(self):
            """Unauthenticated liveness check at /healthz; 404 elsewhere."""
            if self.path == "/healthz":
                self._send_json(HTTPStatus.OK, {"status": "ok"})
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self):
            """Authenticated POST /reservations; adds one reservation."""
            if self.path != "/reservations":
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return

            if not self._authorized():
                self._send_json(
                    HTTPStatus.UNAUTHORIZED,
                    {"error": "unauthorized"},
                    {"WWW-Authenticate": 'Bearer realm="macaddr-reservation-service"'},
                )
                return

            length_header = self.headers.get("Content-Length")
            if length_header is None or not length_header.isdigit():
                self._send_json(HTTPStatus.LENGTH_REQUIRED, {"error": "Content-Length required"})
                return
            length = int(length_header)
            if length > MAX_BODY_BYTES:
                self._send_json(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request body too large"}
                )
                return

            raw_body = self.rfile.read(length)
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON body"})
                return
            if not isinstance(payload, dict):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "JSON body must be an object"})
                return

            try:
                result = process_reservation(payload, config, importer_module)
            except ReservationError as error:
                self._send_json(error.status, error.to_json())
                return
            except Exception:
                traceback.print_exc(file=sys.stderr)
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"})
                return

            status = (
                HTTPStatus.CREATED if result["status"] == "created" else HTTPStatus.OK
            )
            self._send_json(status, result)

        def log_message(self, fmt, *fmt_args):
            print(f"{self.address_string()} - {fmt % fmt_args}", file=sys.stdout)

    return Handler


class TrustedNetworkServer(http.server.ThreadingHTTPServer):
    """Silently drops any connection from outside the allowed network."""

    allow_reuse_address = True

    def __init__(self, server_address, handler_cls, allowed_network):
        self.allowed_network = allowed_network
        super().__init__(server_address, handler_cls)

    def verify_request(self, request, client_address):
        try:
            allowed = ipaddress.ip_address(client_address[0]) in self.allowed_network
        except ValueError:
            allowed = False
        if not allowed:
            print(
                f"rejected connection from {client_address[0]} "
                f"(outside {self.allowed_network})",
                file=sys.stdout,
            )
        return allowed


def _install_shutdown_handler(server):
    """Stop the server cleanly on SIGTERM/SIGINT.

    server.shutdown() blocks until serve_forever() (running on the main
    thread) exits its loop, so it can't be called from the signal handler
    itself -- that would deadlock. It runs on a throwaway thread instead.
    """
    def _handle(signum, _frame):
        print(f"received signal {signum}, shutting down", file=sys.stdout)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)


# ---------- Startup ----------

def _cidr_type(value):
    """argparse ``type=`` for a CIDR argument, e.g. "192.168.0.0/24"."""
    try:
        return ipaddress.ip_network(value, strict=False)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error))


def _network_prefix(network):
    """First three octets of network, with a trailing dot, e.g. "192.168.0."."""
    return str(network.network_address).rsplit(".", 1)[0] + "."


def load_token(token_file):
    """Read the bearer token from the environment or a root-only file."""
    env_token = os.environ.get("MACADDR_SERVICE_TOKEN")
    if env_token:
        return env_token.strip()

    if not os.path.exists(token_file):
        print(
            f"error: no auth token configured. Set MACADDR_SERVICE_TOKEN or "
            f"create {token_file} (mode 600) with a random secret, e.g.:\n"
            f"  openssl rand -hex 32 > {token_file} && chmod 600 {token_file}",
            file=sys.stderr,
        )
        return None

    mode = stat.S_IMODE(os.stat(token_file).st_mode)
    if mode & 0o077:
        print(
            f"warning: {token_file} is readable by group/other "
            f"(mode {oct(mode)}); recommend chmod 600",
            file=sys.stdout,
        )

    with open(token_file, "r", encoding="utf-8") as f:
        token = f.read().strip()
    if not token:
        print(f"error: {token_file} is empty", file=sys.stderr)
        return None
    return token


def build_parser():
    """Build the stdlib-only argument parser (dev ADR 0004 section 2.7)."""
    parser = argparse.ArgumentParser(
        description=(
            "Authenticated HTTP microservice that adds DHCP/DNS host reservations "
            "to a macaddr.txt file and runs pihole_importer.py."
        ),
        epilog=(
            "Examples:\n"
            "  macaddr_reservation_service.py\n"
            "  macaddr_reservation_service.py --port 8600 --allowed-cidr 192.168.0.0/24\n"
            "\n"
            "  curl -sS -X POST http://192.168.0.8:8600/reservations \\\n"
            "    -H \"Authorization: Bearer $TOKEN\" \\\n"
            "    -H \"Content-Type: application/json\" \\\n"
            "    -d '{\"mac\": \"AA:BB:CC:DD:EE:FF\", \"hostname\": \"newhost\", "
            "\"host_type\": \"Servers\"}'\n"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument(
        "--bind-host", default="0.0.0.0",
        help="Address to bind the HTTP server to (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"TCP port to listen on (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--allowed-cidr", type=_cidr_type, default=_cidr_type(DEFAULT_NETWORK),
        help=f"Only accept connections from this CIDR (default: {DEFAULT_NETWORK})",
    )
    parser.add_argument(
        "--network", type=_cidr_type, default=_cidr_type(DEFAULT_NETWORK),
        help=f"The network reservations are allocated from (default: {DEFAULT_NETWORK})",
    )
    parser.add_argument(
        "--reservations-file", default=DEFAULT_RESERVATIONS_FILE,
        help=f"Path to macaddr.txt (default: {DEFAULT_RESERVATIONS_FILE})",
    )
    parser.add_argument(
        "--importer-path", default=DEFAULT_IMPORTER_PATH,
        help=f"Path to pihole_importer.py (default: {DEFAULT_IMPORTER_PATH})",
    )
    parser.add_argument(
        "--python-bin", default=sys.executable,
        help="Python interpreter used to run pihole_importer.py",
    )
    parser.add_argument(
        "--token-file", default=DEFAULT_TOKEN_FILE,
        help=(
            f"File holding the bearer token (default: {DEFAULT_TOKEN_FILE}); "
            "MACADDR_SERVICE_TOKEN env var takes precedence"
        ),
    )
    return parser


def main(argv=None):
    """Parse args, load config, and serve until terminated."""
    args = build_parser().parse_args(argv)

    # stdout is block-buffered rather than line-buffered when it isn't a
    # TTY (systemd redirects it to a pipe), so without this every log
    # line -- request access logs, rejected-connection notices, even the
    # startup banner below -- sits unflushed until the process exits
    # instead of reaching journalctl in real time.
    sys.stdout.reconfigure(line_buffering=True)

    try:
        import pihole_importer
    except ModuleNotFoundError as error:
        print(
            f"error: {error}. Run this script from the same directory as "
            "pihole_importer.py (dev ADR 0004 first-party import rule).",
            file=sys.stderr,
        )
        return 1

    if not os.path.exists(args.reservations_file):
        print(f"error: reservations file not found: {args.reservations_file}", file=sys.stderr)
        return 1
    if not os.path.exists(args.importer_path):
        print(f"error: pihole_importer.py not found: {args.importer_path}", file=sys.stderr)
        return 1

    token = load_token(args.token_file)
    if token is None:
        return 1

    config = ServiceConfig(
        reservations_file=args.reservations_file,
        importer_path=args.importer_path,
        python_bin=args.python_bin,
        network_prefix=_network_prefix(args.network),
        token=token,
    )

    handler_cls = make_handler(config, pihole_importer)
    server = TrustedNetworkServer((args.bind_host, args.port), handler_cls, args.allowed_cidr)
    _install_shutdown_handler(server)

    print(
        f"macaddr-reservation-service {VERSION} listening on "
        f"{args.bind_host}:{args.port} (allowed network: {args.allowed_cidr})"
    )
    print(f"reservations file: {config.reservations_file}")

    try:
        server.serve_forever()
    finally:
        server.server_close()
    print("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
