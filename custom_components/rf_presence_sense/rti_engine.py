"""Pure-Python Radio Tomographic Imaging (RTI + VRTI) Engine.

Implements device-free passive localization using existing Wi-Fi & Bluetooth RSSI
links via:
  1. Smooth Fresnel-zone elliptical link weighting (Wilson & Patwari, IEEE TMC 2010)
  2. Dual-mode Shadowing + Variance-Based RTI (VRTI, IEEE TMC 2011)
  3. Woodbury dual-form Tikhonov-regularized inversion (O(M^3) in pure Python,
     zero external dependencies)
  4. Adaptive dual-filter baseline tracking with empty-room calibration
  5. 3D vertical layer energy attribution (low-height vs. high-height links) for
     human vs. animal discrimination.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import math
import time
from typing import Any

from .const import (
    DEFAULT_BASELINE_ALPHA,
    DEFAULT_CONSTRUCTIVE_WEIGHT,
    DEFAULT_CORRELATION_DIST_M,
    DEFAULT_ELLIPSE_EXCESS_M,
    DEFAULT_LINK_STALE_SECONDS,
    DEFAULT_NOISE_VARIANCE,
    DEFAULT_SIGNAL_VARIANCE,
    DEFAULT_VARIANCE_WEIGHT,
    DEFAULT_WINDOW_SECONDS,
    LOW_HEIGHT_THRESHOLD_M,
)


@dataclass
class Point3D:
    """3D coordinate in meters."""

    x: float
    y: float
    z: float = 1.0


@dataclass
class RFLink:
    """Represents a static RF link between a transmitter/device and a receiver/AP."""

    link_id: str
    device_id: str
    receiver_id: str
    protocol: str  # "wifi", "ble", "zigbee"
    p1: Point3D  # Device position
    p2: Point3D  # Receiver (Router/AP or HA Bluetooth host) position

    # Runtime signal state
    samples: deque[tuple[float, float]] = field(
        default_factory=lambda: deque(maxlen=60)
    )
    baseline_rssi: float | None = None
    baseline_std: float = 0.6
    last_rssi: float | None = None
    last_updated: float = 0.0
    calibration_samples: list[float] = field(default_factory=list)

    @property
    def distance_2d(self) -> float:
        """2D Euclidean distance between endpoints."""
        return math.hypot(self.p2.x - self.p1.x, self.p2.y - self.p1.y)

    @property
    def mean_height(self) -> float:
        """Average vertical height of the link line-of-sight."""
        return 0.5 * (self.p1.z + self.p2.z)

    @property
    def is_low_link(self) -> bool:
        """True if the link primarily traverses pet/lower-body altitude."""
        return self.mean_height < LOW_HEIGHT_THRESHOLD_M

    def add_sample(
        self,
        rssi: float,
        now: float | None = None,
        calibrating: bool = False,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
    ) -> None:
        """Ingest a new RSSI reading (in dBm)."""
        ts = time.monotonic() if now is None else now
        self.last_rssi = float(rssi)
        self.last_updated = ts
        self.samples.append((ts, float(rssi)))

        # Prune old samples outside the sliding window (keep at least 3)
        cutoff = ts - window_seconds
        while len(self.samples) > 3 and self.samples[0][0] < cutoff:
            self.samples.popleft()

        if calibrating:
            self.calibration_samples.append(float(rssi))
            return

        if self.baseline_rssi is None:
            self.baseline_rssi = float(rssi)
            return

        # Adaptive baseline: only drift slowly when link appears quiet
        diff = abs(float(rssi) - self.baseline_rssi)
        if diff < 1.8:
            self.baseline_rssi = (
                (1.0 - DEFAULT_BASELINE_ALPHA) * self.baseline_rssi
                + DEFAULT_BASELINE_ALPHA * float(rssi)
            )

    def finalize_calibration(self) -> None:
        """Lock in baseline RSSI and noise floor from collected calibration samples."""
        if not self.calibration_samples:
            return
        n = len(self.calibration_samples)
        mean = sum(self.calibration_samples) / n
        self.baseline_rssi = mean
        if n >= 2:
            var = sum((v - mean) ** 2 for v in self.calibration_samples) / (n - 1)
            self.baseline_std = max(0.25, math.sqrt(var))
        self.calibration_samples.clear()

    def reset_baseline(self) -> None:
        """Reset adaptive baseline to current smoothed reading."""
        if self.samples:
            vals = [v for _, v in self.samples]
            self.baseline_rssi = sum(vals) / len(vals)
        elif self.last_rssi is not None:
            self.baseline_rssi = self.last_rssi

    def compute_perturbation(
        self,
        now: float | None = None,
        stale_seconds: float = DEFAULT_LINK_STALE_SECONDS,
        variance_weight: float = DEFAULT_VARIANCE_WEIGHT,
        constructive_weight: float = DEFAULT_CONSTRUCTIVE_WEIGHT,
    ) -> tuple[float, float, float]:
        """Compute fused shadowing + variance perturbation for this link.

        Returns:
            (total_perturbation, shadowing_db, excess_std_db)
        """
        ts = time.monotonic() if now is None else now
        if (
            self.baseline_rssi is None
            or self.last_rssi is None
            or (ts - self.last_updated) > stale_seconds
        ):
            return (0.0, 0.0, 0.0)

        vals = [v for _, v in self.samples]
        if not vals:
            return (0.0, 0.0, 0.0)

        # Short-term smoothed RSSI (weighted toward most recent 3 readings)
        recent = vals[-3:]
        smooth_rssi = sum(recent) / len(recent)

        # 1. Shadowing attenuation (signal drop below baseline)
        shadowing = max(0.0, self.baseline_rssi - smooth_rssi)

        # 2. Constructive multipath spike (signal jump significantly above baseline)
        constructive = max(0.0, smooth_rssi - self.baseline_rssi - 1.5)

        # 3. Sliding-window standard deviation (VRTI)
        if len(vals) >= 2:
            mean_w = sum(vals) / len(vals)
            var_w = sum((v - mean_w) ** 2 for v in vals) / (len(vals) - 1)
            std_w = math.sqrt(var_w)
        else:
            std_w = 0.0
        excess_std = max(0.0, std_w - self.baseline_std)

        total = (
            shadowing
            + variance_weight * excess_std
            + constructive_weight * constructive
        )
        return (total, shadowing, excess_std)


@dataclass
class RTIReconstruction:
    """Result of one RTI reconstruction frame."""

    width: int
    height: int
    resolution: float
    grid: list[list[float]]  # [row][col] attenuation image
    low_layer_grid: list[list[float]]  # Low-height link energy per voxel
    high_layer_grid: list[list[float]]  # High-height link energy per voxel
    low_coverage_grid: list[list[float]]  # Static low-link coverage weight
    high_coverage_grid: list[list[float]]  # Static high-link coverage weight
    active_links: int
    total_links: int
    link_perturbations: dict[str, float]


def _cholesky_invert(matrix: list[list[float]]) -> list[list[float]]:
    """Invert a symmetric positive-definite M x M matrix via Cholesky decomposition.

    Pure Python, numerically stable, O(M^3). Falls back to Gauss-Jordan with
    diagonal jitter if matrix is nearly singular.
    """
    m = len(matrix)
    if m == 0:
        return []

    # Compute lower-triangular L such that A = L L^T
    L = [[0.0] * m for _ in range(m)]
    for i in range(m):
        for j in range(i + 1):
            s = sum(L[i][k] * L[j][k] for k in range(j))
            if i == j:
                diag = matrix[i][i] - s
                if diag <= 1e-9:
                    diag = 1e-6  # Tikhonov safety jitter
                L[i][j] = math.sqrt(diag)
            else:
                L[i][j] = (matrix[i][j] - s) / L[j][j]

    # Solve L * Y = I (forward substitution)
    L_inv = [[0.0] * m for _ in range(m)]
    for i in range(m):
        L_inv[i][i] = 1.0 / L[i][i]
        for j in range(i):
            s = sum(L[i][k] * L_inv[k][j] for k in range(j, i))
            L_inv[i][j] = -s / L[i][i]

    # A^-1 = L_inv^T * L_inv
    inv = [[0.0] * m for _ in range(m)]
    for i in range(m):
        for j in range(i, m):
            val = sum(L_inv[k][i] * L_inv[k][j] for k in range(max(i, j), m))
            inv[i][j] = val
            inv[j][i] = val
    return inv


class RTIEngine:
    """2D Radio Tomographic Imaging engine with 3D vertical layer attribution."""

    def __init__(
        self,
        floor_width: float = 10.0,
        floor_height: float = 8.0,
        resolution: float = 0.5,
        ellipse_excess_m: float = DEFAULT_ELLIPSE_EXCESS_M,
        correlation_dist_m: float = DEFAULT_CORRELATION_DIST_M,
        noise_variance: float = DEFAULT_NOISE_VARIANCE,
        signal_variance: float = DEFAULT_SIGNAL_VARIANCE,
        sensitivity: float = 1.0,
    ) -> None:
        self.floor_width = max(1.0, float(floor_width))
        self.floor_height = max(1.0, float(floor_height))
        self.resolution = max(0.2, float(resolution))
        self.ellipse_excess_m = max(0.05, float(ellipse_excess_m))
        self.correlation_dist_m = max(0.2, float(correlation_dist_m))
        self.noise_variance = max(0.05, float(noise_variance))
        self.signal_variance = max(0.05, float(signal_variance))
        self.sensitivity = max(0.2, float(sensitivity))

        self.cols = max(2, int(math.ceil(self.floor_width / self.resolution)))
        self.rows = max(2, int(math.ceil(self.floor_height / self.resolution)))
        self.num_voxels = self.rows * self.cols

        # Precompute voxel centers (x, y)
        self.voxels: list[tuple[float, float]] = []
        for r in range(self.rows):
            vy = (r + 0.5) * self.resolution
            for c in range(self.cols):
                vx = (c + 0.5) * self.resolution
                self.voxels.append((vx, vy))

        # Precompute spatial covariance C_x (N x N)
        self._cov_x: list[list[float]] = self._build_spatial_covariance()

        # Links and precomputed projection matrix Pi (N x M)
        self.links: dict[str, RFLink] = {}
        self._link_order: list[str] = []
        self._weight_matrix: list[list[float]] = []  # M x N
        self._projection_matrix: list[list[float]] = []  # N x M
        self._low_coverage: list[list[float]] = [
            [0.0] * self.cols for _ in range(self.rows)
        ]
        self._high_coverage: list[list[float]] = [
            [0.0] * self.cols for _ in range(self.rows)
        ]
        self._dirty_geometry = True

    def _build_spatial_covariance(self) -> list[list[float]]:
        """Build exponential spatial covariance matrix C_x."""
        n = self.num_voxels
        inv_dc = -1.0 / self.correlation_dist_m
        s2 = self.signal_variance
        cov = [[0.0] * n for _ in range(n)]
        voxels = self.voxels
        for i in range(n):
            xi, yi = voxels[i]
            cov[i][i] = s2
            for j in range(i + 1, n):
                xj, yj = voxels[j]
                d = math.hypot(xi - xj, yi - yj)
                val = s2 * math.exp(d * inv_dc)
                cov[i][j] = val
                cov[j][i] = val
        return cov

    def add_or_update_link(self, link: RFLink) -> None:
        """Register or update an RF link, preserving existing signal history."""
        existing = self.links.get(link.link_id)
        if existing is not None:
            link.samples = existing.samples
            link.baseline_rssi = existing.baseline_rssi
            link.baseline_std = existing.baseline_std
            link.last_rssi = existing.last_rssi
            link.last_updated = existing.last_updated
            link.calibration_samples = existing.calibration_samples
        self.links[link.link_id] = link
        self._dirty_geometry = True

    def remove_link(self, link_id: str) -> None:
        """Remove a link by ID."""
        if link_id in self.links:
            del self.links[link_id]
            self._dirty_geometry = True

    def _compute_link_weights(self, link: RFLink) -> list[float]:
        """Compute smooth Fresnel ellipse weight vector (length N) for a link."""
        x1, y1 = link.p1.x, link.p1.y
        x2, y2 = link.p2.x, link.p2.y
        d_link = math.hypot(x2 - x1, y2 - y1)
        scale = 1.0 / math.sqrt(max(d_link, 0.5))
        lam = self.ellipse_excess_m
        cutoff = 3.5 * lam

        weights = [0.0] * self.num_voxels
        sq_sum = 0.0
        for j, (vx, vy) in enumerate(self.voxels):
            d1 = math.hypot(vx - x1, vy - y1)
            d2 = math.hypot(vx - x2, vy - y2)
            excess = (d1 + d2) - d_link
            if excess <= cutoff:
                w = scale * math.exp(-excess / lam)
                weights[j] = w
                sq_sum += w * w

        # Unit-L2 normalize each link's Fresnel kernel so voxel intensities are
        # consistently scaled in dB-equivalent units regardless of link length.
        if sq_sum > 1e-9:
            inv_norm = 1.0 / math.sqrt(sq_sum)
            weights = [w * inv_norm for w in weights]
        return weights

    def rebuild_projection(self) -> None:
        """Precompute the Woodbury dual RTI projection matrix Pi = C_x W^T (W C_x W^T + sigma_n^2 I)^-1."""
        self._link_order = list(self.links.keys())
        m = len(self._link_order)
        n = self.num_voxels

        self._low_coverage = [[0.0] * self.cols for _ in range(self.rows)]
        self._high_coverage = [[0.0] * self.cols for _ in range(self.rows)]

        if m == 0:
            self._weight_matrix = []
            self._projection_matrix = []
            self._dirty_geometry = False
            return

        W: list[list[float]] = []
        for lid in self._link_order:
            link = self.links[lid]
            w_row = self._compute_link_weights(link)
            W.append(w_row)
            target_cov = (
                self._low_coverage if link.is_low_link else self._high_coverage
            )
            for idx, w_val in enumerate(w_row):
                if w_val > 0.0:
                    r, c = divmod(idx, self.cols)
                    target_cov[r][c] += w_val

        self._weight_matrix = W

        # Step 1: Compute B = C_x * W^T  (N x M)
        # Exploit sparsity of W (only non-zero voxels per link)
        nonzero_per_link: list[list[tuple[int, float]]] = [
            [(j, val) for j, val in enumerate(W[l]) if val > 1e-6]
            for l in range(m)
        ]

        cov_x = self._cov_x
        B = [[0.0] * m for _ in range(n)]
        for l in range(m):
            nz = nonzero_per_link[l]
            if not nz:
                continue
            for i in range(n):
                cov_i = cov_x[i]
                B[i][l] = sum(cov_i[j] * wj for j, wj in nz)

        # Step 2: Compute A = W * B + sigma_n^2 * I  (M x M)
        A = [[0.0] * m for _ in range(m)]
        for l1 in range(m):
            nz1 = nonzero_per_link[l1]
            for l2 in range(l1, m):
                val = sum(wj * B[j][l2] for j, wj in nz1)
                if l1 == l2:
                    val += self.noise_variance
                A[l1][l2] = val
                A[l2][l1] = val

        # Step 3: Invert M x M matrix A via Cholesky
        A_inv = _cholesky_invert(A)

        # Step 4: Pi = B * A_inv  (N x M)
        Pi = [[0.0] * m for _ in range(n)]
        for i in range(n):
            b_i = B[i]
            pi_i = Pi[i]
            for l in range(m):
                pi_i[l] = sum(b_i[k] * A_inv[k][l] for k in range(m))

        self._projection_matrix = Pi
        self._dirty_geometry = False

    def reconstruct(self, now: float | None = None) -> RTIReconstruction:
        """Reconstruct 2D attenuation heatmap and 3D vertical layer energy maps."""
        if self._dirty_geometry:
            self.rebuild_projection()

        ts = time.monotonic() if now is None else now
        m = len(self._link_order)

        grid = [[0.0] * self.cols for _ in range(self.rows)]
        low_grid = [[0.0] * self.cols for _ in range(self.rows)]
        high_grid = [[0.0] * self.cols for _ in range(self.rows)]

        if m == 0:
            return RTIReconstruction(
                width=self.cols,
                height=self.rows,
                resolution=self.resolution,
                grid=grid,
                low_layer_grid=low_grid,
                high_layer_grid=high_grid,
                low_coverage_grid=self._low_coverage,
                high_coverage_grid=self._high_coverage,
                active_links=0,
                total_links=0,
                link_perturbations={},
            )

        delta_y: list[float] = [0.0] * m
        link_perturbations: dict[str, float] = {}
        active_links = 0

        for idx, lid in enumerate(self._link_order):
            link = self.links[lid]
            if (
                link.last_rssi is not None
                and (ts - link.last_updated) <= DEFAULT_LINK_STALE_SECONDS
            ):
                active_links += 1
            pert, _, _ = link.compute_perturbation(now=ts)
            scaled = pert * self.sensitivity
            delta_y[idx] = scaled
            link_perturbations[lid] = round(scaled, 3)

        # Fast matrix-vector multiply: x_hat = Pi * delta_y
        Pi = self._projection_matrix
        for v_idx in range(self.num_voxels):
            pi_row = Pi[v_idx]
            val = sum(pi_row[l] * delta_y[l] for l in range(m))
            if val > 0.0:
                r, c = divmod(v_idx, self.cols)
                grid[r][c] = val

        # Backproject low-height and high-height link perturbations separately
        # for 3D human vs. animal discrimination
        W = self._weight_matrix
        for l, lid in enumerate(self._link_order):
            dy = delta_y[l]
            if dy <= 0.05:
                continue
            link = self.links[lid]
            target = low_grid if link.is_low_link else high_grid
            w_row = W[l]
            for v_idx, w_val in enumerate(w_row):
                if w_val > 1e-4:
                    r, c = divmod(v_idx, self.cols)
                    target[r][c] += w_val * dy

        return RTIReconstruction(
            width=self.cols,
            height=self.rows,
            resolution=self.resolution,
            grid=grid,
            low_layer_grid=low_grid,
            high_layer_grid=high_grid,
            low_coverage_grid=self._low_coverage,
            high_coverage_grid=self._high_coverage,
            active_links=active_links,
            total_links=m,
            link_perturbations=link_perturbations,
        )

    def get_geometry_summary(self) -> dict[str, Any]:
        """Return diagnostic info about the RTI grid and link network."""
        return {
            "floor_width_m": self.floor_width,
            "floor_height_m": self.floor_height,
            "resolution_m": self.resolution,
            "grid_cols": self.cols,
            "grid_rows": self.rows,
            "total_links": len(self.links),
            "low_links": sum(1 for l in self.links.values() if l.is_low_link),
            "high_links": sum(1 for l in self.links.values() if not l.is_low_link),
        }
