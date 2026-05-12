"""Story 10.2 — self-hosted Alpine.js bundle provenance.

The bundle is vendored at ``src/open_ems/web/static/alpine.min.js`` per the
local-first / no-CDN-runtime contract. The accompanying README documents the
expected upstream URL and SHA256. This test enforces the documented hash on
the on-disk file so a silent corruption or unverified replacement is caught
in CI rather than at runtime in the homeowner browser.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import open_ems.web

# Documented in src/open_ems/web/static/README.md alongside the upstream URL
# (https://unpkg.com/alpinejs@3.13.10/dist/cdn.min.js). Update this constant
# in lockstep when bumping the Alpine version per the README's update procedure.
_EXPECTED_ALPINE_SHA256 = "fb9b146b7fbd1bbf251fb3ef464f2e7c5d33a4a83aeb0fcf21e92ca6a9558c4b"


def _static_root() -> Path:
    return Path(open_ems.web.__file__).resolve().parent / "static"


def test_alpine_bundle_is_present_at_expected_path() -> None:
    bundle = _static_root() / "alpine.min.js"
    assert bundle.is_file(), f"Alpine.js bundle missing at {bundle}"


def test_alpine_bundle_matches_documented_sha256() -> None:
    bundle = _static_root() / "alpine.min.js"
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    assert digest == _EXPECTED_ALPINE_SHA256, (
        f"Alpine.js bundle SHA256 drift: expected {_EXPECTED_ALPINE_SHA256}, "
        f"got {digest}. Update src/open_ems/web/static/README.md and this "
        f"test constant in lockstep when bumping the pinned version."
    )


def test_alpine_provenance_readme_documents_pinned_version() -> None:
    readme = _static_root() / "README.md"
    text = readme.read_text(encoding="utf-8")
    # Source URL and hash must both be present so the audit trail survives
    # casual edits.
    assert "unpkg.com/alpinejs@" in text, "README must document the upstream URL"
    assert _EXPECTED_ALPINE_SHA256 in text, "README must document the SHA256 used by this test"
