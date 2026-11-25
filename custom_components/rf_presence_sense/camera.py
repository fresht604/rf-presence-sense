"""Live PNG 2D Radio Tomographic Imaging Heatmap Camera for Home Assistant.

Uses a pure-Python zero-dependency PNG encoder (zlib + struct) so that both
`/api/camera_proxy` thumbnails AND `/api/camera_proxy_stream` MJPEG/multipart
streams render reliably in Chrome, Safari, Firefox, and the HA Companion App
(unlike `image/svg+xml`, which browsers reject inside multipart streams).

Rendering is offloaded to Home Assistant's executor pool and cached per
reconstruction sequence number to prevent event-loop stalls and RAM churn.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the live RF Heatmap camera entity."""
    coordinator: RFPresenceCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([RFHeatmapCamera(coordinator, entry)])


class _Canvas:
    """Minimal fast RGB raster canvas with pure-Python PNG encoding."""

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
    ) -> None:
        dx = abs(x1 - x0)
        dy = -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        step = 0
        while True:
            if not dashed or (step % 8) < 5:
                self._blend_px(x0, y0, color, alpha)
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
class _RenderSnapshot:
    """Immutable snapshot of coordinator state safe to render in an executor thread."""

    floor_width: float
    floor_height: float
    recon: RTIReconstruction | None
    rooms: tuple[tuple[float, float, float, float], ...]
    links: tuple[tuple[str, str, float, float, float, float], ...]
    devices: tuple[tuple[float, float, str], ...]
    receivers: tuple[tuple[float, float], ...]
    targets: tuple[tuple[float, float, float, float, float, bool], ...]


def _build_render_snapshot(coordinator: Any) -> _RenderSnapshot:
    """Capture an immutable snapshot of floorplan, links, and targets on the event loop."""
    if hasattr(coordinator.engine, "snapshot_links"):
        link_snap = tuple(coordinator.engine.snapshot_links())
    else:
        link_snap = tuple(
            (lid, link.protocol, link.p1.x, link.p1.y, link.p2.x, link.p2.y)
            for lid, link in coordinator.engine.links.items()
        )

    rooms_snap = tuple(
        (room.x_min, room.y_min, room.x_max, room.y_max)
        for room in coordinator.rooms
    )
    devices_snap = tuple(
        (
            float(dev.get("x", 0.0)),
            float(dev.get("y", 0.0)),
            str(dev.get("protocol", "wifi")),
        )
        for dev in coordinator.devices.values()
    )
    receivers_snap = tuple(
        (float(recv.get("x", 0.0)), float(recv.get("y", 0.0)))
        for recv in coordinator.receivers.values()
    )
    targets_snap = tuple(
        (
            target.x,
            target.y,
            target.vx,
            target.vy,
            target.speed_mps,
            target.target_type == TARGET_TYPE_HUMAN,
        )
        for target in coordinator.last_targets
    )
    return _RenderSnapshot(
        floor_width=max(float(coordinator.floor_width), 1.0),
        floor_height=max(float(coordinator.floor_height), 1.0),
        recon=coordinator.last_reconstruction,
        rooms=rooms_snap,
        links=link_snap,
        devices=devices_snap,
        receivers=receivers_snap,
        targets=targets_snap,
    )


