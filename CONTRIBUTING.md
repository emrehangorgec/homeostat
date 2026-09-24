# Contributing to homeostat

Thanks for your interest. homeostat welcomes issues, device reports, rule packs, fault
scenarios and code.

## Getting started

```
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"      # .venv/bin/pip on Linux / macOS
.venv/Scripts/python -m pytest
.venv/Scripts/homeostat demo -n 20 --seed 1
```

The whole loop runs against a simulator, so most changes need no phone. The Android
agent builds with `cd android && ./gradlew assembleDebug` (JDK 17 to 23).

## What helps most

- **Device reports.** Run `homeostat m0` and `homeostat eval ... -n 20` on your device
  and open an issue with `docs/m0_capabilities.json` and the report from
  `homeostat report`. Every device teaches the project something the simulator cannot;
  most of the current rules exist because of a real device finding.
- **Captured shell output.** Parser fixtures in `tests/fixtures/` are marked `CAPTURED`
  when they come from a real device. Output from other Android versions and vendors is
  very welcome.
- **Rule packs and fault scenarios.** Rules are TOML (see `homeostat/detect/default_rules.toml`).
  A new rule should come with the fault scenario that motivates it and a test.

## Ground rules

- **Simulator numbers are never results.** The simulator exercises code paths. Reports
  rendered from it are labelled as simulated; never present them as device evidence.
- **Unknown is not false.** A signal that could not be read is `None`, and rules must
  not fire on it.
- **Nothing outside the action catalog runs.** New recovery actions go into
  `homeostat/act/catalog.py` with an impact level, reversibility and required privilege,
  and pass through the policy layer like every other action.
- **Report what happened.** Failed and escalated runs stay in the data. Harness failures
  (lost USB link, unhealthy baseline) are recorded and excluded from rates, never deleted.

## Pull requests

- Keep changes focused; add or update tests; run `pytest` before opening the PR.
- Commit messages follow conventional commits (`feat:`, `fix:`, `docs:`, `test:` ...).
- A `DeviceState` schema change that breaks existing rule packs needs a new
  `SCHEMA_VERSION`.

## License

homeostat is licensed under the Apache License 2.0. By contributing, you agree that
your contributions are licensed under the same terms (Apache 2.0, section 5).
