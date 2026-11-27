# Tether for Android

A Trusted Web Activity: the launcher hands off to Chrome, which shows
https://tether.marstol.com full screen with no address bar. Every web deploy
updates the app; the APK only changes for native parts (icon, link handling,
and later the home-screen widgets).

## Toolchain (user-level, no sudo)

- JDK 21 (Temurin): `~/.local/lib/jdk-21` (`JAVA_HOME`)
- Android SDK: `~/.local/share/android-sdk` (`ANDROID_HOME`), with platform 37 and platform-tools (`adb`)
- Both are exported in `~/.bashrc` (block marked "Android toolchain for the Tether app")

## Build

    cd android
    ./gradlew assembleRelease     # app/build/outputs/apk/release/app-release.apk

## Signing key

Release builds are signed with `~/.android/keystores/tether-release.jks`. Its
path, alias and passwords are in `~/.gradle/gradle.properties` (`TETHER_KEYSTORE`,
`TETHER_KEYSTORE_PASSWORD`, `TETHER_KEY_ALIAS`, `TETHER_KEY_PASSWORD`). Neither
file is in the repo.

Backup: `/root/backups/tether-android/` on the droplet (`tether-release.jks`
and `signing.properties`). Lose the key and updates no longer install over the
existing app: it has to be uninstalled and the new key's fingerprint put on the
server.

The key's SHA-256 fingerprint is `TETHER_ANDROID_CERT_SHA256` in
`/etc/tether/tether.env`; the server publishes it at
`/.well-known/assetlinks.json`. Without a match, Chrome shows an address bar.

## Install on the phone (wireless debugging)

    adb pair <ip>:<pairing port> <code>    # once per computer
    adb connect <ip>:<port>
    adb install -r app/build/outputs/apk/release/app-release.apk

## Links

Pages under /inbox, /tasks, /calendar and /settings open in the app. Sign-in
and OAuth paths stay in the browser on purpose, so a login finishes where it started.

## Notifications

Tether's notifications (new mail, event reminders) are Web Push messages to the
site; Chrome hands them to this app's `DelegationService`, so they show as
Tether with `ic_notification`. Android 13+ asks for the notification permission
when you turn notifications on in Settings (`NotificationPermissionRequestActivity`).
