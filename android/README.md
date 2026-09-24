# homeostat Android agent

A Device Policy Controller that grows into the on-device guardian at M5. Today it has:

| component | process | purpose |
| --- | --- | --- |
| `AdminReceiver` | main | Device Owner entry point |
| `MainActivity` | main | status screen, release Device Owner (long press) |
| `KioskActivity` | `:kiosk` | web kiosk; sets the `homeostat-ready` marker once the page loads |
| `ProbeReceiver` | main | M0 capability probe, background launch test |
| `DebugFaultReceiver` | `:kiosk` | debug-build fault hooks (`crash_on_start`, `blank_ui`, `reset`) |

Both receivers require `android.permission.DUMP`, which the adb shell holds and ordinary
apps cannot get.

Set the kiosk URL (persisted):

```
adb shell am start -n com.homeostat.agent/.KioskActivity --es url https://example.org
```

## Build

Requires JDK 17 to 23 (Gradle 8.11 does not run on newer JDKs) and an Android SDK
with `platforms/android-35` and `build-tools/35.0.0`. Point `local.properties` at
the SDK (`sdk.dir=...`).

```
./gradlew assembleDebug
```

## M0: provision as Device Owner

The device must have **no accounts** (factory reset, skip every sign in during
setup). Accounts can be added after provisioning.

```
adb install -t agent/build/outputs/apk/debug/agent-debug.apk
adb shell dpm set-device-owner com.homeostat.agent/.AdminReceiver
adb shell dumpsys device_policy | grep -i "device owner"
```

Then, from the repo root, `homeostat m0` runs every capability check and writes
`docs/m0_capabilities.json`. Each check restores what it changes. `--disruptive`
(Wi-Fi toggle), `--hide-pkg <pkg>` and `--reboot` are opt in.

## Release Device Owner

Any of these, in order of preference:

1. Long press "Release Device Owner" in the app.
2. `adb shell dpm remove-active-admin com.homeostat.agent/.AdminReceiver`
   (works because the APK is `testOnly`).
3. Factory reset from recovery. homeostat never blocks factory reset.
