import asyncio
from dataclasses import dataclass
import os
from pathlib import Path

import docker
from docker.errors import DockerException, NotFound

from agentforge.models import SandboxPolicy, ToolResult, new_id
from agentforge.tools.base import bounded
from agentforge.tools.fs import Workspace


class SandboxUnavailable(RuntimeError):
    pass


@dataclass
class Sandbox:
    id: str
    container: object
    workspace: Workspace
    image: str
    alive: bool = True


class DockerRunner:
    def __init__(self, root: Path, client=None):
        self.root = root.resolve()
        self.client = client
        self._connect_lock = asyncio.Lock()

    async def connect(self):
        async with self._connect_lock:
            if self.client is None:
                try:
                    self.client = await asyncio.to_thread(docker.from_env, timeout=10)
                    await asyncio.to_thread(self.client.ping)
                except DockerException as exc:
                    self.client = None
                    raise SandboxUnavailable("Docker unavailable. Start Docker and retry.") from exc
        return self.client

    def options(self, workspace: Path, image: str, image_environment=()):
        policy = SandboxPolicy(image=image)
        profile = Path(__file__).with_name("seccomp.json").read_text()
        uid = os.getuid() if os.getuid() != 0 else 1000
        gid = os.getgid() if os.getuid() != 0 else 1000
        return {
            "image": policy.image, "command": ["sleep", "infinity"], "detach": True,
            "read_only": True, "tmpfs": {"/tmp": "rw,nosuid,nodev,noexec,size=256m,mode=1777"},
            "mem_limit": "512m", "memswap_limit": "512m", "nano_cpus": 1_000_000_000,
            "pids_limit": 256, "network_mode": "none", "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true", f"seccomp={profile}"],
            "user": f"{uid}:{gid}", "working_dir": "/workspace", "init": True,
            "volumes": {str(workspace.resolve()): {"bind": "/workspace", "mode": "rw"}},
            "environment": {**{entry.split("=", 1)[0]: None for entry in image_environment},
                            "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp",
                            "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"},
            "labels": {"agentforge.managed": "true"},
            "log_config": docker.types.LogConfig(type="json-file", config={"max-size": "1m", "max-file": "1"}),
        }

    async def create(self, image="python:3.12-slim", workspace=None) -> Sandbox:
        client = await self.connect()
        identity = new_id()
        workspace = workspace or Workspace(self.root / identity)
        if not workspace.root.is_relative_to(self.root):
            raise ValueError("Sandbox workspace is outside the managed root.")
        try:
            descriptor = await asyncio.to_thread(client.images.get, image)
            environment = descriptor.attrs.get("Config", {}).get("Env", []) or []
            container = await asyncio.to_thread(client.containers.run,
                **self.options(workspace.root, image, environment))
        except DockerException as exc:
            raise SandboxUnavailable("Sandbox creation failed. Check Docker and pull the configured image.") from exc
        return Sandbox(identity, container, workspace, image)

    async def destroy(self, sandbox: Sandbox):
        sandbox.alive = False
        try:
            await asyncio.to_thread(sandbox.container.remove, force=True)
        except NotFound:
            pass

    async def healthy(self, sandbox: Sandbox) -> bool:
        if not sandbox.alive:
            return False
        try:
            await asyncio.to_thread(sandbox.container.reload)
            return sandbox.container.status == "running"
        except DockerException:
            return False

    async def execute(self, sandbox: Sandbox, command: list[str], timeout: float = 30) -> ToolResult:
        if not sandbox.alive:
            return ToolResult(ok=False, error="Sandbox was stopped. Reacquire it before retrying.")
        try:
            # Drain without retaining unbounded output; inspect the final exec exit code.
            client = await self.connect()
            def execute():
                identity = client.api.exec_create(sandbox.container.id, command,
                    workdir="/workspace", user=self.options(sandbox.workspace.root, sandbox.image)["user"])["Id"]
                output = bytearray()
                for data in client.api.exec_start(identity, stream=True):
                    if len(output) < 16000:
                        output.extend(data[:16000 - len(output)])
                code = client.api.exec_inspect(identity)["ExitCode"]
                return code, output.decode("utf-8", errors="replace")
            code, output = await asyncio.wait_for(asyncio.to_thread(execute), min(timeout, 30))
            return ToolResult(ok=code == 0, output=bounded(output),
                              error=None if code == 0 else f"Command exited with code {code}. Inspect output and retry.")
        except (TimeoutError, asyncio.CancelledError) as exc:
            await self.destroy(sandbox)
            if isinstance(exc, asyncio.CancelledError):
                raise
            return ToolResult(ok=False, error="Command timed out. Sandbox destroyed to stop all child processes.")
        except DockerException as exc:
            sandbox.alive = False
            raise SandboxUnavailable("Docker execution failed. Rebuild the sandbox before retrying.") from exc
