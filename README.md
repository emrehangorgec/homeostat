# homeostat

**Policy-constrained, verifiable self-healing for Android dedicated devices.**

homeostat turns old Android phones and tablets into reliable always-on screens: Home
Assistant wall panels, menu boards, signage, status displays. When the device silently
breaks (the app crashed, something else took the screen, the display slept, the page
never loaded) homeostat notices, picks the least disruptive fix the evidence justifies,
applies it under a deterministic safety policy, and **checks that the device actually
recovered** before calling it done.

> Status: early development. Milestone M1 (deterministic loop) is complete on a real
> device; M2 (content, backend and network faults) is running its device experiment.
> The AI layer starts at M3.

## First device results

Rules only, OnePlus 5T (Android 10), Device Owner, 20 runs per scenario:

| scenario | recovered and verified | median detection | median recovery |
| --- | --- | --- | --- |
| `app_crash` | 20/20 | 1.8 s | 23.3 s |
| `repeated_crash` | 20/20 | 5.4 s | 26.6 s |
| `wrong_foreground` | 20/20 | 1.3 s | 18.5 s |
| `screen_off` | 20/20 | 1.7 s | 27.0 s |

With 20 runs, 20/20 supports a true rate above about 84% (95% Wilson interval). About
10 s of each recovery is the oracle's stability window. Full report with every run and
step by step incidents: [docs/reports/m1-device-02.html](docs/reports/m1-device-02.html).

These numbers did not come on the first try. The real device surfaced problems the
simulator could not: Android's "app keeps stopping" dialog covering the relaunched app,
the lock screen after sleep, a WebView hiding the health marker, and USB link loss that
looked like an app failure. Each became a signal, a rule or a harness fix, and the
affected scenarios were rerun.

## How it works

```
observe ──> detect ──> propose ──> policy ──> act ──> verify ──> record
   ^          │                      │                   │
   │          └─ no rule matches ──> escalate (M1)       │
   │                                 diagnostician (M3+) │
   └──────────────── not healthy yet, try again ─────────┘
```

* **Observe.** A normalized, versioned `DeviceState`, collected in one shell round trip.
  Unknown signals stay `None`; rules never fire on evidence that could not be collected.
* **Detect.** Declarative *symptoms* decide whether something is wrong; declarative
  *rules* decide what it is and propose an action. Both live in TOML rule packs.
* **Policy.** Every proposal, from a rule or (later) a model, passes a deterministic
  gate: action allowlist, required privilege, impact ceiling, retry budget, per action
  cooldowns, lower impact alternatives first. There is no free-form command path.
* **Verify.** A healthy-state oracle that does not reuse the detection signals: a UI
  marker the target shows only once it has really loaded, plus a stability window in
  which the process must keep its PID and stay in front.
* **Record.** Every incident, action, policy verdict and verification goes to SQLite.

AI is an optional layer (from M3), never the authority: homeostat is fully useful with
rules only and no API key.

## Try it without a device

```
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"      # .venv/bin/pip on Linux / macOS
.venv/Scripts/homeostat demo -n 20 --seed 1
```

This runs every fault scenario 20 times against a simulated device and prints the
results table with 95% Wilson intervals. The simulator exercises the code path; its
numbers are not device results and are labelled as such.

## With a device

Requires `adb` and a device with USB debugging enabled.

```
cp config/homeostat.example.toml homeostat.toml   # set your target app and UI marker
homeostat observe                                 # what the guardian sees right now
homeostat run                                     # guard the device
homeostat eval app_crash wrong_foreground -n 20   # measure recovery
homeostat report <experiment>                     # HTML report from the stored runs
```

Device Owner provisioning of the on-device agent is described in
[android/README.md](android/README.md).

## Fault scenarios

| id | category | |
| --- | --- | --- |
| `app_crash` | known | target app crashes |
| `app_killed` | variant | target app force stopped, no crash log |
| `repeated_crash` | variant | two quick crashes; Android covers the app with its crash dialog |
| `wrong_foreground` | known | another app covers the target |
| `screen_off` | known | display sleeps |
| `crash_then_screen_off` | composite | both at once |
| `crash_loop` | variant | target crashes on every start (simulator or homeostat kiosk) |
| `blank_ui` | known | content disappears while the app believes it is fine (simulator or homeostat kiosk) |
| `stuck_loading` | known | the page request hangs, content never finishes loading (testbed) |
| `backend_down` | known | backend answers 503 for 30 s; the right move is to wait, not restart (testbed) |
| `auth_expired` | known | backend invalidates sessions, the page gets 401 (testbed) |
| `page_script_error` | known | the page is served with a script that throws (testbed) |
| `app_hang` | variant | main thread blocked: alive, in front, silent (simulator or homeostat kiosk) |
| `wifi_off` | known | Wi-Fi switched off |
| `crash_during_outage` | composite | app crashes while the backend is down (testbed) |

Testbed faults need `homeostat backend --reverse`, which serves the kiosk page from the
host and points the kiosk at it over `adb reverse`. Every fault states the lowest action
impact that fixes it; anything more disruptive is reported as an unneeded action.

The `held_out` column is filled only after rules, prompts and policy are frozen, by
scenarios the recovery logic has never seen.

## Health contract

An app can tell homeostat how it is doing. The homeostat kiosk publishes one JSON line
per state change and every 5 s under the log tag `homeostat-health`:

```json
{"v":1,"state":"ready","detail":"","http":null,"age_ms":5021,"declared":true}
```

`state` is `loading`, `ready`, `error`, `auth_error` or `app_error`. A web page reports
its own state through the kiosk's JS bridge (`homeostat.declare(1)`, then
`homeostat.report(state, detail)`). A missing heartbeat is itself a symptom: a hung
app cannot publish one. Commands go the other way as package broadcasts:
`com.homeostat.contract.RELOAD`, `RESET_SESSION` and `RESTART`.

## Writing rules

```toml
schema_version = "1"

[[rule]]
id = "app_not_running"
incident = "app_crash"
action = "relaunch_target"
alternatives = ["restart_target"]
priority = 30
when = [
    { path = "target_process.running", op = "eq", value = false },
    { path = "screen.awake", op = "ne", value = false },
]
```

Rules are written against a `DeviceState` schema version; a pack for another version is
refused at load time. Built-in rules: [homeostat/detect/default_rules.toml](homeostat/detect/default_rules.toml).

## Layout

```
homeostat/
  state/      DeviceState schema, parsers, collector
  detect/     symptom and rule engine, built-in rule pack
  policy/     deterministic action gate
  act/        action catalog and executor
  verify/     healthy-state oracle
  guardian/   the recovery loop
  store/      SQLite persistence
  faults/     fault injection scenarios
  eval/       experiment runner and reports
  device/     adb transport and simulator
android/      on-device agent (Device Owner app)
```

## Roadmap

- [x] M0: Device Owner feasibility on real hardware
- [x] M1: deterministic loop, verified on device
- [ ] M2: content, backend and network faults, rules-only baseline (device experiment running)
- [ ] M3: model-based diagnostician with abstention and calibration
- [ ] M4: rules distilled from resolved incidents, adversarial held-out evaluation
- [ ] M5: on-device agent, first release

## Contributing

Device reports, captured shell output, rule packs and fault scenarios are especially
welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[Apache License 2.0](LICENSE)
