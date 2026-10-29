# Two-way audio

Talk through a camera's speaker from Live. While you hold the **Talk** button on a camera's tile (or hold the **T** key while the tile has focus), your microphone is sent over the signed-in connection to the NVR, which forwards it to the camera over the **ONVIF audio backchannel**. Let go and it stops at once, and the microphone is released.

## Turning it on

Settings, Cameras, the camera, **Talk** tab (administrators):

- **Allow talking through this camera.** Off by default, per camera.
- **Who may talk.** Administrators, operators, or both. People who may not talk never see the button.
- **Keep the audio of each talk.** Off by default. When off, only who talked, when and for how long is kept. When on, the audio of each talk is kept for 30 days and can be downloaded from the camera's **Recent talks** list; the download is audited.

Video walls and API tokens cannot talk: it is for a person at a microphone.

## What people see

- The first time someone holds Talk, the browser asks for microphone permission. It is not asked for before that, and the microphone is only open while the button is held.
- **One talker per camera.** While one person talks, everyone else sees *"name is talking"* on the button and cannot start. The status shows within a few seconds.
- A talk ends when the button is released, after 10 seconds without sound, after 5 minutes, or if the camera stops answering.

## What is recorded

Every talk is in the audit log (`talk_started`, `talk_ended` with the session, duration and reason; `camera_talk_changed` for settings; `talk_audio_downloaded`) and in the camera's talk history.

## How it works, and what the camera must support

The browser sends 8 kHz mono 16-bit audio over a WebSocket (`/api/cameras/{id}/talk/stream`). The server opens an RTSP session to the camera's stream address with `Require: www.onvif.org/ver20/backchannel`, picks the audio track the camera receives (`a=sendonly` in its description), sets it up over the same connection and sends G.711 (µ-law or A-law, whichever the camera lists) as RTP packets on it. The camera's sign-in uses the stream address's user and password (digest or basic).

Needed from the camera:

- an audio backchannel on the RTSP stream the NVR records from, and a speaker (or line out);
- **G.711** (PCMU or PCMA) on that backchannel. Cameras that offer only AAC, G.726 or similar are refused with a message saying so;
- a plain `rtsp://` stream address (not `rtsps://`).

A camera that does not answer the backchannel request, or offers no G.711 track, is reported when someone first tries to talk.

## Limits

- "One talker" is enforced inside one NVR process. A second recorder serving the same camera (failover) does not know about the first.
- The talk goes through the camera's recording stream address, so a camera that limits RTSP sessions may need a spare one.
- Tested here with a stand-in camera that implements the backchannel (digest sign-in, G.711 both ways) and a browser journey with a made-up microphone. It has not been tried against a real camera: see the acceptance checklist.
