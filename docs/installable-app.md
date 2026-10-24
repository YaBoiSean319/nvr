# Installable app

The interface can be installed as an app on desktops and phones. In Chromium-based browsers use **Install** in the address bar or menu. On iPhone and iPad use **Share → Add to Home Screen**. The installed app opens in its own window with the site's name, colour and icon.

## Name and icon

The web app manifest (`/manifest.webmanifest`) is generated from **Settings → Appearance**:

- the name and a short name (the first 12 characters);
- the theme colour (the primary colour);
- square PNG icons at 192 and 512 pixels, plus a maskable version whose artwork stays inside the central safe zone. They are drawn from the uploaded **Icon**, centred on the primary colour. Without an uploaded icon, a simple mark in the primary and accent colours is used.

Icon addresses change whenever branding changes, so an installed app shows the new name, colour and icon after its next load. Some operating systems refresh a home-screen icon only occasionally, or when the app is reinstalled.

## What is stored on the device

A service worker (`/sw.js`) keeps the application shell available:

- **The page:** it always comes from the server when the server can be reached, so a new release shows immediately. A copy is kept only to show the sign-in page when the server is unreachable.
- **Hashed script and style files** (`/assets/…`) are kept. Their names change with every release.
- **Nothing else.** API responses, live video, recorded footage, snapshots, exports and credentials pass straight through and are never stored.

The worker's version is derived from the built page, so every release installs a new worker. That worker deletes the previous release's stored files when it takes over.

Signing out ends the session (its cookie is removed). It leaves only per-person preferences such as the theme, the camera inventory's sort order and the last Playback search; none of them holds credentials, tokens or camera addresses.

## Phones

The flows people use away from the desk are laid out for phone screens from 320 pixels wide, with touch-sized controls:

- the alarm inbox, including **Export clip**, which queues a one-camera export of the alarm's clip;
- a focused Live camera, where adding a camera closes the camera browser so the camera is in view;
- export jobs in the status drawer.

Installing does not make the NVR reachable from outside the local network; that remains a separate deployment decision.
