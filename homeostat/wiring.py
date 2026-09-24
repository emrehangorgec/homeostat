"""Assemble a Guardian from a config and a device."""

from __future__ import annotations

import time
from collections.abc import Callable

from homeostat.act.executor import AdbExecutor
from homeostat.config import HomeostatConfig
from homeostat.detect.rules import RulePack
from homeostat.device.base import Device
from homeostat.guardian.loop import Guardian
from homeostat.policy.engine import Policy
from homeostat.state.collect import Collector
from homeostat.store.sqlite import Store
from homeostat.verify.oracle import HealthOracle


class SimClock:
    """Virtual time for the simulator: sleeping advances the clock instantly."""

    def __init__(self, start: float = 1_700_000_000.0):
        self.now = start

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def build_guardian(
    config: HomeostatConfig,
    device: Device,
    store: Store | None = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> Guardian:
    return Guardian(
        collector=Collector(device, config.target, mode=config.mode),
        rules=RulePack.load(*config.all_rule_packs()),
        policy=Policy(config.policy),
        executor=AdbExecutor(device, config.target),
        oracle=HealthOracle(device, config.target, config.oracle, sleep=sleep),
        store=store or Store(config.store),
        config=config.guardian,
        clock=clock,
        sleep=sleep,
    )
