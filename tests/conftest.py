from datetime import datetime, timezone

import pytest

from homeostat.config import HomeostatConfig
from homeostat.device.sim import SimDevice
from homeostat.state.schema import ActivityRef, DeviceInfo, DeviceState
from homeostat.store.sqlite import Store
from homeostat.verify.oracle import UiMarker
from homeostat.wiring import SimClock, build_guardian

TARGET = ActivityRef(
    package="com.homeostat.agent",
    activity="com.homeostat.agent.KioskActivity",
    process="com.homeostat.agent:kiosk",
)


def make_state(**overrides) -> DeviceState:
    data = {
        "captured_at": datetime(2026, 9, 24, tzinfo=timezone.utc),
        "device": DeviceInfo(serial="t"),
        "target": TARGET,
        "foreground": TARGET,
        "target_process": {"running": True, "pid": 100},
        "screen": {"awake": True},
        "network": {"wifi_enabled": True, "internet_reachable": True},
    }
    data.update(overrides)
    return DeviceState.model_validate(data)


@pytest.fixture
def config() -> HomeostatConfig:
    cfg = HomeostatConfig(target=TARGET)
    cfg.oracle.marker = UiMarker(content_desc="homeostat-ready")
    cfg.oracle.heartbeat = True
    return cfg


@pytest.fixture
def clock() -> SimClock:
    return SimClock()


@pytest.fixture
def sim(config, clock) -> SimDevice:
    return SimDevice(config.target, marker=config.oracle.marker, clock=clock.time)


@pytest.fixture
def guardian(config, sim, clock):
    g = build_guardian(config, sim, store=Store(":memory:"), clock=clock.time, sleep=clock.sleep)
    g.collector.collect()  # set the crash log watermark, as a running guardian would have
    return g
