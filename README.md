# Workplace NVR

A self-hosted network video recorder for workplace cameras. This branch includes secure first-run administration, local and Microsoft Entra sign-in, camera discovery and configuration, ONVIF motion-event recording with bounded pre/post-roll, optional server-side motion fallback, continuous and scheduled recording policies, retention and preservation controls, archived-camera history, authenticated live viewing and synchronized playback, evidence ZIP exports, administrator-controlled opt-in camera audio, and local person/vehicle analysis with validated OpenVINO provider/model selection. See the [rebuild acceptance report](docs/acceptance/rebuild.md) for what has been verified and the deployment checks that still require real cameras, a real tenant, reference hardware or workload measurements, and [console acceptance and operating limits](docs/console-acceptance.md) for the enforced resource bounds.

## Requirements

- Docker Engine with the Compose plugin
- A host with a persistent filesystem for the Compose volumes
- Docker Engine with Linux host-network support and the Docker Compose plugin

## Start the stack

The default stack uses the validated Linux host-network arrangement:

```sh
docker compose pull
docker compose up -d
docker compose logs -f nvr
```

The published `ghcr.io/yaboisean319/nvr:edge` image is built from the latest `main` branch. Compose checks for a newer image on each `up`. The tested deployment uses Linux host networking so camera discovery, ONVIF events, and camera stream access share the host's camera-network routes. The web interface listens on `http://<server>:8280` by default; set `NVR_PORT` in `.env` to choose another host port. Host networking exposes that listener directly on the host, so protect it with the host firewall or approved gateway. Create the first local administrator on a trusted setup network. No default account or password is shipped. The setup endpoint is permanently closed as soon as the first administrator is committed to the database. Configure TLS through the approved workplace gateway and set `NVR_COOKIE_SECURE=true` when the application is served over HTTPS.

The `nvr-data` volume stores the database and account/session state. `nvr-secrets` stores the generated credential-encryption key with owner-only file permissions. `nvr-recordings` stores the MP4 segments. All three survive container replacement and host reboot as long as Docker's volume data is retained. Back up and protect the data and secrets volumes together; a database-only export does not contain the key needed to decrypt camera URLs.

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `NVR_PORT` | `8280` | Host listening port for the web application |
| `NVR_SESSION_HOURS` | `12` | Session lifetime, clamped to 1–168 hours |
| `NVR_COOKIE_SECURE` | `false` | Set `true` behind HTTPS so session cookies are HTTPS-only |
| `NVR_PUBLIC_URL` | unset | Public HTTPS origin used to construct the fixed Entra callback URL, and the origin and relying party of passkeys |
| `NVR_WEBAUTHN_RP_ID` | unset | Optional parent domain for passkeys, to share them between hosts |
| `NVR_TRUST_FORWARDED_FOR` | false | Count sign-in attempts by the last `X-Forwarded-For` address; only behind a proxy you control |
| `NVR_LOG_FORWARD_URL` | unset | Forward logs off-host: `syslog+tcp://host:514`, `syslog+tls://host:6514`, `otlp+http://host:4318` or `otlp+https://…`; see [metrics and log forwarding](docs/metrics-and-log-forwarding.md) |
| `NVR_LOG_FORWARD_CA` / `NVR_LOG_FORWARD_LEVEL` / `NVR_LOG_FORWARD_QUEUE` | system CAs / `INFO` / `2000` | CA bundle for TLS, lowest level forwarded, records allowed to wait |
| `NVR_OIDC_TENANT_ID` | unset | Microsoft Entra workforce tenant GUID |
| `NVR_OIDC_CLIENT_ID` | unset | Entra app registration client ID |
| `NVR_OIDC_CLIENT_SECRET` | unset | Entra app registration client secret; supply through a secret manager |
| `NVR_OIDC_SESSION_SECRET` | unset | Long random key for the short-lived signed OIDC state cookie |
| `NVR_OIDC_ADMIN_ROLES` | unset | Comma-separated Entra app-role values mapped to NVR administrators |
| `NVR_OIDC_OPERATOR_ROLES` | unset | Comma-separated Entra app-role values mapped to NVR operators |
| `NVR_DISCOVERY_INTERFACE` | unset | Optional IPv4 address of the NVR host interface attached to the camera VLAN |
| `NVR_BACKUP_PASSPHRASE` | unset | Enables encrypted configuration backups (at least 16 characters) |
| `NVR_BACKUP_PASSPHRASE_FILE` | unset | Path of a file holding the backup passphrase, used when `NVR_BACKUP_PASSPHRASE` is empty |
| `NVR_BACKUP_KEEP` | `14` | Number of configuration backups kept |

