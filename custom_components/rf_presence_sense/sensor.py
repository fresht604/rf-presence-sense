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
            translation_key="humans_count",
            icon="mdi:account-multiple",
        ),
        RFCountSensor(
            coordinator,
            entry,
            key="animals_count",
            translation_key="animals_count",
            icon="mdi:paw",
        ),
        RFCountSensor(
            coordinator,
            entry,
            key="total_targets",
            translation_key="total_targets",
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
        model="Radio Tomographic Imaging (Wi-Fi + BLE 3D)",
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
        translation_key: str,
        icon: str,
    ) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = translation_key
        self._attr_icon = icon
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> int:
        if not self.coordinator.data:
            return 0
        return int(self.coordinator.data.get(self._key, 0))


class RFTargetsDetailSensor(CoordinatorEntity[RFPresenceCoordinator], SensorEntity):
    """Sensor exposing full 3D target coordinates, rooms, links, and heatmap data."""

    _attr_has_entity_name = True
    _attr_translation_key = "targets_detail"
    _attr_icon = "mdi:crosshairs-gps"
    # Exclude ALL volatile & 3D scene attributes from HA Recorder DB!
    _unrecorded_attributes = frozenset(
        {
            "targets",
            "rooms",
            "rooms_geometry",
            "geometry",
            "calibrating",
            "devices",
            "receivers",
            "active_links_3d",
            "heatmap_peaks",
            "sensitivity",
            "calibration_duration",
            "presence_aware_baseline",
            "mask_outside_rooms",
            "fingerprint_count",
            "edge_walk_active",
            "fingerprints_xy",
        }
    )

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
            return "calibrating"
        humans = data.get("humans_count", 0)
        animals = data.get("animals_count", 0)
        if humans == 0 and animals == 0:
            return "clear"
        parts = []
        if humans:
            parts.append(f"{humans}H")
        if animals:
            parts.append(f"{animals}A")
        return " ".join(parts)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        # Omit nested duplicate 'targets' inside rooms (already exposed at top level)
        raw_rooms = data.get("rooms", {})
        compact_rooms = {
            rid: {
                k: v
                for k, v in rdata.items()
                if k not in ("targets", "name")
            }
            for rid, rdata in raw_rooms.items()
            if isinstance(rdata, dict)
        }
        # Keep serialized targets compact so worst-case 16 targets + 80 links +
        # 80 fingerprints + 8 rooms + peaks stays strictly under HA's 16 KB budget
        raw_targets = data.get("targets", [])
        if len(raw_targets) > 4:
            compact_targets = [
                (
                    {**t, "trail": t["trail"][-4:]}
                    if isinstance(t, dict) and isinstance(t.get("trail"), list)
                    else t
                )
                for t in raw_targets
            ]
        else:
            compact_targets = raw_targets
        return {
            "targets": compact_targets,
            "rooms": compact_rooms,
            "rooms_geometry": data.get("rooms_geometry", []),
            "calibrating": data.get("calibrating", False),
            "geometry": data.get("geometry", {}),
            "devices": data.get("devices", []),
            "receivers": data.get("receivers", []),
            "active_links_3d": data.get("active_links_3d", []),
            "heatmap_peaks": data.get("heatmap_peaks", []),
            "sensitivity": data.get("sensitivity", self.coordinator.sensitivity),
            "calibration_duration": data.get(
                "calibration_duration", self.coordinator.calibration_duration
            ),
            "presence_aware_baseline": data.get(
                "presence_aware_baseline", self.coordinator.presence_aware_baseline
            ),
            "mask_outside_rooms": data.get(
                "mask_outside_rooms", self.coordinator.mask_outside_rooms
            ),
            "fingerprint_count": data.get("fingerprint_count", 0),
            "edge_walk_active": data.get("edge_walk_active", False),
            "fingerprints_xy": data.get("fingerprints_xy", []),
        }


class RFLinkHealthSensor(CoordinatorEntity[RFPresenceCoordinator], SensorEntity):
    """Sensor reporting active RF link coverage and ingested packet count."""

    _attr_has_entity_name = True
    _attr_translation_key = "link_health"
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
