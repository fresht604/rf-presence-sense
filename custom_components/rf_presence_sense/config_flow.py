"""Config Flow & Menu-Based Options Flow for RF Presence Sense."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_ACTIVE_POLL_ENTITIES,
    CONF_AWAY_ENTITY,
    CONF_DEVICES,
    CONF_ENABLE_BLE,
    CONF_ENABLE_WIFI_ENTITIES,
    CONF_FLOOR_HEIGHT,
    CONF_FLOOR_WIDTH,
    CONF_GRID_RESOLUTION,
    CONF_MASK_OUTSIDE_ROOMS,
    CONF_PRESENCE_AWARE_BASELINE,
    CONF_RECEIVERS,
    CONF_SENSITIVITY,
    CONF_UPDATE_INTERVAL,
    DEFAULT_FLOOR_HEIGHT,
    DEFAULT_FLOOR_WIDTH,
    DEFAULT_GRID_RESOLUTION,
    DEFAULT_MASK_OUTSIDE_ROOMS,
    DEFAULT_PRESENCE_AWARE_BASELINE,
    DEFAULT_SENSITIVITY,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    MAX_LINKS,
    MIN_UPDATE_INTERVAL,
    PROTOCOL_BLE,
    PROTOCOL_WIFI,
)
from .signal_collector import (
    discover_ble_devices,
    discover_rssi_entities,
    is_likely_stationary,
)


class RFPresenceConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for RF Presence Sense."""

    VERSION = 1

    def __init__(self) -> None:
        self._base_config: dict[str, Any] = {}
        self._discovered_wifi: dict[str, dict[str, Any]] = {}
        self._discovered_ble: dict[str, dict[str, Any]] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1: Floorplan dimensions, router/HA position, and protocol toggles."""
        if user_input is not None:
            self._base_config = {
                CONF_FLOOR_WIDTH: float(user_input[CONF_FLOOR_WIDTH]),
                CONF_FLOOR_HEIGHT: float(user_input[CONF_FLOOR_HEIGHT]),
                CONF_GRID_RESOLUTION: float(user_input[CONF_GRID_RESOLUTION]),
                CONF_UPDATE_INTERVAL: max(
                    MIN_UPDATE_INTERVAL, float(user_input[CONF_UPDATE_INTERVAL])
                ),
                CONF_SENSITIVITY: float(user_input[CONF_SENSITIVITY]),
                CONF_ENABLE_WIFI_ENTITIES: bool(
                    user_input[CONF_ENABLE_WIFI_ENTITIES]
                ),
                CONF_ENABLE_BLE: bool(user_input[CONF_ENABLE_BLE]),
                CONF_PRESENCE_AWARE_BASELINE: DEFAULT_PRESENCE_AWARE_BASELINE,
                CONF_RECEIVERS: {
                    "router_main": {
                        "name": "WLAN Router",
                        "x": float(user_input["router_x"]),
                        "y": float(user_input["router_y"]),
                        "z": float(user_input["router_z"]),
                    },
                    "ha_bluetooth": {
                        "name": "Home Assistant Bluetooth",
                        "x": float(user_input["ha_x"]),
                        "y": float(user_input["ha_y"]),
                        "z": float(user_input["ha_z"]),
                    },
                },
            }
            return await self.async_step_devices()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_FLOOR_WIDTH, default=DEFAULT_FLOOR_WIDTH
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=2.0, max=50.0, step=0.5, unit_of_measurement="m"
                    )
                ),
                vol.Required(
                    CONF_FLOOR_HEIGHT, default=DEFAULT_FLOOR_HEIGHT
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=2.0, max=50.0, step=0.5, unit_of_measurement="m"
                    )
                ),
                vol.Required(
                    CONF_GRID_RESOLUTION, default=DEFAULT_GRID_RESOLUTION
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0.25, max=1.5, step=0.05, unit_of_measurement="m"
                    )
                ),
                vol.Required("router_x", default=5.0): vol.Coerce(float),
                vol.Required("router_y", default=4.0): vol.Coerce(float),
                vol.Required("router_z", default=1.2): vol.Coerce(float),
                vol.Required("ha_x", default=5.0): vol.Coerce(float),
                vol.Required("ha_y", default=4.0): vol.Coerce(float),
                vol.Required("ha_z", default=1.0): vol.Coerce(float),
                vol.Required(
                    CONF_UPDATE_INTERVAL, default=DEFAULT_UPDATE_INTERVAL
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_UPDATE_INTERVAL,
                        max=30.0,
                        step=0.5,
                        unit_of_measurement="s",
                    )
                ),
                vol.Required(
                    CONF_SENSITIVITY, default=DEFAULT_SENSITIVITY
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0.3, max=2.5, step=0.1)
                ),
                vol.Required(CONF_ENABLE_WIFI_ENTITIES, default=True): bool,
                vol.Required(CONF_ENABLE_BLE, default=True): bool,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2: Select discovered existing Wi-Fi RSSI entities and stationary BLE devices."""
        if not self._discovered_wifi:
            self._discovered_wifi = discover_rssi_entities(self.hass)
        if not self._discovered_ble:
            self._discovered_ble = discover_ble_devices(self.hass)

        if user_input is not None:
            devices: dict[str, dict[str, Any]] = {}
            for eid in user_input.get("wifi_entities", []):
                if len(devices) >= MAX_LINKS:
                    break
                meta = self._discovered_wifi.get(eid, {})
                devices[eid] = {
                    "name": meta.get("name", eid),
                    "protocol": meta.get("protocol", PROTOCOL_WIFI),
                    "receiver_id": meta.get("receiver_id", "router_main"),
                }
            for addr in user_input.get("ble_devices", []):
                if len(devices) >= MAX_LINKS:
                    break
                meta = self._discovered_ble.get(addr, {})
                devices[addr.upper()] = {
                    "name": meta.get("name", addr),
                    "protocol": PROTOCOL_BLE,
                    "receiver_id": meta.get("receiver_id", "ha_bluetooth"),
                }

            data = {**self._base_config, CONF_DEVICES: devices}
            return self.async_create_entry(
                title="RF Presence Sense", data=data
            )

        wifi_options = [
            selector.SelectOptionDict(
                value=eid,
                label=f"{meta['name']} ({eid}, {meta['rssi']:.0f} dBm)",
            )
            for eid, meta in self._discovered_wifi.items()
        ]
        # Only list named BLE devices (skip hundreds of unnamed random-MAC phones)
        named_ble = {
            addr: meta
            for addr, meta in self._discovered_ble.items()
            if meta["name"].strip().lower().replace("-", ":") != addr.lower()
        }
        ble_options = [
            selector.SelectOptionDict(
                value=addr,
                label=f"{meta['name']} ({addr}, {meta['rssi']:.0f} dBm)",
            )
            for addr, meta in sorted(
                named_ble.items(), key=lambda kv: kv[1]["name"].lower()
            )
        ]

        wifi_enabled = bool(self._base_config.get(CONF_ENABLE_WIFI_ENTITIES, True))
        ble_enabled = bool(self._base_config.get(CONF_ENABLE_BLE, True))

        default_wifi = (
            [
                eid
                for eid, meta in self._discovered_wifi.items()
                if is_likely_stationary(meta["name"], eid)
            ][:MAX_LINKS]
            if wifi_enabled
            else []
        )
        remaining_budget = max(0, MAX_LINKS - len(default_wifi))
        default_ble = (
            [
                addr
                for addr, meta in sorted(
                    named_ble.items(),
                    key=lambda kv: kv[1].get("rssi", -100.0),
                    reverse=True,
                )
                if is_likely_stationary(meta["name"], addr)
            ][:remaining_budget]
            if ble_enabled and remaining_budget > 0
            else []
        )

        fields: dict[Any, Any] = {}
        if wifi_options:
            fields[
                vol.Optional("wifi_entities", default=default_wifi)
            ] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=wifi_options,
                    multiple=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
        if ble_options:
            fields[
                vol.Optional("ble_devices", default=default_ble)
            ] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=ble_options,
                    multiple=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )

        return self.async_show_form(
            step_id="devices",
            data_schema=vol.Schema(fields),
            description_placeholders={
                "wifi_count": str(len(wifi_options)),
                "ble_count": str(len(ble_options)),
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> RFPresenceOptionsFlow:
        """Return menu-based options flow."""
        return RFPresenceOptionsFlow(config_entry)


class RFPresenceOptionsFlow(config_entries.OptionsFlow):
    """Menu-based Options Flow for tuning, devices, 3D positions, and rooms."""

    def __init__(self, config_entry: config_entries.ConfigEntry | None = None) -> None:
        self._entry = config_entry
        self._selected_position_id: str | None = None
        self._discovered_wifi: dict[str, dict[str, Any]] = {}
        self._discovered_ble: dict[str, dict[str, Any]] = {}

    def _get_entry(self) -> config_entries.ConfigEntry:
        return self._entry if self._entry is not None else self.config_entry

    def _get_coordinator(self) -> Any | None:
        entry = self._get_entry()
        return self.hass.data.get(DOMAIN, {}).get(entry.entry_id)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show main options menu."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["tuning", "devices", "positions", "rooms"],
        )

    async def async_step_tuning(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage sensitivity, interval, floorplan geometry, active polling & baseline mode."""
        entry = self._get_entry()
        cfg = {**entry.data, **entry.options}

        if user_input is not None:
            new_options = dict(entry.options)
            new_options[CONF_SENSITIVITY] = float(user_input[CONF_SENSITIVITY])
            new_options[CONF_UPDATE_INTERVAL] = max(
                MIN_UPDATE_INTERVAL, float(user_input[CONF_UPDATE_INTERVAL])
            )
            new_options[CONF_FLOOR_WIDTH] = float(user_input[CONF_FLOOR_WIDTH])
            new_options[CONF_FLOOR_HEIGHT] = float(user_input[CONF_FLOOR_HEIGHT])
            new_options[CONF_GRID_RESOLUTION] = float(user_input[CONF_GRID_RESOLUTION])
            new_options[CONF_ENABLE_WIFI_ENTITIES] = bool(
                user_input[CONF_ENABLE_WIFI_ENTITIES]
            )
            new_options[CONF_ENABLE_BLE] = bool(user_input[CONF_ENABLE_BLE])
            new_options[CONF_ACTIVE_POLL_ENTITIES] = list(
                user_input.get(CONF_ACTIVE_POLL_ENTITIES) or []
            )
            new_options[CONF_PRESENCE_AWARE_BASELINE] = bool(
                user_input[CONF_PRESENCE_AWARE_BASELINE]
            )
            new_options[CONF_MASK_OUTSIDE_ROOMS] = bool(
                user_input[CONF_MASK_OUTSIDE_ROOMS]
            )
            new_options[CONF_AWAY_ENTITY] = str(
                user_input.get(CONF_AWAY_ENTITY) or ""
            ).strip()

            # Sync sensitivity into persistent storage so reload keeps user's choice
            coord = self._get_coordinator()
            if coord is not None:
                coord.set_sensitivity(new_options[CONF_SENSITIVITY], persist=False)
                await coord.async_save_storage()

            return self.async_create_entry(title="", data=new_options)

        coord = self._get_coordinator()
        current_sens = (
            coord.sensitivity
            if coord is not None
            else float(cfg.get(CONF_SENSITIVITY, DEFAULT_SENSITIVITY))
        )
        current_away = str(cfg.get(CONF_AWAY_ENTITY) or "").strip()
        away_key = (
            vol.Optional(CONF_AWAY_ENTITY, description={"suggested_value": current_away})
            if current_away
            else vol.Optional(CONF_AWAY_ENTITY)
        )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SENSITIVITY, default=round(float(current_sens), 2)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0.3, max=2.5, step=0.1)
                ),
                vol.Required(
                    CONF_UPDATE_INTERVAL,
                    default=max(
                        MIN_UPDATE_INTERVAL,
                        float(cfg.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)),
                    ),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_UPDATE_INTERVAL,
                        max=30.0,
                        step=0.5,
                        unit_of_measurement="s",
                    )
                ),
                vol.Required(
                    CONF_FLOOR_WIDTH,
                    default=float(cfg.get(CONF_FLOOR_WIDTH, DEFAULT_FLOOR_WIDTH)),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=2.0, max=50.0, step=0.5, unit_of_measurement="m"
                    )
                ),
                vol.Required(
                    CONF_FLOOR_HEIGHT,
                    default=float(cfg.get(CONF_FLOOR_HEIGHT, DEFAULT_FLOOR_HEIGHT)),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=2.0, max=50.0, step=0.5, unit_of_measurement="m"
                    )
                ),
                vol.Required(
                    CONF_GRID_RESOLUTION,
                    default=float(
                        cfg.get(CONF_GRID_RESOLUTION, DEFAULT_GRID_RESOLUTION)
                    ),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0.25, max=1.5, step=0.05, unit_of_measurement="m"
                    )
                ),
                vol.Required(
                    CONF_ENABLE_WIFI_ENTITIES,
                    default=bool(cfg.get(CONF_ENABLE_WIFI_ENTITIES, True)),
                ): bool,
                vol.Required(
                    CONF_ENABLE_BLE,
                    default=bool(cfg.get(CONF_ENABLE_BLE, True)),
                ): bool,
                vol.Optional(
                    CONF_ACTIVE_POLL_ENTITIES,
                    default=list(cfg.get(CONF_ACTIVE_POLL_ENTITIES) or []),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(multiple=True)
                ),
                vol.Required(
                    CONF_PRESENCE_AWARE_BASELINE,
                    default=bool(
                        cfg.get(
                            CONF_PRESENCE_AWARE_BASELINE,
                            DEFAULT_PRESENCE_AWARE_BASELINE,
                        )
                    ),
                ): bool,
                vol.Required(
                    CONF_MASK_OUTSIDE_ROOMS,
                    default=bool(
                        cfg.get(
                            CONF_MASK_OUTSIDE_ROOMS,
                            DEFAULT_MASK_OUTSIDE_ROOMS,
                        )
                    ),
                ): bool,
                away_key: selector.EntitySelector(
                    selector.EntitySelectorConfig(multiple=False)
                ),
            }
        )
        return self.async_show_form(step_id="tuning", data_schema=schema)

    async def async_step_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Re-scan and add/remove stationary Wi-Fi & BLE devices (capped at MAX_LINKS)."""
        entry = self._get_entry()
        coord = self._get_coordinator()
        cfg = {**entry.data, **entry.options}
        existing_devices: dict[str, dict[str, Any]] = (
            dict(coord.devices)
            if coord is not None
            else dict(cfg.get(CONF_DEVICES) or {})
        )

        self._discovered_wifi = discover_rssi_entities(self.hass)
        self._discovered_ble = discover_ble_devices(self.hass)

        if user_input is not None:
            updated_devices: dict[str, dict[str, Any]] = {}
            for eid in user_input.get("wifi_entities", []):
                if len(updated_devices) >= MAX_LINKS:
                    break
                prev = existing_devices.get(eid)
                meta = self._discovered_wifi.get(eid, {})
                dev_entry = dict(prev) if prev else {}
                dev_entry.setdefault("name", meta.get("name", eid))
                dev_entry["protocol"] = meta.get(
                    "protocol", dev_entry.get("protocol", PROTOCOL_WIFI)
                )
                dev_entry.setdefault(
                    "receiver_id", meta.get("receiver_id", "router_main")
                )
                updated_devices[eid] = dev_entry

            for addr in user_input.get("ble_devices", []):
                if len(updated_devices) >= MAX_LINKS:
                    break
                norm_addr = addr.upper()
                prev = existing_devices.get(norm_addr) or existing_devices.get(addr)
                meta = self._discovered_ble.get(addr) or self._discovered_ble.get(
                    norm_addr, {}
                )
                dev_entry = dict(prev) if prev else {}
                dev_entry.setdefault("name", meta.get("name", norm_addr))
                dev_entry["protocol"] = PROTOCOL_BLE
                dev_entry.setdefault(
                    "receiver_id", meta.get("receiver_id", "ha_bluetooth")
                )
                updated_devices[norm_addr] = dev_entry

            new_options = dict(entry.options)
            new_options[CONF_DEVICES] = updated_devices
            return self.async_create_entry(title="", data=new_options)

        # Include already-selected devices in dropdown options even if temporarily silent
        wifi_option_map: dict[str, str] = {
            eid: f"{meta['name']} ({eid}, {meta['rssi']:.0f} dBm)"
            for eid, meta in self._discovered_wifi.items()
        }
        ble_option_map: dict[str, str] = {}
        for addr, meta in self._discovered_ble.items():
            if meta["name"].strip().lower().replace("-", ":") != addr.lower():
                ble_option_map[addr.upper()] = (
                    f"{meta['name']} ({addr.upper()}, {meta['rssi']:.0f} dBm)"
                )

        current_wifi: list[str] = []
        current_ble: list[str] = []
        for did, dmeta in existing_devices.items():
            if dmeta.get("protocol") == PROTOCOL_BLE:
                norm = did.upper()
                current_ble.append(norm)
                ble_option_map.setdefault(norm, f"{dmeta.get('name', norm)} ({norm})")
            else:
                current_wifi.append(did)
                wifi_option_map.setdefault(did, f"{dmeta.get('name', did)} ({did})")

        fields: dict[Any, Any] = {}
        if wifi_option_map:
            fields[
                vol.Optional("wifi_entities", default=current_wifi)
            ] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value=k, label=v)
                        for k, v in sorted(
                            wifi_option_map.items(), key=lambda kv: kv[1].lower()
                        )
                    ],
                    multiple=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
        if ble_option_map:
            fields[
                vol.Optional("ble_devices", default=current_ble)
            ] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value=k, label=v)
                        for k, v in sorted(
                            ble_option_map.items(), key=lambda kv: kv[1].lower()
                        )
                    ],
                    multiple=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )

        return self.async_show_form(
            step_id="devices",
            data_schema=vol.Schema(fields),
            description_placeholders={
                "wifi_count": str(len(wifi_option_map)),
                "ble_count": str(len(ble_option_map)),
                "max_links": str(MAX_LINKS),
            },
        )

    async def async_step_positions(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Select a receiver or stationary device to edit its 3D (x, y, z) coordinates."""
        if user_input is not None:
            self._selected_position_id = str(user_input["target_id"])
            return await self.async_step_position_edit()

        coord = self._get_coordinator()
        entry = self._get_entry()
        cfg = {**entry.data, **entry.options}
        receivers = (
            coord.receivers if coord is not None else dict(cfg.get(CONF_RECEIVERS) or {})
        )
        devices = (
            coord.devices if coord is not None else dict(cfg.get(CONF_DEVICES) or {})
        )

        options: list[selector.SelectOptionDict] = []
        for rid, rmeta in receivers.items():
            label = (
                f"[RX] {rmeta.get('name', rid)} "
                f"({float(rmeta.get('x', 0)):.1f}m, {float(rmeta.get('y', 0)):.1f}m, "
                f"z={float(rmeta.get('z', 1.0)):.1f}m)"
            )
            options.append(selector.SelectOptionDict(value=rid, label=label))
        for did, dmeta in sorted(
            devices.items(), key=lambda kv: str(kv[1].get("name", kv[0])).lower()
        ):
            label = (
                f"{dmeta.get('name', did)} "
                f"({float(dmeta.get('x', 0)):.1f}m, {float(dmeta.get('y', 0)):.1f}m, "
                f"z={float(dmeta.get('z', 1.0)):.1f}m)"
            )
            options.append(selector.SelectOptionDict(value=did, label=label))

        if not options:
            return await self.async_step_init()

        schema = vol.Schema(
            {
                vol.Required("target_id"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
        return self.async_show_form(step_id="positions", data_schema=schema)

    async def async_step_position_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Edit 3D (x, y, z) position of the chosen receiver or device & persist."""
        target_id = self._selected_position_id
        coord = self._get_coordinator()
        if not target_id or coord is None:
            return await self.async_step_init()

        if user_input is not None:
            await coord.async_set_device_position(
                target_id,
                float(user_input["x"]),
                float(user_input["y"]),
                float(user_input["z"]),
            )
            await coord.async_request_refresh()
            return await self.async_step_init()

        meta = coord.receivers.get(target_id) or coord.devices.get(target_id) or {}
        max_x = max(50.0, round(float(coord.floor_width), 1))
        max_y = max(50.0, round(float(coord.floor_height), 1))
        cur_x = max(0.0, min(max_x, round(float(meta.get("x", 0.0)), 2)))
        cur_y = max(0.0, min(max_y, round(float(meta.get("y", 0.0)), 2)))
        cur_z = max(0.0, min(10.0, round(float(meta.get("z", 1.0)), 2)))
        schema = vol.Schema(
            {
                vol.Required("x", default=cur_x): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0.0, max=max_x, step=0.1, unit_of_measurement="m"
                    )
                ),
                vol.Required("y", default=cur_y): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0.0, max=max_y, step=0.1, unit_of_measurement="m"
                    )
                ),
                vol.Required("z", default=cur_z): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0.0, max=10.0, step=0.05, unit_of_measurement="m"
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="position_edit",
            data_schema=schema,
            description_placeholders={"device_name": str(meta.get("name", target_id))},
        )

    async def async_step_rooms(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Create/update a rectangular room or delete an existing room."""
        coord = self._get_coordinator()
        if coord is None:
            return await self.async_step_init()

        if user_input is not None:
            delete_id = str(user_input.get("delete_room_id") or "").strip()
            if delete_id:
                await coord.async_delete_room(delete_id)

            room_id = str(user_input.get("room_id") or "").strip()
            room_name = str(user_input.get("name") or "").strip()
            if room_id and room_name:
                await coord.async_define_room(
                    room_id=room_id,
                    name=room_name,
                    x_min=float(user_input.get("x_min", 0.0)),
                    y_min=float(user_input.get("y_min", 0.0)),
                    x_max=float(user_input.get("x_max", coord.floor_width)),
                    y_max=float(user_input.get("y_max", coord.floor_height)),
                )

            await coord.async_request_refresh()
            return await self.async_step_init()

        room_options = [
            selector.SelectOptionDict(
                value=r.room_id,
                label=f"{r.name} ({r.room_id}: {r.x_min:.1f}..{r.x_max:.1f}m × {r.y_min:.1f}..{r.y_max:.1f}m)",
            )
            for r in coord.rooms
        ]
        fields: dict[Any, Any] = {
            vol.Optional("room_id", default=""): str,
            vol.Optional("name", default=""): str,
            vol.Optional("x_min", default=0.0): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.0, max=50.0, step=0.1, unit_of_measurement="m"
                )
            ),
            vol.Optional("y_min", default=0.0): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.0, max=50.0, step=0.1, unit_of_measurement="m"
                )
            ),
            vol.Optional(
                "x_max", default=round(coord.floor_width, 1)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.0, max=50.0, step=0.1, unit_of_measurement="m"
                )
            ),
            vol.Optional(
                "y_max", default=round(coord.floor_height, 1)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.0, max=50.0, step=0.1, unit_of_measurement="m"
                )
            ),
        }
        if room_options:
            fields[vol.Optional("delete_room_id")] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=room_options,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )

        return self.async_show_form(
            step_id="rooms",
            data_schema=vol.Schema(fields),
            description_placeholders={"room_count": str(len(coord.rooms))},
        )
