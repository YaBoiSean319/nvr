## Problem Statement

Camera Settings is a long page combining discovery, onboarding, group policies, camera summaries, and repeated settings. Administrators cannot quickly compare added cameras or reliably focus on one camera without scrolling through unrelated forms. Discovery results and cameras already added to the installation need distinct spaces.

## Solution

Provide a compact camera administration workspace inspired by the supplied desktop NVR reference: a searchable inventory table and exactly one selected-camera editor. Separate Cameras, Add Cameras, and Groups & Policies. Use bounded panes, focused editor tabs, explicit saves, and useful small-screen navigation while preserving the application's supported configuration and investigation behavior.

The user approved the recommendations in both design rounds and delegated remaining design and testing details. This specification is ready for implementation in a later session; publishing it does not authorize implementation or deployment in this planning session.

## User Stories

1. As an administrator, I want Cameras to be the default workspace, so that I immediately see cameras already added.
2. As an administrator, I want Add Cameras separate from the inventory, so that scans and onboarding do not clutter ongoing administration.
3. As an administrator, I want Groups & Policies separate from individual cameras, so that shared policies have a clear home.
4. As a user, I want a compact table, so that I can compare many cameras without a page of camera cards.
5. As a user, I want name/IP search, group/status filters, and sorting, so that I can find a camera quickly.
6. As a user, I want connection and recording health shown separately, so that healthy live video does not imply footage is being written.
7. As a user, I want optional brand/model columns and honest unknown values, so that available device metadata is useful without fabricated details.
8. As a user, I want no camera automatically opened on initial entry, so that I choose what to inspect.
9. As a user, I want a row click to select a camera, so that its details appear immediately.
10. As an administrator, I want bulk checkboxes independent of row selection, so that choosing cameras for a batch does not change my editor.
11. As a user, I want only one camera editor, so that unrelated settings never accumulate down the page.
12. As an administrator, I want focused settings tabs, so that I see only controls relevant to my current task.
13. As an administrator, I want explicit Save and Revert controls, so that edits do not silently change camera configuration.
14. As an administrator, I want Save / Discard / Cancel before leaving dirty settings, so that navigation does not lose my work.
15. As an administrator, I want validation and save failures to preserve edits, so that I can correct a problem without re-entering everything.
16. As an administrator, I want status refreshes to preserve my editor and draft, so that live health updates do not interrupt editing.
17. As a user, I want a filtered-out selected camera to remain open with a clear notice, so that filtering does not switch my editing target.
18. As a phone user, I want a camera detail view with Back to Cameras, so that settings remain usable on a narrow display.
19. As a keyboard user, I want accessible table selection, tabs, dialogs, and pane controls, so that I can complete the workflow without a mouse.
20. As an administrator, I want discovery to default to devices not yet added, so that I can concentrate on onboarding.
21. As an administrator, I want an optional Show added devices filter with explicit linkage, so that I can inspect rediscovered devices without creating duplicates.
22. As an administrator, I want the existing VLAN and reachable private-range scan methods, so that the new layout preserves working discovery.
23. As an administrator, I want one discovered-device setup panel, so that discovery does not create another long collection of forms.
24. As an administrator, I want to link a discovered device to an existing camera when appropriate, so that existing identity and history remain intact.
25. As an administrator, I want separate address, username, password, brand, and endpoint/profile controls for manual onboarding, so that supported camera connection workflows remain available.
26. As an administrator, I want CSV preview and individual import results, so that I can review accepted, duplicate, invalid, and failed rows.
27. As an administrator, I want explicit tested addition with safe defaults, so that discovery alone never starts recording.
28. As an administrator, I want the exact bulk selection count and scope, so that hidden selections cannot surprise me.
29. As an administrator, I want to preview affected cameras and changes before applying a batch, so that I can verify its impact.
30. As an administrator, I want individual batch outcomes and selective retry, so that a partial failure does not repeat completed work.
31. As an administrator, I want effective group policies and camera overrides distinguished, so that policy inheritance remains understandable.
32. As a user, I want an optional muted preview for the selected camera, so that I can inspect its view while configuring it.
33. As a user, I want preview media released when I change cameras or leave Cameras, so that administration does not leave streams running.
34. As a user, I want explicit archived-camera filtering and historical access, so that simplifying the inventory does not lose history or safeguards.
35. As an operator, I want authorized read-only camera and effective-policy information, so that administration layout does not grant configuration rights.
36. As a user, I want useful table preferences retained without storing secrets or camera drafts, so that returning to the workspace is convenient and safe.
37. As a user, I want clear empty, loading, unavailable, and recovery states, so that I know whether data is absent or a request failed.
38. As a user, I want usable light/dark/system/branded layouts and touch controls, so that the screen fits the rest of the app.
39. As an acceptance owner, I want real observable browser tests, so that a correct-looking markup string is not mistaken for a working screen.
40. As an acceptance owner, I want inventory browsing to avoid starting media for the entire inventory, so that administration preserves recording priority.

