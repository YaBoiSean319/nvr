## Problem Statement

The current NVR console feels like a webpage made from long forms rather than a professional monitoring application. Video competes with stacked setup controls; camera administration repeats large forms for every camera; the sidebar interrupts investigation; and loading, failures, and background work do not form a coherent experience. The user requires a professional, modern, minimal application that retains its full functionality and feels responsive.

The user approved the interactive workspace direction on October 22, 2026 and explicitly requested configurable colors, logos, and application name. This is approved planning scope for a later implementation session.

## Solution

Deliver a full-window application centered on Live viewing, Playback investigation, and evidence export, with separate administrator-only Cameras and Settings destinations. Camera selection immediately displays video and automatically chooses an appropriate arrangement. Use compact persistent navigation and operational status, contextual controls, and drawers rather than permanent stacks of forms.

Provide complete camera and system workflows, visual schedule/zone editing, and persistent site branding. Responsiveness and honest failure/recovery behavior are acceptance requirements, not cosmetic extras. Preserve recording reliability, permissions, retention, evidence protections, and all previously approved media semantics.

## User Stories

1. As an operator, I want to land in Live, so that I can begin monitoring immediately.
2. As an operator, I want video to occupy most of my workspace, so that I can inspect useful detail.
3. As an operator, I want stable navigation between Live and Playback, so that I do not lose my place.
4. As an operator, I want a collapsible camera browser, so that I can choose between browsing and viewing space.
5. As an operator, I want to search camera names and available addresses, so that I can find a camera quickly.
6. As an operator, I want to browse cameras, groups, and saved views together, so that I have one selection model.
7. As an operator, I want one sidebar click to add or remove a camera, so that I can compose a view without a Start action.
8. As an operator, I want selected cameras visibly checked, so that I know what is in the view.
9. As an operator, I want the grid to react to selection count, so that choosing a grid size is never required.
10. As an operator, I want a single selected camera to fill a 1 × 1 view, so that screen space is useful.
11. As an operator, I want existing streams to continue through rearrangement, so that adding a camera does not interrupt the others.
12. As an operator, I want stable camera order, so that I can maintain spatial awareness.
13. As an operator, I want drag-and-drop placement and a keyboard alternative, so that I can arrange feeds precisely.
14. As an operator, I want an active tile distinct from selected-camera membership, so that controls target the intended camera.
15. As an operator, I want to enlarge and restore a camera, so that I can examine detail without losing the arrangement.
16. As an operator, I want optional manual layouts and Return to Auto, so that exceptional arrangements remain possible.
17. As an operator, I want saved views to retain order and Auto/manual mode, so that they restore predictably.
18. As an operator, I want personal views and permitted shared views, so that I can reuse monitoring arrangements.
19. As an operator, I want Live and Playback to remember their own state, so that changing tasks preserves work.
20. As an operator, I want Open in Playback on a live tile, so that I can begin investigating that camera.
21. As an operator, I want automatic grid/focus quality selection with overrides, so that viewing remains efficient and detailed.
22. As an operator, I want truthful loading, stale, offline, and reconnecting states, so that I can trust what is displayed.
23. As an operator, I want a failed camera to keep its labeled last frame and position, so that I can understand a failure without disrupting other cameras.
24. As an operator, I want automatic and manual reconnect, so that temporary faults recover.
25. As an operator, I want compact tile actions for snapshot, audio, enlargement, Playback, and removal, so that features are available without clutter.
26. As an operator, I want audio muted by default and gated by capability and policy, so that listening is intentional.
27. As an operator, I want a shared playback clock and aligned camera timelines, so that I can follow an incident across cameras.
28. As an operator, I want to distinguish coverage, motion, AI markers, and gaps, so that I can navigate evidence accurately.
29. As an operator, I want to scrub, zoom, pan, and jump to a date/time, so that I can locate footage quickly.
30. As an operator, I want shared transport, speeds, real frame stepping, and event navigation, so that I can investigate precisely.
31. As an operator, I want camera additions to join the current playback timestamp, so that investigation does not restart.
32. As an operator, I want up to 16 cameras in Live and Playback, so that the interface and server share a consistent capacity.
33. As an operator, I want clear capacity handling for large groups, so that cameras are never silently omitted.
34. As an operator, I want Mark In/Out and precise range editing, so that I can bound an export.
35. As an operator, I want an export drawer beside my investigation, so that exporting does not take over the workspace.
36. As an operator, I want export jobs to continue in the background, so that I can keep investigating.
37. As an operator, I want progress, cancellation, failure/retry, expiry, and authenticated downloads, so that jobs are manageable.
38. As an operator, I want individual and grid outputs in one ZIP, so that evidence remains together.
39. As an operator, I want existing gap-compression and source-time rules preserved, so that output remains understandable.
40. As an operator, I want persistent recording, camera, storage, and job status, so that I notice operational problems.
41. As an operator, I want live-video health separated from recording-write health, so that a healthy preview does not hide missing recordings.
42. As an operator, I want relevant incident details and acknowledgement actions, so that I can respond without opening administrative settings.
43. As an administrator, I want a searchable inventory with one camera detail editor, so that management scales beyond a few cameras.
44. As an administrator, I want discovery, range scans, separate credentials, profiles, tests, and CSV onboarding preserved, so that camera setup remains complete.
45. As an administrator, I want supported bulk edits with summaries and individual outcomes, so that repeated configuration is efficient.
46. As an administrator, I want visual weekly schedules with inheritance, so that I can manage recording policy without JSON.
47. As an administrator, I want visual motion and AI zone editing, so that areas are understandable.
48. As an administrator, I want organized settings and explicit saves, so that changes are deliberate and verifiable.
49. As an administrator, I want app-name, color, and logo settings with a preview, so that the installation matches its organization.
50. As an administrator, I want branding to persist across restarts and apply to sign-in and the shell, so that users see a coherent product.
51. As an administrator, I want light/dark logo variants, fallbacks, and reset controls, so that branding remains readable and recoverable.
52. As an operator, I want branding changes to preserve readable controls and recognizable warning states, so that customization does not reduce usability.
53. As an administrator, I want role restrictions enforced by the server, so that hidden controls are not the security boundary.
54. As a keyboard or touch user, I want complete access to controls, so that essential actions do not depend on hover or dragging.
55. As an operator, I want useful desktop, tablet, and smaller-screen layouts, so that controls remain readable and reachable.
56. As an operator, I want clicks and filtering to react promptly even during exports, so that the application feels responsive.
57. As an administrator, I want documented workload measurements and deployment checks, so that responsiveness and reliability claims have evidence.
58. As an administrator, I want upgrade compatibility and original 30-day acceptance retained, so that a UI overhaul does not jeopardize recording.

