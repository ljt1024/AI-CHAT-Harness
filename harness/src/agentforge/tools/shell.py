import asyncio
import os
import shlex
import signal

from agentforge.models import ToolResult
from agentforge.tools.base import bounded
from agentforge.tools.fs import Workspace


class ShellTool:
    name = "run_command"
    description = "Run a restricted command: pwd, ls, cat, wc, head, tail, echo, sleep, true, false. No shell syntax."
    permission = "confirm"
    source = "builtin"
    schema = {"type": "object", "properties": {"command": {"type": "string", "maxLength": 2000}},
              "required": ["command"], "additionalProperties": False}

    def __init__(self, workspace: Workspace, timeout: float = 30):
        self.workspace, self.timeout = workspace, min(timeout, 30)

    def validate_command(self, command: str) -> list[str]:
        if any(token in command for token in (";", "|", "&", ">", "<", "`", "$", "\n", "\r", "\x00")):
            raise ValueError("Shell operators and interpolation are disabled")
        args = shlex.split(command)
        allowed = {"pwd", "ls", "cat", "wc", "head", "tail", "echo", "sleep", "true", "false"}
        if not args or args[0] not in allowed:
            raise ValueError("Command is not allowlisted; use workspace file tools")
        if args[0] == "sleep":
            if len(args) != 2 or not 0 <= float(args[1]) <= 60:
                raise ValueError("Use sleep with one duration between 0 and 60")
        elif args[0] in {"pwd", "true", "false"}:
            if len(args) != 1:
                raise ValueError("This command accepts no arguments")
        elif args[0] != "echo":
            for arg in args[1:]:
                if arg.startswith("-"):
                    if arg not in {"-l", "-a", "-la", "-al", "-c", "-n", "-w"}:
                        raise ValueError("Flag is not allowlisted")
                elif arg.isdigit() and args[0] in {"head", "tail"}:
                    continue
                else:
                    self.workspace.path(arg)
        return args

    async def execute(self, args: dict) -> ToolResult:
        try:
            argv = self.validate_command(args["command"])
            process = await asyncio.create_subprocess_exec(
                *argv, cwd=self.workspace.root,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(self.workspace.root)},
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                start_new_session=True)
            async def collect():
                output = bytearray()
                while data := await process.stdout.read(4096):
                    if len(output) < 16000:
                        output.extend(data[:16000 - len(output)])
                await process.wait()
                return output.decode("utf-8", errors="replace")
            try:
                output = await asyncio.wait_for(collect(), self.timeout)
            except (TimeoutError, asyncio.CancelledError) as exc:
                if process.returncode is None:
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                return ToolResult(ok=False, error="Command timed out. Use a shorter command.")
            return ToolResult(ok=process.returncode == 0, output=bounded(output),
                              error=None if process.returncode == 0 else "Command failed. Check arguments.")
        except (OSError, ValueError) as exc:
            return ToolResult(ok=False, error=bounded(f"{exc}. Use an allowlisted command."))
