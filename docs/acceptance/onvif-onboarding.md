# ONVIF camera onboarding: hardware validation

The automated tests use a mock ONVIF camera modelled on the structure Hanwha Wisenet cameras are known to use, not a capture from a real device (see `docs/camera-onboarding.md`). **No real camera has been tried yet.** Use this checklist for each camera model before relying on onboarding for it, and record the result under "Results" below. Do not put real addresses, usernames or passwords in this file; use the model name and firmware only.

## Before you start

- A camera with ONVIF turned on and an ONVIF user, on the camera network, reachable from the NVR.
- Its address, username and password (kept out of this file).
- Optionally, a way to read the camera's own event log, to compare.

## Checks

For each, tick it, or write what happened.

1. **Quick Add from the address and login alone.** Cameras > Add Cameras > Quick Add, enter only the address, username and password, and Check camera.
   - [ ] Every step finishes. Identifying device shows the right make and model.
   - [ ] The login method is right (Diagnostics shows which sign-in each request used; Digest, WS-Security or both).
   - [ ] If the camera's clock is far from the NVR's, sign-in still works.
2. **Streams.**
   - [ ] The recording stream is H.264 (not MJPEG), at the camera's highest H.264 resolution.
   - [ ] A lower-resolution H.264 live stream is chosen where the camera has one.
   - [ ] Verifying video says Working (a frame was decoded from the recording stream). Compare with opening the camera's stream in a player.
3. **Add it.**
   - [ ] The camera is added with recording off, under a name made from its make and model.
   - [ ] Turn recording on (motion mode). A recording appears after motion.
4. **Motion events.**
   - [ ] Configuring motion events says the camera accepted a subscription, and lists its motion topics.
   - [ ] Walk in front of the camera: a motion clip is made, with the pre-roll before the movement.
   - [ ] If the camera reports the same movement on more than one topic, only one clip is made.
   - [ ] Leave recording on for an hour: the subscription is renewed (the camera's Health stays healthy).
   - [ ] Restart the camera while it is recording: motion events recover by themselves within a minute or two; video is not marked unhealthy while only events are down.
5. **Faults.**
   - [ ] A wrong password reports that the login was rejected, and the camera's account is not locked afterwards (check by signing in to the camera's own web page).
   - [ ] Turn ONVIF events off on the camera: the camera still adds and records; motion shows not working with a clear message, and the NVR's own motion detection can be used.
   - [ ] Switch the camera off, try Quick Add: the Connecting step says it could not be reached. Use Add camera anyway; switch the camera on; within a few minutes its video becomes Working without further action (recording stays off until turned on).
6. **Bulk Add.**
   - [ ] A list of the cameras on one network adds them all, a few at a time, each with its own result.
   - [ ] For a large site: the NVR and the cameras stay responsive while a 100+ camera list runs.
7. **Secrets.**
   - [ ] The Diagnostics download, the NVR's logs and the camera's Health tab contain no password.

## Results

| Camera model | Firmware | Date | Checked by | Result and notes |
| --- | --- | --- | --- | --- |
| *(none yet)* | | | | |

Known model to try first: the Hanwha Wisenet XNV-6120R named in the original request (#173).
