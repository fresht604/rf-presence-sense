"""Stability, OOM-protection, caching, rate-limiting, and recorder-safety tests."""

from __future__ import annotations

import asyncio
from collections import deque
import struct
import threading
import time
import types
import unittest
from unittest.mock import patch

from _loader import load_core_modules, load_submodule

_const, _rti, _tracker = load_core_modules()
_collector_mod = load_submodule("signal_collector", with_ha_stubs=True)
_coordinator_mod = load_submodule("coordinator", with_ha_stubs=True)
_camera_mod = load_submodule("camera", with_ha_stubs=True)
_sensor_mod = load_submodule("sensor", with_ha_stubs=True)
_binary_sensor_mod = load_submodule("binary_sensor", with_ha_stubs=True)

Point3D = _rti.Point3D
RFLink = _rti.RFLink
RTIEngine = _rti.RTIEngine
RTIReconstruction = _rti.RTIReconstruction
MAX_VOXELS = _const.MAX_VOXELS
MAX_LINKS = _const.MAX_LINKS
MAX_CALIBRATION_SAMPLES = _const.MAX_CALIBRATION_SAMPLES
MAX_REPORTED_PERTURBATIONS = _const.MAX_REPORTED_PERTURBATIONS
TRACKER_MAX_TRACKS = _const.TRACKER_MAX_TRACKS


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
    """Run an asyncio coroutine without requiring loopback sockets."""
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
    """Minimal fake HomeAssistant with async_add_executor_job tracking."""

    def __init__(self) -> None:
        self.executor_calls: list[str] = []
        self.states = types.SimpleNamespace(get=lambda _eid: None, async_all=lambda: [])
        self.services = types.SimpleNamespace(
            async_call=self._noop_async,
            has_service=lambda *_a: False,
            async_remove=lambda *_a: None,
        )

    async def _noop_async(self, *_args, **_kwargs) -> None:
        return None

    async def async_add_executor_job(self, target, *args):
        self.executor_calls.append(getattr(target, "__name__", str(target)))
        return target(*args)


