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
    PANEL_ICON,
    PANEL_TITLE,
    PANEL_URL_PATH,
    PANEL_WEBCOMPONENT,
    PLATFORMS,
    SERVICE_AUTO_PLACE_FROM_AREAS,
    SERVICE_CALIBRATE_EMPTY,
    SERVICE_CLEAR_FINGERPRINTS,
    SERVICE_DEFINE_ROOM,
    SERVICE_DELETE_ROOM,
    SERVICE_RECORD_CALIBRATION_POINT,
    SERVICE_RESET_BASELINE,
    SERVICE_SET_DEVICE_POSITION,
    SERVICE_START_EDGE_WALK,
    SERVICE_STOP_EDGE_WALK,
    VERSION,
)
from .coordinator import RFPresenceCoordinator
from .storage import RFPresenceStorage

_LOGGER = logging.getLogger(__name__)
_FRONTEND_FLAG = f"{DOMAIN}_frontend_registered"
_PANEL_FLAG = f"{DOMAIN}_panel_registered"


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Serve the interactive 3D Lovelace Card JS module and register sidebar panel."""
    card_path = Path(__file__).parent / "frontend" / FRONTEND_CARD_FILENAME
    url_path = f"{FRONTEND_URL_BASE}/{FRONTEND_CARD_FILENAME}"
    module_url = f"{url_path}?v={VERSION}"

    if not hass.data.get(_FRONTEND_FLAG):
        try:
            exists = await hass.async_add_executor_job(card_path.exists)
        except Exception:  # noqa: BLE001
            exists = card_path.exists()
        if not exists:
            return

        http = getattr(hass, "http", None)
        if http is not None:
            try:
                from homeassistant.components.http import StaticPathConfig  # pylint: disable=import-outside-toplevel

                await http.async_register_static_paths(
                    [StaticPathConfig(url_path, str(card_path), cache_headers=True)]
                )
            except Exception:  # noqa: BLE001
                try:
                    http.register_static_path(
                        url_path, str(card_path), cache_headers=True
                    )
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("Could not register static frontend path: %s", err)
                    return

        try:
            from homeassistant.components.frontend import add_extra_js_url  # pylint: disable=import-outside-toplevel

            add_extra_js_url(hass, module_url)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Frontend add_extra_js_url skipped: %s", err)

        hass.data[_FRONTEND_FLAG] = True

    if not hass.data.get(_PANEL_FLAG):
        try:
            existing_panels = hass.data.get("frontend_panels")
            if isinstance(existing_panels, dict) and PANEL_URL_PATH in existing_panels:
                hass.data[_PANEL_FLAG] = True
            else:
                from homeassistant.components import panel_custom  # pylint: disable=import-outside-toplevel

                await panel_custom.async_register_panel(
                    hass,
                    webcomponent_name=PANEL_WEBCOMPONENT,
                    frontend_url_path=PANEL_URL_PATH,
                    sidebar_title=PANEL_TITLE,
                    sidebar_icon=PANEL_ICON,
                    module_url=module_url,
                    embed_iframe=False,
                    require_admin=False,
                )
                hass.data[_PANEL_FLAG] = True
        except ValueError:
            # Panel was already registered on this HA instance (e.g. partial reload)
            hass.data[_PANEL_FLAG] = True
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Sidebar panel registration skipped: %s", err)


def _unregister_panel(hass: HomeAssistant) -> None:
    """Remove the 3D RF-Map sidebar panel when the last config entry unloads."""
    if not hass.data.pop(_PANEL_FLAG, False):
        return
    try:
        from homeassistant.components import frontend  # pylint: disable=import-outside-toplevel

        frontend.async_remove_panel(hass, PANEL_URL_PATH)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Sidebar panel removal skipped: %s", err)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up RF Presence Sense from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    await _async_register_frontend(hass)

    coordinator = RFPresenceCoordinator(hass, entry)
    try:
        await coordinator.async_load_storage()
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await coordinator.async_shutdown()
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
    """Unload a config entry and clean up services & sidebar panel when last entry unloads."""
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
            _unregister_panel(hass)
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
        SERVICE_RECORD_CALIBRATION_POINT,
        SERVICE_START_EDGE_WALK,
        SERVICE_STOP_EDGE_WALK,
        SERVICE_CLEAR_FINGERPRINTS,
        SERVICE_AUTO_PLACE_FROM_AREAS,
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
        open_edges = call.data.get("open_edges")
        doors = call.data.get("doors")
        windows = call.data.get("windows")
        for coord in _get_coordinators(hass):
            await coord.async_define_room(
                room_id,
                name,
                x_min,
                y_min,
                x_max,
                y_max,
                polygon=polygon,
                open_edges=open_edges,
                doors=doors,
                windows=windows,
            )
            await coord.async_request_refresh()

    async def handle_delete_room(call: ServiceCall) -> None:
        room_id = str(call.data["room_id"])
        for coord in _get_coordinators(hass):
            await coord.async_delete_room(room_id)
            await coord.async_request_refresh()

    async def handle_record_calibration_point(call: ServiceCall) -> None:
        x = float(call.data["x"])
        y = float(call.data["y"])
        room_id = str(call.data.get("room_id", ""))
        duration = int(call.data.get("duration_seconds", 5))
        label = str(call.data.get("label", ""))
        phone_entity = str(call.data.get("phone_entity", ""))
        for coord in _get_coordinators(hass):
            await coord.async_record_calibration_point(
                x=x,
                y=y,
                room_id=room_id,
                duration_seconds=duration,
                label=label,
                phone_entity=phone_entity,
            )

    async def handle_start_edge_walk(call: ServiceCall) -> None:
        room_id = str(call.data.get("room_id", ""))
        start_x = float(call.data["start_x"])
        start_y = float(call.data["start_y"])
        end_x = float(call.data["end_x"])
        end_y = float(call.data["end_y"])
        phone_entity = str(call.data.get("phone_entity", ""))
        for coord in _get_coordinators(hass):
            await coord.async_start_edge_walk(
                room_id=room_id,
                start_x=start_x,
                start_y=start_y,
                end_x=end_x,
                end_y=end_y,
                phone_entity=phone_entity,
            )

    async def handle_stop_edge_walk(call: ServiceCall) -> None:
        for coord in _get_coordinators(hass):
            await coord.async_stop_edge_walk()

    async def handle_clear_fingerprints(call: ServiceCall) -> None:
        for coord in _get_coordinators(hass):
            await coord.async_clear_fingerprints()

    async def handle_auto_place_from_areas(call: ServiceCall) -> None:
        for coord in _get_coordinators(hass):
            await coord.async_auto_place_from_areas()

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
                vol.Optional("open_edges"): vol.All(list, [cv.string]),
                vol.Optional("doors"): list,
                vol.Optional("windows"): list,
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_DELETE_ROOM,
        handle_delete_room,
        schema=vol.Schema({vol.Required("room_id"): cv.string}),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_RECORD_CALIBRATION_POINT,
        handle_record_calibration_point,
        schema=vol.Schema(
            {
                vol.Required("x"): vol.Coerce(float),
                vol.Required("y"): vol.Coerce(float),
                vol.Optional("room_id", default=""): cv.string,
                vol.Optional("duration_seconds", default=5): vol.All(
                    vol.Coerce(int), vol.Range(min=2, max=30)
                ),
                vol.Optional("label", default=""): cv.string,
                vol.Optional("phone_entity", default=""): cv.string,
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_START_EDGE_WALK,
        handle_start_edge_walk,
        schema=vol.Schema(
            {
                vol.Optional("room_id", default=""): cv.string,
                vol.Required("start_x"): vol.Coerce(float),
                vol.Required("start_y"): vol.Coerce(float),
                vol.Required("end_x"): vol.Coerce(float),
                vol.Required("end_y"): vol.Coerce(float),
                vol.Optional("phone_entity", default=""): cv.string,
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_STOP_EDGE_WALK,
        handle_stop_edge_walk,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_FINGERPRINTS,
        handle_clear_fingerprints,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_AUTO_PLACE_FROM_AREAS,
        handle_auto_place_from_areas,
    )
