"""Directory identities that survive macOS volume device renumbering."""

import ctypes
import errno
import os
import struct
import sys
import uuid
from os import fstat
from pathlib import Path


def volume_uuid(fd):
    if sys.platform != "darwin":
        return None
    read = ctypes.CDLL(None, use_errno=True).fgetattrlist
    read.argtypes = [
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_ulong,
    ]
    # struct attrlist: ATTR_BIT_MAP_COUNT, reserved, then five attribute masks.
    attr_vol_info = 0x80000000
    attr_vol_uuid = 0x00040000
    attributes = struct.pack("=HHIIIII", 5, 0, 0, attr_vol_info | attr_vol_uuid, 0, 0, 0)
    result = ctypes.create_string_buffer(20)
    if read(fd, attributes, result, len(result), 0):
        error = ctypes.get_errno()
        if error in {errno.EINVAL, errno.ENOTSUP, errno.ENOSYS}:
            return None
        raise OSError(error, os.strerror(error))
    if struct.unpack_from("=I", result.raw)[0] != len(result):
        return None
    identifier = uuid.UUID(bytes=result.raw[4:])
    return str(identifier) if identifier.int else None


def directory_identity(path):
    fd = os.open(Path(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        stat = fstat(fd)
        value = {"device": stat.st_dev, "inode": stat.st_ino}
        if identifier := volume_uuid(fd):
            value.update(volume_uuid=identifier, birthtime=stat.st_birthtime)
        return value
    finally:
        os.close(fd)


def same_directory(expected, current):
    if not isinstance(expected, dict) or expected.get("inode") != current["inode"]:
        return False
    if "volume_uuid" in expected:
        return (
            bool(expected["volume_uuid"])
            and expected["volume_uuid"] == current.get("volume_uuid")
            and "birthtime" in expected
            and expected["birthtime"] == current.get("birthtime")
        )
    return expected.get("device") == current["device"]