Do not expose the initial HTTP setup screen to untrusted networks. Place it behind the workplace VPN or approved gateway, then use HTTPS and enable secure cookies.

## Using the interface

Open `http://<server>:8280/` in a current Chromium, Firefox or Safari browser. The interface has four workspaces:

- **Live**: 1, 4, 9 or 16 tiles; cameras, groups, and personal or shared saved views; focus and restore. Video arrives as fragmented MP4 over an authenticated WebSocket and plays through Media Source Extensions, falling back to MJPEG when the browser cannot decode the camera's codec. Tiles show whether video is live, stale, reconnecting or unavailable; live video does not prove that footage is being recorded, which the status area reports separately.
- **Playback**: synchronized playback of up to 16 cameras on one timeline in the site's time zone, with motion, person/vehicle and unanalysed-coverage tracks, event and frame stepping, investigation filters, marks, and evidence exports. Exports are listed in the status drawer, where a failed export can be retried.
- **Cameras**: the inventory, a per-camera editor (General & Connection, Recording, Motion, AI, PTZ, Privacy, Neighbours, Schedule & Retention, Health & History) with an on-request preview, **Add Cameras** (Discover, Manual Add, CSV Import), reviewed bulk changes, **Groups & Policies**, and **Rules** (people or vehicles entering a zone, crossing a line, staying too long or moving the wrong way, with a 24-hour test mode and actions: signed webhooks, MQTT, Slack, Teams and ONVIF relay pulses; see [rules](docs/rules.md)). Operators see read-only details.
- **Settings** (administrators): General, Appearance & branding, Users & access (sessions and API tokens), Recording & storage (volumes and configuration backups), Notifications (webhooks and heartbeat), Integrations, and Diagnostics.

The status area's **Alarms** chip opens the alarm inbox from every workspace: acknowledge, assign, comment on and resolve the alarms rules raise, with a snapshot, a clip and Open in Playback (see [alarms](docs/alarms.md)). Each person can turn on push notifications for alarms on their own devices, with per-rule choices and quiet hours; set `NVR_VAPID_SUBJECT` to a contact address push services can use.

Rules can pop their alarms into Live: each person who opts in sees the alarm's camera and its neighbouring cameras in a temporary layout, with Return to my view. Video wall mode at `/wall` rotates shared views full screen for monitors and pops alarms in, signed in with an API token limited to `read` and `media` (see [alarms in Live and video walls](docs/live-alarms-and-video-wall.md)).

ONVIF PTZ cameras can be moved from Live and the camera editor (buttons, keyboard and touch, click-to-centre), with presets, scheduled patrol tours and a control lease so one person drives a camera at a time (see [PTZ](docs/ptz.md)).

Privacy masks black out areas of a camera's picture everywhere: written to cameras that support ONVIF masks, and otherwise drawn by the NVR into every live view, recording, snapshot and export. Administrators can see unmasked footage only for a stated, audited reason (see [privacy masks](docs/privacy-masks.md)).

Access grants limit which cameras an operator sees, by camera group or for everyone holding an Entra role, optionally until an expiry; hidden cameras, their footage, alarms and exports behave as if they did not exist (see [camera access grants](docs/access-grants.md)).

The viewing audit records who watched which camera live or in playback, from when to when, and every export, download and snapshot; administrators report it per person or per camera for a date range, download it as CSV, and set how long it is kept apart from footage (see [viewing audit](docs/viewing-audit.md)).

`/metrics` serves Prometheus metrics to an API token with the `metrics` scope, and the logs can be forwarded as syslog (TCP or TLS) or OTLP, scrubbed of camera addresses and credentials and never blocking the NVR (see [metrics and log forwarding](docs/metrics-and-log-forwarding.md)).

