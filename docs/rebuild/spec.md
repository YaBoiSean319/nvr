# [Spec] Architectural rebuild — modular backend, isolated recording workers, typed SPA, low-latency live, API tokens and backups

Triage: `ready-for-agent`. Extends #1, #33, #49 and #67; incorporates open tickets #77 and #78.

> Publishing to GitHub Issues failed on 2026-10-23 (integration lacks issue write permission). This file is the authoritative copy until it is published.

## Problem Statement

The Workplace NVR has grown through #1, #33, #49 and #67 into a capable product, but its structure now works against the user and the operators who rely on it:

- Recording, motion detection, AI inference, export rendering, webhook delivery and live-view transcoding all share one web process and one event loop. A heavy export, a burst of live viewers or an inference stall can delay required footage writes, and a crash in any part stops recording. Spec #1 requires recording to stay independent of inference and viewing.
- Live viewing re-encodes every stream to MJPEG on the server. This is CPU-expensive per viewer, carries no audio (a separate MP3 stream is used), and makes the #49 target of a first fresh frame within 2 seconds hard to meet under load.
- The API is one very large module and the browser interface is a hand-built script of several thousand lines with no type checking. Each new workflow (tiling, drafts, timeline, exports, camera administration) has been harder to add without regressions, and recent tickets (#77, #78) are layout and presentation corrections the structure made difficult.
- The schema evolves by "create if not exists" plus ad-hoc column additions, so upgrades of existing installations are not versioned or recoverable.
- Spec #1 requirements remain unbuilt: scoped revocable automation credentials and scheduled configuration/index backups with a restore procedure.

## Solution

Rebuild the application architecture **in place**, preserving the product, its data and its public contracts, and deliver the full combined behavior of #1, #33, #49 and #67 plus open tickets #77 and #78:

- A modular backend: domain routers and services behind the existing FastAPI/SQLite/FFmpeg stack, with recording, AI and export work in separately supervised worker processes in the same container.
- A versioned migration runner that upgrades existing installations in place, with an automatic configuration backup before each migration.
- Low-latency live viewing by repackaging H.264 (and AAC audio) into fragmented MP4 over WebSocket for Media Source Extensions playback, with MJPEG kept as a truthful fallback for codecs the browser cannot play.
- A TypeScript single-page application (React, Vite) that implements the Live, Playback, Cameras and Settings workspaces against the approved specifications, served at a temporary preview path during the rebuild and then replacing the legacy interface in a final cutover.
- Scoped, revocable API tokens and encrypted scheduled configuration backups with an offline restore command.

Existing installations upgrade by pulling the new image: accounts, cameras, encrypted credentials, groups, policies, footage, holds, exports, saved views, branding, incidents, webhooks and heartbeat configuration remain intact.

## User Stories

### Upgrade and continuity

1. As an administrator, I want to upgrade by pulling the new image, so that I do not have to re-onboard cameras or users.
2. As an administrator, I want the database migrated with versioned, recorded migrations, so that I know which schema version an installation is on.
3. As an administrator, I want a configuration backup taken automatically before any migration, so that I can recover if an upgrade fails.
4. As an administrator, I want a failed migration to stop startup with a clear message rather than run on a half-migrated database, so that data is not silently corrupted.
5. As an administrator, I want existing camera credentials to keep decrypting with the existing key volume, so that cameras keep recording after the upgrade.
6. As an administrator, I want existing footage, segment indexes, coverage, motion events and AI observations retained, so that historical investigation continues.
7. As an administrator, I want existing holds, export jobs and evidence packages retained and verifiable, so that preservation guarantees survive the upgrade.
8. As an operator, I want my existing personal views and last workspace restored, so that the upgrade does not disrupt my monitoring.
9. As an administrator, I want existing shared views, groups, memberships, policy sources, schedules, retention and storage assignments unchanged, so that effective recording policy does not shift.
10. As an administrator, I want existing branding, display settings and theme preferences retained, so that users see the same product.
11. As an integrator, I want existing API paths and response shapes preserved, so that my automation keeps working.
12. As an integrator, I want webhook event IDs, payloads and signature verification unchanged, so that my receivers keep verifying deliveries.
13. As an administrator, I want the external heartbeat format and signing unchanged, so that my external checker keeps working.
14. As an administrator, I want a documented downgrade boundary, so that I know a schema migration cannot be reversed by simply running an older image.

### Recording isolation and reliability

15. As an administrator, I want recording to run in its own process, so that a web or export failure does not stop footage writes.
16. As an administrator, I want AI inference in its own process, so that inference backlog or crashes cannot block recording.
17. As an administrator, I want export rendering in its own process, so that large exports cannot stall recording or the interface.
18. As an administrator, I want each worker supervised and restarted automatically with bounded backoff, so that transient faults recover without intervention.
19. As an operator, I want a worker crash raised as an incident with recovery, so that I can see when recording or analysis was interrupted.
20. As an administrator, I want recording to continue while the web process restarts, so that interface upgrades or crashes do not create footage gaps.
21. As an administrator, I want configuration changes made in the web interface applied to the recorder promptly, so that schedules, modes and credentials take effect without restarts.
22. As an administrator, I want health to report each worker's liveness separately, so that a healthy web interface is not mistaken for healthy recording.
23. As an administrator, I want abrupt shutdown recovery to reconcile segment files and the index, so that damaged or missing media is reported rather than presented as healthy.
24. As an administrator, I want concurrent access from several processes to the database to stay consistent, so that no worker sees partial writes.
25. As an administrator, I want all existing motion, pre/post-roll, schedule, retention, storage-pressure, preservation and coverage semantics unchanged, so that the rebuild does not alter recording behavior.

### Live viewing transport

26. As an operator, I want live video to start quickly, so that I can respond to events.
27. As an operator, I want live video delivered without server re-encoding where the browser supports the codec, so that many feeds can be watched without exhausting the server.
28. As an operator, I want live audio carried in the same stream as video when enabled, so that sound stays synchronized.
29. As an operator, I want a truthful fallback when a camera codec cannot be played directly, so that I still see video and know it is a fallback.
30. As an operator, I want stale, reconnecting, lost and offline states driven by actual received media, so that a frozen picture is never presented as live.
31. As an operator, I want viewers to be released when tiles are removed, hidden or the workspace changes, so that slots and camera connections are not leaked.
32. As an administrator, I want the existing sixteen video and sixteen audio slot limits enforced on the new transport, so that viewing stays bounded.
33. As an operator, I want one camera connection shared by multiple viewers of the same stream, so that rearranging tiles or opening the same camera twice does not multiply camera sessions.
34. As an administrator, I want live media authenticated per session or token, so that media is never reachable anonymously.
35. As an operator, I want grid tiles to prefer substreams and focused views to prefer main streams, with Auto/Main/Sub overrides, so that quality matches use.

### Application shell, identity and appearance

36. As a user, I want the new interface to look and behave as #49 describes, so that the product feels like a professional monitoring application.
37. As a new installation owner, I want first-run administrator setup in the new interface, so that no default account exists.
38. As a user, I want local sign-in and Entra sign-in in the new interface, so that both identity paths keep working.
39. As a user, I want site branding (name, colors, logos, favicon) applied to sign-in and the shell, so that the product matches the organization.
40. As a user, I want light, dark and system themes, so that I can use a comfortable appearance.
41. As a user, I want persistent operational status and incident access in every workspace, so that I notice problems wherever I am.
42. As an operator, I want administrator-only destinations hidden from me and rejected by the server, so that roles are enforced by the server and the interface matches them.
43. As a user, I want the application to stay responsive with 200+ cameras configured, so that browsing and filtering are fast.
44. As a keyboard, touch or screen-reader user, I want complete access to every workflow, so that essential actions never depend on hover or dragging.
45. As a user on a phone or tablet, I want collapsible panes and reachable controls, so that the application remains usable on small screens.
46. As an administrator, I want the old interface to remain available until the new one reaches parity, so that operations are not interrupted during the rebuild.

### Live workspace

47. As an operator, I want to land in Live with my last workspace restored (without audio), so that I resume monitoring immediately.
48. As an operator, I want one sidebar click to add or remove a camera with Auto tiling, so that I never choose a grid size first.
49. As an operator, I want existing feeds to keep playing through rearrangement, so that adding a camera does not interrupt others.
50. As an operator, I want drag, swap, replace, enlarge/restore, manual layouts with Return to Auto, and keyboard alternatives, so that I can arrange feeds precisely.
51. As an operator, I want group/view loading with a subset chooser beyond sixteen cameras, so that cameras are never silently dropped.
52. As an operator, I want personal and permitted shared views with explicit Save/Save as and a dirty indicator, so that views are never overwritten accidentally.
53. As an operator, I want tile actions for snapshot, audio, enlarge, Open in Playback and remove, so that features are reachable without clutter.
54. As an operator, I want a failed tile to keep its dimmed last frame and last-received time, so that I understand the failure without losing position.

### Playback workspace

55. As an operator, I want Playback to fit one desktop viewport as #77 describes, so that video, transport, search and timeline are visible together.
56. As an operator, I want a shared clock and aligned coverage/motion/AI/gap rows, so that I can follow incidents across cameras.
57. As an operator, I want shared play/pause, 0.25×–16× speeds, real frame stepping, event navigation and Skip Empty Periods, so that investigation is precise.
58. As an operator, I want camera additions to join the current clock and marks, so that investigation does not restart.
59. As an operator, I want site-timezone search with explicit handling of repeated and skipped daylight-saving times, so that I search the intended instant.
60. As an operator, I want rapid seeks coalesced and stale responses discarded, so that scrubbing stays responsive and correct.
61. As an operator, I want snapshots that never represent a gap placeholder as current footage, so that documentation is truthful.

### Exports

62. As an operator, I want Mark In/Out and an export drawer that does not take over Playback, so that I keep investigating while exporting.
63. As an operator, I want individual and grid outputs in one ZIP with existing gap-compression and manifest rules, so that evidence remains complete and understandable.
64. As an operator, I want compact camera/time overlays that never collide with gap notices as #78 describes, so that footage is not obscured.
65. As an operator, I want export jobs to continue in the background with progress, cancellation, retry, expiry and authenticated download, so that jobs are manageable.
66. As an operator, I want an interrupted export to report truthful recovery state after restart, so that I never receive a falsely complete package.
67. As an operator, I want existing evidence packages to stay verifiable, so that integrity checks keep working.

### Camera administration

68. As an administrator, I want the #67 inventory table, one-camera editor with tabs, explicit saves and draft protection, so that administration scales.
69. As an administrator, I want discovery, range scans, manual add, CSV import, link-to-existing and tested addition preserved, so that onboarding stays complete.
70. As an administrator, I want reviewed bulk changes with per-camera outcomes and failed-only retry, so that batches are safe.
71. As an administrator, I want Groups & Policies with one group editor, visual schedules and zone editors, so that policies are understandable.
72. As an administrator, I want an on-demand single-camera preview released on every exit path, so that administration never leaves streams running.
73. As an operator, I want read-only camera and effective-policy information, so that I can understand configuration without changing it.

### Settings

74. As an administrator, I want organized settings categories with explicit saves, so that every supported setting is findable.
75. As an administrator, I want sessions, SSO status, display settings, branding, storage, retention, AI runtime, webhooks, heartbeat, diagnostics, API tokens and backups in Settings, so that no existing function is lost.

### Scoped API tokens

76. As an administrator, I want to create named API tokens with explicit scopes, so that automation receives only the access it needs.
77. As an administrator, I want the token secret shown exactly once and stored only as a hash, so that a database leak does not expose usable tokens.
78. As an administrator, I want optional token expiry, so that temporary integrations lose access automatically.
79. As an administrator, I want to revoke a token immediately, so that I can cut off a compromised integration.
80. As an administrator, I want each token's last use and its lifecycle recorded in the audit log, so that I can review automation activity.
81. As an integrator, I want to authenticate with a bearer header, so that I can call the API without a browser session.
82. As an integrator, I want clear 401/403 responses that distinguish an invalid token from a missing scope, so that I can fix configuration.
83. As an administrator, I want tokens never accepted in URLs or written to logs, so that secrets do not leak through proxies or diagnostics.
84. As an integrator, I want a published API description listing the scope each operation requires, so that I can build automation correctly.

### Configuration backups

85. As an administrator, I want daily encrypted configuration backups to a separately mounted destination, so that I can recover from host or disk loss.
86. As an administrator, I want to trigger a backup on demand, so that I can protect configuration before maintenance.
87. As an administrator, I want backups to include the database, branding assets and the credential key, encrypted with a passphrase I provide, so that a restore is complete and the backup itself is protected.
88. As an administrator, I want backups disabled with a clear explanation when no passphrase is configured, so that unencrypted secrets are never written.
89. As an administrator, I want to see backup age, last result and failures in Settings and as incidents, so that I notice when backups stop.
90. As an administrator, I want a bounded number of backups retained, so that the destination does not fill.
91. As an administrator, I want an offline restore command with a documented procedure, so that I can rebuild an installation.
92. As an administrator, I want footage referenced by a restored index but missing on disk marked unavailable, so that restored search results are truthful.
93. As an administrator, I want documentation stating that footage is not included in configuration backups, so that I arrange separate footage protection.

### Acceptance

94. As an acceptance owner, I want real browser journeys in CI for every workspace, so that working screens are proven by behavior.
95. As an acceptance owner, I want deployment-only checks listed as unchecked post-merge items, so that CI success is never mistaken for field qualification.

## Implementation Decisions

### Approach and authority

- In-place architectural rebuild, not a greenfield rewrite. The four specifications (#1, #33, #49, #67) and open tickets #77 and #78 define the target; later specifications override earlier ones only where they explicitly say so. The behavior of open PR #79 is folded into the Playback and Exports slices; that PR itself is left untouched for the owner to decide.
- Backend stack unchanged: Python, FastAPI, SQLite, FFmpeg/FFprobe, OpenVINO, Authlib. No new database or media server.
- Delivered as ordered vertical slices on one development branch with a draft pull request for CI; each slice lands with passing checks. A progress checklist is kept alongside this spec.

### Backend module structure

- Split the monolithic application module into domain routers and services: setup/auth/sessions/OIDC; users and API tokens; settings (display, branding, theme); cameras and sources; discovery and onboarding; groups, memberships and policies (schedule, retention, storage, AI); live media; recordings, coverage, segments, holds and deletion; playback timeline and frame navigation; exports and evidence; incidents; webhooks; heartbeat; diagnostics; backups; system health.
- Database access through repository functions per domain over the standard sqlite3 module; no ORM. Connections use WAL mode, a busy timeout and foreign keys; write transactions are short.
- Shared authorization dependency resolves a principal from either a session cookie or a bearer API token and exposes role and scopes; every route declares its required role/scope.
- Background loops currently started inside the web process move behind a worker interface.

### Worker processes

- A supervisor entrypoint starts and monitors: the web process, the recorder worker (recording tasks, schedule supervisor, retention cleanup, storage health, ONVIF events and server motion), the AI worker, and the export worker. Webhook delivery, heartbeat and backups run in a lightweight background worker, never in request handlers.
- Workers restart with bounded exponential backoff; repeated failure raises an incident with recovery. Each worker writes a heartbeat row; system health reports per-worker liveness.
- Coordination is through the database plus a local control channel (Unix domain socket within the container) used to nudge workers on configuration change (camera saved, schedule changed, export queued, cancel requested). The database remains the source of truth; a missed nudge is caught by periodic reconciliation.
- Live media streaming stays in the web process but uses repackaging rather than transcoding where possible; it remains bounded by viewer admission limits so it cannot starve recording.
- For tests, the same worker interface can run in-process against an isolated store.

### Migrations

- Versioned migration runner with a schema-version table. The first migration is a baseline equal to the current schema; on an existing installation without a version table, the runner verifies the current schema shape and stamps the baseline rather than re-creating tables.
- Before applying pending migrations on a database with data, the runner takes a configuration backup (when backups are configured) or a local pre-migration copy of the database. Migration failure aborts startup with a clear log message and leaves the pre-migration copy.
- Migrations are forward-only; the downgrade boundary is documented.

### Live transport

- New authenticated WebSocket live endpoint delivering fragmented MP4 (initialization segment then media fragments) produced by FFmpeg stream copy from the camera's selected profile. H.264 video is copied; audio is included only when the camera's audio is administrator-enabled and is copied when AAC, otherwise transcoded to AAC.
- A per-stream fan-out hub (live hub) shares one camera connection among concurrent viewers of the same camera/profile; viewers join at the next keyframe. The hub closes the camera connection after the last viewer leaves (short linger).
- Codec negotiation: the client reports supported MIME codecs; when the stream's codec is unsupported (e.g. H.265 in most browsers) the client falls back to the existing MJPEG endpoint, and the UI labels the tile as fallback quality.
- Existing MJPEG and MP3 endpoints remain for compatibility and fallback. Slot admission (sixteen video, sixteen audio) applies across both transports.
- Tile freshness is derived from media actually received and decoded by the browser, not from connection state.

### Frontend

- TypeScript, React, Vite, React Router, TanStack Query for server state, Zustand for workspace state, Radix UI primitives for accessible dialogs/tabs/menus/popovers, plain CSS with design tokens. Branding derives accent tokens with contrast validation; status colors are fixed and never overridden by branding.
- Multi-stage container build: a Node stage builds the static bundle; the Python image serves it. The built bundle is not committed. Python API tests do not require the bundle; browser tests build it first.
- The new interface is served at a preview path during the rebuild while the legacy interface remains at the root; the final cutover slice serves the new interface at the root and removes the legacy one.
- Workspace state (Live, Playback) persists per user server-side through the existing workspace/saved-view APIs, extended as needed; only harmless preferences use browser storage; no credentials or drafts are persisted.
- Shared modules: API client with typed errors and stale-response guards; session/principal; branding/theme; status/incidents; camera browser; video tile (MSE player with MJPEG fallback and freshness tracking); timeline; drawers and confirm/dirty-guard dialogs.

### API compatibility

- Existing public routes, request/response shapes, status codes, webhook payloads/signing and heartbeat format are preserved. Interface-only endpoints may change. Deliberate API changes are listed in the progress checklist with the reason.
- An OpenAPI description is published and documents the role/scope required per operation.

### Scoped API tokens

- New table of tokens: identifier, name, hashed secret (keyed hash; plaintext shown once at creation), scopes, creating administrator, created/expires/revoked/last-used timestamps.
- Scopes: `read` (camera inventory, status, coverage, events, incidents, metadata), `media` (live, segments, snapshots), `export` (create, view, cancel and download exports; preserve holds), `admin` (all administrator operations). `admin` implies the others. A token never exceeds administrator capability; operator-level operations require read/media/export as appropriate.
- Accepted only through the `Authorization: Bearer` header; never via query string or cookies. Invalid, expired or revoked tokens return 401; insufficient scope returns 403 naming the missing scope.
- Creation, revocation and use are audited; last-used is updated with bounded write frequency.
- Administrator-only management API and Settings UI.

### Configuration backups

- Backups are an online SQLite backup, branding assets and the credential key, packaged into one archive encrypted with a key derived from an administrator-supplied passphrase environment variable (memory-hard KDF plus authenticated encryption).
- Destination is a separately mounted directory, configured by environment, documented in Compose as a separate volume or host path. Defaults: daily at 02:17 site time, keep 14, plus on-demand and pre-migration backups.
- Without a passphrase, backups are disabled and Settings/health say so; nothing unencrypted is written.
- Backup status (last success, last failure, age, count) appears in Settings and system health; failures or excessive age raise incidents.
- Restore is an offline command-line action against stopped services: decrypt, verify, replace the database, assets and key, then on next start reconcile and mark index entries whose files are missing as unavailable.

### Deployment-only checks

Real camera models/codecs/audio, real Entra tenant sign-in, latency and responsiveness targets on reference hardware, 1080p/4K export load, recording health during heavy viewing, persisted-volume upgrade of the production installation and the 30-day parallel pilot are recorded as unchecked **Post-merge testing required** items with version, environment, steps, owner and evidence fields.

## Testing Decisions

- A good test drives the system through its external boundary and asserts observable outcomes: HTTP responses and role decisions, persisted state visible through the API, decoded media and ZIP contents probed with FFprobe, webhook deliveries to a local receiver, recovery after restart, and visible browser behavior. Tests do not assert private helpers, internal module structure or markup strings.
- **Primary seam — authenticated HTTP API:** boot the real application against an isolated database, recordings directory and key; run the recorder, AI and export workers in-process through the same worker interface used in production; use deterministic camera simulators (FFmpeg-generated RTSP through MediaMTX and the existing ONVIF PullPoint fixture). The existing Python suite is preserved as the regression oracle and moves onto this seam; tests change only where an API deliberately changes.
- **Second seam — real-browser journeys:** Playwright against the same server and fixtures at 320/360/768/1024/1440 widths and the 1920×1080, 1440×900 and 1366×768 desktop sizes from #77. Assert visible state, element geometry, network/media lifecycle (including MSE playback and teardown) and persisted results. Markup-assertion navigation tests are replaced by these journeys.
- **Narrow exceptions:** pure logic that is hard to reach from the API — schedule/DST interval arithmetic, export time-mapping and gap compression, and the migration runner applied to snapshot databases of earlier schemas — may be tested directly.
- Required coverage additions: migration from a current-schema snapshot with data; worker crash/restart and recording continuity while the web process restarts; fMP4 live delivery, fan-out sharing, fallback and slot release; API token scopes, expiry, revocation, header-only acceptance and audit; backup encrypt/restore round trip, missing-passphrase behavior and missing-footage reconciliation; #77 viewport geometry; #78 rendered overlay bounds from decoded frames.
- Prior art: FastAPI TestClient administrator/operator checks, isolated SQLite/recording stores, generated FFmpeg footage and FFprobe inspection, ONVIF PullPoint fixture, export integrity/recovery tests, display/DST tests, saved-view ownership, schedule inheritance, audio gates, and the existing Playwright camera-admin acceptance with MediaMTX in CI.
- Report only suites actually executed; never mark an unexecuted suite or deployment check as passed.

## Out of Scope

- PTZ control with leases, metrics export, off-host log forwarding and scheduled discovery (spec #1 items deferred to later work; the module layout leaves room for them).
- A new backend language, database, ORM, bundled media server, WebRTC, or distributed/multi-host recording.
- Footage backup or replication, and in-app restore (restore is an offline command).
- Changes to recording policy semantics, retention/preservation rules, evidence content or role definitions beyond what the existing specifications state.
- Merging PR #79, closing earlier specifications, deploying, or changing the running installation.
- Claims of production capacity, latency or 30-day qualification from CI results.

## Further Notes

- Decided in a grilling session with the project owner on 2026-10-23: in-place rebuild (not greenfield, not frontend-only); all four specifications plus #77/#78; parity first with API tokens and backups added; keep Python/FastAPI/SQLite/FFmpeg; React/TypeScript/Vite frontend; in-place upgrade with versioned migrations; preserve public API/webhook/heartbeat formats; fMP4-over-WebSocket live with MJPEG fallback; separate supervised worker processes; preview path then cutover; draft PR for CI; continuous slice execution; backups daily 02:17 site time keeping 14, disabled without a passphrase.
- Glossary from earlier specifications applies: camera, stream, segment, motion event, AI observation, incident, preservation hold, Live workspace, Playback workspace, selected camera, active tile, Auto layout, saved view, site branding. New terms: **worker** (a supervised background process with one responsibility), **live hub** (shared per-stream fan-out of one camera connection), **API token** (scoped bearer credential), **configuration backup** (encrypted database, assets and key archive excluding footage).
- No customer camera addresses, credentials, footage, tenant identifiers or workplace names belong in fixtures, issues or CI artifacts.
