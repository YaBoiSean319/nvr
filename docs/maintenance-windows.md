# Maintenance windows (#234)

While cameras are being worked on, they should not set off alerts. A **maintenance window** says "this camera, these groups, or the whole site is being worked on from now until then".

## Using it

- **Cameras > Maintenance**: give what is being done, choose the cameras (administrators can also choose camera groups or the whole site), how long, and optionally a later start. **Start maintenance.**
- **On a camera** (Cameras > select a camera): **Start maintenance (1 hour)** with one press, and **End maintenance** to stop early. A camera in a window shows **In maintenance**.
- Windows are one-off: a start and an end. Operators can open a window on cameras they can see for up to **8 hours** (starting within 24 hours); administrators can cover cameras, groups or the whole site for up to **7 days**. If the work goes on, end the window and start another.
- **In every workspace** the status bar shows **Maintenance: 3 cameras** (or **Maintenance: whole site**) while a window covers cameras you can see. Pressing it lists the windows with what each has held back, an **End now** button for the ones you may end (an administrator any window, anyone else the windows they opened), and a link to start or manage them.
- A window can be ended early, or, before it has started, cancelled. Everything is audited (`maintenance_opened`, `maintenance_ended`).

## What happens in a window

- **Alarms from rules are kept but silent.** The alarm is created, appears in the inbox marked "during maintenance (name)", and keeps its clip, but sends no push, no Live pop-in, no webhook and runs none of the rule's actions (email, Slack, relay...).
- **Incidents** (camera offline, recording failure, tamper, AI worker, disk...) raise no webhook. They still show in the incident list and on the camera. A fault that belongs to no camera (a worker, the disk) is quieted only by a window for the whole site.
- **Recording and analysis carry on.** Nothing is paused.
- **When the window ends**, a fault that is still there is announced then, on the next check (within a minute or so), so a camera left broken does not stay silent. A fault that was mended during the work is never announced, and no recovery notice is sent for something that was never announced.
- Each window counts what it held back (alarms, incident notifications), shown in the list and recorded when it ends, so a window that hid something real is visible afterwards.

## Not built

Recurring windows, pausing AI analysis during a window, a reminder to the person before their window ends, and quieting digests (a digest still lists what happened).
