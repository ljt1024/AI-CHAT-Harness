"""Uploaded files and generated artifacts.

Two doors, one URL space: the reader should not have to know whether bytes were
produced by a run or handed to it, so `/api/files/{id}/…` resolves both and the
ownership checks live in `AppDeps.owned_file`.
"""
import logging
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse

from agentforge import session_files
from agentforge.api.deps import AppDeps
from agentforge.auth import is_guest

logger = logging.getLogger(__name__)


def serve_file(body, mime: str, name: str, disposition: str):
    headers = {
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(name, safe='')}",
        "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
    }
    # Generated HTML is the one delivery the browser is allowed to render, and
    # only inside a sandbox; it is never inline, so no CSP is needed elsewhere.
    if mime == "text/html":
        headers["Content-Security-Policy"] = (
            "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:")
    if isinstance(body, bytes):
        return Response(body, media_type=mime, headers=headers)
    return FileResponse(body, media_type=mime, headers=headers)


def build_router(d: AppDeps) -> APIRouter:
    router = APIRouter()
    store, file_store = d.store, d.file_store
    ready, owner_id = d.ready, d.owner_id
    session_or_new, owned_file = d.session_or_new, d.owned_file
    public_file = d.public_file

    @router.post("/api/sessions/{session_id}/files", status_code=201)
    async def upload_session_file(session_id: str, file: UploadFile):
        """Take one file into the conversation's workspace.

        Guests are refused: an anonymous owner id is free to mint, and a disk
        that any visitor can fill is a denial of service, not a feature.
        """
        ready()
        uid = owner_id()
        if is_guest(uid):
            raise HTTPException(401, detail={"code": "sign_in_required",
                                             "reason": "Uploading a file needs an account."})
        await session_or_new(session_id)
        records = await store.list("session_files", {"sessionId": session_id}, owner=uid)
        if len(records) >= file_store.max_files:
            raise HTTPException(409, f"This conversation already holds {file_store.max_files} files. "
                                     "Delete one before uploading another.")
        used = sum(row.get("size", 0) for row in await store.list("session_files", {}, owner=uid))
        budget = min(file_store.max_bytes, file_store.max_owner_bytes - used)
        if budget <= 0:
            raise HTTPException(409, "Your upload allowance is used up. Delete a file to free space.")
        try:
            record = await file_store.save(session_id, file, budget, {row["label"] for row in records})
        except session_files.UploadRejected as exc:
            raise HTTPException(exc.status, str(exc)) from None
        stored = await store.put("session_files", record, owner=uid)
        logger.info("file uploaded session=%s owner=%s size=%d type=%s",
                    session_id, uid, stored["size"], stored["mimeType"])
        return {"code": 201, "data": public_file(stored)}

    @router.get("/api/sessions/{session_id}/files")
    async def list_session_files(session_id: str):
        ready()
        await session_or_new(session_id)
        records = await store.list("session_files", {"sessionId": session_id}, owner=owner_id())
        return {"code": 200, "data": [public_file(row) for row in
                                      sorted(records, key=lambda row: row.get("createdAt", ""))]}

    @router.delete("/api/sessions/{session_id}/files/{file_id}")
    async def delete_session_file(session_id: str, file_id: str):
        ready()
        uid = owner_id()
        await session_or_new(session_id)
        record = await store.get("session_files", file_id, owner=uid)
        if record is None or record.get("sessionId") != session_id:
            raise HTTPException(404, "File not found.")
        await store.db.session_files.delete_one({"id": file_id, "owner_id": uid})
        file_store.blob(session_id, file_id).unlink(missing_ok=True)
        return {"code": 200, "data": {"fileId": file_id}}

    @router.get("/api/files/{file_id}/download")
    async def download_artifact(file_id: str):
        """Always an attachment: a file is never a document of ours."""
        ready()
        body, mime, name = await owned_file(file_id)
        return serve_file(body, mime, name, "attachment")

    @router.get("/api/files/{file_id}/view")
    async def view_artifact(file_id: str):
        """The one file the browser is allowed to render for itself: a PDF.

        Rendering it here meant shipping pdf.js and a worker, and pdf.js draws
        nothing for text set in a CJK font the document does not embed —
        ReportLab's STSong-Light is the common case — because mapping those glyph
        ids needs CMap tables we would also have to serve. The browser's own
        viewer substitutes a system font, so the same file renders. Everything
        else stays an attachment: an uploaded HTML file must never render on this
        origin, and a caller that asks for one gets a refusal rather than a
        silently different disposition.
        """
        ready()
        body, mime, name = await owned_file(file_id)
        if mime != "application/pdf":
            raise HTTPException(415, "Only a PDF can be shown in the viewer.")
        return serve_file(body, mime, name, "inline")

    return router
