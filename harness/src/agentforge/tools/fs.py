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
            "read_file": "Read a UTF-8 workspace file. offset is a 0-based CHARACTER offset, NOT a line number; limit is a character count. At most 1200 characters of file content are returned per page. When truncated, follow the returned next_offset exactly to continue; do not use line numbers or repeat the same offset.",
            "write_file": "Write UTF-8 content inside the workspace; replaces existing content by default. Set append=true to add a subsequent chunk. For long HTML/CSS/JS, use separate files or chunks under 6000 characters per call; do not put a whole large application in one tool argument.",
            "grep": "Search workspace files for literal text; returns path and line number.",
            "glob": "Find workspace file paths by glob pattern.",
        }[name]
        props = {
            "read_file": {"path": {"type": "string"}, "offset": {"type": "integer", "minimum": 0,
                          "description": "0-based character offset, NOT a line number. Follow next_offset from the previous read."},
                          "limit": {"type": "integer", "minimum": 1, "maximum": 4000,
                                    "description": "Character count, capped at 1200 per page."}},
            "write_file": {"path": {"type": "string"}, "content": {"type": "string", "maxLength": 1000000},
                           "append": {"type": "boolean", "default": False}},
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
                    with path.open("a" if args.get("append", False) else "w", encoding="utf-8") as handle:
                        handle.write(args["content"])
                    return ToolResult(ok=True, output=f"Wrote {args['path']}")
                with path.open(encoding="utf-8") as handle:
                    offset = args.get("offset", 0)
                    handle.read(offset)
                    limit = min(args.get("limit", 1200), 1200)
                    text = handle.read(limit + 1)
                if len(text) > limit:
                    text = text[:limit]
                    text += (f"\n[Truncated. Read characters {offset}:{offset + limit}. "
                             f"next_offset={offset + limit}; use read_file offset={offset + limit}, limit=1200. "
                             "Offsets and limits are characters, not lines.]")
                elif offset:
                    text += f"\n[EOF. Read characters {offset}:{offset + len(text)}.]"
                return ToolResult(ok=True, output=text)
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
