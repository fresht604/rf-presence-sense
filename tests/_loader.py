"""Helper to load pure-Python submodules of rf_presence_sense without Home Assistant."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types
from typing import Any

COMP_DIR = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "rf_presence_sense"
)


def _install_ha_stubs() -> None:
    """Install minimal Home Assistant module stubs if homeassistant is not installed."""
    if "homeassistant" in sys.modules:
        return

    def _make_mod(name: str, is_pkg: bool = False) -> types.ModuleType:
        mod = types.ModuleType(name)
        if is_pkg:
            mod.__path__ = []  # type: ignore[attr-defined]
        sys.modules[name] = mod
        return mod

    _make_mod("homeassistant", is_pkg=True)
    core = _make_mod("homeassistant.core")

    class Event:  # noqa: D101
        def __init__(self, data: dict[str, Any] | None = None) -> None:
            self.data = data or {}

    class State:  # noqa: D101
        def __init__(
            self, entity_id: str, state: str, attributes: dict[str, Any] | None = None
        ) -> None:
            self.entity_id = entity_id
            self.state = state
            self.attributes = attributes or {}

    class HomeAssistant:  # noqa: D101
        pass

    class ServiceCall:  # noqa: D101
        def __init__(self, data: dict[str, Any] | None = None) -> None:
            self.data = data or {}

    def callback(func: Any) -> Any:
        return func

    core.Event = Event  # type: ignore[attr-defined]
    core.State = State  # type: ignore[attr-defined]
    core.HomeAssistant = HomeAssistant  # type: ignore[attr-defined]
    core.ServiceCall = ServiceCall  # type: ignore[attr-defined]
    core.callback = callback  # type: ignore[attr-defined]

    cfg_entries = _make_mod("homeassistant.config_entries")

    class ConfigEntry:  # noqa: D101
        def __init__(
            self,
            entry_id: str = "test_entry",
            data: dict[str, Any] | None = None,
            options: dict[str, Any] | None = None,
        ) -> None:
            self.entry_id = entry_id
            self.data = data or {}
            self.options = options or {}

    cfg_entries.ConfigEntry = ConfigEntry  # type: ignore[attr-defined]

    _make_mod("homeassistant.helpers", is_pkg=True)
    event_mod = _make_mod("homeassistant.helpers.event")
    event_mod.async_track_state_change_event = lambda hass, eids, cb: (lambda: None)  # type: ignore[attr-defined]

    dev_reg = _make_mod("homeassistant.helpers.device_registry")
    dev_reg.DeviceInfo = dict  # type: ignore[attr-defined]

    ent_plat = _make_mod("homeassistant.helpers.entity_platform")
    ent_plat.AddEntitiesCallback = Any  # type: ignore[attr-defined]

    upd_coord = _make_mod("homeassistant.helpers.update_coordinator")

    class DataUpdateCoordinator:  # noqa: D101
        def __class_getitem__(cls, _item: Any) -> type:
            return cls

        def __init__(
            self, hass: Any, logger: Any, name: str = "", update_interval: Any = None
        ) -> None:
            self.hass = hass
            self.logger = logger
            self.name = name
            self.update_interval = update_interval
            self.data: dict[str, Any] | None = None

        async def async_request_refresh(self) -> None:
            self.data = await self._async_update_data()

        async def async_config_entry_first_refresh(self) -> None:
            self.data = await self._async_update_data()

    class CoordinatorEntity:  # noqa: D101
        def __class_getitem__(cls, _item: Any) -> type:
            return cls

        def __init__(self, coordinator: Any) -> None:
            self.coordinator = coordinator

    upd_coord.DataUpdateCoordinator = DataUpdateCoordinator  # type: ignore[attr-defined]
    upd_coord.CoordinatorEntity = CoordinatorEntity  # type: ignore[attr-defined]

    _make_mod("homeassistant.components", is_pkg=True)
    cam_mod = _make_mod("homeassistant.components.camera")

    class Camera:  # noqa: D101
        def __init__(self) -> None:
            self.hass: Any = None
            self.content_type = "image/jpeg"

    cam_mod.Camera = Camera  # type: ignore[attr-defined]

    sensor_mod = _make_mod("homeassistant.components.sensor")

    class SensorEntity:  # noqa: D101
        pass

    class SensorStateClass:  # noqa: D101
        MEASUREMENT = "measurement"

    sensor_mod.SensorEntity = SensorEntity  # type: ignore[attr-defined]
    sensor_mod.SensorStateClass = SensorStateClass  # type: ignore[attr-defined]

    bs_mod = _make_mod("homeassistant.components.binary_sensor")

    class BinarySensorEntity:  # noqa: D101
        pass

    class BinarySensorDeviceClass:  # noqa: D101
        OCCUPANCY = "occupancy"

    bs_mod.BinarySensorEntity = BinarySensorEntity  # type: ignore[attr-defined]
    bs_mod.BinarySensorDeviceClass = BinarySensorDeviceClass  # type: ignore[attr-defined]


def _ensure_package() -> str:
    pkg_name = "rf_presence_sense"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(COMP_DIR)]  # type: ignore[attr-defined]
        sys.modules[pkg_name] = pkg
    return pkg_name


def load_submodule(mod_short: str, with_ha_stubs: bool = False) -> types.ModuleType:
    """Load a specific submodule from custom_components/rf_presence_sense."""
    if with_ha_stubs:
        _install_ha_stubs()
    pkg_name = _ensure_package()
    full_name = f"{pkg_name}.{mod_short}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = importlib.util.spec_from_file_location(
        full_name, COMP_DIR / f"{mod_short}.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_core_modules() -> tuple[types.ModuleType, types.ModuleType, types.ModuleType]:
    """Load const, rti_engine, and tracker into a synthetic package namespace."""
    return (
        load_submodule("const"),
        load_submodule("rti_engine"),
        load_submodule("tracker"),
    )
