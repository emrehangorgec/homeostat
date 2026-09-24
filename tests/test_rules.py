import pytest
from pydantic import ValidationError

from homeostat.config import DEFAULT_RULES
from homeostat.detect.rules import Condition, RulePack, Rule
from tests.conftest import make_state


@pytest.fixture
def pack() -> RulePack:
    return RulePack.load(DEFAULT_RULES)


def test_healthy_state_has_no_symptoms(pack):
    assert pack.detect(make_state()).symptoms == []


def test_unknown_signal_never_fires(pack):
    state = make_state(target_process={"running": None}, screen={"awake": None}, foreground=None)
    detection = pack.detect(state)
    assert detection.symptoms == []
    assert detection.matches == []


def test_is_null_is_the_only_op_true_on_unknown():
    state = make_state(screen={"awake": None})
    assert Condition(path="screen.awake", op="is_null").holds(state)
    for op in ("eq", "ne", "lt", "ge", "not_null"):
        assert not Condition(path="screen.awake", op=op, value=False).holds(state)


def test_crash_is_classified(pack):
    state = make_state(target_process={"running": False}, foreground=None)
    detection = pack.detect(state)
    assert "target_not_running" in detection.symptoms
    assert detection.best.rule.id == "app_not_running"
    assert "target_process.running=False (eq False)" in detection.best.evidence


def test_screen_rule_outranks_crash_rule(pack):
    state = make_state(target_process={"running": False}, screen={"awake": False}, foreground=None)
    assert [m.rule.id for m in pack.detect(state).matches] == ["screen_asleep"]


def test_no_internet_is_unclassified(pack):
    detection = pack.detect(make_state(network={"internet_reachable": False}))
    assert detection.unhealthy and not detection.classified


def test_computed_field_is_addressable():
    other = {"package": "com.android.settings", "activity": "com.android.settings.Settings"}
    assert make_state(foreground=other).get("target_in_foreground") is False


def test_unknown_path_is_rejected():
    with pytest.raises(KeyError):
        make_state().get("target_process.colour")


def test_rule_with_unknown_action_is_rejected():
    with pytest.raises(ValidationError):
        Rule(id="x", incident="x", action="rm_rf", when=[{"path": "screen.awake", "op": "eq", "value": True}])


def test_pack_for_other_schema_version_is_refused(tmp_path):
    path = tmp_path / "old.toml"
    path.write_text('schema_version = "0"\n', encoding="utf-8")
    with pytest.raises(ValidationError, match="DeviceState v0"):
        RulePack.load(path)


def test_user_pack_is_merged(tmp_path):
    path = tmp_path / "net.toml"
    path.write_text(
        'schema_version = "1"\n'
        "[[rule]]\n"
        'id = "offline_wait"\nincident = "network_down"\naction = "observe"\n'
        'when = [{ path = "network.internet_reachable", op = "eq", value = false }]\n',
        encoding="utf-8",
    )
    pack = RulePack.load(DEFAULT_RULES, path)
    detection = pack.detect(make_state(network={"internet_reachable": False}))
    assert detection.best.rule.id == "offline_wait"
