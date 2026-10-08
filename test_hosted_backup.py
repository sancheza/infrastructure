"""
Tests for hosted_backup.py.

Covers: credentials and manifest validation, SQLite snapshots (online copy of a
WAL database, empty excluded tables, verification), the run flow against a fake
restic (repository creation, backup arguments, staging contents, retention, exit
codes, cleanup), dry-run, restore safety checks, the single-run lock, and the
mount guard.

Run with: pytest test_hosted_backup.py
"""

import json
import os
import sqlite3
import stat
import textwrap
from pathlib import Path

import pytest

import hosted_backup as hb

TESTS_FOR_VERSION = hb.VERSION

FAKE_RESTIC = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, sys
    from pathlib import Path
    import sqlite3
    args = sys.argv[1:]
    log = Path(os.environ["FAKE_RESTIC_LOG"])
    state = Path(os.environ["FAKE_RESTIC_STATE"])
    entry = {"args": args, "repo": os.environ.get("RESTIC_REPOSITORY"),
             "password_set": bool(os.environ.get("RESTIC_PASSWORD")),
             "aws_key": os.environ.get("AWS_ACCESS_KEY_ID")}
    cmd = args[0]
    if cmd == "cat":
        with log.open("a") as handle:
            handle.write(json.dumps(entry) + "\\n")
        failure = os.environ.get("FAKE_RESTIC_CAT_ERROR")
        if failure:
            print("Fatal: unable to open config file: Stat: " + failure, file=sys.stderr)
            print("Is there a repository at the following location?", file=sys.stderr)
            sys.exit(1)
        if not state.exists():
            print("Fatal: unable to open config file: Stat: The specified key does not exist.",
                  file=sys.stderr)
            print("Is there a repository at the following location?", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)
    elif cmd == "init":
        state.write_text("initialized")
    elif cmd == "backup":
        stage = Path(args[-1])
        files = sorted(str(p.relative_to(stage)) for p in stage.rglob("*") if p.is_file())
        tables = {}
        for p in stage.rglob("*.db"):
            con = sqlite3.connect(p)
            tables = {t: con.execute(f'select count(*) from "{t}"').fetchone()[0]
                      for (t,) in con.execute("select name from sqlite_master where type='table'")}
            con.close()
        entry["stage_files"] = files
        entry["stage_tables"] = tables
        entry["stage_mode"] = oct(stage.stat().st_mode & 0o777)
        print(json.dumps({"message_type": "status", "percent_done": 1}))
        print(json.dumps({"message_type": "summary", "snapshot_id": "abcdef1234567890",
                          "total_files_processed": len(files), "data_added": 2048}))
        with log.open("a") as handle:
            handle.write(json.dumps(entry) + "\\n")
        sys.exit(int(os.environ.get("FAKE_RESTIC_BACKUP_EXIT", "0")))
    elif cmd == "restore":
        target = Path(args[args.index("--target") + 1])
        (target / "restored.txt").write_text("ok")
    elif cmd == "snapshots":
        print("ID        Time                 Host    Tags")
    with log.open("a") as handle:
        handle.write(json.dumps(entry) + "\\n")
