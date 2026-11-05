import asyncio
import hashlib
import io
from pathlib import PurePosixPath
import tarfile

from motor.motor_asyncio import AsyncIOMotorGridFSBucket

from agentforge.models import new_id
from agentforge.tools.fs import Workspace

MAX_BYTES = 32 * 1024 * 1024
MAX_FILES = 2000


def archive_workspace(workspace: Workspace) -> bytes:
    buffer = io.BytesIO()
    total = 0
    count = 0
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for path in sorted(workspace.root.rglob("*")):
            if path.is_symlink():
                raise ValueError("Snapshots do not support symbolic links.")
            if path.is_dir():
                continue
            if not path.is_file():
                raise ValueError("Snapshots only support regular files.")
            relative = str(path.relative_to(workspace.root))
            workspace.path(relative)
            count += 1
            size = path.stat().st_size
            total += size
            if count > MAX_FILES or total > MAX_BYTES:
                raise ValueError("Workspace exceeds snapshot limits.")
            with path.open("rb") as handle:
                info = tarfile.TarInfo(relative)
                info.size, info.mode = size, 0o600
                archive.addfile(info, handle)
    data = buffer.getvalue()
    if len(data) > MAX_BYTES:
        raise ValueError("Archive exceeds snapshot byte limit.")
    return data


def validate_archive(data: bytes, workspace: Workspace) -> list[tuple[str, bytes]]:
    if len(data) > MAX_BYTES:
        raise ValueError("Archive exceeds snapshot byte limit.")
    files, names, total = [], set(), 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        for item in archive:
            name = PurePosixPath(item.name)
            if not item.isfile() or name.is_absolute() or ".." in name.parts or "\\" in item.name:
                raise ValueError("Unsafe snapshot member.")
            if str(name) in names or not name.parts:
                raise ValueError("Duplicate or empty snapshot path.")
            workspace.path(str(name))
            names.add(str(name))
            total += item.size
            if len(names) > MAX_FILES or item.size < 0 or total > MAX_BYTES:
                raise ValueError("Snapshot exceeds extraction limits.")
            handle = archive.extractfile(item)
            content = handle.read(item.size + 1)
            if len(content) != item.size:
                raise ValueError("Snapshot member size mismatch.")
            files.append((str(name), content))
    for name in names:
        if any(str(parent) in names for parent in PurePosixPath(name).parents):
            raise ValueError("Snapshot contains conflicting file paths.")
    return files


class SnapshotStore:
    def __init__(self, store, bucket=None):
        self.store = store
        self.bucket = bucket if bucket is not None else AsyncIOMotorGridFSBucket(store.db)

    async def save(self, session_id: str, workspace: Workspace):
        data = await asyncio.to_thread(archive_workspace, workspace)
        identity = new_id()
        grid_id = await self.bucket.upload_from_stream(identity + ".tar", data,
            metadata={"sessionId": session_id, "sha256": hashlib.sha256(data).hexdigest()})
        record = {"id": identity, "sessionId": session_id, "grid_id": str(grid_id),
                  "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        try:
            return await self.store.put("snapshots", record)
        except BaseException:
            await self.bucket.delete(grid_id)
            raise

    async def restore(self, identity: str, session_id: str, workspace: Workspace):
        from bson import ObjectId
        record = await self.store.get("snapshots", identity)
        if record is None or record["sessionId"] != session_id:
            raise ValueError("Snapshot does not belong to this session.")
        stream = await self.bucket.open_download_stream(ObjectId(record["grid_id"]))
        try:
            if stream.length > MAX_BYTES:
                raise ValueError("Stored snapshot exceeds byte limit.")
            data = await stream.read()
        finally:
            stream.close()
        if hashlib.sha256(data).hexdigest() != record["sha256"]:
            raise ValueError("Snapshot integrity check failed.")
        files = await asyncio.to_thread(validate_archive, data, workspace)
        # Restore to a fresh workspace so removed files cannot survive a rollback.
        if any(workspace.root.iterdir()):
            raise ValueError("Restore requires an empty workspace.")
        for name, content in files:
            path = workspace.path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(path.write_bytes, content)
        return record
