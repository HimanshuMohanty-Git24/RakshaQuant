"""The CI mypy ratchet (scripts/ci/mypy_ratchet.py) must never pass an incomplete mypy run."""

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ci" / "mypy_ratchet.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mypy_ratchet_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(stdout: str, returncode: int, ceiling: str = "374") -> int:
    fake = subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")
    with patch("subprocess.run", return_value=fake):
        return int(_load().main([ceiling]))


@pytest.mark.parametrize(
    ("stdout", "returncode", "expected"),
    [
        ("Found 374 errors in 39 files (checked 79 source files)\n", 1, 0),
        ("Found 12 errors in 3 files (checked 79 source files)\n", 1, 0),
        ("Success: no issues found in 79 source files\n", 0, 0),
        ("Found 375 errors in 39 files (checked 79 source files)\n", 1, 1),
        # A blocking error stops mypy early: "1 error" must not pass under the ceiling.
        (
            "x.py:1: error: Invalid syntax\nFound 1 error in 1 file (errors prevented further checking)\n",
            2,
            1,
        ),
        ("garbage\n", 1, 1),
    ],
)
def test_ratchet_outcomes(stdout, returncode, expected):
    assert _run(stdout, returncode) == expected


def test_bad_usage_exits_2():
    assert _load().main([]) == 2
    assert _load().main(["many"]) == 2