""")


@pytest.fixture(name="env")
def fixture_env(tmp_path, monkeypatch):
    """A fake host: apps root, credentials, fake restic, and shared paths."""
    apps = tmp_path / "opt"
    app = apps / "demo"
    (app / "data").mkdir(parents=True)
    db = app / "data" / "demo.db"
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE history (id INTEGER PRIMARY KEY, price REAL)")
    con.execute("CREATE TABLE search_cache (id INTEGER PRIMARY KEY, body TEXT)")
    con.executemany("INSERT INTO history (price) VALUES (?)", [(1.0,), (2.0,), (3.0,)])
    con.executemany("INSERT INTO search_cache (body) VALUES (?)", [("a",), ("b",)])
    con.commit()
    (app / ".env").write_text("SECRET=1\n")
    (app / "docker-compose.yml").write_text("services: {}\n")
    (app / "backup.toml").write_text(textwrap.dedent("""\
        app = "demo"

        [[sqlite]]
        path = "data/demo.db"
        exclude_tables = ["search_cache", "no_such_table"]

        [[files]]
        path = ".env"

        [[files]]
        path = "docker-compose.yml"
    """))
    creds = tmp_path / "backup.env"
    creds.write_text(
        "S3_ENDPOINT=http://minio:9000\nS3_BUCKET=backups\n"
        "AWS_ACCESS_KEY_ID=backup-demo\nAWS_SECRET_ACCESS_KEY=s\nRESTIC_PASSWORD=pw\n"
    )
    creds.chmod(0o600)
    restic = tmp_path / "restic"
    restic.write_text(FAKE_RESTIC)
    restic.chmod(0o755)
    log = tmp_path / "restic.log"
    monkeypatch.setenv("FAKE_RESTIC_LOG", str(log))
    monkeypatch.setenv("FAKE_RESTIC_STATE", str(tmp_path / "repo-state"))
    monkeypatch.delenv("FAKE_RESTIC_BACKUP_EXIT", raising=False)
    base = [
        "--creds",
        str(creds),
        "--apps-root",
        str(apps),
        "--restic",
        str(restic),
        "--stage-root",
        str(tmp_path / "stage"),
        "--lock-file",
        str(tmp_path / "lock"),
        "--host",
        "testhost",
    ]
    con.close()
    return {
        "base": base,
        "app": app,
        "log": log,
        "creds": creds,
        "tmp": tmp_path,
        "stage": tmp_path / "stage",
    }


def calls(env):
    """Recorded fake-restic calls."""
    if not env["log"].exists():
        return []
    return [json.loads(line) for line in env["log"].read_text().splitlines()]


def test_credentials_must_not_be_readable_by_others(env):
    """Credentials must not be readable by others."""
    env["creds"].chmod(0o644)
    with pytest.raises(hb.BackupError, match="must not be readable"):
        hb.load_credentials(env["creds"])


def test_credentials_require_a_password(tmp_path):
    """Credentials require a password."""
    path = tmp_path / "c.env"
    path.write_text("S3_BUCKET=b\n")
    path.chmod(0o600)
    with pytest.raises(hb.BackupError, match="RESTIC_PASSWORD"):
        hb.load_credentials(path)


def test_repository_for_s3_and_for_a_base_path():
    """Repository for s3 and for a base path."""
    creds = {"S3_ENDPOINT": "http://minio:9000/", "S3_BUCKET": "backups"}
    assert hb.repository_for(creds, "h", "app") == "s3:http://minio:9000/backups/h/app"
    assert hb.repository_for({"REPO_BASE": "/mnt/nas/"}, "h", "app") == "/mnt/nas/h/app"
    with pytest.raises(hb.BackupError):
        hb.repository_for({}, "h", "app")


@pytest.mark.parametrize("bad", ["/etc/passwd", "../outside", "data/../../x", ""])
def test_manifest_rejects_paths_outside_the_app_root(tmp_path, bad):
    """Manifest rejects paths outside the app root."""
    manifest = tmp_path / "backup.toml"
    manifest.write_text(f'app = "x"\n[[files]]\npath = "{bad}"\n')
    with pytest.raises(hb.BackupError):
        hb.load_manifest(manifest)


def test_manifest_requires_something_to_back_up(tmp_path):
    """Manifest requires something to back up."""
    manifest = tmp_path / "backup.toml"
    manifest.write_text('app = "x"\n')
    with pytest.raises(hb.BackupError, match="nothing to back up"):
        hb.load_manifest(manifest)


def test_snapshot_sqlite_empties_excluded_tables_and_keeps_schema(env, tmp_path):
    """Snapshot sqlite empties excluded tables and keeps schema."""
    dest = tmp_path / "copy" / "demo.db"
    missing = hb.snapshot_sqlite(
        env["app"] / "data" / "demo.db", dest, ["search_cache", "no_such_table"]
    )
    con = sqlite3.connect(dest)
    assert con.execute("select count(*) from history").fetchone()[0] == 3
    assert con.execute("select count(*) from search_cache").fetchone()[0] == 0
    assert con.execute("pragma journal_mode").fetchone()[0] == "delete"
    con.close()
    assert missing == ["no_such_table"]


def test_snapshot_sqlite_never_modifies_the_source(env, tmp_path):
    """Snapshot sqlite never modifies the source."""
    source = env["app"] / "data" / "demo.db"
    hb.snapshot_sqlite(source, tmp_path / "copy.db", ["search_cache"])
    con = sqlite3.connect(source)
    assert con.execute("select count(*) from search_cache").fetchone()[0] == 2
    con.close()


def test_run_initializes_backs_up_stages_and_cleans_up(env, capsys):
    """Run initializes backs up stages and cleans up."""
    assert hb.main(["--manifest", str(env["app"] / "backup.toml"), *env["base"], "run"]) == 0
    recorded = calls(env)
    assert [c["args"][0] for c in recorded] == ["cat", "init", "backup", "forget"]
    backup = recorded[2]
    assert backup["repo"] == "s3:http://minio:9000/backups/testhost/demo"
    assert backup["password_set"] and backup["aws_key"] == "backup-demo"
    assert backup["args"][:6] == ["backup", "--json", "--host", "testhost", "--tag", "demo"]
    assert backup["stage_files"] == [".env", "data/demo.db", "docker-compose.yml"]
    assert backup["stage_tables"] == {"history": 3, "search_cache": 0}
    assert backup["stage_mode"] == "0o700"
    forget = recorded[3]["args"]
    assert forget[forget.index("--keep-daily") + 1] == "7"
    assert forget[forget.index("--keep-weekly") + 1] == "4"
    assert "--prune" not in forget
    assert not (env["stage"] / "demo").exists()
    out = capsys.readouterr().out
    assert "warning: data/demo.db: excluded table no_such_table does not exist" in out
    assert "app=demo snapshot=abcdef12" in out


def test_run_does_not_reinitialize_an_existing_repository(env):
    """Run does not reinitialize an existing repository."""
    (env["tmp"] / "repo-state").write_text("initialized")
    assert hb.main(["--manifest", str(env["app"] / "backup.toml"), *env["base"], "run"]) == 0
    assert [c["args"][0] for c in calls(env)] == ["cat", "backup", "forget"]


def test_run_fails_on_an_incomplete_snapshot_and_still_cleans_up(env, monkeypatch, capsys):
    """Run fails on an incomplete snapshot and still cleans up."""
    monkeypatch.setenv("FAKE_RESTIC_BACKUP_EXIT", "3")
    assert hb.main(["--manifest", str(env["app"] / "backup.toml"), *env["base"], "run"]) == 1
    assert "incomplete snapshot" in capsys.readouterr().err
    assert "forget" not in [c["args"][0] for c in calls(env)]
    assert not (env["stage"] / "demo").exists()


def test_run_discovers_manifests_under_the_apps_root(env):
    """Run discovers manifests under the apps root."""
    assert hb.main([*env["base"], "run"]) == 0
    assert any(c["args"][0] == "backup" for c in calls(env))


def test_run_fails_when_the_manifest_lists_a_missing_file(env, capsys):
    """Run fails when the manifest lists a missing file."""
    (env["app"] / ".env").unlink()
    assert hb.main([*env["base"], "run", "demo"]) == 1
    assert "file not found" in capsys.readouterr().err


def test_dry_run_writes_nothing_and_does_not_back_up(env, capsys):
    """Dry run writes nothing and does not back up."""
    assert hb.main([*env["base"], "run", "--dry-run"]) == 0
    assert [c["args"][0] for c in calls(env)] == ["cat"]
    assert not env["stage"].exists()
    out = capsys.readouterr().out
    assert "would be initialized" in out and "dry-run ok" in out


def test_dry_run_reports_a_missing_path(env, capsys):
    """Dry run reports a missing path."""
    (env["app"] / "data" / "demo.db").unlink()
    assert hb.main([*env["base"], "run", "--dry-run"]) == 1
    assert "MISSING" in capsys.readouterr().out


def test_restore_refuses_the_app_directory_and_a_non_empty_target(env, capsys):
    """Restore refuses the app directory and a non empty target."""
    base = ["--manifest", str(env["app"] / "backup.toml"), *env["base"]]
    assert hb.main([*base, "restore", "demo", "--target", str(env["app"] / "data")]) == 1
    assert "refusing to restore into the app directory" in capsys.readouterr().err
    target = env["tmp"] / "scratch"
    target.mkdir()
    (target / "x").write_text("x")
    assert hb.main([*base, "restore", "demo", "--target", str(target)]) == 1
    assert "not empty" in capsys.readouterr().err


def test_restore_prints_where_the_app_files_landed(env, capsys):
    """The restored app tree sits under the staging path inside the target."""
    target = env["tmp"] / "scratch2"
    assert hb.main([*env["base"], "restore", "demo", "--target", str(target)]) == 0
    expected = target / str(env["stage"] / "demo" / "stage").lstrip("/")
    assert f"restored files: {expected}" in capsys.readouterr().out


def test_restore_passes_snapshot_filters_and_verify(env):
    """Restore passes snapshot filters and verify."""
    target = env["tmp"] / "scratch"
    assert (
        hb.main([*env["base"], "restore", "demo", "--snapshot", "latest", "--target", str(target)])
        == 0
    )
    args = calls(env)[-1]["args"]
    assert args[:2] == ["restore", "latest"]
    assert "--verify" in args and args[args.index("--tag") + 1] == "demo"
    assert (target / "restored.txt").exists()


def test_check_passes_the_read_data_subset(env):
    """Check passes the read data subset."""
    assert hb.main([*env["base"], "check", "demo", "--read-data-subset", "5%"]) == 0
    assert calls(env)[-1]["args"] == ["check", "--read-data-subset", "5%"]


def test_prune_uses_forget_with_prune(env):
    """Prune uses forget with prune."""
    assert hb.main([*env["base"], "prune", "demo"]) == 0
    args = calls(env)[-1]["args"]
    assert args[0] == "forget" and "--prune" in args


def test_a_second_run_cannot_start_while_one_holds_the_lock(env, capsys):
    """A second run cannot start while one holds the lock."""
    lock = hb.acquire_lock(env["tmp"] / "lock")
    try:
        assert hb.main([*env["base"], "run"]) == 1
        assert "already" not in capsys.readouterr().out
    finally:
        lock.close()
    assert not calls(env)


def test_require_mount_blocks_a_backup_to_an_unmounted_path(env, capsys):
    """Require mount blocks a backup to an unmounted path."""
    env["creds"].write_text(
        "REPO_BASE=/mnt/nas\nREQUIRE_MOUNT=/definitely/not/a/mount\nRESTIC_PASSWORD=pw\n"
    )
    env["creds"].chmod(0o600)
    assert hb.main([*env["base"], "run"]) == 1
    assert "not mounted" in capsys.readouterr().err
    assert not calls(env)


def test_missing_credentials_file_is_a_clear_error(env, capsys):
    """Missing credentials file is a clear error."""
    env["creds"].unlink()
    assert hb.main([*env["base"], "run"]) == 1
    assert "credentials file not found" in capsys.readouterr().err


def test_script_has_the_standard_header_and_is_executable():
    """Script has the standard header and is executable."""
    path = Path(hb.__file__)
    first = path.read_text(encoding="utf-8").splitlines()[0]
    assert first == "#!/usr/bin/env python3"
    assert path.stat().st_mode & stat.S_IXUSR
    assert os.access(path, os.X_OK)


def test_run_does_not_initialize_when_the_repository_cannot_be_opened(env, monkeypatch, capsys):
    """Access errors are reported and never answered with init."""
    monkeypatch.setenv("FAKE_RESTIC_CAT_ERROR", "Access Denied.")
    assert hb.main([*env["base"], "run"]) == 1
    assert (
        "cannot open the repository: Fatal: unable to open config file: Stat: Access Denied."
        in (capsys.readouterr().err)
    )
    assert [c["args"][0] for c in calls(env)] == ["cat"]


def test_dry_run_reports_an_unreachable_repository_as_an_error(env, monkeypatch, capsys):
    """A dry run with a bad key does not claim the repository would be created."""
    monkeypatch.setenv(
        "FAKE_RESTIC_CAT_ERROR", "The request signature we calculated does not match"
    )
    assert hb.main([*env["base"], "run", "--dry-run"]) == 1
    captured = capsys.readouterr()
    assert "would be initialized" not in captured.out
    assert "cannot open the repository" in captured.err


def test_restore_with_host_override_uses_the_original_host_repository(env):
    """On a rebuilt host, --host selects the repository and snapshots of the original name."""
    target = env["tmp"] / "rebuilt"
    base = [a for a in env["base"] if a != "testhost"]
    base[base.index("--host") + 1 :] = ["originalhost"]
    assert hb.main([*base, "restore", "demo", "--target", str(target)]) == 0
    call = calls(env)[-1]
    assert call["repo"] == "s3:http://minio:9000/backups/originalhost/demo"
    assert call["args"][call["args"].index("--host") + 1] == "originalhost"


def test_help_covers_the_common_needs(capsys):
    """The top-level help is enough for everyday use and points at the full guide."""
    with pytest.raises(SystemExit) as exc:
        hb.main(["--help"])
    assert exc.value.code == 0
    text = capsys.readouterr().out
    for topic in (
        "COMMON TASKS",
        "MANIFEST",
        "backup.toml",
        "CREDENTIALS",
        "RESTIC_PASSWORD",
        "REQUIRE_MOUNT",
        "WHERE THINGS ARE",
        "EXIT STATUS",
        "TROUBLESHOOTING",
        "hosted_backup.rst",
        "hosting-standard.md",
    ):
        assert topic in text, topic


@pytest.mark.parametrize("command", ["run", "list", "restore", "check", "prune"])
def test_every_command_has_its_own_help(command, capsys):
    """Each subcommand explains itself."""
    with pytest.raises(SystemExit) as exc:
        hb.main([command, "--help"])
    assert exc.value.code == 0
    assert len(capsys.readouterr().out.splitlines()) > 8


def test_the_troubleshooting_messages_in_the_help_are_real_messages():
    """Every message the help tells a user to look for appears in the code."""
    source = Path(hb.__file__).read_text(encoding="utf-8")
    for message in (
        "must not be readable by group or others",
        "cannot open the repository",
        "another hosted-backup run is in progress",
        "is not mounted",
        "no manifest for app",
    ):
        assert message in source, message


def test_extra_manifests_from_the_credentials_file_are_backed_up(env, tmp_path):
    """EXTRA_MANIFESTS adds manifests that do not live under the apps root."""
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "conf.txt").write_text("x\n", encoding="utf-8")
    (other / "backup.toml").write_text(
        'app = "other"\n[[files]]\npath = "conf.txt"\n', encoding="utf-8"
    )
    with env["creds"].open("a", encoding="utf-8") as handle:
        handle.write(f"EXTRA_MANIFESTS={other / 'backup.toml'}\n")
    assert hb.main([*env["base"], "run"]) == 0
    repos = {c["repo"] for c in calls(env) if c["args"][0] == "backup"}
    assert repos == {
        "s3:http://minio:9000/backups/testhost/demo",
        "s3:http://minio:9000/backups/testhost/other",
    }


def test_a_host_with_only_extra_manifests_needs_no_apps_root(env, tmp_path):
    """A host with no /opt apps still runs from EXTRA_MANIFESTS alone."""
    other = tmp_path / "solo"
    other.mkdir()
    (other / "conf.txt").write_text("x\n", encoding="utf-8")
    (other / "backup.toml").write_text(
        'app = "solo"\n[[files]]\npath = "conf.txt"\n', encoding="utf-8"
    )
    with env["creds"].open("a", encoding="utf-8") as handle:
        handle.write(f"EXTRA_MANIFESTS={other / 'backup.toml'}\n")
    empty = tmp_path / "empty-root"
    empty.mkdir()
    base = list(env["base"])
    base[base.index("--apps-root") + 1] = str(empty)
    assert hb.main([*base, "run"]) == 0
    assert {c["repo"] for c in calls(env) if c["args"][0] == "backup"} == {
        "s3:http://minio:9000/backups/testhost/solo"
    }


def _manifest_with_directory(env):
    """Add a directory of files to the demo app's manifest."""
    photos = env["app"] / "photos"
    photos.mkdir()
    (photos / "a.jpg").write_bytes(b"a" * 100)
    (photos / "sub").mkdir()
    (photos / "sub" / "b.jpg").write_bytes(b"b" * 100)
    manifest = env["app"] / "backup.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + '\n[[files]]\npath = "photos"\n', encoding="utf-8"
    )
    return photos


