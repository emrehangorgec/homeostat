import random

from homeostat.eval.report import matrix, wilson
from homeostat.eval.runner import run_scenario
from homeostat.faults.scenarios import FAULTS
from homeostat.state.schema import Mode
from tests.conftest import TARGET


def test_healthy_device_opens_no_incident(guardian):
    assert guardian.tick() is None


def test_crash_is_recovered_and_verified(guardian, sim):
    FAULTS["app_crash"].inject(sim, TARGET)
    report = guardian.tick()
    assert report.outcome == "recovered"
    assert report.incident_type == "app_crash"
    assert report.final_verify.strength == "strong"
    assert guardian.store.incident(report.id)["outcome"] == "recovered"
    assert guardian.tick() is None  # the handled crash does not reopen an incident


def test_crash_loop_is_escalated_within_budget(guardian, sim):
    FAULTS["crash_loop"].inject(sim, TARGET)
    report = guardian.tick()
    assert report.outcome == "escalated"
    assert report.attempts == guardian.policy.config.retry_budget
    actions = [a["action"] for a in guardian.store.actions_for(report.id)]
    assert actions[0] == "relaunch_target" and actions[-1] == "escalate"
    assert "dismiss_system_dialogs" in actions  # the repeated crashes raise Android's crash dialog
    assert all(actions.count(a) <= guardian.policy.config.per_action_max for a in set(actions) - {"escalate"})


def test_free_mode_observes_only(guardian, sim):
    guardian.collector.mode = Mode.FREE
    FAULTS["wrong_foreground"].inject(sim, TARGET)
    sent_before = len(sim.log)
    report = guardian.tick()
    assert report.outcome == "observed_only"
    assert not any(cmd.startswith("am start -n") for cmd in sim.log[sent_before:])


def test_unclassified_incident_escalates(guardian, sim):
    sim.s.internet = False
    report = guardian.tick()
    assert report.symptoms == ["no_internet"]
    assert report.outcome == "escalated" and report.incident_type is None


def test_eval_runner_records_every_run(guardian, sim, clock):
    runs = run_scenario(
        guardian, sim, FAULTS["app_crash"], n=5, experiment_id="t",
        rng=random.Random(0), clock=clock.time, sleep=clock.sleep,
    )
    assert [r.outcome for r in runs] == ["recovered"] * 5
    assert all(0 <= r.detection_latency_s <= guardian.config.poll_s for r in runs)
    assert len(guardian.store.runs("t")) == 5
    assert "100% [57%, 100%] n=5" in matrix(runs)


def test_blank_ui_is_undetected_by_rules_only(guardian, sim, clock):
    runs = run_scenario(
        guardian, sim, FAULTS["blank_ui"], n=2, experiment_id="t",
        rng=random.Random(0), clock=clock.time, sleep=clock.sleep, detect_timeout_s=10,
    )
    assert [r.outcome for r in runs] == ["undetected"] * 2


def test_wilson_interval():
    lo, hi = wilson(20, 20)
    assert round(lo, 3) == 0.839 and hi == 1.0
    assert wilson(0, 0) == (0.0, 1.0)


def test_link_loss_is_recorded_and_never_scored(guardian, sim, clock):
    from homeostat.device.base import DeviceUnreachable
    from homeostat.eval.report import summarize

    class FlakyFault:
        id, category = "app_crash", "known"

        def inject(self, device, target):
            device.unplugged = True
            raise DeviceUnreachable("adb: no devices/emulators found")

    runs = run_scenario(guardian, sim, FlakyFault(), n=1, experiment_id="t", rng=random.Random(0),
                        clock=clock.time, sleep=clock.sleep)
    sim.unplugged = False
    runs += run_scenario(guardian, sim, FAULTS["app_crash"], n=2, experiment_id="t", rng=random.Random(0),
                         clock=clock.time, sleep=clock.sleep)
    assert [r.outcome for r in runs] == ["link_lost", "recovered", "recovered"]
    summary = summarize(runs, "app_crash")
    assert (summary.n, summary.recovered, summary.outcomes["link_lost"]) == (2, 2, 1)


def test_guardian_loop_survives_link_loss(guardian, sim):
    sim.unplugged = True
    lost = []

    def stop_after_one(seconds):
        raise KeyboardInterrupt

    guardian.sleep = stop_after_one
    try:
        guardian.run_forever(on_link_lost=lost.append)
    except KeyboardInterrupt:
        pass
    assert lost == ["adb: no devices/emulators found"]


def test_crash_dialog_is_dismissed_then_target_relaunched(guardian, sim):
    FAULTS["repeated_crash"].inject(sim, TARGET)
    assert sim.s.error_dialog
    report = guardian.tick()
    assert report.symptoms[0] == "target_not_running" and "blocking_dialog" in report.symptoms
    actions = [a["action"] for a in guardian.store.actions_for(report.id)]
    assert actions == ["dismiss_system_dialogs", "relaunch_target"]
    assert report.outcome == "recovered"


def test_screen_off_wakes_then_dismisses_lock_screen(guardian, sim):
    FAULTS["screen_off"].inject(sim, TARGET)
    report = guardian.tick()
    actions = [a["action"] for a in guardian.store.actions_for(report.id)]
    assert actions == ["wake_screen", "dismiss_keyguard"]
    assert report.outcome == "recovered"
