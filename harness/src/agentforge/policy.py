"""Intervention policy: one file decides where a run stops to ask a human.

The permission levels hard coded on each tool class are only a fallback for tools
this policy does not mention. Two independent axes describe an intervention:

``level``  auto | deny | confirm -- does the run stop at all?
``kind``   approve | input       -- what is the human asked for?

``approve`` gates an action and fails closed (a timeout denies). ``input`` collects
information only the human has and fails open (a timeout lets the run continue
without an answer), because a missing answer is a degraded run, not an unsafe one.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path

# Shared with the permission gate: auto < rule < deny < confirm. "rule" is a real
# level, not a synonym for auto -- it means "judge this with the tool's own rules".
LEVELS = {"auto": 0, "rule": 1, "deny": 2, "confirm": 3}
PATH_KEYS = ("path", "file", "file_path", "target", "name")


@dataclass(frozen=True)
class Decision:
    level: str
    kind: str = "approve"
    reason: str = ""
    timeout: float | None = None
    on_timeout: str = "deny"


class InterventionPolicy:
    def __init__(self, config: dict | None = None):
        config = config or {}
        self.raw = config
        self.defaults = config.get("defaults") or {}
        self.tools = config.get("tools") or {}
        self.model_requests = config.get("model_requests") or {}
        self.explain_enabled = bool(config.get("explain", True))
        self.default_level = str(self.defaults.get("level", "auto"))
        self.default_timeout = self.defaults.get("timeout", 300)
        self.default_on_timeout = str(self.defaults.get("on_timeout", "deny"))
        self._patterns: dict[str, re.Pattern[str]] = {}

    @classmethod
    def load(cls, path) -> "InterventionPolicy":
        """Read the policy file. A missing or broken file degrades to tool defaults."""
        try:
            import yaml
        except ImportError:
            return cls()
        try:
            with Path(path).open(encoding="utf-8") as handle:
                data = yaml.safe_load(handle) or {}
        except (OSError, yaml.YAMLError):
            # A broken policy must degrade to tool defaults, never crash startup.
            return cls()
        if not isinstance(data, dict):
            return cls()
        return cls(data)

    def _spec(self, tool_name: str):
        """Exact name wins, then the longest glob pattern that matches."""
        if tool_name in self.tools:
            return self.tools[tool_name]
        best, best_key = None, ""
        for key, spec in self.tools.items():
            if "*" in key and fnmatch.fnmatch(tool_name, key) and len(key) > len(best_key):
                best, best_key = spec, key
        return best

    def _regex(self, pattern: str) -> re.Pattern[str]:
        compiled = self._patterns.get(pattern)
        if compiled is None:
            compiled = self._patterns[pattern] = re.compile(pattern, re.IGNORECASE)
        return compiled

    @staticmethod
    def _has_conditions(rule: dict) -> bool:
        return any(rule.get(key) for key in ("command_matches", "path_matches", "args_matches",
                                             "path_outside_workspace"))

    def _matches(self, rule: dict, arguments: dict, workspace_root) -> bool:
        for pattern in rule.get("command_matches", []) or []:
            command = str(arguments.get("command", ""))
            if command and self._regex(pattern).search(command):
                return True
        patterns = rule.get("path_matches", []) or []
        if patterns and self._path_matches(arguments, patterns):
            return True
        for key, pattern in (rule.get("args_matches", {}) or {}).items():
            if self._regex(pattern).search(str(arguments.get(key, ""))):
                return True
        if rule.get("path_outside_workspace") and workspace_root is not None:
            if self._outside(arguments, workspace_root):
                return True
        return False

    def _path_matches(self, arguments: dict, patterns: list[str]) -> bool:
        for key in PATH_KEYS:
            value = arguments.get(key)
            if not isinstance(value, str):
                continue
            for pattern in patterns:
                if self._match_one(value, pattern):
                    return True
        return False

    @staticmethod
    def _match_one(value: str, pattern: str) -> bool:
        """fnmatch has no notion of ``**``, so a globstar prefix also matches a bare name."""
        if fnmatch.fnmatch(value, pattern):
            return True
        stripped = pattern[3:] if pattern.startswith("**/") else pattern
        if stripped == pattern:
            return False
        return fnmatch.fnmatch(value, stripped) or fnmatch.fnmatch(value.split("/")[-1], stripped)

    def _outside(self, arguments: dict, workspace_root) -> bool:
        for key in PATH_KEYS:
            value = arguments.get(key)
            if not isinstance(value, str) or not value:
                continue
            try:
                resolved = Path(value).resolve() if Path(value).is_absolute() \
                    else (Path(workspace_root) / value).resolve()
                resolved.relative_to(Path(workspace_root).resolve())
            except (ValueError, OSError):
                return True
        return False

    @staticmethod
    def _normalize(level) -> str:
        level = str(level or "auto").lower()
        return level if level in LEVELS else "auto"

    def decide(self, tool_name: str, arguments: dict | None = None, default_level: str = "auto",
               in_sandbox: bool = False, workspace_root=None) -> Decision:
        arguments = arguments or {}
        spec = self._spec(tool_name)
        if spec is None:
            return Decision(level=self._normalize(default_level))
        if isinstance(spec, str):
            return Decision(level=self._normalize(spec))
        if not isinstance(spec, dict):
            return Decision(level=self._normalize(default_level))
        kind = str(spec.get("kind", "approve"))
        timeout = spec.get("timeout")
        on_timeout = str(spec.get("on_timeout", self.default_on_timeout))
        for rule in spec.get("rules", []) or []:
            if not isinstance(rule, dict):
                continue
            reason = str(rule.get("reason", ""))
            if not self._has_conditions(rule):
                # A catch-all rule. Inside a container it relaxes to the sandbox
                # fallback, so routine work stays quiet while every destructive
                # command above still matches its own rule.
                if in_sandbox and "in_sandbox" in spec:
                    return Decision(level=self._normalize(spec["in_sandbox"]), kind=kind,
                                    timeout=timeout, on_timeout=on_timeout)
                return self._escalate(rule.get("level", "confirm"), kind, reason, timeout,
                                      on_timeout, default_level)
            if self._matches(rule, arguments, workspace_root):
                return self._escalate(rule.get("level", "confirm"), kind, reason, timeout,
                                      on_timeout, default_level)
        if in_sandbox and "in_sandbox" in spec:
            return Decision(level=self._normalize(spec["in_sandbox"]), kind=kind,
                            timeout=timeout, on_timeout=on_timeout)
        return self._escalate(spec.get("level", default_level), kind, "",
                              timeout, on_timeout, default_level)

    @staticmethod
    def _escalate(level, kind, reason, timeout, on_timeout, default_level) -> Decision:
        """A rule may tighten a tool's own level but never loosen it.

        Without this, listing ``ls`` as routine would also silence the host shell,
        which is deliberately stricter than the container. Denials are absolute.
        """
        wanted = InterventionPolicy._normalize(level)
        floor = InterventionPolicy._normalize(default_level)
        if wanted == "deny":
            return Decision(level="deny", kind=kind, reason=reason)
        return Decision(level=wanted if LEVELS[wanted] >= LEVELS[floor] else floor,
                        kind=kind, reason=reason, timeout=timeout, on_timeout=on_timeout)

    # ---------- the model asking a human for information ----------

    @property
    def input_enabled(self) -> bool:
        return bool(self.model_requests.get("enabled", True))

    @property
    def input_budget(self) -> int:
        return int(self.model_requests.get("max_per_run", 3) or 0)

    @property
    def input_timeout(self) -> float:
        return float(self.model_requests.get("timeout", self.default_timeout) or 0)

    @property
    def input_on_timeout(self) -> str:
        return str(self.model_requests.get("on_timeout", "skip"))

    def input_allowed(self, text: str) -> tuple[bool, str]:
        """Reject questions that fish for credentials through the approval channel."""
        for pattern in self.model_requests.get("forbid_patterns", []) or []:
            if self._regex(pattern).search(str(text)):
                return False, "This question is not allowed; it asks for a credential."
        return True, ""

    def explain(self) -> dict:
        """Everything the UI needs to show which actions stop a run, and why."""
        return {"version": self.raw.get("version", 1),
                "defaults": {"level": self.default_level, "timeout": self.default_timeout,
                             "on_timeout": self.default_on_timeout},
                "tools": self.tools, "model_requests": self.model_requests}
