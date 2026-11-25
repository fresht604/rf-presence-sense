"""Unit tests for RF Presence Sense v2.1.0 features.

Covers:
  - Room masking (neighboring apartment ghost suppression)
  - Doors & open passages (open_edges) + persistence + 3D PNG rendering
  - Walk-calibration (point recording, edge-walk interpolation, clear) & MAX_FINGERPRINTS cap
  - Hybrid RTI + RF-Fingerprinting cosine/L2 fusion
  - Auto-placement of devices & Shelly/BLE scanners from HA Areas & names
  - Worst-case < 16 KB state payload with 80 fingerprints, doors, 80 links & 16 targets
  - Sidebar panel WebComponent <rf-presence-3d-panel> & 3D CAD card JS verification
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import struct
import time
import types
import unittest
from unittest.mock import patch

from _loader import load_core_modules, load_submodule, reset_stub_storage

_const, _rti, _tracker = load_core_modules()
_collector_mod = load_submodule("signal_collector", with_ha_stubs=True)
_storage_mod = load_submodule("storage", with_ha_stubs=True)
_coordinator_mod = load_submodule("coordinator", with_ha_stubs=True)
_camera_mod = load_submodule("camera", with_ha_stubs=True)
_sensor_mod = load_submodule("sensor", with_ha_stubs=True)

Point3D = _rti.Point3D
RFLink = _rti.RFLink
RFFingerprint = _rti.RFFingerprint
RTIEngine = _rti.RTIEngine
RoomZone = _tracker.RoomZone


class _SocketlessEventLoop(asyncio.BaseEventLoop):
    def __init__(self) -> None:
        super().__init__()
        self._selector = self

    def select(self, timeout: float | None = None):
        if timeout is not None and timeout > 0:
            time.sleep(min(timeout, 0.05))
        return []

    def _write_to_self(self) -> None:
        pass

    def _process_events(self, _event_list) -> None:
        pass


def _run_async(coro):
    loop = _SocketlessEventLoop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        try:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
        finally:
            asyncio.set_event_loop(None)
            loop.close()


class _FakeHass:
    def __init__(self) -> None:
        self.data: dict = {}
        self._states: dict = {}
        self.states = types.SimpleNamespace(
            get=lambda eid: self._states.get(eid),
            async_all=lambda: list(self._states.values()),
        )
        self.services = types.SimpleNamespace(
            async_call=self._noop_async,
            has_service=lambda *_a: False,
            async_remove=lambda *_a: None,
        )

    async def _noop_async(self, *_args, **_kwargs) -> None:
        return None

    async def async_add_executor_job(self, target, *args):
        return target(*args)

    def async_create_task(self, coro):
        return asyncio.create_task(coro)


class TestRoomMaskingAndDoors(unittest.TestCase):
    """Verify neighbor-apartment masking and doors/open_edges support."""

    def setUp(self) -> None:
        reset_stub_storage()

    def test_room_mask_zeros_neighbor_apartment_voxels(self) -> None:
        """Voxels outside configured rooms must stay 0.0 when room masking is active."""
        engine = RTIEngine(floor_width=10.0, floor_height=8.0, resolution=0.5)
        link = RFLink(
            link_id="d1->r1",
            device_id="d1",
            receiver_id="r1",
            protocol="wifi",
            p1=Point3D(1.0, 4.0, 1.2),
            p2=Point3D(9.0, 4.0, 1.2),
        )
        link.baseline_rssi = -60.0
        link.add_sample(-68.0, now=100.0)
        engine.add_or_update_link(link)

        # Configure room only in left half (x in [0..5]); right half (x > 5) is neighbor!
        my_room = RoomZone("wz", "Wohnzimmer", 0.0, 0.0, 5.0, 8.0)
        engine.set_room_mask([my_room], enabled=True)
        recon = engine.reconstruct(now=101.0)

        inside_max = 0.0
        outside_max = 0.0
        for r in range(recon.height):
            for c in range(recon.width):
                vx = (c + 0.5) * recon.resolution
                val = recon.grid[r][c]
                if vx <= 5.0:
                    inside_max = max(inside_max, val)
                else:
                    outside_max = max(outside_max, val)

        self.assertGreater(inside_max, 0.5)
        self.assertEqual(outside_max, 0.0)

    def test_doors_and_open_edges_persist_and_render_in_3d_png(self) -> None:
        """Doors & open_edges must survive storage roundtrip and render in 3D PNG."""
        hass = _FakeHass()
        entry = types.SimpleNamespace(
            entry_id="doors_test",
            data={"floor_width": 10.0, "floor_height": 8.0, "enable_ble": False, "enable_wifi_entities": False},
            options={},
        )

        async def _run() -> None:
            coord1 = _coordinator_mod.RFPresenceCoordinator(hass, entry)
            await coord1.async_define_room(
                "wz",
                "Wohnzimmer",
                0.0,
                0.0,
                5.0,
                6.0,
                open_edges=["east"],
                doors=[{"edge": "south", "pos": 0.4, "width": 0.95}],
            )
            await coord1.async_shutdown()

            coord2 = _coordinator_mod.RFPresenceCoordinator(hass, entry)
            await coord2.async_load_storage()
            rm = next(r for r in coord2.rooms if r.room_id == "wz")
            self.assertEqual(rm.open_edges, ["east"])
            self.assertEqual(len(rm.doors), 1)
            self.assertEqual(rm.doors[0]["edge"], "south")
            self.assertAlmostEqual(rm.doors[0]["width"], 0.95, places=2)

            png = _camera_mod.render_heatmap_png(coord2, w_px=640, h_px=480)
            self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
            w, h = struct.unpack("!II", png[16:24])
            self.assertEqual((w, h), (640, 480))
            await coord2.async_shutdown()

        _run_async(_run())


class TestWalkCalibrationAndHybridFusion(unittest.TestCase):
    """Tests for point calibration, edge-walk, MAX_FINGERPRINTS cap, and hybrid fusion."""

    def setUp(self) -> None:
        reset_stub_storage()

    def test_fingerprint_cap_and_hybrid_rti_boost(self) -> None:
        """Fingerprints cap at MAX_FINGERPRINTS and boost matching location in reconstruct()."""
        engine = RTIEngine(floor_width=8.0, floor_height=6.0, resolution=0.5)
        for i in range(4):
            lk = RFLink(
                link_id=f"d{i}->r0",
                device_id=f"d{i}",
                receiver_id="r0",
                protocol="wifi",
                p1=Point3D(0.5 if i < 2 else 7.5, 0.5 if i % 2 == 0 else 5.5, 1.2),
                p2=Point3D(4.0, 3.0, 1.2),
            )
            lk.baseline_rssi = -60.0
            engine.add_or_update_link(lk)

        # Enforce MAX_FINGERPRINTS cap
        for idx in range(_const.MAX_FINGERPRINTS + 25):
            engine.add_fingerprint(
                RFFingerprint(x=1.0, y=1.0, vector={"d0->r0": 1.0 + (idx % 3) * 0.1})
            )
        self.assertEqual(engine.fingerprint_count, _const.MAX_FINGERPRINTS)

        # Clear and add one distinct fingerprint at (6.25, 4.75)
        engine.clear_fingerprints()
        fp_vec = {"d2->r0": 6.0, "d3->r0": 5.5}
        engine.links["d2->r0"].add_sample(-66.0, now=200.0)
        engine.links["d3->r0"].add_sample(-65.5, now=200.0)
        engine.links["d0->r0"].add_sample(-60.0, now=200.0)
        engine.links["d1->r0"].add_sample(-60.0, now=200.0)

        recon_plain = engine.reconstruct(now=201.0)
        r_idx = int(4.75 / recon_plain.resolution)
        c_idx = int(6.25 / recon_plain.resolution)
        val_plain = recon_plain.grid[r_idx][c_idx]

        engine.add_fingerprint(RFFingerprint(x=6.25, y=4.75, room_id="ku", vector=fp_vec))
        recon_hybrid = engine.reconstruct(now=201.0)
        val_hybrid = recon_hybrid.grid[r_idx][c_idx]
        self.assertGreater(val_hybrid, val_plain + 1.0)

    def test_edge_walk_interpolates_and_persists_fingerprints(self) -> None:
        """start_edge_walk + stop_edge_walk distributes interpolated fingerprints along wall."""
        hass = _FakeHass()
        entry = types.SimpleNamespace(
            entry_id="walk_test",
            data={
                "floor_width": 8.0,
                "floor_height": 6.0,
                "enable_ble": False,
                "enable_wifi_entities": False,
                "devices": {
                    "sensor.plug_1": {
                        "name": "Plug 1",
                        "protocol": "wifi",
                        "receiver_id": "router_main",
                        "x": 1.0,
                        "y": 1.0,
                        "z": 0.4,
                    }
                },
            },
            options={},
        )

        async def _run() -> None:
            coord = _coordinator_mod.RFPresenceCoordinator(hass, entry)
            lnk = coord.engine.links["sensor.plug_1->router_main"]
            lnk.baseline_rssi = -60.0
            lnk.add_sample(-67.0, now=time.monotonic())

            await coord.async_start_edge_walk("wz", 0.5, 0.5, 4.5, 0.5)
            # Inject 6 synthetic samples to simulate walking without waiting 6 real seconds
            for i in range(6):
                coord._edge_walk_state["samples"].append(
                    ({"sensor.plug_1->router_main": 5.0 + i * 0.2}, -58.0)
                )
            added = await coord.async_stop_edge_walk()
            self.assertEqual(added, 6)
            self.assertEqual(coord.engine.fingerprint_count, 6)
            self.assertIsNone(coord._edge_walk_state)

            # Verify fingerprints persist across restart
            await coord.async_shutdown()
            coord2 = _coordinator_mod.RFPresenceCoordinator(hass, entry)
            await coord2.async_load_storage()
            self.assertEqual(coord2.engine.fingerprint_count, 6)

            removed = await coord2.async_clear_fingerprints()
            self.assertEqual(removed, 6)
            self.assertEqual(coord2.engine.fingerprint_count, 0)
            await coord2.async_shutdown()

        _run_async(_run())


class TestAutoPlaceAndPayloadBudget(unittest.TestCase):
    """Tests for auto_place_from_areas, < 16 KB payload with fingerprints, and 3D panel JS."""

    def setUp(self) -> None:
        reset_stub_storage()

    def test_auto_place_from_areas_and_names(self) -> None:
        """Devices & Shelly BLE scanners must auto-place into matching rooms with realistic Z."""
        hass = _FakeHass()
        entry = types.SimpleNamespace(
            entry_id="auto_place",
            data={
                "floor_width": 10.0,
                "floor_height": 8.0,
                "enable_ble": False,
                "enable_wifi_entities": False,
                "receivers": {
                    "router_main": {"name": "WLAN Router", "x": 5.0, "y": 4.0, "z": 1.2},
                    "AA:BB:CC:00:00:01": {"name": "Shelly Proxy Bad BZ", "x": 5.0, "y": 4.0, "z": 1.0},
                },
                "devices": {
                    "dev_bz": {"name": "Thermometer BZ", "protocol": "ble", "receiver_id": "router_main"},
                    "dev_az": {"name": "XP-4200 Printer", "protocol": "wifi", "receiver_id": "router_main"},
                    "dev_wz": {"name": "Govee Stehlampe WZ", "protocol": "ble", "receiver_id": "router_main"},
                    "dev_ku": {"name": "Fridge Sensor", "protocol": "ble", "receiver_id": "router_main"},
                    "dev_trv": {"name": "Shelly BLU TRV Bad", "protocol": "ble", "receiver_id": "router_main"},
                    "dev_pstrip": {"name": "Shelly PStrip AZ", "protocol": "wifi", "receiver_id": "router_main"},
                },
            },
            options={},
        )

        async def _run() -> None:
            coord = _coordinator_mod.RFPresenceCoordinator(hass, entry)
            coord.rooms = [
                RoomZone("badezimmer", "Badezimmer", 0.0, 0.0, 3.0, 3.0),
                RoomZone("arbeitszimmer", "Arbeitszimmer", 3.0, 0.0, 7.0, 4.0),
                RoomZone("wohnzimmer", "Wohnzimmer", 0.0, 3.0, 5.0, 8.0),
                RoomZone("kueche", "Küche", 5.0, 4.0, 10.0, 8.0),
            ]
            res = await coord.async_auto_place_from_areas()
            self.assertEqual(res["matched_devices"], 6)
            self.assertGreaterEqual(res["matched_receivers"], 1)

            # Verify each device landed inside its matching room with expected Z height
            self.assertTrue(coord.rooms[0].contains(coord.devices["dev_bz"]["x"], coord.devices["dev_bz"]["y"]))
            self.assertAlmostEqual(coord.devices["dev_bz"]["z"], 1.40, places=2)

            self.assertTrue(coord.rooms[1].contains(coord.devices["dev_az"]["x"], coord.devices["dev_az"]["y"]))
            self.assertAlmostEqual(coord.devices["dev_az"]["z"], 1.10, places=2)

            self.assertTrue(coord.rooms[2].contains(coord.devices["dev_wz"]["x"], coord.devices["dev_wz"]["y"]))
            self.assertAlmostEqual(coord.devices["dev_wz"]["z"], 1.65, places=2)

            self.assertTrue(coord.rooms[3].contains(coord.devices["dev_ku"]["x"], coord.devices["dev_ku"]["y"]))
            self.assertAlmostEqual(coord.devices["dev_ku"]["z"], 1.10, places=2)

            self.assertAlmostEqual(coord.devices["dev_trv"]["z"], 0.55, places=2)
            self.assertAlmostEqual(coord.devices["dev_pstrip"]["z"], 0.35, places=2)
            self.assertTrue(
                coord.rooms[0].contains(
                    coord.receivers["AA:BB:CC:00:00:01"]["x"],
                    coord.receivers["AA:BB:CC:00:00:01"]["y"],
                )
            )
            await coord.async_shutdown()

        _run_async(_run())

    def test_v21_worst_case_payload_with_80_fingerprints_stays_under_16kb(self) -> None:
        """Even with 80 long-ID links + 16 targets + 80 fingerprints + 8 rooms w/ doors, JSON stays < 16 KB."""
        hass = _FakeHass()
        devices = {
            f"sensor.shelly_plug_room_{i}_rssi": {
                "name": f"Shelly Plug Room {i}",
                "protocol": "wifi" if i < 40 else "ble",
                "receiver_id": "router_main" if i < 40 else "ha_bluetooth",
                "x": float(i % 10),
                "y": float(i // 10),
                "z": 0.45 if i % 2 == 0 else 1.5,
            }
            for i in range(_const.MAX_LINKS)
        }
        entry = types.SimpleNamespace(
            entry_id="size_v21",
            data={
                "floor_width": 12.0,
                "floor_height": 10.0,
                "enable_ble": False,
                "enable_wifi_entities": False,
                "devices": devices,
            },
            options={},
        )
        coord = _coordinator_mod.RFPresenceCoordinator(hass, entry)
        coord.rooms = [
            RoomZone(
                f"room_{i}",
                f"Zimmer {i}",
                (i % 4) * 3.0,
                (i // 4) * 5.0,
                (i % 4) * 3.0 + 3.0,
                (i // 4) * 5.0 + 5.0,
                open_edges=["east"] if i % 2 == 0 else ["west"],
                doors=[{"edge": "south" if i < 4 else "north", "pos": 0.5, "width": 0.9}],
            )
            for i in range(8)
        ]
        for i in range(80):
            coord.engine.add_fingerprint(
                RFFingerprint(
                    x=(i % 10) + 0.5,
                    y=(i // 10) + 0.5,
                    vector={"sensor.shelly_plug_room_0_rssi->router_main": 3.0},
                )
            )
        targets = [
            _tracker.TrackedTarget(
                track_id=i + 1,
                x=1.0 + (i % 4) * 2.0,
                y=1.0 + (i // 4) * 2.0,
                human_score=0.9,
                room_id="room_0",
                room_name="Zimmer 0",
            )
            for i in range(_const.TRACKER_MAX_TRACKS)
        ]
        for t in targets:
            for step in range(_const.TRACKER_TRAIL_MAXLEN):
                t.trail.append((t.x + step * 0.05, t.y + step * 0.05))

        async def _run() -> None:
            with (
                patch.object(coord.tracker, "update", return_value=targets),
                patch("time.monotonic", return_value=101.0),
            ):
                data = await coord._async_update_data()
            coord.data = data
            sensor = _sensor_mod.RFTargetsDetailSensor(coord, entry)
            payload_bytes = len(json.dumps(sensor.extra_state_attributes))
            self.assertLess(
                payload_bytes,
                16384,
                f"v2.1.0 worst-case payload exceeded 16 KB: {payload_bytes} bytes",
            )
            # Confirm all new attributes are excluded from HA Recorder
            self.assertTrue(
                {
                    "fingerprint_count",
                    "edge_walk_active",
                    "fingerprints_xy",
                    "mask_outside_rooms",
                }.issubset(_sensor_mod.RFTargetsDetailSensor._unrecorded_attributes)
            )
            await coord.async_shutdown()

        _run_async(_run())

    def test_walk_calibration_rejects_nonfinite_and_caps_vector_size(self) -> None:
        """Non-finite coordinates must be rejected and fingerprint vectors capped at 24 links."""
        engine = RTIEngine(floor_width=8.0, floor_height=6.0, resolution=0.5)
        engine.add_fingerprint(
            RFFingerprint(x=float("nan"), y=2.0, vector={"l1": 3.0})
        )
        self.assertEqual(engine.fingerprint_count, 0)

        big_vec = {f"link_{i}": float(i + 1) * 0.2 for i in range(60)}
        engine.add_fingerprint(RFFingerprint(x=2.0, y=3.0, vector=big_vec))
        self.assertEqual(engine.fingerprint_count, 1)
        exported = engine.export_fingerprints()
        self.assertLessEqual(len(exported[0]["vector"]), 24)

    def test_frontend_3d_panel_and_cad_controls_present(self) -> None:
        """Frontend JS must define both rf-presence-3d-panel and rf-presence-3d-card."""
        js_path = (
            Path(__file__).resolve().parents[1]
            / "custom_components"
            / "rf_presence_sense"
            / "frontend"
            / "rf-presence-3d-card.js"
        )
        content = js_path.read_text(encoding="utf-8")
        for token in (
            "rf-presence-3d-panel",
            "rf-presence-3d-card",
            "hass-toggle-menu",
            "record_calibration_point",
            "start_edge_walk",
            "stop_edge_walk",
            "clear_fingerprints",
            "auto_place_from_areas",
            "_velYaw",
            "open_edges",
            "doors",
        ):
            self.assertIn(token, content)
        self.assertEqual(content.count("{"), content.count("}"))
        self.assertEqual(content.count("("), content.count(")"))
        self.assertEqual(content.count("["), content.count("]"))


if __name__ == "__main__":
    unittest.main()
