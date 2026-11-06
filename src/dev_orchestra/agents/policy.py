"""Shared path boundaries for host-owned workspace editing."""


def in_paths(path, roots):
    return any(path == root or path.startswith(root.rstrip("/") + "/") for root in roots)