## Implementation Decisions

### Workspace structure and module boundaries

- Camera Settings contains Cameras (default), Add Cameras, and Groups & Policies. Operators see only authorized read-only inventory/details; onboarding and policy mutations remain administrator-only at UI and API boundaries.
- Add Cameras has Discover, Manual Add, and CSV Import modes. Discovery, import results, manual forms, group policy editors, and system AI runtime controls must not be stacked above the inventory.
- Keep inventory state, selected-camera editor, onboarding, batch actions, policy editing, and preview lifecycle as distinct responsibilities. Prefactor within the first behavior slice without a speculative backend rewrite or temporary loss of working controls.
- Existing system-wide AI runtime/model controls belong in the existing appropriate App Settings area rather than being copied into every camera or group editor. Retain their permissions and functionality.

### Inventory table and selection

- Default columns: Name, IP address, Group, Connection, Recording health, Recording mode. Brand/model are optional. Use existing metadata where available; display Unknown/Not reported when absent. Do not infer freshness or write health from reachability.
- Default order is camera name ascending with deterministic ties. Search matches name and IP; provide group, connection, recording-health, and active/archived filters. Label policy-group meaning and retain the distinction from overlapping browsing groups.
- Table header/toolbar stay reachable; the table body and editor scroll within bounded panes. The document must not grow by appending one settings form per camera.
- Initial load or new authenticated session has no selected camera and shows Select a camera to view settings. Within the current session, refresh and workspace return can retain a still-valid selection; no initial automatic first-row selection or restored media.
- Row selection is separate from bulk selection. Checkbox/row action activation must not unintentionally open another editor. Keyboard users can reach the same actions without relying on hover, color, double-click, or dragging.
- A selected camera filtered out of the table remains open with a Selected camera is outside the current filter notice. Show in table clears only the filters necessary to reveal it. No automatic switch to a different row.
- Refresh updates visible operational data without replacing the editor/draft. A camera removed or no longer authorized becomes a clear unavailable state; stop preview, disable mutations, and retain only permitted recovery actions.

### Selected-camera editor

- Exactly one settings panel is rendered for the selected camera. Tabs: General & Connection; Recording; Motion; AI; Schedule & Retention; Health & History.
- Preserve separate name/address/credential controls, brands, advertised ONVIF endpoints/profiles, stream tests, main/substream selection, audio/probe, recording enablement/modes, motion source/fallback/pre/post-roll/zones/sensitivity, AI settings/coverage/zones, schedule timezone/inheritance/override removal, retention, storage/policy source, credentials rotation, archive/restore, and protected historical footage actions.
- Place group/storage assignment and effective policy source in the relevant selected-camera tab. Health & History offers existing recording/coverage/error information and history/playback navigation; do not duplicate the full Playback workspace inside every camera editor.
- Provide a persistent camera identity header and tab-scoped save feedback. Saving camera connection fields must not claim recording, motion, AI, or schedules were saved.
- Keep destructive archive/deletion/hold behavior explicit and separate from ordinary saves. Preserve current API protection for historical media, holds, export/playback leases, permission boundaries, and stable camera IDs.

### Save and draft contract

