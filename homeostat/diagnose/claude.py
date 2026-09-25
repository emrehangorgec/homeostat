"""Claude as the diagnostician, through the official Anthropic SDK.

- Structured outputs (`output_config.format`, strict JSON schema): the answer always
  parses; it is validated again with pydantic (label, action, confidence range).
- Adaptive thinking with an explicit effort.
- The system prompt is stable and marked for caching; per-incident evidence goes in the
  user message. `cache_read_tokens` in the usage record shows whether caching works.
- Server-side refusal fallback (`fallbacks: "default"`): a false-positive safety decline
  is re-run on the recommended fallback model instead of failing the incident.
- Any failure (network, API error, refusal, invalid output) returns a result without a
  diagnosis: the guardian escalates, exactly as for an abstention.

Credentials: the SDK resolves ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN or an
`ant auth login` profile. Never pass a key in code.
"""

from __future__ import annotations

import json
import time

from pydantic import ValidationError

from homeostat.detect.rules import RulePack
from homeostat.diagnose.base import Diagnosis, DiagnosisResult, IncidentContext, Usage, output_schema
from homeostat.diagnose.prompt import evidence_message, system_prompt

# USD per million tokens (input, output), Anthropic first-party rates cached 2026-06-24.
# Cache writes cost 1.25x input, cache reads 0.1x input.
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

FALLBACK_BETA = "server-side-fallback-2026-07-01"


def cost_usd(model: str, usage: Usage) -> float:
    price_in, price_out = PRICES.get(model, PRICES["claude-opus-5"])
    per_token_in = price_in / 1_000_000
    return (
        usage.input_tokens * per_token_in
        + usage.cache_write_tokens * per_token_in * 1.25
        + usage.cache_read_tokens * per_token_in * 0.1
        + usage.output_tokens * price_out / 1_000_000
    )


class ClaudeDiagnostician:
    def __init__(
        self,
        rules: RulePack,
        model: str = "claude-opus-5",
        effort: str = "medium",
        max_tokens: int = 16000,
        timeout_s: float = 90.0,
        client=None,
    ):
        import anthropic  # imported lazily: rules-only users need no SDK

        self.model = model
        self.name = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.client = client or anthropic.Anthropic(timeout=timeout_s, max_retries=2)
        self._anthropic = anthropic
        self._system = [{"type": "text", "text": system_prompt(rules), "cache_control": {"type": "ephemeral"}}]

    def diagnose(self, context: IncidentContext) -> DiagnosisResult:
        anthropic = self._anthropic
        started = time.monotonic()
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                betas=[FALLBACK_BETA],
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": output_schema()}},
                system=self._system,
                messages=[{"role": "user", "content": evidence_message(context)}],
                fallbacks="default",
            )
        except anthropic.RateLimitError as e:
            return self._failed(f"rate limited: {e}", started)
        except anthropic.APIStatusError as e:
            return self._failed(f"api error {e.status_code}: {e.message}", started)
        except anthropic.APIConnectionError as e:
            return self._failed(f"connection error: {e}", started)

        usage = Usage(
            input_tokens=response.usage.input_tokens or 0,
            output_tokens=response.usage.output_tokens or 0,
            cache_read_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
            latency_s=time.monotonic() - started,
        )
        served_by = response.model
        usage.cost_usd = cost_usd(served_by, usage)

        if response.stop_reason == "refusal":
            category = getattr(getattr(response, "stop_details", None), "category", None)
            return DiagnosisResult(self.model, None, usage, f"refused (category {category})", served_by)
        if response.stop_reason == "max_tokens":
            return DiagnosisResult(self.model, None, usage, "output truncated at max_tokens", served_by)

        text = next((block.text for block in response.content if block.type == "text"), "")
        try:
            diagnosis = Diagnosis.model_validate(json.loads(text))
        except (ValueError, ValidationError) as e:
            return DiagnosisResult(self.model, None, usage, f"invalid output: {e}", served_by)
        return DiagnosisResult(self.model, diagnosis, usage, None, served_by)

    def _failed(self, error: str, started: float) -> DiagnosisResult:
        return DiagnosisResult(self.model, None, Usage(latency_s=time.monotonic() - started), error)
