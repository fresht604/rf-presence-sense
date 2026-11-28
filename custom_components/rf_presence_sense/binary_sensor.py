"""Binary sensor entities for global and dynamic per-room human & animal occupancy."""

from __future__ import annotations

import json
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
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
    """Set up RF Presence Sense binary sensors with dynamic room discovery."""
    coordinator: RFPresenceCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[BinarySensorEntity] = [
        RFGlobalPresenceBinarySensor(
            coordinator,
            entry,
            key="humans_count",
            translation_key="human_detected",
            icon="mdi:motion-sensor",
        ),
        RFGlobalPresenceBinarySensor(
            coordinator,
            entry,
            key="animals_count",
            translation_key="animal_detected",
            icon="mdi:paw",
        ),
    ]

    created_rooms: set[str] = set()

    def _build_room_entities(room_id: str, room_name: str) -> list[BinarySensorEntity]:
        return [
            RFRoomOccupancyBinarySensor(
                coordinator, entry, room_id, room_name, mode="occupied"
            ),
            RFRoomOccupancyBinarySensor(
                coordinator, entry, room_id, room_name, mode="human"
            ),
            RFRoomOccupancyBinarySensor(
                coordinator, entry, room_id, room_name, mode="animal"
            ),
        ]

    for room in coordinator.rooms:
        created_rooms.add(room.room_id)
        entities.extend(_build_room_entities(room.room_id, room.name))

    async_add_entities(entities)

    @callback
    def _sync_dynamic_rooms() -> None:
        new_entities: list[BinarySensorEntity] = []
        for room in coordinator.rooms:
            if room.room_id not in created_rooms:
                created_rooms.add(room.room_id)
                new_entities.extend(_build_room_entities(room.room_id, room.name))
        if new_entities:
            async_add_entities(new_entities)

    unsub = coordinator.register_room_listener(_sync_dynamic_rooms)
    if hasattr(entry, "async_on_unload"):
        entry.async_on_unload(unsub)


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=NAME,
        manufacturer="RF Presence Sense",
        model="Radio Tomographic Imaging (Wi-Fi + BLE 3D)",
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
        translation_key: str,
        icon: str,
    ) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{entry.entry_id}_global_{key}"
        self._attr_translation_key = translation_key
        self._attr_icon = icon
        self._attr_device_info = _device_info(entry)
        self._last_written_sig: tuple[bool, bool] | None = None

    @property
    def is_on(self) -> bool:
        if not self.coordinator.data:
            return False
        return int(self.coordinator.data.get(self._key, 0)) > 0

    @callback
    def _handle_coordinator_update(self) -> None:
        sig = (self.available, self.is_on)
        if sig != self._last_written_sig:
            self._last_written_sig = sig
            self.async_write_ha_state()


class RFRoomOccupancyBinarySensor(
    CoordinatorEntity[RFPresenceCoordinator], BinarySensorEntity
):
    """Per-room occupancy / human / animal binary sensor."""

    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY
    _attr_has_entity_name = True
    _unrecorded_attributes = frozenset({"room_id", "humans", "animals", "targets"})

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
        self._room_name = room_name
        self._mode = mode
        icon_map = {
            "occupied": "mdi:home-account",
            "human": "mdi:account",
            "animal": "mdi:paw",
        }
        self._attr_unique_id = f"{entry.entry_id}_room_{room_id}_{mode}"
        self._attr_translation_key = f"room_{mode}"
        self._attr_translation_placeholders = {"room": room_name}
        self._attr_icon = icon_map[mode]
        self._attr_device_info = _device_info(entry)
        self._last_written_sig: tuple[Any, ...] | None = None

    @callback
    def _handle_coordinator_update(self) -> None:
        attrs = self.extra_state_attributes
        sig = (
            self.available,
            self.is_on,
            attrs["humans"],
            attrs["animals"],
            json.dumps(attrs["targets"], sort_keys=True, separators=(",", ":")),
        )
        if sig != self._last_written_sig:
            self._last_written_sig = sig
            self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Mark sensor unavailable if its room was deleted."""
        room_exists = any(r.room_id == self._room_id for r in self.coordinator.rooms)
        return room_exists and super().available

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
