Hosted App Backup
=================

**Files:** ``hosted_backup.py`` (installed as ``hosted-backup``),
``hosted-backup.service``, ``hosted-backup.timer``,
``hosted-backup-maintenance.service``, ``hosted-backup-maintenance.timer``

``hosted-backup --help`` covers everyday use: common tasks, the manifest, the
credentials file, file locations, exit status, and the common errors. This page
adds the procedures: installing, adding an app, restoring, recovering a lost
host, rotating credentials, and troubleshooting. The design behind it is in
``~/dev/docs/hosting-standard.md``, section 9.

1. Overview
-----------

One script backs up every hosted app on a host. Each app has a manifest,
``/opt/<app>/backup.toml``, that lists what is worth keeping. For each run the
tool does this for every app:

.. list-table::
   :header-rows: 1
   :widths: 8 92

   * - Step
     - Action
   * - 1
     - Read the manifest and check every listed path exists.
   * - 2
     - Copy each SQLite database online, opened read-only. The live database is never changed.
   * - 3
     - Empty the tables the manifest lists as re-creatable in the copy, then ``VACUUM`` it and run ``PRAGMA integrity_check``.
   * - 4
     - Stage the copies and the listed files under ``/var/tmp/hosted-backup/<app>/stage`` (mode 700). Listed directories are hard-linked, not copied, so a large directory (photos) needs no extra disk space; files are copied. Where the filesystem refuses a link, that file is copied.
   * - 5
     - Add the staging tree to the app's restic repository, creating the repository on the first run.
   * - 6
     - Apply retention: 7 daily and 4 weekly snapshots.
   * - 7
     - Delete the staging tree.

restic encrypts the data before it leaves the host. A run holds a lock, so two
runs never overlap.

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Setting
     - Value
   * - Repository
     - ``<S3_ENDPOINT>/<S3_BUCKET>/<host>/<app>`` or ``<REPO_BASE>/<host>/<app>``
   * - Daily run
     - ``hosted-backup.timer``, 03:15 local time plus up to 10 minutes of random delay
   * - Weekly maintenance
     - ``hosted-backup-maintenance.timer``, Sunday 04:30: ``prune`` and ``check --read-data-subset 5%``
   * - Staging
     - ``/var/tmp/hosted-backup/<app>/`` (removed after each run)
   * - Lock
     - ``/run/hosted-backup.lock``
   * - Data classes
     - A (irreplaceable) and B (configuration) are backed up; C (re-creatable) is not

2. Install on a host
--------------------

**Starting Host & Directory:** the machine that runs Ansible, ``~/dev/iac``.

The ``hosted_backup`` role installs restic, the script, and the four systemd
units. It enables the timers once the credentials file exists.

.. code-block:: bash

    export HOSTED_BACKUP_SRC_DIR=~/dev/infrastructure
    ansible-playbook -i <host>, -u root playbook.yml

``playbook.yml`` contains:

.. code-block:: yaml

    - hosts: all
      gather_facts: false
      roles:
        - role: ../../ansible/roles/hosted_backup

Expected output (last lines):

.. code-block:: text

    TASK [... : Confirm the installed version] ****
    ok: [<host>]
    TASK [... : Show the installed version] ****
    ok: [<host>] => { "msg": "hosted-backup 1.0.0" }
    PLAY RECAP ****
    <host> : ok=12 changed=6 failed=0

Running the role again changes nothing except a file that has changed in the
source directory.

To have the role write the credentials file, pass the values as a dictionary
from a file that is not in a repository:

.. code-block:: bash

    ansible-playbook -i <host>, -u root playbook.yml -e @/path/to/secret-vars.json

.. code-block:: json

    {"hosted_backup_credentials": {"S3_ENDPOINT": "http://minio:9000", "S3_BUCKET": "backups",
     "AWS_ACCESS_KEY_ID": "backup", "AWS_SECRET_ACCESS_KEY": "...", "RESTIC_PASSWORD": "..."}}

