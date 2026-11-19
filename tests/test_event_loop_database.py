"""No asynchronous web handler touches the database on the event loop (#354).

A database write can wait up to ten seconds for the write lock. In an ``async def`` handler that wait stops the whole web process: every page,
every live stream and the worker's own heartbeat. So database work in such a handler runs in a thread (``await asyncio.to_thread(...)``), or the
handler is a plain ``def``, which the server already runs in a thread.

This reads the source of ``app/api`` and fails when an asynchronous function there calls ``connect()``, or a function known to open the database,
directly. A function handed to a thread (``asyncio.to_thread(save)``) is not a call and is not flagged; neither is a nested ``def`` or ``lambda``,
which is how work is handed over.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"

# Calls that are known and accepted, each with its reason. Add to this only with one.
ALLOWED: dict[tuple[str, str, str], str] = {}


def _called(call: ast.Call) -> tuple[str | None, str | None]:
    """(module alias or None, name) of what a call calls, when it is a plain name or ``module.name``."""
    target = call.func
    if isinstance(target, ast.Name):
        return None, target.id
    if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
        return target.value.id, target.attr
    return None, None


def _own_nodes(function: ast.AST):
    """Everything a function itself runs: not the bodies of functions and lambdas defined inside it."""
    stack = [node for node in ast.iter_child_nodes(function)]
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


class Module:
    def __init__(self, path: Path):
        self.path = path
        self.name = ".".join(path.relative_to(APP.parent).with_suffix("").parts)
        self.tree = ast.parse(path.read_text(encoding="utf-8"))
        self.functions = {node.name: node for node in self.tree.body if isinstance(node, ast.FunctionDef)}
        self.modules: dict[str, str] = {}  # alias -> app module
        self.names: dict[str, tuple[str, str]] = {}  # local name -> (app module, name there)
        package = self.name.rsplit(".", 1)[0]
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for item in node.names:
                    if item.name.startswith("app."):
                        self.modules[item.asname or item.name.split(".")[-1]] = item.name
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parts = (self.name if path.name == "__init__.py" else package).split(".")
                    parts = parts[:len(parts) - (node.level - 1)]
                    base = ".".join(parts + ([base] if base else []))
                if not base.startswith("app"):
                    continue
                for item in node.names:
                    self.modules[item.asname or item.name] = f"{base}.{item.name}"
                    self.names[item.asname or item.name] = (base, item.name)


def _load() -> dict[str, Module]:
    modules = {}
    for path in APP.rglob("*.py"):
        module = Module(path)
        modules[module.name.removesuffix(".__init__")] = module
    return modules


def _opens_database(modules: dict[str, Module]) -> set[tuple[str, str]]:
    """Every plain function in the application that opens the database itself, or calls one that does."""
    found: set[tuple[str, str]] = {("app.store", "connect"), ("app.store", "read_one")}
    changed = True
    while changed:
        changed = False
        for name, module in modules.items():
            for function in module.functions.values():
                if (name, function.name) in found:
                    continue
                for node in ast.walk(function):
                    if isinstance(node, ast.Call) and _resolve(modules, name, module, node) in found:
                        found.add((name, function.name))
                        changed = True
                        break
    return found


def _resolve(modules: dict[str, Module], name: str, module: Module, call: ast.Call) -> tuple[str, str] | None:
    alias, called = _called(call)
    if called is None:
        return None
    if alias is None:
        if called in module.functions:
            return name, called
        return module.names.get(called)
    target = module.modules.get(alias)
    return (target, called) if target in modules else None


def offenders() -> list[tuple[str, str, str]]:
    modules = _load()
    opens = _opens_database(modules)
    found = []
    for name, module in sorted(modules.items()):
        if not name.startswith("app.api"):
            continue
        for function in ast.walk(module.tree):
            if not isinstance(function, ast.AsyncFunctionDef):
                continue
            for node in _own_nodes(function):
                if isinstance(node, ast.Call):
                    target = _resolve(modules, name, module, node)
                    if target in opens:
                        found.append((name, function.name, f"{target[0]}.{target[1]}"))
    return sorted(set(found))


class EventLoopDatabaseTests(unittest.TestCase):
    def test_no_asynchronous_handler_opens_the_database_on_the_event_loop(self):
        new = [item for item in offenders() if item not in ALLOWED]
        self.assertEqual(new, [], "\n\nThese asynchronous functions call the database on the event loop. Run the database work in a thread "
                                  "(await asyncio.to_thread(...)) or make the handler a plain def:\n"
                         + "\n".join(f"  {module}.{function} calls {called}" for module, function, called in new))

    def test_the_allowed_list_holds_nothing_that_is_no_longer_there(self):
        self.assertEqual(sorted(set(ALLOWED) - set(offenders())), [])

    def test_the_check_sees_a_direct_call_and_ignores_handed_over_work(self):
        source = ("from app.store import connect\nimport asyncio\n"
                  "def helper():\n    with connect() as db:\n        return db\n"
                  "async def bad():\n    helper()\n"
                  "async def worse():\n    with connect() as db:\n        pass\n"
                  "async def good():\n    def work():\n        return helper()\n    await asyncio.to_thread(work)\n    await asyncio.to_thread(helper)\n    await asyncio.to_thread(lambda: helper())\n")
        module = Module.__new__(Module)
        module.path, module.name, module.tree = Path("app/api/example.py"), "app.api.example", ast.parse(source)
        module.functions = {node.name: node for node in module.tree.body if isinstance(node, ast.FunctionDef)}
        module.modules, module.names = {}, {"connect": ("app.store", "connect")}
        modules = {"app.api.example": module, "app.store": Module(APP / "store.py")}
        opens = _opens_database(modules)
        flagged = sorted({function.name for function in ast.walk(module.tree) if isinstance(function, ast.AsyncFunctionDef)
                          for node in _own_nodes(function) if isinstance(node, ast.Call) and _resolve(modules, "app.api.example", module, node) in opens})
        self.assertEqual(flagged, ["bad", "worse"])


if __name__ == "__main__":
    unittest.main()
