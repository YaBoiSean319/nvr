# 04: [Camera Admin 4] Deliver focused manual addition and CSV review/import workflows

Status: ready-for-agent

Issue: https://github.com/YaBoiSean319/nvr/issues/71

## Parent

https://github.com/YaBoiSean319/nvr/issues/67

## What to build

Add cameras manually or by reviewed CSV from their own Add Cameras modes, with compact forms/results and honest per-row outcomes.

Design and implementation handoff: https://github.com/YaBoiSean319/nvr/tree/docs/nvr-camera-inventory-redesign

## Acceptance criteria

- [ ] Provide Manual Add and CSV Import modes separate from Cameras and Discover, with bounded panels and reachable actions.
- [ ] Retain separate IP/address, ONVIF port, username/password/confirmation, brand, endpoint/profile, main/substream, connection-test and supported manual source behavior.
- [ ] Preserve current camera identity, validation, credential protection and recording defaults; do not silently start recording from form entry or preview.
- [ ] Manual test/add errors preserve editable safe input; success is based on actual API results and appears in inventory without duplicate creation.
- [ ] CSV retains preview-before-apply and accepted/duplicate/invalid classifications; applying imports only reviewed eligible rows.
- [ ] Show a bounded result table with per-row error/detail access, progress and created/failed counts; do not append a full form for each CSV row.
- [ ] Keep failed rows correctable/retryable without resubmitting successful additions, and reconcile unknown outcomes before retry.
- [ ] Apply the same dirty-navigation contract and stale-response handling as other onboarding/editor flows.
- [ ] Test manual profile/test/add and CSV preview/apply/partial failure/duplicate behavior through the authenticated browser/API boundary, including operator rejection.

## Blocked by

- https://github.com/YaBoiSean319/nvr/issues/69

## Verification

Use authenticated observable browser/API behavior, deterministic camera fixtures and generated playable media as applicable. Preserve recording priority, identity, authorization, media/history, retention/holds and existing supported workflows. Do not count markup text or HTTP success alone as proof a complete workflow works.

## Post-merge testing required

- [ ] Validate manual advertised profiles, authenticated camera connection and a sanitized representative import on the intended deployment; record version, environment, workload, steps, owner and evidence.
