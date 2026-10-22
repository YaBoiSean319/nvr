# NVR experience overhaul

Status: visual direction approved; specification and tickets authorized for publication. Implementation is reserved for a new session. Updated October 22, 2026.

## Confirmed decisions

The user accepted questions 1–4 and added: “I want this to be highly polished and more like an app than a webpage.”

1. Scope includes navigation, page layouts, controls, feedback and feature behavior, including confusing steps and incomplete interactions.
2. The main operator workflows are watching live cameras, investigating recorded footage and exporting evidence. Configuration belongs in a dedicated administration area.
3. Previously approved capabilities remain requirements while presentation and interactions may be redesigned. These include synchronized playback, saved camera layouts, gap handling and exports.
4. Use a realistic investigation journey to evaluate the design: find a camera, review an incident, follow it across cameras and export footage. User-identified problem screens can be prioritized.
5. The desired experience is a highly polished application.
6. Use a full-window workspace with a collapsible camera sidebar, compact top toolbar, large video canvas and bottom timeline in Playback. Open secondary tools in panels when needed.
7. Live and Playback each retain cameras, layout and selections. Include an Open in Playback action on live cameras. Camera selection acts within the current workspace.
8. Use neutral dark surfaces, crisp typography, consistent spacing, subtle borders and color primarily for status and important actions. Keep controls comfortable and provide coherent light and system themes.
9. Camera management uses a searchable inventory with one selected camera's settings open at a time. System settings have named categories. Provide visual schedule editing and zone drawing, with progressive disclosure of advanced options.
10. Review realistic Live, Playback and camera-administration mockups before implementation, including populated, loading, missing-footage and camera-failure states.
11. Responsiveness is an explicit product requirement: the user added, “I need this app to be snappy as well.” Target visible UI feedback within 100 ms for clicks, camera filtering, panels and layout changes with 200+ configured cameras, including while background jobs run. Test hardware, load and detailed measurement criteria still need definition.
12. Initial media targets are first live frame within 2 seconds and playback seek within 1 second at the 95th percentile on agreed hardware with healthy streams. These are validation targets, not achieved results. Loading must be visible immediately, controls remain usable and unavailable-camera delays are identified.
13. Clicking a camera in the sidebar toggles membership in the current view, displays a selected checkmark and appends newly added cameras. Clicking a video tile makes it active for controls. Dragging a camera onto an occupied tile explicitly replaces that camera. This supersedes the earlier sidebar-click-to-replace recommendation.
14. Playback has one shared transport bar and a consolidated timeline with aligned camera rows for coverage, motion, gaps and export marks. Include zoom, scrubbing, date/time jumps and previous/next event controls.
15. Marking a range opens a compact export drawer. Exports continue in the background while investigation remains usable. A persistent jobs panel exposes progress, failures with retry and completed downloads.
16. Auto layout reacts immediately to the selected camera count; choosing a grid size is never a prerequisite for viewing video. On a typical widescreen display use columns by rows: 1 camera = 1 by 1; 2 = 2 by 1; 3–4 = 2 by 2; 5–6 = 3 by 2; 7–9 = 3 by 3; 10–12 = 4 by 3; 13–16 = 4 by 4. Adapt to available space, preserve video proportions and keep unused positions unobtrusive.
17. Adding or removing cameras preserves the order and running streams of remaining cameras. Playback preserves timestamp, play/pause state and export marks through rearrangement. Double-click temporarily enlarges a tile; restoring returns to the current Auto arrangement.
18. Auto is the default. Optional manual layouts live in a secondary menu with Return to Auto. Saved views retain camera order and Auto/manual mode.
19. Target up to 16 displayed cameras in both Live and Playback, with matching grid-export support, subject to performance validation. This includes lifting the current eight-camera Playback/export backend limit.
20. Keep compact operational status available throughout the app: camera issues, recording failures, storage warnings and export jobs. Separate live-video health from recording health and make warnings open relevant details.
21. A failed camera keeps its tile. Show a dimmed last frame with explicit status and last-frame timestamp, automatically retry and offer manual retry. Other cameras keep running. Clearly distinguish frozen footage from live video.
22. Live uses substreams in the grid and main streams when enlarged under Auto quality, with Auto/Main/Sub overrides and fallback for cameras without substreams. Recording and export quality remain independent.
23. Tiles normally show camera name and compact status. Reveal consistent enlarge, snapshot, audio, Playback and remove actions on hover, keyboard focus or touch. Audio is muted by default.
24. Provide discoverable shortcuts for transport, seeking, marks, enlargement and closing panels, with a shortcut reference. Do not intercept typing in editable fields.
25. Prioritize polished desktop and tablet use. Smaller screens adapt with collapsible panels and usable controls, without horizontal page scrolling.
26. The user delegated remaining design decisions: “make this whole app professional, snappy, and modern. Keep it as minimal as possible, while maintaining full functionality. I trust your judgement.” The defaults below implement that direction and are explicitly recorded for review.
27. The user approved the visual direction and requested real branding settings for colors, logos and application name. Include a persistent administrator-only Appearance and Branding editor, draft previews, save/reset, light/dark logo variants and safe default fallbacks. Apply published branding to sign-in, application chrome and document title while preserving readable themes and recognizable operational status.