## Implementation Decisions

### Authority, scope, and architecture

- Extend the existing authenticated FastAPI/SQLite NVR and its live, indexed-media, playback, export, settings, incident, and audit services. Preserve working recording and identity paths. Prefer a thin modular application shell with explicit workspace state over relocating legacy forms or a wholesale backend rewrite.
- Preserve public operational API compatibility where feasible. Migrate saved views and settings without discarding identities, camera order, recording policy, stored footage, holds, or evidence metadata. Native module/framework decisions belong to implementation and must serve this contract; a framework migration is not itself a deliverable.
- The glossary terms are Live workspace, Playback workspace, selected camera, active tile, Auto layout, saved view, and site branding.
- This specification extends production spec #1 and console spec #33. It supersedes #33 only where explicitly changed here: sidebar single-click membership, automatic selection-driven layout, immediate contextual camera application, the approved application structure, and 16-camera Playback/export capacity. All other media, permissions, time, archival, and evidence contracts remain.
- User approval of this plan is authorization to publish specifications and tickets. Production implementation, merge, deployment, and changes to the running NVR are reserved for a later session. This planning-session restriction does not prohibit implementation when that later session authorizes it.

### Application shell and minimal presentation

- Full-window layout: compact top navigation, collapsible/resizable contextual camera browser, dominant video canvas, Playback timeline below it, and compact persistent operational status. Destinations are Live, Playback, Cameras, and Settings. Live is the landing destination; Cameras/Settings are administrator-only.
- Use restrained neutral surfaces, crisp typography, consistent spacing, subtle borders, one restrained brand accent, and status colors with text/icons. Support dark, light, and system appearance. Keep comfortable controls, predictable placement, and purposeful density; do not replace functionality with oversized filler cards or repeated explanatory prose.
- Secondary workflows open in contextual panels/drawers; preserve the underlying workspace and return focus on close. Show concise success feedback and persistent actionable failures. Protect unsaved configuration edits with Save/Discard/Stay; routine browsing needs no confirmation.
- Settings categories: General; Appearance and Branding; Users & access including existing SSO status/setup access; Recording & storage; Notifications; Integrations; Diagnostics. Group existing functions coherently rather than duplicating controls. Environment-configured SSO remains honestly described; do not invent in-app secret editing or backup/restore capabilities.
- All existing supported recording, storage, retention, archive, preservation, AI, audio, webhook, heartbeat, session, API/automation, and diagnostics workflows remain reachable. No placeholder successful saves.