Camera tamper detection notices a covered, moved or defocused camera by comparing a small frame from it with a learned view, raises an incident that recovers when the view returns, and can trigger rules; it is off until switched on per camera (see [camera tamper detection](docs/tamper-detection.md)).

Local accounts can add passkeys or an authenticator app with one-time recovery codes; administrators can require them, reset them, and sign-in attempts are rate limited per account and per address (see [passkeys and authenticator apps](docs/second-factors.md)).

Every recording segment is hashed into a signed per-camera chain, so altered, missing or reordered footage can be found from a camera's Health & History tab, and evidence packages carry the chain and public key to verify offline (see [signed footage](docs/signed-footage.md)).

The interface installs as an app on desktops and phones, named and iconed from the site's branding; its service worker keeps only the application shell, never API responses or footage (see [installable app](docs/installable-app.md)).

Every editor saves explicitly. Leaving with unsaved changes asks whether to save, discard or stay, and a refresh never overwrites what you are editing. The interface used to be previewed at `/next/`; those addresses now redirect to the same page at `/`. Keyboard use, screen-reader names, reduced motion and widths from 320 px are covered by the browser suite.

## Health and setup

- `GET /healthz` returns `200` when the app can query its database; Compose uses it as its health check.
- The container runs a supervisor that keeps recording in a separate process from the web interface. A web crash or restart does not interrupt footage writes. Either process is restarted automatically with backoff, and an incident stays active until it is running again. `GET /api/system/status` reports each worker's liveness and restart count. See [architecture](docs/architecture.md).
- `GET /api/setup/status` reports whether initial setup is still required.
- `POST /api/setup/admin` creates the one-time local administrator. Passwords require at least 14 characters, including a letter and a number.
- `GET /api/system/status` requires an authenticated administrator session.
- `POST /api/cameras/test` checks an RTSP/RTSPS video stream with FFprobe.
- Administrators can enable opt-in support diagnostics for 1–24 hours and download a bounded JSON bundle. Capture is off by default. The bundle contains only scan/probe outcomes, aggregate counts, and timings; it excludes camera inventory, names, addresses, stream/service URLs, credentials, sessions, and footage. It is capped at 1,000 events and 7 days.
- `POST /api/cameras` saves the stream URL encrypted and reports tested video capability; credentials and query parameters are omitted from API responses. A camera can be supplied as an `stream_url` or as `camera_ip` with paired optional `camera_username`/`camera_password`, plus an optional `camera_brand`, `device_service_url`, and `onvif_profile_token`.
- `POST /api/cameras/onvif-profiles` returns the ONVIF media profile names/tokens for a camera without exposing stream URLs or credentials. `POST /api/discovery/scan` accepts optional private IPv4 `ip_range` CIDR and reports `scan_mode` and `targets_scanned`.
- `GET /api/cameras/{id}/segments` returns indexed coverage; authenticated media routes serve stored MP4 segments.
- `GET /api/cameras` reports each camera's separate `last_video_at` and `last_footage_write_at` timestamps plus a `recording_health` state. `GET /api/cameras/{id}/coverage` returns the current classification and recent persisted intervals. Coverage is classified as covered, expected idle, failure, or uncertain. Each gap also carries a `reason_code` (camera offline, stream error, disk unavailable, outside schedule, recording disabled, worker restart, no motion or unknown); a weekly coverage report and a nightly self-test are in **Settings › Coverage & self-test** (see [coverage and self-test](docs/coverage-and-self-test.md)). Motion-only idle with healthy video/events is expected idle; stale continuous writes while video remains fresh are surfaced as `write_stalled`.
- Administrators configure weekly group policies and per-camera overrides from the group and camera cards. Schedule JSON uses ISO weekdays (`0=Monday`), local `HH:MM` times, and `motion`, `continuous`, or `disabled` modes. Intervals are start-inclusive/end-exclusive, overnight intervals belong to their start day, and overlaps are rejected. Week schedules use the selected IANA timezone; repeated fall-back wall times apply to both occurrences, while skipped spring-forward wall times do not occur. A schedule supervisor applies changes at most five seconds after a boundary. Cameras without an explicit override inherit their group schedule; removing a camera override restores inheritance.
- Entra OIDC is enabled only when the five `NVR_PUBLIC_URL` and `NVR_OIDC_*` connection values are set and at least one role mapping is configured. The configured public URL must be HTTPS except for localhost development. Register its exact callback URL, `<NVR_PUBLIC_URL>/api/auth/oidc/callback`, as a **Web** redirect URI in the Entra app registration.
- Assign Entra application roles to staff and map their exact role values with `NVR_OIDC_ADMIN_ROLES` and `NVR_OIDC_OPERATOR_ROLES`. Accounts without an explicit mapping are denied. Role mapping is read from the verified ID token; group overage and unassigned accounts do not grant access.
- The OIDC authorization-code flow uses Authlib for discovery, state/nonce validation, ID-token signature and claim validation, and PKCE. Set `NVR_OIDC_SESSION_SECRET` to a long random value and keep it stable across restarts. A provider outage returns a clear sign-in error while the emergency local administrator can still sign in at `/api/auth/login`.
- Operators can list cameras and view indexed footage. Camera configuration, system status, and session administration require an administrator. Administrators can review and terminate active sessions at `/api/admin/sessions`; session lifetimes are capped at 168 hours by `NVR_SESSION_HOURS`.
- Administrators can archive a camera to stop ingest and remove it from live viewing while retaining its identity and indexed footage for historical search and playback. Permanent footage deletion is a separate administrator action that requires the camera to be archived, exact camera-name confirmation, and no active preservation hold, export, or playback lease. The archived camera identity remains after footage deletion. Operators receive `403` for archive or footage-deletion requests. See [console acceptance and operating limits](docs/console-acceptance.md).
- ONVIF WS-Discovery is an explicit administrator action at **Cameras › Add Cameras › Scan camera network**. The default stack uses host networking on Linux, the arrangement validated during deployment testing. Set `NVR_DISCOVERY_INTERFACE` to the camera-network interface IPv4 address when multicast needs a specific source interface. The app binds directly to `NVR_PORT` (8280 by default); no Docker port mapping applies in host mode. Protect the listener with the host firewall or approved gateway.
- Routed camera discovery is available from **Scan range** on the same page. Enter a private IPv4 CIDR for the camera network; the NVR needs an allowed route and firewall access to the camera network, but does not need an interface on that VLAN. The scanner probes common ONVIF ports and is limited to 256 addresses per scan. This is unicast ONVIF probing, so it will not find devices that have ONVIF disabled or use an unsupported port.
- To collect evidence for a discovery issue, start capturing in **Settings › Diagnostics**, run a scan, and download the diagnostics bundle. The bundle deliberately omits IP addresses and device inventory. Do not send camera passwords, RTSP URLs, raw packet captures, or footage.
- Discovery returns review candidates only and never starts recording. Add a camera with separate IP, ONVIF port, brand/type, username, and password fields. The brand selector prioritizes common ONVIF ports for ACTi, Axis, Bosch, Dahua, Hanwha, Hikvision, Reolink, Samsung, and Uniview, then checks other common ports. For cameras with a non-standard ONVIF device-service path, enter its custom URL. **Load stream profiles** lists every ONVIF media profile advertised by the camera (including main, sub, and other profiles); select the desired profile and test the stream before adding. Cameras that do not expose ONVIF media profiles cannot use this flow. Device UUIDs deduplicate repeat WS-Discovery results and link scans to manually added cameras when the UUID is known. Audio capability detection does not mean audio is recorded or playable.
- Administrators can create camera groups, select cameras for bulk group/recording-mode changes, and rotate a camera's saved stream URL. A credential rotation is probed before the encrypted URL is replaced; failures keep the old URL, camera ID, and footage association. Bulk CSV onboarding is available from **Add Cameras › CSV Import**: preview the file first; importing then tests each accepted stream and reports every row. Required columns are `name,stream_url`; optional columns are `recording_mode,group_name,onvif_uuid`. Imports are limited to 500 rows per request, return row-level status without echoing stream URLs, and create a camera only after its video probe succeeds. The API exposes `POST /api/cameras/import/preview`, `POST /api/cameras/import/apply`, `POST /api/cameras/bulk-update`, `POST /api/cameras/{id}/credentials`, and `GET /api/camera-groups`.
- Automation uses scoped API tokens. An administrator creates one with `POST /api/admin/api-tokens` (`name`, `scopes`, optional `expires_at`), lists them with `GET /api/admin/api-tokens`, and revokes one with `DELETE /api/admin/api-tokens/{id}`. The secret is returned once at creation, and only a keyed digest is stored. Send it as `Authorization: Bearer <token>`; tokens in query strings or cookies are ignored. Administrators can also create and revoke tokens in **Settings › Users & access**. Scopes are `read` (inventory, status, coverage, events, incidents), `media` (live, segment media, snapshots, frame navigation), `export` (create, view, cancel, download and verify exports; preserve segments) and `admin` (every administrator operation; implies the others). Other changes, such as personal live views, need a browser session or an `admin` token. A token acts for the administrator who created it and stops working if it is revoked, expires, or its creator is no longer an administrator. Invalid tokens return `401`; a missing scope returns `403` naming the scope. Tokens cannot manage tokens. Creation, revocation and use (at most once a minute per token) are recorded in the audit log. `/openapi.json` lists the scope each operation requires as `x-required-scope`.
- Weekly recording policy APIs are `GET/PUT/DELETE /api/camera-groups/{id}/schedule` and `GET/PUT/DELETE /api/cameras/{id}/schedule`. An explicit camera schedule overrides its group's policy; removing the override restores inheritance. Operators can see the effective mode and policy source but cannot change schedules.

