from enum import StrEnum
import time

from agentforge.sandbox.docker_runner import SandboxUnavailable


class CircuitState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    def __init__(self, threshold=3, recovery_seconds=30, clock=time.monotonic):
        self.threshold, self.recovery_seconds, self.clock = threshold, recovery_seconds, clock
        self.state, self.failures, self.opened_at = CircuitState.CLOSED, 0, 0.0
        self.probing = False

    def before_call(self):
        if self.state == CircuitState.OPEN:
            if self.clock() - self.opened_at < self.recovery_seconds:
                raise SandboxUnavailable("Sandbox circuit is open. Wait for Docker to recover.")
            self.state = CircuitState.HALF_OPEN
        if self.state == CircuitState.HALF_OPEN:
            if self.probing:
                raise SandboxUnavailable("Sandbox recovery probe is already running.")
            self.probing = True

    def success(self):
        self.state, self.failures, self.probing = CircuitState.CLOSED, 0, False

    def failure(self):
        self.failures += 1
        if self.state == CircuitState.HALF_OPEN or self.failures >= self.threshold:
            self.state, self.opened_at = CircuitState.OPEN, self.clock()
        self.probing = False

    def status(self):
        return {"state": self.state.value, "failures": self.failures,
                "retry_after": max(0, self.recovery_seconds - (self.clock() - self.opened_at))
                if self.state == CircuitState.OPEN else 0}