The role hides these values from its output and writes the file with mode 600.

.. note::

   Installing by hand: install the ``restic`` package, copy ``hosted_backup.py``
   to ``/usr/local/bin/hosted-backup`` (mode 755), copy the four units to
   ``/etc/systemd/system``, run ``systemctl daemon-reload``, create the
   credentials file (section 3), then
   ``systemctl enable --now hosted-backup.timer hosted-backup-maintenance.timer``.

3. The credentials file
-----------------------

``/etc/hosted-backup/backup.env``, owner root, **mode 600**. The tool refuses
to run when group or others can read it.

.. list-table::
   :header-rows: 1
   :widths: 24 12 64

   * - Key
     - Required
     - Meaning
   * - ``RESTIC_PASSWORD``
     - Yes
     - Encrypts the repositories. Without it the backups cannot be read.
   * - ``S3_ENDPOINT``
     - S3
     - With scheme and port, for example ``http://minio:9000``
   * - ``S3_BUCKET``
     - S3
     - Bucket that holds the repositories, for example ``backups``
   * - ``AWS_ACCESS_KEY_ID``, ``AWS_SECRET_ACCESS_KEY``
     - S3
     - The shared ``backup`` account's key, limited to the backup bucket
   * - ``AWS_DEFAULT_REGION``
     - No
     - Only when the S3 service requires one
   * - ``REPO_BASE``
     - Mounted repository
     - Local or mounted path. The repository is ``<REPO_BASE>/<host>/<app>``. Replaces the S3 keys.
   * - ``REQUIRE_MOUNT``
     - No
     - A mount point that must be mounted. The run fails when it is not, so nothing is written to the directory under an unmounted share.
   * - ``EXTRA_MANIFESTS``
     - No
     - Comma-separated manifest paths for apps that do not live under ``/opt``

The repository password
~~~~~~~~~~~~~~~~~~~~~~~

The password is unique per host and covers every app on it. Keep a copy off the
host in a password manager. **Do not keep the only copy on the host that is
backed up**, and do not keep a copy next to a repository it protects: the
encryption is then no protection.

4. Add an app
-------------

4.1 Classify the data
~~~~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 8 40 52

   * - Class
     - Meaning
     - Backed up
   * - A
     - User-created or accumulated data that cannot be fetched or derived again
     - Yes
   * - B
     - Configuration: ``.env``, ``docker-compose.yml``, ``backup.toml``
     - Yes
   * - C
     - Caches, search indexes, browser profiles, logs, derived tables
     - No

A table or directory is class C only when the app shows how to rebuild it
**and a restore test confirms the rebuild**. Otherwise it is class A. Read how
the app writes each table: a table that looks derived can hold history that
nothing recomputes. Class C tables are *emptied* in the backup copy, not
dropped, so the restored database still starts.

4.2 Write the manifest
~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: toml

    app = "myapp"

    [[sqlite]]
    path = "data/app.db"
    exclude_tables = ["search_cache"]

    [[files]]
    path = ".env"

    [[files]]
    path = "docker-compose.yml"

    [[files]]
    path = "backup.toml"

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Key
     - Meaning
   * - ``app``
     - The app name; also the repository name. No slashes.
   * - ``[[sqlite]].path``
     - Database path relative to the manifest's directory. Absolute paths and ``..`` are refused.
   * - ``[[sqlite]].exclude_tables``
     - Class C tables to empty in the copy. A name that does not exist is reported as a warning.
   * - ``[[files]].path``
     - File or directory, relative to the manifest's directory

Put the manifest in the app's repository next to ``docker-compose.yml`` and
copy it to ``/opt/<app>/backup.toml`` with the compose file.

4.3 Give the host the key
~~~~~~~~~~~~~~~~~~~~~~~~~

Every host uses the same MinIO account, ``backup``, which can reach only the
``backups`` bucket. The account, its policy, and the bucket are created once
(``~/dev/docs/hosting-standard.md``, section 9.6.2). A new host needs only the
key in its credentials file (section 3): ``AWS_ACCESS_KEY_ID=backup`` and the
shared secret, plus its own ``RESTIC_PASSWORD``. The repository path still
includes the host name, so each host's repositories stay separate.