- Each editable tab provides Save and Revert, disabled appropriately when unchanged or saving. No autosave of configuration.
- Leaving a dirty tab, selecting another camera, changing workspace, or performing an action that would overwrite the draft opens Save / Discard / Cancel. Cancel stays on the same target with edits intact. Save navigates only after the save actually succeeds; validation/network/authorization failure keeps the draft and target visible.
- Revert discards that tab's edits and reloads the current published values. Canceling a discard must not clear dirty markers. Native browser close/reload protection applies when a draft exists.
- Preserve distinct existing saves where a tab spans several configuration resources. If an endpoint sequence partially succeeds, identify the saved and unsaved portions, retain the unsaved values, and do not imply an atomic save or navigate away as though all succeeded.
- During a save, prevent a second submission and prevent newer edits from being cleared by an older response. Ignore stale responses for earlier camera/scan/filter requests.
- Drafts, usernames/passwords, and connection secrets are not placed in URLs, browser persistence, diagnostics, or public handoff materials.

### Add Cameras

- Discover uses a bounded, searchable results table and one selected-device setup panel. Keep scan controls, progress/error/retry, and last-scan context compact.
- Default results exclude devices already linked to inventory. Show added devices exposes them with an Already added/link indicator and Open existing camera action; disable adding them again. Preserve existing stable-identity and duplicate validation on the server, including concurrent add attempts and linking discovered devices to existing inventory.
- Preserve administrator-initiated multicast/VLAN and reachable private IPv4 range scans, current network/time/concurrency bounds, advertised profile loading, separate credential fields, endpoint overrides, stream testing, and clear unsupported/unavailable states. A missing advertised identity is not proof a device is new.
- Manual Add is a focused form using the same tested connection rules. CSV Import retains preview-before-apply and row-level results; keep results in a bounded table with a selected-row error/detail panel where needed.
- Default new cameras to motion policy with recording disabled. An administrator may explicitly enable recording after testing and reviewing the existing prerequisites. Scanning, selecting a device, testing a stream, or importing a preview never implicitly enables recording.
- Failed tests/adds preserve safe draft input for correction; successful adds appear in inventory and cease being unadded discovery candidates. Do not persist or echo saved secrets.

### Bulk changes and Groups & Policies

- Show the bulk toolbar only when checkboxes are selected. Display the total selected count and hidden-by-filter count, Clear selection, and an explicitly counted Select all matching cameras action.
- Before apply, preview the exact camera identities, changes, inherited-policy consequences supported by the current API, and any exclusions. Apply only that reviewed set, not a newly recomputed filter result. Revalidate missing/archived/no-longer-authorized cameras.
- Preserve supported group assignment, recording mode, and recording enablement actions without adding new destructive bulk operations. Bound batches and execution so recording priority remains intact.
- Report success/failure per camera. A transport failure with unknown outcome must be reconciled before retry; do not assume the entire request failed. Existing batch APIs must not be relabeled as providing per-camera results unless that behavior is actually implemented.
- Explicit Retry failed revalidates and resubmits only eligible failed cameras, excluding prior successes. Canceling or closing a dialog cannot falsely report a running operation as canceled.
- Groups & Policies uses a group list and one group editor with focused sections/tabs rather than a stack of every policy form. Preserve browsing membership versus policy inheritance, group name/membership, weekly schedules, retention, storage assignment, AI settings, effective-policy source, override semantics, and role boundaries.
- Preserve schedule timezone, overnight-start-day semantics, overlap validation, DST behavior, normalized motion/AI zone coordinates, and visual/keyboard editing. Changing layout must not modify configured policies.

### Preview and presentation

- Show Preview explicitly starts one muted selected-camera preview. Selecting rows, searching, filtering, opening editor tabs, or polling health does not start all inventory streams.
- Use authenticated existing live delivery and quality/status behavior. Preview visibly distinguishes loading, fresh, reconnecting, stale/lost, unsupported, and failed states; a connection test is not proof of recording.
- Stop and release the preview and its requests/media resources when the selected camera changes, the user leaves Cameras, the camera becomes unavailable/archived, the session ends, or preview is closed. Never autostart it on session restoration.
- Desktop/tablet use adjacent panes with an accessible resizer and bounded sizes. Phones use a table/list adapted to essential columns, selected-camera drill-in, and Back to Cameras restoring search/scroll/selection. Long fields wrap or scroll locally without horizontal document overflow.
- Keep toolbar, tab navigation, camera identity, and save/status actions reachable. Use sufficient touch targets, visible focus, screen-reader labels/status, non-color state cues, reduced motion, and established theme/branding tokens.
- Persist only harmless per-user table preferences such as sort, column visibility, and pane width. Filter/bulk selection/selected camera can remain in session memory; clear user-specific state on logout. Do not persist unsaved configuration or preview playback.
- Keep the existing responsiveness target for 200 configured inventory rows, with no per-row media session or background capability test. Measure representative input/paint behavior and request/media counts rather than claiming 200-camera recording capacity.

