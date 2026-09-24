"""Transport boundary. Everything above this layer talks to a device only through `shell`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class DeviceUnreachable(RuntimeError):
    """The host lost its link to the device. Not a device fault: never recover from it,
    never score it. Raised instead of returning empty output that would look like
    "process not running" to the collector."""


@dataclass(frozen=True)
class ShellResult:
    stdout: str
    returncode: int

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class Device(Protocol):
    serial: str

    def shell(self, command: str, timeout: float = 10.0) -> ShellResult:
        """Run a command in the device shell."""
        ...
