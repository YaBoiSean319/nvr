# 01: [Camera Admin 1] Separate workspaces and deliver the compact camera inventory

Status: ready-for-agent

Issue: https://github.com/YaBoiSean319/nvr/issues/68

## Parent

https://github.com/YaBoiSean319/nvr/issues/67

## What to build

Open Camera Settings into Cameras, with a compact inventory table and exactly one selected-camera editor; keep onboarding and shared policy controls in separate reachable workspaces.

Design and implementation handoff: https://github.com/YaBoiSean319/nvr/tree/docs/nvr-camera-inventory-redesign

## Acceptance criteria

- [ ] Provide Cameras (default), Add Cameras, and Groups & Policies; move current controls into the proper workspace without removing working workflows.
- [ ] Show a bounded searchable/sortable table with Name, IP address, Group, Connection, Recording health, and Recording mode; brand/model are optional and missing metadata is honest.
- [ ] Support active/archived, group, connection, and recording-health filters; distinguish policy grouping from overlapping browsing groups.
- [ ] Initial load/new login selects no camera; row activation opens exactly one editor and bulk checkboxes do not change it.
- [ ] All current per-camera controls belong only to the selected-camera editor, including controls previously rendered outside its conditional branch.
- [ ] Refresh and filtering do not replace the editing target; a filtered-out camera has an explicit notice and Show in table action.
- [ ] Keep table/editor bounded to the viewport, with reachable toolbars and clear empty/loading/unavailable states; no inventory-wide media requests.
- [ ] Prefactor inventory/editor/onboarding state only as needed within this slice, retaining stable camera IDs and existing API contracts.
- [ ] Demonstrate the workflow through authenticated browser tests with 0, 1, and 200 camera fixtures and administrator/operator roles.

## Blocked by

None (can start immediately from current main, which includes merged PR #66).

## Verification

Use authenticated observable browser/API behavior, deterministic camera fixtures and generated playable media as applicable. Preserve recording priority, identity, authorization, media/history, retention/holds and existing supported workflows. Do not count markup text or HTTP success alone as proof a complete workflow works.

## Post-merge testing required

- [ ] Review table metadata/status accuracy and inventory responsiveness against the deployment's configured cameras; record version, environment, workload, steps, owner and evidence.
