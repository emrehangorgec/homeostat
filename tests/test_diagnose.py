import json
from types import SimpleNamespace

import pytest

from homeostat.config import DEFAULT_RULES
from homeostat.detect.rules import RulePack
from homeostat.diagnose.base import Diagnosis, DiagnosisResult, IncidentContext, Usage, output_schema
from homeostat.diagnose.claude import ClaudeDiagnostician, cost_usd
from homeostat.diagnose.prompt import evidence_message, system_prompt
from homeostat.diagnose.scripted import ScriptedDiagnostician
from homeostat.faults.scenarios import FAULTS
from homeostat.store.sqlite import Store
from homeostat.wiring import build_guardian
from tests.conftest import TARGET

RULES = RulePack.load(DEFAULT_RULES)


def diag(action="observe", confidence=0.8, abstain=False, label="unknown"):
    return Diagnosis(diagnosis=label, explanation="test", confidence=confidence, abstain=abstain,
                     proposed_action=action, evidence=["app.detail"])


def guardian_with(config, sim, clock, arm, fn):
    config.guardian.arm = arm
    config.diagnose.provider = "scripted"
    g = build_guardian(config, sim, store=Store(":memory:"), clock=clock.time, sleep=clock.sleep)
    model = ScriptedDiagnostician(fn)
    g.diagnostician = model
    g.collector.collect()
    return g, model


# -- prompt ---------------------------------------------------------------------------

def test_system_prompt_is_deterministic_and_lists_every_action():
    a, b = system_prompt(RULES), system_prompt(RULES)
    assert a == b  # a stable prefix is what makes it cacheable
    for action in ("relaunch_target", "reload_content", "reset_session", "escalate"):
        assert f"- {action} |" in a


def test_evidence_message_carries_state_and_symptoms():
    ctx = IncidentContext(state={"app": {"detail": "maintenance: back soon"}}, symptoms=["content_not_ready"],
                          rule_matches=[])
    msg = evidence_message(ctx)
    assert "maintenance: back soon" in msg and "content_not_ready" in msg


def test_schema_is_strict():
    schema = output_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


def test_diagnosis_rejects_actions_outside_the_catalog_and_bad_confidence():
    with pytest.raises(ValueError):
        diag(action="rm_rf")
    with pytest.raises(ValueError):
        diag(confidence=1.5)


# -- arms -----------------------------------------------------------------------------

def test_hybrid_asks_the_model_only_when_no_rule_matches(config, sim, clock):
    g, model = guardian_with(config, sim, clock, "hybrid", lambda c: diag())
    FAULTS["app_crash"].inject(sim, TARGET)
    assert g.tick().outcome == "recovered"
    assert model.calls == []  # a rule matched
    sim.testbed.set_mode("maintenance", for_s=20)
    clock.sleep(6)
    report = g.tick()
    assert model.calls and report.outcome == "recovered"
    assert report.incident_type == "unknown" and report.llm_calls >= 1


def test_llm_only_asks_the_model_even_when_a_rule_matches(config, sim, clock):
    g, model = guardian_with(config, sim, clock, "llm_only", lambda c: diag("relaunch_target", 0.9, label="app_crash"))
    FAULTS["app_crash"].inject(sim, TARGET)
    report = g.tick()
    assert model.calls and report.outcome == "recovered"
    assert [a["source"] for a in g.store.actions_for(report.id)] == ["model:scripted"]


def test_abstention_escalates_without_acting(config, sim, clock):
    g, _ = guardian_with(config, sim, clock, "llm_only", lambda c: diag("escalate", 0.9, abstain=True))
    FAULTS["app_crash"].inject(sim, TARGET)
    report = g.tick()
    assert report.outcome == "escalated" and report.attempts == 0
    assert "model abstained" in g.store.actions_for(report.id)[0]["reason"]


def test_model_failure_escalates(config, sim, clock):
    g, _ = guardian_with(config, sim, clock, "llm_only", lambda c: diag())
    g.diagnostician = SimpleNamespace(name="broken", diagnose=lambda c: DiagnosisResult("broken", None, error="timeout"))
    FAULTS["app_crash"].inject(sim, TARGET)
    report = g.tick()
    assert report.outcome == "escalated"
    assert "diagnostician unavailable: timeout" in g.store.actions_for(report.id)[0]["reason"]


def test_policy_still_gates_a_model_below_the_confidence_bar(config, sim, clock):
    # Medium impact needs 0.7; the model is at 0.6, so the restart must not run.
    g, _ = guardian_with(config, sim, clock, "llm_only", lambda c: diag("restart_target", 0.6))
    FAULTS["app_crash"].inject(sim, TARGET)
    report = g.tick()
    assert report.outcome == "escalated" and report.attempts == 0
    assert "confidence 0.60 below 0.70" in g.store.actions_for(report.id)[0]["reason"]


def test_diagnosis_is_settled_with_its_outcome(config, sim, clock):
    g, _ = guardian_with(config, sim, clock, "llm_only", lambda c: diag("relaunch_target", 0.9))
    FAULTS["app_crash"].inject(sim, TARGET)
    report = g.tick()
    rows = g.store.diagnoses_for(report.id)
    assert rows[0]["executed_action"] == "relaunch_target" and rows[0]["recovered_after"] == 1


def test_arm_without_a_diagnostician_is_refused(config, sim, clock):
    from homeostat.guardian.loop import Guardian

    g = build_guardian(config, sim, store=Store(":memory:"))
    hybrid = config.guardian.model_copy(update={"arm": "hybrid"})
    with pytest.raises(ValueError, match="needs a diagnostician"):
        Guardian(g.collector, g.rules, g.policy, g.executor, g.oracle, g.store, config=hybrid)


# -- Claude provider (fake client: no network, no key) ----------------------------------

def _response(text, stop="end_turn", model="claude-opus-5"):
    usage = SimpleNamespace(input_tokens=900, output_tokens=120, cache_read_input_tokens=3000,
                            cache_creation_input_tokens=0)
    content = [SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(content=content, usage=usage, stop_reason=stop, model=model, stop_details=None)


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        return self.response


def test_claude_request_shape_and_parsing():
    answer = json.dumps(diag("observe", 0.8, label="planned_maintenance").model_dump())
    client = FakeClient(_response(answer))
    result = ClaudeDiagnostician(RULES, client=client).diagnose(IncidentContext(state={}, symptoms=[], rule_matches=[]))
    request = client.requests[0]
    assert request["model"] == "claude-opus-5"
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert request["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in request["betas"]
    assert result.diagnosis.diagnosis == "planned_maintenance" and not result.abstained
    assert result.usage.cache_read_tokens == 3000 and result.usage.cost_usd > 0


def test_claude_refusal_and_invalid_output_become_failures():
    refused = ClaudeDiagnostician(RULES, client=FakeClient(_response("", stop="refusal")))
    assert refused.diagnose(IncidentContext({}, [], [])).error.startswith("refused")
    garbage = ClaudeDiagnostician(RULES, client=FakeClient(_response('{"diagnosis": "nonsense"}')))
    result = garbage.diagnose(IncidentContext({}, [], []))
    assert result.diagnosis is None and result.error.startswith("invalid output")


def test_cost_counts_cache_reads_at_a_tenth():
    uncached = cost_usd("claude-opus-5", Usage(input_tokens=1_000_000))
    cached = cost_usd("claude-opus-5", Usage(cache_read_tokens=1_000_000))
    assert uncached == pytest.approx(5.0) and cached == pytest.approx(0.5)
