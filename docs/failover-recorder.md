# Failover recorder

A **standby** NVR records in the primary's place while the primary is down, and hands its footage back when the primary returns. Failover is for one primary and one standby; each shows where it stands (Settings, Failover, and a chip in the status area) and raises an incident on takeover and recovers it on handback.

## Setting it up

1. Give both NVRs the same **backup passphrase** (`NVR_BACKUP_PASSPHRASE`, at least 16 characters) and make sure the primary can write backups.
2. Make the standby a copy of the primary **once**: make a backup of the primary (`python -m app.backup create`), then on the standby, while it is stopped, restore it as a standby:

   ```sh
   python -m app.backup restore --standby /backups/<file>.nvrbackup
   ```

   `--standby` makes the restored NVR a standby that records nothing until it takes over and knows no peer yet. (Without it the copy would wake up as a primary and record beside the real one.) The standby needs the primary's credential key, which a restore brings; every later copy of the configuration checks that the key is still the same and is refused if it is not.
3. On the primary, Settings, Failover: **Primary**, the standby's address, and a **failover secret** of at least 16 characters. On the standby: **Standby**, the primary's address and the same secret. Tick the private-network box for an address on a private network. Each address must be reachable from the other NVR (the standby's, by the primary; the primary's, by the standby).
4. Choose the timing (below) and save on both. The standby copies the primary's configuration at once (Settings, Failover, **Copy the primary's configuration now**) and then on its schedule.

## How it works

- **Lease.** The primary renews a lease with the standby (a signed request every quarter of the lease, at least every 3 seconds). While the standby hears the renewals it holds still: its cameras are shown as *Standby (the primary records)* and nothing is recorded.
- **Takeover.** After the **silence** (at least twice the lease; default 120 s, lease 60 s) without a renewal, the standby takes over: it counts one more takeover (the *epoch*), raises an incident, and records every camera of the configuration it copied, as the primary would, within a few seconds. It will not take over before it has a configuration to record from.
- **Return.** When the primary comes back, its first renewal reaches a standby that is recording. The standby **stops at once and says "not yet"** until its recorders have stopped (at most 30 seconds), then grants the lease and offers the footage it recorded. A primary that has just started does not record until it has been granted the lease: it waits up to 20 seconds for an answer, and if the standby cannot be reached at all it assumes the standby is down and records. So when the primary returns during a takeover, the two do not record at once.
- **Handback.** The primary downloads each segment the standby recorded (checked against the digest the standby gives), adds it to its own index marked **Recorded by the standby NVR**, and tells the standby, which recovers its incident; the primary recovers its own. The footage plays and exports like any other, is subject to the primary's retention, and the primary's footage chain hashes and signs it like footage it recorded itself, so it verifies. The standby keeps its own copy until its retention removes it.
- **Gaps.** Time after the primary stopped and before the standby took over is a gap as it always was (for example *Recorder restarting*); Playback says that the standby waits for the primary to be silent before it takes over. Footage the standby recorded fills the rest.

## What is copied to the standby

Cameras (with their stream addresses and logins, encrypted under the key both share), camera groups, recording schedules, retention and privacy masks, and the site's time zone and date format. **Not** copied: footage, the footage index and its signing key, users, rules, alarms, notifications, AI settings, integrations, access grants and storage settings. The standby keeps its own users, so people sign in to it with accounts made on it; a camera removed at the primary is switched off and archived on the standby if it holds footage of it, otherwise removed.

## Limits and honest behaviour

- **A partition is not an outage.** A primary that is running but cannot reach the standby keeps recording (a standby that is merely down must not stop it). If the standby cannot hear the primary it takes over after the silence, so **both record the same cameras while the network is split**. The first renewal that gets through ends the takeover and the primary imports the standby's footage; the overlap is duplicate footage, not lost footage. Use a lease and silence long enough that a brief network fault does not cause a takeover, and make sure every camera allows two simultaneous RTSP sessions if a partition must be survivable.
- **Detection takes the silence.** Nothing is recorded between the primary stopping and the standby taking over, except footage the cameras keep themselves (see edge backfill, which the standby does not run for the primary).
- **Clocks.** Keep both NVRs' clocks right (NTP): the silence is measured on the standby's clock and footage times come from both.
- **One standby, one primary.** More than one standby or primary per secret is not supported. The two NVRs must run the same version (the standby refuses configuration from a newer primary).
- **The standby needs the cameras' network.** A takeover is only as good as the standby's route to the cameras and its storage; check Settings, Failover on the standby after every change of cameras, and exercise a takeover (below).
- **AI analysis, rules and notifications do not run on the standby** during a takeover: it records.
- The configuration is copied through the primary's encrypted backups: the primary must have a backup passphrase set, and `GET /api/inbound/failover/backup` makes a fresh backup if the newest is over five minutes old.

## Exercise it

Before relying on it, with real cameras: stop the primary host (power off, not just the application), time how long the standby takes to start recording (it should be the silence plus a few seconds), check every camera records on the standby, start the primary again, and check that the standby stops, the footage comes back and plays, and both incidents recover. Record the result in `docs/acceptance/roadmap.md`.

## API

Administrators: `GET /api/admin/failover` (settings without the secret, status, takeovers), `PUT /api/admin/failover`, `POST /api/admin/failover/sync` (standby: copy the configuration now). Everyone signed in: `GET /api/failover/status`. Between the two NVRs (signed with the failover secret: `X-Failover-Timestamp` and `X-Failover-Signature`, HMAC-SHA256 over the time, method, path and body digest, valid for a minute): `POST /api/inbound/failover/lease`, `GET /api/inbound/failover/backup`, `GET /api/inbound/failover/footage?epoch=N`, `GET /api/inbound/failover/footage/<id>?epoch=N`, `POST /api/inbound/failover/handback-complete`. Changes to the settings are in the audit log (`failover_settings_changed`); incidents have the fault type `failover` and notify as `system.failure` and `system.recovered`.