### Camera browser, selection, and layout

- Search available camera name/address and group/view names; respect credential-safe inventory responses. Cameras remain alphabetized for browsing. Show selected membership checks, health, and personal/shared view scope. Selection acts in the current Live/Playback workspace and never unexpectedly switches to Live.
- One sidebar camera click toggles membership. Newly added cameras append and load automatically; no Start or grid-size prerequisite. Tile click makes it active for controls without removing it. Camera inclusion and control focus are separate states.
- Auto arrangement on a typical widescreen display:

| Selected cameras | Columns × rows |
| --- | --- |
| 1 | 1 × 1 |
| 2 | 2 × 1 |
| 3–4 | 2 × 2 |
| 5–6 | 3 × 2 |
| 7–9 | 3 × 3 |
| 10–12 | 4 × 3 |
| 13–16 | 4 × 4 |

- Adapt geometry to available space without changing membership or stretching video. Zero selections shows a clear invitation to choose cameras. Unused slots are unobtrusive.
- Keep existing viewers mounted through grid rearrangement. Preserve order; removing a camera compacts remaining membership without reconnecting unaffected streams. This is the explicit Auto-mode exception to old fixed-position expectations.
- Drag a new camera onto an occupied tile to replace it. Drag an already displayed camera onto another tile to swap positions, never duplicate membership. Provide keyboard/touch alternatives.
- Double-click a tile or use Enlarge to temporarily focus it. Restore/Escape returns to its underlying Auto/manual arrangement. Removing the enlarged camera restores the remaining arrangement; removing the active tile chooses a remaining tile.
- Optional manual layouts live in a secondary menu with Return to Auto. If additions exceed manual capacity, return to Auto, preserve selections, and explain briefly. Do not silently drop feeds.
- Group/view loading replaces current membership immediately, deduplicated and ordered. If more than 16 cameras would be selected, show a subset chooser before applying; never truncate invisibly. At capacity, explain the limit and offer a useful next action.
- Personal views remain owner-managed; administrators manage shared views/groups. Retain server-side scope/ownership checks and existing single policy-source semantics for multi-group browsing. Views save camera identities, order, and Auto/manual mode, not historical dates. Loading does not overwrite a saved view; use explicit Save/Save as and show dirty state.
- Preserve per-user Live and Playback state independently, including sidebar state and current investigation. Persist refresh-restorable state without credentials; authentication and new login never restore audible audio.

### Live viewing, controls, and resource lifecycle

- Support up to 16 admitted video viewers with truthful capacity/failure feedback. Honor server admission limits and release removed/inactive viewer resources; do not multiply camera connections while rearranging.
- Auto quality prefers compatible substreams in a grid and main streams when focused; permit Auto/Main/Sub overrides. Detect missing/unsupported profiles and clearly fall back. Changing viewing quality does not alter recording or export source quality.
- Tile chrome normally contains camera name and compact status; reveal enlarge, snapshot, audio, Playback, and remove actions on hover, keyboard focus, or touch. Essential actions remain accessible without hover. Native per-video transport bars must not compete with shared Playback controls.
- Explicit loading/reconnecting/stale/offline states. Failed cameras retain their tile and a dimmed last frame with status and capture/last-received time; use a neutral dark fallback when no frame exists. Never imply frozen imagery is current. Other feeds continue; reconnect uses bounded backoff plus manual retry.
- Live audio defaults off, remains capability/administrator-policy gated, and permits only one audible camera. Hide or explain unavailable actions honestly. Snapshot behavior preserves displayed/source-time and audit semantics.
- Open in Playback switches deliberately, includes the chosen camera, and preserves an existing investigation's range/time. Starting a new investigation uses the previously approved past-hour range in the site timezone, initially paused at its first available recording. Missing footage is explicit. A full workspace uses an explicit replace/subset choice rather than silent eviction.
- Stop inactive viewing/decoding when switching workspaces while retaining metadata for restoration. Temporary enlargement may suspend hidden grid decoding while preserving membership and resume only once on restore; ordinary add/remove/reflow must not restart unaffected viewers.

