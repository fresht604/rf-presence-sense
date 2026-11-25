"""RF Presence Sense: Passive Device-Free 3D Wi-Fi & BLE Localization for Home Assistant."""

from __future__ import annotations

import logging
from pathlib import Path

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
import homeassistant.helpers.config_validation as cv

from .const import (
    DOMAIN,
    FRONTEND_CARD_FILENAME,
    FRONTEND_URL_BASE,
    PLATFORMS,
    SERVICE_CALIBRATE_EMPTY,
    SERVICE_DEFINE_ROOM,
    SERVICE_DELETE_ROOM,
    SERVICE_RESET_BASELINE,
    SERVICE_SET_DEVICE_POSITION,
    VERSION,
)
from .coordinator import RFPresenceCoordinator
from .storage import RFPresenceStorage

_LOGGER = logging.getLogger(__name__)
_FRONTEND_FLAG = f"{DOMAIN}_frontend_registered"


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Serve the interactive 3D Lovelace Card JS module once per HA instance."""
    if hass.data.get(_FRONTEND_FLAG):
        return

    card_path = Path(__file__).parent / "frontend" / FRONTEND_CARD_FILENAME
    try:
        exists = await hass.async_add_executor_job(card_path.exists)
    except Exception:  # noqa: BLE001
        exists = card_path.exists()
    if not exists:
        return

    url_path = f"{FRONTEND_URL_BASE}/{FRONTEND_CARD_FILENAME}"
    http = getattr(hass, "http", None)
    if http is not None:
        try:
            from homeassistant.components.http import StaticPathConfig  # pylint: disable=import-outside-toplevel

            await http.async_register_static_paths(
                [StaticPathConfig(url_path, str(card_path), cache_headers=True)]
            )
        except Exception:  # noqa: BLE001
            try:
                http.register_static_path(url_path, str(card_path), cache_headers=True)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Could not register static frontend path: %s", err)
                return

    try:
        from homeassistant.components.frontend import add_extra_js_url  # pylint: disable=import-outside-toplevel

        add_extra_js_url(hass, f"{url_path}?v={VERSION}")
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Frontend add_extra_js_url skipped: %s", err)

    hass.data[_FRONTEND_FLAG] = True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up RF Presence Sense from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    await _async_register_frontend(hass)

    coordinator = RFPresenceCoordinator(hass, entry)
    try:
        await coordinator.async_load_storage()
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        coordinator.async_shutdown_collector()
        raise

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    _register_services(hass)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload integration when options are updated."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry and unregister services when the last entry is removed."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        domain_data: dict = hass.data.get(DOMAIN, {})
        coordinator: RFPresenceCoordinator | None = domain_data.pop(
            entry.entry_id, None
        )
        if coordinator is not None:
            await coordinator.async_shutdown()
        if not domain_data:
            _unregister_services(hass)
            hass.data.pop(DOMAIN, None)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clean up persisted storage file when the config entry is deleted."""
    await RFPresenceStorage(hass, entry.entry_id).async_remove()


def _unregister_services(hass: HomeAssistant) -> None:
    """Remove domain services when no config entries remain."""
    for service_name in (
        SERVICE_CALIBRATE_EMPTY,
        SERVICE_RESET_BASELINE,
        SERVICE_SET_DEVICE_POSITION,
        SERVICE_DEFINE_ROOM,
        SERVICE_DELETE_ROOM,
    ):
        if hass.services.has_service(DOMAIN, service_name):
            hass.services.async_remove(DOMAIN, service_name)


def _get_coordinators(hass: HomeAssistant) -> list[RFPresenceCoordinator]:
    return list(hass.data.get(DOMAIN, {}).values())


def _register_services(hass: HomeAssistant) -> None:
    """Register domain services once."""
    if hass.services.has_service(DOMAIN, SERVICE_CALIBRATE_EMPTY):
        return

    async def handle_calibrate(call: ServiceCall) -> None:
        duration = call.data.get("duration_seconds")
        for coord in _get_coordinators(hass):
            await coord.async_calibrate_empty_room(
                int(duration) if duration is not None else None
            )

    async def handle_reset_baseline(call: ServiceCall) -> None:
        for coord in _get_coordinators(hass):
            coord.reset_baseline(persist=False)
            await coord.async_save_storage()
            await coord.async_request_refresh()

    async def handle_set_position(call: ServiceCall) -> None:
        device_id = str(call.data["device_id"])
        x = float(call.data["x"])
        y = float(call.data["y"])
        z = float(call.data.get("z", 1.0))
        for coord in _get_coordinators(hass):
            await coord.async_set_device_position(device_id, x, y, z)
            await coord.async_request_refresh()

    async def handle_define_room(call: ServiceCall) -> None:
        room_id = str(call.data["room_id"])
        name = str(call.data["name"])
        x_min = float(call.data.get("x_min", 0.0))
        y_min = float(call.data.get("y_min", 0.0))
        x_max = float(call.data.get("x_max", 0.0))
        y_max = float(call.data.get("y_max", 0.0))
        polygon = call.data.get("polygon")
        for coord in _get_coordinators(hass):
            await coord.async_define_room(
                room_id, name, x_min, y_min, x_max, y_max, polygon=polygon
            )
            await coord.async_request_refresh()

    async def handle_delete_room(call: ServiceCall) -> None:
        room_id = str(call.data["room_id"])
        for coord in _get_coordinators(hass):
            await coord.async_delete_room(room_id)
            await coord.async_request_refresh()

    hass.services.async_register(
        DOMAIN,
        SERVICE_CALIBRATE_EMPTY,
        handle_calibrate,
        schema=vol.Schema(
            {
                vol.Optional("duration_seconds"): vol.All(
                    vol.Coerce(int), vol.Range(min=5, max=120)
                )
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_RESET_BASELINE,
        handle_reset_baseline,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_DEVICE_POSITION,
        handle_set_position,
        schema=vol.Schema(
            {
                vol.Required("device_id"): cv.string,
                vol.Required("x"): vol.Coerce(float),
                vol.Required("y"): vol.Coerce(float),
                vol.Optional("z", default=1.0): vol.Coerce(float),
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_DEFINE_ROOM,
        handle_define_room,
        schema=vol.Schema(
            {
                vol.Required("room_id"): cv.string,
                vol.Required("name"): cv.string,
                vol.Optional("x_min", default=0.0): vol.Coerce(float),
                vol.Optional("y_min", default=0.0): vol.Coerce(float),
                vol.Optional("x_max", default=0.0): vol.Coerce(float),
                vol.Optional("y_max", default=0.0): vol.Coerce(float),
                vol.Optional("polygon"): vol.All(
                    list,
                    [vol.All(list, vol.Length(min=2, max=2), [vol.Coerce(float)])],
                ),
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_DELETE_ROOM,
        handle_delete_room,
        schema=vol.Schema({vol.Required("room_id"): cv.string}),
    )
