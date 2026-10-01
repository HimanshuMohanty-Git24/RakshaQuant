"""Plan M0.6: .gitignore keeps runtime state and secrets out, and never hides sources."""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _ignored(paths: list[str]) -> set[str]:
    proc = subprocess.run(
        ["git", "check-ignore", "--no-index", *paths],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode in (0, 1), proc.stderr  # 128 = git error
    return set(proc.stdout.split())


def test_sources_are_not_ignored():
    sources = [
        "frontend/package.json",
        "frontend/package-lock.json",
        "frontend/tsconfig.json",
        "frontend/src/lib/api.ts",
        "src/config/nse_calendar.json",
        "src/lib/anything.py",
        "tests/fixtures/data/tape.json",
        "docs/data/table.csv",
    ]
    assert _ignored(sources) == set()


def test_state_and_secrets_are_ignored():
    private = [
        ".env",
        "var/paper/rakshaquant.db",
        "var/paper/paper_wallet.json",
        "var/archive/2026-10-01/paper_wallet.json",
        "journal.db",
        ".coverage",
        "logs/rakshaquant.log",
        "frontend/node_modules/react/index.js",
        "frontend/dist/index.html",
        "frontend/tsconfig.tsbuildinfo",
    ]
    assert _ignored(private) == set(private)
