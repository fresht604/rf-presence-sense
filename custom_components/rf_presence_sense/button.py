"""Button entities for one-click RF calibration and baseline reset."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
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
    """Set up RF Presence Sense button entities."""
    coordinator: RFPresenceCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            RFCalibrateButton(coordinator, entry),
            RFResetBaselineButton(coordinator, entry),
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


class RFCalibrateButton(CoordinatorEntity[RFPresenceCoordinator], ButtonEntity):
    """Button to start empty-room RF baseline calibration."""

    _attr_has_entity_name = True
    _attr_translation_key = "calibrate_empty_room"
    _attr_icon = "mdi:radar"

    def __init__(
        self, coordinator: RFPresenceCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_btn_calibrate"
        self._attr_device_info = _device_info(entry)

    async def async_press(self) -> None:
        """Start empty-room calibration using configured duration."""
        await self.coordinator.async_calibrate_empty_room(
            self.coordinator.calibration_duration
        )


class RFResetBaselineButton(CoordinatorEntity[RFPresenceCoordinator], ButtonEntity):
    """Button to immediately reset all link baselines to current readings."""

    _attr_has_entity_name = True
    _attr_translation_key = "reset_baseline"
    _attr_icon = "mdi:restore"

    def __init__(
        self, coordinator: RFPresenceCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_btn_reset_baseline"
        self._attr_device_info = _device_info(entry)

    async def async_press(self) -> None:
        """Reset all baselines, persist, and trigger immediate refresh."""
        self.coordinator.reset_baseline(persist=False)
        await self.coordinator.async_save_storage()
        await self.coordinator.async_request_refresh()
