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


def test_a_silent_heartbeat_in_the_background_is_not_a_hang(pack):
    settings = {"package": "com.android.settings", "activity": "com.android.settings.Settings"}
    background = make_state(foreground=settings, app={"state": "ready", "heartbeat_age_s": 90.0})
    assert "heartbeat_stale" not in pack.detect(background).symptoms
    front = make_state(app={"state": "ready", "heartbeat_age_s": 90.0})
    assert pack.detect(front).best.rule.id == "app_hung"


SETTINGS = {"package": "com.android.settings", "activity": "com.android.settings.Settings"}


def test_recent_touch_contests_the_wrong_foreground_rule(pack):
    detection = pack.detect(make_state(foreground=SETTINGS, screen={"awake": True, "last_user_activity_s": 2.0}))
    assert detection.best.rule.id == "wrong_foreground"
    assert [h.contest.id for h in detection.contested(detection.best)] == ["person_using_device"]


def test_no_contest_without_a_recent_touch(pack):
    detection = pack.detect(make_state(foreground=SETTINGS, screen={"awake": True, "last_user_activity_s": 3600.0}))
    assert detection.best.rule.id == "wrong_foreground" and detection.contested(detection.best) == []


def test_a_rule_that_reads_every_contested_path_is_not_contested(pack, tmp_path):
    path = tmp_path / "user.toml"
    path.write_text(
        'schema_version = "1"\n'
        '[[rule]]\nid = "leave_the_person"\nincident = "user_intent"\naction = "escalate"\npriority = 99\n'
        'when = [\n'
        ' { path = "screen.last_user_activity_s", op = "lt", value = 30.0 },\n'
        ' { path = "target_in_foreground", op = "eq", value = false },\n'
        ' { path = "screen.awake", op = "eq", value = true },\n'
        ']\n',
        encoding="utf-8",
    )
    detection = RulePack.load(DEFAULT_RULES, path).detect(make_state(foreground=SETTINGS, screen={"awake": True, "last_user_activity_s": 2.0}))
    assert detection.best.rule.id == "leave_the_person" and detection.contested(detection.best) == []