def test_directories_are_hard_linked_into_the_staging_tree(env):
    """Staging a directory takes no extra space: files are linked, not copied."""
    photos = _manifest_with_directory(env)
    manifest = hb.load_manifest(env["app"] / "backup.toml")
    stage = env["tmp"] / "stage-links"
    hb.stage_manifest(manifest, stage)
    assert (stage / "photos" / "a.jpg").stat().st_ino == (photos / "a.jpg").stat().st_ino
    assert (stage / "photos" / "sub" / "b.jpg").stat().st_nlink == 2
    assert (stage / ".env").stat().st_ino != (env["app"] / ".env").stat().st_ino


def test_staging_falls_back_to_copying_when_links_are_refused(env, monkeypatch):
    """A filesystem that refuses hard links still stages every file."""
    photos = _manifest_with_directory(env)

    def refuse(*_args, **_kwargs):
        raise OSError("cross-device link")

    monkeypatch.setattr(hb.os, "link", refuse)
    manifest = hb.load_manifest(env["app"] / "backup.toml")
    stage = env["tmp"] / "stage-copy"
    hb.stage_manifest(manifest, stage)
    copied = stage / "photos" / "a.jpg"
    assert copied.read_bytes() == b"a" * 100
    assert copied.stat().st_ino != (photos / "a.jpg").stat().st_ino


def test_a_run_backs_up_a_staged_directory_and_leaves_the_source_alone(env):
    """The staged links are removed after a run; the originals remain."""
    photos = _manifest_with_directory(env)
    assert hb.main([*env["base"], "run", "demo"]) == 0
    backup = next(c for c in calls(env) if c["args"][0] == "backup")
    assert "photos/a.jpg" in backup["stage_files"]
    assert "photos/sub/b.jpg" in backup["stage_files"]
    assert (photos / "a.jpg").read_bytes() == b"a" * 100
    assert (photos / "a.jpg").stat().st_nlink == 1
    assert not (env["stage"] / "demo").exists()
