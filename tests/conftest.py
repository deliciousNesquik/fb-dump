"""Test-wide safety net.

``fb_dump.db`` keeps the chosen isolation in a module-level variable (it patches
firebird-lib's ``tpb``, which takes no context), so a test that sets it would
otherwise leak into every test that runs after it.
"""

from __future__ import annotations

import pytest

from fb_dump import db


@pytest.fixture(autouse=True)
def _reset_isolation():
    yield
    db.set_isolation(None)
