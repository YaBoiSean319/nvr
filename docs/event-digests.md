# Event digests

A digest is a written account of a time range for the site, a group or some cameras: what the alarms were (by rule and by outcome), who was
about after hours, which vehicles came to the gates, where recording has gaps and why, and what went wrong with the system, each item linked to
its clip or record. It is made from stored data only, in the same words every time, with no language model.

## Where to find them

- **Digests** in the top navigation: make one now for the last 12 hours, 24 hours or 7 days (the whole site or one group), and read the ones scheduled for you.
- **Settings > Digests** (administrators): schedules, where each is delivered, and the optional summarising model.

## What is in one

| Section | What it lists | Link opens |
|---|---|---|
| Alarms | every alarm in range, by rule with counts new / acknowledged / assigned / resolved, newest last | the alarm |
| People after hours | person tracks that began in the after-hours window (default 22:00 to 06:00, site clock) | Playback at that moment |
| Vehicles at gates | vehicle events from zone and line rules | the alarm, or Playback |
| Recording gaps | each unexpected gap (not scheduled idle) with its reason, longest-first totals per camera | Playback at the start of the gap |
| System incidents | incidents active in range | the incidents |

Lists show the first 200 alarms and 50 of each other kind; the counts cover everything. The digest also says how much of the time the AI analysed, so
a quiet digest in unanalysed time is not read as a quiet time.

Links are addresses in the NVR (`/?alarm=…`, `/playback?playback=…&at=…`, `/?incident=…`); in deliveries they start with `NVR_PUBLIC_URL` when it is set.

## Schedules

A schedule names what to cover, **whose view** of it (the recipient), the days a shift ends on and its start and end times (site time zone; an end before the
start means it began the day before), the after-hours window, and the actions to deliver through. When a shift ends the digest of that shift is made and queued.

- The recipient decides what the digest may show: it never includes a camera that person may not see, and system incidents that name no camera only if they may see every camera. The recipient and administrators can read it; for anyone else it does not exist.
- A new schedule, or one whose shift changes, starts from the shift that last ended: it never makes a pile of old digests. After a long outage only the latest shift is made.
- **Make last shift's digest now** makes (and delivers) that shift's digest once; asking again returns the same one and sends nothing again.

## Delivery

Actions are the same kinds as rule actions: a signed webhook (event `digest.ready`, with the headline, counts and the full text), Slack, Microsoft Teams, or MQTT. Destinations
are encrypted, never returned, and each delivery is retried and logged under **Deliveries**. Push notifications are not used for digests.

## Optional summary

Off by default. When an administrator gives an OpenAI-compatible chat-completions address (public HTTPS, or an explicit private address they authorise), a model name, and turns it on,
schedules that ask for it send the digest text to that endpoint and add the one paragraph returned, labelled as written by the model. If the endpoint fails the digest is made without it.
With no endpoint configured, nothing is ever sent.

## Not covered by automated tests

Real Slack, Teams, MQTT receivers and a real model are not exercised: deliveries are tested against a captured sender and the summary against a stub reply.
