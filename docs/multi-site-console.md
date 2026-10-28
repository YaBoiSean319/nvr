# Multi-site console

An NVR in **console mode** shows the cameras of other NVRs (sites) next to its own. Each site keeps its own recording, storage and configuration; nothing is replicated, and the console holds only how to reach a site.

## Setting it up

1. At each site, Settings → Users & access: create an API token with **only the `read` and `media` scopes**. (Adding a site checks that the token reads, can watch video, and is *not* an administrator's; a more powerful token is refused.)
2. At the console, Settings → Sites (console): turn on *This NVR is a console* and add each site with a name, its address and the token. A site on a private network address needs that box ticked. Loopback and link-local addresses are never accepted.
3. Give operators their sites: Settings → Users & access → Camera access now offers *sites* as well as camera groups. A grant for a site gives every camera of that site; *every camera* covers all sites; the other grants say nothing about sites.

The token is stored encrypted and is never shown or returned again. It can only look and watch: the console cannot change a camera, rule, alarm or setting at a site (the site refuses, because the token has no such scope), so alarms are acknowledged, assigned and resolved at the site that raised them (the console says so).

## What works through the console

Everything a site owns appears here as `<site>~<id at the site>`, so the same interface shows it:

| | |
|---|---|
| Cameras | One list: this NVR's cameras and each site's, with the site as a camera group. Nothing about how a site reaches its cameras (addresses, stream URLs, profiles) is shown. |
| Live | The site's live video is relayed through the console as it comes (privacy masks are applied by the site; `unmasked` is never forwarded). |
| Playback | A timeline asked of this NVR and the sites together; footage and snapshots are streamed from the site with range requests, never stored here. |
| Alarms | The inbox merges every site's alarms (with counts and the rule filter), newest first; opening one shows its snapshot and clip. |
| Search | Attribute search is asked of every site concurrently; results, thumbnails and the analysis coverage are merged. |

Not federated: natural-language search, find-similar, cases, exports and PTZ control (they need more than read and media), and the viewing audit at a site records the console's token rather than the person.

## A site that cannot be reached

A site that does not answer is marked **unreachable** at once and left alone for ten seconds before one request tries it again, so the console never waits for it request after request and the other sites answer at their own speed. Its cameras stay in the list as last known, shown as unreachable; its timeline shows an unreachable gap; merged lists carry each site's state and last contact. The health of every site (reachable, last contact, cameras, open alarms) is refreshed every fifteen seconds and shown under Settings → Sites (console).

## Notes

- Operators only ever reach sites they have been granted. A site they have no grant for is the same "not found" as one that does not exist, in lists, in links and over WebSocket.
- The camera lists, timelines, alarms and search run concurrently with a bound of eight seconds per answer.
- A site's token is used from the console server only; browsers never see it.