New cameras default to **Motion** with recording disabled. An administrator must explicitly enable recording. Motion mode uses the camera's ONVIF PullPoint events and keeps a bounded timestamped video buffer; the default is 10 seconds of pre-roll and 30 seconds of post-roll. Overlapping events extend a shared clip and keep their source timestamps and topics. Clips with incomplete startup pre-roll are marked in the event list.

Configure the camera's ONVIF device-service URL and optional credentials on its **Motion** tab. ONVIF credentials are encrypted at rest and are not returned by the API. When enabled, **server motion fallback** starts only after the camera event source becomes unavailable or unsupported. It compares low-resolution grayscale video inside the configured normalized rectangular zones and records the same motion clips. Sensitivity ranges from 1 to 100; an empty zone list means the full frame. It does not use person/vehicle inference and does not switch to continuous recording. The camera's **Health & History** tab reports ONVIF and server detector health separately, including detector failure and recovery.

The software tests include a local ONVIF PullPoint fixture and generated playable footage. Live ONVIF discovery and event delivery on the camera VLAN still need validation after deployment. Continuous mode remains available as an explicit separate policy. FFmpeg remuxes source video without re-encoding into MP4 footage; segment boundaries follow source keyframes. Browser playback depends on the camera codec. H.264/AVC in MP4 is a pragmatic compatibility target; HEVC/H.265 and vendor codecs may not play in every browser. Camera audio remains disabled until an administrator probes support and explicitly enables it. When enabled, recordings use AAC in MP4 and live listening uses MP3; see [camera-audio.md](docs/camera-audio.md) for behavior and browser limitations. FFmpeg and FFprobe are included in the image, and the recorder retries interrupted RTSP/TCP connections.