### Playback and investigation

- Consolidated shared ruler/clock with aligned rows for selected cameras, recording coverage, motion/AI markers, and missing/failure/uncertain intervals. A motion-only idle interval is not automatically a write failure.
- Apply selected cameras at the current range/time automatically; no separate camera-apply step. Adding a camera joins the existing clock, timestamp, pause/play state, and export marks. Changing date/range runs a validated bounded query and clearly indicates pending/stale results.
- Keep the existing 24-hour query bound unless a separately justified change is required. Admit up to 16 cameras through actual timeline/media/API/backend validation, not merely larger UI choices. Bound metadata pagination/windowing, decode fan-out, concurrent requests, and media leases.
- Shared play/pause, 0.25×/0.5×/1×/2×/4×/8×/16× speed, scrub/seek, zoom, pan, Fit Range, date/time jump, actual frame stepping, event navigation, and optional Skip Empty Periods retain #33 semantics.
- Frame stepping pauses and follows actual neighboring source-frame timestamps from the active camera; do not assume fixed FPS. Event navigation retains simultaneous-event, scope, no-wrap, and pause/play rules. Snapshots never represent gap placeholders as current footage.
- Coalesce rapid seeks, cancel obsolete work, and discard stale responses. Scrubbing gives immediate visual feedback, but actual decoded-frame completion is separately measured.
- Gaps retain shared wall-clock time by default, showing last preceding image with explicit gap/capture labels or a dark initial tile. Skip Empty Periods removes only globally uncovered intervals. Preserve common time throughout populated intervals.

### Exports, jobs, and evidence

- Compact Mark In/Out controls and highlighted timeline range; precise edit/clear in the export drawer. Default to full applied search range and all selected cameras. A single mark uses the matching search boundary for its missing counterpart. Validate ordered, nonempty, in-range intervals.
- Marking a complete range opens the drawer without taking over the workspace. Export creation supports up to 16 selected cameras, individual files plus grid by default, either output alone, camera subset, 1080p default/4K grid, optional permitted audio, and one ZIP.
- Preserve the underlying Auto/manual grid order/positions captured at submission, not temporary enlargement or later view edits. Snapshot display format/timezone and all rendering choices with the job.
- Individual files concatenate each camera's chronological available recordings and omit its gaps; omit/report cameras with zero footage. Grid keeps the union of selected-camera covered intervals and compresses only globally empty intervals. Preserve selected no-footage tiles; freeze last preceding image with centered No video available and capture time, dark before first footage, plus advancing source clock.
- Keep camera/source-time overlays, skipped-gap indicators, source/output interval mapping, manifests, checksums, audit references, omission reporting, audio gating, grid-source silence, and existing integrity-verifier behavior. Branding does not add a footage watermark or alter evidence content.
- Jobs survive navigation/browser closure; expose durable queued/running/completed/failed/cancelled states, progress, cancellation, authorized downloads, expiry, truthful restart recovery, and retry. Retry creates an auditable new attempt with the immutable prior request and revalidates media availability.
- Raising camera capacity must preserve bounded workers, queue, source bytes/segments, range, output size, free-space reserve, retention leases, and terminal cleanup. Do not raise unrelated current limits merely to admit 16 cameras. Show admission failures before implying success.

### Persistent operational status

- Compact shell summary and contextual details distinguish live-video freshness, recording-write health, expected motion idle, storage pressure, incidents, and jobs.
- Operators receive only authorized operational summaries/actions; administrator-only system/session/diagnostic information remains restricted. Reuse existing incident grouping, acknowledgement, recovery, webhook, and heartbeat semantics. Live health alone is never recording proof.
- Aggregate repeated warnings without hiding unresolved issues. Status must stay usable in every workspace, including Playback and administration.