## Remaining decisions resolved under delegated judgment

### Minimal presentation with complete workflows

- Live is the landing workspace. Playback, Cameras and Settings are stable top-level destinations. Retain role enforcement: operators get operational features and administrators get configuration.
- Put cameras, groups and saved views in one contextual browser. Search covers names and available addresses. Selection is visibly distinct from tile-control focus.
- Keep a compact operational summary in the shell. Open details in one contextual drawer at a time. Use brief nonblocking success feedback and persistent actionable failure feedback.
- Empty views invite camera selection. Unknown or loading states remain distinct from failed states. Motion-only recording with no current event is not itself a recording failure.
- Prevent duplicate camera membership in a view. Dragging an already displayed camera onto another tile swaps positions. Removing the active camera focuses a remaining tile. Removing an enlarged camera restores the arrangement.
- Group or view loading applies immediately when within capacity. For more than 16 cameras, offer an explicit subset chooser; never silently truncate or evict existing selections. At capacity, show why another camera cannot be added and how to proceed.
- Preserve saved camera order. Loading a view does not automatically overwrite it. Offer Save and Save as for intentional persistence, respecting current personal/shared permissions.
- Auto remains the default for new views. If an explicit manual grid becomes too small after adding a camera, return to Auto with a brief explanation and preserve all selected cameras.
- Resizing the window changes presentation without changing camera membership. On small screens, use a single-column arrangement and collapse secondary panels. Large desktop viewing remains the primary monitoring surface.

### Administration and feature completeness

- Camera discovery, manual addition, credentials, endpoint selection, recording rules, schedules, motion/AI zones, retention and archive/evidence controls remain discoverable through the inventory/detail workflow.
- Use explicit Save changes per coherent settings section, field-level validation, clear busy states and protection against accidentally leaving unsaved edits. Reserve confirmation for discarding edits and consequential destructive actions.
- Provide explicit bulk selection and a change summary for supported bulk camera edits, with per-camera results and retry for failures.
- Organize system settings into General, Appearance, Users & access, Recording & storage, Notifications, Integrations and Diagnostics. Search/deep links may open the relevant category directly.
- Preserve API, authentication, authorization, recording, retention, audit and export semantics. New visual controls require real backing behavior; unsupported backend capabilities cannot be represented by fake successful saves. Existing documented gaps must be scoped honestly when writing implementation tickets.
- Keep diagnostics sanitization and actionable technical detail available to administrators in contextual details, with concise operator-facing messages.

### Responsiveness and resource use

- Measure input-to-painted-feedback latency; a fast loading indicator does not satisfy first-frame or seek targets. Use the 95th percentile for the 100 ms UI target and the agreed 2-second first-frame and 1-second seek targets.
- Validate against a 200+ configured-camera inventory and 1, 4, 9 and 16 displayed-camera workloads, including active exports. Also validate against the user's actual 88-camera deployment. Record reference workstation, browser, NVR hardware, network, stream profiles and recording media characteristics alongside results.
- Treat media targets as reference-environment goals to validate, not universal guarantees. Report cold and warm results separately and include failures rather than excluding them from success reporting.
- Only selected, visible live feeds consume viewer resources. Preserve workspace metadata when switching modes while releasing inactive viewer resources. Keep remaining viewers mounted during grid rearrangement.
- Search and browse inventory without loading video for every listed camera. Keep expensive media/export work outside the UI thread and bound backend concurrency so exports do not starve live viewing, seeks or recording.
- Coalesce rapid seek input, cancel obsolete requests and prevent old responses from overwriting the newest selection. Give immediate scrub feedback, then show actual decoded footage when available.
- Preserve the shared playback clock during camera membership changes. A newly added camera joins the current timestamp and indicates loading or gaps without resetting other cameras.
- Use bounded reconnect backoff, readable per-camera status and recovery without disrupting the rest of the grid. Avoid duplicate warning storms.

