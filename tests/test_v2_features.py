"""Comprehensive unit tests for RF Presence Sense v2.0.0 features."""

from __future__ import annotations

import asyncio
from pathlib import Path
import struct
import time
import types
import unittest

from _loader import load_core_modules, load_submodule, reset_stub_storage

_const, _rti, _tracker = load_core_modules()
_collector_mod = load_submodule("signal_collector", with_ha_stubs=True)
_storage_mod = load_submodule("storage", with_ha_stubs=True)
_coordinator_mod = load_submodule("coordinator", with_ha_stubs=True)
_camera_mod = load_submodule("camera", with_ha_stubs=True)
_sensor_mod = load_submodule("sensor", with_ha_stubs=True)
_binary_sensor_mod = load_submodule("binary_sensor", with_ha_stubs=True)
_button_mod = load_submodule("button", with_ha_stubs=True)
_number_mod = load_submodule("number", with_ha_stubs=True)
_diag_mod = load_submodule("diagnostics", with_ha_stubs=True)

Point3D = _rti.Point3D
RFLink = _rti.RFLink
RTIEngine = _rti.RTIEngine
RoomZone = _tracker.RoomZone
TrackedTarget = _tracker.TrackedTarget


class _SocketlessEventLoop(asyncio.BaseEventLoop):
    """Minimal socket-free asyncio loop for sandboxed Windows unit tests."""

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


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def async_fire(self, event_type: str, event_data: dict) -> None:
        self.events.append((event_type, dict(event_data)))


class _FakeHass:
    def __init__(self) -> None:
        self.data: dict = {}
        self.bus = _FakeBus()
        self._states: dict[str, Any] = {}
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


class TestPersistenceAndDynamicRooms(unittest.TestCase):
    """Tests for storage.py roundtrip and dynamic room binary sensor lifecycle."""

    def setUp(self) -> None:
        reset_stub_storage()

    def test_persistence_roundtrip_across_restart(self) -> None:
        """Positions, polygon rooms, calibrated baselines, and live tuning must survive reload."""
        hass = _FakeHass()
        entry = types.SimpleNamespace(
            entry_id="persist_1",
            data={
                "floor_width": 10.0,
                "floor_height": 8.0,
                "enable_ble": False,
                "enable_wifi_entities": False,
                "devices": {
                    "sensor.plug_a": {
                        "name": "Plug A",
                        "protocol": "wifi",
                        "receiver_id": "router_main",
                    }
                },
            },
            options={},
        )

        async def _run() -> None:
            coord1 = _coordinator_mod.RFPresenceCoordinator(hass, entry)
            await coord1.async_set_device_position("sensor.plug_a", 2.4, 5.6, 0.35)
            await coord1.async_set_device_position("router_main", 5.2, 4.1, 1.9)
            await coord1.async_define_room(
                "l_room",
                "Wohnküche",
                0.0,
                0.0,
                6.0,
                6.0,
                polygon=[[0.0, 0.0], [6.0, 0.0], [6.0, 3.0], [3.0, 3.0], [3.0, 6.0], [0.0, 6.0]],
            )
            # Simulate calibrated baseline on link
            link = coord1.engine.links["sensor.plug_a->router_main"]
            link.baseline_rssi = -63.5
            link.baseline_std = 0.42
            coord1.set_sensitivity(1.6)
            coord1.set_calibration_duration(35)
            await coord1.async_save_storage()
            coord1.async_shutdown_collector()

            # Simulate fresh Home Assistant restart with new coordinator
            coord2 = _coordinator_mod.RFPresenceCoordinator(hass, entry)
            await coord2.async_load_storage()

            self.assertAlmostEqual(coord2.devices["sensor.plug_a"]["x"], 2.4, places=2)
            self.assertAlmostEqual(coord2.devices["sensor.plug_a"]["y"], 5.6, places=2)
            self.assertAlmostEqual(coord2.devices["sensor.plug_a"]["z"], 0.35, places=2)
            self.assertAlmostEqual(coord2.receivers["router_main"]["z"], 1.9, places=2)
            self.assertAlmostEqual(coord2.sensitivity, 1.6, places=2)
            self.assertEqual(coord2.calibration_duration, 35)

            room_map = {r.room_id: r for r in coord2.rooms}
            self.assertIn("l_room", room_map)
            self.assertIsNotNone(room_map["l_room"].polygon)
            self.assertTrue(room_map["l_room"].contains(1.5, 4.5))
            self.assertFalse(room_map["l_room"].contains(4.5, 4.5))  # L-notch cutout

            restored_link = coord2.engine.links["sensor.plug_a->router_main"]
            self.assertAlmostEqual(restored_link.baseline_rssi, -63.5, places=2)
            self.assertAlmostEqual(restored_link.baseline_std, 0.42, places=2)
            coord2.async_shutdown_collector()

        _run_async(_run())

    def test_dynamic_room_binary_sensors_added_and_deleted(self) -> None:
        """Defining a new room must spawn 3 binary sensors; deleting marks them unavailable."""
        hass = _FakeHass()
        entry = types.SimpleNamespace(
            entry_id="dyn_rooms",
            data={"enable_ble": False, "enable_wifi_entities": False},
            options={},
            async_on_unload=lambda cb: None,
        )
        coord = _coordinator_mod.RFPresenceCoordinator(hass, entry)
        hass.data[_const.DOMAIN] = {entry.entry_id: coord}

        added: list = []

        async def _run() -> None:
            await _binary_sensor_mod.async_setup_entry(
                hass, entry, lambda ents: added.extend(ents)
            )
            initial_count = len(added)
            # 2 global + 3 default wohnraum = 5
            self.assertEqual(initial_count, 5)

            await coord.async_define_room("kueche", "Küche", 4.0, 0.0, 8.0, 6.0)
            self.assertEqual(len(added), initial_count + 3)

            kueche_sensors = [
                e for e in added if getattr(e, "_room_id", None) == "kueche"
            ]
            self.assertEqual(len(kueche_sensors), 3)
            self.assertTrue(all(s.available for s in kueche_sensors))

            deleted = await coord.async_delete_room("kueche")
            self.assertTrue(deleted)
            self.assertTrue(all(not s.available for s in kueche_sensors))
            coord.async_shutdown_collector()

        _run_async(_run())


