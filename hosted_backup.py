#!/usr/bin/env python3
"""
hosted_backup.py

Back up hosted apps to a restic repository, driven by a per-app manifest
(hosting standard, section 9). Class A and class B data go into the repository;
re-creatable data is excluded.

Each app has /opt/<app>/backup.toml listing SQLite databases (with the tables
to empty in the backup copy) and files. The tool takes a consistent online copy
of every database, empties the excluded tables, stages everything under a fixed
path, and adds it to the app's repository with restic.

Repository: s3:<S3_ENDPOINT>/<S3_BUCKET>/<host>/<app>, or <REPO_BASE>/<host>/<app>
when the credentials file sets REPO_BASE (a local or mounted path).

Usage:
    hosted_backup.py run --dry-run
    hosted_backup.py run ticketsniper
    hosted_backup.py list ticketsniper
    hosted_backup.py restore ticketsniper --snapshot latest --target /tmp/restore-test
    hosted_backup.py check --read-data-subset 5%
    hosted_backup.py prune
"""

import argparse
import dataclasses
import fcntl
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Dict, List, Optional

VERSION = "1.0.0"

DEFAULT_CREDS = "/etc/hosted-backup/backup.env"
DEFAULT_APPS_ROOT = "/opt"
DEFAULT_STAGE_ROOT = "/var/tmp/hosted-backup"
DEFAULT_LOCK = "/run/hosted-backup.lock"
KEEP_DAILY = "7"
KEEP_WEEKLY = "4"
# restic messages that mean "no repository here" (S3 object missing, local path missing)
MISSING_REPOSITORY_MESSAGES = ("The specified key does not exist", "no such file or directory")


class BackupError(Exception):
    """A failure that ends the current command."""


@dataclasses.dataclass(frozen=True)
class SqliteItem:
    """One database to back up."""

    path: str
    exclude_tables: List[str]


@dataclasses.dataclass(frozen=True)
class Manifest:
    """A parsed backup.toml."""

    app: str
    root: Path
    sqlite: List[SqliteItem]
    files: List[str]


def load_credentials(path: Path) -> Dict[str, str]:
    """Read the host credentials file.

    Args:
        path: Credentials file (KEY=value lines).

    Returns:
        The keys and values.

    Raises:
        BackupError: The file is missing, readable by group or others, or invalid.
    """
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError as exc:
        raise BackupError(f"credentials file not found: {path}") from exc
    if mode & 0o077:
        raise BackupError(
            f"{path} must not be readable by group or others (mode {mode:03o}); use 600"
        )
    creds: Dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not key.strip():
            raise BackupError(f"{path}:{number}: expected KEY=value")
        creds[key.strip()] = value
    if not creds.get("RESTIC_PASSWORD"):
        raise BackupError(f"{path}: RESTIC_PASSWORD is not set")
    return creds


def repository_for(creds: Dict[str, str], host: str, app: str) -> str:
    """Build the restic repository location for one app on one host."""
    base = creds.get("REPO_BASE", "").strip()
    if base:
        return f"{base.rstrip('/')}/{host}/{app}"
    endpoint = creds.get("S3_ENDPOINT", "").strip()
    bucket = creds.get("S3_BUCKET", "").strip()
    if not endpoint or not bucket:
        raise BackupError("credentials need S3_ENDPOINT and S3_BUCKET, or REPO_BASE")
    return f"s3:{endpoint.rstrip('/')}/{bucket}/{host}/{app}"


def restic_environment(creds: Dict[str, str], repository: str) -> Dict[str, str]:
    """Environment for a restic subprocess."""
    env = dict(os.environ)
    env["RESTIC_REPOSITORY"] = repository
    env["RESTIC_PASSWORD"] = creds["RESTIC_PASSWORD"]
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION"):
        if creds.get(key):
            env[key] = creds[key]
    return env


def _relative_path(value: object, what: str) -> str:
    """Validate a manifest path: relative, inside the app root."""
    if not isinstance(value, str) or not value:
        raise BackupError(f"manifest: {what} must be a non-empty string")
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise BackupError(
            f"manifest: {what} must be relative and stay inside the app root: {value}"
        )
    return value


