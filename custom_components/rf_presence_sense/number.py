"""Number entities for live sensitivity tuning and calibration duration."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NAME, VERSION
from .coordinator import RFPresenceCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up RF Presence Sense number entities."""
    coordinator: RFPresenceCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            RFSensitivityNumber(coordinator, entry),
            RFCalibrationDurationNumber(coordinator, entry),
        ]
    )


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=NAME,
        manufacturer="RF Presence Sense",
        model="Radio Tomographic Imaging (Wi-Fi + BLE 3D)",
        sw_version=VERSION,
    )


class RFSensitivityNumber(CoordinatorEntity[RFPresenceCoordinator], NumberEntity):
    """Live detection sensitivity multiplier (0.3..2.5) without reload."""

    _attr_has_entity_name = True
    _attr_translation_key = "sensitivity"
    _attr_icon = "mdi:tune-variant"
    _attr_native_min_value = 0.3
    _attr_native_max_value = 2.5
    _attr_native_step = 0.1
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self, coordinator: RFPresenceCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_num_sensitivity"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> float:
        return round(float(self.coordinator.sensitivity), 2)

    async def async_set_native_value(self, value: float) -> None:
        """Apply sensitivity live and persist without reloading integration."""
        self.coordinator.set_sensitivity(value, persist=False)
        await self.coordinator.async_save_storage()
        await self.coordinator.async_request_refresh()


class RFCalibrationDurationNumber(
    CoordinatorEntity[RFPresenceCoordinator], NumberEntity
):
    """Empty-room calibration duration in seconds (5..120)."""

    _attr_has_entity_name = True
    _attr_translation_key = "calibration_duration"
    _attr_icon = "mdi:timer-cog-outline"
    _attr_native_min_value = 5.0
    _attr_native_max_value = 120.0
    _attr_native_step = 5.0
    _attr_native_unit_of_measurement = "s"
    _attr_mode = NumberMode.BOX

    def __init__(
        self, coordinator: RFPresenceCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_num_calibration_duration"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> float:
        return float(self.coordinator.calibration_duration)

    async def async_set_native_value(self, value: float) -> None:
        """Update default calibration duration and persist."""
        self.coordinator.set_calibration_duration(int(round(value)), persist=False)
        await self.coordinator.async_save_storage()
        self.async_write_ha_state()
