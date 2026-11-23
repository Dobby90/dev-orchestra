"""Commit descriptions shared by legacy patches and workspace checkpoints."""

import re

from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.process import sanitize


def validate_commit_message(message):
    subject = message.strip().split("\n", 1)[0]
    if (
        not re.search("[가-힣]", subject)
        or re.match(r"^(자동 개발|역할 작업 완료|테스트 실행 전 변경 확정)(?:\s|:|$)", subject)
        or "\0" in message
        or sanitize(message) != message
    ):
        raise PipelineError("commit requires a concrete Korean subject without sensitive content")


def format_branch_commit(message, branch, *, version):
    message = message.strip()
    prefix = ""
    # Match the historical prefixes first to preserve their commit subjects.
    bases = ("feature/codex/", "feature/ai/")
    if version == 3:
        bases += ("feature/",)
    elif version == 4:
        bases += ("features/",)
    for base in bases:
        if branch.startswith(base) and (suffix := branch.removeprefix(base)):
            prefix = f"[{suffix}] "
            break
    if prefix and message.startswith(prefix):
        message = message.removeprefix(prefix)
    # Validate the description itself so a prefix cannot hide a generic subject.
    validate_commit_message(message)
    message = prefix + message
    validate_commit_message(message)
    return message


def describe_commit(message, summary, paths):
    # Old stored responses and host clients may omit the new description field.
    # Keep those usable without reusing execution metadata as the subject.
    if not message.strip():
        message = ", ".join(sorted(paths)) + " 변경 반영"
    validate_commit_message(message)
    if "\n\n" not in message.strip() and summary.strip():
        message = message.strip() + "\n\n" + summary.strip()
    validate_commit_message(message)
    return message.strip()
