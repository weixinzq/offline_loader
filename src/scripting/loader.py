"""Discovery and validation for selectable interaction scripts."""
from __future__ import annotations

import importlib
import inspect
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Awaitable, Callable

from src.config import RESOURCE_ROOT
from src.network.context import AppContext

ScriptRunner = Callable[[AppContext], Awaitable[None]]


@dataclass(frozen=True)
class InteractionScript:
    module_name: str
    name: str
    description: str
    run: ScriptRunner


def _load_script(module_name: str) -> InteractionScript:
    module: ModuleType = importlib.import_module(module_name)
    runner = getattr(module, "run", None)
    if runner is None or not inspect.iscoroutinefunction(runner):
        raise TypeError(f"{module_name} 必须提供 async run(context)")
    return InteractionScript(
        module_name=module_name,
        name=str(getattr(module, "SCRIPT_NAME", module_name.rsplit(".", 1)[-1])),
        description=str(getattr(module, "SCRIPT_DESCRIPTION", "")),
        run=runner,
    )


def discover_scripts(directory: Path | None = None) -> list[InteractionScript]:
    scripts_dir = directory or RESOURCE_ROOT / "scripts"
    discovered = []
    for path in sorted(scripts_dir.glob("*.py")):
        if (
            path.name == "__init__.py"
            or path.name.startswith("_")
            or path.stem == "verify"
        ):
            continue
        module_name = f"scripts.{path.stem}"
        try:
            discovered.append(_load_script(module_name))
        except TypeError:
            # Standalone tools such as verify.py are intentionally not shown.
            continue
    return discovered
