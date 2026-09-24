import random

from homeostat.eval.html import render
from homeostat.eval.runner import run_scenario
from homeostat.faults.scenarios import FAULTS


def test_report_renders_every_section(guardian, sim, clock):
    for fault in ("app_crash", "crash_loop", "blank_ui"):
        run_scenario(guardian, sim, FAULTS[fault], n=2, experiment_id="r", rng=random.Random(0),
                     clock=clock.time, sleep=clock.sleep, detect_timeout_s=10)
    page = render(guardian.store, "r")
    assert page.startswith("<title>homeostat · r</title>")
    assert "Simulated device" in page  # sim results are always labelled
    for heading in ("Recovery rate", "Detection latency", "Time to verified recovery", "Results matrix", "Inside the loop"):
        assert f">{heading}</h2>" in page
    assert "escalated to a human" in page and "undetected" in page
    assert page.count("<article class=\"story\">") == 2  # blank_ui never opens an incident


def test_report_escapes_stored_text(guardian, sim, clock):
    run_scenario(guardian, sim, FAULTS["app_crash"], n=1, experiment_id="<x>", rng=random.Random(0),
                 clock=clock.time, sleep=clock.sleep)
    page = render(guardian.store, "<x>")
    assert "<x>" not in page.replace("<title>", "").split("</title>")[1]
    assert "&lt;x&gt;" in page


def test_report_merges_sessions(guardian, sim, clock):
    run_scenario(guardian, sim, FAULTS["app_crash"], n=2, experiment_id="a", rng=random.Random(0),
                 clock=clock.time, sleep=clock.sleep)
    run_scenario(guardian, sim, FAULTS["screen_off"], n=3, experiment_id="b", rng=random.Random(0),
                 clock=clock.time, sleep=clock.sleep)
    page = render(guardian.store, ["a", "b"], name="ab")
    assert "<title>homeostat · ab</title>" in page
    assert "<dd>5</dd>" in page and "<dd>a; b</dd>" in page


def test_report_can_keep_only_some_scenarios_of_a_session(guardian, sim, clock):
    run_scenario(guardian, sim, FAULTS["app_crash"], n=2, experiment_id="a", rng=random.Random(0),
                 clock=clock.time, sleep=clock.sleep)
    run_scenario(guardian, sim, FAULTS["screen_off"], n=2, experiment_id="a", rng=random.Random(0),
                 clock=clock.time, sleep=clock.sleep)
    page = render(guardian.store, ["a:app_crash"], name="only")
    assert ">app_crash</th>" in page and ">screen_off</th>" not in page


def test_real_device_serials_are_masked():
    from homeostat.eval.html import mask_serial

    assert mask_serial("0123abcd") == "masked"
    assert mask_serial("sim-0001") == "sim-0001"
