# 02: [Camera Admin 2] Deliver focused camera settings tabs with safe saves and drafts

Status: ready-for-agent

Issue: https://github.com/YaBoiSean319/nvr/issues/69

## Parent

https://github.com/YaBoiSean319/nvr/issues/67

## What to build

Configure one selected camera through focused tabs with explicit Save/Revert and reliable Save / Discard / Cancel navigation.

Design and implementation handoff: https://github.com/YaBoiSean319/nvr/tree/docs/nvr-camera-inventory-redesign

## Acceptance criteria

- [ ] Provide General & Connection, Recording, Motion, AI, Schedule & Retention, and Health & History tabs with one active tab and persistent camera identity/save feedback.
- [ ] Preserve all supported name/source/credential/profile/test/audio, recording, motion/fallback/zone, AI/coverage/zone, schedule/retention/storage/inheritance, archive/restore and protected history actions.
- [ ] Each editable tab has explicit Save and Revert with unchanged/saving/failed/success states; configuration is never autosaved.
- [ ] Leaving a dirty tab, camera, workspace, or overwriting action offers Save / Discard / Cancel; canceled or failed navigation keeps the target and draft intact.
- [ ] Validation, network, and permission failures retain drafts and show field/context feedback; refresh and stale responses cannot erase edits or mark newer edits saved.
- [ ] If saving spans multiple existing resources, report partial successes precisely and retain failed/unsaved portions; do not imply unsupported atomicity.
- [ ] Protect close/reload when dirty; keep drafts and secrets out of browser persistence, URLs, diagnostics and public metadata.
- [ ] Revert reloads published settings; camera changes cannot retain the old camera's fields or apply a late response to the new one.
- [ ] Enforce operator read-only behavior and preserve archive, hold, export/playback lease, evidence, schedule DST and normalized-zone semantics.
- [ ] Exercise actual edit/save/reload and dirty navigation for every tab at the authenticated browser/API boundary, including partial/failed saves and rapid selection.

## Blocked by

- https://github.com/YaBoiSean319/nvr/issues/68

## Verification

Use authenticated observable browser/API behavior, deterministic camera fixtures and generated playable media as applicable. Preserve recording priority, identity, authorization, media/history, retention/holds and existing supported workflows. Do not count markup text or HTTP success alone as proof a complete workflow works.

## Post-merge testing required

- [ ] Verify camera profile/credential reconnection, motion/AI zones, effective policy and persisted configuration after service restart using intended device models; record version, environment, workload, steps, owner and evidence.
