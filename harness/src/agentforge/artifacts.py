"""Deliver what the agent left in the workspace, independently of the sandbox.

A sandbox is torn down the moment a turn ends, so anything the agent wants the
user to see has to be copied out first. HTML was the only format that made the
trip; a report the agent rendered to PDF or a deck it built as PPTX stayed in the
workspace, invisible to the conversation. These formats are delivered the same
way now, each with whatever preview the browser can render on its own: PDF pages
through pdf.js, images as they are, and the Office formats through a bounded
text view of their content.
"""

import asyncio
import base64
from datetime import datetime, timezone
import hashlib
from html import escape
from html.parser import HTMLParser
import json
import mimetypes
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from agentforge.events import EventBus
from agentforge.models import new_id
from agentforge.previews import build_preview

MAX_ARTIFACT_BYTES = 2_000_000
MAX_ARTIFACTS = 20
# Images are cheap to deliver and easy to generate in bulk; a deck of fifty
# charts should not push the actual deck out of the delivery.
MAX_IMAGE_ARTIFACTS = 6

# Files worth handing to the browser. Everything else in the workspace -- source,
# data, intermediate CSV -- is the agent's, not the reader's.
PREVIEW_FORMATS = {".html": "html", ".htm": "html", ".pdf": "pdf", ".xlsx": "xlsx",
                   ".pptx": "pptx", ".docx": "docx", ".png": "image", ".jpg": "image",
                   ".jpeg": "image", ".gif": "image", ".webp": "image"}

MIME_TYPES = {".html": "text/html", ".htm": "text/html", ".pdf": "application/pdf",
              ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
              ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
              ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
              ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".gif": "image/gif", ".webp": "image/webp"}

# Dependency folders a generated file never lives in, but a workspace scan can
# still walk into.
SKIP_DIRECTORIES = {"node_modules", "__pycache__", ".venv", "venv", ".git", "dist", "build"}


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


def read_document(workspace, relative):
    """Read a workspace file whole, refusing anything past the delivery limit."""
    with workspace.path(relative).open("rb") as handle:
        data = handle.read(MAX_ARTIFACT_BYTES + 1)
    if len(data) > MAX_ARTIFACT_BYTES:
        raise ValueError(f"{relative} exceeds the 2 MB delivery limit")
    return data


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
        self.calls, self.paths = {}, {}
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
                    if Path(path).suffix.lower() in PREVIEW_FORMATS:
                        self.paths[path] = call["id"]
                elif call.get("name") == "run_command":
                    self.discover_call = call["id"]
        elif kind == "done":
            # CSS/JS-only follow-up turns must deliver a fresh HTML snapshot too,
            # and a binary file -- a rendered PDF, a built deck -- is only ever
            # written by a command, so it can only be found by looking.
            if self.discover_call:
                workspace = self.workspace()
                for path in workspace.files():
                    relative = str(path.relative_to(workspace.root))
                    if path.suffix.lower() not in PREVIEW_FORMATS:
                        continue
                    if any(part.startswith(".") or part in SKIP_DIRECTORIES
                           for part in Path(relative).parts):
                        continue
                    self.paths.setdefault(relative, self.discover_call)
            artifacts = await self._deliver(event)
            event = {**event, "result": {**event["result"], "artifacts": artifacts}}
        await super().emit(event)

    async def _deliver(self, event):
        workspace = self.workspace()
        candidates = []
        for relative, call_id in self.paths.items():
            try:
                modified = workspace.path(relative).stat().st_mtime
            except (OSError, ValueError):
                continue
            candidates.append((modified, relative, call_id))
        # Newest first: what the agent just produced outranks what it left behind.
        candidates.sort(reverse=True)
        delivered, images = [], 0
        for _, relative, call_id in candidates:
            suffix = Path(relative).suffix.lower()
            fmt = PREVIEW_FORMATS[suffix]
            if fmt == "image":
                images += 1
                if images > MAX_IMAGE_ARTIFACTS:
                    continue
            if len(delivered) >= MAX_ARTIFACTS:
                break
            try:
                delivered.append(await self._deliver_one(event, relative, suffix, fmt, call_id))
            except (OSError, ValueError, UnicodeError) as cause:
                await super().emit({"type": "step", "sessionId": event["sessionId"],
                    "turnId": event["turnId"], "step": {"id": new_id(), "phase": "observation",
                    "status": "failed",
                    "output": f"无法交付文件 {relative}：{cause}，或超过 2 MB。"}})
        return delivered

    async def _deliver_one(self, event, relative, suffix, fmt, call_id):
        workspace = self.workspace()
        name = Path(relative).name
        if fmt == "html":
            content = await asyncio.to_thread(bundle_html, workspace, relative)
        else:
            content = await asyncio.to_thread(read_document, workspace, relative)
        digest = hashlib.sha256(content).hexdigest()
        # A file the agent did not touch since the last turn was delivered then;
        # hand back that delivery instead of storing the same bytes again.
        artifact = next((record["artifact"] for record in
                         await self.store.list("artifacts", {"sessionId": event["sessionId"],
                                                             "sha256": digest})
                         if record["artifact"]["fileName"] == name), None)
        if artifact is None:
            identity = new_id()
            artifact = {"fileId": identity, "toolCallId": call_id, "fileName": name,
                "format": fmt, "mimeType": MIME_TYPES[suffix], "size": len(content),
                "downloadPath": f"/api/files/{identity}/download",
                "createdAt": datetime.now(timezone.utc).isoformat()}
            preview = build_preview(fmt, content)
            if preview is not None:
                kind, payload = preview
                artifact["previewFileId"] = await self._store_preview(
                    event, name, fmt, kind, payload)
                artifact["previewKind"] = kind
            await self.store.put("artifacts", {"id": identity, "sessionId": event["sessionId"],
                "artifact": artifact, "content": content, "sha256": digest})
        await super().emit({"type": "artifact", "sessionId": event["sessionId"],
                            "turnId": event["turnId"], "artifact": artifact})
        return artifact

    async def _store_preview(self, event, name, fmt, kind, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        identity = new_id()
        # The preview is a sidecar, never a download of its own: the browser
        # renders it, the user downloads the original.
        await self.store.put("artifacts", {"id": identity, "sessionId": event["sessionId"],
            "delivery": "preview", "artifact": {"fileId": identity, "fileName": f"{name}.json",
                "format": fmt, "previewKind": kind, "mimeType": "application/json",
                "size": len(body), "downloadPath": f"/api/files/{identity}/download"},
            "content": body, "sha256": hashlib.sha256(body).hexdigest()})
        return identity
