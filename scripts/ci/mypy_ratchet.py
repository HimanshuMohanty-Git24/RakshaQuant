"""
CI ratchet for the global strict-mypy error count (plan §8, M0.8).

    uv run --extra dev python scripts/ci/mypy_ratchet.py 374

Fails if ``mypy src`` reports more errors than the ceiling, or if mypy did not finish (a
blocking error prints "Found 1 error", which would otherwise slip under any ceiling). When
the count drops, it asks for the ceiling to be lowered so the gain is locked in.
"""

import re
import subprocess
import sys

USAGE = "usage: mypy_ratchet.py <max-errors>"


def main(argv: list[str]) -> int:
    if len(argv) != 1 or not argv[0].isdigit():
        print(USAGE, file=sys.stderr)
        return 2
    ceiling = int(argv[0])

    proc = subprocess.run(
        [sys.executable, "-m", "mypy", "src"], capture_output=True, text=True, check=False
    )
    output = proc.stdout + proc.stderr
    lines = output.strip().splitlines()
    summary = lines[-1] if lines else ""
    print(summary)

    if proc.returncode not in (0, 1) or "prevented further checking" in output:
        print(output)
        print("::error::mypy did not complete; the ratchet cannot be evaluated")
        return 1

    found = re.search(r"Found (\d+) errors? in", summary)
    if found is None and not summary.startswith("Success"):
        print("::error::could not parse the mypy summary line")
        return 1
    count = int(found.group(1)) if found else 0

    if count > ceiling:
        print(f"::error::mypy errors rose to {count} (ratchet {ceiling}); fix the new ones")
        return 1
    if count < ceiling:
        print(f"::notice::mypy errors fell to {count}; lower the ratchet from {ceiling}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
