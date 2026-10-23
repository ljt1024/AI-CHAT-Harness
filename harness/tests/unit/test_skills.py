import json
from types import SimpleNamespace

from mongomock_motor import AsyncMongoMockClient
from pydantic import ValidationError
import pytest

from agentforge.skills import (SkillManager, SkillPackage, SkillTool, remove_synced_skill)
from agentforge.store import MongoDBStore
from agentforge.sandbox.docker_runner import DockerRunner


@pytest.mark.parametrize("path", ["../escape", "/absolute", "a/../b", "a//b", "a/./b",
                                 "a\\b", "a/", "x\x00y"])
def test_skill_rejects_unsafe_paths(path):
    with pytest.raises(ValidationError):
        SkillPackage(name="demo", files={"SKILL.md": "Instructions", path: "unsafe"})


@pytest.mark.parametrize("files", [{"scripts/run.py": "pass"}, {"SKILL.md": " "},
    {"SKILL.md": "x" * 24001}, {"SKILL.md": "run", "scripts": "x", "scripts/a.py": "x"}])
def test_skill_requires_bounded_instructions(files):
    with pytest.raises(ValidationError):
        SkillPackage(name="demo", files=files)


def test_skill_accepts_real_world_sized_files():
    large = "x" * 900_000
    package = SkillPackage(name="demo", files={"SKILL.md": "Instructions", "data/big.csv": large})
    assert package.files["data/big.csv"] == large
    with pytest.raises(ValidationError, match="1 MB"):
        SkillPackage(name="demo", files={"SKILL.md": "Instructions", "data/big.csv": "x" * 1_000_001})
    with pytest.raises(ValidationError, match="5 MB"):
        SkillPackage(name="demo", files={"SKILL.md": "Instructions",
            **{f"data/f{i}.csv": large for i in range(6)}})


async def test_delete_refuses_unknown_skills_and_saved_agent_references():
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    manager = SkillManager(store)
    installed = await manager.install(SkillPackage(name="demo", files={"SKILL.md": "Instructions"}))
    tool = SkillTool(manager, "delete_skill")
    missing = await tool.execute({"skill_id": "demo-0123456789ab"})
    assert not missing.ok and "not installed" in missing.error
    assert not (await tool.execute({"skill_id": "../../etc"})).ok
    await store.put("agents", {"id": "saved", "skills": [installed["id"]]})
    blocked = await tool.execute({"skill_id": installed["id"]})
    assert not blocked.ok and "referenced by a saved agent" in blocked.error
    assert await store.get("skills", installed["id"]) is not None
    await store.db.agents.delete_one({"id": "saved"})
    deleted = await tool.execute({"skill_id": installed["id"]})
    assert deleted.ok and json.loads(deleted.output)["deleted"] is True
    assert await store.get("skills", installed["id"]) is None
    assert await manager.catalog() == []


async def test_delete_unloads_skill_and_clears_synced_sandbox_files(tmp_path):
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    manager = SkillManager(store)
    installed = await manager.install(SkillPackage(name="demo", files={
        "SKILL.md": "Run scripts/a.py", "scripts/a.py": "print(42)"}))
    box = SimpleNamespace(workspace=SimpleNamespace(root=tmp_path))
    assert (await manager.sync([installed["id"]], box))["changed_files"] == 2
    agent = SimpleNamespace(skills=[installed["id"]])
    result = await SkillTool(manager, "delete_skill", None, box, agent).execute(
        {"skill_id": installed["id"]})
    assert result.ok
    assert json.loads(result.output)["removed_sandbox_files"] is True
    assert json.loads(result.output)["unloaded"] is True
    assert agent.skills == []
    assert not (tmp_path / ".skills" / installed["id"]).exists()
    assert (tmp_path / ".skills").is_dir()


