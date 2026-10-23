# Workplace NVR

A self-hosted network video recorder for workplace cameras. This branch includes secure first-run administration, local and Microsoft Entra sign-in, camera discovery and configuration, ONVIF motion-event recording with bounded pre/post-roll, optional server-side motion fallback, continuous and scheduled recording policies, retention and preservation controls, archived-camera history, authenticated live viewing and synchronized playback, evidence ZIP exports, administrator-controlled opt-in camera audio, and local person/vehicle analysis with validated OpenVINO provider/model selection. See [console acceptance and operating limits](docs/console-acceptance.md) for the supported bounds and deployment checks that still require real browsers, cameras, or workload measurements.

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
| `NVR_PUBLIC_URL` | unset | Public HTTPS origin used to construct the fixed Entra callback URL |
| `NVR_OIDC_TENANT_ID` | unset | Microsoft Entra workforce tenant GUID |
| `NVR_OIDC_CLIENT_ID` | unset | Entra app registration client ID |
| `NVR_OIDC_CLIENT_SECRET` | unset | Entra app registration client secret; supply through a secret manager |
| `NVR_OIDC_SESSION_SECRET` | unset | Long random key for the short-lived signed OIDC state cookie |
| `NVR_OIDC_ADMIN_ROLES` | unset | Comma-separated Entra app-role values mapped to NVR administrators |
| `NVR_OIDC_OPERATOR_ROLES` | unset | Comma-separated Entra app-role values mapped to NVR operators |
| `NVR_DISCOVERY_INTERFACE` | unset | Optional IPv4 address of the NVR host interface attached to the camera VLAN |

Do not expose the initial HTTP setup screen to untrusted networks. Place it behind the workplace VPN or approved gateway, then use HTTPS and enable secure cookies.

## Health and setup

