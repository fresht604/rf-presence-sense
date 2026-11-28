"""Unit tests for RF Presence Sense v2.2.0 features.

Covers:
  - Per-link baseline freeze at shadowing >= 1.2 dB (even before track confirmation)
  - 30-second micro-motion / breathing variance window in RFLink
  - Static-Presence Hysteresis (0.55 * threshold), TRACKER_MAX_MISSED_STATIONARY = 12,
    motion_state ("stationary" after >= 10s with speed < 0.12 m/s), and dwell_s
  - RoomZone windows persistence, compact 'wn' state attr, 3D PNG rendering & Ruhend badge
  - Smart window/floor placement in auto_place_from_areas (Rollo/SBDW -> window edge z=1.6m,
    Leiste/Plug/PStrip -> z=0.35m, strictly clamped)
  - South-Front default camera (-20 deg yaw, 38 deg pitch) & windows/Ruhend in 3D card JS
  - Strict < 16 KB worst-case state payload with v2.2.0 fields (motion, dwell_s, wn)
  - Translation sync & ICU safety (no literal {edge...} in service descriptions)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import struct
import time
import types
import unittest

from _loader import load_core_modules, load_submodule, reset_stub_storage

_const, _rti, _tracker = load_core_modules()
_storage_mod = load_submodule("storage", with_ha_stubs=True)
_coordinator_mod = load_submodule("coordinator", with_ha_stubs=True)
_camera_mod = load_submodule("camera", with_ha_stubs=True)
_sensor_mod = load_submodule("sensor", with_ha_stubs=True)

Point3D = _rti.Point3D
RFLink = _rti.RFLink
RTIReconstruction = _rti.RTIReconstruction
MultiTargetTracker = _tracker.MultiTargetTracker
RoomZone = _tracker.RoomZone
TrackedTarget = _tracker.TrackedTarget


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


def _make_single_peak_recon(
    x: float, y: float, intensity: float, cols: int = 16, rows: int = 12, res: float = 0.5
) -> RTIReconstruction:
    """Create a synthetic RTIReconstruction with a single peak at (x, y)."""
    grid = [[0.0] * cols for _ in range(rows)]
    low_g = [[0.0] * cols for _ in range(rows)]
    high_g = [[0.0] * cols for _ in range(rows)]
    low_cov = [[1.0] * cols for _ in range(rows)]
    high_cov = [[1.0] * cols for _ in range(rows)]
    if intensity > 0.0:
        c = max(0, min(cols - 1, int(x / res)))
        r = max(0, min(rows - 1, int(y / res)))
        grid[r][c] = intensity
        high_g[r][c] = intensity * 0.8
        low_g[r][c] = intensity * 0.3
    return RTIReconstruction(
        width=cols,
        height=rows,
        resolution=res,
        grid=grid,
        low_layer_grid=low_g,
        high_layer_grid=high_g,
        low_coverage_grid=low_cov,
        high_coverage_grid=high_cov,
        active_links=4,
        total_links=4,
        link_perturbations={"d1->r1": intensity} if intensity > 0 else {},
    )


class TestStationaryPresenceDetection(unittest.TestCase):
    """Tests for v2.2.0 stationary/unmoving presence detection."""

    def test_per_link_baseline_freezes_at_1_2_db_shadowing(self) -> None:
        """A link shadowed by >= 1.2 dB must freeze baseline even when freeze_baseline=False."""
        link = RFLink(
            link_id="d1->r1",
            device_id="d1",
            receiver_id="r1",
            protocol="wifi",
            p1=Point3D(1.0, 1.0, 1.0),
            p2=Point3D(5.0, 5.0, 1.0),
        )
        link.baseline_rssi = -60.0

        # Shadowing of exactly 1.2 dB and 1.5 dB must NOT drift baseline
        for i in range(30):
            link.add_sample(-61.2, now=10.0 + i, freeze_baseline=False)
        self.assertEqual(link.baseline_rssi, -60.0)

        for i in range(30):
            link.add_sample(-61.5, now=50.0 + i, freeze_baseline=False)
        self.assertEqual(link.baseline_rssi, -60.0)

    def test_30s_micro_motion_variance_window_stabilizes_stationary_signal(self) -> None:
        """Slow breathing/posture shifts across 30s must contribute excess_std even if 12s is flat."""
        link = RFLink(
            link_id="d1->r1",
            device_id="d1",
            receiver_id="r1",
            protocol="wifi",
            p1=Point3D(1.0, 1.0, 1.0),
            p2=Point3D(5.0, 5.0, 1.0),
        )
        link.baseline_rssi = -60.0
        link.baseline_std = 0.4

        # Earlier in 30s window: subtle posture shift (-62.5 dBm)
        link.add_sample(-62.5, now=100.0, freeze_baseline=True)
        link.add_sample(-62.3, now=104.0, freeze_baseline=True)
        # Recent 12s window: completely flat at -60.0 dBm (0 shadowing, 0 short-term std)
        link.add_sample(-60.0, now=115.0, freeze_baseline=True)
        link.add_sample(-60.0, now=118.0, freeze_baseline=True)
        link.add_sample(-60.0, now=121.0, freeze_baseline=True)
        link.add_sample(-60.0, now=124.0, freeze_baseline=True)

        total, shadowing, excess_std = link.compute_perturbation(now=125.0)
        self.assertAlmostEqual(shadowing, 0.0, places=3)
        self.assertGreater(excess_std, 0.3)
        self.assertGreater(total, 0.4)

    def test_static_presence_hysteresis_dwell_and_12_tick_hold(self) -> None:
        """Confirmed target holds at 0.55*threshold, transitions to stationary >=10s, holds 12 missed ticks."""
        room = RoomZone("wz", "Wohnzimmer", 0.0, 0.0, 8.0, 6.0)
        tracker = MultiTargetTracker(rooms=[room], detection_threshold=1.0, min_hits=2)

        # 1. A weak signal (0.65 < 1.0) must NOT spawn a new target from scratch
        weak_recon = _make_single_peak_recon(2.25, 2.25, intensity=0.65)
        self.assertEqual(len(tracker.update(weak_recon, dt=4.0)), 0)
        self.assertEqual(len(tracker.update(weak_recon, dt=4.0)), 0)

        # 2. Strong signal confirms the target
        strong_recon = _make_single_peak_recon(2.25, 2.25, intensity=2.4)
        tracker.update(strong_recon, dt=4.0)
        targets = tracker.update(strong_recon, dt=4.0)
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].motion_state, "moving")

        # 3. Drop signal to 0.65 (between 0.55 and 1.0) -> hysteresis MUST keep target alive
        #    and after >= 10s dwell with speed < 0.12 m/s, motion_state becomes "stationary"
        for _ in range(3):
            targets = tracker.update(weak_recon, dt=4.0)
            self.assertEqual(len(targets), 1)
        t = targets[0]
        self.assertEqual(t.motion_state, "stationary")
        self.assertGreaterEqual(t.dwell_seconds, 10.0)
        payload = t.to_dict()
        self.assertEqual(payload["motion"], "stationary")
        self.assertIsInstance(payload["dwell_s"], int)
        self.assertGreaterEqual(payload["dwell_s"], 10)

        # 4. Completely silent frames -> stationary target holds for TRACKER_MAX_MISSED_STATIONARY (12) ticks
        empty_recon = _make_single_peak_recon(2.25, 2.25, intensity=0.0)
        for tick in range(1, _const.TRACKER_MAX_MISSED_STATIONARY + 1):
            held = tracker.update(empty_recon, dt=4.0)
            self.assertEqual(
                len(held), 1, f"Stationary target dropped early at missed tick {tick}"
            )

        # Tick 13 -> finally dropped
        gone = tracker.update(empty_recon, dt=4.0)
        self.assertEqual(len(gone), 0)


class TestWindowsPlacementAndBudget(unittest.TestCase):
    """Tests for 3D windows, smart auto-placement, frontend defaults, translations & 16 KB limit."""

    def setUp(self) -> None:
        reset_stub_storage()

    def test_windows_persist_compact_attr_and_render_3d_png(self) -> None:
        """RoomZone.windows must persist, serialize as 'wn', and render in 3D PNG with Ruhend badge."""
        hass = _FakeHass()
        entry = types.SimpleNamespace(
            entry_id="win_test",
            data={
                "floor_width": 10.0,
                "floor_height": 10.0,
                "enable_ble": False,
                "enable_wifi_entities": False,
            },
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
                doors=[{"edge": "south", "pos": 0.5, "width": 0.9}],
                windows=[
                    {"edge": "north", "pos": 0.5, "width": 1.4},
                    {"edge": "west", "pos": 0.35, "width": 1.6},
                ],
            )
            await coord1.async_shutdown()

            coord2 = _coordinator_mod.RFPresenceCoordinator(hass, entry)
            await coord2.async_load_storage()
            rm = next(r for r in coord2.rooms if r.room_id == "wz")
            self.assertEqual(len(rm.windows), 2)
            self.assertEqual(rm.windows[0]["edge"], "north")
            self.assertAlmostEqual(rm.windows[0]["width"], 1.4, places=2)
            compact = rm.to_dict()
            self.assertEqual(compact["wn"], [["n", 0.5, 1.4], ["w", 0.35, 1.6]])

            # Add a stationary target and render 3D PNG
            coord2.last_targets = [
                TrackedTarget(
                    track_id=1,
                    x=2.5,
                    y=3.0,
                    hits=5,
                    human_score=0.92,
                    confidence=0.88,
                    room_id="wz",
                    room_name="Wohnzimmer",
                    motion_state="stationary",
                    dwell_seconds=245.0,
                )
            ]
            png = _camera_mod.render_heatmap_png(coord2, w_px=640, h_px=480)
            self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
            w, h = struct.unpack("!II", png[16:24])
            self.assertEqual((w, h), (640, 480))
            await coord2.async_shutdown()

        _run_async(_run())

    def test_auto_place_window_and_floor_devices_clamped(self) -> None:
        """Rollo/Fenster/SBDW place near window edge at z=1.6m; Leiste/PStrip at z=0.35m."""
        hass = _FakeHass()
        entry = types.SimpleNamespace(
            entry_id="auto_win_place",
            data={
                "floor_width": 10.0,
                "floor_height": 10.0,
                "enable_ble": False,
                "enable_wifi_entities": False,
                "devices": {
                    "sensor.rollo_wz_rssi": {
                        "name": "Rollo Wohnzimmer",
                        "protocol": "wifi",
                        "receiver_id": "router_main",
                    },
                    "AA:BB:CC:11:22:33": {
                        "name": "SBDW Fenster Wohnzimmer",
                        "protocol": "ble",
                        "receiver_id": "ha_bluetooth",
                    },
                    "sensor.pstrip_wz_rssi": {
                        "name": "PStrip Leiste Wohnzimmer",
                        "protocol": "wifi",
                        "receiver_id": "router_main",
                    },
                },
            },
            options={},
        )

        async def _run() -> None:
            coord = _coordinator_mod.RFPresenceCoordinator(hass, entry)
            await coord.async_define_room(
                "wz",
                "Wohnzimmer",
                0.0,
                0.0,
                5.0,
                6.0,
                windows=[{"edge": "north", "pos": 0.5, "width": 1.6}],
            )
            res = await coord.async_auto_place_from_areas()
            self.assertEqual(res["matched_devices"], 3)

            rollo = coord.devices["sensor.rollo_wz_rssi"]
            sbdw = coord.devices["AA:BB:CC:11:22:33"]
            pstrip = coord.devices["sensor.pstrip_wz_rssi"]

            # Window devices near north edge (y <= 0.5) at z = 1.6m
            self.assertLessEqual(rollo["y"], 0.5)
            self.assertAlmostEqual(rollo["z"], 1.6, places=2)
            self.assertLessEqual(sbdw["y"], 0.5)
            self.assertAlmostEqual(sbdw["z"], 1.6, places=2)

            # Floor strip device at z = 0.35m
            self.assertAlmostEqual(pstrip["z"], 0.35, places=2)

            # Strictly clamped inside room bounds
            for dev in (rollo, sbdw, pstrip):
                self.assertGreaterEqual(dev["x"], 0.0)
                self.assertLessEqual(dev["x"], 5.0)
                self.assertGreaterEqual(dev["y"], 0.0)
                self.assertLessEqual(dev["y"], 6.0)

            await coord.async_shutdown()

        _run_async(_run())

    def test_v22_worst_case_payload_stays_under_16kb(self) -> None:
        """Worst-case 80 links + 16 stationary targets + 80 fingerprints + 8 rooms w/ doors & windows < 16 KB."""
        hass = _FakeHass()
        devices = {
            f"sensor.shelly_plus_plug_s_{i:02d}_wifi_signal_strength": {
                "name": f"Plug {i:02d}",
                "protocol": "wifi",
                "receiver_id": "router_main",
                "x": (i % 10) + 0.5,
                "y": (i // 10) + 0.5,
                "z": 0.35,
            }
            for i in range(_const.MAX_LINKS)
        }
        entry = types.SimpleNamespace(
            entry_id="payload_v22",
            data={
                "floor_width": 10.0,
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
                float(i % 4) * 2.5,
                float(i // 4) * 4.0,
                float(i % 4 + 1) * 2.5,
                float(i // 4 + 1) * 4.0,
                doors=[{"edge": "south", "pos": 0.5, "width": 0.9}],
                windows=[{"edge": "north", "pos": 0.5, "width": 1.4}],
            )
            for i in range(8)
        ]

        targets = []
        for tid in range(1, _const.TRACKER_MAX_TRACKS + 1):
            t = TrackedTarget(
                track_id=tid,
                x=2.0 + (tid % 4),
                y=2.0 + (tid // 4),
                hits=10,
                human_score=0.9,
                confidence=0.88,
                room_id=f"room_{tid % 8}",
                room_name=f"Zimmer {tid % 8}",
                motion_state="stationary",
                dwell_seconds=360.0,
            )
            for step in range(15):
                t.trail.append((t.x + step * 0.01, t.y + step * 0.01))
            targets.append(t)

        coord.data = {
            "calibrating": False,
            "humans_count": len(targets),
            "animals_count": 0,
            "total_targets": len(targets),
            "targets": [t.to_dict() for t in targets],
            "rooms": {
                r.room_id: {
                    "name": r.name,
                    "occupied": True,
                    "human_detected": True,
                    "animal_detected": False,
                    "humans": 2,
                    "animals": 0,
                }
                for r in coord.rooms
            },
            "rooms_geometry": [r.to_dict() for r in coord.rooms],
            "devices": [
                [did, f"Plug {i:02d}", "w", round((i % 10) + 0.5, 2), round((i // 10) + 0.5, 2), 0.35]
                for i, did in enumerate(devices)
            ],
            "receivers": [{"id": "router_main", "name": "WLAN Router", "x": 5.0, "y": 5.0, "z": 1.2}],
            "active_links_3d": [[i, 0, "w", 2.4] for i in range(_const.MAX_LINKS)],
            "heatmap_peaks": [[1.5, 2.5, 3.1] for _ in range(_const.MAX_REPORTED_HEATMAP_PEAKS)],
            "geometry": coord.engine.get_geometry_summary(),
            "sensitivity": 1.0,
            "calibration_duration": 20,
            "presence_aware_baseline": True,
            "mask_outside_rooms": True,
            "fingerprint_count": _const.MAX_REPORTED_FINGERPRINTS,
            "edge_walk_active": False,
            "fingerprints_xy": [[round(i * 0.1, 1), round(i * 0.1, 1)] for i in range(_const.MAX_REPORTED_FINGERPRINTS)],
        }

        sensor = _sensor_mod.RFTargetsDetailSensor(coord, entry)
        serialized = json.dumps(sensor.extra_state_attributes, separators=(",", ":"))
        self.assertLess(
            len(serialized.encode("utf-8")),
            16384,
            f"Worst-case v2.2.0 payload exceeded 16 KB: {len(serialized)} bytes",
        )

    def test_version_translations_icu_safety_and_frontend_camera_defaults(self) -> None:
        """Verify v2.2.0 version, ICU-safe service descriptions, translation sync & JS camera defaults."""
        base = (
            Path(__file__).resolve().parent.parent
            / "custom_components"
            / "rf_presence_sense"
        )
        self.assertEqual(_const.VERSION, "2.2.1")
        manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], "2.2.1")

        # Check translations are 100% key-synchronized and contain no literal ICU-breaking {edge...}
        strings = json.loads((base / "strings.json").read_text(encoding="utf-8"))
        de_tr = json.loads((base / "translations" / "de.json").read_text(encoding="utf-8"))
        en_tr = json.loads((base / "translations" / "en.json").read_text(encoding="utf-8"))

        def _collect_keys(obj, prefix=""):
            keys = set()
            for k, v in obj.items():
                p = f"{prefix}.{k}" if prefix else k
                keys.add(p)
                if isinstance(v, dict):
                    keys |= _collect_keys(v, p)
            return keys

        self.assertEqual(_collect_keys(strings), _collect_keys(en_tr))
        self.assertEqual(_collect_keys(strings), _collect_keys(de_tr))

        for tr_name, tr_obj in (("strings.json", strings), ("de.json", de_tr), ("en.json", en_tr)):
            svc_text = json.dumps(tr_obj.get("services", {}))
            self.assertNotIn("{edge", svc_text, f"ICU-breaking brace in {tr_name}")
            self.assertIn("windows", tr_obj["services"]["define_room"]["fields"])

        services_yaml = (base / "services.yaml").read_text(encoding="utf-8")
        self.assertIn("windows:", services_yaml)
        self.assertNotIn("{edge", services_yaml)

        js = (base / "frontend" / "rf-presence-3d-card.js").read_text(encoding="utf-8")
        self.assertIn("(-20 * Math.PI) / 180", js)
        self.assertIn("(38 * Math.PI) / 180", js)
        self.assertIn("Ruhend", js)
        self.assertIn("base.wn", js)

    def test_v221_link_median_warmup_suppresses_initial_noise_spikes(self) -> None:
        """New links must warm up over 3 samples using median baseline and emit 0.0 during warmup."""
        link = RFLink(
            link_id="ble_new->proxy1",
            device_id="ble_new",
            receiver_id="proxy1",
            protocol="ble",
            p1=Point3D(1.0, 1.0, 1.0),
            p2=Point3D(5.0, 5.0, 1.0),
        )
        self.assertIsNone(link.baseline_rssi)

        # Sample 1: noisy outlier (-82 dBm) -> still warming up, perturbation must be 0.0
        link.add_sample(-82.0, now=10.0)
        self.assertIsNone(link.baseline_rssi)
        self.assertEqual(link.compute_perturbation(now=10.5), (0.0, 0.0, 0.0))

        # Sample 2: normal reading (-60 dBm) -> still warming up
        link.add_sample(-60.0, now=12.0)
        self.assertIsNone(link.baseline_rssi)
        self.assertEqual(link.compute_perturbation(now=12.5), (0.0, 0.0, 0.0))

        # Sample 3: normal reading (-61 dBm) -> locks baseline to median (-61.0), ignoring -82 outlier!
        link.add_sample(-61.0, now=14.0)
        self.assertAlmostEqual(link.baseline_rssi, -61.0, places=3)

    def test_v221_sidelobe_pruning_min_separation_and_min_hits_3(self) -> None:
        """Verify 1.05 base threshold, 1.8m separation, 36% sidelobe pruning & min_hits=3."""
        self.assertAlmostEqual(_const.DETECTION_THRESHOLD_BASE, 1.05, places=3)
        self.assertEqual(_const.TRACKER_MIN_HITS, 3)

        cols, rows, res = 16, 12, 0.5
        grid = [[0.0] * cols for _ in range(rows)]
        low_g = [[0.0] * cols for _ in range(rows)]
        high_g = [[0.0] * cols for _ in range(rows)]
        cov = [[1.0] * cols for _ in range(rows)]

        # 1. Dominant main peak at (2.25, 2.25) -> 3.0
        grid[4][4] = 3.0
        high_g[4][4] = 2.5
        # 2. Too-close artifact at (3.25, 2.25) -> 2.1 (distance 1.0m < 1.8m -> pruned!)
        grid[4][6] = 2.1
        # 3. Weak sidelobe at (6.25, 2.25) -> 1.0 (< 0.36 * 3.0 = 1.08 -> pruned!)
        grid[4][12] = 1.0
        # 4. Real second target at (6.25, 5.25) -> 1.45 (>= 1.08 and >= 1.8m away -> kept!)
        grid[10][12] = 1.45
        high_g[10][12] = 1.2

        recon = RTIReconstruction(
            width=cols,
            height=rows,
            resolution=res,
            grid=grid,
            low_layer_grid=low_g,
            high_layer_grid=high_g,
            low_coverage_grid=cov,
            high_coverage_grid=cov,
            active_links=6,
            total_links=6,
            link_perturbations={"d1->r1": 3.0},
        )
        peaks = _tracker.extract_peaks(recon)
        self.assertEqual(len(peaks), 2, f"Expected 2 peaks after pruning, got {peaks}")
        self.assertAlmostEqual(peaks[0].intensity, 3.0, places=2)
        self.assertAlmostEqual(peaks[1].intensity, 1.45, places=2)

        # Default tracker requires TRACKER_MIN_HITS=3 consistent frames before confirming
        tracker = MultiTargetTracker(rooms=[RoomZone("az", "Arbeitszimmer", 0.0, 0.0, 8.0, 6.0)])
        single = _make_single_peak_recon(2.25, 2.25, intensity=2.5)
        self.assertEqual(len(tracker.update(single, dt=4.0)), 0)
        self.assertEqual(len(tracker.update(single, dt=4.0)), 0)
        self.assertEqual(len(tracker.update(single, dt=4.0)), 1)


if __name__ == "__main__":
    unittest.main()