.. note::

   Because the account is shared, a compromised host could delete or overwrite
   every host's repositories. It cannot read them: each host's repositories are
   encrypted with that host's own password. Bucket versioning is off.

4.4 Dry run, then the first backup
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Starting Host & Directory:** the app's host, as root, any directory.

.. code-block:: bash

    hosted-backup run myapp --dry-run
    hosted-backup run myapp

.. code-block:: text

    app=myapp repository=s3:http://minio:9000/backups/<host>/myapp (would be initialized)
      sqlite data/app.db (108.2MiB); empty tables: search_cache
      file   .env
      file   docker-compose.yml
      file   backup.toml
    app=myapp dry-run ok
    app=myapp initialized repository s3:http://minio:9000/backups/<host>/myapp
    app=myapp snapshot=78a518e3 files=4 added=19.0MiB

The dry run reads the repository but creates nothing. It reports an error, not
"would be initialized", when the credentials or the policy are wrong.

5. Daily use
------------

.. list-table::
   :header-rows: 1
   :widths: 50 50

   * - Task
     - Command
   * - Back up one app now
     - ``hosted-backup run <app>``
   * - Back up every app
     - ``hosted-backup run``
   * - Preview without writing
     - ``hosted-backup run --dry-run``
   * - List snapshots
     - ``hosted-backup list <app>``
   * - Check repository structure
     - ``hosted-backup check <app>``
   * - Also read 5% of the data
     - ``hosted-backup check <app> --read-data-subset 5%``
   * - Apply retention and remove unused data
     - ``hosted-backup prune <app>``
   * - Next scheduled runs
     - ``systemctl list-timers 'hosted-backup*'``
   * - Last scheduled run
     - ``journalctl -u hosted-backup.service -n 20``

Several apps: one failure does not stop the others, and the exit status is 1.

6. Restore
----------

6.1 Test a backup
~~~~~~~~~~~~~~~~~

**Starting Host & Directory:** the app's host, as root, any directory.

.. code-block:: bash

    hosted-backup restore <app> --snapshot latest --target /tmp/restore-test
    R=/tmp/restore-test/var/tmp/hosted-backup/<app>/stage
    sqlite3 $R/data/app.db 'PRAGMA integrity_check;'

.. code-block:: text

    app=<app> restored snapshot latest to /tmp/restore-test
    restored files: /tmp/restore-test/var/tmp/hosted-backup/<app>/stage
    ok

The target must be empty and outside the app's directory. Restored files are
verified against the repository. The restore tree holds ``.env``: delete the
target when done.

6.2 Restore test checklist
~~~~~~~~~~~~~~~~~~~~~~~~~~

Run after the first backup of an app and then quarterly. Record the date in the
app's ``docs/HOSTING.md``.

.. list-table::
   :header-rows: 1
   :widths: 5 45 50

   * - #
     - Check
     - Expected
   * - 1
     - ``PRAGMA integrity_check`` on the restored database
     - ``ok``
   * - 2
     - ``select count(*)`` on each class A table, restored and live
     - Equal
   * - 3
     - ``select count(*)`` on each class C table, restored
     - ``0``
   * - 4
     - ``cmp`` the restored ``.env`` and ``docker-compose.yml`` with the live files
     - Identical
   * - 5
     - Rebuild each class C table that the app claims to rebuild (for example run its recompute job against a copy)
     - Rows return. If not, the table is class A.
   * - 6
     - ``hosted-backup check <app> --read-data-subset 5%``
     - ``check ok``

Compare the schemas by content, not by line order: ``VACUUM`` can reorder
statements.

6.3 Recover onto a rebuilt host
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

You need: the repository password, the host's MinIO key (or ``REPO_BASE``), and
the manifest.