- `GET /healthz` returns `200` when the app can query its database; Compose uses it as its health check.
- `GET /api/setup/status` reports whether initial setup is still required.
- `POST /api/setup/admin` creates the one-time local administrator. Passwords require at least 14 characters, including a letter and a number.
- `GET /api/system/status` requires an authenticated administrator session.
- `POST /api/cameras/test` checks an RTSP/RTSPS video stream with FFprobe.
- Administrators can enable opt-in support diagnostics for 1–24 hours and download a bounded JSON bundle. Capture is off by default. The bundle contains only scan/probe outcomes, aggregate counts, and timings; it excludes camera inventory, names, addresses, stream/service URLs, credentials, sessions, and footage. It is capped at 1,000 events and 7 days.
- `POST /api/cameras` saves the stream URL encrypted and reports tested video capability; credentials and query parameters are omitted from API responses. A camera can be supplied as an `stream_url` or as `camera_ip` with paired optional `camera_username`/`camera_password`, plus an optional `camera_brand`, `device_service_url`, and `onvif_profile_token`.
- `POST /api/cameras/onvif-profiles` returns the ONVIF media profile names/tokens for a camera without exposing stream URLs or credentials. `POST /api/discovery/scan` accepts optional private IPv4 `ip_range` CIDR and reports `scan_mode` and `targets_scanned`.
- `GET /api/cameras/{id}/segments` returns indexed coverage; authenticated media routes serve stored MP4 segments.
- `GET /api/cameras` reports each camera's separate `last_video_at` and `last_footage_write_at` timestamps plus a `recording_health` state. `GET /api/cameras/{id}/coverage` returns the current classification and recent persisted intervals. Coverage is classified as covered, expected idle, failure, or uncertain. Motion-only idle with healthy video/events is expected idle; stale continuous writes while video remains fresh are surfaced as `write_stalled`.
- Administrators configure weekly group policies and per-camera overrides from the group and camera cards. Schedule JSON uses ISO weekdays (`0=Monday`), local `HH:MM` times, and `motion`, `continuous`, or `disabled` modes. Intervals are start-inclusive/end-exclusive, overnight intervals belong to their start day, and overlaps are rejected. Week schedules use the selected IANA timezone; repeated fall-back wall times apply to both occurrences, while skipped spring-forward wall times do not occur. A schedule supervisor applies changes at most five seconds after a boundary. Cameras without an explicit override inherit their group schedule; removing a camera override restores inheritance.
- Entra OIDC is enabled only when the five `NVR_PUBLIC_URL` and `NVR_OIDC_*` connection values are set and at least one role mapping is configured. The configured public URL must be HTTPS except for localhost development. Register its exact callback URL, `<NVR_PUBLIC_URL>/api/auth/oidc/callback`, as a **Web** redirect URI in the Entra app registration.
- Assign Entra application roles to staff and map their exact role values with `NVR_OIDC_ADMIN_ROLES` and `NVR_OIDC_OPERATOR_ROLES`. Accounts without an explicit mapping are denied. Role mapping is read from the verified ID token; group overage and unassigned accounts do not grant access.
- The OIDC authorization-code flow uses Authlib for discovery, state/nonce validation, ID-token signature and claim validation, and PKCE. Set `NVR_OIDC_SESSION_SECRET` to a long random value and keep it stable across restarts. A provider outage returns a clear sign-in error while the emergency local administrator can still sign in at `/api/auth/login`.
- Operators can list cameras and view indexed footage. Camera configuration, system status, and session administration require an administrator. Administrators can review and terminate active sessions at `/api/admin/sessions`; session lifetimes are capped at 168 hours by `NVR_SESSION_HOURS`.
- Administrators can archive a camera to stop ingest and remove it from live viewing while retaining its identity and indexed footage for historical search and playback. Permanent footage deletion is a separate administrator action that requires the camera to be archived, exact camera-name confirmation, and no active preservation hold, export, or playback lease. The archived camera identity remains after footage deletion. Operators receive `403` for archive or footage-deletion requests. See [console acceptance and operating limits](docs/console-acceptance.md).
- ONVIF WS-Discovery is an explicit administrator action at **Scan camera VLAN**. The default stack uses host networking on Linux, the arrangement validated during deployment testing. Set `NVR_DISCOVERY_INTERFACE` to the camera-network interface IPv4 address when multicast needs a specific source interface. The app binds directly to `NVR_PORT` (8280 by default); no Docker port mapping applies in host mode. Protect the listener with the host firewall or approved gateway.
- Routed camera discovery is available from **Scan IP range**. Enter a private IPv4 CIDR for the camera network; the NVR needs an allowed route and firewall access to the camera network, but does not need an interface on that VLAN. The scanner probes common ONVIF ports and is limited to 256 addresses per scan. This is unicast ONVIF probing, so it will not find devices that have ONVIF disabled or use an unsupported port.
- To collect evidence for a discovery issue, enable **Support diagnostics** in the administrator UI, run a scan, and download the diagnostics bundle. The bundle deliberately omits IP addresses and device inventory. Do not send camera passwords, RTSP URLs, raw packet captures, or footage.
- Discovery returns review candidates only and never starts recording. Add a camera with separate IP, ONVIF port, brand/type, username, and password fields. The brand selector prioritizes common ONVIF ports for ACTi, Axis, Bosch, Dahua, Hanwha, Hikvision, Reolink, Samsung, and Uniview, then checks other common ports. For cameras with a non-standard ONVIF device-service path, enter its custom URL. **Load camera endpoints** lists every ONVIF media profile advertised by the camera (including main, sub, and other profiles); select the desired profile and test the stream before adding. Cameras that do not expose ONVIF media profiles cannot use this flow. Device UUIDs deduplicate repeat WS-Discovery results and link scans to manually added cameras when the UUID is known. Audio capability detection does not mean audio is recorded or playable.
- Administrators can create camera groups, select cameras for bulk group/recording-mode changes, and rotate a camera's saved stream URL. A credential rotation is probed before the encrypted URL is replaced; failures keep the old URL, camera ID, and footage association. Bulk CSV onboarding is available from the admin screen: preview the file first, then test accepted streams and apply rows. Required columns are `name,stream_url`; optional columns are `recording_mode,group_name,onvif_uuid`. Imports are limited to 500 rows per request, return row-level status without echoing stream URLs, and create a camera only after its video probe succeeds. The API exposes `POST /api/cameras/import/preview`, `POST /api/cameras/import/apply`, `POST /api/cameras/bulk-update`, `POST /api/cameras/{id}/credentials`, and `GET /api/camera-groups`.
- Automation uses scoped API tokens. An administrator creates one with `POST /api/admin/api-tokens` (`name`, `scopes`, optional `expires_at`), lists them with `GET /api/admin/api-tokens`, and revokes one with `DELETE /api/admin/api-tokens/{id}`. The secret is returned once at creation, and only a keyed digest is stored. Send it as `Authorization: Bearer <token>`; tokens in query strings or cookies are ignored. Scopes are `read` (inventory, status, coverage, events, incidents), `media` (live, segment media, snapshots, frame navigation), `export` (create, view, cancel, download and verify exports; preserve segments) and `admin` (every administrator operation; implies the others). Other changes, such as personal live views, need a browser session or an `admin` token. A token acts for the administrator who created it and stops working if it is revoked, expires, or its creator is no longer an administrator. Invalid tokens return `401`; a missing scope returns `403` naming the scope. Tokens cannot manage tokens. Creation, revocation and use (at most once a minute per token) are recorded in the audit log. `/openapi.json` lists the scope each operation requires as `x-required-scope`.
- Weekly recording policy APIs are `GET/PUT/DELETE /api/camera-groups/{id}/schedule` and `GET/PUT/DELETE /api/cameras/{id}/schedule`. An explicit camera schedule overrides its group's policy; removing the override restores inheritance. Operators can see the effective mode and policy source but cannot change schedules.

