#!/usr/bin/env python3
"""Run a skill's script inside the AgentForge sandbox instead of on the host.

Skills are instruction documents plus optional scripts, and a skill script
executed directly would run with the user's own privileges. This wrapper ships
the skill's files to /api/sandbox/exec, where they run in a disposable,
offline, unprivileged container, and prints the result.

Usage:
    scripts/skill-run.py <skill_dir> [command ...]
    scripts/skill-run.py ~/.workbuddy/skills/demo
    scripts/skill-run.py ~/.workbuddy/skills/demo python run.py --flag

The command defaults to run.py / main.py / run.sh / main.sh when present.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

MAX_FILES = 200
MAX_FILE_BYTES = 2_000_000
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv"}
TEXT_SUFFIXES = {".py", ".sh", ".bash", ".js", ".ts", ".json", ".yaml", ".yml",
                 ".md", ".txt", ".toml", ".ini", ".cfg", ".csv", ".html", ".css"}
DEFAULT_SCRIPTS = {"run.py": ["python", "run.py"], "main.py": ["python", "main.py"],
                   "run.sh": ["bash", "run.sh"], "main.sh": ["bash", "main.sh"]}


def collect(root):
    files = []
    for current, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in sorted(names):
            path = os.path.join(current, name)
            relative = os.path.relpath(path, root)
            if os.path.splitext(name)[1].lower() not in TEXT_SUFFIXES:
                continue
            try:
                with open(path, encoding="utf-8") as handle:
                    content = handle.read(MAX_FILE_BYTES + 1)
            except (OSError, UnicodeDecodeError) as exc:
                print(f"skip {relative}: {exc}", file=sys.stderr)
                continue
            if len(content) > MAX_FILE_BYTES:
                print(f"skip {relative}: larger than {MAX_FILE_BYTES} bytes", file=sys.stderr)
                continue
            files.append({"path": relative.replace(os.sep, "/"), "content": content})
    return files[:MAX_FILES]


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("skill_dir", help="directory containing the skill")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="command to run in the sandbox")
    parser.add_argument("--base", default=os.environ.get("AGENTFORGE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()

    root = os.path.abspath(os.path.expanduser(args.skill_dir))
    if not os.path.isdir(root):
        print(f"not a directory: {root}", file=sys.stderr)
        return 2

    command = [item for item in args.command if item != "--"]
    if not command:
        for name, guessed in DEFAULT_SCRIPTS.items():
            if os.path.isfile(os.path.join(root, name)):
                command = guessed
                break
    if not command:
        print("no command given and no run.py/main.py/run.sh/main.sh found", file=sys.stderr)
        return 2

    payload = json.dumps({"command": command, "files": collect(root),
                          "timeout": args.timeout}).encode()
    request = urllib.request.Request(
        f"{args.base}/api/sandbox/exec", data=payload, method="POST",
        headers={"Content-Type": "application/json"})
    # The host exports HTTP_PROXY, which would send this loopback call out through
    # a proxy and fail, so proxies are disabled explicitly.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=args.timeout + 30) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        print(f"sandbox rejected the request: {exc.code} {exc.read().decode(errors='replace')}",
              file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"could not reach Harness at {args.base}: {exc}", file=sys.stderr)
        return 1

    data = body.get("data", {})
    if data.get("output"):
        print(data["output"], end="")
    if data.get("error"):
        print(data["error"], file=sys.stderr)
    return 0 if data.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
