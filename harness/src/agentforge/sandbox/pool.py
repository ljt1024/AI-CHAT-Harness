import asyncio
from dataclasses import dataclass
from enum import StrEnum
import logging
import re
import time

from agentforge.sandbox.breaker import CircuitBreaker
from agentforge.sandbox.docker_runner import Sandbox, SandboxUnavailable

logger = logging.getLogger(__name__)


class SandboxState(StrEnum):
    CREATE = "CREATE"
    WARM = "WARM"
    CLAIMED = "CLAIMED"
    CACHED = "CACHED"
    DESTROY = "DESTROY"


@dataclass
class Lease:
    sandbox: Sandbox
    state: SandboxState
    session_id: str | None = None
    used_at: float = 0


class SandboxPool:
    def __init__(self, runner, warm_size=2, max_size=8, idle_seconds=600,
                 breaker=None, clock=time.monotonic, acquire_attempts=3):
        if not 0 <= warm_size <= max_size:
            raise ValueError("Warm size must be between zero and max size.")
        self.runner, self.warm_size, self.max_size = runner, warm_size, max_size
        self.idle_seconds, self.clock = idle_seconds, clock
        self.acquire_attempts = acquire_attempts
        self.breaker = breaker or CircuitBreaker(clock=clock)
        self.entries: dict[str, Lease] = {}
        self.pending_sessions: set[str] = set()
        self.workspaces: dict[str, object] = {}
        self.lock, self.creating = asyncio.Lock(), 0
        self.background: asyncio.Task | None = None
        self.maintenance: asyncio.Task | None = None
        self.closed, self.last_error = False, None

    async def start(self):
        # Nothing this process remembers from a predecessor can be rebuilt onto: the
        # session-to-workspace map lives in memory. Sweeping before warming therefore
        # reclaims real leftovers instead of racing the warm-up.
        await self.sweep()
        await self.replenish()
        self.maintenance = asyncio.create_task(self._maintain())

    async def sweep(self):
        """Reclaim containers and directories no live session can rebuild onto."""
        sweep = getattr(self.runner, "sweep", None)
        if sweep is None:
            return None
        try:
            return await sweep(keep=[workspace.root for workspace in self.workspaces.values()])
        except Exception as exc:
            # Docker being down is not a reason to refuse to start.
            logger.warning("Sandbox sweep skipped: %s", exc)
            return None

    async def _maintain(self):
        cycles = 0
        while True:
            try:
                await asyncio.sleep(min(30, max(0.1, self.idle_seconds / 2)))
                await self.reap()
                await self.replenish()
                # Directories outlive the leases that used them, so one that no session
                # remembers and no container mounts is reclaimed roughly hourly. Faster
                # would be wasted work; slower lets a long-lived process accumulate.
                cycles += 1
                if cycles % 120 == 0:
                    await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                # One bad cycle must not silently end maintenance: without it, idle
                # sandboxes are never reaped and the pool leaks containers forever.
                logger.warning("Sandbox pool maintenance cycle failed; retrying.", exc_info=True)

    def _schedule_replenish(self):
        if not self.closed and (self.background is None or self.background.done()):
            self.background = asyncio.create_task(self.replenish())

    async def replenish(self):
        while not self.closed:
            async with self.lock:
                warm = sum(e.state == SandboxState.WARM for e in self.entries.values())
                if warm + self.creating >= self.warm_size or len(self.entries) + self.creating >= self.max_size:
                    return
                try:
                    self.breaker.before_call()
                except SandboxUnavailable:
                    return
                self.creating += 1
            sandbox = None
            try:
                sandbox = await self.runner.create()
                self.breaker.success()
                async with self.lock:
                    if not self.closed:
                        self.entries[sandbox.id] = Lease(sandbox, SandboxState.WARM, used_at=self.clock())
                if self.closed:
                    await self.runner.destroy(sandbox)
                    return
            except SandboxUnavailable as exc:
                self.last_error = str(exc)
                self.breaker.failure()
                return
            finally:
                async with self.lock:
                    self.creating -= 1

    async def _reserve(self, session_id: str, image: str) -> Lease | None:
        """Claim a cached sandbox for this session, else a warm one.

        Returns None when the caller has to create a fresh sandbox; the lock
        section already reserved a creation slot in that case.
        """
        async with self.lock:
            if session_id in self.pending_sessions:
                raise SandboxUnavailable("Session sandbox is already being created.")
            for lease in self.entries.values():
                if lease.session_id == session_id and lease.state != SandboxState.DESTROY:
                    if lease.state == SandboxState.CLAIMED:
                        raise SandboxUnavailable("Session already owns an active sandbox lease.")
                    if lease.sandbox.image != image:
                        raise SandboxUnavailable("A session cannot change sandbox image.")
                    self.breaker.before_call()
                    lease.state, lease.session_id = SandboxState.CLAIMED, session_id
                    lease.used_at = self.clock()
                    return lease
            if session_id not in self.workspaces:
                warm = next((e for e in self.entries.values()
                    if e.state == SandboxState.WARM and e.sandbox.image == image), None)
                if warm is not None:
                    self.breaker.before_call()
                    warm.state, warm.session_id = SandboxState.CLAIMED, session_id
                    warm.used_at = self.clock()
                    return warm
            if len(self.entries) + self.creating >= self.max_size:
                raise SandboxUnavailable("Sandbox pool is at capacity. Wait for another session to finish.")
            self.breaker.before_call()
            self.creating += 1
            self.pending_sessions.add(session_id)
            return None

    async def acquire(self, session_id, image="python:3.12-slim") -> Lease:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", session_id):
            raise ValueError("Invalid sandbox session ID.")
        if self.closed:
            raise SandboxUnavailable("Sandbox pool is closed.")
        # A cached or warm sandbox can die while idle. Drop it and try another
        # candidate instead of recursing, which had no depth limit and could
        # exhaust the stack whenever Docker kept reporting unhealthy leases.
        for _ in range(self.acquire_attempts):
            selected = await self._reserve(session_id, image)
            if selected is None:
                # No cached or warm sandbox; a creation slot is already reserved.
                break
            if await self.runner.healthy(selected.sandbox):
                async with self.lock:
                    self.workspaces[session_id] = selected.sandbox.workspace
                self.breaker.success()
                self._schedule_replenish()
                return selected
            await self._destroy(selected)
            self.breaker.failure()
        else:
            raise SandboxUnavailable("No healthy sandbox is available. Check the Docker daemon and retry.")
        try:
            sandbox = await self.runner.create(image, workspace=self.workspaces.get(session_id))
            if self.closed:
                await self.runner.destroy(sandbox)
                raise SandboxUnavailable("Sandbox pool closed during creation.")
            selected = Lease(sandbox, SandboxState.CLAIMED, session_id, self.clock())
            async with self.lock:
                self.entries[sandbox.id] = selected
                self.workspaces[session_id] = sandbox.workspace
            self.breaker.success()
        except SandboxUnavailable:
            self.breaker.failure()
            raise
        finally:
            async with self.lock:
                self.creating -= 1
                self.pending_sessions.discard(session_id)
        self._schedule_replenish()
        return selected

    async def release(self, session_id):
        async with self.lock:
            lease = next((e for e in self.entries.values() if e.session_id == session_id), None)
            if lease is None or lease.state != SandboxState.CLAIMED:
                return
            if lease.sandbox.alive:
                lease.state, lease.used_at = SandboxState.CACHED, self.clock()
                return
        await self._destroy(lease)
        self.breaker.failure()
        self._schedule_replenish()

    async def _destroy(self, lease):
        async with self.lock:
            lease.state = SandboxState.DESTROY
            self.entries.pop(lease.sandbox.id, None)
        await self.runner.destroy(lease.sandbox)

    async def discard(self, session_id: str) -> bool:
        """Forget a session's sandbox: container, lease and directory.

        `release` deliberately keeps all three so the same conversation can
        resume onto the files it produced. Deleting the conversation means none
        of them should outlive it — and the directory otherwise never goes
        away: `workspaces` protects it from every sweep, and nothing prunes
        that map.
        """
        async with self.lock:
            lease = next((e for e in self.entries.values()
                          if e.session_id == session_id), None)
            workspace = self.workspaces.pop(session_id, None)
            if lease is not None:
                lease.state = SandboxState.DESTROY
                self.entries.pop(lease.sandbox.id, None)
        if lease is not None:
            await self.runner.destroy(lease.sandbox)
        if workspace is not None:
            discard = getattr(self.runner, "discard_workspace", None)
            if discard is not None:
                # Runs in a thread: removing a directory holds no event loop work.
                await asyncio.to_thread(discard, workspace)
        return lease is not None or workspace is not None

    async def reap(self):
        async with self.lock:
            expired = [e for e in self.entries.values() if e.state == SandboxState.CACHED
                       and self.clock() - e.used_at >= self.idle_seconds]
            for lease in expired:
                lease.state = SandboxState.DESTROY
        for lease in expired:
            await self._destroy(lease)

    def status(self):
        return {"states": {state.value: sum(e.state == state for e in self.entries.values())
                           for state in SandboxState}, "creating": self.creating,
                "circuit": self.breaker.status(), "last_error": self.last_error,
                "sandboxes": [{"id": e.sandbox.id, "state": e.state.value,
                    "session_id": e.session_id, "image": e.sandbox.image} for e in self.entries.values()]}

    async def close(self):
        self.closed = True
        if self.maintenance:
            self.maintenance.cancel()
            await asyncio.gather(self.maintenance, return_exceptions=True)
        if self.background:
            await asyncio.gather(self.background, return_exceptions=True)
        await asyncio.gather(*(self._destroy(e) for e in list(self.entries.values())))
        # Withdraw the instance file last: until the containers above are gone, a
        # starting successor must still treat this process as live.
        unregister = getattr(self.runner, "unregister", None)
        if unregister is not None:
            unregister()
