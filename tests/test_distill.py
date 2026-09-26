import json

import pytest

from homeostat.config import DEFAULT_RULES
from homeostat.detect.rules import RulePack
from homeostat.distill import distill, resolved_incidents, write_pack
from homeostat.distill.distiller import Candidate, Resolved, ScriptedDistiller, parse, to_rule
from homeostat.distill.replay import ASK_MODEL, Case, decide, fit_priority, replay
from homeostat.store.sqlite import Store
from tests.conftest import make_state

RULES = RulePack.load(DEFAULT_RULES)
SETTINGS = {"package": "com.android.settings", "activity": "com.android.settings.Settings"}


def person_using(touch_s=2.0):
    return make_state(foreground=SETTINGS, screen={"awake": True, "last_user_activity_s": touch_s})


def maintenance():
    return make_state(app={"state": "error", "detail": "maintenance: back soon"})


LEAVE_THE_PERSON = {
    "id": "leave the person", "description": "someone is using another app", "incident": "user_intent",
    "action": "escalate",
    "when": [{"path": "screen.last_user_activity_s", "op": "lt", "value": "30"},
             {"path": "target_in_foreground", "op": "eq", "value": "false"},
             {"path": "screen.awake", "op": "eq", "value": "true"}],
}


def resolved(state, action="escalate", diagnosis="user_intent"):
    return Resolved("i1", state, diagnosis, action, "test", [])


# -- candidate parsing ------------------------------------------------------------------

def test_values_are_coerced_to_the_type_the_state_holds():
    rule = to_rule(Candidate.model_validate(LEAVE_THE_PERSON), person_using())
    assert [c.value for c in rule.when] == [30.0, False, True]
    assert rule.id == "distilled_leave_the_person" and rule.origin == "distilled"


def test_unknown_paths_and_bad_values_are_refused():
    bad_path = dict(LEAVE_THE_PERSON, when=[{"path": "screen.mood", "op": "eq", "value": "happy"}])
    with pytest.raises(ValueError, match="unknown path"):
        to_rule(Candidate.model_validate(bad_path), person_using())
    bad_bool = dict(LEAVE_THE_PERSON, when=[{"path": "screen.awake", "op": "eq", "value": "yes"}])
    with pytest.raises(ValueError, match="true or false"):
        to_rule(Candidate.model_validate(bad_bool), person_using())


def test_a_candidate_must_describe_its_own_incident():
    result = parse(resolved(person_using(touch_s=90.0)), json.dumps(LEAVE_THE_PERSON))
    assert result.rule is None and "does not match its own incident" in result.error
    result = parse(resolved(person_using(), action="observe"), json.dumps(LEAVE_THE_PERSON))
    assert result.rule is None and "verified decision" in result.error


# -- replay -----------------------------------------------------------------------------

def case(state, action, correct, scenario="s"):
    return Case("i", "e", scenario, state, action, correct)


def test_replay_sorts_what_a_candidate_changes():
    rule = fit_priority(RULES, to_rule(Candidate.model_validate(LEAVE_THE_PERSON), person_using()), [person_using()])
    report = replay(RULES, rule, [
        case(person_using(), "escalate", True),          # the model left the person alone: right
        case(person_using(), "relaunch_target", False),  # the rule pulled the kiosk back: wrong
        case(make_state(foreground=SETTINGS, screen={"awake": True, "last_user_activity_s": 900.0}),
             "relaunch_target", True),                   # nobody there: the candidate stays out
        case(person_using(), "escalate", False),          # same action, wrong outcome
    ])
    assert (len(report.support), len(report.unverified), len(report.failures)) == (1, 1, 1)
    assert not report.promotable(min_support=1)  # one failure in two tries is far above 10%


def test_a_candidate_that_overrides_a_correct_decision_is_rejected():
    too_broad = to_rule(Candidate.model_validate(dict(LEAVE_THE_PERSON, when=LEAVE_THE_PERSON["when"][1:])),
                        person_using())
    too_broad = fit_priority(RULES, too_broad, [person_using()])
    nobody = make_state(foreground=SETTINGS, screen={"awake": True, "last_user_activity_s": 900.0})
    report = replay(RULES, too_broad, [case(nobody, "relaunch_target", True)]
                    + [case(person_using(), "escalate", True)] * 30)
    assert len(report.overrides) == 1 and not report.promotable(min_support=3)


def test_a_rare_failure_of_the_same_action_is_tolerated():
    rule = fit_priority(RULES, to_rule(Candidate.model_validate(LEAVE_THE_PERSON), person_using()), [person_using()])
    report = replay(RULES, rule, [case(person_using(), "escalate", True)] * 19 + [case(person_using(), "escalate", False)])
    assert report.promotable(min_support=3)


def test_priority_lands_just_above_the_rules_it_must_outrank():
    rule = to_rule(Candidate.model_validate(LEAVE_THE_PERSON), person_using())
    fitted = fit_priority(RULES, rule, [person_using()])
    assert fitted.priority == 21  # wrong_foreground is 20
    assert decide(RULES, person_using()) == (ASK_MODEL, None)  # contested without the candidate


# -- end to end on a store --------------------------------------------------------------

def _record(store, run_id, scenario, state, source, action, correct, diagnosis):
    incident = f"inc-{run_id}"
    store.save_incident({"id": incident, "run_id": run_id, "detected_at": 1.0, "closed_at": 2.0, "symptoms": [],
                         "incident_type": diagnosis, "rule_id": None, "outcome": "escalated", "attempts": 0,
                         "schema_version": "1", "first_state": state.model_dump(mode="json"),
                         "final_state": state.model_dump(mode="json")})
    store.save_action({"incident_id": incident, "at": 1.5, "proposed": action, "action": action, "source": source,
                       "verdict": "escalate" if action == "escalate" else "allow", "reason": ""})
    store.save_run({"id": run_id, "experiment_id": "exp", "scenario_id": scenario, "category": "variant",
                    "arm": "hybrid", "injected_at": 1.0, "incident_id": incident, "outcome": "escalated",
                    "correct": int(correct), "excess_actions": 0})
    if source.startswith("model:"):
        store.save_diagnosis({"incident_id": incident, "at": 1.5, "model": "m", "diagnosis": diagnosis,
                              "confidence": 0.9, "abstain": 1, "proposed_action": action, "explanation": "x"})


def test_distill_promotes_a_rule_that_survives_replay(tmp_path):
    store = Store(":memory:")
    for n in range(4):
        _record(store, f"r{n}", "user_takeover", person_using(), "model:m", "escalate", True, "user_intent")
    for n in range(4):
        _record(store, f"w{n}", "user_takeover", person_using(), "rule:wrong_foreground", "relaunch_target",
                False, "wrong_foreground")
    assert len(resolved_incidents(store, ["exp"])) == 4
    distiller = ScriptedDistiller(lambda r: LEAVE_THE_PERSON)
    outcomes = distill(store, ["exp"], RULES, distiller, per_cluster=2)
    assert len(distiller.calls) == 2
    promoted = [o for o in outcomes if o.replay and o.replay.promotable(3)]
    assert len(promoted) == 1  # the second identical candidate is a duplicate
    assert len(promoted[0].replay.support) == 4 and len(promoted[0].replay.unverified) == 4

    path = tmp_path / "distilled.toml"
    write_pack([promoted[0].result.rule], path, "test")
    pack = RulePack.load(DEFAULT_RULES, path)
    assert decide(pack, person_using()) == ("escalate", pack.rules[-1])
    assert pack.rules[-1].origin == "distilled"
