# Architecture and module boundaries

This describes how the backend is organised during the architectural rebuild (#80): domain routers for the HTTP API, and supervised worker processes for background work.

## Processes

The container entrypoint (`app.entrypoint`) runs `app.workers.supervisor`. The supervisor applies schema migrations once, then starts each worker as its own process:

| Worker | Module | Responsibility |
|---|---|---|
| `recorder` | `app.workers.recorder` | Recording tasks, schedule supervision, retention cleanup, storage health, ONVIF events and server motion fallback. |
| `exporter` | `app.workers.exporter` | Rendering queued export jobs; polls every 2 seconds for queued jobs and cancel requests. |
| `ai` | `app.workers.ai` | Person/vehicle analysis on selected cameras, storing detections and tracks. |
| `rules` | `app.workers.rules` | Evaluating enabled rules over newly stored detections and recording rule events; polls every second, and a rule change nudges it. |
| `background` | `app.workers.background` | Signed webhook delivery and the external heartbeat. |
| `web` | `app.workers.web` | The HTTP API and browser interface (uvicorn serving `app.main:app`). |

Each worker writes a heartbeat to `worker_status` every 5 seconds. The supervisor restarts a worker that exits, or that stops reporting for 30 seconds, after an exponential backoff from 1 to 60 seconds. The failure counter resets after 60 seconds of stable running. While a worker is down, an incident is active (`worker_recorder`, `worker_exporter`, `ai_worker`, `worker_rules`, `worker_background` or `worker_web`), and it recovers once the replacement reports liveness. Webhooks receive recorder incidents as `recording.*` and AI incidents as `ai.*`; the other workers' incidents are `system.*`. When the exporter restarts, any export it was rendering is marked failed. When the AI worker stops, every analysed camera is marked `interrupted`, with an unanalysed coverage interval, until analysis resumes. On SIGTERM the supervisor stops the web worker first, then the exporter, AI, rules and background workers, and the recorder last, and allows each `NVR_STOP_GRACE_SECONDS` (default 25) before killing it. `GET /api/system/status` lists every worker's liveness, and reports `degraded` when one is not alive.

The database is the source of truth. When the web process saves something the recorder must act on, it sends a nudge over a Unix socket in the data volume's `run/` directory (`app.workers.control`). It then waits up to 10 seconds for the worker to confirm it has acted, which keeps the ordering callers had in-process (for example, recording has stopped before an archive request returns). The recorder also reconciles every camera from the database every 5 seconds, so a lost nudge only delays the change. Inside the supervised web process, `app.state.recorders`, `app.state.exports`, `app.state.detectors` and `app.state.rules` are `RecorderClient`, `ExportClient`, `DetectionClient` and `RulesClient`. They send these nudges in place of the in-process managers, and no background loop runs in the web process.

Without the supervisor (tests, `uvicorn app.main:app --reload`), `create_app()` runs every manager in-process, as before.

Media leases for playback, snapshots and downloads are shared `flock` locks on the files themselves (`app.retention`). Retention cleanup and footage deletion in any process take a non-blocking exclusive lock before unlinking, and a crashed holder's locks disappear with it. Exports in progress are also protected by their `export_sources` reservation in the database.

## Live video

`/api/cameras/{id}/live/stream` is an authenticated WebSocket. It needs a session cookie or an API token with the `media` scope, and it refuses cross-origin browser handshakes. It delivers fragmented MP4 for Media Source Extensions: a JSON `init` message (MIME type with codecs, profile, audio), the binary initialization segment, then binary media fragments of about 300 ms each.

FFmpeg copies the camera's H.264 video without re-encoding it, except for cameras the NVR must mask: their video is transcoded to H.264 with the privacy mask drawn in, in a separate shared stream, so masked and unmasked viewers never share one (see [privacy masks](privacy-masks.md)). Open streams stop when the camera's masks or a viewer's unmasked grant change. When an administrator has enabled the camera's audio, the audio is transcoded to mono AAC in the same stream. It is always transcoded, even when the camera already sends AAC, which keeps one code path for every camera audio codec. `app.live_hub` keeps one camera connection per camera, profile, audio and mask combination, shared by every viewer. A late viewer starts at the most recent keyframe fragment, so it decodes immediately. The connection closes 5 seconds after the last viewer leaves. A viewer that cannot keep up is disconnected (`4408`) rather than holding the shared stream back.

`quality=low` uses the configured substream and falls back to the main stream. Refusals send a JSON message and close with a code that mirrors HTTP: `4401`/`4403` authentication, `4404` unknown camera, `4410` archived, `4415` unsupported codec, `4429` no free viewer slot, `4502` camera stream failure. An unsupported-codec refusal (for example H.265, detected from the probed codec or the initialization segment) names the MJPEG `fallback` URL. The existing MJPEG and MP3 endpoints remain, and the sixteen video and sixteen audio slots are shared across both transports.

## Composition

- `app.main` is the ASGI app the web worker serves. It only builds the application with `create_app()`.
- `app.application.create_app(start_workers=True)` owns the middleware, the lifespan, and router registration. The lifespan applies schema migrations, then starts the export, webhook, heartbeat, recording, detection and rules managers. `start_workers=False` serves the API without background loops.
- `app.api.ROUTERS` lists every domain router in registration order. The browser interface router (`app.api.web_interface`) is registered after all of them, so its client-route catch-all can never shadow an API route; paths under `/api/` and `/assets/` that match nothing return a JSON `404` instead of the interface.

## Browser interface

`web/` is a React and TypeScript single-page application built by Vite into `app/web_dist`. `app.api.web_interface` serves it at the site root with a strict Content-Security-Policy: hashed files under `/assets/` are cached as immutable, and every other non-API path returns the application page so client routes (`/live`, `/playback`, `/cameras`, `/settings/…`) survive a reload. The preview address `/next/…` permanently redirects (`308`) to the same path at the root. If the interface has not been built, the page returns `503` while the API keeps working. The building blocks are:

- `api/client.ts`: same-origin fetch with typed `ApiError`s, a session-expiry signal, and `latestOnly` so overlapping requests never let an older response win.
- `api/queries.ts`: TanStack Query hooks for server state.
- The shell: role-gated navigation, plus a status area that reports live-video freshness, recording-write health, incidents and export jobs separately.
- Site branding: the accent colour is adjusted until it reaches 4.5:1 contrast in both themes, and status colours are never branded.
- Theme: a per-user preference stored on the server (`/api/me/preferences`).
- Drawers that return focus to whatever opened them.
- Workspace state (`features/*/store.ts`, Zustand) belongs to one signed-in person and is cleared on sign-out or a change of user. Only harmless table preferences are kept in browser storage; selections, drafts and secrets never are.
- Editor drafts (`features/admin/tabDraft.ts`, `features/admin/drafts.tsx`): explicit Save and Revert, refreshes that never overwrite edits, partial-save outcomes, and one leave guard for route changes, sign-out and closing the tab.
- Live video (`features/live/LivePlayer.ts`): the fMP4 WebSocket described above through Media Source Extensions, with the MJPEG fallback when the browser cannot decode the stream.
- Playback (`features/playback`): one wall-clock-driven `PlaybackClock` shared by every tile, site-time helpers in `lib/zonedTime.ts`, and microsecond frame positions.

## Domain routers (`app.api`)

| Module | Responsibility |
|---|---|
| `system` | Liveness check used by Compose (`/healthz`). |
| `identity` | First-run administrator, local sign-in/out, Entra OIDC, session administration. |
| `site_settings` | Display (time zone/format) settings and site branding, including public branding. |
| `live_views` | Personal and shared saved views and per-user live workspace state. |
| `policies` | Camera groups and membership; group/camera schedules, retention and storage placement. |
| `cameras` | Camera inventory, stream sources and profiles, credential rotation, audio probing, motion sources. Owns `camera_view`, `insert_camera` and discovery probe bookkeeping. |
| `onboarding` | ONVIF discovery and candidates, CSV import, reviewed bulk updates. Builds on `cameras`. |
| `ai_settings` | Per-camera, per-group and runtime person/vehicle detection settings. |
| `live_media` | Authenticated live video/audio delivery and viewer admission. |
| `live_alarms` | Camera neighbours, the feed of alarms that pop into Live, and video wall sign-in. |
| `footage` | Segments, coverage, motion and AI events, archive/restore, footage deletion, preservation holds. |
| `playback` | Synchronized timeline, segment media, snapshots and frame navigation. |
| `export_jobs` | Export job submission, status, cancellation, download and evidence verification. |
| `alerts` | Incidents, webhooks and the external heartbeat. |
| `operations` | Administrator system status and opt-in diagnostics. |
| `web_interface` | The browser interface: the built application, its hashed assets and the `/next/` redirect. Registered after every API router. |

Routers may import public helpers from another router only in one direction (for example `onboarding` → `cameras` → `policies`). They never import `app.main` or `app.application`. Shared constants that several layers need live in small modules (`app.viewing` for live admission limits, `app.version` for the release identifier).

Domain logic that does not depend on HTTP (recording, retention, storage, exports, incidents, webhooks, heartbeat, detection, discovery, scheduling) stays in the existing top-level `app` modules, and routers call into it.

## Authorization

`app.principal` resolves the caller. Every route that needs a signed-in user declares one of two annotated dependencies:

- `CurrentUser`: any signed-in administrator or operator.
- `Administrator`: administrators only (403 otherwise).

Both yield a `Principal` (`id`, `username`, `role`, and for API tokens `scopes`/`token_id`) that also supports `principal["id"]` style access. A bearer API token resolves to its creating administrator, limited by scope: an endpoint marked `@requires_scope("media")` or `@requires_scope("export")` needs that scope, administrator routes need `admin`, other reads need `read`, and other changes need `admin`. The OpenAPI document publishes the result as `x-required-scope`. Unauthenticated routes are deliberate: setup status/bootstrap, sign-in, OIDC, public branding, health, video wall sign-in (`POST /api/wall/session`, which takes a token) and sign-out, push notification thumbnails (behind short-lived signed links), and the browser interface's page and assets (which contain no data). A video wall exchanges a token with exactly the `read` and `media` scopes for an `nvr_wall` session cookie. `current_user` honours that cookie only when no person's session is present, re-checks the token on every request (so revoking it ends the session), and allows only the routes in `app.principal.WALL_ROUTES`, which watching needs. Tests replace identity by overriding `app.principal.current_user` or `app.principal.administrator` in `app.dependency_overrides`.

## Database

- `app.store.connect()` opens the SQLite database named by `NVR_DB_PATH` in autocommit mode, with foreign keys enforced and a 10 second busy timeout. The database runs in WAL mode, set when migrations are applied, so separate processes can read while one writes.
- Keep write transactions short. Use explicit `BEGIN IMMEDIATE … COMMIT` when several statements must apply together.
- Schema changes are made only through versioned migrations in `app.migrations` (see the README's upgrade section). No module creates or alters tables at runtime.
