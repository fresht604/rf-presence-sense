"""Pure-Python Radio Tomographic Imaging (RTI + VRTI) Engine.

Implements device-free passive 3D localization using existing Wi-Fi & Bluetooth RSSI
links via:
  1. Smooth Fresnel-zone elliptical link weighting (Wilson & Patwari, IEEE TMC 2010)
  2. Dual-mode Shadowing + Variance-Based RTI (VRTI, IEEE TMC 2011)
  3. Woodbury dual-form Tikhonov-regularized inversion (O(M^3) in pure Python,
     zero external dependencies) with on-the-fly sparse covariance projection
     (eliminating permanent O(N^2) RAM matrices)
  4. Presence-aware adaptive baseline tracking with empty-room calibration & persistence
  5. Voxel-accurate 3D vertical height interpolation z(x, y) along every link for
     precise human vs. animal discrimination.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import logging
import math
from operator import mul
import threading
import time
from typing import Any

from .const import (
    DEFAULT_BASELINE_ALPHA,
    DEFAULT_CONSTRUCTIVE_WEIGHT,
    DEFAULT_CORRELATION_DIST_M,
    DEFAULT_ELLIPSE_EXCESS_M,
    DEFAULT_LINK_STALE_SECONDS,
    DEFAULT_LONG_WINDOW_SECONDS,
    DEFAULT_NOISE_VARIANCE,
    DEFAULT_SIGNAL_VARIANCE,
    DEFAULT_VARIANCE_WEIGHT,
    DEFAULT_WINDOW_SECONDS,
    LINK_BASELINE_FREEZE_SHADOWING_DB,
    LOW_HEIGHT_THRESHOLD_M,
    MAX_CALIBRATION_SAMPLES,
    MAX_FINGERPRINTS,
    MAX_LINKS,
    MAX_VOXELS,
    PROTOCOL_STALE_SECONDS,
    REBUILD_COOLDOWN_SECONDS,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class RFFingerprint:
    """Empirical RF perturbation fingerprint recorded at a known (x, y) location."""

    x: float
    y: float
    vector: dict[str, float]
    room_id: str = ""
    label: str = ""
    phone_rssi: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize fingerprint for persistent storage."""
        data: dict[str, Any] = {
            "x": round(float(self.x), 2),
            "y": round(float(self.y), 2),
            "room_id": str(self.room_id),
            "label": str(self.label),
            "vector": {
                str(k): round(float(v), 3)
                for k, v in self.vector.items()
                if float(v) > 0.05
            },
        }
        if self.phone_rssi is not None:
            data["phone_rssi"] = round(float(self.phone_rssi), 2)
        return data


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
    long_samples: deque[tuple[float, float]] = field(
        default_factory=lambda: deque(maxlen=90)
    )
    baseline_rssi: float | None = None
    baseline_std: float = 0.6
    last_rssi: float | None = None
    last_updated: float = 0.0
    calibration_samples: deque[float] = field(
        default_factory=lambda: deque(maxlen=MAX_CALIBRATION_SAMPLES)
    )

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

    @property
    def stale_seconds(self) -> float:
        """Protocol-appropriate silence timeout before a link is considered stale."""
        return PROTOCOL_STALE_SECONDS.get(self.protocol, DEFAULT_LINK_STALE_SECONDS)

    def height_at(self, vx: float, vy: float) -> float:
        """Interpolate exact 3D link height z(x, y) at closest point on segment p1->p2."""
        dx = self.p2.x - self.p1.x
        dy = self.p2.y - self.p1.y
        seg_len_sq = dx * dx + dy * dy
        if seg_len_sq <= 1e-9:
            return self.mean_height
        t = ((vx - self.p1.x) * dx + (vy - self.p1.y) * dy) / seg_len_sq
        t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
        return self.p1.z + t * (self.p2.z - self.p1.z)

    def add_sample(
        self,
        rssi: float,
        now: float | None = None,
        calibrating: bool = False,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        freeze_baseline: bool = False,
    ) -> None:
        """Ingest a new RSSI reading (in dBm)."""
        ts = time.monotonic() if now is None else now
        val = float(rssi)
        self.last_rssi = val
        self.last_updated = ts
        self.samples.append((ts, val))
        self.long_samples.append((ts, val))

        # Prune old samples outside the sliding window (keep at least 3)
        cutoff = ts - window_seconds
        while len(self.samples) > 3 and self.samples[0][0] < cutoff:
            self.samples.popleft()

        long_cutoff = ts - DEFAULT_LONG_WINDOW_SECONDS
        while len(self.long_samples) > 3 and self.long_samples[0][0] < long_cutoff:
            self.long_samples.popleft()

        if calibrating:
            self.calibration_samples.append(val)
            return

        if self.baseline_rssi is None:
            self.baseline_rssi = val
            return

        # Presence-aware baseline: skip drift when targets/presence freeze adaptation
        if freeze_baseline:
            return

        # Per-link baseline freeze: never adapt baseline while shadowed >= 1.2 dB
        # (protects stationary/sleeping persons even before a track is confirmed)
        inst_shadowing = self.baseline_rssi - val
        recent_vals = [v for _, v in list(self.samples)[-3:]]
        smooth_shadowing = (
            self.baseline_rssi - (sum(recent_vals) / len(recent_vals))
            if recent_vals
            else inst_shadowing
        )
        if (
            inst_shadowing >= LINK_BASELINE_FREEZE_SHADOWING_DB
            or smooth_shadowing >= LINK_BASELINE_FREEZE_SHADOWING_DB
        ):
            return

        # Adaptive baseline: only drift slowly when link appears quiet
        diff = abs(val - self.baseline_rssi)
        if diff < 1.8:
            self.baseline_rssi = (
                (1.0 - DEFAULT_BASELINE_ALPHA) * self.baseline_rssi
                + DEFAULT_BASELINE_ALPHA * val
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
        stale_seconds: float | None = None,
        variance_weight: float = DEFAULT_VARIANCE_WEIGHT,
        constructive_weight: float = DEFAULT_CONSTRUCTIVE_WEIGHT,
    ) -> tuple[float, float, float]:
        """Compute fused shadowing + variance perturbation for this link.

        Returns:
            (total_perturbation, shadowing_db, excess_std_db)
        """
        ts = time.monotonic() if now is None else now
        timeout = self.stale_seconds if stale_seconds is None else stale_seconds
        if (
            self.baseline_rssi is None
            or self.last_rssi is None
            or (ts - self.last_updated) > timeout
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

        # 3. Sliding-window standard deviation (12s VRTI + 30s micro-motion/breathing)
        if len(vals) >= 2:
            mean_w = sum(vals) / len(vals)
            var_w = sum((v - mean_w) ** 2 for v in vals) / (len(vals) - 1)
            std_w = math.sqrt(var_w)
        else:
            std_w = 0.0

        long_vals = [
            v
            for t_s, v in self.long_samples
            if (ts - t_s) <= DEFAULT_LONG_WINDOW_SECONDS
        ]
        if len(long_vals) >= 3:
            mean_l = sum(long_vals) / len(long_vals)
            var_l = sum((v - mean_l) ** 2 for v in long_vals) / (len(long_vals) - 1)
            std_l = math.sqrt(var_l)
        else:
            std_l = 0.0

        excess_std = max(
            max(0.0, std_w - self.baseline_std),
            0.85 * max(0.0, std_l - self.baseline_std),
        )

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

    Pure Python, numerically stable, O(M^3). Adds diagonal Tikhonov jitter
    if the matrix is nearly singular.
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
    """2D Radio Tomographic Imaging engine with voxel-accurate 3D height attribution."""

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

        orig_res = self.resolution
        self.cols = max(2, int(math.ceil(self.floor_width / self.resolution)))
        self.rows = max(2, int(math.ceil(self.floor_height / self.resolution)))
        while self.rows * self.cols > MAX_VOXELS:
            scale = math.sqrt((self.rows * self.cols) / MAX_VOXELS) * 1.02
            self.resolution = max(
                round(self.resolution * scale, 3), self.resolution + 0.01
            )
            self.cols = max(2, int(math.ceil(self.floor_width / self.resolution)))
            self.rows = max(2, int(math.ceil(self.floor_height / self.resolution)))

        if self.resolution != orig_res:
            _LOGGER.warning(
                "Floorplan %.1fm x %.1fm at %.2fm exceeds MAX_VOXELS=%d; "
                "auto-adjusted resolution to %.3fm (%dx%d = %d voxels)",
                self.floor_width,
                self.floor_height,
                orig_res,
                MAX_VOXELS,
                self.resolution,
                self.cols,
                self.rows,
                self.rows * self.cols,
            )

        self.num_voxels = self.rows * self.cols

        # Precompute voxel centers (x, y) — O(N) memory only (no N x N C_x matrix!)
        self.voxels: list[tuple[float, float]] = []
        for r in range(self.rows):
            vy = (r + 0.5) * self.resolution
            for c in range(self.cols):
                vx = (c + 0.5) * self.resolution
                self.voxels.append((vx, vy))

        # Thread-safety lock protecting link dict mutations and snapshots
        self._lock = threading.RLock()
        self._geometry_version: int = 0
        self._max_links_warned: bool = False

        # Links and precomputed projection matrix Pi (N x M)
        self.links: dict[str, RFLink] = {}
        self._link_order: list[str] = []
        # M sparse rows of (voxel_idx, weight, is_low_voxel)
        self._sparse_weights: list[list[tuple[int, float, bool]]] = []
        self._projection_matrix: list[list[float]] = []  # N x M
        self._low_coverage: list[list[float]] = [
            [0.0] * self.cols for _ in range(self.rows)
        ]
        self._high_coverage: list[list[float]] = [
            [0.0] * self.cols for _ in range(self.rows)
        ]
        self._dirty_geometry = True
        self._force_rebuild = True
        self._last_rebuild_ts: float = 0.0
        self._rebuild_cooldown: float = REBUILD_COOLDOWN_SECONDS

        # Room mask (True = inside any configured room; None = unmasked)
        self._voxel_mask: list[bool] | None = None
        # Empirical RF fingerprints for hybrid RTI + fingerprinting (bounded by MAX_FINGERPRINTS)
        self._fingerprints: list[RFFingerprint] = []

    def set_room_mask(self, rooms: list[Any] | None, enabled: bool = True) -> None:
        """Build voxel mask covering only configured rooms (suppresses neighbor ghosts)."""
        if not enabled or not rooms:
            with self._lock:
                self._voxel_mask = None
            return
        mask = [
            any(room.contains(vx, vy) for room in rooms)
            for vx, vy in self.voxels
        ]
        # Only activate mask if at least one voxel falls inside a room
        with self._lock:
            self._voxel_mask = mask if any(mask) else None

    def add_fingerprint(self, fp: RFFingerprint) -> None:
        """Thread-safely store a calibration fingerprint (bounded to MAX_FINGERPRINTS & top 24 links)."""
        if not fp.vector:
            return
        if not (math.isfinite(fp.x) and math.isfinite(fp.y)):
            return
        clean_vec = dict(
            sorted(
                (
                    (str(k), round(float(v), 3))
                    for k, v in fp.vector.items()
                    if math.isfinite(float(v)) and float(v) > 0.05
                ),
                key=lambda item: item[1],
                reverse=True,
            )[:24]
        )
        if not clean_vec:
            return
        bounded_fp = RFFingerprint(
            x=float(fp.x),
            y=float(fp.y),
            room_id=str(fp.room_id),
            label=str(fp.label),
            vector=clean_vec,
            phone_rssi=(
                float(fp.phone_rssi)
                if fp.phone_rssi is not None and math.isfinite(float(fp.phone_rssi))
                else None
            ),
        )
        with self._lock:
            self._fingerprints.append(bounded_fp)
            if len(self._fingerprints) > MAX_FINGERPRINTS:
                self._fingerprints = self._fingerprints[-MAX_FINGERPRINTS:]

    def clear_fingerprints(self) -> int:
        """Thread-safely clear all stored RF fingerprints; returns count removed."""
        with self._lock:
            n = len(self._fingerprints)
            self._fingerprints.clear()
            return n

    @property
    def fingerprint_count(self) -> int:
        """Current count of stored RF calibration fingerprints."""
        with self._lock:
            return len(self._fingerprints)

    def snapshot_fingerprints_xy(self, limit: int = 80) -> list[list[float]]:
        """Return compact [[x, y], ...] coordinates of stored fingerprints."""
        with self._lock:
            return [
                [round(fp.x, 1), round(fp.y, 1)]
                for fp in self._fingerprints[-limit:]
            ]

    def export_fingerprints(self) -> list[dict[str, Any]]:
        """Thread-safely export fingerprints for persistent storage."""
        with self._lock:
            return [fp.to_dict() for fp in self._fingerprints]

    def import_fingerprints(self, raw_items: list[dict[str, Any]]) -> int:
        """Thread-safely restore persisted RF fingerprints (capped at MAX_FINGERPRINTS)."""
        loaded: list[RFFingerprint] = []
        for item in raw_items[-MAX_FINGERPRINTS:]:
            if not isinstance(item, dict):
                continue
            vec = item.get("vector")
            if not isinstance(vec, dict) or not vec:
                continue
            try:
                fx = float(item["x"])
                fy = float(item["y"])
                if not (math.isfinite(fx) and math.isfinite(fy)):
                    continue
                clean_vec = dict(
                    sorted(
                        (
                            (str(k), round(float(v), 3))
                            for k, v in vec.items()
                            if math.isfinite(float(v)) and float(v) > 0.05
                        ),
                        key=lambda kv: kv[1],
                        reverse=True,
                    )[:24]
                )
                if not clean_vec:
                    continue
                phone_val = item.get("phone_rssi")
                parsed_phone = (
                    float(phone_val)
                    if phone_val is not None and math.isfinite(float(phone_val))
                    else None
                )
                loaded.append(
                    RFFingerprint(
                        x=fx,
                        y=fy,
                        room_id=str(item.get("room_id", "")),
                        label=str(item.get("label", "")),
                        vector=clean_vec,
                        phone_rssi=parsed_phone,
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        with self._lock:
            self._fingerprints = loaded
        return len(loaded)

    def sample_perturbation_vector(self, now: float | None = None) -> dict[str, float]:
        """Thread-safely capture current raw link perturbation vector for fingerprinting."""
        ts = time.monotonic() if now is None else now
        vec: dict[str, float] = {}
        with self._lock:
            for lid, link in self.links.items():
                pert, _, _ = link.compute_perturbation(now=ts)
                if pert > 0.05:
                    vec[lid] = round(pert, 3)
        return vec

    def add_or_update_link(self, link: RFLink, *, dynamic: bool = False) -> bool:
        """Register or update an RF link, preserving existing signal history.

        Returns False if the link was rejected due to MAX_LINKS capacity limit.
        When ``dynamic=True`` (auto-discovered roaming/proxy link), projection rebuilds
        respect ``_rebuild_cooldown`` instead of re-inverting on every tick.
        """
        with self._lock:
            existing = self.links.get(link.link_id)
            if existing is None and len(self.links) >= MAX_LINKS:
                if not self._max_links_warned:
                    _LOGGER.warning(
                        "RF link limit reached (MAX_LINKS=%d); ignoring additional links",
                        MAX_LINKS,
                    )
                    self._max_links_warned = True
                return False

            if existing is not None:
                link.samples = existing.samples
                link.long_samples = existing.long_samples
                if link.baseline_rssi is None:
                    link.baseline_rssi = existing.baseline_rssi
                    link.baseline_std = existing.baseline_std
                link.last_rssi = existing.last_rssi
                link.last_updated = existing.last_updated
                link.calibration_samples = existing.calibration_samples
            self.links[link.link_id] = link
            self._dirty_geometry = True
            if not dynamic:
                self._force_rebuild = True
            self._geometry_version += 1
            return True

    def remove_link(self, link_id: str) -> None:
        """Remove a link by ID."""
        with self._lock:
            if link_id in self.links:
                del self.links[link_id]
                self._dirty_geometry = True
                self._force_rebuild = True
                self._geometry_version += 1
                if len(self.links) < MAX_LINKS:
                    self._max_links_warned = False

    def clear_links_except(self, valid_link_ids: set[str]) -> None:
        """Remove links that are no longer in the configured device set."""
        with self._lock:
            stale = [lid for lid in self.links if lid not in valid_link_ids]
            for lid in stale:
                del self.links[lid]
            if stale:
                self._dirty_geometry = True
                self._force_rebuild = True
                self._geometry_version += 1
                if len(self.links) < MAX_LINKS:
                    self._max_links_warned = False

    def ingest_sample(
        self,
        link_id: str,
        rssi: float,
        now: float | None = None,
        calibrating: bool = False,
        freeze_baseline: bool = False,
    ) -> bool:
        """Thread-safely ingest a live RSSI sample into an existing link."""
        with self._lock:
            link = self.links.get(link_id)
            if link is None:
                return False
            link.add_sample(
                rssi,
                now=now,
                calibrating=calibrating,
                freeze_baseline=freeze_baseline,
            )
            return True

    def clear_calibration_samples(self) -> None:
        """Thread-safely clear calibration buffers on all links."""
        with self._lock:
            for link in self.links.values():
                link.calibration_samples.clear()

    def finalize_calibration(self) -> None:
        """Thread-safely finalize baseline calibration on all links."""
        with self._lock:
            for link in self.links.values():
                link.finalize_calibration()

    def reset_baselines(self) -> None:
        """Thread-safely reset adaptive baselines on all links."""
        with self._lock:
            for link in self.links.values():
                link.reset_baseline()

    def export_baselines(self) -> dict[str, dict[str, float]]:
        """Thread-safely export calibrated link baselines for persistent storage."""
        with self._lock:
            return {
                lid: {
                    "baseline_rssi": round(link.baseline_rssi, 3),
                    "baseline_std": round(link.baseline_std, 3),
                }
                for lid, link in self.links.items()
                if link.baseline_rssi is not None
            }

    def import_baselines(self, baselines: dict[str, dict[str, Any]]) -> int:
        """Thread-safely restore persisted baselines onto matching links."""
        applied = 0
        with self._lock:
            for lid, vals in baselines.items():
                link = self.links.get(lid)
                if link is None or not isinstance(vals, dict):
                    continue
                b_rssi = vals.get("baseline_rssi")
                if b_rssi is not None:
                    link.baseline_rssi = float(b_rssi)
                    applied += 1
                b_std = vals.get("baseline_std")
                if b_std is not None:
                    link.baseline_std = max(0.25, float(b_std))
        return applied

    def snapshot_links(self) -> list[tuple[str, str, float, float, float, float]]:
        """Return an immutable 2D snapshot of link geometries (backward-compatible)."""
        with self._lock:
            return [
                (
                    lid,
                    link.protocol,
                    link.p1.x,
                    link.p1.y,
                    link.p2.x,
                    link.p2.y,
                )
                for lid, link in self.links.items()
            ]

    def snapshot_links_3d(
        self,
    ) -> list[tuple[str, str, float, float, float, float, float, float]]:
        """Return an immutable 3D snapshot of link endpoints (x1, y1, z1, x2, y2, z2)."""
        with self._lock:
            return [
                (
                    lid,
                    link.protocol,
                    link.p1.x,
                    link.p1.y,
                    link.p1.z,
                    link.p2.x,
                    link.p2.y,
                    link.p2.z,
                )
                for lid, link in self.links.items()
            ]

    def _compute_sparse_link_weights(
        self,
        x1: float,
        y1: float,
        z1: float,
        x2: float,
        y2: float,
        z2: float,
    ) -> list[tuple[int, float, bool]]:
        """Compute L2-normalized sparse Fresnel weights with per-voxel 3D height z(x, y).

        Returns list of (voxel_idx, normalized_weight, is_low_voxel).
        """
        dx_seg = x2 - x1
        dy_seg = y2 - y1
        dz_seg = z2 - z1
        seg_len_sq = dx_seg * dx_seg + dy_seg * dy_seg
        d_link = math.sqrt(seg_len_sq)
        scale = 1.0 / math.sqrt(max(d_link, 0.5))
        lam = self.ellipse_excess_m
        cutoff = 3.5 * lam
        mean_z = 0.5 * (z1 + z2)

        raw_nz: list[tuple[int, float, bool]] = []
        sq_sum = 0.0
        for j, (vx, vy) in enumerate(self.voxels):
            d1 = math.hypot(vx - x1, vy - y1)
            d2 = math.hypot(vx - x2, vy - y2)
            excess = (d1 + d2) - d_link
            if excess <= cutoff:
                w = scale * math.exp(-excess / lam)
                if seg_len_sq > 1e-9:
                    t = ((vx - x1) * dx_seg + (vy - y1) * dy_seg) / seg_len_sq
                    if t < 0.0:
                        t = 0.0
                    elif t > 1.0:
                        t = 1.0
                    z_voxel = z1 + t * dz_seg
                else:
                    z_voxel = mean_z
                raw_nz.append((j, w, z_voxel < LOW_HEIGHT_THRESHOLD_M))
                sq_sum += w * w

        # Unit-L2 normalize each link's Fresnel kernel so voxel intensities are
        # consistently scaled in dB-equivalent units regardless of link length.
        if sq_sum > 1e-9:
            inv_norm = 1.0 / math.sqrt(sq_sum)
            return [
                (j, w * inv_norm, is_low)
                for j, w, is_low in raw_nz
                if (w * inv_norm) > 1e-6
            ]
        return []

    def rebuild_projection(self, *, force: bool = False) -> bool:
        """Precompute the Woodbury dual RTI projection matrix Pi = C_x W^T (W C_x W^T + sigma_n^2 I)^-1.

        Computes B = C_x W^T on-the-fly over sparse non-zero voxels without
        allocating or storing an N x N spatial covariance matrix in RAM.
        Heavy math executes completely outside self._lock.

        Returns True if the projection matrix was rebuilt, False if debounced.
        """
        now_mono = time.monotonic()
        with self._lock:
            if (
                not force
                and not self._force_rebuild
                and self._projection_matrix
                and self._last_rebuild_ts > 0.0
                and (now_mono - self._last_rebuild_ts) < self._rebuild_cooldown
            ):
                return False
            geo_version = self._geometry_version
            link_order = list(self.links.keys())
            link_specs = [
                (
                    self.links[lid].p1.x,
                    self.links[lid].p1.y,
                    self.links[lid].p1.z,
                    self.links[lid].p2.x,
                    self.links[lid].p2.y,
                    self.links[lid].p2.z,
                )
                for lid in link_order
            ]

        m = len(link_order)
        n = self.num_voxels
        low_cov = [[0.0] * self.cols for _ in range(self.rows)]
        high_cov = [[0.0] * self.cols for _ in range(self.rows)]

        if m == 0:
            with self._lock:
                self._link_order = []
                self._sparse_weights = []
                self._projection_matrix = []
                self._low_coverage = low_cov
                self._high_coverage = high_cov
                self._last_rebuild_ts = time.monotonic()
                if self._geometry_version == geo_version:
                    self._dirty_geometry = False
                    self._force_rebuild = False
            return True

        # Step 1: Compute sparse Fresnel weights and voxel-exact 3D vertical coverage maps
        sparse_weights: list[list[tuple[int, float, bool]]] = []
        for x1, y1, z1, x2, y2, z2 in link_specs:
            nz = self._compute_sparse_link_weights(x1, y1, z1, x2, y2, z2)
            sparse_weights.append(nz)
            for idx, w_val, is_low in nz:
                r, c = divmod(idx, self.cols)
                if is_low:
                    low_cov[r][c] += w_val
                else:
                    high_cov[r][c] += w_val

        # Step 2: Compute B = C_x * W^T (N x M) on-the-fly via sparse inverted index
        # Maps each active voxel j -> [(link_idx, w_lj), ...], avoiding N x N storage.
        voxel_links: dict[int, list[tuple[int, float]]] = {}
        for l_idx, nz in enumerate(sparse_weights):
            for j, wj, _ in nz:
                voxel_links.setdefault(j, []).append((l_idx, wj))

        B = [[0.0] * m for _ in range(n)]
        inv_dc = -1.0 / self.correlation_dist_m
        cov_cutoff = 7.0 * self.correlation_dist_m  # exp(-7) < 0.001
        s2 = self.signal_variance
        voxels = self.voxels
        for j, l_weights in voxel_links.items():
            xj, yj = voxels[j]
            for i in range(n):
                xi, yi = voxels[i]
                dx = xi - xj
                if dx > cov_cutoff or dx < -cov_cutoff:
                    continue
                dy = yi - yj
                if dy > cov_cutoff or dy < -cov_cutoff:
                    continue
                d = math.hypot(dx, dy)
                if d > cov_cutoff:
                    continue
                c_ij = s2 * math.exp(d * inv_dc)
                b_i = B[i]
                for l_idx, wj in l_weights:
                    b_i[l_idx] += c_ij * wj

        # Step 3: Compute A = W * B + sigma_n^2 * I  (M x M)
        A = [[0.0] * m for _ in range(m)]
        for l1 in range(m):
            nz1 = sparse_weights[l1]
            for l2 in range(l1, m):
                val = sum(wj * B[j][l2] for j, wj, _ in nz1)
                if l1 == l2:
                    val += self.noise_variance
                A[l1][l2] = val
                A[l2][l1] = val

        # Step 4: Invert M x M matrix A via Cholesky
        A_inv = _cholesky_invert(A)

        # Step 5: Pi = B * A_inv  (N x M); A_inv is symmetric so row l == col l
        Pi = [
            [sum(map(mul, b_i, a_row)) for a_row in A_inv]
            for b_i in B
        ]

        # Step 6: Atomically commit projection state
        with self._lock:
            self._link_order = link_order
            self._sparse_weights = sparse_weights
            self._projection_matrix = Pi
            self._low_coverage = low_cov
            self._high_coverage = high_cov
            self._last_rebuild_ts = time.monotonic()
            if self._geometry_version == geo_version:
                self._dirty_geometry = False
                self._force_rebuild = False
        return True

    def reconstruct(self, now: float | None = None) -> RTIReconstruction:
        """Reconstruct 2D attenuation heatmap and voxel-accurate 3D vertical layer energy maps."""
        if self._dirty_geometry:
            self.rebuild_projection(force=False)

        ts = time.monotonic() if now is None else now

        # Snapshot projection matrices and compute link perturbations under lock
        with self._lock:
            link_order = self._link_order
            Pi = self._projection_matrix
            sparse_weights = self._sparse_weights
            low_cov = self._low_coverage
            high_cov = self._high_coverage
            voxel_mask = self._voxel_mask
            fingerprints = list(self._fingerprints) if self._fingerprints else []
            m = len(link_order)

            if m == 0:
                grid = [[0.0] * self.cols for _ in range(self.rows)]
                low_grid = [[0.0] * self.cols for _ in range(self.rows)]
                high_grid = [[0.0] * self.cols for _ in range(self.rows)]
                return RTIReconstruction(
                    width=self.cols,
                    height=self.rows,
                    resolution=self.resolution,
                    grid=grid,
                    low_layer_grid=low_grid,
                    high_layer_grid=high_grid,
                    low_coverage_grid=low_cov,
                    high_coverage_grid=high_cov,
                    active_links=0,
                    total_links=0,
                    link_perturbations={},
                )

            delta_y: list[float] = [0.0] * m
            link_perturbations: dict[str, float] = {}
            active_links = 0

            for idx, lid in enumerate(link_order):
                link = self.links.get(lid)
                if link is None:
                    continue
                if (
                    link.last_rssi is not None
                    and (ts - link.last_updated) <= link.stale_seconds
                ):
                    active_links += 1
                pert, _, _ = link.compute_perturbation(now=ts)
                scaled = pert * self.sensitivity
                delta_y[idx] = scaled
                if scaled > 0.0:
                    link_perturbations[lid] = round(scaled, 3)

        # Heavy matrix-vector multiply & voxel-accurate 3D backprojection execute lock-free
        grid = [[0.0] * self.cols for _ in range(self.rows)]
        low_grid = [[0.0] * self.cols for _ in range(self.rows)]
        high_grid = [[0.0] * self.cols for _ in range(self.rows)]

        for v_idx in range(self.num_voxels):
            if voxel_mask is not None and not voxel_mask[v_idx]:
                continue
            val = sum(map(mul, Pi[v_idx], delta_y))
            if val > 0.0:
                r, c = divmod(v_idx, self.cols)
                grid[r][c] = val

        # Backproject voxel-level low-height (<0.9m) and high-height (>=0.9m) energy
        for l_idx in range(m):
            dy = delta_y[l_idx]
            if dy <= 0.05:
                continue
            for v_idx, w_val, is_low in sparse_weights[l_idx]:
                if w_val > 1e-4:
                    if voxel_mask is not None and not voxel_mask[v_idx]:
                        continue
                    r, c = divmod(v_idx, self.cols)
                    if is_low:
                        low_grid[r][c] += w_val * dy
                    else:
                        high_grid[r][c] += w_val * dy

        # Hybrid RTI + RF-Fingerprinting fusion (cosine + L2 similarity boost)
        if fingerprints and link_perturbations:
            curr_norm_sq = sum(
                v * v for v in link_perturbations.values() if math.isfinite(v)
            )
            if math.isfinite(curr_norm_sq) and curr_norm_sq > 0.12:  # curr_norm > 0.35
                curr_norm = math.sqrt(curr_norm_sq)
                sens = max(0.2, self.sensitivity)
                scored_fps: list[tuple[float, RFFingerprint]] = []
                for fp in fingerprints:
                    fp_vec = fp.vector
                    fp_norm_sq = sum(
                        v * v for v in fp_vec.values() if math.isfinite(v)
                    )
                    if not math.isfinite(fp_norm_sq) or fp_norm_sq <= 1e-6:
                        continue
                    fp_norm = math.sqrt(fp_norm_sq)
                    dot = 0.0
                    l2_sq = 0.0
                    all_keys = set(link_perturbations.keys()) | set(fp_vec.keys())
                    for k in all_keys:
                        cv = link_perturbations.get(k, 0.0)
                        fv = fp_vec.get(k, 0.0) * sens
                        dot += cv * fv
                        diff = cv - fv
                        l2_sq += diff * diff
                    denom = max(1e-9, curr_norm * fp_norm * sens)
                    cos_sim = max(0.0, min(1.0, dot / denom))
                    l2_denom = max(2.5, 0.7 * (curr_norm + fp_norm * sens))
                    l2_sim = math.exp(-min(40.0, math.sqrt(max(0.0, l2_sq)) / l2_denom))
                    sim = 0.6 * cos_sim + 0.4 * l2_sim
                    if math.isfinite(sim) and sim >= 0.45:
                        scored_fps.append((sim, fp))

                if scored_fps:
                    scored_fps.sort(key=lambda item: item[0], reverse=True)
                    sigma_sq = 0.85 * 0.85
                    boost_scale = min(2.8, curr_norm * 0.48)
                    for sim, fp in scored_fps[:3]:
                        amp = sim * boost_scale
                        fpx, fpy = fp.x, fp.y
                        for v_idx, (vx, vy) in enumerate(self.voxels):
                            if voxel_mask is not None and not voxel_mask[v_idx]:
                                continue
                            d2 = (vx - fpx) ** 2 + (vy - fpy) ** 2
                            if d2 <= 4.5:  # within ~2.1m radius
                                r, c = divmod(v_idx, self.cols)
                                grid[r][c] += amp * math.exp(-d2 / (2.0 * sigma_sq))

        return RTIReconstruction(
            width=self.cols,
            height=self.rows,
            resolution=self.resolution,
            grid=grid,
            low_layer_grid=low_grid,
            high_layer_grid=high_grid,
            low_coverage_grid=low_cov,
            high_coverage_grid=high_cov,
            active_links=active_links,
            total_links=m,
            link_perturbations=link_perturbations,
        )

    def get_geometry_summary(self) -> dict[str, Any]:
        """Return diagnostic info about the RTI grid and link network."""
        with self._lock:
            total = len(self.links)
            low = sum(1 for l in self.links.values() if l.is_low_link)
        return {
            "floor_width_m": self.floor_width,
            "floor_height_m": self.floor_height,
            "resolution_m": self.resolution,
            "grid_cols": self.cols,
            "grid_rows": self.rows,
            "num_voxels": self.num_voxels,
            "total_links": total,
            "low_links": low,
            "high_links": total - low,
        }
