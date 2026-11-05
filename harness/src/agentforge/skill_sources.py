import asyncio
import base64
from dataclasses import dataclass
import io
import json
import os
from pathlib import PurePosixPath
import re
import tarfile
import zlib
from typing import Literal
from urllib.parse import quote, unquote, urlsplit

import httpx
from docker.errors import DockerException
from pydantic import BaseModel, ConfigDict, Field, model_validator


# Bounds chosen so real-world skills (e.g. nextlevelbuilder/ui-ux-pro-max-skill,
# whose folder holds 73 UTF-8 files totalling ~3.4 MB with data files up to ~824 KB)
# import successfully while keeping packages well inside the 16 MB MongoDB document limit.
FILE_LIMIT, PACKAGE_LIMIT, FILE_COUNT, DOC_LIMIT = 1_000_000, 5_000_000, 100, 24_000
# Transport caps: metadata per response, whole-repo tar.gz, and total bytes per install.
METADATA_LIMIT, ARCHIVE_LIMIT, DOWNLOAD_LIMIT = 2_000_000, 32_000_000, 12_000_000


def relative_path(value):
    path = PurePosixPath(value)
    if (not re.fullmatch(r"[a-zA-Z0-9_./-]{1,160}", value) or path.is_absolute()
            or any(part in {".", ".."} for part in value.split("/")) or str(path) != value):
        raise ValueError("Use a safe relative skill folder path without '..' or symlinks.")
    return value


class SkillInstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["github", "workspace", "inline"]
    name: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,39}$")
    url: str | None = Field(default=None, max_length=2000)
    path: str | None = Field(default=None, max_length=160)
    revision: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_./-]{0,99}$")
    files: dict[str, str] | None = Field(default=None, max_length=FILE_COUNT)

    @model_validator(mode="after")
    def validate_source(self):
        if self.source == "github":
            if not self.url or self.files is not None:
                raise ValueError("GitHub installs require a public GitHub URL, not inline files.")
            github_location(self)
        elif self.source == "workspace":
            if not self.path or self.url or self.revision or self.files is not None:
                raise ValueError("Workspace installs require only a sandbox folder path and optional name.")
            relative_path(self.path.removeprefix("/workspace/"))
        elif not self.name or not self.files or self.url or self.path or self.revision:
            raise ValueError("Inline installs require name and files including SKILL.md, without URL or path.")
        return self


def github_location(request):
    parsed = urlsplit(request.url)
    if parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.query or parsed.fragment:
        raise ValueError("Use an https://github.com public repository or skill-folder URL without credentials.")
    parts = unquote(parsed.path).strip("/").split("/")
    if len(parts) < 2 or not all(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}", p) for p in parts[:2]):
        raise ValueError("GitHub URL must identify an owner and repository.")
    owner, repo = parts[:2]
    repo = repo.removesuffix(".git")
    revision, folder = request.revision or "HEAD", request.path or ""
    if len(parts) > 2:
        if len(parts) < 5 or parts[2] not in {"tree", "blob"} or request.path:
            raise ValueError("Use a GitHub tree folder URL, or a repository URL with path and revision.")
        revision = request.revision or parts[3]
        remainder = "/".join(parts[3:])
        if not remainder.startswith(revision + "/"):
            raise ValueError("GitHub URL does not match the supplied revision.")
        folder = remainder[len(revision) + 1:]
        if parts[2] == "blob":
            if not folder.endswith("SKILL.md"):
                raise ValueError("A GitHub file URL must point to SKILL.md.")
            folder = str(PurePosixPath(folder).parent)
            folder = "" if folder == "." else folder
    if folder:
        relative_path(folder)
    return owner, repo, revision, folder


@dataclass
class SkillSource:
    name: str
    files: dict[str, str]
    provenance: dict


def package_name(value):
    name = re.sub(r"[^a-z0-9-]", "-", value.lower())[:40]
    return name if re.match(r"^[a-z]", name) else ("skill-" + name)[:40]


def extract_skill_archive(data, folder):
    """Pull one skill folder out of a GitHub commit tar.gz. Returns path -> bytes.

    The archive root is whatever GitHub named it; entries outside it, links and
    special files are rejected, and only files under `folder` are kept.
    """
    prefix = (folder + "/") if folder else ""
    files, total, root = {}, 0, None
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive:
            parts = tuple(p for p in PurePosixPath(member.name).parts if p != ".")
            root = root or parts[0]
            if len(parts) < 2 or parts[0] != root:
                continue
            relative = "/".join(parts[1:])
            # Only the requested folder is imported, so unrelated repository files
            # (including non-ASCII names) are ignored rather than rejected.
            if not relative.startswith(prefix):
                continue
            relative_path(relative)
            if member.type in {tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE}:
                continue
            if member.isdir():
                continue
            if not member.isfile():
                raise ValueError("Choose a skill directory without symlinks or special files.")
            if not relative.startswith(prefix):
                continue
            name = relative[len(prefix):]
            if name in files or not 0 <= member.size <= FILE_LIMIT:
                raise ValueError("Skill contains duplicate files or files exceeding 1 MB.")
            content = archive.extractfile(member).read()
            files[name] = content
            total += len(content)
            if len(files) >= FILE_COUNT or total > PACKAGE_LIMIT:
                raise ValueError("Skill exceeds 100 files or 5 MB.")
    return files


