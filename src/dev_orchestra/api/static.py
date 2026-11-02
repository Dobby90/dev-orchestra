"""Manifest-only GUI files; API misses and arbitrary filesystem paths never fall back."""

import json
import re
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse

from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.files import contained, digest

CSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"


def bundle(directory=None, required=False):
    root = Path(directory) if directory else Path(__file__).resolve().parents[1] / "_web"
    manifest = root / "manifest.json"
    if not manifest.is_file():
        if required:
            raise PipelineError(
                "GUI assets missing; run python scripts/build_web.py before web start"
            )
        return None
    try:
        values = json.loads(contained(root, "manifest.json").read_text())
        if "index.html" not in values or any(
            name != "index.html"
            and not re.fullmatch(r"assets/[A-Za-z0-9_.-]+\.(js|css|woff2?|png|svg|ico)", name)
            for name in values
        ):
            raise ValueError()
        for name, sha in values.items():
            if digest(contained(root, name).read_bytes()) != sha:
                raise ValueError()
    except (OSError, ValueError, PipelineError) as error:
        raise PipelineError("GUI bundle missing or modified") from error
    return root, values


def attach(app, directory=None):
    verified = bundle(directory)
    if verified is None:
        return
    root, values = verified

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def static(path: str):
        if path in values and path != "index.html":
            target = contained(root, path)
            if digest(target.read_bytes()) != values[path]:
                raise HTTPException(409, "GUI file changed")
            return FileResponse(
                target,
                headers={
                    "Cache-Control": "public, max-age=31536000, immutable",
                    "Content-Security-Policy": CSP,
                },
            )
        if (
            path == ""
            or path in {"projects", "runs/new", "settings"}
            or re.fullmatch(r"projects/prj-[0-9a-f]{32}|runs/run-[0-9a-f]{16}", path)
        ):
            target = contained(root, "index.html")
            if digest(target.read_bytes()) != values["index.html"]:
                raise HTTPException(409, "GUI file changed")
            return FileResponse(
                target,
                media_type="text/html",
                headers={"Cache-Control": "no-store", "Content-Security-Policy": CSP},
            )
        raise HTTPException(404, "not found")
