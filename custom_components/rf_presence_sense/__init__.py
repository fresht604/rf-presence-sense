"""RF Presence Sense: Passive Device-Free Wi-Fi & BLE Localization for Home Assistant."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
import homeassistant.helpers.config_validation as cv

from .const import (
    DOMAIN,
    PLATFORMS,
    SERVICE_CALIBRATE_EMPTY,
    SERVICE_DEFINE_ROOM,
    SERVICE_RESET_BASELINE,
    SERVICE_SET_DEVICE_POSITION,
)
from .coordinator import RFPresenceCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up RF Presence Sense from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    coordinator = RFPresenceCoordinator(hass, entry)
    try:
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
            coordinator.async_shutdown_collector()
        if not domain_data:
            _unregister_services(hass)
            hass.data.pop(DOMAIN, None)
    return unload_ok


def _unregister_services(hass: HomeAssistant) -> None:
    """Remove domain services when no config entries remain."""
    for service_name in (
        SERVICE_CALIBRATE_EMPTY,
        SERVICE_RESET_BASELINE,
        SERVICE_SET_DEVICE_POSITION,
        SERVICE_DEFINE_ROOM,
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
        duration = int(call.data.get("duration_seconds", 20))
        for coord in _get_coordinators(hass):
            await coord.async_calibrate_empty_room(duration)

    async def handle_reset_baseline(call: ServiceCall) -> None:
        for coord in _get_coordinators(hass):
            coord.reset_baseline()
            await coord.async_request_refresh()

    async def handle_set_position(call: ServiceCall) -> None:
        device_id = str(call.data["device_id"])
        x = float(call.data["x"])
        y = float(call.data["y"])
        z = float(call.data.get("z", 1.0))
        for coord in _get_coordinators(hass):
            coord.set_device_position(device_id, x, y, z)
            await coord.async_request_refresh()

    async def handle_define_room(call: ServiceCall) -> None:
        room_id = str(call.data["room_id"])
        name = str(call.data["name"])
        x_min = float(call.data["x_min"])
        y_min = float(call.data["y_min"])
        x_max = float(call.data["x_max"])
        y_max = float(call.data["y_max"])
        for coord in _get_coordinators(hass):
            coord.define_room(room_id, name, x_min, y_min, x_max, y_max)
            await coord.async_request_refresh()

    hass.services.async_register(
        DOMAIN,
        SERVICE_CALIBRATE_EMPTY,
        handle_calibrate,
        schema=vol.Schema(
            {
                vol.Optional("duration_seconds", default=20): vol.All(
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
                vol.Required("x_min"): vol.Coerce(float),
                vol.Required("y_min"): vol.Coerce(float),
                vol.Required("x_max"): vol.Coerce(float),
                vol.Required("y_max"): vol.Coerce(float),
            }
        ),
    )
