"""
Write the web API's OpenAPI document to ``frontend/openapi.json`` (plan M9.5). The frontend's
types are generated from it (``cd frontend && npm run gen:api`` → ``src/api/types.gen.ts``);
CI fails when either file is stale.

    uv run --extra web python scripts/export_openapi.py          # regenerate
    uv run --extra web python scripts/export_openapi.py --check  # exit 1 if stale
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.web.security import WebSecurity  # noqa: E402
from src.web.server import create_app  # noqa: E402

OUT = ROOT / "frontend" / "openapi.json"


def document() -> str:
    app = create_app(security=WebSecurity(token="contract-only"))
    return json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    parser.add_argument("--check", action="store_true", help="fail if the file is stale")
    args = parser.parse_args()
    text = document()
    current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
    if args.check:
        if current != text:
            print(f"{OUT.relative_to(ROOT)} is stale: run scripts/export_openapi.py, then "
                  "`npm run gen:api` in frontend/")  # fmt: skip
            return 1
        print("OpenAPI document is up to date")
        return 0
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
