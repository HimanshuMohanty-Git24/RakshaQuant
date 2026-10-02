"""
(Re)write the demo's bundled fixture tape, ``src/engine/demo_tape/`` (plan M9.6), from the
deterministic generator in ``src/engine/demo.py``. Only needed after changing the generator;
``tests/test_demo_tape.py`` fails until the committed tape matches it again.

    uv run python scripts/build_demo_tape.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.engine.demo import DEMO_TAPE, write_demo_tape  # noqa: E402


def main() -> int:
    for path in write_demo_tape():
        print(f"wrote {path.relative_to(DEMO_TAPE.parent.parent.parent)} "
              f"({path.stat().st_size:,} bytes)")  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
