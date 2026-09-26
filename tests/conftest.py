from pathlib import Path

import pytest

LIVE_DIR = Path(__file__).parent / "live"


def pytest_addoption(parser):
    parser.addoption(
        "--snapshot-update",
        action="store_true",
        default=False,
        help="rewrite tests/snapshots/*.json from the current endpoint output",
    )


def pytest_collection_modifyitems(items):
    for item in items:
        if item.path.is_relative_to(LIVE_DIR):
            item.add_marker(pytest.mark.live)