def github_token():
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("AGENTFORGE_GITHUB_TOKEN") or ""


async def github_source(request, client=None):
    owner, repo, revision, folder = github_location(request)
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False)
    base = f"https://api.github.com/repos/{owner}/{repo}"
    token = github_token()
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "AgentForge"}
    if token:
        headers["Authorization"] = "Bearer " + token
    transferred = 0

    async def get(path, params=None):
        """Read JSON metadata from api.github.com. Counts against the API rate limit,
        so only the commit lookup and the recursive tree listing use it."""
        nonlocal transferred
        async with client.stream("GET", base + path, params=params, follow_redirects=False,
                headers=headers) as response:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                transferred += len(chunk)
                if len(body) + len(chunk) > METADATA_LIMIT or transferred > DOWNLOAD_LIMIT:
                    raise ValueError("GitHub skill metadata exceeds the download limit; choose a smaller skill folder.")
                body.extend(chunk)
            return json.loads(body)

    async def get_raw(path):
        """Read a single file from raw.githubusercontent.com at the pinned commit.

        Raw downloads do not consume the api.github.com rate limit, which keeps
        multi-file skills (73 files and up) installable without a token.
        """
        nonlocal transferred
        url = f"https://raw.githubusercontent.com/{owner}/{repo}/{commit}/{quote(path, safe='/')}"
        async with client.stream("GET", url, follow_redirects=False,
                headers={"Accept": "text/plain", "User-Agent": "AgentForge",
                    **({"Authorization": "Bearer " + token} if token else {})}) as response:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                transferred += len(chunk)
                if len(body) + len(chunk) > FILE_LIMIT or transferred > DOWNLOAD_LIMIT:
                    raise ValueError("GitHub skill download exceeds the limit; choose a smaller skill folder.")
                body.extend(chunk)
            return bytes(body)

    async def get_archive():
        """Download the pinned commit as a tar.gz from codeload.github.com.

        One request carries the whole repository, so it is the fallback when raw
        downloads are unreachable: it still avoids one rate-limited API call per file.
        """
        nonlocal transferred
        url = f"https://codeload.github.com/{owner}/{repo}/tar.gz/{commit}"
        async with client.stream("GET", url, follow_redirects=False,
                headers={"User-Agent": "AgentForge"}) as response:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                transferred += len(chunk)
                if len(body) + len(chunk) > ARCHIVE_LIMIT or transferred > DOWNLOAD_LIMIT:
                    raise ValueError("GitHub skill archive exceeds the download limit; choose a smaller skill folder.")
                body.extend(chunk)
            return bytes(body)

    async def get_blob(entry):
        """Last-resort per-file read through api.github.com (counts against the rate limit)."""
        blob = await get("/git/blobs/" + entry["sha"])
        if blob.get("encoding") != "base64":
            raise ValueError("GitHub returned unsupported file encoding.")
        return base64.b64decode("".join(blob["content"].split()), validate=True)

    async def download(entries):
        """Raw per file, else the commit archive, else blobs. Only network failures fall through."""
        try:
            return {entry["path"]: await get_raw(entry["path"]) for entry in entries}
        except (httpx.HTTPStatusError, httpx.TransportError, TimeoutError):
            pass
        try:
            archive = await get_archive()
            stripped = len(folder) + 1 if folder else 0
            relative = {entry["path"][stripped:] for entry in entries}
            found = await asyncio.to_thread(extract_skill_archive, archive, folder)
            if relative <= set(found):
                return {entry["path"]: found[entry["path"][stripped:]] for entry in entries}
        except (tarfile.TarError, OSError, KeyError, EOFError, zlib.error):
            pass
        return {entry["path"]: await get_blob(entry) for entry in entries}

    try:
        async with asyncio.timeout(120):
            commit = (await get("/commits/" + quote(revision, safe="")))["sha"]
            if not re.fullmatch(r"[a-fA-F0-9]{40}", commit):
                raise ValueError("GitHub returned an invalid commit identifier.")
            tree = await get("/git/trees/" + quote(commit, safe=""), {"recursive": "1"})
            if not isinstance(tree.get("tree"), list):
                raise ValueError("GitHub returned an invalid skill tree.")
            if tree.get("truncated"):
                raise ValueError("Skill folder is too large to import; choose a smaller skill folder.")
            prefix = (folder + "/") if folder else ""
            entries, directories, seen = [], 0, set()
            for entry in tree["tree"]:
                path = entry.get("path") or ""
                if folder and not path.startswith(prefix):
                    continue
                relative_path(path)
                if path in seen:
                    raise ValueError("GitHub returned duplicate skill paths.")
                seen.add(path)
                if entry.get("type") == "tree":
                    directories += 1
                    if directories > 100:
                        raise ValueError("Skill folder contains too many directories.")
                    continue
                mode = entry.get("mode")
                if mode == "160000" or entry.get("type") == "commit":
                    raise ValueError("Skills cannot contain symlinks or submodules.")
                if mode == "120000":
                    raise ValueError("Skills cannot contain symlinks or submodules.")
                if entry.get("type") != "blob":
                    continue
                if not 0 <= entry.get("size", -1) <= FILE_LIMIT:
                    raise ValueError(f"Skill file exceeds 1 MB: {path}")
                entries.append(entry)
                if len(entries) > FILE_COUNT or sum(e["size"] for e in entries) > PACKAGE_LIMIT:
                    raise ValueError("Skill exceeds 100 files or 5 MB; choose a single skill folder.")
            document = next((e for e in entries if e["path"] == prefix + "SKILL.md"), None)
            if document is None or not 0 < document["size"] <= DOC_LIMIT:
                raise ValueError("Choose a skill folder with a nonempty SKILL.md of at most 24 KB at its root.")
            downloaded = await download(entries)
            files = {}
            for entry in entries:
                data = downloaded[entry["path"]]
                if len(data) != entry["size"] or len(data) > FILE_LIMIT:
                    raise ValueError("GitHub file size changed unexpectedly.")
                relative = entry["path"][len(prefix):]
                files[relative] = data.decode("utf-8")
            return SkillSource(request.name or package_name(PurePosixPath(folder).name or repo), files,
                {"source": "github", "url": request.url, "commit": commit, "path": folder})
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {401, 403, 429}:
            raise ValueError("GitHub access denied or rate limited. Only public repositories are supported; retry later.") from None
        raise ValueError("GitHub skill download failed. Check the repository, revision and skill folder URL.") from None
    except (httpx.TransportError, TimeoutError):
        raise ValueError("GitHub skill download timed out or lost its connection. Nothing was installed.") from None
    except (KeyError, TypeError):
        raise ValueError("GitHub returned an invalid skill response. Nothing was installed.") from None
    finally:
        if own_client:
            await client.aclose()


