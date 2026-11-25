"""DataUpdateCoordinator for RF Presence Sense."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
import logging
import math
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_ACTIVE_POLL_ENTITIES,
    CONF_AWAY_ENTITY,
    CONF_CALIBRATION_DURATION,
    CONF_DEVICES,
    CONF_ENABLE_BLE,
    CONF_ENABLE_WIFI_ENTITIES,
    CONF_FLOOR_HEIGHT,
    CONF_FLOOR_WIDTH,
    CONF_GRID_RESOLUTION,
    CONF_PRESENCE_AWARE_BASELINE,
    CONF_RECEIVERS,
    CONF_ROOMS,
    CONF_SENSITIVITY,
    CONF_UPDATE_INTERVAL,
    DEFAULT_CALIBRATION_DURATION,
    DEFAULT_FLOOR_HEIGHT,
    DEFAULT_FLOOR_WIDTH,
    DEFAULT_GRID_RESOLUTION,
    DEFAULT_PRESENCE_AWARE_BASELINE,
    DEFAULT_SENSITIVITY,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    EVENT_ROOM_CHANGED,
    EVENT_TARGET_DETECTED,
    EVENT_TARGET_LOST,
    MAX_LINKS,
    MAX_REPORTED_HEATMAP_PEAKS,
    MAX_REPORTED_PERTURBATIONS,
    PROTOCOL_BLE,
    TARGET_TYPE_ANIMAL,
    TARGET_TYPE_HUMAN,
)
from .rti_engine import Point3D, RFLink, RTIEngine, RTIReconstruction
from .signal_collector import SignalCollector, discover_entity_areas
from .storage import RFPresenceStorage
from .tracker import MultiTargetTracker, RoomZone, TrackedTarget

_LOGGER = logging.getLogger(__name__)

_AWAY_STATES = frozenset({"not_home", "away", "off", "0", "false", "empty"})
_UNAVAILABLE_STATES = frozenset({"unknown", "unavailable", "none", ""})
_MAX_ROOMS = 64
_MAX_POLYGON_VERTICES = 32


def _auto_perimeter_positions(
    count: int,
    width: float,
    height: float,
    x_offset: float = 0.0,
    y_offset: float = 0.0,
) -> list[tuple[float, float, float]]:
    """Generate evenly spaced perimeter (x, y, z) positions within a rectangle."""
    if count <= 0:
        return []
    margin = min(0.4, width * 0.15, height * 0.15)
    eff_w = max(0.2, width - 2.0 * margin)
    eff_h = max(0.2, height - 2.0 * margin)
    perimeter = 2.0 * (eff_w + eff_h)
    positions: list[tuple[float, float, float]] = []
    for idx in range(count):
        dist = (idx / count) * perimeter
        if dist < eff_w:
            x, y = margin + dist, margin
        elif dist < eff_w + eff_h:
            x, y = margin + eff_w, margin + (dist - eff_w)
        elif dist < 2.0 * eff_w + eff_h:
            x, y = margin + eff_w - (dist - eff_w - eff_h), margin + eff_h
        else:
            x, y = margin, margin + eff_h - (dist - 2.0 * eff_w - eff_h)
        # Alternate low (plug height 0.45m) and high (table/wall height 1.45m) if unknown
        z = 0.45 if idx % 2 == 0 else 1.45
        positions.append((round(x_offset + x, 2), round(y_offset + y, 2), z))
    return positions


class RFPresenceCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinates signal collection, RTI reconstruction, persistence, events, and tracking."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        cfg = {**entry.data, **entry.options}

        self.floor_width = float(cfg.get(CONF_FLOOR_WIDTH, DEFAULT_FLOOR_WIDTH))
        self.floor_height = float(cfg.get(CONF_FLOOR_HEIGHT, DEFAULT_FLOOR_HEIGHT))
        self.grid_resolution = float(
            cfg.get(CONF_GRID_RESOLUTION, DEFAULT_GRID_RESOLUTION)
        )
        self.sensitivity = float(cfg.get(CONF_SENSITIVITY, DEFAULT_SENSITIVITY))
        self.calibration_duration = max(
            5,
            min(
                120,
                int(cfg.get(CONF_CALIBRATION_DURATION, DEFAULT_CALIBRATION_DURATION)),
            ),
        )
        self.presence_aware_baseline = bool(
            cfg.get(CONF_PRESENCE_AWARE_BASELINE, DEFAULT_PRESENCE_AWARE_BASELINE)
        )
        self.away_entity = str(cfg.get(CONF_AWAY_ENTITY, "") or "").strip()
        update_interval = max(
            0.5, float(cfg.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL))
        )

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(seconds=update_interval),
        )

        self.store = RFPresenceStorage(hass, entry.entry_id)

        self.engine = RTIEngine(
            floor_width=self.floor_width,
            floor_height=self.floor_height,
            resolution=self.grid_resolution,
            sensitivity=self.sensitivity,
        )
        # Reflect any auto-upscaled resolution back on coordinator
        self.grid_resolution = self.engine.resolution

        raw_receivers = cfg.get(CONF_RECEIVERS) or {
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
        self.receivers: dict[str, dict[str, Any]] = {
            rid: dict(r) for rid, r in raw_receivers.items()
        }

        raw_devices = cfg.get(CONF_DEVICES) or {}
        self.devices: dict[str, dict[str, Any]] = {
            did: dict(d) for did, d in raw_devices.items()
        }
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

        # Dynamic room listeners, pending storage saves & event transition tracking
        self._room_listeners: list[Callable[[], None]] = []
        self._save_tasks: set[asyncio.Task[Any]] = set()
        self._prev_targets_by_id: dict[int, tuple[str, str | None, str | None]] = {}

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

    def register_room_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        """Register callback invoked when rooms are added, updated, or deleted."""
        self._room_listeners.append(cb)

        def _unsub() -> None:
            if cb in self._room_listeners:
                self._room_listeners.remove(cb)

        return _unsub

    def _notify_room_listeners(self) -> None:
        for cb in list(self._room_listeners):
            try:
                cb()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Room listener error: %s", err)

    async def async_load_storage(self) -> None:
        """Restore persisted positions, rooms, calibrated baselines, and live tuning."""
        stored = await self.store.async_load()
        if not stored:
            return

        # 1. Restore receiver positions (merge so newly added receivers stay intact)
        stored_receivers = stored.get("receivers")
        if isinstance(stored_receivers, dict):
            for rid, rdata in stored_receivers.items():
                if not isinstance(rdata, dict):
                    continue
                if rid in self.receivers:
                    for key in ("x", "y", "z", "name"):
                        if key in rdata:
                            self.receivers[rid][key] = rdata[key]
                elif len(self.receivers) < MAX_LINKS:
                    self.receivers[rid] = dict(rdata)

        # 2. Restore device 3D positions onto currently configured devices
        stored_devices = stored.get("devices")
        if isinstance(stored_devices, dict):
            for did, ddata in stored_devices.items():
                if not isinstance(ddata, dict):
                    continue
                target_key = did if did in self.devices else (
                    did.upper() if did.upper() in self.devices else None
                )
                if target_key is not None:
                    for key in ("x", "y", "z", "area"):
                        if key in ddata:
                            self.devices[target_key][key] = ddata[key]

        # 3. Restore rooms (rectangles or polygons)
        stored_rooms = stored.get("rooms")
        if isinstance(stored_rooms, list) and stored_rooms:
            self.rooms = self._parse_rooms(stored_rooms)
            self.tracker.set_rooms(self.rooms)

        # 4. Restore live tuning (only if not overridden explicitly in options)
        stored_tuning = stored.get("tuning")
        if isinstance(stored_tuning, dict):
            if "sensitivity" in stored_tuning:
                self.sensitivity = max(
                    0.3, min(2.5, float(stored_tuning["sensitivity"]))
                )
                self.engine.sensitivity = self.sensitivity
            if "calibration_duration" in stored_tuning:
                self.calibration_duration = max(
                    5, min(120, int(stored_tuning["calibration_duration"]))
                )

        # Rebuild link objects with restored 3D geometry
        self._sync_links_from_config()

        # 5. Restore calibrated baselines onto links
        stored_baselines = stored.get("baselines")
        if isinstance(stored_baselines, dict) and stored_baselines:
            restored_count = self.engine.import_baselines(stored_baselines)
            _LOGGER.debug(
                "Restored %d calibrated RF link baselines from persistent storage",
                restored_count,
            )

        self._notify_room_listeners()

    async def async_save_storage(self) -> None:
        """Persist current positions, rooms, baselines, and live tuning to disk."""
        payload = {
            "devices": self.devices,
            "receivers": self.receivers,
            "rooms": [room.to_dict() for room in self.rooms],
            "baselines": self.engine.export_baselines(),
            "tuning": {
                "sensitivity": round(self.sensitivity, 2),
                "calibration_duration": int(self.calibration_duration),
            },
        }
        await self.store.async_save(payload)

    def _schedule_save(self) -> None:
        """Schedule an async storage save without blocking synchronous callers."""
        if self._shutdown:
            return
        if hasattr(self.hass, "async_create_task"):
            task = self.hass.async_create_task(self.async_save_storage())
            if isinstance(task, asyncio.Task):
                self._save_tasks.add(task)
                task.add_done_callback(self._save_tasks.discard)

    def _parse_rooms(self, raw_rooms: Any) -> list[RoomZone]:
        """Parse configured rooms (rectangular or polygon) or create default full-floor room."""
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
        for item in raw_rooms[:_MAX_ROOMS]:
            if not isinstance(item, dict) or "id" not in item:
                continue
            raw_poly = item.get("polygon")
            polygon: list[tuple[float, float]] | None = None
            if isinstance(raw_poly, list) and len(raw_poly) >= 3:
                try:
                    polygon = [
                        (float(pt[0]), float(pt[1]))
                        for pt in raw_poly[:_MAX_POLYGON_VERTICES]
                    ]
                except (TypeError, ValueError, IndexError):
                    polygon = None
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
                    polygon=polygon,
                )
            )
        return rooms or [
            RoomZone(
                room_id="wohnraum",
                name="Wohnraum",
                x_min=0.0,
                y_min=0.0,
                x_max=self.floor_width,
                y_max=self.floor_height,
            )
        ]

    def _assign_default_positions(self, missing_ids: list[str]) -> None:
        """Assign smart default (x, y, z) using HA Area grouping when available."""
        if not missing_ids:
            return

        area_map = discover_entity_areas(self.hass, missing_ids)
        # Group devices by discovered HA area name
        by_area: dict[str, list[str]] = {}
        unassigned: list[str] = []
        for did in missing_ids:
            area = self.devices[did].get("area") or area_map.get(did)
            if area:
                self.devices[did]["area"] = str(area)
                by_area.setdefault(str(area), []).append(did)
            else:
                unassigned.append(did)

        if by_area:
            area_names = sorted(by_area.keys())
            n_areas = len(area_names)
            cols = max(1, int(math.ceil(math.sqrt(n_areas))))
            rows = max(1, int(math.ceil(n_areas / cols)))
            cell_w = self.floor_width / cols
            cell_h = self.floor_height / rows

            for a_idx, area_name in enumerate(area_names):
                r_idx, c_idx = divmod(a_idx, cols)
                dids = by_area[area_name]
                pts = _auto_perimeter_positions(
                    len(dids),
                    cell_w,
                    cell_h,
                    x_offset=c_idx * cell_w,
                    y_offset=r_idx * cell_h,
                )
                for did, (dx, dy, dz) in zip(dids, pts):
                    self.devices[did]["x"] = dx
                    self.devices[did]["y"] = dy
                    self.devices[did].setdefault("z", dz)

        if unassigned:
            defaults = _auto_perimeter_positions(
                len(unassigned), self.floor_width, self.floor_height
            )
            for did, (dx, dy, dz) in zip(unassigned, defaults):
                self.devices[did]["x"] = dx
                self.devices[did]["y"] = dy
                self.devices[did].setdefault("z", dz)

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
            self._assign_default_positions(missing)

        entity_map: dict[str, str] = {}
        ble_macs: set[str] = set()
        valid_link_ids: set[str] = set()

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
            valid_link_ids.add(link_id)

            if proto == PROTOCOL_BLE:
                ble_macs.add(device_id.upper())
            else:
                entity_map[device_id] = recv_id

        # Keep dynamically discovered Multi-AP/BLE-proxy links belonging to valid devices
        # and synchronize their 3D endpoints if the device or receiver moved.
        with self.engine._lock:
            for lid, existing_link in list(self.engine.links.items()):
                if existing_link.device_id in self.devices:
                    valid_link_ids.add(lid)
                    if lid not in {
                        f"{d}->{m.get('receiver_id', 'ha_bluetooth' if m.get('protocol') == PROTOCOL_BLE else 'router_main')}"
                        for d, m in self.devices.items()
                    }:
                        d_meta = self.devices[existing_link.device_id]
                        r_meta = self.receivers.get(existing_link.receiver_id)
                        if r_meta is not None:
                            new_p1 = Point3D(
                                float(d_meta["x"]),
                                float(d_meta["y"]),
                                float(d_meta.get("z", 1.0)),
                            )
                            new_p2 = Point3D(
                                float(r_meta["x"]),
                                float(r_meta["y"]),
                                float(r_meta.get("z", 1.1)),
                            )
                            if existing_link.p1 != new_p1 or existing_link.p2 != new_p2:
                                existing_link.p1 = new_p1
                                existing_link.p2 = new_p2
                                self.engine._dirty_geometry = True
                                self.engine._geometry_version += 1
        self.engine.clear_links_except(valid_link_ids)

        self._recon_seq += 1
        self.collector.configure(entity_map, ble_macs)

    def _should_freeze_baseline(self) -> bool:
        """Return True if adaptive baseline drift should be paused to protect stationary targets."""
        if not self.presence_aware_baseline:
            return False
        if self.away_entity:
            state = self.hass.states.get(self.away_entity)
            if state is not None:
                norm = str(state.state).strip().lower()
                if norm not in _UNAVAILABLE_STATES:
                    if norm in _AWAY_STATES:
                        return False  # Confirmed empty house -> allow adaptation
                    return True  # Someone is home -> freeze baseline
        # Fallback: freeze drift whenever any confirmed target is active
        return len(self.last_targets) > 0

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
        freeze = self._should_freeze_baseline()

        if self.engine.ingest_sample(
            link_id,
            rssi_dbm,
            now=ts,
            calibrating=self._calibrating,
            freeze_baseline=freeze,
        ):
            return

        # If a BLE device or roaming Wi-Fi device was received by another adapter/AP,
        # auto-create the link within the MAX_LINKS capacity limit.
        if norm_dev in self.devices and len(self.engine.links) < MAX_LINKS:
            dev = self.devices[norm_dev]
            if receiver_id not in self.receivers:
                if len(self.receivers) >= MAX_LINKS:
                    return
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
                    link_id,
                    rssi_dbm,
                    now=ts,
                    calibrating=self._calibrating,
                    freeze_baseline=freeze,
                )

    async def async_calibrate_empty_room(self, duration_seconds: int | None = None) -> None:
        """Record empty-room baseline RSSI and noise floor over duration_seconds."""
        if self._shutdown:
            return
        dur_val = self.calibration_duration if duration_seconds is None else duration_seconds
        duration = max(5, min(int(dur_val), 120))

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
            self._recon_seq += 1
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
                await self.async_save_storage()
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

    def reset_baseline(self, persist: bool = True) -> None:
        """Reset all link baselines to their current readings and optionally persist."""
        self.engine.reset_baselines()
        if persist:
            self._schedule_save()

    def set_sensitivity(self, value: float, persist: bool = True) -> None:
        """Update detection sensitivity live without integration reload."""
        clamped = round(max(0.3, min(2.5, float(value))), 2)
        self.sensitivity = clamped
        self.engine.sensitivity = clamped
        self._recon_seq += 1
        if persist:
            self._schedule_save()

    def set_calibration_duration(self, seconds: int, persist: bool = True) -> None:
        """Update default calibration duration in seconds."""
        self.calibration_duration = max(5, min(120, int(seconds)))
        if persist:
            self._schedule_save()

    def set_device_position(
        self, device_id: str, x: float, y: float, z: float = 1.0
    ) -> bool:
        """Update (x, y, z) position of a device or receiver and schedule RTI rebuild."""
        updated = False
        if device_id in self.receivers:
            self.receivers[device_id]["x"] = round(float(x), 3)
            self.receivers[device_id]["y"] = round(float(y), 3)
            self.receivers[device_id]["z"] = round(float(z), 3)
            updated = True
        for key in (device_id, device_id.upper()):
            if key in self.devices:
                self.devices[key]["x"] = round(float(x), 3)
                self.devices[key]["y"] = round(float(y), 3)
                self.devices[key]["z"] = round(float(z), 3)
                updated = True

        if updated:
            self._sync_links_from_config()
            self._schedule_save()
        return updated

    async def async_set_device_position(
        self, device_id: str, x: float, y: float, z: float = 1.0
    ) -> bool:
        """Update 3D position and immediately persist to storage."""
        updated = False
        if device_id in self.receivers:
            self.receivers[device_id]["x"] = round(float(x), 3)
            self.receivers[device_id]["y"] = round(float(y), 3)
            self.receivers[device_id]["z"] = round(float(z), 3)
            updated = True
        for key in (device_id, device_id.upper()):
            if key in self.devices:
                self.devices[key]["x"] = round(float(x), 3)
                self.devices[key]["y"] = round(float(y), 3)
                self.devices[key]["z"] = round(float(z), 3)
                updated = True
        if updated:
            self._sync_links_from_config()
            await self.async_save_storage()
        return updated

    def define_room(
        self,
        room_id: str,
        name: str,
        x_min: float,
        y_min: float,
        x_max: float,
        y_max: float,
        polygon: list[tuple[float, float]] | list[list[float]] | None = None,
    ) -> None:
        """Add or update a rectangular or polygon room zone."""
        poly_pts: list[tuple[float, float]] | None = None
        if polygon is not None and len(polygon) >= 3:
            poly_pts = [
                (float(p[0]), float(p[1])) for p in polygon[:_MAX_POLYGON_VERTICES]
            ]
        x1, x2 = float(x_min), float(x_max)
        y1, y2 = float(y_min), float(y_max)
        existing_other = [r for r in self.rooms if r.room_id != room_id]
        if len(existing_other) >= _MAX_ROOMS:
            _LOGGER.warning("Room limit (%d) reached; ignoring %s", _MAX_ROOMS, room_id)
            return
        self.rooms = existing_other
        self.rooms.append(
            RoomZone(
                room_id=room_id,
                name=name,
                x_min=min(x1, x2),
                y_min=min(y1, y2),
                x_max=max(x1, x2),
                y_max=max(y1, y2),
                polygon=poly_pts,
            )
        )
        self.tracker.set_rooms(self.rooms)
        self._recon_seq += 1
        self._notify_room_listeners()
        self._schedule_save()

    async def async_define_room(
        self,
        room_id: str,
        name: str,
        x_min: float,
        y_min: float,
        x_max: float,
        y_max: float,
        polygon: list[tuple[float, float]] | list[list[float]] | None = None,
    ) -> None:
        """Add or update a room zone and persist immediately."""
        poly_pts: list[tuple[float, float]] | None = None
        if polygon is not None and len(polygon) >= 3:
            poly_pts = [
                (float(p[0]), float(p[1])) for p in polygon[:_MAX_POLYGON_VERTICES]
            ]
        x1, x2 = float(x_min), float(x_max)
        y1, y2 = float(y_min), float(y_max)
        existing_other = [r for r in self.rooms if r.room_id != room_id]
        if len(existing_other) >= _MAX_ROOMS:
            _LOGGER.warning("Room limit (%d) reached; ignoring %s", _MAX_ROOMS, room_id)
            return
        self.rooms = existing_other
        self.rooms.append(
            RoomZone(
                room_id=room_id,
                name=name,
                x_min=min(x1, x2),
                y_min=min(y1, y2),
                x_max=max(x1, x2),
                y_max=max(y1, y2),
                polygon=poly_pts,
            )
        )
        self.tracker.set_rooms(self.rooms)
        self._recon_seq += 1
        self._notify_room_listeners()
        await self.async_save_storage()

    def delete_room(self, room_id: str) -> bool:
        """Delete a room zone by ID."""
        before = len(self.rooms)
        self.rooms = [r for r in self.rooms if r.room_id != room_id]
        if len(self.rooms) == before:
            return False
        self.tracker.set_rooms(self.rooms)
        self._recon_seq += 1
        self._notify_room_listeners()
        self._schedule_save()
        return True

    async def async_delete_room(self, room_id: str) -> bool:
        """Delete a room zone by ID and persist immediately."""
        before = len(self.rooms)
        self.rooms = [r for r in self.rooms if r.room_id != room_id]
        if len(self.rooms) == before:
            return False
        self.tracker.set_rooms(self.rooms)
        self._recon_seq += 1
        self._notify_room_listeners()
        await self.async_save_storage()
        return True

    def _fire_target_events(self, targets: list[TrackedTarget]) -> None:
        """Emit Home Assistant bus events on target detection, room change, and loss."""
        bus = getattr(self.hass, "bus", None)
        if bus is None or not hasattr(bus, "async_fire"):
            return

        current_by_id: dict[int, TrackedTarget] = {t.track_id: t for t in targets}

        # 1. Newly detected or room-changed targets
        for tid, target in current_by_id.items():
            prev = self._prev_targets_by_id.get(tid)
            if prev is None:
                bus.async_fire(
                    EVENT_TARGET_DETECTED,
                    {
                        "entry_id": self.entry.entry_id,
                        "track_id": tid,
                        "type": target.target_type,
                        "room_id": target.room_id,
                        "room": target.room_name or "Unbekannt",
                        "x": round(target.x, 2),
                        "y": round(target.y, 2),
                        "confidence": round(target.confidence, 2),
                    },
                )
            else:
                _, prev_room_id, prev_room_name = prev
                if target.room_id != prev_room_id:
                    bus.async_fire(
                        EVENT_ROOM_CHANGED,
                        {
                            "entry_id": self.entry.entry_id,
                            "track_id": tid,
                            "type": target.target_type,
                            "from_room_id": prev_room_id,
                            "from_room": prev_room_name or "Unbekannt",
                            "to_room_id": target.room_id,
                            "to_room": target.room_name or "Unbekannt",
                            "x": round(target.x, 2),
                            "y": round(target.y, 2),
                            "confidence": round(target.confidence, 2),
                        },
                    )

        # 2. Lost targets
        for prev_tid, (prev_type, prev_room_id, prev_room_name) in self._prev_targets_by_id.items():
            if prev_tid not in current_by_id:
                bus.async_fire(
                    EVENT_TARGET_LOST,
                    {
                        "entry_id": self.entry.entry_id,
                        "track_id": prev_tid,
                        "type": prev_type,
                        "last_room_id": prev_room_id,
                        "last_room": prev_room_name or "Unbekannt",
                    },
                )

        self._prev_targets_by_id = {
            tid: (t.target_type, t.room_id, t.room_name)
            for tid, t in current_by_id.items()
        }

    def _extract_compact_heatmap_peaks(
        self, recon: RTIReconstruction
    ) -> list[list[float]]:
        """Extract up to MAX_REPORTED_HEATMAP_PEAKS hot voxels for the 3D Lovelace Card."""
        peaks: list[tuple[float, float, float]] = []
        res = recon.resolution
        for r in range(recon.height):
            cy = (r + 0.5) * res
            row = recon.grid[r]
            for c in range(recon.width):
                val = row[c]
                if val > 0.25:
                    peaks.append((val, (c + 0.5) * res, cy))
        peaks.sort(key=lambda item: item[0], reverse=True)
        return [
            [round(px, 2), round(py, 2), round(val, 2)]
            for val, px, py in peaks[:MAX_REPORTED_HEATMAP_PEAKS]
        ]

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
            self._fire_target_events(targets)
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

        # Compact 3D scene payload for the interactive 3D Lovelace Card (all unrecorded,
        # engineered to stay strictly under 16 KB even at MAX_LINKS=80 + 16 targets!)
        devices_3d: list[list[Any]] = []
        dev_idx_map: dict[str, int] = {}
        for did, dev in list(self.devices.items())[:MAX_LINKS]:
            dev_idx_map[did] = len(devices_3d)
            raw_name = str(dev.get("name", did))[:20]
            devices_3d.append(
                [
                    did,
                    "" if raw_name == did else raw_name,
                    "b" if dev.get("protocol") == PROTOCOL_BLE else "w",
                    round(float(dev.get("x", 0.0)), 2),
                    round(float(dev.get("y", 0.0)), 2),
                    round(float(dev.get("z", 1.0)), 2),
                ]
            )

        receivers_3d: list[dict[str, Any]] = []
        recv_idx_map: dict[str, int] = {}
        for rid, recv in list(self.receivers.items())[:MAX_LINKS]:
            recv_idx_map[rid] = len(receivers_3d)
            receivers_3d.append(
                {
                    "id": rid,
                    "name": str(recv.get("name", rid))[:24],
                    "x": round(float(recv.get("x", 0.0)), 2),
                    "y": round(float(recv.get("y", 0.0)), 2),
                    "z": round(float(recv.get("z", 1.1)), 2),
                }
            )

        active_links_3d: list[Any] = []
        for lid, proto, x1, y1, z1, x2, y2, z2 in self.engine.snapshot_links_3d():
            pert = round(recon.link_perturbations.get(lid, 0.0), 2)
            p_code = "b" if proto == PROTOCOL_BLE else "w"
            d_part, _, r_part = lid.partition("->")
            di = dev_idx_map.get(d_part)
            ri = recv_idx_map.get(r_part)
            if di is not None and ri is not None:
                entry_link: list[Any] = (
                    [di, ri, p_code, pert] if pert > 0.1 else [di, ri, p_code]
                )
                active_links_3d.append(entry_link)
            else:
                active_links_3d.append(
                    {
                        "id": lid,
                        "p": proto,
                        "p1": [round(x1, 2), round(y1, 2), round(z1, 2)],
                        "p2": [round(x2, 2), round(y2, 2), round(z2, 2)],
                        "pert": pert,
                    }
                )

        return {
            "calibrating": self._calibrating,
            "humans_count": len(humans),
            "animals_count": len(animals),
            "total_targets": len(targets),
            "targets": [t.to_dict() for t in targets],
            "rooms": room_summary,
            "rooms_geometry": [r.to_dict() for r in self.rooms],
            "devices": devices_3d,
            "receivers": receivers_3d,
            "active_links_3d": active_links_3d,
            "heatmap_peaks": self._extract_compact_heatmap_peaks(recon),
            "active_links": recon.active_links,
            "total_links": recon.total_links,
            "link_health_pct": link_health_pct,
            "packets_received": self.collector.packet_count,
            "link_perturbations": top_perturbations,
            "geometry": self.engine.get_geometry_summary(),
            "sensitivity": self.sensitivity,
            "calibration_duration": self.calibration_duration,
            "presence_aware_baseline": self.presence_aware_baseline,
        }

    def async_shutdown_collector(self) -> None:
        """Stop all signal collector subscriptions, room listeners, and cancel calibration."""
        self._shutdown = True
        self._calibrating = False
        if self._calibration_task is not None and not self._calibration_task.done():
            self._calibration_task.cancel()
        self._calibration_task = None
        self._room_listeners.clear()
        self.collector.stop()

    async def async_shutdown(self) -> None:
        """Gracefully stop collector, cancel calibration, and flush pending storage saves."""
        self.async_shutdown_collector()
        if self._save_tasks:
            pending = list(self._save_tasks)
            self._save_tasks.clear()
            await asyncio.gather(*pending, return_exceptions=True)
