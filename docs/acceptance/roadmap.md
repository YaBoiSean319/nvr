# Roadmap acceptance: post-merge testing required

Scope: the roadmap in #106. These checks cannot be made from this repository (they need real cameras, disks, devices or models). Each needs the version under test, the environment, the steps followed, who ran it, and the evidence kept. Tick an item only when its evidence is attached. Everything the automated tests can prove is in the pull request that delivered the ticket.

### Disk health and retention forecast (#123)

- [ ] **Unchecked.** SMART readings from the physical recording disks.
  - Version:
  - Environment: the host's disks (SATA, NVMe), the compose `devices`, `cap_add: SYS_RAWIO` and `group_add` settings, `NVR_SMART_DEVICES`.
  - Steps: pass the disks through as `docs/disk-health-and-retention-forecast.md` describes; use Check disks now and confirm each volume reports a model and a state; confirm a disk behind a RAID controller or a USB bridge reports "not available" with its reason rather than "healthy".
  - Owner:
  - Evidence:

### Camera network hardening (#124)

- [ ] **Unchecked.** The hardening checks against the cameras in service.
  - Version:
  - Environment: camera models and firmware, the camera network ranges.
  - Steps: name the camera networks; run the checks; confirm a camera still on its factory login is reported and one with a chosen login is not; confirm a camera that offers RTSPS or HTTPS is reported while its stream or ONVIF address is plain, and is not once switched; confirm running the checks twice within an hour contacts each camera once (the cameras' own logs, or a packet capture).
  - Owner:
  - Evidence:

### Attribute search (#125)

- [ ] **Unchecked.** Accuracy of colours and direction on site footage, and the vehicle attribute model.
  - Version:
  - Environment: the cameras and lighting in service; the OpenVINO host; `vehicle-attributes-recognition-barrier-0039` (Open Model Zoo) files placed beside the other models, or its absence noted.
  - Steps: with AI analysis on, walk a person in a known top and trousers and drive a known vehicle past each camera type at day and at night; search by each attribute and record whether the track is found and how often the colours are wrong; install the vehicle attribute model, confirm Vehicle type becomes available in Playback's Find by attributes and that its types match the vehicles driven; confirm the unanalysed time listed for a range matches the AI coverage shown on the camera.
  - Owner:
  - Evidence:

### Licence plate recognition (#142)

Measured here (not a post-merge check): the plate pipeline, on a labelled sample of 500 plates rendered at eight levels of picture quality. The recogniser used is the template-matching stand-in in `tests/fixtures/plate_models.py`, built as a real OpenVINO model and run by the real `PlateReader`; the detector is a fixed-box stand-in. This measures the code (crop, resize, recognise, CTC decode, normalise, look-alike folding) and how quickly a simple reader degrades as pictures get worse. **It is not the accuracy of the Open Model Zoo models, which were not available to this build, and it says nothing about any camera.** Reproduce it with `python -m tests.fixtures.plate_accuracy` (deterministic: fixed seeds). "Right ignoring look-alikes" counts a read as right when it differs from the true plate only in characters the search treats as the same (O, Q or D and 0; I or L and 1; S and 5; B and 8; Z and 2).

| Picture quality | Plates | Read | Whole plate right | Right ignoring look-alikes | Characters right |
|---|---|---|---|---|---|
| Clean | 500 | 500 | 100.0% | 100.0% | 100.0% |
| Noise (sigma 35) | 500 | 500 | 100.0% | 100.0% | 100.0% |
| Noise (sigma 70) | 500 | 500 | 100.0% | 100.0% | 100.0% |
| Plate 2 px off, noise 35 | 500 | 500 | 92.0% | 93.2% | 98.6% |
| Plate 4 px off, noise 35 | 500 | 500 | 56.2% | 58.4% | 83.9% |
| Blur (radius 5), noise 35 | 500 | 500 | 94.2% | 97.4% | 99.2% |
| Low contrast (25%), noise 35 | 500 | 500 | 100.0% | 100.0% | 100.0% |
| Blur 4, low contrast 35%, noise 60, 2 px off | 500 | 500 | 21.8% | 33.2% | 79.7% |

- [ ] **Unchecked.** Plate models and accuracy on real cameras.
  - Version:
  - Environment: the gate or car-park cameras designated for plates (model, mounting height and angle, lighting day and night, vehicle speed); an OpenVINO plate detector (`vehicle-license-plate-detection-barrier-0106`) and a recogniser that matches the plates of the region (see `docs/licence-plates.md` for the contract and the charset file), placed beside the other models.
  - Steps: designate the cameras under Settings, Licence plates; drive known plates past each camera at day and at night at the speeds in use (at least 100 passes per camera); record a labelled sample (a CSV of `image,plate` and the frames) and run `python -m app.plates evaluate labels.csv frames/` inside the AI worker's environment; record the whole-plate, look-alike-tolerant and character accuracy and the share not read; search each plate in full, in part and with wildcards; add a deny-list plate and an allow-list plate to lists, make a rule for each, and confirm only the deny-list plate and a plate not on the allow list raise alarms; confirm plates read in a privacy-masked area never appear; confirm reads disappear when their footage is deleted.
  - Owner:
  - Evidence:

### Failover recorder (#143)

- [ ] **Unchecked.** A takeover and handback with real cameras and hosts.
  - Version:
  - Environment: the primary and standby hosts (storage, clocks, network), the cameras (models, how many simultaneous RTSP sessions each allows), the lease and silence chosen.
  - Steps: set the standby up as `docs/failover-recorder.md` describes; power off the primary host (not only the application) and time how long the standby takes to start recording every camera (the silence plus a few seconds), and whether any camera refused the standby's session; confirm the standby shows the takeover and an incident; power the primary on and confirm it does not record until the standby has stopped, that the standby's footage appears in its timeline marked as recorded by the standby and verifies (Playback's footage verification), and that both incidents recover; cut the network between the two (leave both running) and confirm what happens to recording on both sides, and that the footage comes back when it is restored.
  - Owner:
  - Evidence:

Tested here with two real NVR processes and a stand-in recorder that writes a small footage file per camera each second (the real decision whether to record is the production one; the ffmpeg pull from a camera is not exercised on takeover): the standby takes over within the silence, records every camera, stops when the primary returns before the primary records, and hands its footage back into the primary's index, where it verifies.

### Storage tiering (#144)

- [ ] **Unchecked.** Your object store, and real volumes.
  - Version:
  - Environment: the object store (AWS S3, MinIO, B2, Wasabi...: endpoint, region, whether it needs path-style addressing and TLS), the recorder's volumes, the cold folder (disk or share).
  - Steps: add the tier and confirm the check passes; set a policy for a volume with an age under the retention days and confirm footage moves (rate and bandwidth as set); play a moved segment from a bucket and from a folder in Playback, export across hot and cold footage, and verify a range (Playback's footage verification) across both; hold a moved segment and a hot one and confirm holds work; stop the store mid-move (or cut the network) and confirm the hot copies stay and the tier raises an incident, then confirm moving resumes; let retention delete a moved segment and confirm the object is gone from the bucket.
  - Owner:
  - Evidence:

Tested here: a local folder tier and an S3-compatible stand-in that checks AWS Signature Version 4 and upload hashes (the signing code reproduces two examples AWS publishes); moves, playback with ranges, holds, exports, interrupted moves, the chain across tiers, retention and deletion. The CI workflow runs the same against MinIO when it can start it; that leg could not be run where this was built.

### PTZ auto-tracking (#145)

- [ ] **Unchecked.** A real PTZ camera.
  - Version:
  - Environment: the camera (model, firmware, whether it supports continuous move and zoom), its AI analysis rate, the picture size and lighting.
  - Steps: set a home preset and timeouts on the camera's PTZ tab; walk through the scene, choose yourself from Live with **Follow**, and confirm the camera keeps you centred without hunting or overshooting (note the speeds if it does, as the gains are fixed), that it zooms to the size set, and that it keeps up as you leave the picture; hide so the camera loses you and confirm it stops, then returns to the home preset after the timeout; press an arrow in Live while it follows and confirm tracking ends at once; start tracking from a rule that watches a zone; confirm a person driving the camera is never taken over by a rule.
  - Owner:
  - Evidence:

Tested here: a camera stand-in that records the ONVIF commands it gets, with a simulated scene whose object walks out of the picture unless the camera follows (the commands keep it within a few percent of the centre), the lease rules, interruption, the lost timeout and home preset, privacy masks, a camera that stops answering, rule starts, and browser journeys. How well a real camera's motors, latency and the AI worker's rate work with the fixed gains is not tested.

### Fisheye dewarping (#146)

- [ ] **Unchecked.** A real 360° camera.
  - Version:
  - Environment: the camera (make, model, firmware, mount), its picture size and where the lens circle sits, the browser and its WebGL (a phone and a desktop).
  - Steps: set the lens on the camera's Lens tab (mount, centre, radius, field of view) using the raw circle on a Live tile; show a panorama, a double panorama and one to four virtual views, and check straight things (door frames, a table edge) stay straight and the views turn the way you drag; check the lens numbers if they curve near the rim; use the keyboard, a touch screen and the wheel; save a view and reload it; export a virtual view and a panorama and compare each file with the tile; try a browser without WebGL.
  - Owner:
  - Evidence:

Tested here: a synthetic fisheye picture of a room with straight lines (an equidistant lens model, ceiling, wall and floor mounts) rendered by the WebGL stage and by the export, straight to within about a pixel, and the two agreeing with a numpy reference; the same picture as a playing video; controls by keyboard, mouse and touch events; saved views and the workspace keeping views; exports through FFmpeg. Not tested here: a real fisheye lens (whose projection is not exactly equidistant), a stream from a real camera, or phones' WebGL.


### Two-way audio (#147)

- [ ] **Unchecked.** Real cameras with a speaker, and a real microphone.
  - Version:
  - Environment: the camera (make, model, firmware), whether it has a speaker or line out, its audio backchannel codec (G.711 is needed), the browser and microphone.
  - Steps: Settings, Cameras, the camera, Talk: allow talking for administrators and operators. Open the camera in Live, hold Talk, speak, and confirm the sound comes out of the camera's speaker and is clear (note the delay). Let go and confirm the sound stops at once. Hold Talk as one person and try as another: the second is told who is talking. Confirm the browser asked for the microphone only on the first talk. Confirm the talks are in the audit log and the camera's Recent talks. Turn on Keep the audio, talk, and download the audio. Try a camera with no speaker and confirm the message says so.
  - Owner:
  - Evidence:

Tested here: the G.711 encoders (round trip within quantisation), an RTSP stand-in that implements the backchannel with digest sign-in, the one-talker rule, release, the audit log, the optional recording and a browser journey with a made-up microphone. No real camera speaker was available.


### Guided updates (#148)

- [ ] **Unchecked.** A real release and a real host.
  - Version:
  - Environment: how the NVR is run (compose file location, service name, image tag in use), whether backups are set up, internet access to api.github.com.
  - Steps: tag a release (see docs/guided-updates.md) and confirm the workflow publishes the image and the release. In Settings, Updates choose Check now and confirm the new version, its notes and the migration line. Choose Back up, then show the upgrade steps and confirm the backup is listed before the commands. Run the commands on the host and confirm the container comes back at the new version and the page lists the upgrade and its migrations. Then follow the rollback and confirm the old version runs with the database copy named on the page. Turn the check off (switch and `NVR_UPDATE_CHECK=false`) and confirm no request leaves the host.
  - Owner:
  - Evidence:

Tested here: the feed (releases, drafts, pre-releases, markers, broken feeds), the off switch making no request, the backup running before the steps, the rollback naming the pre-migration copy present, the upgrade history at startup, and that no route or file can reach Docker. The release workflow could not be run.