def read_workspace_archive(container, path):
    stream, info = container.get_archive("/workspace/" + path)
    archive = bytearray()
    try:
        for chunk in stream:
            if len(archive) + len(chunk) > 8_000_000:
                raise ValueError("Skill archive exceeds the 8 MB transport limit.")
            archive.extend(chunk)
    finally:
        if hasattr(stream, "close"):
            stream.close()
    files, total = {}, 0
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        for member in tar:
            relative_path(member.name.rstrip("/") if member.isdir() else member.name)
            parts = PurePosixPath(member.name).parts
            if parts[0] != info["name"]:
                raise ValueError("Unexpected sandbox archive root.")
            if member.isdir():
                continue
            if not member.isfile() or len(parts) < 2:
                raise ValueError("Choose a skill directory without symlinks or special files.")
            relative = "/".join(parts[1:])
            if relative in files or not 0 <= member.size <= FILE_LIMIT:
                raise ValueError("Skill contains duplicate files or files exceeding 1 MB.")
            total += member.size
            if len(files) >= FILE_COUNT or total > PACKAGE_LIMIT:
                raise ValueError("Skill exceeds 100 files or 5 MB.")
            files[relative] = tar.extractfile(member).read().decode("utf-8")
    return files


async def resolve_skill_source(request, sandbox=None, github_client=None):
    if request.source == "inline":
        return SkillSource(request.name, request.files, {"source": "inline"})
    if request.source == "github":
        return await github_source(request, github_client)
    if sandbox is None or not sandbox.alive:
        raise ValueError("Workspace skill installation requires a running Docker sandbox. Host folders cannot be imported by the agent.")
    path = relative_path(request.path.removeprefix("/workspace/"))
    try:
        files = await asyncio.to_thread(read_workspace_archive, sandbox.container, path)
    except DockerException:
        raise ValueError("Cannot read the sandbox skill folder. Check its path and sandbox health.") from None
    return SkillSource(request.name or package_name(PurePosixPath(path).name), files,
        {"source": "workspace", "path": path})
