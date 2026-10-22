import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import tempfile

from pydantic import BaseModel, Field, ValidationError, model_validator

from agentforge.models import ToolResult
from agentforge.skill_sources import (DOC_LIMIT, FILE_LIMIT, PACKAGE_LIMIT,
    SkillInstallRequest, resolve_skill_source)


SKILL_TOOL_NAMES = ("list_skills", "load_skill", "install_skill", "delete_skill")
# Installed IDs are "<name>-<12 hex>"; anything else must never reach a delete path.
SKILL_ID_PATTERN = r"[a-z][a-z0-9-]{0,39}-[a-f0-9]{12}"


class SkillPackage(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,39}$")
    files: dict[str, str] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_files(self):
        if not self.files.get("SKILL.md", "").strip():
            raise ValueError("A nonempty SKILL.md is required at the folder root.")
        if len(self.files["SKILL.md"].encode()) > DOC_LIMIT:
            raise ValueError("SKILL.md must be at most 24 KB.")
        total = 0
        for name, content in self.files.items():
            if "\x00" in content:
                raise ValueError("Binary files containing NUL bytes are not supported.")
            path = PurePosixPath(name)
            if (not re.fullmatch(r"[a-zA-Z0-9_./-]{1,160}", name) or path.is_absolute()
                    or any(part in {".", ".."} for part in name.split("/"))
                    or str(path) != name):
                raise ValueError("Skill files must have safe relative paths.")
            if len(content.encode()) > FILE_LIMIT:
                raise ValueError("Each skill file must be at most 1 MB.")
            total += len(content.encode())
            if any(other.startswith(name + "/") for other in self.files):
                raise ValueError("A file cannot also be a directory.")
        if total > PACKAGE_LIMIT:
            raise ValueError("A skill package must be at most 5 MB.")
        return self


