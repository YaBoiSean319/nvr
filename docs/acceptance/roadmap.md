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
