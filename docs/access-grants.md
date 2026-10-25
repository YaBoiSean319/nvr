# Camera access grants

An **access grant** limits which cameras an operator may see. Administrators always see every camera and need no grant.

## What a grant is

A grant links a **user**, or everyone holding a Microsoft Entra **role**, to:

- **every camera**, or
- **one camera group**: its cameras, including any added to the group later,

optionally **until** an expiry (for a contractor or an investigator). A person may have several grants; they add up. An operator with no live grant can sign in but sees no cameras.

**Upgrading changes nothing.** Every operator that exists when this version is installed gets an "every camera" grant, and so does every operator created later (including one provisioned by Entra), unless they hold an Entra role that has grants of its own. An administrator narrows from there.

Manage grants in **Settings › Users & access › Camera access**: choose an operator or an Entra role, then every camera, chosen groups (each with its own optional expiry), or none. Saving replaces everything that subject was granted, in one audited step. The same is available as `GET`/`PUT /api/admin/access-grants` and `DELETE /api/admin/access-grants/{id}`. Entra roles are read from the sign-in token and stored with the user at each sign-in; a role grant applies to whoever holds the role at that moment.

## What is hidden

The person's access is resolved once per request, from their grants as they stand then. A camera outside it is **indistinguishable from one that does not exist**:

- Any route with a `camera_id` in its path or query, or a `group_id` in its path, answers `404` (`Camera not found` / `Camera group not found`) before it runs: live video, snapshots, playback media, segments, PTZ, privacy, AI, schedules and the rest, and WebSocket handshakes.
- Lists show only permitted cameras: the camera list, camera groups (and their members), AI event search, alarms (with their counts and rule filter), incidents, exports, saved live views, layouts and the live workspace (a hidden tile shows as an empty tile, a layout with nothing left to show is omitted), neighbours, and alarm pop-ins.
- A request that names a hidden camera in its body (a playback timeline, an export, a saved view) is refused exactly as if the camera did not exist.
- An export that includes a camera the person can no longer see is hidden from them, as are its download and verification.
- A restricted operator sees incidents of their cameras only, not system incidents or other cameras'.
- Push notifications are sent only for cameras the recipient may see.

What is stored is untouched, so restoring access brings a saved view back.

An **API token** has its creator's access. Tokens are made by administrators, who see every camera, and a token stops working if its creator stops being an administrator.

## Expiry and changes take effect at once

A grant that has expired gives nothing from that moment: the next request already sees it gone. **Open live video** (the WebSocket stream and the MJPEG fallback) re-checks the viewer's access about once a second, even when the camera is quiet, and closes with `4403` ("Your access to this camera has ended"). Changing a grant has the same effect.

## Audit

`access_grants_changed` (with the person or role, and the grants before and after) and `access_grant_deleted` are recorded in the audit log with the administrator who made them.

## For developers

Any new route an operator can reach must be accounted for: `tests/test_access_grants.py` walks every route, and fails if one that operators can reach is not gated by a `camera_id` or `group_id`, listed as filtering what it returns, or listed as touching no camera data. A route that names a camera in its path or query is protected by default (`app.principal`); a route that returns cameras another way must filter with `principal.access`.
