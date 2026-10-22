# 07: [Camera Admin 7] Add an on-demand selected-camera preview with complete teardown

Status: ready-for-agent

Issue: https://github.com/YaBoiSean319/nvr/issues/74

## Parent

https://github.com/YaBoiSean319/nvr/issues/67

## What to build

Inspect one selected camera with a small explicit muted preview that never creates media sessions for the rest of the inventory.

Design and implementation handoff: https://github.com/YaBoiSean319/nvr/tree/docs/nvr-camera-inventory-redesign

## Acceptance criteria

- [ ] Show Preview starts authenticated live delivery only on explicit request; selecting/filtering rows, opening tabs, polling health or restoring a session does not start preview streams.
- [ ] At most one selected-camera preview is active; keep existing delivery, quality, admission and role behavior.
- [ ] Show loading, fresh/reconnecting/stale/lost, unsupported and failed states with retry/close controls; never use live success as recording-write proof.
- [ ] Stop and release media connections, pending requests and browser resources on camera change, leaving Cameras, close, archive/unavailability or session end.
- [ ] Reject stale media/status responses during rapid selection and recover without displaying the old camera as the newly selected one.
- [ ] Do not autoplay audio, persist credentials/media URLs or automatically restart preview on a new session.
- [ ] Keep preview bounded/collapsible with reachable selected-camera controls on small screens.
- [ ] Use generated playable media and authenticated browser tests to observe explicit start, one-camera resource bounds, failure/recovery and teardown under rapid churn.

## Blocked by

- https://github.com/YaBoiSean319/nvr/issues/69

## Verification

Use authenticated observable browser/API behavior, deterministic camera fixtures and generated playable media as applicable. Preserve recording priority, identity, authorization, media/history, retention/holds and existing supported workflows. Do not count markup text or HTTP success alone as proof a complete workflow works.

## Post-merge testing required

- [ ] Verify intended camera codecs/profiles, preview recovery and recording health while previewing on actual browser/workstation/network combinations; record version, environment, workload, steps, owner and evidence.
