"""Atomic workspace preferences; only new runs resolve these defaults."""

import json
import threading
import time

from pydantic import ValidationError

from dev_orchestra.application.errors import ApplicationError
from dev_orchestra.config import ROLES, AgentSettings
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.files import digest, write
from dev_orchestra.infrastructure.locking import lock


class AgentSettingsService:
    def __init__(self, config):
        self.config = config
        self.path = config.storage.database.with_suffix(".agents.json")
        self._catalog = None
        self._catalog_at = 0
        self._catalog_lock = threading.Lock()

    def models(self, refresh=False):
        from dev_orchestra.infrastructure.codex_catalog import list_models

        with self._catalog_lock:
            if refresh or self._catalog is None or time.monotonic() - self._catalog_at > 300:
                try:
                    self._catalog = list_models(self.config.codex.command)
                except (PipelineError, OSError) as error:
                    raise ApplicationError(
                        "CODEX_CATALOG_UNAVAILABLE", "Codex model catalog unavailable"
                    ) from error
                self._catalog_at = time.monotonic()
            return self._catalog

    def get(self):
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            settings = AgentSettings.model_validate(
                self.config.codex.model_dump(include=set(AgentSettings.model_fields))
            )
            return {"version": settings_fingerprint(self.config), "settings": settings}
        if len(raw) > 65536:
            raise PipelineError("agent settings exceed safety limit")
        try:
            settings = AgentSettings.model_validate_json(raw)
        except ValidationError as error:
            raise PipelineError("invalid saved agent settings") from error
        return {"version": digest(raw), "settings": settings}

    def apply(self, config):
        snapshot = config.model_dump()
        snapshot["codex"].update(self.get()["settings"].model_dump())
        return type(config).model_validate(snapshot)

    def save(self, settings, expected_version, models):
        by_model = {m["model"]: m for m in models}
        for effective in [settings, *(settings.for_role(role) for role in ROLES)]:
            if effective.model and effective.model not in by_model:
                raise ApplicationError("VALIDATION_ERROR", "model is not in the Codex catalog")
            if effective.reasoning_effort:
                model = by_model.get(effective.model) if effective.model else None
                if model is None or effective.reasoning_effort not in model["reasoning_efforts"]:
                    raise ApplicationError(
                        "VALIDATION_ERROR",
                        "select a model supporting the requested reasoning effort",
                    )
        with lock(self.path.with_suffix(".lock"), wait=True):
            if self.get()["version"] != expected_version:
                raise ApplicationError("VERSION_CONFLICT", "agent settings changed")
            write(self.path, json.loads(settings.model_dump_json()))
            return self.get()


def settings_fingerprint(config):
    value = config.codex.model_dump(include=set(AgentSettings.model_fields))
    return digest(json.dumps(value, sort_keys=True).encode())
