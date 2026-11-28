"""Live PNG 3D Isometric Radio Tomographic Imaging Map Camera for Home Assistant.

Uses a pure-Python zero-dependency PNG encoder (zlib + struct) with a built-in
5x7 bitmap pixel font and isometric 3D projection so that both `/api/camera_proxy`
thumbnails AND `/api/camera_proxy_stream` MJPEG/multipart streams render reliably
in Chrome, Safari, Firefox, and the HA Companion App.

Rendering is offloaded to Home Assistant's executor pool and cached per
reconstruction sequence number to prevent event-loop stalls and RAM churn.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import math
import struct
from typing import Any
import zlib

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, NAME, PROTOCOL_BLE, TARGET_TYPE_HUMAN, VERSION
from .coordinator import RFPresenceCoordinator
from .rti_engine import RTIReconstruction

# Zero-dependency 5x7 uppercase bitmap pixel font (each int is a 5-bit row)
_FONT_5X7: dict[str, tuple[int, ...]] = {
    " ": (0, 0, 0, 0, 0, 0, 0),
    "A": (0b01110, 0b10001, 0b10001, 0b11111, 0b10001, 0b10001, 0b10001),
    "B": (0b11110, 0b10001, 0b10001, 0b11110, 0b10001, 0b10001, 0b11110),
    "C": (0b01110, 0b10001, 0b10000, 0b10000, 0b10000, 0b10001, 0b01110),
    "D": (0b11100, 0b10010, 0b10001, 0b10001, 0b10001, 0b10010, 0b11100),
    "E": (0b11111, 0b10000, 0b10000, 0b11110, 0b10000, 0b10000, 0b11111),
    "F": (0b11111, 0b10000, 0b10000, 0b11110, 0b10000, 0b10000, 0b10000),
    "G": (0b01110, 0b10001, 0b10000, 0b10111, 0b10001, 0b10001, 0b01110),
    "H": (0b10001, 0b10001, 0b10001, 0b11111, 0b10001, 0b10001, 0b10001),
    "I": (0b01110, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b01110),
    "J": (0b00111, 0b00010, 0b00010, 0b00010, 0b10010, 0b10010, 0b01100),
    "K": (0b10001, 0b10010, 0b10100, 0b11000, 0b10100, 0b10010, 0b10001),
    "L": (0b10000, 0b10000, 0b10000, 0b10000, 0b10000, 0b10000, 0b11111),
    "M": (0b10001, 0b11011, 0b10101, 0b10101, 0b10001, 0b10001, 0b10001),
    "N": (0b10001, 0b11001, 0b10101, 0b10011, 0b10001, 0b10001, 0b10001),
    "O": (0b01110, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01110),
    "P": (0b11110, 0b10001, 0b10001, 0b11110, 0b10000, 0b10000, 0b10000),
    "Q": (0b01110, 0b10001, 0b10001, 0b10001, 0b10101, 0b10010, 0b01101),
    "R": (0b11110, 0b10001, 0b10001, 0b11110, 0b10100, 0b10010, 0b10001),
    "S": (0b01111, 0b10000, 0b10000, 0b01110, 0b00001, 0b00001, 0b11110),
    "T": (0b11111, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100),
    "U": (0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01110),
    "V": (0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01010, 0b00100),
    "W": (0b10001, 0b10001, 0b10001, 0b10101, 0b10101, 0b11011, 0b10001),
    "X": (0b10001, 0b10001, 0b01010, 0b00100, 0b01010, 0b10001, 0b10001),
    "Y": (0b10001, 0b10001, 0b01010, 0b00100, 0b00100, 0b00100, 0b00100),
    "Z": (0b11111, 0b00001, 0b00010, 0b00100, 0b01000, 0b10000, 0b11111),
    "0": (0b01110, 0b10001, 0b10011, 0b10101, 0b11001, 0b10001, 0b01110),
    "1": (0b00100, 0b01100, 0b00100, 0b00100, 0b00100, 0b00100, 0b01110),
    "2": (0b01110, 0b10001, 0b00001, 0b00110, 0b01000, 0b10000, 0b11111),
    "3": (0b11110, 0b00001, 0b00001, 0b01110, 0b00001, 0b00001, 0b11110),
    "4": (0b00010, 0b00110, 0b01010, 0b10010, 0b11111, 0b00010, 0b00010),
    "5": (0b11111, 0b10000, 0b11110, 0b00001, 0b00001, 0b10001, 0b01110),
    "6": (0b01110, 0b10000, 0b11110, 0b10001, 0b10001, 0b10001, 0b01110),
    "7": (0b11111, 0b00001, 0b00010, 0b00100, 0b01000, 0b01000, 0b01000),
    "8": (0b01110, 0b10001, 0b10001, 0b01110, 0b10001, 0b10001, 0b01110),
    "9": (0b01110, 0b10001, 0b10001, 0b01111, 0b00001, 0b00010, 0b01100),
    ":": (0b00000, 0b00100, 0b00100, 0b00000, 0b00100, 0b00100, 0b00000),
    ".": (0b00000, 0b00000, 0b00000, 0b00000, 0b00000, 0b01100, 0b01100),
    ",": (0b00000, 0b00000, 0b00000, 0b00000, 0b00100, 0b00100, 0b01000),
    "-": (0b00000, 0b00000, 0b00000, 0b11111, 0b00000, 0b00000, 0b00000),
    "/": (0b00001, 0b00010, 0b00010, 0b00100, 0b01000, 0b01000, 0b10000),
    "%": (0b11001, 0b11010, 0b00010, 0b00100, 0b01000, 0b01011, 0b10011),
    "|": (0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100),
    "(": (0b00010, 0b00100, 0b01000, 0b01000, 0b01000, 0b00100, 0b00010),
    ")": (0b01000, 0b00100, 0b00010, 0b00010, 0b00010, 0b00100, 0b01000),
    "+": (0b00000, 0b00100, 0b00100, 0b11111, 0b00100, 0b00100, 0b00000),
    "!": (0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b00000, 0b00100),
    "_": (0b00000, 0b00000, 0b00000, 0b00000, 0b00000, 0b00000, 0b11111),
}

_UMLAUT_MAP = str.maketrans(
    {"Ä": "AE", "Ö": "OE", "Ü": "UE", "ä": "AE", "ö": "OE", "ü": "UE", "ß": "SS"}
)


def _normalize_label(text: str) -> str:
    """Convert text to uppercase ASCII supported by the 5x7 pixel font."""
    return text.translate(_UMLAUT_MAP).upper()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the live 3D RF Map camera entity."""
    coordinator: RFPresenceCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([RFHeatmapCamera(coordinator, entry)])