def sync_skill_file(root: Path, relative: str, content: str) -> bool:
    """Write one skill file into the sandbox workspace from the host side.

    The sandbox workspace is bind-mounted at /workspace, so files written here
    are immediately visible inside the container. Unlike the previous
    in-container sync worker, content never travels through exec argv, which
    Linux caps at 128 KB per argument. Never follow a symlink into user files.
    """
    parts = PurePosixPath(relative).parts
    directory = root
    for part in parts[:-1]:
        directory = directory / part
        if directory.is_symlink():
            raise ValueError("Skill directory is a symlink")
        if not directory.exists():
            directory.mkdir()
    target = root / relative
    if target.is_symlink():
        raise ValueError("Skill file is a symlink")
    data = content.encode()
    changed = not target.exists() or hashlib.sha256(target.read_bytes()).digest() \
        != hashlib.sha256(data).digest()
    if changed:
        fd, temp = tempfile.mkstemp(dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(temp, target)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
    return changed


def remove_synced_skill(root: Path, identity: str) -> bool:
    """Delete a skill's synced directory from a sandbox workspace from the host side.

    Only the exact "<name>-<digest>" directory under .skills is touched, so a
    deleted skill cannot take user files with it; a symlinked skill directory is
    refused instead of followed.
    """
    if not re.fullmatch(SKILL_ID_PATTERN, identity):
        raise ValueError("Invalid skill identity.")
    target = root / ".skills" / identity
    if target.is_symlink():
        raise ValueError("Skill directory is a symlink")
    if not target.is_dir():
        return False
    shutil.rmtree(target)
    return True


class SkillManager:
    def __init__(self, store):
        self.store = store

    async def install(self, package: SkillPackage, provenance=None):
        digest = hashlib.sha256(json.dumps(package.files, sort_keys=True).encode()).hexdigest()
        return await self.store.put("skills", {"id": f"{package.name}-{digest[:12]}",
            "name": package.name, "sha256": digest, "summary": package.files["SKILL.md"][:120],
            "files": package.files, "provenance": provenance or {"source": "upload"}})

    async def catalog(self):
        packages = await self.store.db.skills.find({}, {"_id": 0, "id": 1, "name": 1,
            "summary": 1}).sort("name", 1).to_list(length=10000)
        for package in packages:
            if "summary" not in package:
                original = await self.store.get("skills", package["id"])
                package["summary"] = original["files"]["SKILL.md"][:120]
        return packages

    async def packages(self, identities):
        packages = []
        for identity in dict.fromkeys(identities):
            package = await self.store.get("skills", identity)
            if not package:
                raise ValueError(f"Skill is not installed: {identity}")
            packages.append(package)
        return packages

    async def instructions(self, identities):
        return "\n\n".join(f"Skill: {p['name']}\nDirectory: /workspace/.skills/{p['id']}\n"
            f"{p['files']['SKILL.md']}" for p in await self.packages(identities))

    async def sync(self, identities, sandbox):
        changed = 0
        try:
            for package in await self.packages(identities):
                prefix = PurePosixPath(".skills") / package["id"]
                for path, content in package["files"].items():
                    changed += sync_skill_file(sandbox.workspace.root, str(prefix / path), content)
        except (OSError, ValueError) as exc:
            raise ValueError("Skill sync failed. Check sandbox health and .skills paths.") from exc
        return {"changed_files": changed}

    async def delete(self, identity):
        """Remove an installed skill, refusing when a saved agent still selects it."""
        package = await self.store.get("skills", identity)
        if package is None:
            raise ValueError(f"Skill is not installed: {identity}")
        if await self.store.list("agents", {"skills": identity}):
            raise ValueError("Skill is referenced by a saved agent; remove it from that agent first.")
        await self.store.db.skills.delete_one({"id": identity})
        return package


class SkillTool:
    source = "builtin"
    permission = "auto"

    def __init__(self, manager, name, runner=None, sandbox=None, agent=None,
                 on_loaded=None, on_installed=None, on_deleted=None):
        self.manager, self.name = manager, name
        self.runner, self.sandbox, self.agent = runner, sandbox, agent
        self.on_loaded = on_loaded
        self.on_installed = on_installed
        self.on_deleted = on_deleted
        if name == "install_skill":
            self.permission = "confirm"
            self.description = "Install a skill after human approval. Sources: github (public GitHub folder URL, optional revision/path), workspace (folder in current Docker /workspace), inline (explicit name and UTF-8 files including SKILL.md). GitHub files are downloaded by this backend tool even when the sandbox is offline or the shell is restricted; no shell network access is needed. Never use pip/npm/curl or run_command to install skills. Import only; does not execute scripts or install dependencies. Return the installed ID, then use load_skill if requested. Ask the user for a source if missing; never invent one."
            self.schema = SkillInstallRequest.model_json_schema()
        elif name == "delete_skill":
            self.permission = "confirm"
            self.description = "Delete an installed skill after human approval. Removes the skill record, its files already synced into this sandbox and its active load. Refused when a saved agent still selects the skill. Deleting installed files is irreversible; confirm the exact skill_id from list_skills and never invent one."
            self.schema = {"type": "object", "properties": {"skill_id": {
                "type": "string", "pattern": SKILL_ID_PATTERN}}, "required": ["skill_id"],
                "additionalProperties": False}
        elif name == "list_skills":
            self.description = "Discover installed skills, their IDs and summaries. Inspect this catalog before claiming a skill is unavailable."
            self.schema = {"type": "object", "properties": {"offset": {
                "type": "integer", "minimum": 0}}, "additionalProperties": False}
        else:
            self.description = "Load a relevant installed skill by ID: read SKILL.md and synchronize its files into the sandbox. Then follow its instructions using run_command inside the offline Docker sandbox; commands run automatically unless the agent explicitly requires approval."
            self.schema = {"type": "object", "properties": {"skill_id": {"type": "string"}},
                "required": ["skill_id"], "additionalProperties": False}

    async def execute(self, args):
        if self.name == "delete_skill":
            identity = args.get("skill_id")
            if not isinstance(identity, str) or not re.fullmatch(SKILL_ID_PATTERN, identity):
                return ToolResult(ok=False, error="delete_skill requires an exact installed skill_id from list_skills.")
            try:
                package = await self.manager.delete(identity)
                cleaned = False
                if self.sandbox is not None:
                    cleaned = remove_synced_skill(self.sandbox.workspace.root, identity)
            except ValueError as exc:
                return ToolResult(ok=False, error=str(exc))
            unloaded = False
            if self.agent is not None and identity in self.agent.skills:
                self.agent.skills.remove(identity)
                unloaded = True
            if self.on_deleted:
                await self.on_deleted(package)
            return ToolResult(ok=True, output=json.dumps({"deleted": True, "id": identity,
                "name": package["name"], "removed_sandbox_files": cleaned, "unloaded": unloaded,
                "next": "Use list_skills to verify the catalog. Re-install with install_skill if it was removed by mistake."}))
        if self.name == "install_skill":
            try:
                request = SkillInstallRequest.model_validate(args)
                source = await resolve_skill_source(request, self.sandbox)
                package = SkillPackage(name=source.name, files=source.files)
                installed = await self.manager.install(package, source.provenance)
            except (UnicodeError, tarfile.TarError):
                return ToolResult(ok=False, error="Skill import failed: only UTF-8 text files in a valid skill folder are supported. Nothing was installed.")
            except ValidationError as exc:
                return ToolResult(ok=False, error="Skill install rejected: " + "; ".join(
                    e["msg"] for e in exc.errors(include_input=False)[:3]))
            except ValueError as exc:
                return ToolResult(ok=False, error=str(exc))
            if self.on_installed:
                await self.on_installed(installed)
            return ToolResult(ok=True, output=json.dumps({"installed": True, "id": installed["id"],
                "name": installed["name"], "sha256": installed["sha256"],
                "file_count": len(installed["files"]), "files": list(installed["files"])[:8],
                "provenance": installed["provenance"], "next": "Use load_skill with this ID to activate it. No scripts or dependency installers were executed."}))
        if self.name == "list_skills":
            catalog = await self.manager.catalog()
            offset = args.get("offset", 0)
            return ToolResult(ok=True, output=json.dumps({"skills": catalog[offset:offset + 8],
                "next_offset": offset + 8 if len(catalog) > offset + 8 else None}, ensure_ascii=False))
        if self.sandbox is None:
            return ToolResult(ok=False, error="This conversation uses host tools. Start a new Docker sandbox conversation to load and run skills; the skill catalog is available via list_skills.")
        identity = args["skill_id"]
        package = (await self.manager.packages([identity]))[0]
        if identity not in self.agent.skills and len(self.agent.skills) >= 8:
            return ToolResult(ok=False, error="At most eight skills can be loaded per run.")
        sync = await self.manager.sync([identity], self.sandbox)
        instructions = await self.manager.instructions([identity])
        if identity not in self.agent.skills:
            self.agent.skills.append(identity)
        if self.on_loaded:
            await self.on_loaded(package)
        return ToolResult(ok=True, output=f"Loaded skill. Synced {sync['changed_files']} files. "
            "Use read_file on the skill's SKILL.md for full instructions if this result is truncated. "
            "Run scripts only through run_command in the Docker sandbox. Routine sandbox commands "
            "run automatically unless the agent explicitly requires approval.\n" + instructions)
