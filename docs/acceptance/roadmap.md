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