class _Canvas:
    """Fast RGB raster canvas with 3D polygon, ellipse, text, and pure-Python PNG support."""

    def __init__(self, width: int, height: int, bg: tuple[int, int, int]) -> None:
        self.w = width
        self.h = height
        # Allocate contiguous RGB bytes directly (avoids a multi-MB temporary int list)
        self.buf = bytearray(bytes(bg) * (width * height))

    def _blend_px(
        self, x: int, y: int, color: tuple[int, int, int], alpha: float = 1.0
    ) -> None:
        if x < 0 or x >= self.w or y < 0 or y >= self.h:
            return
        idx = (y * self.w + x) * 3
        if alpha >= 0.99:
            self.buf[idx] = color[0]
            self.buf[idx + 1] = color[1]
            self.buf[idx + 2] = color[2]
        else:
            inv = 1.0 - alpha
            self.buf[idx] = int(self.buf[idx] * inv + color[0] * alpha)
            self.buf[idx + 1] = int(self.buf[idx + 1] * inv + color[1] * alpha)
            self.buf[idx + 2] = int(self.buf[idx + 2] * inv + color[2] * alpha)

    def fill_rect(
        self,
        x0: int,
        y0: int,
        x1: int,
        y1: int,
        color: tuple[int, int, int],
        alpha: float = 1.0,
    ) -> None:
        x0 = max(0, min(self.w, x0))
        x1 = max(0, min(self.w, x1))
        y0 = max(0, min(self.h, y0))
        y1 = max(0, min(self.h, y1))
        if x1 <= x0 or y1 <= y0:
            return
        if alpha >= 0.99:
            row_bytes = bytes(color) * (x1 - x0)
            row_len = (x1 - x0) * 3
            for y in range(y0, y1):
                start = (y * self.w + x0) * 3
                self.buf[start : start + row_len] = row_bytes
        else:
            for y in range(y0, y1):
                for x in range(x0, x1):
                    self._blend_px(x, y, color, alpha)

    def stroke_rect(
        self,
        x0: int,
        y0: int,
        x1: int,
        y1: int,
        color: tuple[int, int, int],
        thickness: int = 1,
    ) -> None:
        self.fill_rect(x0, y0, x1, y0 + thickness, color)
        self.fill_rect(x0, y1 - thickness, x1, y1, color)
        self.fill_rect(x0, y0, x0 + thickness, y1, color)
        self.fill_rect(x1 - thickness, y0, x1, y1, color)

    def draw_line(
        self,
        x0: int,
        y0: int,
        x1: int,
        y1: int,
        color: tuple[int, int, int],
        alpha: float = 1.0,
        dashed: bool = False,
        thickness: int = 1,
    ) -> None:
        # Reject segments completely outside the canvas bounds
        if (
            max(x0, x1) < -1
            or min(x0, x1) >= self.w
            or max(y0, y1) < -1
            or min(y0, y1) >= self.h
        ):
            return
        dx = abs(x1 - x0)
        dy = -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        step = 0
        max_steps = (self.w + self.h) * 3
        while step <= max_steps:
            if not dashed or (step % 8) < 5:
                self._blend_px(x0, y0, color, alpha)
                if thickness >= 2:
                    self._blend_px(x0 + 1, y0, color, alpha)
                    self._blend_px(x0, y0 + 1, color, alpha)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy
            step += 1

    def fill_circle(
        self,
        cx: int,
        cy: int,
        radius: int,
        color: tuple[int, int, int],
        alpha: float = 1.0,
    ) -> None:
        r2 = radius * radius
        for dy in range(-radius, radius + 1):
            y = cy + dy
            if y < 0 or y >= self.h:
                continue
            for dx in range(-radius, radius + 1):
                if dx * dx + dy * dy <= r2:
                    self._blend_px(cx + dx, y, color, alpha)

    def fill_ellipse(
        self,
        cx: int,
        cy: int,
        rx: int,
        ry: int,
        color: tuple[int, int, int],
        alpha: float = 1.0,
    ) -> None:
        if rx <= 0 or ry <= 0:
            return
        inv_rx2 = 1.0 / (rx * rx)
        inv_ry2 = 1.0 / (ry * ry)
        for dy in range(-ry, ry + 1):
            y = cy + dy
            if y < 0 or y >= self.h:
                continue
            dy_term = dy * dy * inv_ry2
            for dx in range(-rx, rx + 1):
                if dx * dx * inv_rx2 + dy_term <= 1.0:
                    self._blend_px(cx + dx, y, color, alpha)

    def fill_polygon(
        self,
        points: list[tuple[int, int]],
        color: tuple[int, int, int],
        alpha: float = 1.0,
    ) -> None:
        """Scanline-fill a 2D polygon cleanly with alpha blending."""
        n = len(points)
        if n < 3:
            return
        min_y = max(0, min(p[1] for p in points))
        max_y = min(self.h - 1, max(p[1] for p in points))
        if min_y > max_y:
            return

        for y in range(min_y, max_y + 1):
            nodes: list[float] = []
            j = n - 1
            for i in range(n):
                xi, yi = points[i]
                xj, yj = points[j]
                if (yi < y <= yj) or (yj < y <= yi):
                    nodes.append(xi + (y - yi) * (xj - xi) / (yj - yi))
                j = i
            if len(nodes) < 2:
                continue
            nodes.sort()
            for k in range(0, len(nodes) - 1, 2):
                x_start = max(0, int(math.ceil(nodes[k])))
                x_end = min(self.w - 1, int(math.floor(nodes[k + 1])))
                if x_end < x_start:
                    continue
                if alpha >= 0.99:
                    span = x_end - x_start + 1
                    start = (y * self.w + x_start) * 3
                    self.buf[start : start + span * 3] = bytes(color) * span
                else:
                    for x in range(x_start, x_end + 1):
                        self._blend_px(x, y, color, alpha)

    def draw_text(
        self,
        x: int,
        y: int,
        text: str,
        color: tuple[int, int, int],
        scale: int = 1,
        bg: tuple[int, int, int] | None = None,
        bg_alpha: float = 0.78,
    ) -> None:
        """Render crisp bitmap text with optional pill background."""
        norm = _normalize_label(text)
        if not norm:
            return
        char_w = 6 * scale
        text_w = len(norm) * char_w - scale
        text_h = 7 * scale
        if bg is not None:
            pad = 2 * scale
            self.fill_rect(
                x - pad,
                y - pad,
                x + text_w + pad,
                y + text_h + pad,
                bg,
                alpha=bg_alpha,
            )

        cursor_x = x
        for ch in norm:
            glyph = _FONT_5X7.get(ch, _FONT_5X7[" "])
            for row_idx, bits in enumerate(glyph):
                if bits == 0:
                    continue
                py = y + row_idx * scale
                for col_idx in range(5):
                    if bits & (1 << (4 - col_idx)):
                        px = cursor_x + col_idx * scale
                        if scale == 1:
                            self._blend_px(px, py, color, 1.0)
                        else:
                            self.fill_rect(px, py, px + scale, py + scale, color, 1.0)
            cursor_x += char_w

    def to_png(self) -> bytes:
        """Encode RGB canvas into a standard PNG byte stream."""

        def _chunk(tag: bytes, data: bytes) -> bytes:
            return (
                struct.pack("!I", len(data))
                + tag
                + data
                + struct.pack("!I", zlib.crc32(tag + data) & 0xFFFFFFFF)
            )

        stride = self.w * 3
        row_step = stride + 1
        raw = bytearray(row_step * self.h)
        for y in range(self.h):
            dst = y * row_step
            raw[dst] = 0  # PNG filter type 0 (None)
            src = y * stride
            raw[dst + 1 : dst + row_step] = self.buf[src : src + stride]

        ihdr = struct.pack("!IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0)
        idat = zlib.compress(raw, level=3)
        return (
            b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", idat)
            + _chunk(b"IEND", b"")
        )


@dataclass(frozen=True)
class _RoomSnap:
    """Immutable snapshot of one rectangular or polygon room."""

    name: str
    vertices: tuple[tuple[float, float], ...]
    cx: float
    cy: float
    is_rect: bool = True
    open_edges: tuple[str, ...] = ()
    doors: tuple[tuple[str, float, float], ...] = ()
    windows: tuple[tuple[str, float, float], ...] = ()


@dataclass(frozen=True)
class _TargetSnap:
    """Immutable snapshot of one tracked target with movement trail."""

    track_id: int
    x: float
    y: float
    vx: float
    vy: float
    speed_mps: float
    is_human: bool
    confidence: float
    trail: tuple[tuple[float, float], ...]
    motion_state: str = "moving"
    dwell_seconds: float = 0.0


@dataclass(frozen=True)
class _RenderSnapshot:
    """Immutable snapshot of coordinator state safe to render in an executor thread."""

    floor_width: float
    floor_height: float
    recon: RTIReconstruction | None
    rooms: tuple[_RoomSnap, ...]
    links: tuple[tuple[str, str, float, float, float, float, float, float], ...]
    devices: tuple[tuple[float, float, float, str], ...]
    receivers: tuple[tuple[float, float, float, str], ...]
    targets: tuple[_TargetSnap, ...]
    calibrating: bool
    edge_walk_active: bool = False
    fingerprints_xy: tuple[tuple[float, float], ...] = ()


def _build_render_snapshot(coordinator: Any) -> _RenderSnapshot:
    """Capture an immutable 3D snapshot of floorplan, links, and targets on the event loop."""
    if hasattr(coordinator.engine, "snapshot_links_3d"):
        link_snap = tuple(coordinator.engine.snapshot_links_3d())
    elif hasattr(coordinator.engine, "snapshot_links"):
        link_snap = tuple(
            (lid, proto, x1, y1, 1.0, x2, y2, 1.0)
            for lid, proto, x1, y1, x2, y2 in coordinator.engine.snapshot_links()
        )
    else:
        link_snap = tuple(
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
            for lid, link in coordinator.engine.links.items()
        )

    rooms_list: list[_RoomSnap] = []
    for room in coordinator.rooms:
        poly = getattr(room, "polygon", None)
        is_rect = not (poly and len(poly) >= 3)
        if not is_rect:
            verts = tuple((float(pt[0]), float(pt[1])) for pt in poly)
            cx = sum(pt[0] for pt in verts) / len(verts)
            cy = sum(pt[1] for pt in verts) / len(verts)
        else:
            verts = (
                (float(room.x_min), float(room.y_min)),
                (float(room.x_max), float(room.y_min)),
                (float(room.x_max), float(room.y_max)),
                (float(room.x_min), float(room.y_max)),
            )
            cx = 0.5 * (float(room.x_min) + float(room.x_max))
            cy = 0.5 * (float(room.y_min) + float(room.y_max))
        raw_oe = tuple(str(e) for e in (getattr(room, "open_edges", None) or ()))
        raw_dr = tuple(
            (str(d["edge"]), float(d["pos"]), float(d["width"]))
            for d in (getattr(room, "doors", None) or ())
            if isinstance(d, dict) and "edge" in d
        )
        raw_wn = tuple(
            (str(w["edge"]), float(w["pos"]), float(w["width"]))
            for w in (getattr(room, "windows", None) or ())
            if isinstance(w, dict) and "edge" in w
        )
        rooms_list.append(
            _RoomSnap(
                name=str(room.name),
                vertices=verts,
                cx=cx,
                cy=cy,
                is_rect=is_rect,
                open_edges=raw_oe,
                doors=raw_dr,
                windows=raw_wn,
            )
        )

    devices_snap = tuple(
        (
            float(dev.get("x", 0.0)),
            float(dev.get("y", 0.0)),
            float(dev.get("z", 1.0)),
            str(dev.get("protocol", "wifi")),
        )
        for dev in coordinator.devices.values()
    )
    receivers_snap = tuple(
        (
            float(recv.get("x", 0.0)),
            float(recv.get("y", 0.0)),
            float(recv.get("z", 1.1)),
            str(recv.get("name", rid)),
        )
        for rid, recv in coordinator.receivers.items()
    )

    targets_list: list[_TargetSnap] = []
    for target in coordinator.last_targets:
        raw_trail = getattr(target, "trail", None)
        trail_pts = (
            tuple((float(p[0]), float(p[1])) for p in raw_trail)
            if raw_trail
            else ((float(target.x), float(target.y)),)
        )
        targets_list.append(
            _TargetSnap(
                track_id=int(getattr(target, "track_id", 1)),
                x=float(target.x),
                y=float(target.y),
                vx=float(getattr(target, "vx", 0.0)),
                vy=float(getattr(target, "vy", 0.0)),
                speed_mps=float(getattr(target, "speed_mps", 0.0)),
                is_human=getattr(target, "target_type", TARGET_TYPE_HUMAN)
                == TARGET_TYPE_HUMAN,
                confidence=float(getattr(target, "confidence", 0.75)),
                trail=trail_pts,
                motion_state=str(getattr(target, "motion_state", "moving")),
                dwell_seconds=float(getattr(target, "dwell_seconds", 0.0)),
            )
        )

    fps_xy: tuple[tuple[float, float], ...] = ()
    if hasattr(coordinator.engine, "snapshot_fingerprints_xy"):
        fps_xy = tuple(
            (float(p[0]), float(p[1]))
            for p in coordinator.engine.snapshot_fingerprints_xy(limit=80)
        )

    return _RenderSnapshot(
        floor_width=max(float(coordinator.floor_width), 1.0),
        floor_height=max(float(coordinator.floor_height), 1.0),
        recon=coordinator.last_reconstruction,
        rooms=tuple(rooms_list),
        links=link_snap,
        devices=devices_snap,
        receivers=receivers_snap,
        targets=tuple(targets_list),
        calibrating=bool(getattr(coordinator, "_calibrating", False)),
        edge_walk_active=getattr(coordinator, "_edge_walk_state", None) is not None,
        fingerprints_xy=fps_xy,
    )


def _heat_color(t: float) -> tuple[int, int, int]:
    """Map normalized intensity t in [0, 1] to cyan -> amber -> red RGB."""
    t = max(0.0, min(1.0, t))
    if t < 0.5:
        u = t * 2.0
        return (int(56 + u * 190), int(189 - u * 40), int(248 - u * 210))
    u = (t - 0.5) * 2.0
    return (int(246 - u * 7), int(149 - u * 110), int(38 + u * 30))


def render_heatmap_png_from_snapshot(
    snap: _RenderSnapshot, w_px: int = 640, h_px: int = 480
) -> bytes:
    """Render an immutable 3D _RenderSnapshot into an isometric 3D Live Map PNG."""
    w_px = max(160, min(1280, int(w_px)))
    h_px = max(120, min(960, int(h_px)))
    header_h = 26 if h_px >= 320 else 20
    footer_h = 22 if h_px >= 320 else 18
    pad_x = max(14, int(w_px * 0.04))
    pad_y = max(10, int(h_px * 0.03))
    max_z = 2.4

    # Isometric basis: 35° yaw-like view, 30° pitch tilt
    cos_a = 0.8660254  # cos(30 deg)
    sin_a = 0.45  # vertical foreshortening for crisp floor readability
    z_lift = 1.05  # vertical Z axis scale relative to X/Y

    fw = max(1.0, snap.floor_width if math.isfinite(snap.floor_width) else 10.0)
    fh = max(1.0, snap.floor_height if math.isfinite(snap.floor_height) else 8.0)

    def _raw_iso(x: float, y: float, z: float) -> tuple[float, float]:
        return ((x - y) * cos_a, (x + y) * sin_a - z * z_lift)

    # Auto-fit all 8 corners of the 3D volume [0..fw] x [0..fh] x [0..max_z]
    corners = [
        _raw_iso(cx, cy, cz)
        for cx in (0.0, fw)
        for cy in (0.0, fh)
        for cz in (0.0, max_z)
    ]
    min_ix = min(c[0] for c in corners)
    max_ix = max(c[0] for c in corners)
    min_iy = min(c[1] for c in corners)
    max_iy = max(c[1] for c in corners)

    avail_w = max(40.0, w_px - 2.0 * pad_x)
    avail_h = max(40.0, h_px - header_h - footer_h - 2.0 * pad_y)
    scale = min(
        avail_w / max(max_ix - min_ix, 1e-3),
        avail_h / max(max_iy - min_iy, 1e-3),
    )

    offset_x = pad_x + (avail_w - (max_ix - min_ix) * scale) * 0.5 - min_ix * scale
    offset_y = (
        header_h
        + pad_y
        + (avail_h - (max_iy - min_iy) * scale) * 0.5
        - min_iy * scale
    )

    def proj(x: float, y: float, z: float = 0.0) -> tuple[int, int]:
        cx = x if math.isfinite(x) else 0.0
        cy = y if math.isfinite(y) else 0.0
        cz = z if math.isfinite(z) else 0.0
        cx = max(-fw, min(2.0 * fw, cx))
        cy = max(-fh, min(2.0 * fh, cy))
        cz = max(-1.0, min(10.0, cz))
        ix, iy = _raw_iso(cx, cy, cz)
        return (int(round(offset_x + ix * scale)), int(round(offset_y + iy * scale)))

    # Background canvas (#0b1120)
    canvas = _Canvas(w_px, h_px, (11, 17, 32))

    # 1. 3D Floor Slab with extruded base edges for strong 3D depth perception
    slab_d = -0.22
    fl_00 = proj(0.0, 0.0, 0.0)
    fl_w0 = proj(fw, 0.0, 0.0)
    fl_wh = proj(fw, fh, 0.0)
    fl_0h = proj(0.0, fh, 0.0)
    sb_w0 = proj(fw, 0.0, slab_d)
    sb_wh = proj(fw, fh, slab_d)
    sb_0h = proj(0.0, fh, slab_d)

    # Front-right and front-left slab sides
    canvas.fill_polygon([fl_w0, fl_wh, sb_wh, sb_w0], (20, 30, 48), 1.0)
    canvas.fill_polygon([fl_0h, fl_wh, sb_wh, sb_0h], (15, 23, 42), 1.0)
    # Main floor surface (#1e293b)
    canvas.fill_polygon([fl_00, fl_w0, fl_wh, fl_0h], (30, 41, 59), 1.0)

    # 2. Adaptive 3D Floor Grid & Axis Labels (at most ~14 lines per axis)
    grid_step = max(1.0, float(math.ceil(max(fw, fh) / 14.0)))
    gx = grid_step
    while gx < fw - 1e-3:
        p0 = proj(gx, 0.0, 0.0)
        p1 = proj(gx, fh, 0.0)
        canvas.draw_line(p0[0], p0[1], p1[0], p1[1], (51, 65, 85), alpha=0.55)
        gx += grid_step
    gy = grid_step
    while gy < fh - 1e-3:
        p0 = proj(0.0, gy, 0.0)
        p1 = proj(fw, gy, 0.0)
        canvas.draw_line(p0[0], p0[1], p1[0], p1[1], (51, 65, 85), alpha=0.55)
        gy += grid_step

    # Floor outer border
    border_pts = [fl_00, fl_w0, fl_wh, fl_0h]
    for i in range(4):
        a, b = border_pts[i], border_pts[(i + 1) % 4]
        canvas.draw_line(a[0], a[1], b[0], b[1], (100, 116, 139), alpha=0.9)

    # Vertical Z reference axis at back corner (0, 0, 0 -> 2.2m)
    z_top = proj(0.0, 0.0, 2.2)
    canvas.draw_line(fl_00[0], fl_00[1], z_top[0], z_top[1], (100, 116, 139), alpha=0.8)
    for zm in (1.0, 2.0):
        zp = proj(0.0, 0.0, zm)
        canvas.draw_line(zp[0] - 3, zp[1], zp[0] + 3, zp[1], (148, 163, 184), 0.9)
        if w_px >= 420:
            canvas.draw_text(zp[0] + 5, zp[1] - 3, f"{int(zm)}M", (148, 163, 184))

    # Meter scale labels along front edges
    if w_px >= 420:
        mx = proj(fw * 0.5, fh + 0.35, 0.0)
        canvas.draw_text(mx[0] - 18, mx[1], f"X:{fw:.0f}M", (148, 163, 184))
        my = proj(fw + 0.35, fh * 0.5, 0.0)
        canvas.draw_text(my[0] - 10, my[1], f"Y:{fh:.0f}M", (148, 163, 184))

    # 3. 3D Room Floors & Translucent 3D Walls with open_edges, doors & windows (z = 0 .. 2.2m)
    wall_h = 2.2
    door_h = 2.0
    win_bot = 0.9
    win_top = 2.1
    rect_edge_names = ("north", "east", "south", "west")
    room_palette = [
        (56, 189, 248),
        (129, 140, 248),
        (52, 211, 153),
        (251, 191, 36),
        (244, 114, 182),
    ]
    for r_idx, room in enumerate(snap.rooms):
        r_col = room_palette[r_idx % len(room_palette)]
        floor_poly = [proj(vx, vy, 0.0) for vx, vy in room.vertices]
        ceil_poly = [proj(vx, vy, wall_h) for vx, vy in room.vertices]
        # Subtle tinted floor surface per room
        canvas.fill_polygon(floor_poly, r_col, alpha=0.10)
        nv = len(floor_poly)
        open_set = set(room.open_edges)
        doors_by_edge: dict[str, list[tuple[float, float]]] = {}
        for d_edge, d_pos, d_width in room.doors:
            doors_by_edge.setdefault(d_edge, []).append((d_pos, d_width))
        windows_by_edge: dict[str, list[tuple[float, float]]] = {}
        for w_edge, w_pos, w_width in room.windows:
            windows_by_edge.setdefault(w_edge, []).append((w_pos, w_width))

        for i in range(nv):
            v0 = room.vertices[i]
            v1 = room.vertices[(i + 1) % nv]
            f0, f1 = floor_poly[i], floor_poly[(i + 1) % nv]
            c0, c1 = ceil_poly[i], ceil_poly[(i + 1) % nv]
            edge_name = rect_edge_names[i] if (room.is_rect and nv == 4) else ""

            if edge_name and edge_name in open_set:
                # Open passage: faint floor guide only, no wall!
                canvas.draw_line(f0[0], f0[1], f1[0], f1[1], r_col, alpha=0.22, dashed=True)
                continue

            # Floor edge, vertical corner pillar & ceiling edge
            canvas.draw_line(f0[0], f0[1], f1[0], f1[1], r_col, alpha=0.62)
            canvas.draw_line(f0[0], f0[1], c0[0], c0[1], r_col, alpha=0.35, dashed=True)
            canvas.draw_line(c0[0], c0[1], c1[0], c1[1], r_col, alpha=0.30)

            # Door cutouts along this wall edge (golden frames, z = 0 .. 2.0m)
            if edge_name and edge_name in doors_by_edge:
                edge_len = max(0.1, math.hypot(v1[0] - v0[0], v1[1] - v0[1]))
                for d_pos, d_width in doors_by_edge[edge_name]:
                    half_t = min(0.45, (d_width / edge_len) * 0.5)
                    t_a = max(0.02, d_pos - half_t)
                    t_b = min(0.98, d_pos + half_t)
                    ax = v0[0] + t_a * (v1[0] - v0[0])
                    ay = v0[1] + t_a * (v1[1] - v0[1])
                    bx = v0[0] + t_b * (v1[0] - v0[0])
                    by = v0[1] + t_b * (v1[1] - v0[1])
                    da0, db0 = proj(ax, ay, 0.0), proj(bx, by, 0.0)
                    da1, db1 = proj(ax, ay, door_h), proj(bx, by, door_h)
                    door_col = (253, 224, 71)
                    canvas.draw_line(da0[0], da0[1], da1[0], da1[1], door_col, alpha=0.85)
                    canvas.draw_line(db0[0], db0[1], db1[0], db1[1], door_col, alpha=0.85)
                    canvas.draw_line(da1[0], da1[1], db1[0], db1[1], door_col, alpha=0.85)

            # Window frames along this wall edge (light-blue/cyan glass, z = 0.9 .. 2.1m)
            if edge_name and edge_name in windows_by_edge:
                edge_len = max(0.1, math.hypot(v1[0] - v0[0], v1[1] - v0[1]))
                for w_pos, w_width in windows_by_edge[edge_name]:
                    half_t = min(0.45, (w_width / edge_len) * 0.5)
                    t_a = max(0.02, w_pos - half_t)
                    t_b = min(0.98, w_pos + half_t)
                    t_m = 0.5 * (t_a + t_b)
                    ax = v0[0] + t_a * (v1[0] - v0[0])
                    ay = v0[1] + t_a * (v1[1] - v0[1])
                    bx = v0[0] + t_b * (v1[0] - v0[0])
                    by = v0[1] + t_b * (v1[1] - v0[1])
                    mx_w = v0[0] + t_m * (v1[0] - v0[0])
                    my_w = v0[1] + t_m * (v1[1] - v0[1])
                    wa0, wb0 = proj(ax, ay, win_bot), proj(bx, by, win_bot)
                    wa1, wb1 = proj(ax, ay, win_top), proj(bx, by, win_top)
                    wm0, wm1 = proj(mx_w, my_w, win_bot), proj(mx_w, my_w, win_top)
                    canvas.fill_polygon([wa0, wb0, wb1, wa1], (56, 189, 248), alpha=0.28)
                    win_col = (125, 211, 252)
                    canvas.draw_line(wa0[0], wa0[1], wb0[0], wb0[1], win_col, alpha=0.92)
                    canvas.draw_line(wa1[0], wa1[1], wb1[0], wb1[1], win_col, alpha=0.92)
                    canvas.draw_line(wa0[0], wa0[1], wa1[0], wa1[1], win_col, alpha=0.92)
                    canvas.draw_line(wb0[0], wb0[1], wb1[0], wb1[1], win_col, alpha=0.92)
                    canvas.draw_line(wm0[0], wm0[1], wm1[0], wm1[1], win_col, alpha=0.65)

        # Room name label on floor center
        lbl_x, lbl_y = proj(room.cx, room.cy, 0.02)
        short_name = room.name[:14]
        canvas.draw_text(
            lbl_x - len(short_name) * 3,
            lbl_y - 3,
            short_name,
            (203, 213, 225),
            bg=(15, 23, 42),
            bg_alpha=0.55,
        )

    # 3b. Calibration Fingerprint Markers (green 3D floor diamonds)
    for fpx, fpy in snap.fingerprints_xy:
        fx, fy = proj(fpx, fpy, 0.03)
        diamond = [(fx, fy - 3), (fx + 4, fy), (fx, fy + 3), (fx - 4, fy)]
        canvas.fill_polygon(diamond, (52, 211, 153), alpha=0.78)

    # 4. 3D RTI Heatmap Floor Tiles & Vertical Energy Columns (back-to-front)
    recon = snap.recon
    if recon is not None:
        max_val = max((max(row) for row in recon.grid), default=0.0)
        norm = max(max_val, 2.5)
        res = recon.resolution
        # Iterate back-to-front by (r + c) for proper 3D column occlusion
        active_voxels: list[tuple[int, int, int, float]] = []
        for r in range(recon.height):
            row = recon.grid[r]
            for c in range(recon.width):
                val = row[c]
                if val > 0.12:
                    active_voxels.append((r + c, r, c, val))
        active_voxels.sort(key=lambda item: item[0])

        for _, r, c, val in active_voxels:
            x0, y0 = c * res, r * res
            x1, y1 = min(fw, (c + 1) * res), min(fh, (r + 1) * res)
            t = min(1.0, val / norm)
            col = _heat_color(t)
            alpha = 0.22 + 0.62 * t
            quad = [
                proj(x0, y0, 0.01),
                proj(x1, y0, 0.01),
                proj(x1, y1, 0.01),
                proj(x0, y1, 0.01),
            ]
            canvas.fill_polygon(quad, col, alpha=alpha)

            # Translucent 3D energy column rising from strong RTI hotspots
            if val > 0.48:
                col_h = min(1.45, 0.25 + 1.2 * t)
                cx_m = 0.5 * (x0 + x1)
                cy_m = 0.5 * (y0 + y1)
                hw = res * 0.28
                b0 = proj(cx_m - hw, cy_m + hw, 0.02)
                b1 = proj(cx_m + hw, cy_m + hw, 0.02)
                b2 = proj(cx_m + hw, cy_m - hw, 0.02)
                t0 = proj(cx_m - hw, cy_m + hw, col_h)
                t1 = proj(cx_m + hw, cy_m + hw, col_h)
                t2 = proj(cx_m + hw, cy_m - hw, col_h)
                t3 = proj(cx_m - hw, cy_m - hw, col_h)
                canvas.fill_polygon([b0, b1, t1, t0], col, alpha=0.22)
                canvas.fill_polygon([b1, b2, t2, t1], col, alpha=0.30)
                canvas.fill_polygon([t3, t2, t1, t0], col, alpha=0.45)

    # 5. Fading 3D Movement Trails on Floor
    for target in snap.targets:
        if len(target.trail) >= 2:
            t_col = (16, 185, 129) if target.is_human else (251, 146, 60)
            n_seg = len(target.trail) - 1
            for idx in range(n_seg):
                p_a = proj(target.trail[idx][0], target.trail[idx][1], 0.04)
                p_b = proj(target.trail[idx + 1][0], target.trail[idx + 1][1], 0.04)
                fade = 0.20 + 0.70 * ((idx + 1) / n_seg)
                canvas.draw_line(
                    p_a[0], p_a[1], p_b[0], p_b[1], t_col, alpha=fade, thickness=2
                )

    # 6. True 3D RF Links (drawn between real 3D heights z1 and z2!)
    perts = recon.link_perturbations if recon else {}
    for lid, protocol, p1x, p1y, p1z, p2x, p2y, p2z in snap.links:
        pert = perts.get(lid, 0.0)
        a = proj(p1x, p1y, p1z)
        b = proj(p2x, p2y, p2z)
        if pert > 1.2:
            col = (249, 115, 22)  # bright orange perturbed 3D beam
            canvas.draw_line(a[0], a[1], b[0], b[1], col, alpha=0.88, thickness=2)
        elif protocol == PROTOCOL_BLE:
            col = (167, 139, 250)  # violet BLE 3D link
            canvas.draw_line(a[0], a[1], b[0], b[1], col, alpha=0.32, dashed=True)
        else:
            col = (56, 189, 248)  # cyan Wi-Fi 3D link
            canvas.draw_line(a[0], a[1], b[0], b[1], col, alpha=0.38, dashed=True)

    # 7. Stationary Devices & Receivers with 3D Vertical Drop-Pins to Floor (z = 0)
    for dev_x, dev_y, dev_z, proto in sorted(snap.devices, key=lambda d: d[0] + d[1]):
        col = (192, 132, 252) if proto == PROTOCOL_BLE else (56, 189, 248)
        base = proj(dev_x, dev_y, 0.0)
        top = proj(dev_x, dev_y, dev_z)
        canvas.draw_line(
            base[0], base[1], top[0], top[1], (148, 163, 184), alpha=0.45, dashed=True
        )
        canvas.fill_circle(base[0], base[1], 2, (100, 116, 139), alpha=0.7)
        canvas.fill_circle(top[0], top[1], 4, col, alpha=0.95)
        canvas.fill_circle(top[0] - 1, top[1] - 1, 1, (255, 255, 255), alpha=0.85)

    for recv_x, recv_y, recv_z, _rname in sorted(
        snap.receivers, key=lambda r: r[0] + r[1]
    ):
        base = proj(recv_x, recv_y, 0.0)
        top = proj(recv_x, recv_y, recv_z)
        canvas.draw_line(
            base[0], base[1], top[0], top[1], (250, 204, 21), alpha=0.55, dashed=True
        )
        canvas.fill_circle(base[0], base[1], 2, (250, 204, 21), alpha=0.7)
        # Golden 3D diamond receiver marker
        rx, ry = top
        diamond = [(rx, ry - 6), (rx + 6, ry), (rx, ry + 6), (rx - 6, ry)]
        canvas.fill_polygon(diamond, (250, 204, 21), alpha=1.0)
        canvas.fill_rect(rx - 2, ry - 2, rx + 2, ry + 2, (255, 255, 255), alpha=0.85)

    # 8. 3D Human & Animal Figures (depth-sorted back-to-front by x + y)
    for target in sorted(snap.targets, key=lambda t: t.x + t.y):
        col = (16, 185, 129) if target.is_human else (251, 146, 60)
        hi_col = (110, 231, 183) if target.is_human else (253, 186, 116)
        base = proj(target.x, target.y, 0.02)

        # Ground glow halo ellipse
        halo_rx = max(8, int(round(0.45 * scale)))
        halo_ry = max(4, int(round(0.24 * scale)))
        canvas.fill_ellipse(base[0], base[1], halo_rx, halo_ry, col, alpha=0.28)
        canvas.fill_ellipse(
            base[0], base[1], max(4, halo_rx // 2), max(2, halo_ry // 2), col, alpha=0.55
        )

        # Velocity direction arrow on ground
        if target.speed_mps > 0.15:
            tip = proj(target.x + target.vx * 0.85, target.y + target.vy * 0.85, 0.04)
            canvas.draw_line(
                base[0], base[1], tip[0], tip[1], hi_col, alpha=0.95, thickness=2
            )

        if target.is_human:
            # 3D Human avatar: torso pillar (0.05..1.38m) + shoulders + head sphere (1.62m)
            torso_top = proj(target.x, target.y, 1.38)
            head_pt = proj(target.x, target.y, 1.62)
            body_w = max(4, int(round(0.16 * scale)))
            body_poly = [
                (base[0] - body_w, base[1]),
                (base[0] + body_w, base[1]),
                (torso_top[0] + body_w + 1, torso_top[1]),
                (torso_top[0] - body_w - 1, torso_top[1]),
            ]
            canvas.fill_polygon(body_poly, col, alpha=0.92)
            canvas.draw_line(
                base[0] - body_w,
                base[1],
                torso_top[0] - body_w - 1,
                torso_top[1],
                hi_col,
                alpha=0.85,
            )
            head_r = max(4, int(round(0.15 * scale)))
            canvas.fill_circle(head_pt[0], head_pt[1], head_r + 1, (255, 255, 255), 0.95)
            canvas.fill_circle(head_pt[0], head_pt[1], head_r, col, 1.0)
            label_y = head_pt[1] - head_r - 12
            tag = f"M{target.track_id} {int(round(target.confidence * 100))}%"
        else:
            # 3D Animal avatar: low warm-orange dome (0.04..0.38m)
            dome_c = proj(target.x, target.y, 0.22)
            dome_rx = max(6, int(round(0.24 * scale)))
            dome_ry = max(4, int(round(0.18 * scale)))
            canvas.fill_ellipse(
                dome_c[0], dome_c[1], dome_rx + 1, dome_ry + 1, (255, 255, 255), 0.9
            )
            canvas.fill_ellipse(dome_c[0], dome_c[1], dome_rx, dome_ry, col, 1.0)
            canvas.fill_circle(dome_c[0] - 2, dome_c[1] - 1, 2, hi_col, 0.9)
            label_y = dome_c[1] - dome_ry - 12
            tag = f"T{target.track_id} {int(round(target.confidence * 100))}%"

        if target.motion_state == "stationary":
            dwell_s = int(round(max(0.0, target.dwell_seconds)))
            dwell_txt = f"{dwell_s // 60}M" if dwell_s >= 60 else f"{dwell_s}S"
            tag = f"{tag} RUH {dwell_txt}"

        canvas.draw_text(
            base[0] - len(tag) * 3,
            max(header_h + 2, label_y),
            tag,
            (248, 250, 252),
            bg=(15, 23, 42),
            bg_alpha=0.85,
        )

    # 9. HUD Header Bar
    canvas.fill_rect(0, 0, w_px, header_h, (15, 23, 42), alpha=0.95)
    canvas.fill_rect(0, header_h - 1, w_px, header_h, (51, 65, 85), alpha=1.0)
    humans_n = sum(1 for t in snap.targets if t.is_human)
    animals_n = sum(1 for t in snap.targets if not t.is_human)
    act_links = recon.active_links if recon else 0
    tot_links = recon.total_links if recon else len(snap.links)
    header_text = (
        f"RF 3D LIVE MAP | M:{humans_n} T:{animals_n} | LINKS:{act_links}/{tot_links}"
    )
    canvas.draw_text(10, (header_h - 7) // 2, header_text, (226, 232, 240))

    # Calibrating or Edge-Walk status banner overlay
    if snap.calibrating or snap.edge_walk_active:
        banner_txt = (
            "KALIBRIERUNG LAEUFT..."
            if snap.calibrating
            else "KANTE WIRD ABGELAUFEN..."
        )
        bw = len(banner_txt) * 6 + 16
        bx = max(8, (w_px - bw) // 2)
        by = header_h + 6
        bg_b = (217, 119, 6) if snap.calibrating else (5, 150, 105)
        bd_b = (253, 224, 71) if snap.calibrating else (110, 231, 183)
        canvas.fill_rect(bx, by, bx + bw, by + 17, bg_b, alpha=0.92)
        canvas.stroke_rect(bx, by, bx + bw, by + 17, bd_b, 1)
        canvas.draw_text(bx + 8, by + 5, banner_txt, (255, 255, 255))

    # 10. Compact Bottom Legend Bar
    fy0 = h_px - footer_h
    canvas.fill_rect(0, fy0, w_px, h_px, (15, 23, 42), alpha=0.95)
    canvas.fill_rect(0, fy0, w_px, fy0 + 1, (51, 65, 85), alpha=1.0)
    legend_items = [
        ((16, 185, 129), "MENSCH"),
        ((251, 146, 60), "TIER"),
        ((56, 189, 248), "WIFI"),
        ((167, 139, 250), "BLE"),
        ((249, 115, 22), "STOERUNG"),
        ((250, 204, 21), "RX"),
    ]
    lx = 10
    ly = fy0 + (footer_h - 7) // 2
    for swatch_col, lbl in legend_items:
        item_w = 10 + len(lbl) * 6 + 10
        if lx + item_w > w_px - 6:
            break
        canvas.fill_rect(lx, ly, lx + 7, ly + 7, swatch_col, 1.0)
        canvas.draw_text(lx + 10, ly, lbl, (203, 213, 225))
        lx += item_w

    return canvas.to_png()


def render_heatmap_png(
    coordinator: RFPresenceCoordinator, w_px: int = 640, h_px: int = 480
) -> bytes:
    """Render the live 3D RTI map, RF links, rooms, trails, and targets as PNG bytes."""
    snap = _build_render_snapshot(coordinator)
    return render_heatmap_png_from_snapshot(snap, w_px=w_px, h_px=h_px)


class RFHeatmapCamera(Camera):
    """Camera entity serving the live 3D isometric RTI map as PNG with per-tick caching."""

    _attr_has_entity_name = True
    _attr_translation_key = "live_3d_map"
    _attr_icon = "mdi:cube-scan"

    def __init__(
        self, coordinator: RFPresenceCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__()
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_heatmap_camera"
        self.content_type = "image/png"
        self._cached_seq: int = -1
        self._cached_png: bytes | None = None
        self._render_lock = asyncio.Lock()
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=NAME,
            manufacturer="RF Presence Sense",
            model="Radio Tomographic Imaging (Wi-Fi + BLE 3D)",
            sw_version=VERSION,
        )

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return cached or freshly rendered live 3D PNG map bytes via executor."""
        seq = self._coordinator.recon_seq
        if self._cached_png is not None and self._cached_seq == seq:
            return self._cached_png

        async with self._render_lock:
            seq = self._coordinator.recon_seq
            if self._cached_png is not None and self._cached_seq == seq:
                return self._cached_png

            snapshot = _build_render_snapshot(self._coordinator)
            png_bytes: bytes = await self.hass.async_add_executor_job(
                render_heatmap_png_from_snapshot, snapshot
            )
            self._cached_png = png_bytes
            self._cached_seq = seq
            return png_bytes
