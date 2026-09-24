"""The action allowlist. Nothing outside this catalog can ever be executed, whoever proposes it."""

from __future__ import annotations

from enum import IntEnum
from typing import Literal

from pydantic import BaseModel

Privilege = Literal["none", "adb", "device_owner"]


class Impact(IntEnum):
    NONE = 0  # no change to the device
    LOW = 1  # brings something to the front, user state preserved
    MEDIUM = 2  # kills a process, in-memory state lost
    HIGH = 3  # data loss or whole-device disruption


class ActionSpec(BaseModel):
    name: str
    impact: Impact
    reversible: bool
    privilege: Privilege
    description: str


CATALOG: dict[str, ActionSpec] = {
    spec.name: spec
    for spec in [
        ActionSpec(
            name="observe",
            impact=Impact.NONE,
            reversible=True,
            privilege="none",
            description="Do nothing, observe again after a delay.",
        ),
        ActionSpec(
            name="escalate",
            impact=Impact.NONE,
            reversible=True,
            privilege="none",
            description="Stop automated recovery and request human intervention.",
        ),
        ActionSpec(
            name="wake_screen",
            impact=Impact.LOW,
            reversible=True,
            privilege="adb",
            description="Wake the display.",
        ),
        ActionSpec(
            name="dismiss_system_dialogs",
            impact=Impact.LOW,
            reversible=True,
            privilege="adb",
            description="Close system dialogs (crash, ANR) covering the screen. Closing a crash "
            "dialog ends the crashed app, so a relaunch usually follows.",
        ),
        ActionSpec(
            name="dismiss_keyguard",
            impact=Impact.LOW,
            reversible=True,
            privilege="adb",
            description="Dismiss an insecure (swipe) lock screen. Never bypasses a PIN; a Device "
            "Owner agent disables the keyguard instead.",
        ),
        ActionSpec(
            name="relaunch_target",
            impact=Impact.LOW,
            reversible=True,
            privilege="adb",
            description="Start the target activity, or bring it to the front if it is running.",
        ),
        ActionSpec(
            name="restart_target",
            impact=Impact.MEDIUM,
            reversible=True,
            privilege="adb",
            description="Force stop the target app and start it again.",
        ),
        ActionSpec(
            name="clear_target_data",
            impact=Impact.HIGH,
            reversible=False,
            privilege="adb",
            description="Clear the target app's data (sessions, caches, settings) and start it again.",
        ),
        ActionSpec(
            name="reboot",
            impact=Impact.HIGH,
            reversible=True,
            privilege="adb",
            description="Reboot the whole device.",
        ),
    ]
}


def spec(name: str) -> ActionSpec:
    return CATALOG[name]
