"""ADB transport. A prototype tool for M1 to M4; the production guardian runs on the device (M5)."""

from __future__ import annotations

import shutil
import subprocess

from homeostat.device.base import DeviceUnreachable, ShellResult


class AdbError(RuntimeError):
    pass


# adb's own errors, as opposed to errors of the command run on the device.
_LINK_ERRORS = ("no devices/emulators found", "device offline", "not found", "unauthorized", "closed")


def _is_link_error(stderr: str) -> bool:
    # adb prefixes its own errors with its program name, whose case varies on Windows
    # ("adb.EXE: device '...' not found", seen on the reference setup).
    lowered = stderr.lower()
    return lowered.startswith(("adb: ", "adb.exe: ", "error: ")) and any(e in lowered for e in _LINK_ERRORS)


class AdbDevice:
    def __init__(self, serial: str | None = None, adb_path: str | None = None):
        self.adb = adb_path or shutil.which("adb")
        if self.adb is None:
            raise AdbError("adb not found on PATH")
        self.serial = serial or self._single_device()

    def _base(self) -> list[str]:
        return [self.adb, "-s", self.serial]

    def _single_device(self) -> str:
        out = subprocess.run([self.adb, "devices"], capture_output=True, text=True, check=True).stdout
        serials = [line.split()[0] for line in out.splitlines()[1:] if line.strip().endswith("device")]
        if len(serials) != 1:
            raise AdbError(f"expected exactly one connected device, found {len(serials)}; pass a serial")
        return serials[0]

    def shell(self, command: str, timeout: float = 10.0) -> ShellResult:
        try:
            proc = subprocess.run(
                [*self._base(), "shell", command],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return ShellResult(stdout="", returncode=124)
        stderr = proc.stderr.strip()
        if proc.returncode != 0 and _is_link_error(stderr):
            raise DeviceUnreachable(stderr)
        return ShellResult(stdout=proc.stdout, returncode=proc.returncode)

    def shell_bytes(self, command: str, timeout: float = 20.0) -> bytes:
        # exec-out, unlike shell, passes binary output through untouched.
        try:
            proc = subprocess.run([*self._base(), "exec-out", command], capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return b""
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        if proc.returncode != 0 and _is_link_error(stderr):
            raise DeviceUnreachable(stderr)
        return proc.stdout
