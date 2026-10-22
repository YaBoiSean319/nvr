# Camera inventory and selected-camera editor

Status: Approved for specification and ticket publication on 2026-10-22. Implementation will occur in a later session.

## Accepted layout

- Camera Settings has Cameras (default), Add Cameras, and Groups & Policies.
- Cameras uses a compact searchable/sortable inventory table and one selected-camera editor in bounded desktop panes; phones use a detail view with Back to Cameras.
- Default columns: Name, IP address, Group, Connection, Recording health, Recording mode. Brand/model are optional; unknown data stays explicit.
- Row selection opens one camera; checkboxes define independent bulk selection. Initial entry opens no camera. Refresh preserves the current target and draft.
- Editor tabs: General & Connection, Recording, Motion, AI, Schedule & Retention, Health & History.
- Each editable tab has Save/Revert. Leaving dirty settings offers Save / Discard / Cancel. Failed or partial saves preserve unsaved edits and explain their outcome.
- Add Cameras has Discover, Manual Add, and CSV Import. Discover uses a results table and one selected-device setup panel, excludes added devices by default, and offers Show added devices.
- All additions require explicit review/test/action. Motion is the default policy and recording starts disabled unless an administrator explicitly enables it through supported prerequisites.
- Bulk tools appear for checked cameras, show total/hidden counts, preview the exact affected set, and report per-camera outcomes with failed-only retry.
- Show Preview explicitly starts one muted camera preview; selecting another camera or leaving Cameras releases it. Browsing inventory never starts every feed.
- A filtered-out selected camera stays open with a notice and Show in table action.

## Details resolved under delegated judgment

- Keep current-session selection/filter/scroll context, but start a new session without automatically opening a camera or preview.
- Preserve role, identity, recording, archive, history, hold, retention, schedule DST, normalized-zone, and evidence semantics.
- Keep one focused group-policy editor. Retain system-wide AI runtime/model controls in the appropriate App Settings area.
- Persist only harmless user presentation preferences; never persist draft configuration or secrets in the browser.
- Use authenticated real-browser/API journeys with deterministic cameras/generated media; add browser coverage to CI rather than treating markup assertions as user-workflow proof.
- No new ADR is needed for these reversible presentation choices. The specification records the behavior contract.

## Sanitized reference

The supplied desktop NVR screenshot is a table-plus-editor workflow reference. Do not copy its workplace camera names, addresses, credentials, footage or proprietary visual identity into repository documentation or fixtures.

## Handoff

Specification: [#67](https://github.com/YaBoiSean319/nvr/issues/67). See the [implementation index](camera-inventory-redesign/README.md) for tickets #68–75 and their dependencies. Earlier parent specifications and deployment acceptance gates remain unchanged.