The application encrypts camera URLs before database storage, keeps the key on a separate Docker volume, suppresses FFmpeg diagnostics that could contain credentials, and passes the URL to the media process over stdin rather than exposing it in process arguments. Protect both volumes and restrict access to the Docker host.

### Upgrades and schema migrations

The database schema is versioned. On startup the application applies any pending migrations in order and records each one in the `schema_migrations` table. An installation created before versioning is recognised, converged onto the baseline schema and stamped as version 1 without changing its records. Before migrating a database that holds data, the application writes an owner-only copy beside it in the data volume (`nvr.sqlite3.pre-migration-v<from>-to-v<to>-<timestamp>`) and keeps the three most recent copies. If a migration fails, startup stops with the failing version and the copy's location; the failed migration's changes are rolled back and it is not recorded.

To upgrade, pull the new image and recreate the container (`docker compose pull && docker compose up -d`). With backups enabled, a configuration backup is taken before any migration runs, in addition to the pre-migration copy. Watch `docker compose logs -f nvr` until the health check passes, then confirm **Recording** in the status area reports every expected camera as writing. A browser that still shows the old interface loads the new one on its next reload, and bookmarks to `/next/…` redirect.

Migrations are forward-only. An image older than the database's recorded schema version refuses to start rather than run against a schema it does not understand, so downgrading requires restoring the pre-upgrade copy (or configuration backup) together with the older image. Recordings are not affected by schema migrations.