def load_manifest(path: Path) -> Manifest:
    """Parse and validate a backup.toml.

    Args:
        path: Path to backup.toml. The app root is its directory.

    Returns:
        The manifest.

    Raises:
        BackupError: The file is missing or invalid.
    """
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise BackupError(f"manifest not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise BackupError(f"{path}: invalid TOML: {exc}") from exc
    app = data.get("app")
    if not isinstance(app, str) or not app or "/" in app:
        raise BackupError(f"{path}: 'app' must be a name without slashes")
    sqlite_items: List[SqliteItem] = []
    for entry in data.get("sqlite", []):
        tables = entry.get("exclude_tables", [])
        if not isinstance(tables, list) or not all(isinstance(t, str) and t for t in tables):
            raise BackupError(f"{path}: exclude_tables must be a list of table names")
        sqlite_items.append(
            SqliteItem(_relative_path(entry.get("path"), "[[sqlite]].path"), tables)
        )
    files = [_relative_path(entry.get("path"), "[[files]].path") for entry in data.get("files", [])]
    if not sqlite_items and not files:
        raise BackupError(f"{path}: nothing to back up")
    return Manifest(app=app, root=path.parent, sqlite=sqlite_items, files=files)


def discover_manifests(apps_root: Path) -> List[Path]:
    """Find every <apps_root>/*/backup.toml."""
    return sorted(apps_root.glob("*/backup.toml"))


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def snapshot_sqlite(source: Path, dest: Path, exclude_tables: List[str]) -> List[str]:
    """Copy a database online, empty the excluded tables, and verify the copy.

    Args:
        source: Live database. Opened read-only.
        dest: Copy to create.
        exclude_tables: Tables whose rows are removed from the copy.

    Returns:
        Excluded table names that do not exist in the database.

    Raises:
        BackupError: The source is missing or the copy fails its integrity check.
    """
    if not source.is_file():
        raise BackupError(f"database not found: {source}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
        existing = {
            row[0] for row in dst.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        missing = [name for name in exclude_tables if name not in existing]
        for name in exclude_tables:
            if name in existing:
                dst.execute(f"DELETE FROM {_quote(name)}")
        dst.commit()
        dst.execute("PRAGMA journal_mode=DELETE")
        dst.execute("VACUUM")
        result = dst.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        dst.close()
        src.close()
    if result != "ok":
        raise BackupError(f"integrity check failed on the copy of {source}: {result}")
    return missing


def link_or_copy(source: str, target: str) -> str:
    """Hard-link a file into the staging tree; copy it when the filesystem does not allow a link.

    A hard link takes no extra space, so staging a large directory (photos, for example)
    does not need room for a second copy. Files in such directories are not modified in place.
    """
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)
    return target


def stage_manifest(manifest: Manifest, stage_dir: Path) -> List[str]:
    """Build the staging tree for one app.

    Returns:
        Warnings about excluded tables that do not exist.
    """
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True, mode=0o700)
    warnings: List[str] = []
    for item in manifest.sqlite:
        missing = snapshot_sqlite(
            manifest.root / item.path, stage_dir / item.path, item.exclude_tables
        )
        warnings.extend(f"{item.path}: excluded table {name} does not exist" for name in missing)
    for rel in manifest.files:
        source = manifest.root / rel
        target = stage_dir / rel
        if not source.exists():
            raise BackupError(f"file not found: {source}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, symlinks=True, copy_function=link_or_copy)
        else:
            shutil.copy2(source, target)
    return warnings


class Restic:
    """Thin wrapper around the restic command."""

    def __init__(self, binary: str, env: Dict[str, str]) -> None:
        self.binary = binary
        self.env = env

    def run(
        self, args: List[str], check: bool = True, capture: bool = True
    ) -> subprocess.CompletedProcess:
        """Run restic with the given arguments."""
        try:
            result = subprocess.run(
                [self.binary, *args],
                env=self.env,
                text=True,
                check=False,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE if capture else None,
            )
        except FileNotFoundError as exc:
            raise BackupError(f"restic not found: {self.binary}") from exc
        if check and result.returncode != 0:
            detail = (result.stderr or "").strip().splitlines()
            raise BackupError(
                f"restic {args[0]} failed (exit {result.returncode}): "
                f"{detail[-1] if detail else 'no error output'}"
            )
        return result

    def repository_exists(self) -> bool:
        """Report whether the repository exists.

        Returns:
            True when it can be opened, False when restic reports it is not there.

        Raises:
            BackupError: restic failed for another reason (credentials, policy, network).
        """
        result = self.run(["cat", "config"], check=False)
        if result.returncode == 0:
            return True
        message = (result.stderr or "").strip()
        if any(text in message for text in MISSING_REPOSITORY_MESSAGES):
            return False
        lines = message.splitlines()
        raise BackupError(f"cannot open the repository: {lines[0] if lines else 'no error output'}")

    def ensure_repository(self) -> bool:
        """Create the repository if it does not exist.

        Returns:
            True when it was created.
        """
        if self.repository_exists():
            return False
        self.run(["init"])
        return True


def parse_summary(output: str) -> Dict[str, object]:
    """Return the summary message from `restic backup --json` output."""
    summary: Dict[str, object] = {}
    for line in output.splitlines():
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict) and message.get("message_type") == "summary":
            summary = message
    return summary


def human_size(count: object) -> str:
    """Format a byte count."""
    value = float(count or 0)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{value:.1f}GiB"


def check_mount(creds: Dict[str, str]) -> None:
    """Fail when REQUIRE_MOUNT names a path that is not a mount point."""
    required = creds.get("REQUIRE_MOUNT", "").strip()
    if required and not os.path.ismount(required):
        raise BackupError(
            f"{required} is not mounted; refusing to write to the underlying directory"
        )


@dataclasses.dataclass
class Context:
    """Settings shared by the commands."""

    creds_path: Path
    apps_root: Path
    stage_root: Path
    lock_file: Path
    host: str
    restic_binary: str
    manifest_path: Optional[Path]


def extra_manifest_paths(ctx: Context) -> List[Path]:
    """Manifests named by EXTRA_MANIFESTS in the credentials file (comma-separated paths)."""
    if not ctx.creds_path.exists():
        return []
    value = load_credentials(ctx.creds_path).get("EXTRA_MANIFESTS", "")
    return [Path(item.strip()) for item in value.split(",") if item.strip()]


def select_manifests(ctx: Context, app: Optional[str]) -> List[Manifest]:
    """Load the manifests a command applies to."""
    if ctx.manifest_path:
        paths = [ctx.manifest_path]
    else:
        paths = discover_manifests(ctx.apps_root) + extra_manifest_paths(ctx)
    manifests = [load_manifest(path) for path in paths]
    if app:
        manifests = [m for m in manifests if m.app == app]
        if not manifests:
            raise BackupError(f"no manifest for app '{app}'")
    if not manifests:
        raise BackupError(f"no backup.toml found under {ctx.apps_root}")
    return manifests


def restic_for(ctx: Context, manifest: Manifest) -> "Restic":
    """Build a Restic bound to one app's repository."""
    creds = load_credentials(ctx.creds_path)
    check_mount(creds)
    repository = repository_for(creds, ctx.host, manifest.app)
    return Restic(ctx.restic_binary, restic_environment(creds, repository))


def report_dry_run(manifest: Manifest, restic: "Restic") -> None:
    """Report what a backup would do, and fail when a listed path is missing."""
    repository = restic.env["RESTIC_REPOSITORY"]
    state = "exists" if restic.repository_exists() else "would be initialized"
    print(f"app={manifest.app} repository={repository} ({state})")
    missing = False
    for item in manifest.sqlite:
        path = manifest.root / item.path
        missing = missing or not path.is_file()
        size = human_size(path.stat().st_size) if path.is_file() else "MISSING"
        tables = ", ".join(item.exclude_tables) or "none"
        print(f"  sqlite {item.path} ({size}); empty tables: {tables}")
    for rel in manifest.files:
        exists = (manifest.root / rel).exists()
        missing = missing or not exists
        print(f"  file   {rel}{'' if exists else ' MISSING'}")
    if missing:
        raise BackupError(f"app={manifest.app}: manifest lists a path that does not exist")
    print(f"app={manifest.app} dry-run ok")


def backup_app(ctx: Context, manifest: Manifest, dry_run: bool) -> None:
    """Back up one app (or report what would be backed up)."""
    restic = restic_for(ctx, manifest)
    if dry_run:
        report_dry_run(manifest, restic)
        return
    stage_dir = ctx.stage_root / manifest.app / "stage"
    try:
        for warning in stage_manifest(manifest, stage_dir):
            print(f"warning: {warning}")
        if restic.ensure_repository():
            print(f"app={manifest.app} initialized repository {restic.env['RESTIC_REPOSITORY']}")
        command = ["backup", "--json", "--host", ctx.host, "--tag", manifest.app, str(stage_dir)]
        result = restic.run(command, check=False)
        summary = parse_summary(result.stdout or "")
        if result.returncode == 3:
            raise BackupError(f"app={manifest.app}: incomplete snapshot (some files unreadable)")
        if result.returncode != 0:
            detail = (result.stderr or "").strip().splitlines()
            raise BackupError(
                f"restic backup failed (exit {result.returncode}): "
                f"{detail[-1] if detail else 'no error output'}"
            )
        restic.run(
            ["forget", "--host", ctx.host, "--tag", manifest.app]
            + ["--keep-daily", KEEP_DAILY, "--keep-weekly", KEEP_WEEKLY]
        )
        print(
            f"app={manifest.app} snapshot={str(summary.get('snapshot_id', ''))[:8]} "
            f"files={summary.get('total_files_processed', '?')} "
            f"added={human_size(summary.get('data_added'))}"
        )
    finally:
        shutil.rmtree(ctx.stage_root / manifest.app, ignore_errors=True)


def acquire_lock(path: Path):
    """Take the single-run lock, or fail when another run holds it."""
    try:
        handle = open(path, "w", encoding="utf-8")  # pylint: disable=consider-using-with
    except OSError as exc:
        raise BackupError(f"cannot open lock file {path}: {exc}") from exc
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise BackupError("another hosted-backup run is in progress") from exc
    return handle


def cmd_run(ctx: Context, args: argparse.Namespace) -> int:
    """Back up one app or every app."""
    manifests = select_manifests(ctx, args.app)
    lock = acquire_lock(ctx.lock_file)
    failures = 0
    try:
        for manifest in manifests:
            try:
                backup_app(ctx, manifest, args.dry_run)
            except BackupError as exc:
                failures += 1
                print(f"Error: {exc}", file=sys.stderr)
    finally:
        lock.close()
    return 1 if failures else 0


def cmd_list(ctx: Context, args: argparse.Namespace) -> int:
    """List an app's snapshots."""
    manifest = select_manifests(ctx, args.app)[0]
    restic = restic_for(ctx, manifest)
    result = restic.run(["snapshots", "--host", ctx.host, "--tag", manifest.app])
    print(result.stdout, end="")
    return 0


def cmd_restore(ctx: Context, args: argparse.Namespace) -> int:
    """Restore a snapshot into a scratch directory."""
    manifest = select_manifests(ctx, args.app)[0]
    target = Path(args.target).resolve()
    if manifest.root.resolve() in (target, *target.parents):
        raise BackupError(f"refusing to restore into the app directory: {target}")
    if target.exists() and any(target.iterdir()):
        raise BackupError(f"restore target is not empty: {target}")
    restic = restic_for(ctx, manifest)
    if args.dry_run:
        print(f"app={manifest.app} would restore snapshot {args.snapshot} to {target}")
        return 0
    target.mkdir(parents=True, exist_ok=True)
    restic.run(
        [
            "restore",
            args.snapshot,
            "--target",
            str(target),
            "--host",
            ctx.host,
            "--tag",
            manifest.app,
            "--verify",
        ]
    )
    restored_root = target / str(ctx.stage_root / manifest.app / "stage").lstrip("/")
    print(f"app={manifest.app} restored snapshot {args.snapshot} to {target}")
    print(f"restored files: {restored_root}")
    return 0


def cmd_check(ctx: Context, args: argparse.Namespace) -> int:
    """Check repository integrity."""
    failures = 0
    for manifest in select_manifests(ctx, args.app):
        try:
            restic = restic_for(ctx, manifest)
            command = ["check"]
            if args.read_data_subset:
                command += ["--read-data-subset", args.read_data_subset]
            restic.run(command)
            print(f"app={manifest.app} check ok")
        except BackupError as exc:
            failures += 1
            print(f"Error: {exc}", file=sys.stderr)
    return 1 if failures else 0


def cmd_prune(ctx: Context, args: argparse.Namespace) -> int:
    """Apply retention and remove unreferenced data."""
    failures = 0
    for manifest in select_manifests(ctx, args.app):
        try:
            restic = restic_for(ctx, manifest)
            command = [
                "forget",
                "--host",
                ctx.host,
                "--tag",
                manifest.app,
                "--keep-daily",
                KEEP_DAILY,
                "--keep-weekly",
                KEEP_WEEKLY,
            ]
            if args.dry_run:
                command.append("--dry-run")
            else:
                command.append("--prune")
            restic.run(command)
            print(f"app={manifest.app} prune ok")
        except BackupError as exc:
            failures += 1
            print(f"Error: {exc}", file=sys.stderr)
    return 1 if failures else 0


MAIN_DESCRIPTION = """\
Back up hosted apps to restic repositories, driven by a per-app manifest.

Each app lists what to keep in /opt/<app>/backup.toml. The tool takes a consistent
copy of the app's SQLite databases (emptying the tables that can be re-created),
adds the copies and the listed files to a restic repository, and keeps 7 daily and
4 weekly snapshots. restic encrypts the data before it leaves the host.
"""

MAIN_EPILOG = """\
COMMON TASKS
  Preview every app's backup (creates nothing):
      hosted-backup run --dry-run
  Back up one app now:
      hosted-backup run ticketsniper
  List its snapshots:
      hosted-backup list ticketsniper
  Test a backup: restore to a scratch directory, then check the database:
      hosted-backup restore ticketsniper --target /tmp/restore-test
      sqlite3 /tmp/restore-test/var/tmp/hosted-backup/ticketsniper/stage/data/<db> "PRAGMA integrity_check;"
      rm -rf /tmp/restore-test        (it contains .env)
  Check the repository (reads 5% of the stored data):
      hosted-backup check --read-data-subset 5%
  See the scheduled runs:
      systemctl list-timers 'hosted-backup*'
      journalctl -u hosted-backup.service -n 20

MANIFEST  /opt/<app>/backup.toml  (data that cannot be re-created, and config)
      app = "myapp"
      [[sqlite]]
      path = "data/app.db"                  # relative to /opt/<app>
      exclude_tables = ["search_cache"]     # emptied in the backup copy
      [[files]]
      path = ".env"

CREDENTIALS  /etc/hosted-backup/backup.env  (mode 600; a looser mode is refused)
      S3_ENDPOINT=http://minio:9000   S3_BUCKET=backups
      AWS_ACCESS_KEY_ID=...           AWS_SECRET_ACCESS_KEY=...
      RESTIC_PASSWORD=...             without it the backups cannot be read
  A mounted repository instead of S3: REPO_BASE=<path> and REQUIRE_MOUNT=<mount point>.
  Apps outside /opt: EXTRA_MANIFESTS=<path>,<path> adds manifests to every run.

WHERE THINGS ARE
  repository    <S3_ENDPOINT>/<S3_BUCKET>/<host>/<app>   (or <REPO_BASE>/<host>/<app>)
  staging       /var/tmp/hosted-backup/<app>/            (removed after each run)
  restored      <target>/var/tmp/hosted-backup/<app>/stage/
  run lock      /run/hosted-backup.lock

EXIT STATUS
  0 success; 1 failure (message on stderr); 2 usage error. With several apps, one
  failure does not stop the others, and the exit status is 1.

TROUBLESHOOTING
  must not be readable by group or others    chmod 600 /etc/hosted-backup/backup.env
  cannot open the repository: ... Access Denied      the host's MinIO key or policy
  ... signature we calculated does not match         wrong AWS_SECRET_ACCESS_KEY
  another hosted-backup run is in progress           wait; a run holds the lock
  ... is not mounted                                 mount the REQUIRE_MOUNT path first
  no manifest for app                                no /opt/<app>/backup.toml; copy it from
                                                     the app repo, or pass --manifest

MORE
  Full guide:  ~/dev/infrastructure/docs/hosted_backup.rst
  Design:      ~/dev/docs/hosting-standard.md, section 9
"""

RUN_EPILOG = """\
What a run does, per app: read the manifest, copy each database online (read-only),
empty the excluded tables in the copy, vacuum and integrity-check the copy, stage it
with the listed files, add the staging tree to the repository (creating the repository
on the first run), then apply retention. The live database is never changed. Exit
status is 1 if any app fails.

  hosted-backup run --dry-run          every app: show the plan, create nothing
  hosted-backup run ticketsniper       one app
"""

LIST_EPILOG = """\
Shows the snapshots for this host and app. Use an ID from this list with
`restore --snapshot <id>`.
"""

RESTORE_EPILOG = """\
Restores into an empty directory. The tool refuses a non-empty target and any target
inside the app's own directory. The app's files land under the staging path inside the
target and the tool prints that path:

  hosted-backup restore ticketsniper --target /tmp/restore-test
  restored files: /tmp/restore-test/var/tmp/hosted-backup/ticketsniper/stage

Restored files are verified against the repository. The restore needs the app's
manifest (/opt/<app>/backup.toml, or --manifest). On a rebuilt host with a different
name, put --host <original host name> before the command; the repository and its
snapshots are filed under the original name. Delete the target afterwards: it
contains .env.
"""

CHECK_EPILOG = """\
Without --read-data-subset only the repository structure is checked. With it, that share
of the stored data is also read and verified, for example 5%. A weekly timer runs
`check --read-data-subset 5%`.
"""

PRUNE_EPILOG = """\
`run` already forgets snapshots beyond 7 daily and 4 weekly. `prune` also removes the
data only those snapshots used, which takes longer, so a weekly timer runs it. With
--dry-run it reports what would be removed and removes nothing.
"""


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        description=MAIN_DESCRIPTION,
        epilog=MAIN_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument(
        "--creds", default=DEFAULT_CREDS, help="credentials file (default: %(default)s)"
    )
    parser.add_argument(
        "--apps-root",
        default=DEFAULT_APPS_ROOT,
        help="directory holding <app>/backup.toml (default: %(default)s)",
    )
    parser.add_argument("--manifest", help="use this backup.toml instead of discovering manifests")
    parser.add_argument(
        "--host",
        default=socket.gethostname().split(".")[0],
        help="host name in repository paths and snapshots (default: this host's short name)",
    )
    parser.add_argument(
        "--restic",
        default=os.environ.get("HOSTED_BACKUP_RESTIC", "restic"),
        help="restic binary (default: %(default)s)",
    )
    parser.add_argument(
        "--stage-root", default=DEFAULT_STAGE_ROOT, help="staging directory (default: %(default)s)"
    )
    parser.add_argument(
        "--lock-file", default=DEFAULT_LOCK, help="single-run lock (default: %(default)s)"
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    raw = argparse.RawDescriptionHelpFormatter

    run = sub.add_parser(
        "run",
        help="back up one app, or every app",
        description="Back up one app, or every app that has a backup.toml.",
        epilog=RUN_EPILOG,
        formatter_class=raw,
    )
    run.add_argument("app", nargs="?", help="app name (default: every manifest)")
    run.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="report what would be backed up; change nothing",
    )
    run.set_defaults(func=cmd_run)

    lst = sub.add_parser(
        "list",
        help="list an app's snapshots",
        description="List an app's snapshots.",
        epilog=LIST_EPILOG,
        formatter_class=raw,
    )
    lst.add_argument("app")
    lst.set_defaults(func=cmd_list)

    restore = sub.add_parser(
        "restore",
        help="restore a snapshot into a scratch directory",
        description="Restore a snapshot into an empty scratch directory.",
        epilog=RESTORE_EPILOG,
        formatter_class=raw,
    )
    restore.add_argument("app")
    restore.add_argument(
        "--snapshot", default="latest", help="snapshot id or latest (default: %(default)s)"
    )
    restore.add_argument(
        "--target", required=True, help="empty directory outside the app directory"
    )
    restore.add_argument("-n", "--dry-run", action="store_true", help="report only")
    restore.set_defaults(func=cmd_restore)

    check = sub.add_parser(
        "check",
        help="check repository integrity",
        description="Check repository integrity for one app, or every app.",
        epilog=CHECK_EPILOG,
        formatter_class=raw,
    )
    check.add_argument("app", nargs="?")
    check.add_argument(
        "--read-data-subset", metavar="N%", help="also read this share of the stored data"
    )
    check.set_defaults(func=cmd_check)

    prune = sub.add_parser(
        "prune",
        help="apply retention and remove unreferenced data",
        description="Apply retention and remove data no snapshot uses.",
        epilog=PRUNE_EPILOG,
        formatter_class=raw,
    )
    prune.add_argument("app", nargs="?")
    prune.add_argument("-n", "--dry-run", action="store_true", help="report only")
    prune.set_defaults(func=cmd_prune)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point."""
    args = build_parser().parse_args(argv)
    ctx = Context(
        creds_path=Path(args.creds),
        apps_root=Path(args.apps_root),
        stage_root=Path(args.stage_root),
        lock_file=Path(args.lock_file),
        host=args.host,
        restic_binary=args.restic,
        manifest_path=Path(args.manifest) if args.manifest else None,
    )
    try:
        return args.func(ctx, args)
    except BackupError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
