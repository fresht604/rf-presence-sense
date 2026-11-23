"""Helper to load pure-Python submodules of rf_presence_sense without Home Assistant."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types

COMP_DIR = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "rf_presence_sense"
)


def load_core_modules() -> tuple[types.ModuleType, types.ModuleType, types.ModuleType]:
    """Load const, rti_engine, and tracker into a synthetic package namespace."""
    pkg_name = "rf_presence_sense"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(COMP_DIR)]  # type: ignore[attr-defined]
        sys.modules[pkg_name] = pkg

    loaded = []
    for mod_short in ("const", "rti_engine", "tracker"):
        full_name = f"{pkg_name}.{mod_short}"
        if full_name in sys.modules:
            loaded.append(sys.modules[full_name])
            continue
        spec = importlib.util.spec_from_file_location(
            full_name, COMP_DIR / f"{mod_short}.py"
        )
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[full_name] = mod
        spec.loader.exec_module(mod)
        loaded.append(mod)

    return loaded[0], loaded[1], loaded[2]
