"""Host-collected token accounting, independent of agent response and checkpoints."""

import time
from typing import Literal

from pydantic import Field, model_validator

from dev_orchestra.config import AgentOptions
from dev_orchestra.domain.models import Model
from dev_orchestra.infrastructure.files import write


class TokenUsage(Model):
    input_tokens: int = Field(ge=0, strict=True)
    cached_input_tokens: int = Field(ge=0, strict=True)
    output_tokens: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def cache_is_subset(self):
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached tokens exceed input tokens")
        return self


class AgentTelemetry(AgentOptions):
    usage: TokenUsage | None = None
    usage_status: Literal["reported", "partial", "unknown"] = "unknown"
    completed_turns: int = Field(default=0, ge=0, strict=True)
    duration_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class UsageCollector:
    def __init__(self, path, options):
        self.path = path
        self.value = AgentTelemetry(**options.model_dump())
        self.incomplete = False
        self.started = time.monotonic()
        write(path, self.value.model_dump())

    def event(self, event):
        if event.get("type") in {"turn.failed", "error"}:
            self.incomplete = True
        if event.get("type") != "turn.completed":
            return
        try:
            # Only documented accounting fields; never persist model messages or reasoning.
            usage = TokenUsage.model_validate(
                {key: event["usage"][key] for key in TokenUsage.model_fields}
            )
        except (ValueError, KeyError, TypeError):
            self.incomplete = True
            return
        previous = self.value.usage
        self.value.usage = TokenUsage(
            **{
                key: getattr(usage, key) + (getattr(previous, key) if previous else 0)
                for key in TokenUsage.model_fields
            }
        )
        self.value.completed_turns += 1
        self.value.usage_status = "partial"
        write(self.path, self.value.model_dump())

    def finish(self, succeeded):
        self.value.duration_seconds = time.monotonic() - self.started
        self.value.usage_status = (
            ("reported" if succeeded and not self.incomplete else "partial")
            if self.value.usage is not None
            else "unknown"
        )
        write(self.path, self.value.model_dump())
