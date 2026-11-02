"""Stable error codes for adapters without HTTP status coupling."""

from dev_orchestra.domain.models import PipelineError


class ApplicationError(PipelineError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