### Camera inventory and visual editors

- Searchable inventory with one detail editor, archived/history filtering, and progressive advanced settings. Preserve separate address/username/password, brands/advertised endpoints/profiles, stream tests, discovery/private-range scans, CSV preview/apply, recording/audio/AI controls, storage/group policy, overrides, credentials, and archive/history/hold protections.
- Bulk actions have explicit selection and summary, bounded execution, per-camera success/failure, and selective retry. Preserve stable camera identities, effective policy source, and old media associations.
- Weekly schedule editor uses day/time bands and motion/continuous/disabled blocks. Preserve timezone, overnight-start-day, overlap rejection, boundary, DST, group inheritance, and override-removal semantics. Operators may inspect effective policy but cannot change it. Do not require routine JSON editing.
- Motion/AI editors draw, resize, move, and remove normalized rectangular zones over a representative frame; provide coordinate inputs/keyboard alternatives. Keep motion and AI scopes/sensitivity/enablement distinct. Handle absent frames and stream dimension changes without silently moving zones.
- Explicit validated saves; test/persistence/restart feedback must reflect actual results. Archival/deletion safeguards and authenticated history access remain unchanged.

### Site branding — required new end-to-end capability

- Administrator-controlled site-wide branding in Appearance and Branding: application name, restrained primary/accent colors, primary logo, optional light/dark logo variants, and compact app icon. Provide preview in dark/light contexts, explicit Save, remove/reset per asset, and Reset to defaults.
- Application name is trimmed plain text, 1–80 characters; render it as text, not HTML. Apply to sign-in/first-run shell, app header, and browser document title. The compact icon supplies the favicon where supported; fall back to the default identity when absent.
- Support uploaded PNG, WebP, and JPEG logos/icons, including transparency where the format supports it. Bound each upload to 2 MiB and decoded dimensions to 4096 × 4096; validate actual decoded type/dimensions, reject corrupt/unsupported content and oversized/decompression-abusive inputs, and return field-level errors. Arbitrary remote logo URLs, raw HTML/CSS, and executable SVG are not accepted.
- Persist settings and uploaded assets on existing durable application storage with migration/defaults and no mandatory new Compose mount. Store assets under server-managed identities, serve with correct type and cache/version behavior, and never interpret user paths.
- Expose only the minimal public branding required before sign-in; it must not expose cameras, tenant details, secrets, private settings, or sessions. Mutations/reset are administrator-only, audited, validated atomically, and rejected for operators/anonymous users.
- Unsaved previews affect only the editor's draft. Failed saves preserve prior published branding. After successful save, branding applies to all users on refresh/next settings refresh and survives restart; a missing/failed asset falls back without breaking sign-in or layout.
- Derive legible theme tokens and validate contrast, including text and focus indicators. Preserve error/warning/success meanings and text/icon cues; brand colors must not hide recording failures. Logos fit bounded containers with preserved aspect ratio.
- Branding does not change camera names, security roles, OIDC behavior, evidence overlays, or existing completed exports. Theme preference remains a user appearance choice over site branding.

### Performance and accessibility contracts

- Target p95 input-to-painted-feedback within 100 ms for clicks, camera filtering, panel opening, and layout changes with at least 200 configured cameras and while background exports run.
- On declared reference hardware/network with healthy supported streams, target p95 first decoded fresh live frame within 2 seconds and committed seek to actual decoded requested footage within 1 second. A spinner is not a frame/seek success.
- Document cold/warm conditions, browser/workstation, host CPU/RAM/storage, stream codec/profile/bitrate, network, workload, sample counts, errors/timeouts, and observed p50/p95. Healthy-stream success latency and all-attempt failure rate must both be reported. Do not claim blanket 200-camera production certification from a synthetic inventory.
- Workloads include 1/4/9/16 displayed cameras, at least 200 inventory entries, and representative CPU-only exports. Keep recording priority, bounded admission, and asynchronous UI/media work. Inventory browsing must not start feeds for all configured cameras.
- Keyboard access, visible focus, screen-reader labels/status, touch alternatives, sufficient contrast, reduced motion, and shortcuts/reference are required. Suppress shortcuts in editable controls. Check viewport sizes including 320/360/768/1024/1440 widths; allow local timeline panning while preventing horizontal page overflow.
- Desktop/tablet receive the complete monitoring experience; smaller displays use collapsible panels/single-column feeds and reachable controls without losing functionality.

