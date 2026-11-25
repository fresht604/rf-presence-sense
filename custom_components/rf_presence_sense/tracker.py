"""Multi-target 2D Kalman Tracker and 3D Human/Animal Classifier.

Extracts sub-voxel peaks from the RTI attenuation image, tracks multiple moving
or stationary targets with a 2D Constant-Velocity Kalman Filter, classifies each
target as 'human' or 'animal' using 3D vertical link energy & RF cross-section
heuristics, and assigns targets to configured rooms.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any

from .const import (
    ANIMAL_ATTENUATION_THRESHOLD,
    DETECTION_THRESHOLD_BASE,
    HUMAN_ATTENUATION_THRESHOLD,
    TARGET_TYPE_ANIMAL,
    TARGET_TYPE_HUMAN,
    TRACKER_MAX_ASSOC_DIST_M,
    TRACKER_MAX_MISSED,
    TRACKER_MAX_TRACKS,
    TRACKER_MIN_HITS,
)
from .rti_engine import RTIReconstruction

MAX_TRACKS = TRACKER_MAX_TRACKS


def _sigmoid(x: float) -> float:
    """Numerically safe logistic sigmoid (prevents math.exp OverflowError)."""
    clamped = max(-40.0, min(40.0, x))
    return 1.0 / (1.0 + math.exp(-clamped))


@dataclass
class RoomZone:
    """Axis-aligned rectangular room zone in floorplan coordinates (meters)."""

    room_id: str
    name: str
    x_min: float
    y_min: float
    x_max: float
    y_max: float

    def contains(self, x: float, y: float) -> bool:
        """Return True if point (x, y) lies inside this room."""
        return self.x_min <= x <= self.x_max and self.y_min <= y <= self.y_max


@dataclass
class DetectionPeak:
    """Single detected peak in an RTI frame."""

    x: float
    y: float
    intensity: float
    footprint_area_m2: float
    low_energy: float
    high_energy: float
    low_coverage: float
    high_coverage: float


@dataclass
class TrackedTarget:
    """State of a tracked human or animal target."""

    track_id: int
    x: float
    y: float
    vx: float = 0.0
    vy: float = 0.0
    # 4x4 diagonal-dominant covariance approximation for lightweight Kalman filter
    p_pos: float = 0.8
    p_vel: float = 0.5
    hits: int = 1
    missed: int = 0
    intensity: float = 0.0
    human_score: float = 0.5  # Smoothed [0.0 = animal, 1.0 = human]
    confidence: float = 0.5
    room_id: str | None = None
    room_name: str | None = None
    last_low_energy: float = 0.0
    last_high_energy: float = 0.0

    @property
    def target_type(self) -> str:
        """Classify target based on smoothed human_score."""
        return TARGET_TYPE_HUMAN if self.human_score >= 0.5 else TARGET_TYPE_ANIMAL

    @property
    def speed_mps(self) -> float:
        """Estimated movement speed in meters/second."""
        return math.hypot(self.vx, self.vy)

    def to_dict(self) -> dict[str, Any]:
        """Serialize target for Home Assistant state attributes."""
        return {
            "id": self.track_id,
            "type": self.target_type,
            "x": round(self.x, 2),
            "y": round(self.y, 2),
            "vx": round(self.vx, 2),
            "vy": round(self.vy, 2),
            "speed_mps": round(self.speed_mps, 2),
            "intensity": round(self.intensity, 2),
            "confidence": round(self.confidence, 2),
            "human_probability": round(self.human_score, 2),
            "room_id": self.room_id,
            "room": self.room_name or "Unbekannt",
        }


def extract_peaks(
    recon: RTIReconstruction,
    threshold: float = DETECTION_THRESHOLD_BASE,
    min_separation_m: float = 1.2,
    max_peaks: int = MAX_TRACKS,
) -> list[DetectionPeak]:
    """Extract sub-voxel local maxima from an RTI attenuation grid."""
    grid = recon.grid
    rows = recon.height
    cols = recon.width
    res = recon.resolution

    candidates: list[tuple[float, int, int]] = []
    for r in range(rows):
        for c in range(cols):
            val = grid[r][c]
            if val < threshold:
                continue
            # Check 3x3 neighborhood for local maximum
            is_max = True
            for dr in (-1, 0, 1):
                nr = r + dr
                if nr < 0 or nr >= rows:
                    continue
                for dc in (-1, 0, 1):
                    if dr == 0 and dc == 0:
                        continue
                    nc = c + dc
                    if nc < 0 or nc >= cols:
                        continue
                    if grid[nr][nc] > val:
                        is_max = False
                        break
                if not is_max:
                    break
            if is_max:
                candidates.append((val, r, c))

    # Sort strongest peaks first and enforce minimum spatial separation
    candidates.sort(key=lambda item: item[0], reverse=True)
    peaks: list[DetectionPeak] = []

    for val, r, c in candidates:
        # Sub-voxel centroid refinement in 3x3 window
        w_sum = 0.0
        wx_sum = 0.0
        wy_sum = 0.0
        low_e = 0.0
        high_e = 0.0
        low_cov = 0.0
        high_cov = 0.0
        active_cells = 0
        half_thresh = val * 0.45

        for dr in (-1, 0, 1):
            nr = r + dr
            if nr < 0 or nr >= rows:
                continue
            for dc in (-1, 0, 1):
                nc = c + dc
                if nc < 0 or nc >= cols:
                    continue
                cell_v = grid[nr][nc]
                if cell_v > 0.0:
                    cx = (nc + 0.5) * res
                    cy = (nr + 0.5) * res
                    w_sum += cell_v
                    wx_sum += cell_v * cx
                    wy_sum += cell_v * cy
                if cell_v >= half_thresh:
                    active_cells += 1
                low_e = max(low_e, recon.low_layer_grid[nr][nc])
                high_e = max(high_e, recon.high_layer_grid[nr][nc])
                low_cov = max(low_cov, recon.low_coverage_grid[nr][nc])
                high_cov = max(high_cov, recon.high_coverage_grid[nr][nc])

        px = wx_sum / w_sum if w_sum > 0 else (c + 0.5) * res
        py = wy_sum / w_sum if w_sum > 0 else (r + 0.5) * res

        # Enforce minimum separation from already-accepted stronger peaks
        if any(math.hypot(px - p.x, py - p.y) < min_separation_m for p in peaks):
            continue

        peaks.append(
            DetectionPeak(
                x=px,
                y=py,
                intensity=val,
                footprint_area_m2=active_cells * (res * res),
                low_energy=low_e,
                high_energy=high_e,
                low_coverage=low_cov,
                high_coverage=high_cov,
            )
        )
        if len(peaks) >= max_peaks:
            break

    return peaks


def classify_peak_human_score(peak: DetectionPeak) -> tuple[float, float]:
    """Compute instantaneous P(human) in [0, 1] and classification confidence [0, 1].

    Uses 3 physical cues:
      1. 3D Vertical Link Ratio: If both low (<0.9m) and high (>=0.9m) links cover
         the peak's location, high-link attenuation strongly indicates a human,
         whereas low-only attenuation strongly indicates a pet/animal.
      2. RF Cross-Section (Peak Attenuation Intensity): Larger water mass -> human.
      3. Spatial Footprint Area: Wider shadow across voxels -> human.
    """
    # Cue 1: Attenuation intensity (sigmoid-like mapping)
    mid_atten = 0.5 * (HUMAN_ATTENUATION_THRESHOLD + ANIMAL_ATTENUATION_THRESHOLD)
    span_atten = max(0.3, HUMAN_ATTENUATION_THRESHOLD - ANIMAL_ATTENUATION_THRESHOLD)
    z_atten = (peak.intensity - mid_atten) / (0.5 * span_atten)
    score_atten = _sigmoid(2.2 * z_atten)

    # Cue 2: Footprint area (pets ~ 0.25-0.75 m^2, humans ~ 1.0-2.5 m^2)
    z_area = (peak.footprint_area_m2 - 0.95) / 0.55
    score_area = _sigmoid(1.8 * z_area)

    # Cue 3: 3D vertical layer discrimination (strongest cue when both layers exist)
    has_low_cov = peak.low_coverage > 0.05
    has_high_cov = peak.high_coverage > 0.05

    if has_low_cov and has_high_cov:
        norm_low = peak.low_energy / max(peak.low_coverage, 0.1)
        norm_high = peak.high_energy / max(peak.high_coverage, 0.1)
        total_norm = norm_low + norm_high
        if total_norm > 0.05:
            high_ratio = norm_high / total_norm
            # Humans typically trigger high links strongly (high_ratio >= 0.35),
            # while small pets leave high links quiet (high_ratio < 0.20).
            score_3d = _sigmoid(8.0 * (high_ratio - 0.28))
            human_prob = 0.55 * score_3d + 0.30 * score_atten + 0.15 * score_area
            confidence = min(0.98, 0.55 + 0.43 * abs(2.0 * human_prob - 1.0))
            return (human_prob, confidence)

    # Fallback when only 2D (single height layer) coverage is available
    human_prob = 0.72 * score_atten + 0.28 * score_area
    confidence = min(0.85, 0.40 + 0.45 * abs(2.0 * human_prob - 1.0))
    return (human_prob, confidence)


class MultiTargetTracker:
    """Tracks multiple humans and animals across RTI frames."""

    def __init__(
        self,
        rooms: list[RoomZone] | None = None,
        detection_threshold: float = DETECTION_THRESHOLD_BASE,
        max_assoc_dist: float = TRACKER_MAX_ASSOC_DIST_M,
        min_hits: int = TRACKER_MIN_HITS,
        max_missed: int = TRACKER_MAX_MISSED,
        max_tracks: int = MAX_TRACKS,
    ) -> None:
        self.rooms: list[RoomZone] = rooms or []
        self.detection_threshold = detection_threshold
        self.max_assoc_dist = max_assoc_dist
        self.min_hits = min_hits
        self.max_missed = max_missed
        self.max_tracks = max(1, int(max_tracks))
        self._tracks: dict[int, TrackedTarget] = {}
        self._next_id = 1

    def set_rooms(self, rooms: list[RoomZone]) -> None:
        """Update configured room zones."""
        self.rooms = rooms

    def _find_room(self, x: float, y: float) -> tuple[str | None, str | None]:
        for room in self.rooms:
            if room.contains(x, y):
                return (room.room_id, room.name)
        return (None, None)

    def update(self, recon: RTIReconstruction, dt: float = 2.0) -> list[TrackedTarget]:
        """Run one predict-associate-update cycle and return confirmed targets."""
        dt = max(0.2, min(dt, 15.0))
        peaks = extract_peaks(
            recon,
            threshold=self.detection_threshold,
            max_peaks=self.max_tracks,
        )

        # 1. Predict all existing tracks (Constant-Velocity + damping)
        for track in self._tracks.values():
            track.x += track.vx * dt
            track.y += track.vy * dt
            # Clamp inside floorplan
            max_x = recon.width * recon.resolution
            max_y = recon.height * recon.resolution
            track.x = max(0.0, min(max_x, track.x))
            track.y = max(0.0, min(max_y, track.y))
            # Velocity decay (indoor humans/pets stop frequently)
            track.vx *= 0.72
            track.vy *= 0.72
            track.p_pos += 0.35 * dt

        # 2. Greedy nearest-neighbor data association (sorted by distance)
        pairs: list[tuple[float, int, int]] = []
        for tid, track in self._tracks.items():
            for pidx, peak in enumerate(peaks):
                dist = math.hypot(peak.x - track.x, peak.y - track.y)
                if dist <= self.max_assoc_dist:
                    pairs.append((dist, tid, pidx))
        pairs.sort(key=lambda item: item[0])

        matched_tracks: set[int] = set()
        matched_peaks: set[int] = set()

        for _, tid, pidx in pairs:
            if tid in matched_tracks or pidx in matched_peaks:
                continue
            matched_tracks.add(tid)
            matched_peaks.add(pidx)
            track = self._tracks[tid]
            peak = peaks[pidx]

            # Kalman position & velocity gain
            r_meas = 0.35
            k_gain = track.p_pos / (track.p_pos + r_meas)
            dx = peak.x - track.x
            dy = peak.y - track.y

            track.x += k_gain * dx
            track.y += k_gain * dy
            track.vx = 0.55 * track.vx + 0.45 * (dx / dt)
            track.vy = 0.55 * track.vy + 0.45 * (dy / dt)
            track.p_pos = (1.0 - k_gain) * track.p_pos
            track.intensity = 0.6 * peak.intensity + 0.4 * track.intensity
            track.last_low_energy = peak.low_energy
            track.last_high_energy = peak.high_energy

            # Update smoothed human/animal classification
            inst_human, inst_conf = classify_peak_human_score(peak)
            track.human_score = 0.70 * track.human_score + 0.30 * inst_human
            track.confidence = 0.65 * track.confidence + 0.35 * inst_conf

            track.hits += 1
            track.missed = 0
            track.room_id, track.room_name = self._find_room(track.x, track.y)

        # 3. Age unmatched tracks
        to_delete: list[int] = []
        for tid, track in self._tracks.items():
            if tid not in matched_tracks:
                track.missed += 1
                if track.missed > self.max_missed:
                    to_delete.append(tid)
        for tid in to_delete:
            del self._tracks[tid]

        # 4. Spawn new tentative tracks for unmatched peaks (bounded by max_tracks)
        for pidx, peak in enumerate(peaks):
            if pidx in matched_peaks:
                continue
            if len(self._tracks) >= self.max_tracks:
                break
            inst_human, inst_conf = classify_peak_human_score(peak)
            room_id, room_name = self._find_room(peak.x, peak.y)
            new_track = TrackedTarget(
                track_id=self._next_id,
                x=peak.x,
                y=peak.y,
                intensity=peak.intensity,
                human_score=inst_human,
                confidence=inst_conf,
                room_id=room_id,
                room_name=room_name,
                last_low_energy=peak.low_energy,
                last_high_energy=peak.high_energy,
            )
            self._tracks[self._next_id] = new_track
            self._next_id += 1

        # Return confirmed targets
        return [
            t
            for t in self._tracks.values()
            if t.hits >= self.min_hits and t.missed <= 2
        ]