def render_heatmap_png_from_snapshot(
    snap: _RenderSnapshot, w_px: int = 480, h_px: int = 360
) -> bytes:
    """Render an immutable _RenderSnapshot into PNG bytes (safe for executor threads)."""
    pad = 24
    plot_w = w_px - 2 * pad
    plot_h = h_px - 2 * pad

    scale_x = plot_w / snap.floor_width
    scale_y = plot_h / snap.floor_height

    def sx(x: float) -> int:
        return int(round(pad + x * scale_x))

    def sy(y: float) -> int:
        return int(round(pad + y * scale_y))

    canvas = _Canvas(w_px, h_px, (15, 23, 42))  # #0f172a
    canvas.fill_rect(pad, pad, w_px - pad, h_px - pad, (30, 41, 59))  # #1e293b
    canvas.stroke_rect(pad, pad, w_px - pad, h_px - pad, (51, 65, 85), 2)

    # 1. RTI Heatmap voxels
    recon = snap.recon
    if recon is not None:
        max_val = max((max(row) for row in recon.grid), default=0.0)
        norm = max(max_val, 2.5)
        for r in range(recon.height):
            y0 = sy(r * recon.resolution)
            y1 = sy((r + 1) * recon.resolution)
            for c in range(recon.width):
                val = recon.grid[r][c]
                if val <= 0.08:
                    continue
                t = min(1.0, val / norm)
                if t < 0.5:
                    u = t * 2.0
                    col = (int(56 + u * 190), int(189 - u * 40), int(248 - u * 210))
                else:
                    u = (t - 0.5) * 2.0
                    col = (int(246 - u * 7), int(149 - u * 110), int(38 + u * 30))
                alpha = 0.18 + 0.72 * t
                x0 = sx(c * recon.resolution)
                x1 = sx((c + 1) * recon.resolution)
                canvas.fill_rect(x0, y0, x1, y1, col, alpha)

    # 2. Rooms
    for x_min, y_min, x_max, y_max in snap.rooms:
        canvas.stroke_rect(
            sx(x_min),
            sy(y_min),
            sx(x_max),
            sy(y_max),
            (100, 116, 139),
            1,
        )

    # 3. RF Links
    perts = recon.link_perturbations if recon else {}
    for lid, protocol, p1x, p1y, p2x, p2y in snap.links:
        pert = perts.get(lid, 0.0)
        if pert > 1.2:
            col = (249, 115, 22)  # bright orange perturbed link
            alpha = 0.75
        elif protocol == PROTOCOL_BLE:
            col = (167, 139, 250)  # violet BLE
            alpha = 0.28
        else:
            col = (56, 189, 248)  # cyan Wi-Fi
            alpha = 0.35
        canvas.draw_line(
            sx(p1x),
            sy(p1y),
            sx(p2x),
            sy(p2y),
            col,
            alpha=alpha,
            dashed=(pert <= 1.2),
        )

    # 4. Stationary Devices & Receivers
    for dev_x, dev_y, proto in snap.devices:
        dx, dy = sx(dev_x), sy(dev_y)
        col = (192, 132, 252) if proto == PROTOCOL_BLE else (56, 189, 248)
        canvas.fill_circle(dx, dy, 4, col)

    for recv_x, recv_y in snap.receivers:
        rx, ry = sx(recv_x), sy(recv_y)
        canvas.fill_rect(rx - 5, ry - 5, rx + 6, ry + 6, (250, 204, 21))

    # 5. Detected Targets (Humans = Emerald Green, Animals = Warm Orange)
    for tx_m, ty_m, vx, vy, speed_mps, is_human in snap.targets:
        tx, ty = sx(tx_m), sy(ty_m)
        col = (16, 185, 129) if is_human else (251, 146, 60)
        if speed_mps > 0.15:
            canvas.draw_line(
                tx,
                ty,
                sx(tx_m + vx * 0.8),
                sy(ty_m + vy * 0.8),
                col,
                alpha=0.95,
            )
        canvas.fill_circle(tx, ty, 13, col, alpha=0.28)
        canvas.fill_circle(tx, ty, 7, (255, 255, 255))
        canvas.fill_circle(tx, ty, 5, col)

    return canvas.to_png()


def render_heatmap_png(
    coordinator: RFPresenceCoordinator, w_px: int = 480, h_px: int = 360
) -> bytes:
    """Render the live RTI heatmap, RF links, rooms, and targets as PNG bytes."""
    snap = _build_render_snapshot(coordinator)
    return render_heatmap_png_from_snapshot(snap, w_px=w_px, h_px=h_px)


class RFHeatmapCamera(Camera):
    """Camera entity serving the live 2D RTI heatmap as PNG with per-tick caching."""

    _attr_has_entity_name = True
    _attr_name = "Live RF-Heatmap"
    _attr_icon = "mdi:map-marker-radius"

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
            model="Radio Tomographic Imaging (Wi-Fi + BLE)",
            sw_version=VERSION,
        )

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return cached or freshly rendered live PNG heatmap bytes via executor."""
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
