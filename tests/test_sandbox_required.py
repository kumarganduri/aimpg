import os

from aimpg.replay import sandbox


def test_sandbox_is_really_available_where_ci_says_so():
    """CI's Linux sandbox job sets AIMPG_REQUIRE_SANDBOX, so the escape tests can't silently skip there."""
    if os.environ.get("AIMPG_REQUIRE_SANDBOX"):
        assert sandbox.available(), sandbox.unavailable_reason()