class TestOOMAndResourceLimits(unittest.TestCase):
    """Tests for memory bounds, voxel caps, link caps, and calibration caps."""

    def test_huge_floorplan_auto_upscales_and_has_no_cov_x(self) -> None:
        """A 50m x 50m floorplan at 0.25m (40,000 voxels) must cap to <= MAX_VOXELS without _cov_x."""
        engine = RTIEngine(floor_width=50.0, floor_height=50.0, resolution=0.25)
        self.assertLessEqual(engine.num_voxels, MAX_VOXELS)
        self.assertGreater(engine.resolution, 1.5)
        self.assertFalse(
            hasattr(engine, "_cov_x"),
            "N x N spatial covariance matrix must NOT be stored permanently in RAM",
        )
        # Ensure reconstruction runs cleanly on the capped grid
        link = RFLink(
            link_id="d1->r1",
            device_id="d1",
            receiver_id="r1",
            protocol="wifi",
            p1=Point3D(2.0, 2.0, 1.2),
            p2=Point3D(25.0, 25.0, 1.2),
        )
        link.baseline_rssi = -60.0
        link.add_sample(-68.0, now=100.0)
        self.assertTrue(engine.add_or_update_link(link))
        recon = engine.reconstruct(now=101.0)
        self.assertEqual(recon.width * recon.height, engine.num_voxels)
        self.assertEqual(recon.active_links, 1)

    def test_max_links_cap_enforced(self) -> None:
        """Engine must accept at most MAX_LINKS distinct links while allowing updates."""
        engine = RTIEngine(floor_width=10.0, floor_height=8.0, resolution=0.5)
        accepted = 0
        for i in range(MAX_LINKS + 40):
            link = RFLink(
                link_id=f"dev_{i}->router",
                device_id=f"dev_{i}",
                receiver_id="router",
                protocol="wifi",
                p1=Point3D((i % 10) * 0.9, (i // 10) * 0.5, 1.0),
                p2=Point3D(5.0, 4.0, 1.2),
            )
            if engine.add_or_update_link(link):
                accepted += 1

        self.assertEqual(accepted, MAX_LINKS)
        self.assertEqual(len(engine.links), MAX_LINKS)

        # Updating an existing link must still succeed even at capacity
        updated = RFLink(
            link_id="dev_0->router",
            device_id="dev_0",
            receiver_id="router",
            protocol="wifi",
            p1=Point3D(1.0, 1.0, 1.5),
            p2=Point3D(5.0, 4.0, 1.2),
        )
        self.assertTrue(engine.add_or_update_link(updated))
        self.assertEqual(len(engine.links), MAX_LINKS)

        # Removing a link frees capacity for a new one
        engine.remove_link("dev_0->router")
        replacement = RFLink(
            link_id="dev_new->router",
            device_id="dev_new",
            receiver_id="router",
            protocol="wifi",
            p1=Point3D(2.0, 2.0, 1.0),
            p2=Point3D(5.0, 4.0, 1.2),
        )
        self.assertTrue(engine.add_or_update_link(replacement))
        self.assertEqual(len(engine.links), MAX_LINKS)

    def test_calibration_samples_bounded_deque(self) -> None:
        """calibration_samples must be a bounded deque(maxlen=MAX_CALIBRATION_SAMPLES)."""
        link = RFLink(
            link_id="d1->r1",
            device_id="d1",
            receiver_id="r1",
            protocol="ble",
            p1=Point3D(1.0, 1.0, 1.0),
            p2=Point3D(5.0, 4.0, 1.0),
        )
        self.assertIsInstance(link.calibration_samples, deque)
        self.assertEqual(link.calibration_samples.maxlen, MAX_CALIBRATION_SAMPLES)

        for i in range(500):
            link.add_sample(-65.0 + (i % 3) * 0.5, now=10.0 + i * 0.1, calibrating=True)

        self.assertEqual(len(link.calibration_samples), MAX_CALIBRATION_SAMPLES)
        link.finalize_calibration()
        self.assertEqual(len(link.calibration_samples), 0)
        self.assertIsNotNone(link.baseline_rssi)
        assert link.baseline_rssi is not None
        self.assertAlmostEqual(link.baseline_rssi, -64.5, delta=0.5)

    def test_tracker_max_tracks_and_overflow_safety(self) -> None:
        """Tracker must cap active tracks to TRACKER_MAX_TRACKS and survive extreme peaks."""
        cols, rows = 30, 30
        grid = [[0.0] * cols for _ in range(rows)]
        # Create 25 well-separated peaks (every 6 cells = 3.0m apart)
        for r in range(2, rows - 2, 6):
            for c in range(2, cols - 2, 6):
                grid[r][c] = 5.0
        # Add one extreme outlier peak to test sigmoid OverflowError immunity
        grid[2][2] = 1e6
        zeros = [[0.0] * cols for _ in range(rows)]

        recon = RTIReconstruction(
            width=cols,
            height=rows,
            resolution=0.5,
            grid=grid,
            low_layer_grid=zeros,
            high_layer_grid=zeros,
            low_coverage_grid=zeros,
            high_coverage_grid=zeros,
            active_links=10,
            total_links=10,
            link_perturbations={},
        )
        tracker = _tracker.MultiTargetTracker()
        tracker.update(recon, dt=2.0)
        targets = tracker.update(recon, dt=2.0)
        self.assertLessEqual(len(tracker._tracks), TRACKER_MAX_TRACKS)
        self.assertLessEqual(len(targets), TRACKER_MAX_TRACKS)

    def test_concurrent_sample_ingestion_and_reconstruction_thread_safe(self) -> None:
        """Concurrent RSSI ingestion on event loop + reconstruct in executor must not race."""
        engine = RTIEngine(floor_width=8.0, floor_height=6.0, resolution=0.5)
        for i in range(12):
            link = RFLink(
                link_id=f"d{i}->r0",
                device_id=f"d{i}",
                receiver_id="r0",
                protocol="ble",
                p1=Point3D((i % 6) * 1.4 + 0.3, (i // 6) * 5.0 + 0.5, 1.0),
                p2=Point3D(4.0, 3.0, 1.0),
            )
            link.baseline_rssi = -60.0
            engine.add_or_update_link(link)

        errors: list[Exception] = []
        stop_event = threading.Event()

        def writer() -> None:
            t = 100.0
            try:
                while not stop_event.is_set():
                    for i in range(12):
                        engine.ingest_sample(f"d{i}->r0", -62.0 - (i % 4), now=t)
                    t += 0.1
            except Exception as err:  # noqa: BLE001
                errors.append(err)

        def reader() -> None:
            try:
                for step in range(15):
                    engine.reconstruct(now=100.0 + step)
            except Exception as err:  # noqa: BLE001
                errors.append(err)
            finally:
                stop_event.set()

        t_w = threading.Thread(target=writer)
        t_r = threading.Thread(target=reader)
        t_w.start()
        t_r.start()
        t_w.join(timeout=10)
        t_r.join(timeout=10)
        self.assertEqual(errors, [])


class TestSignalCollectorAndFilters(unittest.TestCase):
    """Tests for stationary device filtering and BLE callback rate limiting."""

    def test_is_likely_stationary_filter(self) -> None:
        """Reject unnamed random MACs and mobile devices; accept stationary IoT devices."""
        fn = _collector_mod.is_likely_stationary

        # Should reject
        self.assertFalse(fn("", "AA:BB:CC:DD:EE:FF"))
        self.assertFalse(fn("AA:BB:CC:DD:EE:FF", "AA:BB:CC:DD:EE:FF"))
        self.assertFalse(fn("AA-BB-CC-DD-EE-FF", "aa:bb:cc:dd:ee:ff"))
        self.assertFalse(fn("Tims iPhone", "sensor.iphone_rssi"))
        self.assertFalse(fn("Apple Watch Series 9", "11:22:33:44:55:66"))
        self.assertFalse(fn("AirPods Pro", "11:22:33:44:55:67"))
        self.assertFalse(fn("MacBook Pro", "sensor.macbook_wifi"))
        self.assertFalse(fn("Tim-PC", "sensor.tim_pc_rssi"))
        self.assertFalse(fn("Work Handy", "sensor.handy_rssi"))
        self.assertFalse(fn("Jabra Elite", "11:22:33:44:55:68"))

        # Should accept
        self.assertTrue(fn("Shelly Plug S Wohnzimmer", "sensor.shelly_plug_rssi"))
        self.assertTrue(fn("ATC_4F1A2B Temp Sensor", "A4:C1:38:4F:1A:2B"))
        self.assertTrue(fn("LG OLED TV", "sensor.lg_tv_signal"))
        self.assertTrue(fn("Hue Stehlampe", "sensor.hue_lamp_linkquality"))
        self.assertTrue(fn("SwitchBot Meter", "CC:DD:EE:11:22:33"))

    def test_ble_rate_limiting_per_link(self) -> None:
        """High-rate BLE ads for the same (MAC, source) must be throttled to >= 0.4s."""
        hass = _FakeHass()
        collector = _collector_mod.SignalCollector(
            hass=hass,
            on_sample=lambda *_a: None,
            ble_min_interval=0.4,
        )
        # First packet accepted
        self.assertTrue(collector._should_accept_ble("AA:BB:CC:00:00:01", "hci0", 10.0))
        # Rapid burst within 0.4s rejected
        self.assertFalse(collector._should_accept_ble("AA:BB:CC:00:00:01", "hci0", 10.1))
        self.assertFalse(collector._should_accept_ble("AA:BB:CC:00:00:01", "hci0", 10.39))
        # Different adapter or different MAC is independent and accepted immediately
        self.assertTrue(collector._should_accept_ble("AA:BB:CC:00:00:01", "proxy1", 10.1))
        self.assertTrue(collector._should_accept_ble("AA:BB:CC:00:00:02", "hci0", 10.1))
        # After >= 0.4s elapsed, original link is accepted again
        self.assertTrue(collector._should_accept_ble("AA:BB:CC:00:00:01", "hci0", 10.41))


class TestCameraPNGAndCaching(unittest.TestCase):
    """Tests for pure-Python PNG validity, caching by recon_seq, and executor usage."""

    def test_render_heatmap_png_valid_format(self) -> None:
        """Rendered heatmap must be a structurally valid PNG with expected dimensions."""
        engine = RTIEngine(floor_width=8.0, floor_height=6.0, resolution=0.5)
        link = RFLink(
            link_id="d1->r1",
            device_id="d1",
            receiver_id="r1",
            protocol="wifi",
            p1=Point3D(0.5, 0.5, 1.2),
            p2=Point3D(7.5, 5.5, 1.2),
        )
        link.baseline_rssi = -60.0
        link.add_sample(-68.0, now=100.0)
        engine.add_or_update_link(link)
        recon = engine.reconstruct(now=101.0)

        fake_coord = types.SimpleNamespace(
            floor_width=8.0,
            floor_height=6.0,
            engine=engine,
            rooms=[_tracker.RoomZone("r1", "Wohnzimmer", 0.0, 0.0, 8.0, 6.0)],
            devices={"d1": {"x": 0.5, "y": 0.5, "protocol": "wifi"}},
            receivers={"r1": {"x": 7.5, "y": 5.5}},
            last_reconstruction=recon,
            last_targets=[
                _tracker.TrackedTarget(
                    track_id=1, x=4.0, y=3.0, vx=0.3, vy=0.1, human_score=0.9
                )
            ],
            recon_seq=1,
        )

        png = _camera_mod.render_heatmap_png(fake_coord, w_px=320, h_px=240)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertIn(b"IHDR", png[:24])
        self.assertIn(b"IDAT", png)
        self.assertTrue(png.endswith(b"IEND\xaeB`\x82"))
        # Verify IHDR width & height bytes
        w, h = struct.unpack("!II", png[16:24])
        self.assertEqual((w, h), (320, 240))

    def test_camera_caches_png_per_recon_seq_and_uses_executor(self) -> None:
        """Repeated async_camera_image calls for same recon_seq must hit cache without re-rendering."""
        hass = _FakeHass()
        engine = RTIEngine(floor_width=6.0, floor_height=6.0, resolution=0.5)
        fake_coord = types.SimpleNamespace(
            floor_width=6.0,
            floor_height=6.0,
            engine=engine,
            rooms=[],
            devices={},
            receivers={},
            last_reconstruction=None,
            last_targets=[],
            recon_seq=1,
        )
        entry = types.SimpleNamespace(entry_id="entry_1")
        cam = _camera_mod.RFHeatmapCamera(fake_coord, entry)
        cam.hass = hass

        async def _run() -> None:
            img1 = await cam.async_camera_image()
            img2 = await cam.async_camera_image()
            self.assertIsNotNone(img1)
            self.assertIs(img1, img2)
            self.assertEqual(len(hass.executor_calls), 1)

            # Bump recon_seq -> must invalidate cache and render once more in executor
            fake_coord.recon_seq = 2
            img3 = await cam.async_camera_image()
            self.assertIsNotNone(img3)
            self.assertEqual(len(hass.executor_calls), 2)

        _run_async(_run())


class TestRecorderProtectionAndCoordinator(unittest.TestCase):
    """Tests for _unrecorded_attributes, Top-N perturbations, and coordinator lifecycle."""

    def test_unrecorded_attributes_declared_on_high_churn_entities(self) -> None:
        """High-churn JSON attributes must be excluded from HA Recorder."""
        self.assertTrue(
            {"targets", "rooms", "geometry", "calibrating"}.issubset(
                _sensor_mod.RFTargetsDetailSensor._unrecorded_attributes
            )
        )
        self.assertTrue(
            {"link_perturbations", "packets_received"}.issubset(
                _sensor_mod.RFLinkHealthSensor._unrecorded_attributes
            )
        )
        self.assertTrue(
            {"targets", "humans", "animals"}.issubset(
                _binary_sensor_mod.RFRoomOccupancyBinarySensor._unrecorded_attributes
            )
        )

    def test_coordinator_offloads_reconstruct_and_caps_perturbations(self) -> None:
        """Coordinator must run reconstruct in executor and cap reported link_perturbations."""
        hass = _FakeHass()
        devices = {
            f"sensor.dev_{i}": {
                "name": f"Plug {i}",
                "protocol": "wifi",
                "receiver_id": "router_main",
                "x": float(i % 10),
                "y": float(i // 10),
                "z": 1.0,
            }
            for i in range(35)
        }
        entry = types.SimpleNamespace(
            entry_id="test_coord",
            data={
                "floor_width": 10.0,
                "floor_height": 8.0,
                "grid_resolution": 0.5,
                "enable_ble": False,
                "enable_wifi_entities": True,
                "devices": devices,
            },
            options={},
        )
        coord = _coordinator_mod.RFPresenceCoordinator(hass, entry)
        # Constructor must NOT have blocked on rebuild_projection
        self.assertTrue(coord.engine._dirty_geometry)

        # Feed perturbed RSSI samples for all 35 links
        for i, lid in enumerate(list(coord.engine.links.keys())):
            link = coord.engine.links[lid]
            link.baseline_rssi = -60.0
            link.add_sample(-62.0 - (i % 8), now=500.0)

        async def _run() -> None:
            with patch("time.monotonic", return_value=501.0):
                data = await coord._async_update_data()
            self.assertIn("reconstruct", hass.executor_calls)
            self.assertFalse(coord.engine._dirty_geometry)
            self.assertLessEqual(
                len(data["link_perturbations"]), MAX_REPORTED_PERTURBATIONS
            )
            self.assertTrue(
                all(v > 0.1 for v in data["link_perturbations"].values())
            )
            coord.async_shutdown_collector()

        _run_async(_run())

    def test_calibration_task_cancelled_on_shutdown(self) -> None:
        """A long running calibration must be cancelled cleanly when coordinator shuts down."""
        hass = _FakeHass()
        entry = types.SimpleNamespace(
            entry_id="test_cal",
            data={"enable_ble": False, "enable_wifi_entities": False},
            options={},
        )
        coord = _coordinator_mod.RFPresenceCoordinator(hass, entry)

        async def _run() -> None:
            cal_coro = asyncio.create_task(coord.async_calibrate_empty_room(60))
            await asyncio.sleep(0.05)
            self.assertTrue(coord._calibrating)
            coord.async_shutdown_collector()
            await asyncio.wait_for(cal_coro, timeout=1.0)
            self.assertFalse(coord._calibrating)
            self.assertIsNone(coord._calibration_task)

        _run_async(_run())

    def test_worst_case_3d_state_payload_under_16kb(self) -> None:
        """At 80 links + 16 targets (15-pt trails) + 40 peaks, JSON payload must stay < 16 KB."""
        import json

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
            for i in range(MAX_LINKS)
        }
        entry = types.SimpleNamespace(
            entry_id="size_test",
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
        for i, lid in enumerate(coord.engine.links):
            lnk = coord.engine.links[lid]
            lnk.baseline_rssi = -60.0
            lnk.add_sample(-65.0 if i % 3 == 0 else -60.0, now=100.0)

        targets = [
            _tracker.TrackedTarget(
                track_id=i + 1,
                x=1.0 + (i % 4) * 2.0,
                y=1.0 + (i // 4) * 2.0,
                human_score=0.9,
                room_id="wohnraum",
                room_name="Wohnraum",
            )
            for i in range(TRACKER_MAX_TRACKS)
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
                f"3D detail sensor payload too large: {payload_bytes} bytes",
            )
            await coord.async_shutdown()
            self.assertEqual(len(coord._room_listeners), 0)

        _run_async(_run())

    def test_camera_survives_extreme_and_nonfinite_coordinates(self) -> None:
        """3D PNG renderer must clamp NaN/Inf/huge coordinates without hanging."""
        engine = RTIEngine(floor_width=10.0, floor_height=8.0, resolution=0.5)
        fake_coord = types.SimpleNamespace(
            floor_width=10.0,
            floor_height=8.0,
            engine=engine,
            rooms=[_tracker.RoomZone("r1", "Bad", -1e6, 0.0, 1e6, 8.0)],
            devices={
                "d1": {
                    "x": float("nan"),
                    "y": float("inf"),
                    "z": 999.0,
                    "protocol": "wifi",
                }
            },
            receivers={"r1": {"x": 1e9, "y": -1e9, "z": -50.0, "name": "RX"}},
            last_reconstruction=None,
            last_targets=[
                _tracker.TrackedTarget(
                    track_id=1, x=1e7, y=-1e7, vx=1e5, vy=1e5, human_score=0.9
                )
            ],
            recon_seq=1,
            _calibrating=False,
        )
        png = _camera_mod.render_heatmap_png(fake_coord, w_px=640, h_px=480)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertTrue(png.endswith(b"IEND\xaeB`\x82"))

    def test_translation_files_key_parity(self) -> None:
        """strings.json, translations/de.json, and translations/en.json must have identical key trees."""
        import json
        from pathlib import Path

        comp = (
            Path(__file__).resolve().parents[1]
            / "custom_components"
            / "rf_presence_sense"
        )

        def _keys(obj, prefix=""):
            out = set()
            if isinstance(obj, dict):
                for k, v in obj.items():
                    path = f"{prefix}.{k}" if prefix else k
                    out.add(path)
                    out.update(_keys(v, path))
            return out

        k_str = _keys(json.loads((comp / "strings.json").read_text(encoding="utf-8")))
        k_de = _keys(
            json.loads((comp / "translations" / "de.json").read_text(encoding="utf-8"))
        )
        k_en = _keys(
            json.loads((comp / "translations" / "en.json").read_text(encoding="utf-8"))
        )
        self.assertEqual(k_str, k_de)
        self.assertEqual(k_str, k_en)

    def test_away_entity_unavailable_falls_back_to_targets(self) -> None:
        """An unavailable away_entity must not permanently freeze baseline adaptation."""
        hass = _FakeHass()
        hass.states = types.SimpleNamespace(
            get=lambda _eid: types.SimpleNamespace(state="unavailable"),
            async_all=lambda: [],
        )
        entry = types.SimpleNamespace(
            entry_id="away_test",
            data={
                "enable_ble": False,
                "enable_wifi_entities": False,
                "presence_aware_baseline": True,
                "away_entity": "zone.home",
            },
            options={},
        )
        coord = _coordinator_mod.RFPresenceCoordinator(hass, entry)
        coord.last_targets = []
        self.assertFalse(coord._should_freeze_baseline())
        coord.last_targets = [_tracker.TrackedTarget(track_id=1, x=2.0, y=2.0)]
        self.assertTrue(coord._should_freeze_baseline())
        coord.async_shutdown_collector()


class TestARM64HardeningAndHA2026Fixes(unittest.TestCase):
    """Tests for ARM64 OOM/CPU hardening, state-change gating, event throttling & HA 2026 fixes."""

    def test_defaults_and_minimum_update_interval_and_max_voxels(self) -> None:
        """DEFAULT_UPDATE_INTERVAL=4.0s, MIN=2.0s, MAX_VOXELS=500 enforced on coordinator."""
        self.assertEqual(_const.DEFAULT_UPDATE_INTERVAL, 4.0)
        self.assertEqual(_const.MIN_UPDATE_INTERVAL, 2.0)
        self.assertEqual(_const.MAX_VOXELS, 500)

        hass = _FakeHass()
        entry = types.SimpleNamespace(
            entry_id="clamp_test",
            data={
                "enable_ble": False,
                "enable_wifi_entities": False,
                "update_interval": 0.5,
                "floor_width": 10.0,
                "floor_height": 10.0,
                "grid_resolution": 0.5,
            },
            options={},
        )
        coord = _coordinator_mod.RFPresenceCoordinator(hass, entry)
        self.assertEqual(coord.update_interval.total_seconds(), 2.0)
        # 10x10m @ 0.5m = 400 voxels <= 500 -> keeps 0.5m resolution
        self.assertEqual(coord.engine.num_voxels, 400)
        self.assertEqual(coord.engine.resolution, 0.5)
        coord.async_shutdown_collector()

    def test_state_change_gating_on_sensors_and_binary_sensors(self) -> None:
        """Entities must only call async_write_ha_state when relevant state/attrs change."""
        entry = types.SimpleNamespace(entry_id="gate_test")
        room = _tracker.RoomZone(
            room_id="wz", name="Wohnzimmer", x_min=0.0, y_min=0.0, x_max=5.0, y_max=5.0
        )
        coord = types.SimpleNamespace(
            data={
                "humans_count": 0,
                "animals_count": 0,
                "total_targets": 0,
                "calibrating": False,
                "targets": [],
                "rooms": {
                    "wz": {
                        "name": "Wohnzimmer",
                        "occupied": False,
                        "human_detected": False,
                        "animal_detected": False,
                        "humans": 0,
                        "animals": 0,
                        "targets": [],
                    }
                },
                "rooms_geometry": [],
                "geometry": {},
                "devices": [],
                "receivers": [],
                "active_links_3d": [],
                "heatmap_peaks": [],
                "active_links": 5,
                "total_links": 5,
                "link_health_pct": 100.0,
                "packets_received": 10,
                "link_perturbations": {},
                "fingerprint_count": 0,
                "edge_walk_active": False,
                "fingerprints_xy": [],
            },
            rooms=[room],
            sensitivity=1.0,
            calibration_duration=20,
            presence_aware_baseline=True,
            mask_outside_rooms=True,
        )

        count_s = _sensor_mod.RFCountSensor(coord, entry, "humans_count", "humans_count", "mdi:account")
        health_s = _sensor_mod.RFLinkHealthSensor(coord, entry)
        detail_s = _sensor_mod.RFTargetsDetailSensor(coord, entry)
        global_bs = _binary_sensor_mod.RFGlobalPresenceBinarySensor(
            coord, entry, "humans_count", "human_detected", "mdi:motion-sensor"
        )
        room_bs = _binary_sensor_mod.RFRoomOccupancyBinarySensor(
            coord, entry, "wz", "Wohnzimmer", mode="occupied"
        )

        writes: dict[str, int] = {"count": 0, "health": 0, "detail": 0, "global": 0, "room": 0}
        count_s.async_write_ha_state = lambda: writes.__setitem__("count", writes["count"] + 1)
        health_s.async_write_ha_state = lambda: writes.__setitem__("health", writes["health"] + 1)
        detail_s.async_write_ha_state = lambda: writes.__setitem__("detail", writes["detail"] + 1)
        global_bs.async_write_ha_state = lambda: writes.__setitem__("global", writes["global"] + 1)
        room_bs.async_write_ha_state = lambda: writes.__setitem__("room", writes["room"] + 1)

        with patch("time.monotonic", return_value=100.0):
            for ent in (count_s, health_s, detail_s, global_bs, room_bs):
                ent._handle_coordinator_update()
        self.assertEqual(writes, {"count": 1, "health": 1, "detail": 1, "global": 1, "room": 1})

        # Unchanged state + incremented packets_received within 30s -> ZERO writes!
        coord.data["packets_received"] = 25
        with patch("time.monotonic", return_value=108.0):
            for ent in (count_s, health_s, detail_s, global_bs, room_bs):
                ent._handle_coordinator_update()
        self.assertEqual(writes, {"count": 1, "health": 1, "detail": 1, "global": 1, "room": 1})

        # After 31s -> RFLinkHealthSensor heartbeat writes once; others still 1
        coord.data["packets_received"] = 40
        with patch("time.monotonic", return_value=131.0):
            for ent in (count_s, health_s, detail_s, global_bs, room_bs):
                ent._handle_coordinator_update()
        self.assertEqual(writes, {"count": 1, "health": 2, "detail": 1, "global": 1, "room": 1})

        # Now a human appears in Wohnzimmer -> count, detail, global, room write; health unchanged
        coord.data["humans_count"] = 1
        coord.data["rooms"]["wz"]["occupied"] = True
        coord.data["rooms"]["wz"]["humans"] = 1
        with patch("time.monotonic", return_value=135.0):
            for ent in (count_s, health_s, detail_s, global_bs, room_bs):
                ent._handle_coordinator_update()
        self.assertEqual(writes, {"count": 2, "health": 2, "detail": 2, "global": 2, "room": 2})

    def test_bus_event_rate_limit_and_room_change_debounce(self) -> None:
        """Bus events must cap at 10 per 60s and ignore 1-tick room-change flickers."""
        hass = _FakeHass()
        hass.bus = types.SimpleNamespace(
            events=[],
            async_fire=lambda etype, edata: hass.bus.events.append((etype, edata)),
        )
        entry = types.SimpleNamespace(
            entry_id="rate_test",
            data={"enable_ble": False, "enable_wifi_entities": False},
            options={},
        )
        coord = _coordinator_mod.RFPresenceCoordinator(hass, entry)

        t_wz = _tracker.TrackedTarget(track_id=1, x=1.0, y=1.0, human_score=0.9, room_id="wz", room_name="Wohnzimmer")
        t_ku = _tracker.TrackedTarget(track_id=1, x=6.0, y=1.0, human_score=0.9, room_id="ku", room_name="Küche")

        # Detect in wz
        coord._fire_target_events([t_wz], now=100.0)
        self.assertEqual(len(hass.bus.events), 1)
        # 1-tick flicker to ku and back to wz -> NO room_changed event!
        coord._fire_target_events([t_ku], now=104.0)
        coord._fire_target_events([t_wz], now=108.0)
        self.assertEqual(len(hass.bus.events), 1)

        # Flood 20 distinct new targets within 10s -> capped at 10 total bus events in 60s
        for idx in range(2, 22):
            t_new = _tracker.TrackedTarget(
                track_id=idx, x=2.0, y=2.0, human_score=0.9, room_id="wz", room_name="Wohnzimmer"
            )
            coord._fire_target_events([t_wz, t_new], now=110.0 + idx * 0.2)
        self.assertEqual(len(hass.bus.events), _const.BUS_EVENT_MAX_PER_WINDOW)

        # After >60s window expires, new events are allowed again
        t_after = _tracker.TrackedTarget(
            track_id=99, x=2.0, y=2.0, human_score=0.9, room_id="wz", room_name="Wohnzimmer"
        )
        coord._fire_target_events([t_wz, t_after], now=200.0)
        self.assertGreater(len(hass.bus.events), _const.BUS_EVENT_MAX_PER_WINDOW)
        coord.async_shutdown_collector()

    def test_projection_rebuild_debounced_for_dynamic_links_and_forced_for_user(self) -> None:
        """Dynamic links respect 8s cooldown; explicit updates force immediate rebuild."""
        engine = RTIEngine(floor_width=6.0, floor_height=6.0, resolution=1.0)
        l1 = RFLink("d1->r1", "d1", "r1", "ble", Point3D(1.0, 1.0, 1.0), Point3D(5.0, 5.0, 1.0))
        engine.add_or_update_link(l1, dynamic=False)
        with patch("time.monotonic", return_value=100.0):
            self.assertTrue(engine.rebuild_projection(force=False))

        # Dynamic link added 2s later -> debounced!
        l2 = RFLink("d1->r2", "d1", "r2", "ble", Point3D(1.0, 1.0, 1.0), Point3D(5.0, 1.0, 1.0))
        engine.add_or_update_link(l2, dynamic=True)
        with patch("time.monotonic", return_value=102.0):
            self.assertFalse(engine.rebuild_projection(force=False))
            self.assertTrue(engine._dirty_geometry)

        # Explicit force=True bypasses cooldown
        with patch("time.monotonic", return_value=103.0):
            self.assertTrue(engine.rebuild_projection(force=True))
            self.assertFalse(engine._dirty_geometry)

        # Another dynamic link: after cooldown (>= 8s) it rebuilds automatically
        l3 = RFLink("d1->r3", "d1", "r3", "ble", Point3D(1.0, 1.0, 1.0), Point3D(1.0, 5.0, 1.0))
        engine.add_or_update_link(l3, dynamic=True)
        with patch("time.monotonic", return_value=112.0):
            self.assertTrue(engine.rebuild_projection(force=False))
            self.assertFalse(engine._dirty_geometry)

    def test_overlapping_executor_reconstruction_skipped(self) -> None:
        """If _reconstructing is True, a concurrent _async_update_data must skip cleanly."""
        hass = _FakeHass()
        entry = types.SimpleNamespace(
            entry_id="overlap_test",
            data={"enable_ble": False, "enable_wifi_entities": False},
            options={},
        )
        coord = _coordinator_mod.RFPresenceCoordinator(hass, entry)
        coord.data = {"sentinel": True}
        coord._reconstructing = True

        async def _run() -> None:
            res = await coord._async_update_data()
            self.assertEqual(res, {"sentinel": True})
            self.assertEqual(hass.executor_calls, [])

        _run_async(_run())
        coord._reconstructing = False
        coord.async_shutdown_collector()

    def test_ha2026_device_registry_items_and_esp32_mac_offset_and_room_fallbacks(self) -> None:
        """Support non-dict DeviceRegistryItems, ESP32 ±3 MAC offset, balkon->wohnzimmer & hauseingang."""
        from collections import UserDict
        import sys

        class FakeDeviceRegistryItems(UserDict):
            """Mimics HA 2025/2026 DeviceRegistryItems (not an instance of dict)."""

        area_wz = types.SimpleNamespace(id="area_wz", name="Wohnzimmer")
        area_eb = types.SimpleNamespace(id="area_eb", name="Eingangsbereich")
        shelly_dev = types.SimpleNamespace(
            id="dev_shelly_1",
            name="Shelly Plus Plug WZ",
            name_by_user=None,
            area_id="area_wz",
            connections={("mac", "A0:DD:6C:12:34:56")},
            identifiers={("shelly", "shellyplusplugs-a0dd6c123456")},
        )
        extra_dev = types.SimpleNamespace(
            id="dev_no_area",
            name="Shelly Flur",
            name_by_user="Shelly Hauseingang",
            area_id="area_eb",
            connections={("mac", "B0:AA:11:22:33:44")},
            identifiers=set(),
        )
        fake_dev_reg = types.SimpleNamespace(
            devices=FakeDeviceRegistryItems(
                {"dev_shelly_1": shelly_dev, "dev_no_area": extra_dev}
            ),
            async_get=lambda did: fake_dev_reg.devices.get(did),
        )
        fake_area_reg = types.SimpleNamespace(
            async_list_areas=lambda: [area_wz, area_eb],
            async_get_area=lambda aid: {"area_wz": area_wz, "area_eb": area_eb}.get(aid),
        )
        fake_ent_reg = types.SimpleNamespace(async_get=lambda _eid: None)

        ar_mod = types.ModuleType("homeassistant.helpers.area_registry")
        ar_mod.async_get = lambda _h: fake_area_reg  # type: ignore[attr-defined]
        dr_mod = types.ModuleType("homeassistant.helpers.device_registry")
        dr_mod.async_get = lambda _h: fake_dev_reg  # type: ignore[attr-defined]
        dr_mod.async_entries_for_area = lambda reg, aid: [  # type: ignore[attr-defined]
            d for d in reg.devices.values() if d.area_id == aid
        ]
        er_mod = types.ModuleType("homeassistant.helpers.entity_registry")
        er_mod.async_get = lambda _h: fake_ent_reg  # type: ignore[attr-defined]

        orig_ar = sys.modules.get("homeassistant.helpers.area_registry")
        orig_dr = sys.modules.get("homeassistant.helpers.device_registry")
        orig_er = sys.modules.get("homeassistant.helpers.entity_registry")
        sys.modules["homeassistant.helpers.area_registry"] = ar_mod
        sys.modules["homeassistant.helpers.device_registry"] = dr_mod
        sys.modules["homeassistant.helpers.entity_registry"] = er_mod
        try:
            # Verify our container is indeed NOT a dict
            self.assertFalse(isinstance(fake_dev_reg.devices, dict))
            meta = _collector_mod.discover_devices_and_receivers_metadata(
                _FakeHass(),
                # BLE MAC +2 offset from Shelly Wi-Fi MAC (:56 -> :58), plus +4 out-of-range (:5A)
                device_ids=["A0:DD:6C:12:34:58", "A0:DD:6C:12:34:5A"],
                # BLE Proxy MAC -2 offset (:44 -> :42)
                receiver_ids=["B0:AA:11:22:33:42"],
            )
            self.assertEqual(meta["A0:DD:6C:12:34:58"]["area"], "Wohnzimmer")
            self.assertEqual(meta["A0:DD:6C:12:34:58"]["name"], "Shelly Plus Plug WZ")
            self.assertNotIn("A0:DD:6C:12:34:5A", meta)
            self.assertEqual(meta["B0:AA:11:22:33:42"]["area"], "Eingangsbereich")
            self.assertEqual(meta["B0:AA:11:22:33:42"]["name"], "Shelly Hauseingang")
        finally:
            for name, orig in (
                ("homeassistant.helpers.area_registry", orig_ar),
                ("homeassistant.helpers.device_registry", orig_dr),
                ("homeassistant.helpers.entity_registry", orig_er),
            ):
                if orig is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = orig

        # Room inference fallbacks: balkon -> wohnzimmer (when no balkon room), hauseingang -> eingangsbereich
        rooms_no_balcony = [
            _tracker.RoomZone("wohnzimmer", "Wohnzimmer", 0, 0, 5, 5),
            _tracker.RoomZone("eingangsbereich", "Eingangsbereich", 5, 0, 7, 3),
        ]
        self.assertEqual(
            _collector_mod.infer_room_from_name("Shelly Balkon", rooms_no_balcony),
            "wohnzimmer",
        )
        self.assertEqual(
            _collector_mod.infer_room_from_name("Hauseingang Licht", rooms_no_balcony),
            "eingangsbereich",
        )
        rooms_with_balcony = rooms_no_balcony + [
            _tracker.RoomZone("balkon", "Balkon", 0, 5, 5, 7)
        ]
        self.assertEqual(
            _collector_mod.infer_room_from_name("Shelly Balkon", rooms_with_balcony),
            "balkon",
        )


if __name__ == "__main__":
    unittest.main()