### Accessibility and validation

- Provide visible focus, accessible names, sufficient contrast, keyboard alternatives to drag-and-drop, touch-accessible tile actions and reduced-motion support. Status must not rely on color alone.
- Keep controls in stable locations. Preserve focus through drawer changes and return it to the initiating control when a drawer closes.
- Retain prior evidence requirements: shared-clock playback, actual frame stepping, event navigation, marks, source timestamps, individual-feed gap compression, grid compression only for globally empty intervals, current grid arrangement and one ZIP.
- Validate 0/1/2/3/4/6/9/12/16-camera views, adding/removing the focused camera, offline recovery, missing footage, mode switching, saved-view restoration, export failures and role restrictions.
- Include representative desktop, tablet and phone sizes, both themes, keyboard-only use and reduced-motion preferences. Real camera/media checks and deployment performance remain explicit post-merge checks when local simulation cannot establish them.
- Preserve the original 30-day parallel reliability acceptance gate; visual polish does not establish recording reliability.

## Visual review

An interactive in-conversation design preview covers Live, Playback, camera administration and settings categories. It demonstrates camera add/remove with Auto tiling, focus/restore, independent workspace selections, shared timeline scrubbing, range marks, a contextual export drawer, background job status, a camera failure and organized settings.

The preview uses illustrative sample frames and simulated actions. It is not connected to cameras or the production backend and is not production implementation. Detailed zone drawing, media decoding, real exports and full administration behavior remain implementation work.

Preview verification: JavaScript syntax and static element/selector checks passed. Automated rendered-browser verification could not run because the browser executable was absent and its download did not produce a valid archive. The user approved the visible design; deployment performance validation remains pending and no measured performance result is claimed. Branding was added to the approved implementation scope after the illustrative prototype.

## Source inspection

The current default branch of YaBoiSean319/nvr was inspected on October 22, 2026. These are source findings, not deployed-browser observations or camera tests.

- The console uses a padded, centered main card capped at 78rem.
- Navigation relocates legacy sections into Live, Footage Search, Camera Settings and App Settings containers.
- Live has stacked configuration controls before the video grid, with camera selection duplicated between controls and sidebar.
- Playback places camera/date controls and export forms before the ruler and grid, with additional per-tile controls and event lists.
- Camera Settings renders large forms for all matching cameras. Recording schedules and motion/AI zones still use text/JSON entry.
- App Settings combines many settings sections into one long surface.
- Sidebar selection switches to Live, even when the operator is browsing footage.
- Live supports 16 viewers while Search/export are currently bounded to 8 cameras and 24 hours. How these limits are presented needs consideration.

Sources:

- https://github.com/YaBoiSean319/nvr/blob/main/app/static/index.html
- https://github.com/YaBoiSean319/nvr/blob/main/app/static/app.js
- https://github.com/YaBoiSean319/nvr/issues/33
- https://github.com/YaBoiSean319/nvr/pull/48
- https://github.com/YaBoiSean319/nvr/blob/main/docs/console-acceptance.md

## Decision tree

Scope, workflow priorities, application structure, workspace continuity, visual direction, settings organization and visual validation are confirmed.

Responsiveness targets, shared playback controls and background export behavior are confirmed in principle. The explicit automatic-grid requirement supersedes any need to choose a grid before viewing.

Automatic-grid arrangements, additive camera selection, layout continuity, optional manual overrides and the target displayed-camera capacity are confirmed.

Live controls, failure recovery, persistent operational status, stream quality, accessibility and responsive behavior were accepted. The user delegated remaining choices; the documented defaults resolve the remaining design branches.

The user approved the visual direction and explicitly authorized publishing To Spec and To Tickets, with implementation to follow in a new session. No production code changes are part of this planning session.
