"""Live PNG 2D Radio Tomographic Imaging Heatmap Camera for Home Assistant.

Uses a pure-Python zero-dependency PNG encoder (zlib + struct) so that both
`/api/camera_proxy` thumbnails AND `/api/camera_proxy_stream` MJPEG/multipart
streams render reliably in Chrome, Safari, Firefox, and the HA Companion App
(unlike `image/svg+xml`, which browsers reject inside multipart streams).
"""

from __future__ import annotations

import struct
import zlib

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, NAME, PROTOCOL_BLE, TARGET_TYPE_HUMAN, VERSION
from .coordinator import RFPresenceCoordinator


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
        r, g, b = bg
        self.buf = bytearray([r, g, b] * (width * height))

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

        raw = bytearray()
        stride = self.w * 3
        for y in range(self.h):
            raw.append(0)  # PNG filter type 0 (None)
            start = y * stride
            raw.extend(self.buf[start : start + stride])

        ihdr = struct.pack("!IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0)
        idat = zlib.compress(bytes(raw), level=3)
        return (
            b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", idat)
            + _chunk(b"IEND", b"")
        )


def render_heatmap_png(
    coordinator: RFPresenceCoordinator, w_px: int = 480, h_px: int = 360
) -> bytes:
    """Render the live RTI heatmap, RF links, rooms, and targets as PNG bytes."""
    pad = 24
    plot_w = w_px - 2 * pad
    plot_h = h_px - 2 * pad

    fw = max(coordinator.floor_width, 1.0)
    fh = max(coordinator.floor_height, 1.0)
    scale_x = plot_w / fw
    scale_y = plot_h / fh

    def sx(x: float) -> int:
        return int(round(pad + x * scale_x))

    def sy(y: float) -> int:
        return int(round(pad + y * scale_y))

    canvas = _Canvas(w_px, h_px, (15, 23, 42))  # #0f172a
    canvas.fill_rect(pad, pad, w_px - pad, h_px - pad, (30, 41, 59))  # #1e293b
    canvas.stroke_rect(pad, pad, w_px - pad, h_px - pad, (51, 65, 85), 2)

    # 1. RTI Heatmap voxels
    recon = coordinator.last_reconstruction
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
    for room in coordinator.rooms:
        canvas.stroke_rect(
            sx(room.x_min),
            sy(room.y_min),
            sx(room.x_max),
            sy(room.y_max),
            (100, 116, 139),
            1,
        )

    # 3. RF Links
    perts = recon.link_perturbations if recon else {}
    for lid, link in coordinator.engine.links.items():
        pert = perts.get(lid, 0.0)
        if pert > 1.2:
            col = (249, 115, 22)  # bright orange perturbed link
            alpha = 0.75
        elif link.protocol == PROTOCOL_BLE:
            col = (167, 139, 250)  # violet BLE
            alpha = 0.28
        else:
            col = (56, 189, 248)  # cyan Wi-Fi
            alpha = 0.35
        canvas.draw_line(
            sx(link.p1.x),
            sy(link.p1.y),
            sx(link.p2.x),
            sy(link.p2.y),
            col,
            alpha=alpha,
            dashed=(pert <= 1.2),
        )

    # 4. Stationary Devices & Receivers
    for dev in coordinator.devices.values():
        dx, dy = sx(float(dev.get("x", 0.0))), sy(float(dev.get("y", 0.0)))
        col = (192, 132, 252) if dev.get("protocol") == PROTOCOL_BLE else (56, 189, 248)
        canvas.fill_circle(dx, dy, 4, col)

    for recv in coordinator.receivers.values():
        rx, ry = sx(float(recv.get("x", 0.0))), sy(float(recv.get("y", 0.0)))
        canvas.fill_rect(rx - 5, ry - 5, rx + 6, ry + 6, (250, 204, 21))

    # 5. Detected Targets (Humans = Emerald Green, Animals = Warm Orange)
    for target in coordinator.last_targets:
        tx, ty = sx(target.x), sy(target.y)
        is_human = target.target_type == TARGET_TYPE_HUMAN
        col = (16, 185, 129) if is_human else (251, 146, 60)
        if target.speed_mps > 0.15:
            canvas.draw_line(
                tx,
                ty,
                sx(target.x + target.vx * 0.8),
                sy(target.y + target.vy * 0.8),
                col,
                alpha=0.95,
            )
        canvas.fill_circle(tx, ty, 13, col, alpha=0.28)
        canvas.fill_circle(tx, ty, 7, (255, 255, 255))
        canvas.fill_circle(tx, ty, 5, col)

    return canvas.to_png()


class RFHeatmapCamera(Camera):
    """Camera entity serving the live 2D RTI heatmap as PNG."""

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
        """Return live PNG heatmap bytes."""
        return render_heatmap_png(self._coordinator)
