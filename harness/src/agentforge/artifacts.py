"""Persist bounded HTML deliveries independently of the sandbox lifetime."""

import asyncio
import base64
from datetime import datetime, timezone
import hashlib
from html import escape
from html.parser import HTMLParser
import mimetypes
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from agentforge.events import EventBus
from agentforge.models import new_id

MAX_ARTIFACT_BYTES = 2_000_000
MAX_ARTIFACTS = 20


class HtmlBundle(HTMLParser):
    def __init__(self, workspace, path):
        super().__init__(convert_charrefs=False)
        self.workspace, self.path = workspace, path
        self.parts = []
        self.total = 0

    def asset(self, value):
        url = urlsplit(value)
        if not value or url.scheme or url.netloc or value.startswith(("/", "#")):
            return None
        candidate = self.workspace.path(str(
            self.path.parent.relative_to(self.workspace.root) / unquote(url.path)))
        if candidate.is_symlink() or any(p.startswith(".") for p in candidate.relative_to(self.workspace.root).parts):
            raise ValueError("Internal assets cannot be delivered")
        with candidate.open("rb") as handle:
            data = handle.read(MAX_ARTIFACT_BYTES + 1)
        self.total += len(data)
        if self.total > MAX_ARTIFACT_BYTES:
            raise ValueError("HTML and its assets exceed the 2 MB delivery limit")
        return data

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        try:
            if tag == "link" and values.get("rel", "").lower() == "stylesheet":
                data = self.asset(values.get("href", ""))
                if data is not None:
                    css = re.sub(r"</style", r"<\\/style", data.decode("utf-8"), flags=re.I)
                    self.parts.append(f"<style>{css}</style>")
                    return
            if tag == "script" and values.get("src"):
                data = self.asset(values["src"])
                if data is not None:
                    attributes = "".join(f' {key}="{escape(value or "", quote=True)}"'
                                         for key, value in attrs if key != "src")
                    script = re.sub(r"</script", r"<\\/script", data.decode("utf-8"), flags=re.I)
                    self.parts.append(f"<script{attributes}>{script}")
                    return
            if tag == "img" and values.get("src"):
                data = self.asset(values["src"])
                if data is not None:
                    mime = mimetypes.guess_type(values["src"])[0] or "application/octet-stream"
                    values["src"] = f"data:{mime};base64,{base64.b64encode(data).decode()}"
                    self.parts.append("<img" + "".join(
                        f' {key}="{escape(value or "", quote=True)}"' for key, value in values.items()) + ">")
                    return
        except FileNotFoundError:
            # Subsequent writes may supply this dependency; the final snapshot retries it.
            pass
        self.parts.append(self.get_starttag_text())

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        self.parts.append(data)

    def handle_entityref(self, name):
        self.parts.append(f"&{name};")

    def handle_charref(self, name):
        self.parts.append(f"&#{name};")

    def handle_decl(self, decl):
        self.parts.append(f"<!{decl}>")

    def handle_comment(self, data):
        self.parts.append(f"<!--{data}-->")


def bundle_html(workspace, relative):
    path = workspace.path(relative)
    parser = HtmlBundle(workspace, path)
    data = parser.asset(path.name)
    parser.feed(data.decode("utf-8"))
    parser.close()
    result = "".join(parser.parts).encode("utf-8")
    if len(result) > MAX_ARTIFACT_BYTES:
        raise ValueError("Bundled HTML exceeds the 2 MB delivery limit")
    return result


class ArtifactEventBus(EventBus):
    def __init__(self, runtime, store, workspace):
        super().__init__(runtime)
        self.store, self.workspace = store, workspace
        self.calls, self.html_paths = {}, {}
        self.discover_call = None

    async def emit(self, event):
        kind = event["type"]
        if kind == "tool_call":
            call = event["call"]
            self.calls[call["id"]] = call
        elif kind == "tool_result":
            call = self.calls.pop(event["call_id"], {})
            if event["result"]["ok"]:
                if call.get("name") == "write_file":
                    path = call["arguments"]["path"]
                    if Path(path).suffix.lower() in {".html", ".htm", ".css", ".js"}:
                        self.discover_call = call["id"]
                    if Path(path).suffix.lower() in {".html", ".htm"} and len(self.html_paths) < MAX_ARTIFACTS:
                        self.html_paths[path] = call["id"]
                elif call.get("name") == "run_command":
                    self.discover_call = call["id"]
        elif kind == "done":
            # CSS/JS-only follow-up turns must deliver a fresh HTML snapshot too.
            if self.discover_call:
                workspace = self.workspace()
                for path in workspace.files():
                    if len(self.html_paths) >= MAX_ARTIFACTS:
                        break
                    relative = str(path.relative_to(workspace.root))
                    if path.suffix.lower() in {".html", ".htm"} and not any(
                            p.startswith(".") for p in Path(relative).parts):
                        self.html_paths.setdefault(relative, self.discover_call)
            artifacts = []
            for path, call_id in self.html_paths.items():
                try:
                    content = await asyncio.to_thread(bundle_html, self.workspace(), path)
                except (OSError, ValueError, UnicodeError):
                    await super().emit({"type": "step", "sessionId": event["sessionId"],
                        "turnId": event["turnId"], "step": {"id": new_id(), "phase": "observation",
                        "status": "failed", "output": f"无法交付 HTML 文件 {path}：文件或依赖不可访问，或超过 2 MB。"}})
                    continue
                identity = new_id()
                artifact = {"fileId": identity, "toolCallId": call_id,
                    "fileName": Path(path).name, "format": "html", "mimeType": "text/html",
                    "size": len(content), "downloadPath": f"/api/files/{identity}/download",
                    "createdAt": datetime.now(timezone.utc).isoformat()}
                await self.store.put("artifacts", {"id": identity, "sessionId": event["sessionId"],
                    "artifact": artifact, "content": content, "sha256": hashlib.sha256(content).hexdigest()})
                artifacts.append(artifact)
                await super().emit({"type": "artifact", "sessionId": event["sessionId"],
                                    "turnId": event["turnId"], "artifact": artifact})
            event = {**event, "result": {**event["result"], "artifacts": artifacts}}
        await super().emit(event)
