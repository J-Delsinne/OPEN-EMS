"""AC9: Settings.peak_limit_kw / Settings.battery_reserve_floor_percent are seed-only.

Story 9.0b: at runtime PolicyGuard and ControlLoop read the two safety values
via ``ActiveConstraintsProvider``; the only legal read of the ``Settings``
fields is inside ``ActiveConstraintsProvider.hydrate()`` (the cold-start
fallback when no installer activation has happened yet).

This test greps the ``src/`` tree and fails if any module other than the
allowed ones references ``settings.peak_limit_kw`` or
``settings.battery_reserve_floor_percent``. CI failure on violation prevents
the source-of-truth fragmentation R6 calls out.

Allowed modules:

* ``src/open_ems/settings.py`` — the field definitions themselves
* ``src/open_ems/services/active_constraints.py`` — the provider's
  cold-start seed branch
"""

from __future__ import annotations

import re
from pathlib import Path

# Forbidden pattern: a read of ``peak_limit_kw`` or
# ``battery_reserve_floor_percent`` whose LHS identifier ends with
# ``settings`` (e.g. ``settings.peak_limit_kw``,
# ``self._settings.peak_limit_kw``, ``cfg_settings.peak_limit_kw``,
# ``app_settings.peak_limit_kw``). Reads of these attributes on an
# ``ActiveConstraints`` / ``ActiveConstraintsInput`` instance (e.g.
# ``constraints.peak_limit_kw``, ``input.peak_limit_kw``) are NOT flagged —
# those are the contract by which the provider's snapshot is consumed.
#
# The identifier prefix is any Python identifier character sequence
# ([A-Za-z_][A-Za-z0-9_]*) ending in literal ``settings``. The leading
# negative-class boundary ensures we don't match substrings like
# ``unrelatedsettings_table.peak_limit_kw`` that happen to contain the
# letters ``settings`` mid-identifier without ending the identifier.
_FORBIDDEN_RE = re.compile(
    r"(?:^|[^A-Za-z0-9_])"
    r"(?:[A-Za-z_][A-Za-z0-9_]*?)?settings"
    r"\.(?:peak_limit_kw|battery_reserve_floor_percent)\b"
)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SRC_ROOT = _PROJECT_ROOT / "src" / "open_ems"


def _allowed_files(src_root: Path) -> frozenset[Path]:
    return frozenset(
        {
            src_root / "settings.py",
            src_root / "services" / "active_constraints.py",
        }
    )


def _find_violations(src_root: Path) -> list[tuple[Path, int, str]]:
    """Walk ``src_root`` and return every forbidden-pattern match."""
    allowed = _allowed_files(src_root)
    violations: list[tuple[Path, int, str]] = []
    for py_path in src_root.rglob("*.py"):
        if py_path in allowed:
            continue
        text = py_path.read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), start=1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if _FORBIDDEN_RE.search(line):
                violations.append((py_path, line_no, line.strip()))
    return violations


def test_no_forbidden_settings_reads_at_runtime() -> None:
    violations = _find_violations(_SRC_ROOT)
    assert not violations, (
        "Forbidden Settings reads found — Story 9.0b AC9. "
        "Runtime reads of peak_limit_kw / battery_reserve_floor_percent MUST "
        "go through ActiveConstraintsProvider:\n"
        + "\n".join(
            f"  {path.relative_to(_PROJECT_ROOT)}:{ln}: {line}" for path, ln, line in violations
        )
    )


def test_grep_test_actually_fails_on_a_seeded_violation() -> None:
    """Sentinel: prove the grep is not a no-op.

    The regex must match canonical forbidden reads AND must NOT match reads
    against an ``ActiveConstraints`` / ``ActiveConstraintsInput`` instance
    (which are the legal provider-snapshot reads).
    """
    # Forbidden patterns — the regex MUST match these.
    assert _FORBIDDEN_RE.search("value = self._settings.peak_limit_kw\n")
    assert _FORBIDDEN_RE.search("x = settings.battery_reserve_floor_percent + 1\n")
    assert _FORBIDDEN_RE.search("if settings.peak_limit_kw > 0:\n")
    # Aliased-identifier reads — the regex MUST match these too.
    assert _FORBIDDEN_RE.search("y = cfg_settings.peak_limit_kw\n")
    assert _FORBIDDEN_RE.search("z = app_settings.battery_reserve_floor_percent\n")
    assert _FORBIDDEN_RE.search("w = self.app_settings.peak_limit_kw\n")

    # Allowed patterns — the regex MUST NOT match these.
    assert not _FORBIDDEN_RE.search("limit = constraints.peak_limit_kw\n")
    assert not _FORBIDDEN_RE.search("v = input.peak_limit_kw\n")
    assert not _FORBIDDEN_RE.search("p = previous.battery_reserve_floor_percent\n")
    assert not _FORBIDDEN_RE.search("q = snapshot.peak_limit_kw\n")


def test_walker_actually_catches_in_tree_violation(tmp_path: Path) -> None:
    """Sentinel: build a synthetic ``src/`` tree, plant a forbidden read,
    and prove ``_find_violations`` walks it and reports the violation.

    Without this test the regex-shape sentinel above could pass even if a
    refactor of ``_find_violations`` broke its file traversal — silently
    making AC9 a no-op against the real codebase.
    """
    fake_src = tmp_path / "open_ems"
    (fake_src / "services").mkdir(parents=True)
    (fake_src / "core").mkdir()
    (fake_src / "engine").mkdir()

    # Allowed files: should be skipped.
    (fake_src / "settings.py").write_text(
        "peak_limit_kw: float = 25.0\nbattery_reserve_floor_percent: float = 20.0\n",
        encoding="utf-8",
    )
    (fake_src / "services" / "active_constraints.py").write_text(
        "x = settings.peak_limit_kw  # legal seed-from-Settings read\n",
        encoding="utf-8",
    )

    # Innocent files: should NOT be flagged.
    (fake_src / "core" / "constraints.py").write_text(
        "peak_limit_kw: float = 0.0  # field definition, not a read\n"
        "limit = constraints.peak_limit_kw  # legal provider-snapshot read\n",
        encoding="utf-8",
    )

    # Violation: should be flagged.
    offending = fake_src / "engine" / "naughty.py"
    offending.write_text(
        "from open_ems.settings import get_settings\n"
        "\n"
        "def evaluate():\n"
        "    settings = get_settings()\n"
        "    return settings.peak_limit_kw * 2  # FORBIDDEN runtime read\n",
        encoding="utf-8",
    )

    violations = _find_violations(fake_src)
    offending_hits = [v for v in violations if v[0] == offending]
    assert offending_hits, (
        f"Walker did not flag the seeded violation in {offending}. "
        f"All violations found: {violations}"
    )
    # And it didn't false-positive on the allowed files or innocent reads.
    allowed_or_innocent = {
        fake_src / "settings.py",
        fake_src / "services" / "active_constraints.py",
        fake_src / "core" / "constraints.py",
    }
    for path, _, _ in violations:
        assert path not in allowed_or_innocent, (
            f"Walker false-positive on legal/allowed file {path}"
        )
