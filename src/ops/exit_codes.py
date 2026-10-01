"""Process exit codes shared by every entry point, so a scheduler can tell outcomes apart."""

from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0
    CRASH = 1
    CONFIG_ERROR = 2
    LOCK_HELD = 3
