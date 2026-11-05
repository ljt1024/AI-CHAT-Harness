import io
import tarfile

import pytest
from pymongo.errors import PyMongoError

from agentforge.models import new_id
from agentforge.sandbox.snapshot import SnapshotStore, archive_workspace, validate_archive
from agentforge.store import MongoDBStore
from agentforge.tools.fs import Workspace


@pytest.mark.parametrize("name", ["../escape", "/absolute", ".env", "nested/../../escape"])
def test_reject_unsafe_archive(tmp_path, name):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo(name)
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(ValueError):
        validate_archive(buffer.getvalue(), Workspace(tmp_path))


def test_reject_symlink(tmp_path):
    (tmp_path / "link").symlink_to("/etc/passwd")
    with pytest.raises(ValueError, match="symbolic"):
        archive_workspace(Workspace(tmp_path))


async def test_real_gridfs_roundtrip_and_session_isolation(tmp_path):
    database = "agentforge_test_" + new_id().replace("-", "")
    store = MongoDBStore("mongodb://127.0.0.1:27017", database)
    try:
        await store.client.admin.command("ping")
    except PyMongoError:
        store.client.close()
        pytest.skip("Real MongoDB required")
    try:
        await store.setup()
        source = Workspace(tmp_path / "source")
        source.path("file.txt").write_text("checkpoint data")
        snapshots = SnapshotStore(store)
        record = await snapshots.save("session", source)
        destination = Workspace(tmp_path / "restored")
        with pytest.raises(ValueError, match="belong"):
            await snapshots.restore(record["id"], "other", destination)
        await snapshots.restore(record["id"], "session", destination)
        assert destination.path("file.txt").read_text() == "checkpoint data"
        with pytest.raises(ValueError, match="empty"):
            await snapshots.restore(record["id"], "session", destination)
    finally:
        await store.client.drop_database(database)
        store.client.close()
