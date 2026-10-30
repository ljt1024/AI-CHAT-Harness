import inspect
import json

from agentforge.models import ToolResult
from agentforge.tools import fs
from agentforge.tools.base import DEFAULT_OUTPUT_LIMIT, bounded


class SandboxTool:
    source = "builtin"

    def __init__(self, runner, sandbox, name, timeout=30):
        self.runner, self.sandbox, self.name, self.timeout = runner, sandbox, name, timeout
        self.permission = "rule" if name in {"write_file", "run_command"} else "auto"
        self.workspace = sandbox.workspace
        if name == "run_command":
            self.description = "Execute a shell command automatically inside the isolated, offline /workspace Python 3.12 container, unless this agent explicitly requires approval. This cannot access the host filesystem or network. Python standard library is available; Node.js, npm and browsers are not installed. Do not repeatedly attempt unavailable runtimes or denied actions; report checks you cannot perform."
            self.schema = {"type": "object", "properties": {"command": {"type": "string", "maxLength": 2000}},
                           "required": ["command"], "additionalProperties": False}
        else:
            reference = fs.FileTool(sandbox.workspace, name)
            self.description, self.schema = reference.description, reference.schema

    async def execute(self, args):
        if self.name == "run_command":
            return await self.runner.execute(self.sandbox, ["sh", "-c", args["command"]], self.timeout)
        # Transfer trusted source in the exec argument; no host mount or runtime dependencies.
        # `bounded` is copied by source, so whatever its signature references has to
        # travel with it -- the container has only what this prelude defines.
        prelude = f'''import asyncio, json, sys, fnmatch, re
from pathlib import Path
DEFAULT_OUTPUT_LIMIT = {DEFAULT_OUTPUT_LIMIT}
class ToolResult:
    def __init__(self, ok, output='', error=None):
        self.ok, self.output, self.error = ok, output, error
'''
        worker = prelude + inspect.getsource(bounded) + '\n' + inspect.getsource(fs.Workspace)
        worker += '\n' + inspect.getsource(fs.FileTool)
        worker += ('\n_original_bounded = bounded\n' +
                   f'def bounded(text, limit={DEFAULT_OUTPUT_LIMIT}): '
                   'return _original_bounded(text, min(limit, 1500))\n')
        worker += '\nr=asyncio.run(FileTool(Workspace(Path("/workspace")),sys.argv[1]).execute(json.loads(sys.argv[2])))\nprint(json.dumps(vars(r), ensure_ascii=False))'
        result = await self.runner.execute(self.sandbox, ["python", "-c", worker, self.name, json.dumps(args)], self.timeout)
        if not result.ok:
            return result
        try:
            return ToolResult.model_validate_json(result.output)
        except ValueError:
            return ToolResult(ok=False, error="File result exceeded transport limits. Request a smaller range.")
