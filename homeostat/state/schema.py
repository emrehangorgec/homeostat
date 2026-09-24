"""DeviceState v1: the normalized evidence contract.

Rules, the policy layer and (later) the diagnostician all read this object, never raw
device output. `None` means "unknown" and is deliberately distinct from False: a rule
must not fire on a signal that could not be collected.

Distilled rules are written against a schema version, so any breaking change here must
bump SCHEMA_VERSION and invalidate or migrate existing rule packs.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field

SCHEMA_VERSION = "1"


class Mode(str, Enum):
    PANEL = "panel"  # guardian observes and recovers
    FREE = "free"  # normal phone use: guardian observes only, never acts


class ActivityRef(BaseModel):
    package: str
    activity: str | None = None
    process: str | None = None  # when the activity runs in its own process, e.g. "pkg:kiosk"

    @property
    def process_name(self) -> str:
        return self.process or self.package

    def matches(self, other: ActivityRef | None) -> bool:
        if other is None or other.package != self.package:
            return False
        return self.activity is None or other.activity is None or self.activity == other.activity

    @property
    def component(self) -> str:
        return f"{self.package}/{self.activity}" if self.activity else self.package


class DeviceInfo(BaseModel):
    serial: str
    model: str | None = None
    sdk: int | None = None


class ProcessInfo(BaseModel):
    running: bool | None = None
    pid: int | None = None


class Screen(BaseModel):
    awake: bool | None = None
    # Title of the window holding input focus, e.g. "Application Error: <pkg>" when the
    # system crash dialog covers everything. Window titles are not localized.
    focus_window: str | None = None
    keyguard_showing: bool | None = None  # the lock screen covers everything while True

    @computed_field
    @property
    def blocking_dialog(self) -> Literal["none", "app_error", "anr"] | None:
        if self.focus_window is None:
            return None
        if self.focus_window.startswith("Application Error:"):
            return "app_error"
        if self.focus_window.startswith("Application Not Responding:"):
            return "anr"
        return "none"


class Network(BaseModel):
    wifi_enabled: bool | None = None
    internet_reachable: bool | None = None


class Battery(BaseModel):
    level: int | None = None
    temperature_c: float | None = None
    charging: bool | None = None
    health: str | None = None


class Memory(BaseModel):
    available_mb: int | None = None


class DeviceState(BaseModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    captured_at: datetime
    mode: Mode = Mode.PANEL
    device: DeviceInfo
    target: ActivityRef
    foreground: ActivityRef | None = None
    target_process: ProcessInfo = Field(default_factory=ProcessInfo)
    screen: Screen = Field(default_factory=Screen)
    network: Network = Field(default_factory=Network)
    battery: Battery = Field(default_factory=Battery)
    memory: Memory = Field(default_factory=Memory)
    recent_errors: list[str] = Field(default_factory=list, max_length=20)

    @computed_field
    @property
    def target_in_foreground(self) -> bool | None:
        if self.foreground is None:
            return None
        return self.target.matches(self.foreground)

    def get(self, path: str) -> Any:
        """Resolve a dotted path such as `target_process.running`. Missing paths raise KeyError."""
        node: Any = self
        for part in path.split("."):
            if isinstance(node, BaseModel):
                if part not in type(node).model_fields and part not in type(node).model_computed_fields:
                    raise KeyError(path)
                node = getattr(node, part)
            elif isinstance(node, dict):
                node = node[part]
            else:
                raise KeyError(path)
        return node.value if isinstance(node, Enum) else node
