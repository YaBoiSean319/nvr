# Workplace NVR

A self-hosted network video recorder for workplace cameras. The current implementation includes secure first-run administration, persistent Docker Compose storage, ONVIF motion-event recording with bounded pre/post-roll, optional server-side motion fallback, continuous recording, and authenticated playback of indexed MP4 footage. The broader approved NVR specification remains in progress.

## Requirements

- Docker Engine with the Compose plugin
- A host with a persistent filesystem for the Compose volumes
- Docker Compose 2.24 or newer when using the optional camera-discovery network override

## Start the stack

For a basic install, use the default isolated Docker bridge network:

```sh
docker compose pull
docker compose up -d
docker compose logs -f nvr
```

The published `ghcr.io/yaboisean319/nvr:edge` image is built from the latest `main` branch. Compose checks for a newer image on each `up`. The web interface is available at `http://<server>:8280` by default; set `NVR_PORT` in `.env` to choose another host port. Create the first local administrator on a trusted setup network. No default account or password is shipped. The setup endpoint is permanently closed as soon as the first administrator is committed to the database. Configure TLS through the approved workplace gateway and set `NVR_COOKIE_SECURE=true` when the application is served over HTTPS.

The `nvr-data` volume stores the database and account/session state. `nvr-secrets` stores the generated credential-encryption key with owner-only file permissions. `nvr-recordings` stores the MP4 segments. All three survive container replacement and host reboot as long as Docker's volume data is retained. Back up and protect the data and secrets volumes together; a database-only export does not contain the key needed to decrypt camera URLs.

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `NVR_PORT` | `8280` | Host port for the web application |
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
- Entra OIDC is enabled only when the five `NVR_PUBLIC_URL` and `NVR_OIDC_*` connection values are set and at least one role mapping is configured. The configured public URL must be HTTPS except for localhost development. Register its exact callback URL, `<NVR_PUBLIC_URL>/api/auth/oidc/callback`, as a **Web** redirect URI in the Entra app registration.
- Assign Entra application roles to staff and map their exact role values with `NVR_OIDC_ADMIN_ROLES` and `NVR_OIDC_OPERATOR_ROLES`. Accounts without an explicit mapping are denied. Role mapping is read from the verified ID token; group overage and unassigned accounts do not grant access.
- The OIDC authorization-code flow uses Authlib for discovery, state/nonce validation, ID-token signature and claim validation, and PKCE. Set `NVR_OIDC_SESSION_SECRET` to a long random value and keep it stable across restarts. A provider outage returns a clear sign-in error while the emergency local administrator can still sign in at `/api/auth/login`.
- Operators can list cameras and view indexed footage. Camera configuration, system status, and session administration require an administrator. Administrators can review and terminate active sessions at `/api/admin/sessions`; session lifetimes are capped at 168 hours by `NVR_SESSION_HOURS`.
- Administrators can remove a camera and its stored footage. The API removes footage before deleting the camera record; if footage cleanup fails, the camera record is retained. Operators receive `403` for camera deletion requests.
- ONVIF WS-Discovery is an explicit administrator action at **Scan camera VLAN**. The default Docker bridge does not reliably pass camera VLAN multicast. On a Linux Docker host with a camera VLAN interface, configure `NVR_DISCOVERY_INTERFACE` to that host interface's IPv4 address and run the stack with `docker compose -f compose.yaml -f compose.discovery.yaml up -d`. Host networking lets multicast replies reach the app; it removes the normal Docker port isolation and binds the app directly to host port `8080`, so protect that port with the host firewall or approved gateway. Keep both Compose files on future `up`/`down` commands.
- Routed camera discovery is available from **Scan IP range**. Enter a private IPv4 CIDR such as `10.45.50.0/24`; the NVR needs an allowed route and firewall access to the camera network, but does not need an interface on that VLAN. The scanner probes common ONVIF ports and is limited to 256 addresses per scan. This is unicast ONVIF probing, so it will not find devices that have ONVIF disabled or use an unsupported port.
- To collect evidence for a discovery issue, enable **Support diagnostics** in the administrator UI, run a scan, and download the diagnostics bundle. The bundle deliberately omits IP addresses and device inventory. Do not send camera passwords, RTSP URLs, raw packet captures, or footage.
- Discovery returns review candidates only and never starts recording. Add a camera with separate IP, ONVIF port, brand/type, username, and password fields. The brand selector prioritizes common ONVIF ports for ACTi, Axis, Bosch, Dahua, Hanwha, Hikvision, Reolink, Samsung, and Uniview, then checks other common ports. **Load camera endpoints** lists every ONVIF media profile advertised by the camera (including main, sub, and other profiles); select the desired profile and test the stream before adding. Cameras that do not expose ONVIF media profiles cannot use this flow. Device UUIDs deduplicate repeat WS-Discovery results and link scans to manually added cameras when the UUID is known. Audio capability detection does not mean audio is recorded or playable.
- Administrators can create camera groups, select cameras for bulk group/recording-mode changes, and rotate a camera's saved stream URL. A credential rotation is probed before the encrypted URL is replaced; failures keep the old URL, camera ID, and footage association. Bulk CSV onboarding is available from the admin screen: preview the file first, then test accepted streams and apply rows. Required columns are `name,stream_url`; optional columns are `recording_mode,group_name,onvif_uuid`. Imports are limited to 500 rows per request, return row-level status without echoing stream URLs, and create a camera only after its video probe succeeds. The API exposes `POST /api/cameras/import/preview`, `POST /api/cameras/import/apply`, `POST /api/cameras/bulk-update`, `POST /api/cameras/{id}/credentials`, and `GET /api/camera-groups`.

New cameras default to **Motion** with recording disabled. An administrator must explicitly enable recording. Motion mode uses the camera's ONVIF PullPoint events and keeps a bounded timestamped video buffer; the default is 10 seconds of pre-roll and 30 seconds of post-roll. Overlapping events extend a shared clip and keep their source timestamps and topics. Clips with incomplete startup pre-roll are marked in the event list.

Configure the camera's ONVIF device-service URL and optional credentials from its admin card. ONVIF credentials are encrypted at rest and are not returned by the API. When enabled, **server motion fallback** starts only after the camera event source becomes unavailable or unsupported. It compares low-resolution grayscale video inside the configured normalized rectangular zones and records the same motion clips. Sensitivity ranges from 1 to 100; an empty zone list means the full frame. It does not use person/vehicle inference and does not switch to continuous recording. The camera card reports ONVIF and server detector health separately, including detector failure and recovery.

The software tests include a local ONVIF PullPoint fixture and generated playable footage. Live ONVIF discovery and event delivery on the camera VLAN still need validation after deployment. Continuous mode remains available as an explicit separate policy. FFmpeg remuxes source video without re-encoding into MP4 footage; segment boundaries follow source keyframes. Browser playback depends on the camera codec. H.264/AVC in MP4 is a pragmatic compatibility target; HEVC/H.265 and vendor codecs may not play in every browser. Audio is omitted. FFmpeg and FFprobe are included in the image, and the recorder retries interrupted RTSP/TCP connections.

The application encrypts camera URLs before database storage, keeps the key on a separate Docker volume, suppresses FFmpeg diagnostics that could contain credentials, and passes the URL to the media process over stdin rather than exposing it in process arguments. Protect both volumes and restrict access to the Docker host.

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

Motion detection, multi-camera load controls, retention, and the remaining approved NVR features are still to be implemented. Entra sign-in and role enforcement are implemented in this slice, but the installation still needs tenant-specific setup and validation. This recorder path has not been qualified for 200+ cameras or a production retention workload; compatibility and capacity tests are required before replacement use.