### Configuration backups and restore

Set `NVR_BACKUP_PASSPHRASE` (at least 16 characters), or point `NVR_BACKUP_PASSPHRASE_FILE` at a file containing it (for example a Docker secret), to enable encrypted configuration backups. Keep a copy of the passphrase off the NVR host. Without it, backups are disabled, the Settings status says so, and nothing is written. A backup contains the database (users, cameras and encrypted camera credentials, groups and policies, saved views, branding, webhooks, and the footage index) and the credential key. It is encrypted with AES-256-GCM using a key derived from the passphrase with scrypt. **Footage is never included**, so protect recordings separately; losing the only recordings disk loses that footage permanently.

Backups run daily at 02:17 in the site time zone, before every schema migration, and on demand (**Back up now** in **Settings › Recording & storage**, or `POST /api/backups`; administrators only). They are written to `NVR_BACKUP_PATH` (`/backups` in Compose), and the newest `NVR_BACKUP_KEEP` (default 14) are kept. Mount `/backups` on a different disk or host than the NVR data. `GET /api/backups` reports backup age, the last success and failure, and the stored files. A failed backup, or no success for 26 hours, raises an incident (`system.failure` webhook) that recovers on the next success.

To restore, stop the NVR, then run the restore command in a one-off container with the same volumes and passphrase:

```sh
docker compose stop nvr
docker compose run --rm nvr python -m app.backup restore /backups/<file>.nvrbackup
docker compose up -d
```

Restore verifies the archive's integrity and the database before replacing anything, and keeps the replaced files beside the originals as `*.before-restore-<timestamp>`. It refuses to run while NVR workers are reporting liveness unless `--force` is given. On the next start, indexed footage whose file no longer exists is marked unavailable and shown as a failure gap rather than as recorded footage. Segments on a storage volume that is not mounted are left alone and checked again on the following start.

Use `docker compose restart` to restart the service. The account remains in the `nvr-data` volume and the health check returns healthy after startup. To intentionally reset the test installation, run `docker compose down -v`; this permanently deletes the local database and any volume data.

## Development

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
uvicorn app.main:app --reload --port 8280
```

The browser interface lives in `web/` (React, TypeScript, Vite) and is served at `/`. The container image builds it in a separate stage; for local work:

```sh
npm ci --prefix web
npm run build --prefix web     # type-checks, then writes app/web_dist, served at /
npm run dev --prefix web       # or a hot-reloading dev server that proxies /api to port 8280
```

Without a build, the API still works and the page answers `503` saying the interface has not been built. Real-browser journeys run with `npm run test:browser` after `npm ci` and the web build. Set `PLAYWRIGHT_CHROMIUM_EXECUTABLE` to use an already-installed Chromium instead of Playwright's download. The browser server receives only file paths for its secrets (`NVR_BROWSER_FIXTURE_PASSWORD_PATH`, `NVR_BACKUP_PASSPHRASE_FILE`); `tests/browser/validate_acceptance_report_privacy.cjs` enforces that list. Run one Playwright invocation at a time: they share the fixture directory.

## Current boundary

This implementation is not yet qualified as a production replacement. Complete every **Post-merge testing required** item in the [rebuild acceptance report](docs/acceptance/rebuild.md) (real cameras and codecs, the Entra tenant, latency on reference hardware, export load, recording health under viewing load, the production volume upgrade) and then the 30-day parallel pilot. No capacity claim beyond the implemented sixteen live video and sixteen live audio slots is made, and no 200-camera certification is claimed. Configure Entra for the target tenant and verify its callback and role assignments before enabling staff sign-in. Configuration backups are encrypted and in-app (see above) but never include footage; protect the recordings volume with the host's approved backup process.
