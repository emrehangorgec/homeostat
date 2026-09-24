"""Control the testbed backend from fault injectors, over HTTP or inside the simulator."""

from __future__ import annotations

import json
import urllib.request
from typing import Protocol


class Testbed(Protocol):
    def set_mode(self, mode: str, for_s: float | None = None, once: bool = False) -> str: ...

    def reset(self) -> None: ...


class HttpTestbed:
    def __init__(self, url: str = "http://127.0.0.1:8080"):
        self.url = url.rstrip("/")

    def set_mode(self, mode: str, for_s: float | None = None, once: bool = False) -> str:
        query = f"mode={mode}" + (f"&for={for_s}" if for_s else "") + ("&once=1" if once else "")
        request = urllib.request.Request(f"{self.url}/_control?{query}", method="POST")
        with urllib.request.urlopen(request, timeout=5) as response:
            state = json.loads(response.read())
        return f"backend {state['mode']}" + (" (once)" if once else "") + (f" for {for_s:g}s" if for_s else "")

    def reset(self) -> None:
        self.set_mode("ok")
