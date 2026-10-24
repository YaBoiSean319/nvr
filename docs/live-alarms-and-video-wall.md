# Alarm-driven Live layouts and video wall mode

## Alarms in Live

An alarm can bring its camera, and the cameras around it, into Live. It does this without changing anyone's saved view.

Set it up:

1. **Neighbours.** In **Cameras**, open a camera's **Neighbours** tab and add the cameras near it, in the order Live should show them (up to 15). An alarm on that camera shows it first, then its neighbours. Archived neighbours are left out.
2. **Rules.** In a rule's settings, tick **Pop alarms into Live**. Only alarms from those rules pop in. The choice is copied onto each alarm when it is raised, so changing the rule later never changes past alarms.
3. **People.** Each person turns **Alarm pop-in** on in Live's toolbar. It is off at first, and the choice follows the person to every browser.

When an alarm pops in, Live shows a temporary **alarm layout**, with a banner that names the rule, camera, object and time:

- **Return to my view** puts back exactly what was there before: the cameras, their layout, the loaded view and its unsaved changes, and the active and enlarged tile.
- Alarms that arrive while one is shown **wait their turn**. **Next alarm (n waiting)** shows the next one. Return dismisses the alarms still waiting.
- The alarm layout is never saved as the person's workspace. Loading a saved view ends it.
- Live's limits apply as always: at most 16 tiles, and the server's live slot limit. Tiles that leave the screen close their streams.

Live checks for new alarms every 2 seconds while it is open. It never replays alarms raised before it opened. People see only alarms on cameras they may view.

## Video wall mode

Open `/wall` on the screen's browser for full-screen video with no menus or navigation. The wall rotates shared views and pops alarms in.

1. An administrator creates an API token with **exactly** the `read` and `media` scopes (**Settings → Users & access**). The wall refuses any token with another scope, including `admin` and `export`, and any token without both.
2. On the wall, enter the token once. The wall exchanges it for its own session cookie and never stores the token. The session lasts 30 days. It ends at once when the token is revoked or expires, and the wall then asks for a token again.
3. Choose the shared views to rotate and the seconds on each, then **Start wall**. The address records the choice, for example `/wall?view=Lobby&view=Yard&dwell=30`, so a bookmarked address opens the wall straight to its views.

On the wall:

- Each view shows for its dwell time, then the next. A caption names the view.
- An alarm from a pop-in rule replaces the view with the alarm's camera and its neighbours. It stays for the dwell time (at least 20 seconds), then the rotation resumes. Further alarms queue and show in turn.
- The wall's session can only watch: cameras, shared views, live video and alarm pop-ins. Anything else is refused, and the main interface does not open with it. A person signed in on the same browser keeps their own access.

## API

| Method | Path | Who | What |
|---|---|---|---|
| `GET` | `/api/cameras/{id}/neighbours` | signed in | `{camera_ids}` in order |
| `PUT` | `/api/cameras/{id}/neighbours` | administrator | Replace the list: `{camera_ids}`, at most 15, not the camera itself |
| `GET` | `/api/live/alarm-pop-ins?after=<cursor>` | signed in | `{cursor, alarms}`: pop-in alarms after the cursor, oldest first. Without `after`, returns the current cursor and no alarms |
| `GET`/`PUT` | `/api/me/preferences` | signed in | `live_alarm_pop_in` alongside `theme`. Fields left out keep their value |
| `POST` | `/api/wall/session` | anyone with a token | `{token}`. Signs a wall in: `403` unless the token's scopes are exactly `read` and `media` |
| `GET` | `/api/wall/session` | the wall | `{name, scopes}` of the wall's token, or `401` |
| `DELETE` | `/api/wall/session` | the wall | Sign the wall out |

Rules carry `pop_into_live` in `/api/rules`.
