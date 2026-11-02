"""A private inherited pipe binds owned services to their supervisor, never to disk PIDs."""

import os
import threading
from contextlib import contextmanager


@contextmanager
def parent_channel():
    read_fd, write_fd = os.pipe()
    try:
        yield read_fd
    finally:
        os.close(write_fd)
        os.close(read_fd)


def watch_parent(read_fd, stop):
    def watch():
        try:
            while os.read(read_fd, 1):
                pass
        finally:
            os.close(read_fd)
            stop()

    threading.Thread(target=watch, name="web-supervisor-lifetime", daemon=True).start()
