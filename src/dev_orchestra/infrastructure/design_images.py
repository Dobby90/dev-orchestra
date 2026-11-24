"""Validate and preserve design inputs independently from Git worktrees."""

import json
import os
import stat
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

from dev_orchestra.domain.models import MAX_DESIGN_IMAGE_BYTES, DesignImage, PipelineError
from dev_orchestra.infrastructure.files import contained, digest, write

MAX_PIXELS = 4096 * 4096
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
BIT_DEPTHS = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}


def design_image_binding(run):
    images = run.get("design_images")
    if images:
        return {"design_image_hash": digest(json.dumps(images, sort_keys=True).encode())}
    return {}


def validate_png(data: bytes):
    if len(data) > MAX_DESIGN_IMAGE_BYTES:
        raise PipelineError("design reference exceeds 5 MiB")
    if not data.startswith(PNG_SIGNATURE) or data[8:16] != b"\0\0\0\rIHDR" or len(data) < 33:
        raise PipelineError("design reference requires a valid PNG")
    width, height, depth, color, compression, filtering, interlace = struct.unpack_from(
        ">IIBBBBB", data, 16
    )
    if (
        not 0 < width * height <= MAX_PIXELS
        or depth not in BIT_DEPTHS.get(color, ())
        or compression
        or filtering
        or interlace not in (0, 1)
    ):
        raise PipelineError("unsupported PNG header or dimensions")
    offset = len(PNG_SIGNATURE)
    has_pixels = False
    while offset + 12 <= len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        body_end = offset + 8 + length
        if body_end + 4 > len(data):
            break
        body = memoryview(data)[offset + 4 : body_end]
        if zlib.crc32(body) != struct.unpack_from(">I", data, body_end)[0]:
            break
        kind = data[offset + 4 : offset + 8]
        if kind == b"IDAT" and length:
            has_pixels = True
        if kind == b"IEND":
            if has_pixels and length == 0 and body_end + 4 == len(data):
                return
            break
        offset = body_end + 4
    raise PipelineError("design reference has invalid PNG chunks")


def read_png_bytes(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_DESIGN_IMAGE_BYTES:
            raise PipelineError("design reference must be a regular file up to 5 MiB")
        data = stream.read(MAX_DESIGN_IMAGE_BYTES + 1)
    if len(data) > MAX_DESIGN_IMAGE_BYTES:
        raise PipelineError("design reference exceeds 5 MiB")
    return data


@dataclass(frozen=True)
class DesignImageInput:
    image: DesignImage
    data: bytes

    @classmethod
    def create(cls, name: str, data: bytes):
        validate_png(data)
        return cls(DesignImage(name=name, sha256=digest(data), bytes=len(data)), data)

    @classmethod
    def from_path(cls, path: Path):
        return cls.create(path.name, read_png_bytes(path.expanduser()))


def validate_image_set(images):
    if len({image.name for image in images}) != len(images):
        raise PipelineError("design reference filenames must be unique")
    if len({image.sha256 for image in images}) != len(images):
        raise PipelineError("attach each design image only once")


def snapshot_images(directory: Path, inputs: tuple[DesignImageInput, ...]):
    images = [item.image for item in inputs]
    validate_image_set(images)
    for item in inputs:
        path = contained(directory, f"design-images/{item.image.sha256}.png")
        write(path, item.data)
    return [image.model_dump() for image in images]


def read_verified_image(path: Path, image: DesignImage) -> bytes:
    data = read_png_bytes(path)
    if len(data) != image.bytes or digest(data) != image.sha256:
        raise PipelineError("design image checksum mismatch")
    return data


def run_image_path(run, image: DesignImage):
    return contained(Path(run["directory"]), f"design-images/{image.sha256}.png")


def verified_run_images(run) -> tuple[Path, ...]:
    paths = []
    for value in run.get("design_images", []):
        image = DesignImage.model_validate(value)
        try:
            path = run_image_path(run, image)
            read_verified_image(path, image)
        except (OSError, PipelineError) as error:
            raise PipelineError(f"missing or changed design reference [{image.name}]") from error
        paths.append(path)
    return tuple(paths)
