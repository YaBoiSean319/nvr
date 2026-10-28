# Retention by purpose and camera notices

Settings → Privacy & notices.

## Purpose and legal basis

Each retention policy (the site default of 30 days, a group's, a camera's) can carry a **purpose** ("site security") and a **legal basis** ("legitimate interests"). Turn on *Require a purpose for every retention policy* and saving a group or camera retention without one is refused (422). Policies that existed before keep an empty purpose and are listed as needing one; the policy list and the retention endpoints (`needs_purpose`) show the prompt. Saving a policy without sending a purpose keeps the one already saved.

## Record of automatic deletions

Each time the automatic clean-up deletes a recording, it is counted in that camera's record for the day: how many recordings and bytes, the retention period and the purpose in force. `GET /api/retention-deletions` (administrators; optional `camera_id`, `start`, `end`) returns it, newest day first, and the Privacy page shows it. Footage kept for an alarm, a hold or an export is not deleted and so not counted. The record keeps the camera's name, so it outlives a renamed or removed camera.

## Generated notices

Both are built from the live configuration whenever they are opened, so a change to audio, retention or purposes shows at once:

- **Camera notice** (`/api/privacy/notice`): signage text with the organisation, the purposes, the longest retention, whether sound is recorded and by which cameras, and the contact details.
- **Data-protection summary** (`/api/privacy/summary`): each camera with its group, purpose, legal basis, retention, recording mode and audio; who can view cameras (administrators, and operators with how many cameras their grants give them); how much was deleted in the last 30 days; the contact.

Add `?format=html` for a printable page (print it or save it as PDF from the browser). Cameras are named, never their addresses or credentials. These endpoints are for administrators.
