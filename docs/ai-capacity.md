# How many cameras are analysed (#238)

There is **no fixed cap** on the cameras the AI can analyse. By default the number follows what the machine can carry.

## Automatic (the default)

Every analysed camera costs three things, and the AI worker measures them every 15 seconds:

- **CPU.** Each analysed camera has its own FFmpeg process that decodes the camera's whole stream (the analysis rate only picks which frames are kept), and everything else (recording, live, the web) needs the CPU too. Measured against the cores the container may use.
- **The AI model's time.** One inference runs at a time, for all cameras; its busy share is measured, and a camera whose frames wait too long for it counts as late.
- **Memory**, not counting file cache, against the container's limit (or the machine's memory), because running out gets the worker killed (#217).

It then moves a limit:

| What it sees | What it does |
| --- | --- |
| Room on every measure for a minute (CPU under 65%, the model busy under 65%, memory under 70% with 2 GB free, nothing late) while cameras are waiting | Adds **one** camera |
| Any measure past its limit (CPU 85%, the model 85% busy, memory 85% or under 1 GB free, a quarter of the cameras late) | Takes **10%** of the cameras away at once (20% when severe), lowest priority first |
| After taking cameras away | Waits 30 s before taking more (the change has to show), 10 minutes before growing, and does not try the count that failed again for an hour |
| A restart | Resumes at 75% of the last limit and grows back, and a new install starts with 4 |

The search is bounded by what the hardware could plausibly carry (8 cameras a core, and memory at about 200 MB a camera), but the measurements decide, not that estimate.

**Which cameras are analysed first:** those with an enabled rule, fall detection or plate reading, then the rest by name. A camera that is waiting says why ("Waiting: this machine is at its limit (CPU)").

**Settings > Integrations** shows what it is doing now: how many of the wanting cameras are analysed, what the limit is and what is holding it, and the CPU, model and memory it measured.

## A fixed number

Choose **A fixed number** to set any whole number yourself (there is no longer a ceiling of 16). Cameras beyond it wait. The automatic measurements are not used.

## Upgrading

Installations that had a number set become **Automatic**; the old number is kept as the fixed number if you choose that. The ceiling of 16 is gone from the page, the API and the database.

## What it does not do

It does not lower a camera's analysis rate instead of dropping it, does not spread analysis over several worker processes (see #217), and does not know about a GPU. Its measurements describe the whole container, so heavy exports or many live viewers also make it hold back cameras.

## Not yet checked on real hardware

How many cameras it settles on, and that the worker is not killed, on the 8 vCPU / 16 GB VM with 34 cameras. Look at Settings > Integrations a few minutes after start, and again after an hour.
