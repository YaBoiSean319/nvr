# Bandwidth-aware viewing and low-data mode

## Auto quality

A tile left on **Auto** plays the substream in a grid and the main stream when enlarged (or when the camera has no
substream). An enlarged tile on the main stream is watched: if video keeps arriving late (fragments more than two seconds
apart, or playback stopping to wait, twice within 15 seconds) it steps down to the substream and says why on the tile
("Auto: Connection is slow: video was arriving late, using Sub"). After a calm spell (twice the interval) it tries the main
stream again ("Connection recovered, back on Main"). If the step up goes badly, the next wait doubles (up to eight times).

- No change comes sooner than the interval after the last one: 30 seconds by default, 15 seconds to 2 minutes in
  the **Data** menu of the Live toolbar (stored in this browser).
- A tile set to **Main** or **Sub** is never changed by Auto.

## Main streams only

**Data > Main streams only** (stored in this browser, off by default) puts every tile on its main stream: a grid no longer plays substreams, and Auto never steps a tile down when the connection is slow. The video wall follows the same choice when it runs in the same browser (the choice is stored in the browser, not on the server). The cost is bandwidth and server load: a wall of main streams carries several times the data of the same wall on substreams, so use it where the connection and the machine can carry it.

What wins when settings meet:

- A tile set to **Sub** keeps its substream (your explicit choice for that tile).
- **Low-data mode** switched on by hand keeps substreams and the four-tile cap: it is also an explicit choice, and it is the one that saves data.
- **Automatic** low-data (the installed app on cellular) does not turn on while Main streams only is chosen.
- Recorded footage (Playback and exports) is always the main stream: recording never uses the substream, so there is nothing to switch there.

## Low-data mode

Turn it on from the Live toolbar (**Data**), or leave it on **Automatic**: it then turns on by itself for the installed
app on a cellular connection (or with the browser's data-saver on), where the browser reports that. In low-data mode:

- Auto tiles use substreams (a tile with a Main choice, or no substream, still uses the main stream).
- Only the first four tiles play; the others say so and play when tapped.
- Tiles that are scrolled out of sight, or when the page is in the background, are released: their connection and server
  slot are freed, and they resume when they are visible again.

## Not covered by automated tests

The browser journeys (`tests/browser/bandwidth.spec.cjs`) hold video back in bursts through a WebSocket proxy to simulate
a slow link; they need the CI camera stream. Cellular detection uses stand-ins for the browser's connection API.
