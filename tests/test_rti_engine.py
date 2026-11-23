"""Unit tests for the pure-Python Radio Tomographic Imaging (RTI) engine."""

from __future__ import annotations

import math
import sys
from pathlib import Path
import unittest

from _loader import load_core_modules

_, _rti, _ = load_core_modules()
Point3D = _rti.Point3D
RFLink = _rti.RFLink
RTIEngine = _rti.RTIEngine
_cholesky_invert = _rti._cholesky_invert


class TestRTIEngine(unittest.TestCase):
    """Tests for Woodbury dual RTI inversion, baseline calibration, and Fresnel weights."""

    def test_cholesky_invert_identity_and_spd(self) -> None:
        """Verify Cholesky inverse satisfies A * A^-1 = I."""
        A = [
            [4.0, 1.2, 0.3],
            [1.2, 3.5, 0.8],
            [0.3, 0.8, 2.5],
        ]
        inv = _cholesky_invert(A)
        for i in range(3):
            for j in range(3):
                prod = sum(A[i][k] * inv[k][j] for k in range(3))
                expected = 1.0 if i == j else 0.0
                self.assertAlmostEqual(prod, expected, places=6)

    def test_empty_room_produces_zero_heatmap(self) -> None:
        """Calibrated unperturbed links should reconstruct zero attenuation."""
        engine = RTIEngine(floor_width=6.0, floor_height=6.0, resolution=0.5)
        router = Point3D(3.0, 3.0, 1.2)
        for idx, (x, y) in enumerate([(0.5, 0.5), (5.5, 0.5), (5.5, 5.5), (0.5, 5.5)]):
            link = RFLink(
                link_id=f"dev_{idx}->router",
                device_id=f"dev_{idx}",
                receiver_id="router",
                protocol="wifi",
                p1=Point3D(x, y, 1.2),
                p2=router,
            )
            for t in range(10):
                link.add_sample(-60.0 + (t % 2) * 0.2, now=100.0 + t, calibrating=True)
            link.finalize_calibration()
            link.add_sample(-60.0, now=120.0)
            engine.add_or_update_link(link)

        recon = engine.reconstruct(now=121.0)
        max_val = max(max(row) for row in recon.grid)
        self.assertLess(max_val, 0.15)
        self.assertEqual(recon.active_links, 4)

    def test_single_target_localization_accuracy(self) -> None:
        """A body attenuating links crossing (2.0, 4.0) should produce a peak near (2.0, 4.0)."""
        engine = RTIEngine(floor_width=8.0, floor_height=6.0, resolution=0.5)
        rx_a = Point3D(4.0, 3.0, 1.2)
        rx_b = Point3D(1.0, 1.0, 1.2)

        nodes = [
            (0.5, 5.5),
            (3.5, 5.5),
            (7.5, 5.5),
            (7.5, 0.5),
            (0.5, 3.0),
            (4.0, 0.5),
        ]
        target_x, target_y = 2.0, 4.0

        for idx, (nx, ny) in enumerate(nodes):
            for r_idx, rx in enumerate((rx_a, rx_b)):
                lid = f"n{idx}->r{r_idx}"
                link = RFLink(
                    link_id=lid,
                    device_id=f"n{idx}",
                    receiver_id=f"r{r_idx}",
                    protocol="wifi" if r_idx == 0 else "ble",
                    p1=Point3D(nx, ny, 1.2),
                    p2=rx,
                )
                link.baseline_rssi = -62.0
                link.baseline_std = 0.4

                # Simulate Fresnel shadowing from target at (2.0, 4.0)
                d_link = math.hypot(rx.x - nx, rx.y - ny)
                d1 = math.hypot(target_x - nx, target_y - ny)
                d2 = math.hypot(target_x - rx.x, target_y - rx.y)
                excess = (d1 + d2) - d_link
                atten = 6.5 * math.exp(-excess / 0.35) if excess < 1.2 else 0.0
                link.add_sample(-62.0 - atten, now=200.0)
                engine.add_or_update_link(link)

        recon = engine.reconstruct(now=201.0)
        # Find grid maximum
        best_val, best_r, best_c = -1.0, 0, 0
        for r in range(recon.height):
            for c in range(recon.width):
                if recon.grid[r][c] > best_val:
                    best_val = recon.grid[r][c]
                    best_r, best_c = r, c

        est_x = (best_c + 0.5) * recon.resolution
        est_y = (best_r + 0.5) * recon.resolution
        err = math.hypot(est_x - target_x, est_y - target_y)
        self.assertGreater(best_val, 1.0)
        self.assertLess(err, 0.9, f"Localization error too large: {err:.2f}m at ({est_x}, {est_y})")


if __name__ == "__main__":
    unittest.main()
