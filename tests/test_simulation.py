"""End-to-end 3D apartment RF physics simulation for human & animal tracking."""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path
import unittest

from _loader import load_core_modules

_const, _rti, _tracker = load_core_modules()
TARGET_TYPE_ANIMAL = _const.TARGET_TYPE_ANIMAL
TARGET_TYPE_HUMAN = _const.TARGET_TYPE_HUMAN
Point3D = _rti.Point3D
RFLink = _rti.RFLink
RTIEngine = _rti.RTIEngine
MultiTargetTracker = _tracker.MultiTargetTracker
RoomZone = _tracker.RoomZone


def _simulate_body_attenuation(
    link: RFLink,
    bx: float,
    by: float,
    body_height_m: float,
    max_atten_db: float,
) -> float:
    """3D Fresnel attenuation model: a body only blocks links at or below its height."""
    # Check vertical intersection: link height at closest approach
    link_z = link.mean_height
    if link_z > body_height_m + 0.25:
        return 0.0  # Link passes cleanly over the animal's head!

    d_link = math.hypot(link.p2.x - link.p1.x, link.p2.y - link.p1.y)
    d1 = math.hypot(bx - link.p1.x, by - link.p1.y)
    d2 = math.hypot(bx - link.p2.x, by - link.p2.y)
    excess = (d1 + d2) - d_link
    if excess > 1.3:
        return 0.0
    return max_atten_db * math.exp(-excess / 0.35)


class TestApartmentSimulation(unittest.TestCase):
    """Simulates a 8m x 6m apartment with existing Wi-Fi & BLE devices."""

    def _build_apartment(self) -> tuple[RTIEngine, MultiTargetTracker]:
        engine = RTIEngine(
            floor_width=8.0, floor_height=6.0, resolution=0.5, sensitivity=1.5
        )
        rooms = [
            RoomZone("wohnzimmer", "Wohnzimmer", 0.0, 0.0, 4.5, 6.0),
            RoomZone("kueche", "Küche", 4.5, 0.0, 8.0, 6.0),
        ]
        tracker = MultiTargetTracker(rooms=rooms)

        # Two existing receivers at distinct spots: Router (1.5m) & HA Bluetooth host (0.45m)
        receivers = {
            "router_main": Point3D(4.0, 3.0, 1.5),
            "ha_bluetooth": Point3D(4.0, 0.5, 0.45),
        }

        # Existing stationary IoT devices: mix of smart plugs (z=0.35m) & lamps/sensors (z=1.7m)
        devices = [
            ("shelly_plug_wz1", "wifi", "router_main", Point3D(0.4, 0.6, 0.35)),
            ("shelly_lamp_wz1", "wifi", "router_main", Point3D(0.5, 0.8, 1.85)),
            ("shelly_plug_wz2", "wifi", "router_main", Point3D(0.4, 5.4, 0.35)),
            ("shelly_lamp_wz2", "wifi", "router_main", Point3D(0.6, 5.2, 1.85)),
            ("tasmota_plug_ku1", "wifi", "router_main", Point3D(7.6, 0.6, 0.35)),
            ("tasmota_lamp_ku1", "wifi", "router_main", Point3D(7.4, 0.8, 1.85)),
            ("tasmota_plug_ku2", "wifi", "router_main", Point3D(7.6, 5.4, 0.35)),
            ("tasmota_lamp_ku2", "wifi", "router_main", Point3D(7.4, 5.2, 1.85)),
            # Stationary BLE devices (temp sensors, smart plugs, TV) -> HA host
            ("AA:BB:CC:00:00:01", "ble", "ha_bluetooth", Point3D(1.5, 5.6, 0.40)),
            ("AA:BB:CC:00:00:02", "ble", "ha_bluetooth", Point3D(1.5, 0.4, 0.40)),
            ("AA:BB:CC:00:00:03", "ble", "ha_bluetooth", Point3D(6.5, 5.6, 0.40)),
            ("AA:BB:CC:00:00:04", "ble", "ha_bluetooth", Point3D(6.5, 0.4, 0.40)),
            ("AA:BB:CC:00:00:05", "ble", "ha_bluetooth", Point3D(2.2, 3.0, 1.60)),
            ("AA:BB:CC:00:00:06", "ble", "ha_bluetooth", Point3D(6.2, 3.0, 1.60)),
            ("AA:BB:CC:00:00:07", "ble", "ha_bluetooth", Point3D(7.6, 2.5, 0.40)),
        ]

        for did, proto, rid, pos in devices:
            link = RFLink(
                link_id=f"{did}->{rid}",
                device_id=did,
                receiver_id=rid,
                protocol=proto,
                p1=pos,
                p2=receivers[rid],
            )
            link.baseline_rssi = -60.0
            link.baseline_std = 0.35
            engine.add_or_update_link(link)

        engine.rebuild_projection()
        return engine, tracker

    def test_simultaneous_human_and_animal_localization_and_classification(self) -> None:
        """Human in Wohnzimmer (2.2, 4.2) + Cat in Küche (6.0, 1.6) tracked simultaneously."""
        rng = random.Random(42)
        engine, tracker = self._build_apartment()

        human_pos = (2.2, 4.2)  # Wohnzimmer, height 1.75m
        cat_pos = (6.0, 1.6)  # Küche, height 0.35m

        targets = []
        for step in range(6):
            now = 500.0 + step * 2.0
            for link in engine.links.values():
                atten_h = _simulate_body_attenuation(
                    link, human_pos[0], human_pos[1], body_height_m=1.75, max_atten_db=6.8
                )
                atten_c = _simulate_body_attenuation(
                    link, cat_pos[0], cat_pos[1], body_height_m=0.38, max_atten_db=3.2
                )
                noise = rng.gauss(0.0, 0.25)
                rssi = -60.0 - atten_h - atten_c + noise
                link.add_sample(rssi, now=now)

            recon = engine.reconstruct(now=now)
            targets = tracker.update(recon, dt=2.0)

        self.assertEqual(len(targets), 2, f"Expected 2 targets, got {[t.to_dict() for t in targets]}")

        humans = [t for t in targets if t.target_type == TARGET_TYPE_HUMAN]
        animals = [t for t in targets if t.target_type == TARGET_TYPE_ANIMAL]

        self.assertEqual(len(humans), 1, f"Expected 1 human, got {[t.to_dict() for t in targets]}")
        self.assertEqual(len(animals), 1, f"Expected 1 animal, got {[t.to_dict() for t in targets]}")

        h = humans[0]
        a = animals[0]
        self.assertEqual(h.room_id, "wohnzimmer")
        self.assertEqual(a.room_id, "kueche")
        self.assertLess(math.hypot(h.x - human_pos[0], h.y - human_pos[1]), 1.0)
        # Single low-height link crossing the cat yields ~1.4m along-link precision
        self.assertLess(math.hypot(a.x - cat_pos[0], a.y - cat_pos[1]), 1.6)


if __name__ == "__main__":
    unittest.main()