class TestAccuracyAndEvents(unittest.TestCase):
    """Tests for 3D voxel height interpolation, presence-aware baseline, Multi-AP, and events."""

    def test_voxel_accurate_3d_height_interpolation(self) -> None:
        """A slanted link from z=0.3m to z=2.1m must split into low_cov near p1 and high_cov near p2."""
        engine = RTIEngine(floor_width=8.0, floor_height=6.0, resolution=0.5)
        link = RFLink(
            link_id="plug->router",
            device_id="plug",
            receiver_id="router",
            protocol="wifi",
            p1=Point3D(0.5, 3.0, 0.3),  # Low plug end (<0.9m)
            p2=Point3D(7.5, 3.0, 2.1),  # High router end (>=0.9m)
        )
        link.baseline_rssi = -60.0
        link.add_sample(-68.0, now=100.0)
        engine.add_or_update_link(link)
        recon = engine.reconstruct(now=101.0)

        row_mid = int(3.0 / recon.resolution)
        col_near_plug = int(1.0 / recon.resolution)
        col_near_router = int(6.5 / recon.resolution)

        # Near the low plug: z ~ 0.43m < 0.9m -> low coverage & low energy
        self.assertGreater(recon.low_coverage_grid[row_mid][col_near_plug], 0.0)
        self.assertEqual(recon.high_coverage_grid[row_mid][col_near_plug], 0.0)
        self.assertGreater(recon.low_layer_grid[row_mid][col_near_plug], 0.0)

        # Near the high router: z ~ 1.84m >= 0.9m -> high coverage & high energy
        self.assertGreater(recon.high_coverage_grid[row_mid][col_near_router], 0.0)
        self.assertEqual(recon.low_coverage_grid[row_mid][col_near_router], 0.0)
        self.assertGreater(recon.high_layer_grid[row_mid][col_near_router], 0.0)

    def test_presence_aware_baseline_freezes_drift(self) -> None:
        """When freeze_baseline=True, quiet readings must NOT shift baseline_rssi."""
        link = RFLink(
            link_id="d->r",
            device_id="d",
            receiver_id="r",
            protocol="wifi",
            p1=Point3D(1.0, 1.0, 1.0),
            p2=Point3D(5.0, 5.0, 1.0),
        )
        link.baseline_rssi = -60.0
        for i in range(20):
            link.add_sample(-60.9, now=10.0 + i, freeze_baseline=True)
        self.assertEqual(link.baseline_rssi, -60.0)

        # When unfrozen, small ambient deviation (< 1.2 dB freeze threshold) drifts slowly toward -60.9
        for i in range(20):
            link.add_sample(-60.9, now=40.0 + i, freeze_baseline=False)
        self.assertLess(link.baseline_rssi, -60.15)

    def test_protocol_specific_stale_timeouts(self) -> None:
        """Wi-Fi links stay active up to 600s; BLE links go stale after 180s."""
        wifi_link = RFLink(
            link_id="w->r",
            device_id="w",
            receiver_id="r",
            protocol="wifi",
            p1=Point3D(1.0, 1.0, 1.0),
            p2=Point3D(5.0, 5.0, 1.0),
        )
        ble_link = RFLink(
            link_id="b->h",
            device_id="b",
            receiver_id="h",
            protocol="ble",
            p1=Point3D(1.0, 1.0, 1.0),
            p2=Point3D(5.0, 5.0, 1.0),
        )
        wifi_link.baseline_rssi = -60.0
        ble_link.baseline_rssi = -60.0
        wifi_link.add_sample(-66.0, now=100.0)
        ble_link.add_sample(-66.0, now=100.0)

        # At t=350 (250s elapsed): Wi-Fi still perturbed, BLE stale
        w_pert, _, _ = wifi_link.compute_perturbation(now=350.0)
        b_pert, _, _ = ble_link.compute_perturbation(now=350.0)
        self.assertGreater(w_pert, 4.0)
        self.assertEqual(b_pert, 0.0)

    def test_dynamic_multi_ap_and_polygon_room_and_events(self) -> None:
        """Verify dynamic AP extraction, L-polygon containment, and HA room-change events."""
        state = types.SimpleNamespace(
            entity_id="device_tracker.tv",
            state="home",
            attributes={"rssi": -64, "ap_mac": "AA:11:22:33:44:55"},
        )
        self.assertEqual(
            _collector_mod.extract_receiver_from_state(state), "AA:11:22:33:44:55"
        )

        hass = _FakeHass()
        entry = types.SimpleNamespace(
            entry_id="evt_test",
            data={"enable_ble": False, "enable_wifi_entities": False},
            options={},
        )
        coord = _coordinator_mod.RFPresenceCoordinator(hass, entry)

        t1_room_a = TrackedTarget(
            track_id=1, x=2.0, y=2.0, human_score=0.9, room_id="wz", room_name="Wohnzimmer"
        )
        t1_room_b = TrackedTarget(
            track_id=1, x=6.0, y=2.0, human_score=0.9, room_id="ku", room_name="Küche"
        )

        # Tick 1: Target detected
        coord._fire_target_events([t1_room_a])
        self.assertEqual(hass.bus.events[-1][0], _const.EVENT_TARGET_DETECTED)

        # Tick 2: Same room -> no new event
        count_before = len(hass.bus.events)
        coord._fire_target_events([t1_room_a])
        self.assertEqual(len(hass.bus.events), count_before)

        # Tick 3a: First tick in new room -> debounced, no room_changed yet
        count_before = len(hass.bus.events)
        coord._fire_target_events([t1_room_b])
        self.assertEqual(len(hass.bus.events), count_before)

        # Tick 3b: Second consecutive tick in new room -> room_changed fires
        coord._fire_target_events([t1_room_b])
        self.assertEqual(hass.bus.events[-1][0], _const.EVENT_ROOM_CHANGED)
        self.assertEqual(hass.bus.events[-1][1]["from_room_id"], "wz")
        self.assertEqual(hass.bus.events[-1][1]["to_room_id"], "ku")

        # Tick 4: Target lost
        coord._fire_target_events([])
        self.assertEqual(hass.bus.events[-1][0], _const.EVENT_TARGET_LOST)
        coord.async_shutdown_collector()


