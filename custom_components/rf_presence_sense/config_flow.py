"""Config Flow & Options Flow for RF Presence Sense."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_DEVICES,
    CONF_ENABLE_BLE,
    CONF_ENABLE_WIFI_ENTITIES,
    CONF_FLOOR_HEIGHT,
    CONF_FLOOR_WIDTH,
    CONF_GRID_RESOLUTION,
    CONF_RECEIVERS,
    CONF_SENSITIVITY,
    CONF_UPDATE_INTERVAL,
    DEFAULT_FLOOR_HEIGHT,
    DEFAULT_FLOOR_WIDTH,
    DEFAULT_GRID_RESOLUTION,
    DEFAULT_SENSITIVITY,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
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
                CONF_UPDATE_INTERVAL: float(user_input[CONF_UPDATE_INTERVAL]),
                CONF_SENSITIVITY: float(user_input[CONF_SENSITIVITY]),
                CONF_ENABLE_WIFI_ENTITIES: bool(
                    user_input[CONF_ENABLE_WIFI_ENTITIES]
                ),
                CONF_ENABLE_BLE: bool(user_input[CONF_ENABLE_BLE]),
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
                        min=0.5, max=10.0, step=0.5, unit_of_measurement="s"
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
                meta = self._discovered_wifi.get(eid, {})
                devices[eid] = {
                    "name": meta.get("name", eid),
                    "protocol": meta.get("protocol", PROTOCOL_WIFI),
                    "receiver_id": meta.get("receiver_id", "router_main"),
                }
            for addr in user_input.get("ble_devices", []):
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
        # while still letting the user pick any named BLE device.
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

        default_wifi = [
            eid
            for eid, meta in self._discovered_wifi.items()
            if is_likely_stationary(meta["name"], eid)
        ]
        default_ble = [
            addr
            for addr, meta in named_ble.items()
            if is_likely_stationary(meta["name"], addr)
        ]

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
        """Return options flow."""
        return RFPresenceOptionsFlow(config_entry)


class RFPresenceOptionsFlow(config_entries.OptionsFlow):
    """Handle tuning options for RF Presence Sense."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage sensitivity, update interval, and active polling."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        cfg = {**self._entry.data, **self._entry.options}
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SENSITIVITY,
                    default=float(cfg.get(CONF_SENSITIVITY, DEFAULT_SENSITIVITY)),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0.3, max=2.5, step=0.1)
                ),
                vol.Required(
                    CONF_UPDATE_INTERVAL,
                    default=float(
                        cfg.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
                    ),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0.5, max=10.0, step=0.5, unit_of_measurement="s"
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
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