## Testing Decisions

- Use one primary product boundary: authenticated browser journeys backed by real application APIs, isolated stores, deterministic camera fixtures, and generated playable media. The user delegated remaining testing choices; this extends the approved browser/API boundary of the earlier workspace specification.
- Observe visible selected-camera details, changed persisted settings, request outcomes, authorized data, actual preview lifecycle, and decoded media when available. Do not use markup text counts or private-helper tests as proof of a working user journey.
- Prior art includes camera inventory/profile/discovery-range tests, administrator/operator API checks, group/schedule/storage/retention fixtures, camera archive and hold/lease safeguards, live-view media fixtures, and the console's existing browser JavaScript checks.
- Required automated behavior: 0/1/200-camera inventory, no initial auto-selection, row versus bulk selection, filters/sort/unknown values, one editor and one active settings tab, filtered-out selection, dirty navigation/save/discard/cancel/revert, failed and partial saves, and refresh/race handling.
- Required onboarding/batch behavior: unadded versus linked discovery results, scan failure/retry, stable-identity/concurrent duplicate rejection, link-existing history preservation, manual connection/profile tests, CSV preview/apply/partial results, fixed reviewed bulk scope, uncertain-outcome reconciliation, and failed-only retry.
- Required preview behavior: explicit start only, at most one active camera preview, authenticated media, visible media failure/recovery, no inventory-wide stream creation, and teardown after rapid camera/workspace/session changes.
- Run administrator and operator keyboard/touch journeys at 320/360/768/1024/1440 widths and light/dark/system/branded themes. Verify focus restoration, accessible tab and table actions, no document overflow, readable status, and all retained controls.
- Add a real browser test runner/fixture to CI if one is absent. The core layout/navigation/draft workflows must have executable browser coverage; do not defer all UI behavior because existing navigation tests assert markup.
- Run appropriate existing API/media/recording/identity/evidence regressions and repository checks. Follow the repository warning about the known unwanted Analytics request; report actual executed suites and any limits truthfully.
- Deployment-only checks are explicitly unchecked Post-merge testing required entries with application version, browser/OS, camera/network/host context, workload, steps, owner, result, and evidence fields. Include real devices/profiles/codec/audio, target identity, persisted configuration upgrade/restart, and recording health during administration/preview/bulk changes. Do not claim production capacity, latency, or the 30-day gate from fixture tests.

## Out of Scope

- Application implementation, implementation PR creation, merge, deployment, or changes to a running installation in this planning session.
- Replacing recording/media/identity backends, changing roles, redesigning Live/Playback, removing existing supported controls, or relaxing archive/hold/retention/export protections.
- Automatically adding discovered devices, automatically enabling recording, loading previews for every inventory row, bulk destructive footage deletion, arbitrary new vendor integrations, or unsupported metadata claims.
- Copying the reference application's branding, proprietary implementation, real workplace identifiers, camera addresses, credentials, or footage into published materials.
- Closing or modifying earlier parent specifications or claiming their deployment qualification is complete.

## Further Notes

- Repository: YaBoiSean319/nvr. Tracker: GitHub Issues. Triage label: ready-for-agent.
- Starting point: current main contains merged PR #66. The observed planning baseline is 6069e037ad7290ee02ea949878e7b4f9057dc15c; use the latest main and reconcile later changes before implementation.
- This is a follow-up refinement of the earlier workspace/inventory work, not a replacement for the wider product specification or its pending deployment acceptance.
- Documentation handoff: https://github.com/YaBoiSean319/nvr/tree/docs/nvr-camera-inventory-redesign
- The companion ticket index gives the implementation frontier and blockers. Earlier specification and acceptance issues remain unchanged.
