# Checking how well person detection works on your cameras

Person detection can miss people that look unlike the pictures it was trained on: someone in a wheelchair, someone seated, far from the camera or half hidden. No published figure says how it does on **your** cameras, so this measures it, and says whether simply lowering the person confidence threshold would fix the misses.

Nothing here sends pictures anywhere. The pictures stay on your own machines.

## 1. Pull pictures out of a recording (on the NVR host)

Pick a recording that shows the cases that go wrong. Motion clips are in `<recordings>/<camera id>/motion/`.

```
docker exec nvr-nvr-1 python -m app.detector_eval extract /data/recordings/<camera id>/motion/<clip>.mp4 /tmp/frames --every 3 --limit 150
docker cp nvr-nvr-1:/tmp/frames ./frames
```

(Adjust the paths to where your recordings are; `docker exec nvr-nvr-1 printenv NVR_RECORDINGS_PATH` shows it.) About 100 to 300 pictures is enough, with a good number showing the people it struggles with.

## 2. Mark where the people are (on any computer)

Open `tools/frame_labeler.html` (download it from the repository; it needs no server and nothing is uploaded), choose the pictures, and draw a box around **every** person in each. Set the tag to *wheelchair* (or *walker*, *seated*) for the cases you are checking. Move between pictures with the arrow keys. A picture you look at and leave without boxes counts as "nobody here", which tests false alarms. Press **Save labels.json**; you can load it again later to carry on.

## 3. Score the detector (on the NVR host)

```
docker cp ./labels.json nvr-nvr-1:/tmp/frames/labels.json
docker exec nvr-nvr-1 python -m app.detector_eval score /tmp/frames /tmp/frames/labels.json
```

It prints, for a range of confidence thresholds, how many people were found (overall and for each tag), false alarms per picture and the time per picture, then lists every person missed at the usual threshold (0.55) with the model's best confidence for their place:

- **"best 0.42"**: the model saw them but not confidently enough. Lowering the person threshold on that camera (Cameras > the camera > AI) to about 0.4 finds them, at the cost of the false alarms shown in the table for that row.
- **"nothing seen"**: the model gave nothing at all there. No setting fixes that; it needs a better model, or one trained on your pictures (ticket #255).

Use `--thresholds 0.3,0.4,0.5` to choose the thresholds and `--iou 0.3` to count a looser box as finding a person (the default 0.5 asks for a close fit).

## What this does not do yet

It scores the model the NVR already uses. Comparing other models (and training one on your labelled pictures) is the next step of #255 and needs the labelled pictures this produces.
