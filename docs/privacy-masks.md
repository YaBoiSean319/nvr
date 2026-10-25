# Privacy masks

A privacy mask is an area of a camera's picture that nobody sees: a neighbour's window, a public pavement, a till keypad. Masks are permanent until an administrator changes them.

## Setting masks

Open a camera's **Privacy** tab in **Cameras** and draw up to 8 rectangles over a recent frame, then **Save**. The tab says plainly whether the recordings on disk are masked:

- **The camera applies the masks.** Where the camera accepts ONVIF privacy masks (Media2 `CreateMask`), the NVR writes them to it. Footage the camera records from then on carries them. Footage recorded earlier is still masked by the NVR whenever it is viewed or exported.
- **The NVR masks everything.** For cameras without ONVIF masks, or if writing them fails, recordings on disk are **not** masked. Every live view, recording, snapshot, alarm picture, notification thumbnail and export is masked by the NVR as it is delivered.

Changing the masks replaces the ones the NVR wrote to the camera. Saving with no areas removes them.

## How the NVR masks

- **Live video** for a masked camera is transcoded with the mask drawn in (H.264, so it plays in every browser). Viewers share one transcoded stream per camera and quality. Each viewer counts against the live-view slot limit, as every viewer does.
- **The MJPEG fallback** draws the mask too.
- **Recordings** are delivered as a masked copy, rendered on first viewing and kept for a day. Seeking works as usual. The file on disk is never sent.
- **Snapshots, alarm pictures, notification thumbnails and tracked objects' keyframes** are masked. If a mask cannot be drawn, no picture is sent at all.
- **Exports** burn the mask into each camera's video and into the grid. `metadata.json` marks masked cameras with `"privacy_masked": true`.

Masks are filled black, rounded outwards to whole pixels so no edge of a masked area shows.

## Seeing unmasked footage

Only an administrator, signed in themselves (never through an API token), can see unmasked footage:

1. On the camera's **Privacy** tab, under **See unmasked footage**, give a reason (at least 10 characters) and choose **View unmasked for 15 minutes**.
2. For 15 minutes, Live, Playback and snapshots of that camera show the unmasked picture for that administrator only. **Mask again** ends it early. Reloading the page or signing out also ends it. Exports always stay masked.

The reason is recorded in the audit log (`privacy_unmasked_view_granted`), and so is every unmasked view (`privacy_unmasked_viewed`). Operators can never get unmasked media: asking for it is refused with `403` on every route.

## API

| Method | Path | Who | What |
|---|---|---|---|
| `GET` | `/api/cameras/{id}/privacy` | signed in | `{zones, camera_side, camera_masked_since, recordings, server_enforced, unmasked_until}` |
| `PUT` | `/api/cameras/{id}/privacy` | administrator | `{zones: [{x, y, width, height}]}` from 0 to 1, top left first, up to 8 |
| `POST` | `/api/cameras/{id}/privacy/unmasked` | administrator (session) | `{reason}`. Returns `{expires_at}` |

Media routes (`/live`, `/live/stream`, `/segments/{id}/media`, `/segments/{id}/snapshot`) take `?unmasked=true`. It is refused unless the caller is an administrator's own session with an active grant for that camera.
