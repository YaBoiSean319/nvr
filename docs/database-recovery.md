# Recovering from a damaged database

The NVR keeps its settings, users, alarms, recording index and history in one SQLite file (`nvr.sqlite3`, in the `nvr-data` volume). Recorded video is in separate files and is not part of it.

## How you find out

- Every page fails with "The NVR's database is damaged" (or, on older versions, "The NVR could not complete the request"), and the log shows `database disk image is malformed` or `file is not a database`.
- The log has the line `THE DATABASE FILE IS DAMAGED` at startup, and an incident **Self-test failed: The database file is damaged** is raised by the nightly self-test (03:41 site time), which reads every page of the file. Running the self-test by hand only probes the file; the full check runs at night because it takes minutes on a large database.

The container's health check (`/healthz`) also fails with "Database damaged" when it cannot open the file and read the start of every table, so `docker compose ps` shows the container as unhealthy. Docker only marks it; it does not restart it.

A busy database ("database is locked") is not damage and is reported differently.

## Why it happens

SQLite files are damaged by what is under them, not by the NVR: a disk or virtual-disk fault, a host crash or power loss, a snapshot, clone or backup of a running VM taken without freezing the guest, a full host volume, or network storage. On Hyper-V, a checkpoint that is left in place makes the VM write to a growing differencing disk, which is slower and riskier than a normal disk. After a recovery, find the cause, or it will happen again.

## What to do

Run these from the folder with the compose file. If Docker says permission denied, put `sudo` in front.

1. **Stop the NVR so nothing writes to the file.**
   ```
   docker compose stop nvr
   ```
   Do not delete anything and do not restart it repeatedly.

2. **See what is in the data volume.** The volume is named after the compose project (`docker volume ls`; for a project called `nvr` it is `nvr_nvr-data`).
   ```
   docker run --rm -v nvr_nvr-data:/data --entrypoint ls ghcr.io/gvsccrc-it/nvr:edge -la /data
   ```
   You should see `nvr.sqlite3` and files named `nvr.sqlite3.pre-migration-vA-to-vB-<time>`. The NVR saves one before every upgrade that changes the database. Configuration backups, if they are turned on, are in the `nvr-backups` volume and anywhere you copy them.

3. **Copy the whole folder somewhere safe before anything else is tried.** Use a folder with enough free space (each copy of a large database is as big as the database).
   ```
   mkdir -p ~/nvr-data-rescue
   docker run --rm -v nvr_nvr-data:/data -v ~/nvr-data-rescue:/rescue --entrypoint cp ghcr.io/gvsccrc-it/nvr:edge -a /data/. /rescue/
   ```

4. **Check each copy, read only.** This changes nothing and takes a few minutes per gigabyte. Each line ends in `['ok']` for a healthy file.
   ```
   docker run --rm -i -v nvr_nvr-data:/data:ro --entrypoint python ghcr.io/gvsccrc-it/nvr:edge - <<'EOF'
   import sqlite3, glob, time
   for f in sorted(glob.glob('/data/nvr.sqlite3*')):
       t = time.time()
       try:
           db = sqlite3.connect(f'file:{f}?mode=ro&immutable=1', uri=True)
           rows = db.execute('PRAGMA quick_check(5)').fetchall()
           print(f, [r[0] for r in rows], round(time.time() - t), 's', flush=True)
       except Exception as e:
           print(f, 'ERROR', e, flush=True)
   EOF
   ```

5. **Restore the newest healthy copy.** The damaged file is renamed, not deleted, so it can still be examined. Replace the copy's name with the one from step 2.
   ```
   docker run --rm -v nvr_nvr-data:/data --entrypoint sh ghcr.io/gvsccrc-it/nvr:edge -c 'cd /data && mv nvr.sqlite3 nvr.sqlite3.damaged && cp nvr.sqlite3.pre-migration-v56-to-v59-20261110T142411614532Z nvr.sqlite3 && ls -la'
   ```
   Make sure there is room: the NVR takes another full copy when it starts and migrates the restored file.

6. **Start it and watch the log until the workers have started.**
   ```
   docker compose up -d nvr
   docker compose logs -f nvr
   ```
   Sign in and check the cameras, groups, rules and users. Everything written to the database after the copy was made is gone (settings changes, alarm actions, sessions); recorded video is not affected.

7. **Find the cause** (host disk errors, snapshots or checkpoints, host crashes, storage), turn on configuration backups, and copy them to another machine. A backup on the same disk does not protect against a failed disk. A backup needs free space for one copy of the database on the data disk while it runs; if there is not enough, it is refused with a message saying how much is needed where. Restoring one needs the same room, and is refused before anything changes when there is not.

If every copy is damaged, keep the files and ask for help before deleting anything: part of a damaged file can often be recovered with SQLite's `.recover` command.

## Keep the credential key with the data

Camera addresses and passwords, webhook addresses and signing secrets are stored encrypted with a key kept outside the database, in the secrets volume (`NVR_CREDENTIAL_KEY_PATH`, by default `/secrets/credentials.key`). A restored or moved data folder is only readable together with that key: copy and restore the two as a pair.

On a first run, when nothing has been saved yet, the NVR makes this key itself. Once anything encrypted with it is stored (a camera, a webhook, a sign-in key), a missing key file is never replaced: the NVR logs one line beginning `THE CREDENTIAL KEY FILE ... IS MISSING`, raises an incident, and answers any request that needs the key with "The NVR's credential key file is missing" in place of doing it. Recorded footage can still be watched. If you see this: stop the NVR, put the original key file back, and start again; everything reads as before and the incident clears. If a different key file is put in place by mistake, the webhook and heartbeat pages show the affected addresses as "cannot be read" until the right one is back.
