"""Anti-regression: ``_VALID_GAP_LABELS`` MUST exclude ``grid_meter_missing``.

Story 9.2 R3 #2: the grid-meter hard block is enforced *structurally* by
removing the label from the whitelist. Any future change that adds it back
would silently allow the hard block to be acknowledged-and-bypassed —
exactly the safety property we are protecting.

This test pins the contents of the whitelist *and* asserts the four
consumers (service module, repo module) hold identical values.
"""

from __future__ import annotations

from open_ems.services.role_assignment import _VALID_GAP_LABELS
from open_ems.storage.repositories.wizard_state_repo import (
    _VALID_GAP_LABELS as _REPO_VALID_GAP_LABELS,
)


def test_grid_meter_missing_is_not_acknowledgeable() -> None:
    assert "grid_meter_missing" not in _VALID_GAP_LABELS


def test_valid_gap_labels_is_exactly_three_known_labels() -> None:
    assert _VALID_GAP_LABELS == frozenset(
        {"battery_missing", "inverter_missing", "ev_charger_missing"}
    )


def test_valid_gap_labels_consistent_across_modules() -> None:
    """The service module and the repo module both hold the same whitelist.

    The two-name shape is intentional (the import in role_assignment.py
    enforces equality at module-load time too). This test keeps a Python
    refactor that would silently split them honest.
    """
    assert _VALID_GAP_LABELS == _REPO_VALID_GAP_LABELS
