import fnmatch
import re
from pathlib import Path

from agentforge.models import ToolResult
from agentforge.tools.base import bounded


class Workspace:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, value: str) -> Path:
        candidate = (self.root / value).resolve()
        if not candidate.is_relative_to(self.root):
            raise ValueError("Path escapes workspace")
        for part in candidate.relative_to(self.root).parts:
            lower = part.lower()
            if (lower.startswith(".env") or lower in {".git", ".ssh", ".aws", ".config", "secrets"}
                    or lower.endswith((".pem", ".key")) or lower in {"id_rsa", "id_ed25519"}):
                raise ValueError("Credential or internal path is not accessible")
        return candidate

    def files(self):
        count = 0
        for entry in self.root.rglob("*"):
            count += 1
            if count > 10000:
                break
            try:
                self.path(str(entry.relative_to(self.root)))
                if entry.is_file() and not entry.is_symlink():
                    yield entry
            except ValueError:
                continue


class FileTool:
    source = "builtin"

    def __init__(self, workspace: Workspace, name: str):
        self.workspace, self.name = workspace, name
        self.permission = "rule" if name == "write_file" else "auto"
        self.description = {
            "read_file": "Read a UTF-8 workspace file. Use offset/limit to narrow long output.",
            "write_file": "Write UTF-8 content inside the workspace; replaces existing content.",
            "grep": "Search workspace files for literal text; returns path and line number.",
            "glob": "Find workspace file paths by glob pattern.",
        }[name]
        props = {
            "read_file": {"path": {"type": "string"}, "offset": {"type": "integer", "minimum": 0},
                          "limit": {"type": "integer", "minimum": 1, "maximum": 4000}},
            "write_file": {"path": {"type": "string"}, "content": {"type": "string", "maxLength": 1000000}},
            "grep": {"query": {"type": "string", "minLength": 1}},
            "glob": {"pattern": {"type": "string", "minLength": 1}},
        }[name]
        self.schema = {"type": "object", "properties": props, "additionalProperties": False,
                       "required": {"read_file": ["path"], "write_file": ["path", "content"],
                                    "grep": ["query"], "glob": ["pattern"]}[name]}

    async def execute(self, args: dict) -> ToolResult:
        try:
            if self.name in {"read_file", "write_file"}:
                path = self.workspace.path(args["path"])
                if self.name == "write_file":
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(args["content"], encoding="utf-8")
                    return ToolResult(ok=True, output=f"Wrote {args['path']}")
                with path.open(encoding="utf-8") as handle:
                    handle.seek(args.get("offset", 0))
                    text = handle.read(args.get("limit", 4000) + 1)
                return ToolResult(ok=True, output=bounded(text, args.get("limit", 4000)))
            found = []
            for path in self.workspace.files():
                relative = str(path.relative_to(self.workspace.root))
                if self.name == "glob":
                    if fnmatch.fnmatch(relative, args["pattern"]):
                        found.append(relative)
                elif path.stat().st_size <= 1_000_000:
                    text = path.read_text(encoding="utf-8", errors="replace")
                    found.extend(f"{relative}:{i}:{line}" for i, line in enumerate(text.splitlines(), 1)
                                 if re.search(re.escape(args["query"]), line))
                if sum(map(len, found)) >= 4000:
                    break
            return ToolResult(ok=True, output=bounded("\n".join(found)))
        except (OSError, ValueError) as exc:
            return ToolResult(ok=False, error=bounded(f"{exc}. Use glob to inspect accessible paths."))
