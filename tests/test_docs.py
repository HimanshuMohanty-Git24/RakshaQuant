"""Plan M12.3: the reference docs are generated from the code, and no doc links nowhere."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"\]\(([^)\s]+)\)")


def gen_docs() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_docs", ROOT / "scripts" / "gen_docs.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_reference_docs_are_current():
    for name, text in gen_docs().render_all().items():
        committed = (ROOT / "docs" / "reference" / name).read_text(encoding="utf-8")
        assert committed == text, f"docs/reference/{name} is stale: run scripts/gen_docs.py"


def test_every_variable_in_the_env_template_is_a_real_setting(monkeypatch):
    """`.env.example` names only settings the code reads (commented examples included), and
    loads with no configuration warning."""
    from src.config.limits import RiskLimits
    from src.config.settings import Settings

    template = ROOT / ".env.example"
    names = re.findall(r"^#? ?([A-Z][A-Z0-9_]+)=", template.read_text(encoding="utf-8"), re.M)
    known = {f.upper() for f in Settings.model_fields}
    known |= {f"RISK_{f.upper()}" for f in RiskLimits.model_fields}
    assert names and sorted(set(names) - known) == []
    for name in ("VAR_DIR", "STATE_DIR", "ENVIRONMENT"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=template)
    assert settings.environment == "paper" and settings.config_warnings == []


def test_relative_links_in_the_docs_resolve():
    pages = [ROOT / "README.md", ROOT / "CLAUDE.md", *sorted((ROOT / "docs").glob("*.md")),
             *sorted((ROOT / "docs" / "runbooks").glob("*.md")),
             *sorted((ROOT / "docs" / "reference").glob("*.md"))]  # fmt: skip
    broken = []
    for page in pages:
        for target in LINK.findall(page.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            path = (page.parent / target.split("#", 1)[0]).resolve()
            if not path.exists():
                broken.append(f"{page.relative_to(ROOT).as_posix()}: {target}")
    assert broken == []
