"""Keep real environment files private; permit a narrow public example format."""

import os
import re
from pathlib import Path

from dev_orchestra.infrastructure.process import redact


def environment_example_path(path):
    path = Path(path)
    return path.name == ".env.example" and not any(
        part.startswith(".env") for part in path.parts[:-1]
    )


def public_environment_example(path, content):
    if not environment_example_path(path):
        return False
    if not isinstance(content, str) or len(content.encode()) > 65536 or redact(content) != content:
        return False
    for line in content.splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Z][A-Z0-9_]*)=([^\x00\r\n]*)", value)
        if not match:
            return False
        key, default = match.groups()
        # Credential-like settings may be documented only with an empty value.
        if (
            re.search(r"SECRET|TOKEN|PASSWORD|PASSWD|COOKIE|CREDENTIAL|PRIVATE|KEY|AUTH|USER", key)
            and default
        ):
            return False
        if any(char in default for char in ("$", "`", ";", "\\", "@")):
            return False
    return True


def secret_name(name):
    return name.startswith(".env") or (
        name.endswith((".pem", ".key")) and Path(name).suffix in {".pem", ".key"}
    )


def secret_path(path):
    return any(secret_name(part) for part in Path(path).parts)


def denied_subpaths(worktree):
    """One rule covers a directory; nested symlinks also need their canonical targets denied."""
    pending = [(worktree, False)]
    while pending:
        directory, denied = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                child_denied = denied
                if denied:
                    if entry.is_symlink():
                        yield Path(entry.path)
                elif secret_name(entry.name):
                    path = Path(entry.path)
                    if not readable_environment_example(path, path.relative_to(worktree)):
                        yield path
                        child_denied = True
                if entry.is_dir(follow_symlinks=False):
                    pending.append((entry.path, child_denied))


def readable_environment_example(path, relative):
    if (
        not environment_example_path(relative)
        or path.is_symlink()
        or not path.is_file()
        or path.stat().st_size > 65536
    ):
        return False
    try:
        return public_environment_example(relative, path.read_text())
    except (OSError, UnicodeError):
        return False
