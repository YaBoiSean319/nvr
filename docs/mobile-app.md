# Native mobile app (iOS and Android)

The app is the NVR's own web interface in a native shell ([Capacitor](https://capacitorjs.com)): there is one interface codebase (`web/`), and `mobile/` holds only the shell. The app adds what a browser tab cannot: push notifications through FCM and APNs, a biometric lock, the sign-in kept in the phone's secure storage, and a tap on a notification that opens its alarm.

## How it works

- **Start screen** (`mobile/www`): asks once for the NVR's address (https anywhere; plain http only for a private-network address), checks that it answers like an NVR, keeps it, and from then on opens the NVR's own interface. The address can be your NVR directly or the one a tunnel from remote access gives it. *Use a different NVR* in the app's settings returns here.
- **Identifying the app**: the shell adds `NVRApp/1.0` to the user agent. The interface switches its native features on only when that and Capacitor's bridge are present, and the server then marks every response `Cache-Control: no-store` and the interface registers no service worker, so nothing it shows (pages, pictures, footage, API answers) is kept on the phone beyond the screen. Android also blocks screenshots and the recent-apps preview (`FLAG_SECURE`), and `allowBackup` is off.
- **Push**: in *Notification settings* the person turns notifications on for the phone; the app obtains an FCM (Android) or APNs (iOS) token and registers it (`POST /api/native/devices`). From then on it is an ordinary push subscription: the person's rule, group and quiet-hours choices, the access check (made again when sending) and the queue with its retries all apply. The notification carries the alarm's title, text and link; a tap opens `/?alarm=<id>` on the NVR (links that leave the NVR are never followed). The app registers its current token each time it opens.
- **Lock**: with *Lock the app* on, the app hides the whole interface after the idle time chosen (1, 5, 15, 30 or 60 minutes), when opened after that time, and when returned to, until the phone's face, fingerprint or passcode unlocks it. A phone with none of these set up cannot turn the lock on.
- **Sign-in kept securely**: after a normal sign-in the NVR gives the app a *device session* secret (`POST /api/native/device-sessions`, shown once, stored only as a digest on the NVR) which the app keeps in the Keychain or Keystore. When the session has ended, the app exchanges it (once unlocked) for a new session and a replacement secret, so a copy of an old one is useless. It lasts 30 days from last use; ten per person; signing out of the app revokes it.

## Administrator setup (push)

Settings are written once and never shown again (`PUT /api/native/push-settings`, administrators only):

- **Android (FCM)**: create a Firebase project, add the Android app `app.nvr.mobile`, download `google-services.json` into `mobile/android/app/` before building, and paste the project's *service account* JSON (Project settings → Service accounts → Generate new private key) into the NVR.
- **iOS (APNs)**: in your Apple developer account enable Push Notifications for the app id, create an APNs key (`.p8`), and give the NVR the key, its key id, your team id and the bundle id; use the sandbox for development builds.

## Building

CI (`.github/workflows/mobile.yml`) builds a debug APK, builds for the iOS simulator, starts the APK in an Android emulator against a real NVR (the emulator test seeds the NVR's address and checks that the app opens the NVR's own page), and runs the existing browser journeys (shell, alarms, settings, rules, access) as the app runs them: with its user agent and a stand-in for Capacitor's bridge (`NVR_APP_MODE=1`; the others need the installable web app's worker or the simulated camera and stay in the main workflow). Locally: `cd mobile && npm ci && npm run build:android` (Android Studio's SDK) or `npm run build:ios` (macOS with Xcode).

## Things to know

- **The NVR's own site is trusted with the bridge.** To open an NVR whose address is only known at run time, the shell lets its web view navigate anywhere (`allowNavigation`), and every page it opens can use the app's plugins, including the secure-storage secret. The address comes only from the start screen (https, or http on a private-network address, checked host by host), so this holds as long as your NVR does not redirect somewhere else. A native navigation check that confines the web view to the saved address is a worthwhile hardening step.
- **The lock is a screen, not the Keychain.** The device secret is in the Keychain/Keystore but is not itself gated by the biometrics; with the lock off, a phone signs back in without asking. With it on, the app shows none of the interface until the phone says yes. A phone with no biometrics or passcode cannot be locked.
- **Idle means no touches.** Watching a live view without touching the screen counts as idle.
- **Notifications go through Google's and Apple's servers.** Unlike web push, their content (the rule and camera name, the time, and a link to a picture) is not end-to-end encrypted to the phone; the picture is fetched from your NVR by a short-lived signed link naming one alarm.
- **Tapping a notification opens its alarm**, which shows its clip; there are no separate links to clips. A tap that launches an app that was closed is handled once the person has unlocked it and signed in (check this on real devices).
- **Misconfigured push never loses phones.** Only FCM's "unregistered" and APNs' `Unregistered` forget a phone; wrong credentials, project, bundle id or environment show as errors on the delivery and are retried.

## Post-merge checks (not done in CI)

These need accounts, certificates or hardware:

1. **Store submission** for Google Play and the App Store: app icons and screenshots, privacy labels, signing, review.
2. **Release signing**: an Android upload keystore and an iOS distribution certificate and provisioning profile (CI builds unsigned debug builds only).
3. **Push certificates**: the Firebase project with `google-services.json`, and the APNs key with the push entitlement (`aps-environment` is `development` in the project; Xcode sets `production` when signing for distribution).
4. **Real devices**: an alarm on a real Android phone and a real iPhone arrives and opens; the biometric prompt and its fallback; the Keychain/Keystore secret survives an app restart and is gone after sign-out; nothing of the NVR is in the phone's app storage after use.
5. **Tunnel**: the app against an NVR reached through each remote-access tunnel (docs/remote-access.md).
6. **Push through the stores' infrastructure**: delivery while the app is closed, in Doze on Android, and under iOS notification summaries and Focus modes.

## What the tests cover

`tests/test_native_app.py` (FCM and APNs requests and signatures against stand-in services, device sessions, no-store); `tests/browser/native-app.spec.cjs` (the interface in the app's user agent with a stand-in for Capacitor's bridge and plugins: lock, secure-storage sign-in, push registration and tap); the CI workflow above (builds and emulator smoke). The phone's own dialogs, the push services and the secure storage are stood in for by those tests, and are the first of the checks above.