.. list-table::
   :header-rows: 1
   :widths: 5 95

   * - #
     - Step
   * - 1
     - Install Docker and the tool (section 2).
   * - 2
     - Create ``/etc/hosted-backup/backup.env`` (mode 600) with the same ``RESTIC_PASSWORD`` and the host's S3 key.
   * - 3
     - Put the manifest in ``/opt/<app>/backup.toml``. Take it from the app's repository, or pass ``--manifest <file>`` to every command. The restore command needs it to know the app.
   * - 4
     - Restore to a scratch directory: ``hosted-backup restore <app> --target /tmp/recovery``. If the new host has a **different name**, put ``--host <original name>`` before ``restore``: the repository and its snapshots are filed under the original name.
   * - 5
     - Create ``/opt/<app>`` with ``data/`` owned by the runtime account, then copy the restored files in: ``cp -a /tmp/recovery/var/tmp/hosted-backup/<app>/stage/. /opt/<app>/`` and ``chown -R <uid>:<gid> /opt/<app>/data``.
   * - 6
     - ``docker compose up -d`` in ``/opt/<app>``, then check ``docker compose ps`` shows ``healthy``.
   * - 7
     - Rebuild what the app rebuilds itself (caches refill on use; an app's recompute jobs rebuild derived tables). Delete ``/tmp/recovery``.

7. Back up the MinIO server's own configuration
-----------------------------------------------

MinIO keeps its configuration, IAM data, and bucket metadata in ``.minio.sys``
inside its data directory. The bucket data is not backed up. This backup goes
to a mounted NFS share, not to MinIO itself.

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Item
     - Value on the MinIO host
   * - Mount
     - ``/etc/fstab``: ``<nas-host>:<exported-path> /mnt/nas-backup nfs vers=4,soft,timeo=100,retrans=3,_netdev,nofail,x-systemd.mount-timeout=30s 0 0``
   * - Manifest
     - ``/mnt/usbhd/backup.toml``: ``app = "minio-config"``, files ``.minio.sys`` and ``backup.toml``
   * - Credentials
     - ``REPO_BASE=/mnt/nas-backup/backup/hosted-backup``, ``REQUIRE_MOUNT=/mnt/nas-backup``, ``EXTRA_MANIFESTS=/mnt/usbhd/backup.toml``, ``RESTIC_PASSWORD``
   * - Repository
     - ``/mnt/nas-backup/backup/hosted-backup/minio/minio-config``

The copy of a running ``.minio.sys`` is crash-consistent. It also contains
``tmp/.trash``, which is transient. The compose file and the root credentials
are outside the data directory and are not in this backup: keep them in a
password manager.

8. Rotate credentials
---------------------

8.1 The shared S3 key
~~~~~~~~~~~~~~~~~~~~~

**Starting Host & Directory:** the MinIO host, with an ``mc`` alias that has administrator rights.

.. code-block:: bash

    mc admin user remove <alias> backup
    mc admin user add <alias> backup <new-secret>
    mc admin policy attach <alias> backup --user backup

Every host stops working until its key is replaced, so change them together:
replace ``AWS_SECRET_ACCESS_KEY`` in ``/etc/hosted-backup/backup.env`` on each
host (or run the Ansible role with the new value), then run
``hosted-backup run --dry-run`` on each. Expected last line:
``app=<app> dry-run ok``.

8.2 The repository password
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Add the new password first, test it, and only then remove the old one.

**Starting Host & Directory:** the app's host, as root.

.. code-block:: bash

    set -a; . /etc/hosted-backup/backup.env; set +a
    export RESTIC_REPOSITORY="s3:$S3_ENDPOINT/$S3_BUCKET/<host>/<app>"
    restic key list
    restic key add                      # prompts for the new password
    RESTIC_PASSWORD=<new> restic snapshots

When the new password lists the snapshots, remove the old key with
``restic key remove <old key id>`` (the ID is in the ``key list`` output; the
current key is marked ``*``), put the new password in ``backup.env`` and in the
password manager, and repeat for each app on the host.

9. Troubleshooting
------------------

.. list-table::
   :header-rows: 1
   :widths: 34 33 33

   * - Message or symptom
     - Cause
     - Fix
   * - ``must not be readable by group or others``
     - The credentials file mode is looser than 600
     - ``chmod 600 /etc/hosted-backup/backup.env``
   * - ``credentials file not found``
     - No ``/etc/hosted-backup/backup.env``
     - Create it (section 3), or pass ``--creds``
   * - ``cannot open the repository: ... Access Denied``
     - The key or policy does not allow this bucket, or the key was removed
     - Check that ``AWS_ACCESS_KEY_ID`` is ``backup`` and the policy is attached (hosting standard, section 9.6.2)
   * - ``... signature we calculated does not match``
     - Wrong ``AWS_SECRET_ACCESS_KEY``
     - Correct the secret (section 8.1)
   * - ``... connection refused``
     - MinIO is down or the endpoint is wrong
     - Check ``S3_ENDPOINT`` and the MinIO container
   * - ``restic init failed ... BucketExists: Access Denied``
     - The policy lacks ``s3:ListBucket`` on the bucket
     - Use the policy in the hosting standard, section 9.6.2
   * - ``another hosted-backup run is in progress``
     - A run holds the lock
     - Wait. If none is running, check ``/run/hosted-backup.lock`` and the journal.
   * - ``... is not mounted; refusing to write``
     - ``REQUIRE_MOUNT`` names a path that is not a mount point
     - Mount it, then run again
   * - ``no manifest for app '<app>'``
     - No ``/opt/<app>/backup.toml``
     - Copy the manifest from the app's repository, or use ``--manifest``
   * - ``no backup.toml found under /opt``
     - No manifests, and no ``EXTRA_MANIFESTS``
     - Add a manifest or set ``EXTRA_MANIFESTS``
   * - ``manifest lists a path that does not exist``
     - A typo or a deleted file
     - Fix the manifest; ``--dry-run`` shows ``MISSING``
   * - ``warning: ... excluded table <name> does not exist``
     - A typo in ``exclude_tables``, or the table was removed
     - Fix the name; the backup itself is unaffected
   * - ``incomplete snapshot``
     - Some files were unreadable (restic exit 3)
     - Check permissions on the listed paths; the snapshot exists but is partial
   * - ``refusing to restore into the app directory``
     - ``--target`` is inside ``/opt/<app>``
     - Use a scratch directory such as ``/tmp/restore-test``
   * - ``restore target is not empty``
     - The directory has files
     - Use an empty directory
   * - ``restic not found``
     - restic is not installed
     - ``apt install restic`` or run the Ansible role

10. Reference
-------------

Global options come before the command.

.. list-table::
   :header-rows: 1
   :widths: 28 72

   * - Option
     - Meaning
   * - ``--creds PATH``
     - Credentials file (default ``/etc/hosted-backup/backup.env``)
   * - ``--apps-root DIR``
     - Directory holding ``<app>/backup.toml`` (default ``/opt``)
   * - ``--manifest PATH``
     - Use this manifest instead of discovering them
   * - ``--host NAME``
     - Host name in repository paths and snapshots (default: this host's short name)
   * - ``--restic PATH``
     - restic binary (default ``restic``, or ``HOSTED_BACKUP_RESTIC``)
   * - ``--stage-root DIR``
     - Staging directory (default ``/var/tmp/hosted-backup``)
   * - ``--lock-file PATH``
     - Single-run lock (default ``/run/hosted-backup.lock``)
   * - ``-v``, ``--version``
     - Print the version

Exit status: ``0`` success, ``1`` failure (message on stderr), ``2`` usage
error.

Tests: ``pytest test_hosted_backup.py`` (a fake ``restic`` records the calls;
no network). Related: ``~/dev/docs/adr/0016-container-hosting.md``,
``~/dev/docs/hosting-standard.md``, and the Ansible role
``~/dev/iac/ansible/roles/hosted_backup``.
