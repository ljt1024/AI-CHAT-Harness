import asyncio
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import shutil
import time

import docker
from docker.errors import DockerException, NotFound

from agentforge.models import SandboxPolicy, ToolResult, new_id
from agentforge.tools.base import bounded
from agentforge.tools.fs import Workspace


logger = logging.getLogger(__name__)


class SandboxUnavailable(RuntimeError):
    pass


@dataclass
class Sandbox:
    id: str
    container: object
    workspace: Workspace
    image: str
    alive: bool = True


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class DockerRunner:
    def __init__(self, root: Path, client=None, instance: str | None = None):
        self.root = root.resolve()
        self.client = client
        # Tells this process's containers apart from a predecessor's: both carry the
        # managed label, and only the instance file says whether the owner still runs.
        self.instance = instance or new_id()
        self.state_dir = self.root.parent / "instances"
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
            "labels": {"agentforge.managed": "true", "agentforge.instance": self.instance},
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
        # The directory deliberately survives: an idle sandbox is reaped and the same
        # session rebuilds onto the same workspace, so its files must still be there.
        # Leftovers are reclaimed by sweep() once no session remembers them.

    def discard_workspace(self, workspace):
        """Remove a sandbox directory, refusing anything outside its own root."""
        # A bare Path is taken as the directory itself: Path.root is "/", so asking
        # for `.root` here would resolve to the filesystem root.
        path = workspace.resolve() if isinstance(workspace, (str, Path)) else Path(workspace.root)
        root = path.resolve()
        # Only a direct child of the managed root, never the root itself: a bug in a
        # caller must not turn this into "delete the workspaces directory".
        if not self.root or root.parent != self.root:
            return False
        try:
            shutil.rmtree(root)
            return True
        except FileNotFoundError:
            return True
        except OSError as exc:
            logger.warning("Could not remove sandbox workspace %s: %s", root.name, exc)
            return False

    def register(self) -> None:
        """Announce this process, so a survivor can tell its containers from mine."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / f"{self.instance}.json").write_text(
            json.dumps({"pid": os.getpid(), "startedAt": time.time()}))

    def unregister(self) -> None:
        try:
            (self.state_dir / f"{self.instance}.json").unlink()
        except OSError:
            pass

    def live_instances(self) -> set[str]:
        """Instance IDs whose process is still running; stale files are removed."""
        live: set[str] = set()
        if not self.state_dir.is_dir():
            return live
        for path in self.state_dir.glob("*.json"):
            if path.stem == self.instance:
                live.add(path.stem)
                continue
            try:
                pid = int(json.loads(path.read_text())["pid"])
            except (OSError, ValueError, KeyError, TypeError):
                pid = -1
            if pid > 0 and _pid_alive(pid):
                live.add(path.stem)
            else:
                try:
                    path.unlink()
                except OSError:
                    pass
        return live

    async def sweep(self, force: bool = False, min_age_seconds: float = 600, keep=()) -> dict:
        """Remove containers and directories a previous process left behind.

        A SIGKILL never reaches lifespan shutdown, so containers keep running long
        after the backend that owned them is gone, and nothing can exec into them.
        Running containers are only touched with force: another backend on this
        machine can legitimately own them.
        """
        self.register()
        client = await self.connect()
        removed = {"containers": 0, "workspaces": 0, "keptRunning": 0}
        in_use: set[Path] = set()
        live = self.live_instances() if not force else set()
        containers = await asyncio.to_thread(
            client.containers.list, all=True, filters={"label": "agentforge.managed=true"})
        for container in containers:
            for mount in container.attrs.get("Mounts") or []:
                if mount.get("Source"):
                    in_use.add(Path(mount["Source"]).resolve())
            if getattr(container, "status", "") == "running" and not force:
                owner = (getattr(container, "labels", None) or {}).get("agentforge.instance")
                # A running container is only a leftover when its owner is known to be
                # gone. No owner label means a container from before this feature.
                if owner is None or owner in live:
                    removed["keptRunning"] += 1
                    continue
            try:
                await asyncio.to_thread(container.remove, force=True)
                removed["containers"] += 1
            except NotFound:
                removed["containers"] += 1
            except DockerException as exc:
                logger.warning("Could not remove a leftover sandbox container: %s", exc)
        if not self.root.is_dir():
            return removed
        # A reaped sandbox keeps its directory on purpose, so "no container mounts it"
        # is not enough: the pool also says which ones it may rebuild onto.
        retained = {Path(path).resolve() for path in keep}
        now = time.time()
        for entry in self.root.iterdir():
            # A directory still mounted by a container is in use, and a fresh one may
            # not be mounted yet, so only old unmounted ones count as leftovers.
            if not entry.is_dir() or entry.resolve() in in_use or entry.resolve() in retained:
                continue
            try:
                if now - entry.stat().st_mtime < min_age_seconds:
                    continue
            except OSError:
                continue
            if self.discard_workspace(entry):
                removed["workspaces"] += 1
        if removed["containers"] or removed["workspaces"]:
            logger.info("sandbox sweep removed containers=%d workspaces=%d kept_running=%d",
                        removed["containers"], removed["workspaces"], removed["keptRunning"])
        return removed

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
