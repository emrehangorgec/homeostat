from homeostat.act.catalog import Impact
from homeostat.policy.engine import PastAction, Policy, PolicyConfig, Proposal
from homeostat.state.schema import Mode
from tests.conftest import make_state

NOW = 1_000_000.0
EVIDENCE = ["target_process.running=False (eq False)"]


def evaluate(proposal, history=(), config=None, state=None):
    return Policy(config).evaluate(proposal, state or make_state(), "inc", list(history), NOW)


def test_allows_a_plain_rule_proposal():
    d = evaluate(Proposal("relaunch_target", "rule:x", EVIDENCE))
    assert (d.verdict, d.action) == ("allow", "relaunch_target")


def test_free_mode_never_acts():
    d = evaluate(Proposal("relaunch_target", "rule:x", EVIDENCE), state=make_state(mode=Mode.FREE))
    assert d.verdict == "deny"


def test_action_outside_catalog_escalates():
    d = evaluate(Proposal("adb shell rm -rf /sdcard", "model:x", EVIDENCE, confidence=0.99))
    assert (d.verdict, d.action) == ("escalate", "escalate")


def test_proposal_without_evidence_escalates():
    assert evaluate(Proposal("relaunch_target", "rule:x")).verdict == "escalate"


def test_per_action_max_substitutes_the_alternative():
    history = [PastAction("relaunch_target", NOW - 10, "inc")] * 2
    d = evaluate(Proposal("relaunch_target", "rule:x", EVIDENCE, ["restart_target"]), history)
    assert (d.verdict, d.action) == ("substitute", "restart_target")


def test_retry_budget_escalates():
    history = [PastAction(a, NOW - 10, "inc") for a in ("relaunch_target", "relaunch_target", "restart_target")]
    d = evaluate(Proposal("relaunch_target", "rule:x", EVIDENCE, ["restart_target"]), history)
    assert d.verdict == "escalate" and "budget" in d.reason


def test_other_incidents_do_not_count_against_budget():
    history = [PastAction("relaunch_target", NOW - 10, "other")] * 5
    assert evaluate(Proposal("relaunch_target", "rule:x", EVIDENCE), history).verdict == "allow"


def test_cooldown_spans_incidents():
    history = [PastAction("restart_target", NOW - 30, "earlier")]
    d = evaluate(Proposal("restart_target", "rule:x", EVIDENCE), history)
    assert d.verdict == "escalate" and "cooldown" in d.reason


def test_high_impact_needs_a_human():
    d = evaluate(Proposal("reboot", "rule:x", EVIDENCE))
    assert d.verdict == "escalate" and "human approval" in d.reason


def test_high_impact_allowed_when_configured():
    config = PolicyConfig(max_autonomous_impact=Impact.HIGH)
    assert evaluate(Proposal("reboot", "rule:x", EVIDENCE), config=config).verdict == "allow"


def test_missing_privilege_blocks():
    config = PolicyConfig(privileges=["none"])
    d = evaluate(Proposal("relaunch_target", "rule:x", EVIDENCE), config=config)
    assert d.verdict == "escalate" and "privilege" in d.reason


def test_low_confidence_model_proposal_is_refused():
    d = evaluate(Proposal("restart_target", "model:x", EVIDENCE, confidence=0.6))
    assert d.verdict == "escalate" and "confidence" in d.reason


def test_irreversible_action_never_on_model_proposal_alone():
    config = PolicyConfig(max_autonomous_impact=Impact.HIGH)
    d = evaluate(Proposal("clear_target_data", "model:x", EVIDENCE, confidence=0.99), config=config)
    assert d.verdict == "escalate" and "irreversible" in d.reason


def test_untried_lower_impact_alternative_goes_first():
    d = evaluate(Proposal("restart_target", "model:x", EVIDENCE, ["relaunch_target"], confidence=0.95))
    assert (d.verdict, d.action) == ("substitute", "relaunch_target")
