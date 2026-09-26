import logging
from collections.abc import Iterator

import pytest

from app import main

QUIETED = ("httpx2", "httpcore2")


@pytest.fixture
def pristine_logging(monkeypatch) -> Iterator[logging.Handler]:
    """Logging as before startup, holding one foreign root handler; everything is restored afterwards."""
    root = logging.getLogger()
    foreign = logging.NullHandler()
    foreign.set_name("foreign")
    monkeypatch.setattr(root, "handlers", [foreign])
    monkeypatch.setattr(logging.getLogger("uvicorn.access"), "disabled", False)
    levels = {name: logging.getLogger(name).level for name in ("", *QUIETED)}
    for name in QUIETED:
        logging.getLogger(name).setLevel(logging.NOTSET)
    root.setLevel(logging.CRITICAL)
    yield foreign
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)


def test_configure_logging_from_scratch(pristine_logging):
    main.configure_logging("debug")

    root = logging.getLogger()
    assert root.level == logging.DEBUG
    assert root.handlers[0] is pristine_logging
    ours = [h for h in root.handlers if h.get_name() == "transfermarkt-api"]
    assert len(ours) == 1
    handler = ours[0]
    assert type(handler) is logging.StreamHandler
    assert handler.formatter is not None
    assert handler.formatter._fmt == "%(asctime)s %(levelname)s %(name)s %(message)s"
    assert logging.getLogger("uvicorn.access").disabled
    for name in QUIETED:
        assert logging.getLogger(name).level == logging.WARNING, name


def test_configure_logging_is_idempotent(pristine_logging):
    main.configure_logging("info")
    main.configure_logging("warning")

    root = logging.getLogger()
    assert root.level == logging.WARNING
    names = [h.get_name() for h in root.handlers]
    assert names[0] == pristine_logging.get_name()
    assert names.count("transfermarkt-api") == 1
