"""homeostat.toml: one file configures target, oracle, guardian timing, policy and rule packs."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from homeostat.guardian.loop import GuardianConfig
from homeostat.policy.engine import PolicyConfig
from homeostat.state.schema import ActivityRef, Mode
from homeostat.verify.oracle import OracleConfig

DEFAULT_RULES = Path(__file__).parent / "detect" / "default_rules.toml"


class DiagnoseConfig(BaseModel):
    provider: Literal["claude", "ollama", "scripted"] = "claude"
    model: str = "claude-opus-5"  # for ollama, a local model tag such as "qwen3:4b"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"  # claude only
    ollama_url: str = "http://127.0.0.1:11434"
    think: bool = False  # ollama only: let a reasoning model think before answering


class TestbedConfig(BaseModel):
    url: str = "http://127.0.0.1:8080"  # where the host reaches `homeostat backend`


class HomeostatConfig(BaseModel):
    mode: Mode = Mode.PANEL
    store: Path = Path("homeostat.sqlite")
    target: ActivityRef
    rule_packs: list[Path] = Field(default_factory=list)  # added on top of the built-in pack
    oracle: OracleConfig = Field(default_factory=OracleConfig)
    guardian: GuardianConfig = Field(default_factory=GuardianConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    testbed: TestbedConfig | None = None  # set to run backend faults against a real device
    diagnose: DiagnoseConfig = Field(default_factory=DiagnoseConfig)  # used by the hybrid and llm_only arms

    @classmethod
    def load(cls, path: str | Path) -> HomeostatConfig:
        path = Path(path)
        config = cls.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))
        config.rule_packs = [p if p.is_absolute() else path.parent / p for p in config.rule_packs]
        return config

    def all_rule_packs(self) -> list[Path]:
        return [DEFAULT_RULES, *self.rule_packs]
