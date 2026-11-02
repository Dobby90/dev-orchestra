import sys

from dev_orchestra import __version__


def test_supported_runtime():
    assert sys.version_info >= (3, 11)
    assert __version__ == "0.1.0"