def test_remove_synced_skill_never_follows_a_symlinked_directory(tmp_path):
    (tmp_path / ".skills").mkdir()
    (tmp_path / "victim").mkdir()
    (tmp_path / "victim" / "keep.txt").write_text("keep")
    (tmp_path / ".skills" / "demo-0123456789ab").symlink_to(tmp_path / "victim")
    with pytest.raises(ValueError, match="symlink"):
        remove_synced_skill(tmp_path, "demo-0123456789ab")
    assert (tmp_path / "victim" / "keep.txt").read_text() == "keep"
    with pytest.raises(ValueError, match="Invalid skill identity"):
        remove_synced_skill(tmp_path, "../victim")


async def test_install_is_content_addressed_and_durable():
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    manager = SkillManager(store)
    package = SkillPackage(name="demo", files={"SKILL.md": "Instructions", "scripts/a.py": "print(42)"})
    first = await manager.install(package)
    assert (await manager.install(package))["id"] == first["id"]
    assert first["id"] in await SkillManager(store).instructions([first["id"]])
    changed = await manager.install(SkillPackage(name="demo", files={"SKILL.md": "New instructions"}))
    assert changed["id"] != first["id"]
    with pytest.raises(ValueError, match="not installed"):
        await manager.packages(["missing"])


async def test_skill_discovery_is_available_without_sandbox_but_loading_is_denied():
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    manager = SkillManager(store)
    installed = await manager.install(SkillPackage(name="demo", files={"SKILL.md": "Instructions"}))
    result = await SkillTool(manager, "list_skills").execute({})
    assert json.loads(result.output)["skills"][0]["id"] == installed["id"]
    blocked = await SkillTool(manager, "load_skill").execute({"skill_id": installed["id"]})
    assert not blocked.ok and "sandbox conversation" in blocked.error


async def test_legacy_installed_skill_without_summary_remains_discoverable():
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    await store.put("skills", {"id": "legacy", "name": "demo", "files": {"SKILL.md": "Old instructions"}})
    catalog = await SkillManager(store).catalog()
    assert catalog[0]["summary"] == "Old instructions"


async def test_real_sandbox_incremental_sync_restore_and_symlink_denial(tmp_path, docker_client):
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    manager = SkillManager(store)
    installed = await manager.install(SkillPackage(name="demo", files={
        "SKILL.md": "Run scripts/a.py", "scripts/a.py": "print(42)"}))
    runner = DockerRunner(tmp_path, docker_client)
    box = await runner.create()
    try:
        ids = [installed["id"]]
        assert (await manager.sync(ids, box))["changed_files"] == 2
        assert (await manager.sync(ids, box))["changed_files"] == 0
        path = f".skills/{ids[0]}/scripts/a.py"
        result = await runner.execute(box, ["python", path])
        assert result.ok and result.output.strip() == "42"
        await runner.execute(box, ["python", "-c", f"from pathlib import Path; Path({json.dumps(path)}).unlink()"])
        await runner.destroy(box)
        box = await runner.create(workspace=box.workspace)
        assert (await manager.sync(ids, box))["changed_files"] == 1
        assert (await runner.execute(box, ["python", path])).output.strip() == "42"
        await runner.execute(box, ["python", "-c",
            f"from pathlib import Path; p=Path({json.dumps(path)}); p.unlink(); p.symlink_to('/workspace/victim')"])
        with pytest.raises(ValueError, match="sync failed"):
            await manager.sync(ids, box)
        assert not (box.workspace.root / "victim").exists()
    finally:
        await runner.destroy(box)


async def test_real_sandbox_syncs_files_larger_than_exec_argv_limit(tmp_path, docker_client):
    store = MongoDBStore("", "test", client=AsyncMongoMockClient())
    manager = SkillManager(store)
    payload = "print(len(open(__file__).read()))\n" + "#" * 200_000
    installed = await manager.install(SkillPackage(name="demo", files={
        "SKILL.md": "Run scripts/big.py", "scripts/big.py": payload}))
    runner = DockerRunner(tmp_path, docker_client)
    box = await runner.create()
    try:
        assert (await manager.sync([installed["id"]], box))["changed_files"] == 2
        result = await runner.execute(box, ["python", f".skills/{installed['id']}/scripts/big.py"])
        assert result.ok and result.output.strip() == str(len(payload))
    finally:
        await runner.destroy(box)