## Testing Decisions

Use the previously approved authenticated API/browser system boundary from #1/#33, with deterministic camera fixtures and generated playable media, then inspect visible behavior, persisted state, real decoded frames, and rendered exports. The user delegated remaining design/testing details; no additional interview is needed. Prefer this single external boundary and extend existing fixtures rather than testing private helpers or recreating implementation logic.

Existing prior art includes FastAPI TestClient administrator/operator checks, isolated SQLite/recording stores, generated FFmpeg footage and FFprobe inspection, coverage/hold fixtures, display-format/DST validation, saved-view ownership, schedule inheritance, audio gates, and export integrity/recovery tests. Current console navigation checks largely assert markup; replace brittle selector/old-grid expectations with real browser journeys rather than counting those strings as workflow proof.

Required automated/CI checks:
- Login-to-Live, Auto layout at 0/1/2/3/4/6/9/12/16 cameras, toggle/reorder/replace/swap, focus/restore, resource release, group overflow, and separate workspace restoration.
- Playback timeline, rapid-seek stale response races, shared-clock continuity through membership changes, actual source-frame/event navigation, gaps, marks, snapshots, and authenticated media/leases.
- 16-camera API admission and real generated media, overflow rejection, missing-camera/partial failure behavior, and decoded 16-camera grid/individual ZIP output under existing resource caps.
- Persistent camera/settings behavior, all supported inventory/onboarding workflows, bulk partial failure, weekly overnight/DST/inheritance semantics, and normalized visual-zone round trips.
- Branding admin/public/operator boundaries, valid and malicious uploads, corrupted/oversized/type-spoofed assets, plain-text name handling, draft/failed-save isolation, cache invalidation, reset/fallback, restart persistence, and both-theme/browser title/sign-in rendering.
- Full keyboard/touch role journeys, responsive layouts, themes, contrast/focus, error/loading/recovery, background jobs, and no lost controls in enlarged mode.
- Existing recording, retention, holds, archival/deletion, identity/SSO, evidence, audio, diagnostics sanitization, webhook, and heartbeat regressions; preserve supported API behavior.
- Targeted repository checks plus syntax/diff checks and browser tests in CI. Follow the current repository warning about any test that attempts an unwanted Analytics network request; do not label unexecuted suites passed.

Deployment-only checks must be explicitly marked **Post-merge testing required**, with version, environment, workload, steps, and result owner/evidence fields. They include real browser/camera codec/profile compatibility, target identity provider, persisted-volume upgrade/recovery, real deployment responsiveness/resource contention, camera failure recovery, actual audio, 1080p/4K outputs, and the original 30-day parallel reliability gate. Keep implementation-complete and deployment-validated status distinct; never check off an unrun deployment test or close the overall qualification gate on CI success alone.

## Out of Scope

- Implementing, merging, deploying, or changing the running installation during this planning session. A later authorized implementation session will execute the tickets.
- More than 16 displayed cameras, unlimited export workloads, mandatory GPU, recording-policy redesign, a new backend stack, or unrelated new PTZ/AI features.
- New in-app backup/restore or OIDC secret-management workflows merely because settings have categories.
- Remote branding asset URLs, arbitrary theme CSS/HTML, executable SVG, multi-tenant branding, or branding watermarks on footage.
- Removing existing functionality, weakening permission/media/retention safeguards, modifying or closing earlier parent issues, or claiming unmeasured performance/reliability.

## Further Notes

- This user-approved design and the final branding request are authoritative when they differ from illustrative prototype behavior. The prototype uses sample frames/simulated actions and is a visual reference, not reusable production media or proof of feature completion.
- Tracker: YaBoiSean319/nvr GitHub Issues. Triage: ready-for-agent. Each implementation slice links this specification and its blockers; the companion ticket index provides the execution frontier.
- Preserve the approved design reference and glossary in a documentation-only planning branch so a fresh implementation session does not need this conversation. Treat the prototype as reference material; build real API-backed behavior.
- No customer camera addresses, credentials, footage, tenant identifiers, or workplace-specific names belong in the published planning material.
