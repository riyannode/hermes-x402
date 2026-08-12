"""Runtime version reporting tests."""

from __future__ import annotations

import inspect
from importlib.metadata import version as package_version

from hermes_x402.hermes_plugin import runtime
from hermes_x402.hermes_plugin.runtime import X402Runtime


def test_runtime_version_matches_installed_package_metadata():
    assert X402Runtime().version == package_version("hermes-x402")


def test_runtime_has_no_stale_hardcoded_version_constant():
    source = inspect.getsource(runtime)
    assert '_VERSION = "0.2.0"' not in source
    assert "return _package_version()" in source


def test_status_handler_exposes_resolved_runtime_version():
    from hermes_x402.hermes_plugin.tools import register_status_tools

    class Context:
        def __init__(self):
            self.tools = []

        def register_tool(self, **kwargs):
            self.tools.append(kwargs)

    ctx = Context()
    register_status_tools(ctx)
    status = next(item for item in ctx.tools if item["name"] == "x402_status")
    # The handler's runtime version is the same value exposed by the package.
    import json

    result = json.loads(status["handler"]({}))
    assert result["version"] == package_version("hermes-x402")
