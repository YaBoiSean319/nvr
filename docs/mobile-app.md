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

CI (`.github/workflows/mobile.yml`) builds a debug APK, builds for the iOS simulator, and starts the APK in an Android emulator against a real NVR as a smoke test. Locally: `cd mobile && npm ci && npm run build:android` (Android Studio's SDK) or `npm run build:ios` (macOS with Xcode).

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
