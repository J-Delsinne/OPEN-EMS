#!/usr/bin/env python3
"""Pre-commit hook: fail if any story file has open review findings.

A story with any unchecked [ ] item inside a "Senior Developer Review (AI)"
section cannot be marked done (Story Closure Gate C5).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_STORY_DIR = Path("_bmad-output/implementation-artifacts")
_REVIEW_SECTION_RE = re.compile(r"^#{1,3}\s+Senior Developer Review", re.MULTILINE)
_OPEN_FINDING_RE = re.compile(r"^\s*-\s+\[ \]", re.MULTILINE)


def _check_file(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    match = _REVIEW_SECTION_RE.search(text)
    if not match:
        return []
    review_body = text[match.start() :]
    # Limit to the review section (stop at next same-level or higher heading).
    # offset is relative to review_body; next_section.start() is relative to the
    # sub-slice, so the correct upper bound is offset + next_section.start().
    offset = match.end() - match.start() + 1
    next_section = re.search(r"^#{1,3} ", review_body[offset:], re.MULTILINE)
    if next_section:
        review_body = review_body[: offset + next_section.start()]
    findings = _OPEN_FINDING_RE.findall(review_body)
    return [f"{path}: {len(findings)} open review finding(s)" for _ in findings[:1]]


def main() -> None:
    if not _STORY_DIR.exists():
        return
    errors: list[str] = []
    for story_file in sorted(_STORY_DIR.glob("*.md")):
        errors.extend(_check_file(story_file))
    if errors:
        print(
            "❌ Open review findings detected — a story with open [ ] review items cannot be done:"
        )
        for msg in errors:
            print(f"  {msg}")
        sys.exit(1)
    print("✅ No open review findings.")


if __name__ == "__main__":
    main()
