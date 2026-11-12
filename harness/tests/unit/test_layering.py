"""Import boundaries, checked rather than remembered.

The harness is one package, so nothing in Python stops a lower module from
importing a higher one -- it just works, and the cycle becomes a layout nobody
can reason about. These rules are the ones this codebase actually holds; they
are asserted here so the next refactor cannot quietly break them, and so a
reader can tell "this module is allowed to know about that one" from a comment.

Rules:

1. `agentforge.api` is a leaf. Only the API package itself (and `server`, which
   is the entry point) may import it; nothing under `runtime`, `tools`, `sandbox`
   or `store` may.
2. The leaf tier (`config`, `models`, `logging_config`, `redact`, `netguard`)
   imports nothing but itself. These are the names every layer shares; if one of
   them starts importing `tools`, the shared tier stops being shared.
3. `tools` never imports `sandbox`, `loop` or `middleware`. A tool runs inside a
   workspace or a container, but it does not know which: the sandbox wraps tools,
   not the other way round.
4. Router modules do not import each other. They share `AppDeps`, `schemas` and
   `events`; a shortcut between two routers is how the god-module grows back.
"""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "src" / "agentforge"
LEAF = {"config", "models", "logging_config", "redact", "netguard"}
BELOW_TOOLS = {"sandbox", "loop", "middleware", "api"}


def modules():
    for path in sorted(ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(ROOT).with_suffix("")
        yield ".".join(relative.parts), path


def imports_of(path: Path) -> set[str]:
    """Top-level `agentforge.<name>` targets this file imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("agentforge."):
                found.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("agentforge."):
                    found.add(alias.name.split(".")[1])
    return found


def offenders(predicate, forbidden) -> list[str]:
    broken = []
    for name, path in modules():
        if not predicate(name):
            continue
        for target in sorted(imports_of(path) & forbidden):
            broken.append(f"{name} imports agentforge.{target}")
    return broken


def test_the_api_package_is_a_leaf():
    broken = [line for line in offenders(
        lambda name: not name.startswith("api") and name != "server", {"api"})]
    assert not broken, (
        "only agentforge.api (and the entry point) may import the API package:\n  "
        + "\n  ".join(broken))


def test_the_shared_leaf_imports_nothing_higher():
    broken = offenders(lambda name: name.split(".")[0] in LEAF,
                       {"tools", "sandbox", "loop", "middleware", "api", "skills",
                        "capabilities", "artifacts", "auth", "store"})
    assert not broken, (
        "the shared leaf tier must not reach up:\n  " + "\n  ".join(broken))


def test_tools_do_not_know_the_sandbox():
    broken = offenders(lambda name: name.startswith("tools"), BELOW_TOOLS)
    assert not broken, (
        "a tool runs in a workspace or a container but must not depend on which:\n  "
        + "\n  ".join(broken))


def test_routers_do_not_import_each_other():
    routers = {name for name, _ in modules() if name.startswith("api.routes.")}
    broken = []
    for name, path in modules():
        if not name.startswith("api.routes."):
            continue
        sibling = {target for target in imports_of(path) if target.startswith("api.routes")}
        if sibling:
            broken.append(f"{name} imports {sorted(sibling)}")
    assert not broken, (
        "routers share deps/schemas/events, not each other:\n  " + "\n  ".join(broken))
    assert len(routers) >= 8, "expected the API split to still be a package of routers"


@pytest.mark.parametrize("module", ["api", "api.deps", "api.app", "api.events",
                                    "api.schemas", "api.lifespan", "api.middleware"])
def test_the_split_modules_exist(module):
    path = ROOT.joinpath(*module.split("."))
    assert path.with_suffix(".py").is_file() or (path / "__init__.py").is_file()
