import pytest

from spoolman_prusalink import app
from spoolman_prusalink.accounting import Result, violations


@pytest.fixture(autouse=True)
def legal_tool_states(monkeypatch):
    """Check every tool state the bridge passes to the accounting or gets back from it.

    It covers each suite, so a state that the bridge changes outside `account()`, such
    as with a sync write or an eviction, is checked at the next poll.
    """

    def checked(function):
        def call(state, *args, **kwargs):
            assert violations(state) == [], state
            result = function(state, *args, **kwargs)
            after = result.state if isinstance(result, Result) else result
            assert violations(after) == [], after
            return result

        return call

    monkeypatch.setattr(app, "account", checked(app.account))
    monkeypatch.setattr(app, "hold_deleted", checked(app.hold_deleted))
