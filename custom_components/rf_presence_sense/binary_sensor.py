"""Binary sensor entities for room-level human & animal occupancy."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
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
    """Set up RF Presence Sense binary sensors."""
    coordinator: RFPresenceCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[BinarySensorEntity] = [
        RFGlobalPresenceBinarySensor(
            coordinator,
            entry,
            key="humans_count",
            name="Mensch erkannt",
            icon="mdi:motion-sensor",
        ),
        RFGlobalPresenceBinarySensor(
            coordinator,
            entry,
            key="animals_count",
            name="Tier erkannt",
            icon="mdi:paw",
        ),
    ]

    for room in coordinator.rooms:
        entities.append(
            RFRoomOccupancyBinarySensor(
                coordinator, entry, room.room_id, room.name, mode="occupied"
            )
        )
        entities.append(
            RFRoomOccupancyBinarySensor(
                coordinator, entry, room.room_id, room.name, mode="human"
            )
        )
        entities.append(
            RFRoomOccupancyBinarySensor(
                coordinator, entry, room.room_id, room.name, mode="animal"
            )
        )

    async_add_entities(entities)


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=NAME,
        manufacturer="RF Presence Sense",
        model="Radio Tomographic Imaging (Wi-Fi + BLE)",
        sw_version=VERSION,
    )


class RFGlobalPresenceBinarySensor(
    CoordinatorEntity[RFPresenceCoordinator], BinarySensorEntity
):
    """Global human or animal presence binary sensor."""

    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RFPresenceCoordinator,
        entry: ConfigEntry,
        key: str,
        name: str,
        icon: str,
    ) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{entry.entry_id}_global_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_device_info = _device_info(entry)

    @property
    def is_on(self) -> bool:
        if not self.coordinator.data:
            return False
        return int(self.coordinator.data.get(self._key, 0)) > 0


class RFRoomOccupancyBinarySensor(
    CoordinatorEntity[RFPresenceCoordinator], BinarySensorEntity
):
    """Per-room occupancy / human / animal binary sensor."""

    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RFPresenceCoordinator,
        entry: ConfigEntry,
        room_id: str,
        room_name: str,
        mode: str,
    ) -> None:
        super().__init__(coordinator)
        self._room_id = room_id
        self._mode = mode
        suffix_map = {
            "occupied": "Präsenz",
            "human": "Mensch",
            "animal": "Tier",
        }
        icon_map = {
            "occupied": "mdi:home-account",
            "human": "mdi:account",
            "animal": "mdi:paw",
        }
        self._attr_unique_id = f"{entry.entry_id}_room_{room_id}_{mode}"
        self._attr_name = f"{room_name} {suffix_map[mode]}"
        self._attr_icon = icon_map[mode]
        self._attr_device_info = _device_info(entry)

    @property
    def is_on(self) -> bool:
        rooms = (self.coordinator.data or {}).get("rooms", {})
        room_data = rooms.get(self._room_id, {})
        if self._mode == "occupied":
            return bool(room_data.get("occupied", False))
        if self._mode == "human":
            return bool(room_data.get("human_detected", False))
        return bool(room_data.get("animal_detected", False))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        rooms = (self.coordinator.data or {}).get("rooms", {})
        room_data = rooms.get(self._room_id, {})
        return {
            "room_id": self._room_id,
            "humans": room_data.get("humans", 0),
            "animals": room_data.get("animals", 0),
            "targets": room_data.get("targets", []),
        }
