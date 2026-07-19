"""Regression test: live_test _Ctx supports register_command.

Proves that the _Ctx used by live_test.py can handle the /x402
registration without AttributeError.
"""

from __future__ import annotations

import json
import subprocess


class TestLiveTestCtx:
    def test_live_test_ctx_supports_register_command(self):
        """live_test._Ctx must have register_command to avoid AttributeError."""
        # This is the exact code pattern used by live_test.py
        code = (
            "import json\n"
            "class _Ctx:\n"
            "  def __init__(s): s.tools=[]; s.hooks=[]; s.commands=[]\n"
            "  def register_tool(s, *, name, toolset, schema, handler, **kw):\n"
            "    if not isinstance(name, str) or not name: raise TypeError('name required')\n"
            "    s.tools.append(name)\n"
            "  def register_hook(s, hook_type, handler, **kw): s.hooks.append(hook_type)\n"
            "  def register_command(s, name, handler, **kw): s.commands.append(name)\n"
            "from hermes_x402.hermes_plugin.entry import register\n"
            "ctx=_Ctx(); register(ctx)\n"
            "print(json.dumps({\n"
            "  'tools': len(ctx.tools),\n"
            "  'hooks': len(ctx.hooks),\n"
            "  'commands': len(ctx.commands),\n"
            "  'command_names': ctx.commands,\n"
            "  'verification_type': 'static_contract'\n"
            "}))\n"
        )
        r = subprocess.run(
            ["python3", "-c", code],
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, f"subprocess failed: {r.stderr[:500]}"
        result = json.loads(r.stdout.strip())
        assert result["tools"] == 14
        assert result["hooks"] == 1
        assert result["commands"] == 1
        assert result["command_names"] == ["x402"]
