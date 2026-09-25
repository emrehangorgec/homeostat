"""A local model as the diagnostician, through Ollama's HTTP API (plan: the offline fallback).

Same contract, same prompt, same JSON schema as the cloud diagnostician, so the two are
directly comparable: how much correctness is lost when the cloud is unreachable, and at
what latency. No cost is recorded (it runs on the host), latency is.

Ollama's default context window (2048 tokens) is shorter than the prompt (about 2100 tokens)
and would silently truncate it. 4096 leaves room for the answer; on the reference host
(GTX 1650, 4 GB) qwen3:4b then runs 67% on the GPU at ~22 s per call, against 55% and
~30 s with 8192.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from pydantic import ValidationError

from homeostat.detect.rules import RulePack
from homeostat.diagnose.base import Diagnosis, DiagnosisResult, IncidentContext, Usage, output_schema
from homeostat.diagnose.prompt import evidence_message, system_prompt


class OllamaDiagnostician:
    def __init__(
        self,
        rules: RulePack,
        model: str = "qwen3:4b",
        url: str = "http://127.0.0.1:11434",
        think: bool = False,
        num_ctx: int = 4096,
        timeout_s: float = 300.0,
    ):
        self.model = model
        self.name = f"ollama:{model}"
        self.url = url.rstrip("/")
        self.think = think
        self.num_ctx = num_ctx
        self.timeout_s = timeout_s
        self._system = system_prompt(rules)

    def diagnose(self, context: IncidentContext) -> DiagnosisResult:
        started = time.monotonic()
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self._system},
                {"role": "user", "content": evidence_message(context)},
            ],
            "format": output_schema(),
            "think": self.think,
            "stream": False,
            "options": {"temperature": 0, "num_ctx": self.num_ctx},
        }
        request = urllib.request.Request(
            f"{self.url}/api/chat", data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                data = json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            return DiagnosisResult(self.name, None, Usage(latency_s=time.monotonic() - started), f"ollama unreachable: {e}")

        usage = Usage(
            input_tokens=data.get("prompt_eval_count") or 0,
            output_tokens=data.get("eval_count") or 0,
            latency_s=time.monotonic() - started,
        )
        text = (data.get("message") or {}).get("content", "")
        try:
            diagnosis = Diagnosis.model_validate(json.loads(text))
        except (ValueError, ValidationError) as e:
            return DiagnosisResult(self.name, None, usage, f"invalid output: {str(e)[:200]}")
        return DiagnosisResult(self.name, diagnosis, usage)
