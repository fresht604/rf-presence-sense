"""Sensor entities for RF Presence Sense."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
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
    """Set up RF Presence Sense sensors."""
    coordinator: RFPresenceCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = [
        RFCountSensor(
            coordinator,
            entry,
            key="humans_count",
            name="Erkannte Menschen",
            icon="mdi:account-multiple",
        ),
        RFCountSensor(
            coordinator,
            entry,
            key="animals_count",
            name="Erkannte Tiere",
            icon="mdi:paw",
        ),
        RFCountSensor(
            coordinator,
            entry,
            key="total_targets",
            name="Erkannte Lebewesen Gesamt",
            icon="mdi:radar",
        ),
        RFTargetsDetailSensor(coordinator, entry),
        RFLinkHealthSensor(coordinator, entry),
    ]
    async_add_entities(entities)


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=NAME,
        manufacturer="RF Presence Sense",
        model="Radio Tomographic Imaging (Wi-Fi + BLE)",
        sw_version=VERSION,
    )


class RFCountSensor(CoordinatorEntity[RFPresenceCoordinator], SensorEntity):
    """Numeric count sensor for detected humans, animals, or total targets."""

    _attr_state_class = SensorStateClass.MEASUREMENT
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
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> int:
        if not self.coordinator.data:
            return 0
        return int(self.coordinator.data.get(self._key, 0))


class RFTargetsDetailSensor(CoordinatorEntity[RFPresenceCoordinator], SensorEntity):
    """Sensor exposing full target coordinates, velocities, rooms, and classifications."""

    _attr_has_entity_name = True
    _attr_name = "Positionen & Ziele"
    _attr_icon = "mdi:crosshairs-gps"
    _unrecorded_attributes = frozenset({"targets", "rooms", "geometry", "calibrating"})

    def __init__(
        self, coordinator: RFPresenceCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_targets_detail"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> str:
        data = self.coordinator.data or {}
        if data.get("calibrating"):
            return "Kalibrierung läuft"
        humans = data.get("humans_count", 0)
        animals = data.get("animals_count", 0)
        if humans == 0 and animals == 0:
            return "Frei"
        parts = []
        if humans:
            parts.append(f"{humans} Mensch(en)")
        if animals:
            parts.append(f"{animals} Tier(e)")
        return ", ".join(parts)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        return {
            "targets": data.get("targets", []),
            "rooms": data.get("rooms", {}),
            "calibrating": data.get("calibrating", False),
            "geometry": data.get("geometry", {}),
        }


class RFLinkHealthSensor(CoordinatorEntity[RFPresenceCoordinator], SensorEntity):
    """Sensor reporting active RF link coverage and ingested packet count."""

    _attr_has_entity_name = True
    _attr_name = "RF-Netz Abdeckung"
    _attr_icon = "mdi:wifi-check"
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset(
        {"active_links", "total_links", "packets_received", "link_perturbations"}
    )

    def __init__(
        self, coordinator: RFPresenceCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_link_health"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> float:
        if not self.coordinator.data:
            return 0.0
        return float(self.coordinator.data.get("link_health_pct", 0.0))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        return {
            "active_links": data.get("active_links", 0),
            "total_links": data.get("total_links", 0),
            "packets_received": data.get("packets_received", 0),
            "link_perturbations": data.get("link_perturbations", {}),
        }
