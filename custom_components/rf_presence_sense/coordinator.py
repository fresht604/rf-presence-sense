"""DataUpdateCoordinator for RF Presence Sense."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_ACTIVE_POLL_ENTITIES,
    CONF_DEVICES,
    CONF_ENABLE_BLE,
    CONF_ENABLE_WIFI_ENTITIES,
    CONF_FLOOR_HEIGHT,
    CONF_FLOOR_WIDTH,
    CONF_GRID_RESOLUTION,
    CONF_RECEIVERS,
    CONF_ROOMS,
    CONF_SENSITIVITY,
    CONF_UPDATE_INTERVAL,
    DEFAULT_FLOOR_HEIGHT,
    DEFAULT_FLOOR_WIDTH,
    DEFAULT_GRID_RESOLUTION,
    DEFAULT_SENSITIVITY,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    MAX_LINKS,
    MAX_REPORTED_PERTURBATIONS,
    PROTOCOL_BLE,
    TARGET_TYPE_ANIMAL,
    TARGET_TYPE_HUMAN,
)
from .rti_engine import Point3D, RFLink, RTIEngine, RTIReconstruction
from .signal_collector import SignalCollector
from .tracker import MultiTargetTracker, RoomZone, TrackedTarget

_LOGGER = logging.getLogger(__name__)


def _auto_perimeter_positions(
    count: int, width: float, height: float
) -> list[tuple[float, float, float]]:
    """Generate evenly spaced perimeter (x, y, z) positions as smart defaults."""
    if count <= 0:
        return []
    perimeter = 2.0 * (width + height)
    margin = 0.4
    positions: list[tuple[float, float, float]] = []
    for idx in range(count):
        dist = (idx / count) * perimeter
        if dist < width:
            x, y = dist, margin
        elif dist < width + height:
            x, y = width - margin, dist - width
        elif dist < 2 * width + height:
            x, y = width - (dist - width - height), height - margin
        else:
            x, y = margin, height - (dist - 2 * width - height)
        # Alternate low (plug height 0.45m) and high (table/wall height 1.45m) if unknown
        z = 0.45 if idx % 2 == 0 else 1.45
        positions.append((round(x, 2), round(y, 2), z))
    return positions


class RFPresenceCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinates signal collection, RTI reconstruction, and target tracking."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        cfg = {**entry.data, **entry.options}

        self.floor_width = float(cfg.get(CONF_FLOOR_WIDTH, DEFAULT_FLOOR_WIDTH))
        self.floor_height = float(cfg.get(CONF_FLOOR_HEIGHT, DEFAULT_FLOOR_HEIGHT))
        self.grid_resolution = float(
            cfg.get(CONF_GRID_RESOLUTION, DEFAULT_GRID_RESOLUTION)
        )
        self.sensitivity = float(cfg.get(CONF_SENSITIVITY, DEFAULT_SENSITIVITY))
        update_interval = max(
            0.5, float(cfg.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL))
        )

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(seconds=update_interval),
        )

        self.engine = RTIEngine(
            floor_width=self.floor_width,
            floor_height=self.floor_height,
            resolution=self.grid_resolution,
            sensitivity=self.sensitivity,
        )
        # Reflect any auto-upscaled resolution back on coordinator
        self.grid_resolution = self.engine.resolution

        self.receivers: dict[str, dict[str, Any]] = dict(
            cfg.get(CONF_RECEIVERS)
            or {
                "router_main": {
                    "name": "WLAN Router",
                    "x": round(self.floor_width * 0.5, 2),
                    "y": round(self.floor_height * 0.5, 2),
                    "z": 1.2,
                },
                "ha_bluetooth": {
                    "name": "Home Assistant Bluetooth",
                    "x": round(self.floor_width * 0.5, 2),
                    "y": round(self.floor_height * 0.5, 2),
                    "z": 1.0,
                },
            }
        )

        self.devices: dict[str, dict[str, Any]] = dict(cfg.get(CONF_DEVICES) or {})
        self.rooms: list[RoomZone] = self._parse_rooms(cfg.get(CONF_ROOMS))
        self.tracker = MultiTargetTracker(rooms=self.rooms)

        self._active_poll_entities: list[str] = list(
            cfg.get(CONF_ACTIVE_POLL_ENTITIES) or []
        )
        self._calibrating = False
        self._calibration_task: asyncio.Task[None] | None = None
        self._shutdown = False
        self._last_update_ts = time.monotonic()
        self._recon_seq: int = 0
        self.last_reconstruction: RTIReconstruction | None = None
        self.last_targets: list[TrackedTarget] = []

        self.collector = SignalCollector(
            hass=hass,
            on_sample=self._handle_rssi_sample,
            enable_ble=bool(cfg.get(CONF_ENABLE_BLE, True)),
            enable_wifi_entities=bool(cfg.get(CONF_ENABLE_WIFI_ENTITIES, True)),
        )

        # Register links without blocking the event loop on matrix inversion;
        # the first coordinator refresh runs rebuild_projection in executor.
        self._sync_links_from_config()

    @property
    def recon_seq(self) -> int:
        """Monotonic sequence number bumped on every reconstruction or geometry update."""
        return self._recon_seq

    def _parse_rooms(self, raw_rooms: Any) -> list[RoomZone]:
        """Parse configured rooms or create a default full-floor room."""
        if not raw_rooms:
            return [
                RoomZone(
                    room_id="wohnraum",
                    name="Wohnraum",
                    x_min=0.0,
                    y_min=0.0,
                    x_max=self.floor_width,
                    y_max=self.floor_height,
                )
            ]
        rooms: list[RoomZone] = []
        for item in raw_rooms:
            x1 = float(item.get("x_min", 0.0))
            x2 = float(item.get("x_max", self.floor_width))
            y1 = float(item.get("y_min", 0.0))
            y2 = float(item.get("y_max", self.floor_height))
            rooms.append(
                RoomZone(
                    room_id=str(item["id"]),
                    name=str(item.get("name", item["id"])),
                    x_min=min(x1, x2),
                    y_min=min(y1, y2),
                    x_max=max(x1, x2),
                    y_max=max(y1, y2),
                )
            )
        return rooms

    def _sync_links_from_config(self) -> None:
        """Build RFLink objects from configured devices and receivers.

        Leaves projection rebuild deferred to the executor thread during refresh.
        """
        missing = [
            did
            for did, d in self.devices.items()
            if "x" not in d or "y" not in d
        ]
        if missing:
            defaults = _auto_perimeter_positions(
                len(missing), self.floor_width, self.floor_height
            )
            for did, (dx, dy, dz) in zip(missing, defaults):
                self.devices[did]["x"] = dx
                self.devices[did]["y"] = dy
                self.devices[did].setdefault("z", dz)

        entity_map: dict[str, str] = {}
        ble_macs: set[str] = set()

        for device_id, dev in self.devices.items():
            proto = dev.get("protocol", "wifi")
            recv_id = dev.get(
                "receiver_id",
                "ha_bluetooth" if proto == PROTOCOL_BLE else "router_main",
            )
            if recv_id not in self.receivers:
                self.receivers[recv_id] = {
                    "name": recv_id,
                    "x": round(self.floor_width * 0.5, 2),
                    "y": round(self.floor_height * 0.5, 2),
                    "z": 1.1,
                }

            recv = self.receivers[recv_id]
            link_id = f"{device_id}->{recv_id}"
            link = RFLink(
                link_id=link_id,
                device_id=device_id,
                receiver_id=recv_id,
                protocol=proto,
                p1=Point3D(
                    float(dev["x"]),
                    float(dev["y"]),
                    float(dev.get("z", 1.0)),
                ),
                p2=Point3D(
                    float(recv["x"]),
                    float(recv["y"]),
                    float(recv.get("z", 1.1)),
                ),
            )
            if not self.engine.add_or_update_link(link):
                continue

            if proto == PROTOCOL_BLE:
                ble_macs.add(device_id.upper())
            else:
                entity_map[device_id] = recv_id

        self._recon_seq += 1
        self.collector.configure(entity_map, ble_macs)

    def _handle_rssi_sample(
        self,
        device_id: str,
        receiver_id: str,
        rssi_dbm: float,
        protocol: str,
        ts: float,
    ) -> None:
        """Ingest a live RSSI reading into the corresponding RFLink (thread-safe)."""
        if self._shutdown:
            return
        norm_dev = device_id.upper() if protocol == PROTOCOL_BLE else device_id
        link_id = f"{norm_dev}->{receiver_id}"

        if self.engine.ingest_sample(
            link_id, rssi_dbm, now=ts, calibrating=self._calibrating
        ):
            return

        # If a BLE device was received by a different adapter/source, auto-create link
        # within the MAX_LINKS capacity limit.
        if norm_dev in self.devices and len(self.engine.links) < MAX_LINKS:
            dev = self.devices[norm_dev]
            if receiver_id not in self.receivers:
                self.receivers[receiver_id] = {
                    "name": receiver_id,
                    "x": round(self.floor_width * 0.5, 2),
                    "y": round(self.floor_height * 0.5, 2),
                    "z": 1.0,
                }
            recv = self.receivers[receiver_id]
            link = RFLink(
                link_id=link_id,
                device_id=norm_dev,
                receiver_id=receiver_id,
                protocol=protocol,
                p1=Point3D(
                    float(dev["x"]), float(dev["y"]), float(dev.get("z", 1.0))
                ),
                p2=Point3D(
                    float(recv["x"]), float(recv["y"]), float(recv.get("z", 1.0))
                ),
            )
            if self.engine.add_or_update_link(link):
                self.engine.ingest_sample(
                    link_id, rssi_dbm, now=ts, calibrating=self._calibrating
                )

    async def async_calibrate_empty_room(self, duration_seconds: int = 20) -> None:
        """Record empty-room baseline RSSI and noise floor over duration_seconds."""
        if self._shutdown:
            return
        duration = max(5, min(int(duration_seconds), 120))

        # Cancel any overlapping calibration before starting a fresh one
        if self._calibration_task is not None and not self._calibration_task.done():
            self._calibration_task.cancel()
            try:
                await self._calibration_task
            except asyncio.CancelledError:
                pass

        async def _run_calibration() -> None:
            _LOGGER.info(
                "Starting empty-room RF baseline calibration for %d seconds", duration
            )
            self.engine.clear_calibration_samples()
            self._calibrating = True
            cancelled = False
            try:
                await asyncio.sleep(duration)
            except asyncio.CancelledError:
                cancelled = True
                raise
            finally:
                self._calibrating = False
                if not cancelled and not self._shutdown:
                    self.engine.finalize_calibration()

            if not self._shutdown:
                _LOGGER.info("Empty-room RF baseline calibration completed")
                await self.async_request_refresh()

        task = asyncio.create_task(_run_calibration())
        self._calibration_task = task
        try:
            await task
        except asyncio.CancelledError:
            _LOGGER.debug("Empty-room RF baseline calibration cancelled")
        finally:
            if self._calibration_task is task:
                self._calibration_task = None

    def reset_baseline(self) -> None:
        """Reset all link baselines to their current readings."""
        self.engine.reset_baselines()

    def set_device_position(
        self, device_id: str, x: float, y: float, z: float = 1.0
    ) -> None:
        """Update (x, y, z) position of a device or receiver and schedule RTI rebuild."""
        updated = False
        if device_id in self.receivers:
            self.receivers[device_id]["x"] = float(x)
            self.receivers[device_id]["y"] = float(y)
            self.receivers[device_id]["z"] = float(z)
            updated = True
        for key in (device_id, device_id.upper()):
            if key in self.devices:
                self.devices[key]["x"] = float(x)
                self.devices[key]["y"] = float(y)
                self.devices[key]["z"] = float(z)
                updated = True

        if updated:
            self._sync_links_from_config()

    def define_room(
        self,
        room_id: str,
        name: str,
        x_min: float,
        y_min: float,
        x_max: float,
        y_max: float,
    ) -> None:
        """Add or update a room zone."""
        x1, x2 = float(x_min), float(x_max)
        y1, y2 = float(y_min), float(y_max)
        self.rooms = [r for r in self.rooms if r.room_id != room_id]
        self.rooms.append(
            RoomZone(
                room_id=room_id,
                name=name,
                x_min=min(x1, x2),
                y_min=min(y1, y2),
                x_max=max(x1, x2),
                y_max=max(y1, y2),
            )
        )
        self.tracker.set_rooms(self.rooms)
        self._recon_seq += 1

    async def _async_update_data(self) -> dict[str, Any]:
        """Run RTI reconstruction in executor and target tracking on each tick."""
        now = time.monotonic()
        dt = max(0.2, now - self._last_update_ts)
        self._last_update_ts = now

        # Optional active polling for slow Wi-Fi RSSI entities
        if self._active_poll_entities:
            try:
                await self.hass.services.async_call(
                    "homeassistant",
                    "update_entity",
                    {"entity_id": self._active_poll_entities},
                    blocking=False,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Active entity poll skipped: %s", err)

        # Offload CPU-bound RTI projection rebuild & reconstruction off the event loop
        recon: RTIReconstruction = await self.hass.async_add_executor_job(
            self.engine.reconstruct, now
        )
        self.last_reconstruction = recon

        if self._calibrating:
            targets: list[TrackedTarget] = []
        else:
            targets = self.tracker.update(recon, dt=dt)
        self.last_targets = targets
        self._recon_seq += 1

        humans = [t for t in targets if t.target_type == TARGET_TYPE_HUMAN]
        animals = [t for t in targets if t.target_type == TARGET_TYPE_ANIMAL]

        room_summary: dict[str, dict[str, Any]] = {}
        for room in self.rooms:
            r_targets = [t for t in targets if t.room_id == room.room_id]
            r_humans = [t for t in r_targets if t.target_type == TARGET_TYPE_HUMAN]
            r_animals = [t for t in r_targets if t.target_type == TARGET_TYPE_ANIMAL]
            room_summary[room.room_id] = {
                "name": room.name,
                "occupied": len(r_targets) > 0,
                "human_detected": len(r_humans) > 0,
                "animal_detected": len(r_animals) > 0,
                "humans": len(r_humans),
                "animals": len(r_animals),
                "targets": [t.to_dict() for t in r_targets],
            }

        link_health_pct = (
            round(100.0 * recon.active_links / recon.total_links, 1)
            if recon.total_links > 0
            else 0.0
        )

        # Report only actively perturbed links (Top-N) to keep HA state objects compact
        top_perturbations = dict(
            sorted(
                (
                    (lid, val)
                    for lid, val in recon.link_perturbations.items()
                    if val > 0.1
                ),
                key=lambda item: item[1],
                reverse=True,
            )[:MAX_REPORTED_PERTURBATIONS]
        )

        return {
            "calibrating": self._calibrating,
            "humans_count": len(humans),
            "animals_count": len(animals),
            "total_targets": len(targets),
            "targets": [t.to_dict() for t in targets],
            "rooms": room_summary,
            "active_links": recon.active_links,
            "total_links": recon.total_links,
            "link_health_pct": link_health_pct,
            "packets_received": self.collector.packet_count,
            "link_perturbations": top_perturbations,
            "geometry": self.engine.get_geometry_summary(),
        }

    def async_shutdown_collector(self) -> None:
        """Stop all signal collector subscriptions and cancel running calibration."""
        self._shutdown = True
        self._calibrating = False
        if self._calibration_task is not None and not self._calibration_task.done():
            self._calibration_task.cancel()
        self._calibration_task = None
        self.collector.stop()
