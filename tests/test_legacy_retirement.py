"""Plan M12.1: the legacy stack is gone - and stays gone."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DELETED = ("src.api", "src.execution", "src.finops", "src.legacy", "src.market",
           "src.memory", "src.observability", "src.profit", "src.live",
           "src.engine.view_model")  # fmt: skip
IMPORT = re.compile(r"^\s*(?:from|import)\s+(src(?:\.\w+)+)", re.M)


def test_the_legacy_packages_do_not_exist():
    for module in DELETED:
        path = ROOT / Path(*module.split("."))
        assert not path.exists() and not path.with_suffix(".py").exists(), module


def test_nothing_imports_them():
    offenders = []
    for path in [
        *SRC.rglob("*.py"),
        *(ROOT / "scripts").rglob("*.py"),
        *(ROOT / "tests").glob("*.py"),
    ]:
        if path.name == Path(__file__).name:
            continue
        for module in IMPORT.findall(path.read_text(encoding="utf-8")):
            if any(module == gone or module.startswith(f"{gone}.") for gone in DELETED):
                offenders.append(f"{path.relative_to(ROOT)}: {module}")
    assert offenders == []
