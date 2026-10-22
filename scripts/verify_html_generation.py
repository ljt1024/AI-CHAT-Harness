"""Replay a failed task in a separate Docker session using the real model.

By default only generated web-file writes and validated skill-document reads are approved.
--approve-sandbox-checks also approves model-requested checks in this script's NEW isolated
Docker workspace (offline, no credentials); never use it to approve an existing user's run.
No source conversation is changed. Generated files are copied into ignored out/ for inspection.
Run from harness: uv run python ../scripts/verify_html_generation.py --source-session <id>
"""
import argparse
import asyncio
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
from uuid import uuid4

import httpx

from agentforge.config import Settings
from agentforge.store import MongoDBStore


def safe_approval(tool, arguments):
    if tool == "write_file":
        path = PurePosixPath(arguments.get("path", "").removeprefix("/workspace/"))
        return (not path.is_absolute() and ".." not in path.parts and bool(path.parts)
                and not any(part.startswith(".") for part in path.parts)
                and path.suffix in {".html", ".css", ".js", ".json"})
    if tool == "run_command":
        try:
            lexer = shlex.shlex(arguments.get("command", ""), posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            parts = list(lexer)
        except ValueError:
            return False
        commands, current = [], []
        for part in parts:
            if part in {"&&", ";"}:
                if not current:
                    return False
                commands.append(current)
                current = []
            else:
                current.append(part)
        commands.append(current)
        for command in commands:
            if not command:
                return False
            skill_path = bool(re.fullmatch(r"/?(?:workspace/)?\.skills/[A-Za-z0-9_.-]+/SKILL\.md", command[-1]))
            if skill_path and ((len(command) == 4 and command[:2] == ["sed", "-n"]
                               and re.fullmatch(r"\d+,\d+p", command[2]))
                              or len(command) == 3 and command[:2] == ["wc", "-l"]):
                continue
            if (len(command) == 2 and command[0] == "cd" and command[1].startswith("/workspace/")
                    and re.fullmatch(r"[A-Za-z0-9_./-]+", command[1]) and ".." not in PurePosixPath(command[1]).parts):
                continue
            if command in [["ls", "-l"], ["echo", "JS OK"]]:
                continue
            if len(command) == 3 and command[:2] == ["node", "--check"] and re.fullmatch(r"[A-Za-z0-9_-]+\.js", command[2]):
                continue
            return False
        return bool(commands)
    return False


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-session", required=True)
    parser.add_argument("--url", default="http://127.0.0.1:5196")
    parser.add_argument("--approve-sandbox-checks", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    store = MongoDBStore(settings.mongodb_uri, settings.agentforge_db)
    identity = "html-verify-" + uuid4().hex
    root = (settings.agentforge_workspace / "sandboxes").resolve()
    before = set(root.rglob("*.html"))
    try:
        source = await store.get("sessions", args.source_session)
        checkpoint = await store.latest_checkpoint(args.source_session)
        if not source or not checkpoint:
            raise ValueError("Source session and failed task checkpoint are required")
        await store.put("sessions", {"id": identity, "isolated": True,
            "history": source.get("history", []), "loaded_skills": source.get("loaded_skills", [])})
        approvals = 0
        async with asyncio.timeout(360):
            async with httpx.AsyncClient(base_url=args.url, timeout=180, trust_env=False) as client:
                async with client.stream("POST", "/api/agents/run", json={"sessionId": identity,
                    "input": checkpoint["state"]["task"], "sandbox": True, "stream": True}) as response:
                    response.raise_for_status()
                    terminal = None
                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        event = json.loads(line[6:])
                        if event["type"] == "await_approval":
                            approved = safe_approval(event["tool"], event["arguments"])
                            if args.approve_sandbox_checks and event["tool"] == "run_command":
                                approved = True
                            decision = await client.post(f"/api/approvals/{event['call_id']}", json={"approved": approved})
                            decision.raise_for_status()
                            approvals += int(approved)
                            print(f"APPROVAL {event['tool']} approved={approved}", flush=True)
                        elif event["type"] == "step" and event["step"]["id"].startswith("recovery:"):
                            print("RECOVERY", event["step"]["output"], flush=True)
                        elif event["type"] in {"done", "error"}:
                            terminal = event
                    assert terminal and terminal["type"] == "done", terminal
                    assert terminal["result"]["stop_reason"] == "completed", terminal
        created = set(root.rglob("*.html")) - before
        written = {}
        for call in sorted(await store.list("tool_calls", {"trace_id": terminal["result"]["id"]}), key=lambda c: c["index"]):
            if call["call"]["name"] != "write_file" or not call["result"]["ok"]:
                continue
            arguments = call["call"]["arguments"]
            path = PurePosixPath(arguments["path"].removeprefix("/workspace/"))
            written[path] = (written.get(path, "") if arguments.get("append") else "") + arguments["content"]
        # Match this run's exact output rather than copying concurrent sessions' new files.
        created = {path for path in created if PurePosixPath(*path.relative_to(root).parts[1:]) in written
                   and path.read_bytes() == written[PurePosixPath(*path.relative_to(root).parts[1:])].encode("utf-8")}
        assert created, "No HTML file matching this test run's successful writes was found"
        output = Path(__file__).resolve().parents[1] / "out/html-generation" / identity
        for directory in {path.relative_to(root).parts[0] for path in created}:
            shutil.copytree(root / directory, output / directory, ignore=shutil.ignore_patterns(".skills", "__pycache__"))
        print("PASS: real model completed original HTML task; approved actions:", approvals, flush=True)
        print("GENERATED", output, flush=True)
    finally:
        await store.db.sessions.delete_one({"id": identity})
        store.client.close()


if __name__ == "__main__":
    asyncio.run(main())