New cameras default to **Motion** with recording disabled. An administrator must explicitly enable recording. Motion mode uses the camera's ONVIF PullPoint events and keeps a bounded timestamped video buffer; the default is 10 seconds of pre-roll and 30 seconds of post-roll. Overlapping events extend a shared clip and keep their source timestamps and topics. Clips with incomplete startup pre-roll are marked in the event list.

Configure the camera's ONVIF device-service URL and optional credentials from its admin card. ONVIF credentials are encrypted at rest and are not returned by the API. When enabled, **server motion fallback** starts only after the camera event source becomes unavailable or unsupported. It compares low-resolution grayscale video inside the configured normalized rectangular zones and records the same motion clips. Sensitivity ranges from 1 to 100; an empty zone list means the full frame. It does not use person/vehicle inference and does not switch to continuous recording. The camera card reports ONVIF and server detector health separately, including detector failure and recovery.

The software tests include a local ONVIF PullPoint fixture and generated playable footage. Live ONVIF discovery and event delivery on the camera VLAN still need validation after deployment. Continuous mode remains available as an explicit separate policy. FFmpeg remuxes source video without re-encoding into MP4 footage; segment boundaries follow source keyframes. Browser playback depends on the camera codec. H.264/AVC in MP4 is a pragmatic compatibility target; HEVC/H.265 and vendor codecs may not play in every browser. Camera audio remains disabled until an administrator probes support and explicitly enables it. When enabled, recordings use AAC in MP4 and live listening uses MP3; see [camera-audio.md](docs/camera-audio.md) for behavior and browser limitations. FFmpeg and FFprobe are included in the image, and the recorder retries interrupted RTSP/TCP connections.

The application encrypts camera URLs before database storage, keeps the key on a separate Docker volume, suppresses FFmpeg diagnostics that could contain credentials, and passes the URL to the media process over stdin rather than exposing it in process arguments. Protect both volumes and restrict access to the Docker host.

### Upgrades and schema migrations

The database schema is versioned. On startup the application applies any pending migrations in order and records each one in the `schema_migrations` table. An installation created before versioning is recognised, converged onto the baseline schema and stamped as version 1 without changing its records. Before migrating a database that holds data, the application writes an owner-only copy beside it in the data volume (`nvr.sqlite3.pre-migration-v<from>-to-v<to>-<timestamp>`) and keeps the three most recent copies. If a migration fails, startup stops with the failing version and the copy's location; the failed migration's changes are rolled back and it is not recorded.

Migrations are forward-only. An image older than the database's recorded schema version refuses to start rather than run against a schema it does not understand, so downgrading requires restoring the pre-upgrade copy (or configuration backup) together with the older image. Recordings are not affected by schema migrations.

Use `docker compose restart` to restart the service. The account remains in the `nvr-data` volume and the health check returns healthy after startup. To intentionally reset the test installation, run `docker compose down -v`; this permanently deletes the local database and any volume data.

## Development

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
uvicorn app.main:app --reload
```

## Current boundary

This implementation is not yet qualified as a production replacement. Complete the post-merge browser, camera compatibility, upgrade, and workload checks in [the console acceptance notes](docs/console-acceptance.md), followed by the original 30-day production acceptance gate. No capacity claim beyond the implemented sixteen live video and sixteen live audio slots is made, and no 200-camera certification is claimed. Configure Entra for the target tenant and verify its callback and role assignments before enabling staff sign-in. Administrators can set the application name, colors, logos, and favicon in Appearance and Branding. An in-app backup workflow is not provided; back up the database, secrets, and recordings volumes together with the host's approved backup process. See the [workspace overhaul acceptance report](docs/workspace-overhaul-acceptance.md) for the checks completed and deployment gates still pending.
