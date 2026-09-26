import random

import pytest
from pydantic import ValidationError

from homeostat.adversary import scenario_schema, system_prompt, to_scenario
from homeostat.eval.runner import run_scenario
from homeostat.faults.composed import PRIMITIVES, Scenario, dump, load

QUIET_UPGRADE = {
    "id": "quiet_upgrade", "description": "An upgrade notice in the operator's words, then back.",
    "expected_outcome": "recovered", "needed_impact": "none",
    "steps": [{"do": "notice", "text": "Upgrading the heating controller, back in a minute", "for_s": 45}],
}


def test_steps_are_validated_against_the_primitives():
    assert Scenario.model_validate(QUIET_UPGRADE).testbed
    for bad in ({"do": "format_disk"}, {"do": "notice"}, {"do": "backend", "mode": "melt"},
                {"do": "touch_screen", "times": 50}, {"do": "wait", "seconds": 5, "colour": "red"}):
        with pytest.raises(ValidationError):
            Scenario.model_validate(dict(QUIET_UPGRADE, steps=[bad]))


def test_scenario_files_round_trip(tmp_path):
    path = tmp_path / "held_out.toml"
    two = Scenario.model_validate(dict(QUIET_UPGRADE, id="two_at_once", needed_impact="low",
                                       steps=[{"do": "crash_app"}, {"do": "wifi_off"}]))
    dump([Scenario.model_validate(QUIET_UPGRADE), two], path, "test set")
    loaded = load(path)
    assert [s.id for s in loaded] == ["quiet_upgrade", "two_at_once"]
    assert loaded[0].steps[0].params["for_s"] == 45 and loaded[1].to_fault().category == "held_out"


def test_a_free_text_notice_is_not_caught_by_the_known_prefixes(guardian, sim, clock):
    # rules_only: no rule reads free text, so the notice is escalated; waiting was right.
    fault = Scenario.model_validate(QUIET_UPGRADE).to_fault()
    runs = run_scenario(guardian, sim, fault, n=1, experiment_id="t", rng=random.Random(0),
                        clock=clock.time, sleep=clock.sleep)
    assert runs[0].outcome == "escalated" and runs[0].correct is False
    first = guardian.store.incident(runs[0].incident_id)["first_state"]
    assert "Upgrading the heating controller" in first and "maintenance:" not in first


def test_composite_steps_run_in_order(guardian, sim, clock):
    fault = Scenario.model_validate(dict(QUIET_UPGRADE, id="crash_and_wifi", expected_outcome="recovered",
                                         needed_impact="low", steps=[{"do": "crash_app"}, {"do": "wifi_off"}]))
    runs = run_scenario(guardian, sim, fault.to_fault(), n=1, experiment_id="t", rng=random.Random(0),
                        clock=clock.time, sleep=clock.sleep)
    assert runs[0].outcome == "recovered" and runs[0].correct


# -- adversary ----------------------------------------------------------------------------

def test_adversary_output_keeps_only_each_primitives_parameters():
    raw = dict(QUIET_UPGRADE, rationale="r",
               steps=[{"do": "notice", "text": "Back at noon", "for_s": 30, "mode": "down", "times": 0, "seconds": 0}])
    scenario = to_scenario(raw)
    assert scenario.steps[0].params == {"text": "Back at noon", "for_s": 30}


def test_adversary_sees_the_device_surface_but_not_the_guardian():
    prompt = system_prompt()
    for name in PRIMITIVES:
        assert f"- {name}:" in prompt
    for secret in ("wrong_foreground ->", "[[rule]]", "contest", "priority", "you will not be asked"):
        assert secret not in prompt
    assert set(scenario_schema()["properties"]["steps"]["items"]["properties"]["do"]["enum"]) == set(PRIMITIVES)
