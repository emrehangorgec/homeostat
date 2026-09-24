from homeostat.verify.oracle import HealthOracle, OracleConfig, UiMarker
from tests.conftest import TARGET


def oracle(sim, clock, marker=True, **kw):
    cfg = OracleConfig(marker=sim.marker if marker else None, **kw)
    return HealthOracle(sim, TARGET, cfg, sleep=clock.sleep)


def test_marker_pattern_requires_all_fields_on_one_node():
    m = UiMarker(text="ready", resource_id="app:id/status")
    assert m.pattern().search('<node text="ready" resource-id="app:id/status" />')
    assert not m.pattern().search('<node text="ready" /><node resource-id="app:id/status" />')


def test_healthy_with_marker_is_strong(sim, clock):
    result = oracle(sim, clock).verify()
    assert result.healthy and result.strength == "strong"
    assert result.checks == {"foreground": True, "process": True, "ui_marker": True, "visual": True, "stable": True}


def test_without_marker_verdict_is_weak(sim, clock):
    result = oracle(sim, clock, marker=False).verify()
    assert result.healthy and result.strength == "weak"


def test_blank_ui_is_unhealthy_even_though_the_app_reports_ready(sim, clock):
    sim.s.blank_ui = True  # the kiosk does not notice: health contract and marker still say ready
    result = oracle(sim, clock).verify()
    assert not result.healthy
    assert result.checks["ui_marker"] is True and result.checks["visual"] is False


def test_restart_during_window_fails_stability(sim, clock):
    o = oracle(sim, clock, stable_for_s=4, sample_every_s=2)
    calls = {"n": 0}
    original = o._snapshot

    def flapping():
        calls["n"] += 1
        if calls["n"] == 2:  # the process dies and comes back with a new pid
            sim.shell(f"am broadcast -p {TARGET.package} -a com.homeostat.contract.RESTART")
        return original()

    o._snapshot = flapping
    result = o.verify()
    assert not result.healthy and result.checks["stable"] is False
