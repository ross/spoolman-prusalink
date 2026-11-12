import logging

import pytest


@pytest.fixture(autouse=True)
def quiet_httpx():
    """Keep httpx at WARNING, as the bridge does at runtime, and restore it after.

    __main__ sets this level, so a test's own httpx INFO "HTTP Request" records
    reach caplog or not depending on whether a test that called main() ran first.
    """
    logger = logging.getLogger("httpx")
    before = logger.level
    logger.setLevel(logging.WARNING)
    yield
    logger.setLevel(before)