class TestLiveControls3DMapAndDiagnostics(unittest.TestCase):
    """Tests for 3D Live Map PNG, live number/button controls, diagnostics, and 3D card asset."""

    def test_3d_live_map_png_with_trails_polygon_and_calibrating(self) -> None:
        """3D isometric PNG renderer must handle 640x480, polygon rooms, trails, and umlauts."""
        engine = RTIEngine(floor_width=8.0, floor_height=6.0, resolution=0.5)
        link = RFLink(
            link_id="d1->r1",
            device_id="d1",
            receiver_id="r1",
            protocol="ble",
            p1=Point3D(0.5, 0.5, 0.35),
            p2=Point3D(7.5, 5.5, 1.9),
        )
        link.baseline_rssi = -60.0
        link.add_sample(-68.0, now=100.0)
        engine.add_or_update_link(link)
        recon = engine.reconstruct(now=101.0)

        human = TrackedTarget(track_id=1, x=3.2, y=2.8, vx=0.4, vy=0.2, human_score=0.92, confidence=0.91)
        human.trail.append((2.4, 2.2))
        human.trail.append((2.8, 2.5))
        human.trail.append((3.2, 2.8))
        cat = TrackedTarget(track_id=2, x=6.0, y=1.5, human_score=0.15, confidence=0.84)

        fake_coord = types.SimpleNamespace(
            floor_width=8.0,
            floor_height=6.0,
            engine=engine,
            rooms=[
                RoomZone(
                    "kueche",
                    "Küche",
                    0.0,
                    0.0,
                    8.0,
                    6.0,
                    polygon=[(0.0, 0.0), (8.0, 0.0), (8.0, 3.0), (4.0, 3.0), (4.0, 6.0), (0.0, 6.0)],
                )
            ],
            devices={"d1": {"x": 0.5, "y": 0.5, "z": 0.35, "protocol": "ble"}},
            receivers={"r1": {"x": 7.5, "y": 5.5, "z": 1.9, "name": "Router"}},
            last_reconstruction=recon,
            last_targets=[human, cat],
            recon_seq=1,
            _calibrating=True,
        )

        png = _camera_mod.render_heatmap_png(fake_coord, w_px=640, h_px=480)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertTrue(png.endswith(b"IEND\xaeB`\x82"))
        w, h = struct.unpack("!II", png[16:24])
        self.assertEqual((w, h), (640, 480))

    def test_live_number_slider_and_diagnostics_and_unrecorded_3d_attrs(self) -> None:
        """Verify live sensitivity number, diagnostics redaction, and unrecorded 3D card attributes."""
        hass = _FakeHass()
        entry = types.SimpleNamespace(
            entry_id="live_ctrl",
            data={
                "enable_ble": False,
                "enable_wifi_entities": False,
                "devices": {
                    "AA:BB:CC:DD:EE:FF": {
                        "name": "BLE Plug",
                        "protocol": "ble",
                        "receiver_id": "ha_bluetooth",
                        "x": 2.0,
                        "y": 2.0,
                        "z": 0.4,
                    }
                },
            },
            options={},
        )
        coord = _coordinator_mod.RFPresenceCoordinator(hass, entry)
        hass.data[_const.DOMAIN] = {entry.entry_id: coord}

        async def _run() -> None:
            sens_num = _number_mod.RFSensitivityNumber(coord, entry)
            await sens_num.async_set_native_value(1.8)
            self.assertAlmostEqual(coord.sensitivity, 1.8, places=2)
            self.assertAlmostEqual(coord.engine.sensitivity, 1.8, places=2)

            dur_num = _number_mod.RFCalibrationDurationNumber(coord, entry)
            await dur_num.async_set_native_value(45.0)
            self.assertEqual(coord.calibration_duration, 45)

            reset_btn = _button_mod.RFResetBaselineButton(coord, entry)
            await reset_btn.async_press()

            diag = await _diag_mod.async_get_config_entry_diagnostics(hass, entry)
            self.assertEqual(diag["version"], _const.VERSION)
            self.assertEqual(diag["tuning"]["sensitivity"], 1.8)
            # MAC must be redacted in diagnostics
            link_ids = [item["link_id"] for item in diag["links"]]
            self.assertTrue(any("AA:BB:**:**:FF" in lid for lid in link_ids))
            coord.async_shutdown_collector()

        _run_async(_run())

        # Ensure all 3D card attributes are excluded from HA Recorder
        required_unrecorded = {
            "targets",
            "rooms",
            "rooms_geometry",
            "geometry",
            "calibrating",
            "devices",
            "receivers",
            "active_links_3d",
            "heatmap_peaks",
        }
        self.assertTrue(
            required_unrecorded.issubset(
                _sensor_mod.RFTargetsDetailSensor._unrecorded_attributes
            )
        )

    def test_frontend_3d_card_asset_present(self) -> None:
        """Interactive 3D Lovelace Card JS must exist and define rf-presence-3d-card."""
        js_path = (
            Path(__file__).resolve().parents[1]
            / "custom_components"
            / "rf_presence_sense"
            / "frontend"
            / "rf-presence-3d-card.js"
        )
        self.assertTrue(js_path.exists())
        content = js_path.read_text(encoding="utf-8")
        self.assertIn("rf-presence-3d-card", content)
        self.assertIn("set_device_position", content)
        self.assertIn("define_room", content)
        self.assertIn("delete_room", content)


if __name__ == "__main__":
    unittest.main()
