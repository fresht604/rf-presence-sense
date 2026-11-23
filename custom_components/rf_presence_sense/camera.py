"""Live SVG 2D Radio Tomographic Imaging Heatmap Camera for Home Assistant."""

from __future__ import annotations

from html import escape

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


def render_heatmap_svg(coordinator: RFPresenceCoordinator) -> str:
    """Render the current RTI heatmap, RF links, rooms, and targets as crisp SVG."""
    w_px = 640
    h_px = 480
    pad_l, pad_r, pad_t, pad_b = 36, 20, 44, 34
    plot_w = w_px - pad_l - pad_r
    plot_h = h_px - pad_t - pad_b

    fw = max(coordinator.floor_width, 1.0)
    fh = max(coordinator.floor_height, 1.0)
    scale_x = plot_w / fw
    scale_y = plot_h / fh

    def sx(x: float) -> float:
        return pad_l + x * scale_x

    def sy(y: float) -> float:
        return pad_t + y * scale_y

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w_px} {h_px}" width="{w_px}" height="{h_px}">',
        '<rect width="100%" height="100%" fill="#0f172a"/>',
        f'<rect x="{pad_l}" y="{pad_t}" width="{plot_w}" height="{plot_h}" fill="#1e293b" stroke="#334155" stroke-width="1.5" rx="6"/>',
    ]

    # 1. RTI Heatmap voxels
    recon = coordinator.last_reconstruction
    if recon is not None:
        max_val = max(
            (max(row) for row in recon.grid),
            default=0.0,
        )
        norm = max(max_val, 2.5)
        cell_w = recon.resolution * scale_x
        cell_h = recon.resolution * scale_y
        for r in range(recon.height):
            for c in range(recon.width):
                val = recon.grid[r][c]
                if val <= 0.08:
                    continue
                t = min(1.0, val / norm)
                # Blue -> Amber -> Crimson heat ramp
                if t < 0.5:
                    u = t * 2.0
                    red = int(56 + u * 190)
                    green = int(189 - u * 40)
                    blue = int(248 - u * 210)
                else:
                    u = (t - 0.5) * 2.0
                    red = int(246 - u * 7)
                    green = int(149 - u * 110)
                    blue = int(38 + u * 30)
                alpha = round(0.15 + 0.72 * t, 2)
                rx = sx(c * recon.resolution)
                ry = sy(r * recon.resolution)
                parts.append(
                    f'<rect x="{rx:.1f}" y="{ry:.1f}" width="{cell_w + 0.6:.1f}" '
                    f'height="{cell_h + 0.6:.1f}" fill="rgb({red},{green},{blue})" '
                    f'fill-opacity="{alpha}"/>'
                )

    # 2. Rooms
    for room in coordinator.rooms:
        rx = sx(room.x_min)
        ry = sy(room.y_min)
        rw = (room.x_max - room.x_min) * scale_x
        rh = (room.y_max - room.y_min) * scale_y
        parts.append(
            f'<rect x="{rx:.1f}" y="{ry:.1f}" width="{rw:.1f}" height="{rh:.1f}" '
            f'fill="none" stroke="#64748b" stroke-width="1.2" stroke-dasharray="5,4"/>'
        )
        parts.append(
            f'<text x="{rx + 6:.1f}" y="{ry + 16:.1f}" fill="#94a3b8" '
            f'font-family="sans-serif" font-size="11">{escape(room.name)}</text>'
        )

    # 3. RF Links
    perts = (recon.link_perturbations if recon else {})
    for lid, link in coordinator.engine.links.items():
        pert = perts.get(lid, 0.0)
        base_color = "#a78bfa" if link.protocol == PROTOCOL_BLE else "#38bdf8"
        if pert > 1.2:
            stroke = "#f97316"
            opacity = "0.75"
            width = "2.0"
        else:
            stroke = base_color
            opacity = "0.28"
            width = "1.0"
        parts.append(
            f'<line x1="{sx(link.p1.x):.1f}" y1="{sy(link.p1.y):.1f}" '
            f'x2="{sx(link.p2.x):.1f}" y2="{sy(link.p2.y):.1f}" '
            f'stroke="{stroke}" stroke-opacity="{opacity}" stroke-width="{width}" '
            f'stroke-dasharray="4,3"/>'
        )

    # 4. Stationary Devices & Receivers
    for dev in coordinator.devices.values():
        dx, dy = sx(float(dev.get("x", 0.0))), sy(float(dev.get("y", 0.0)))
        col = "#c084fc" if dev.get("protocol") == PROTOCOL_BLE else "#38bdf8"
        parts.append(
            f'<circle cx="{dx:.1f}" cy="{dy:.1f}" r="4.2" fill="{col}" stroke="#0f172a" stroke-width="1.2"/>'
        )

    for recv in coordinator.receivers.values():
        rx, ry = sx(float(recv.get("x", 0.0))), sy(float(recv.get("y", 0.0)))
        parts.append(
            f'<rect x="{rx - 5.5:.1f}" y="{ry - 5.5:.1f}" width="11" height="11" '
            f'fill="#facc15" stroke="#0f172a" stroke-width="1.5" rx="2"/>'
        )

    # 5. Detected Targets (Humans & Animals)
    for target in coordinator.last_targets:
        tx, ty = sx(target.x), sy(target.y)
        is_human = target.target_type == TARGET_TYPE_HUMAN
        color = "#10b981" if is_human else "#fb923c"
        label = (
            f"Mensch #{target.track_id}"
            if is_human
            else f"Tier #{target.track_id}"
        )
        # Velocity arrow
        if target.speed_mps > 0.15:
            vx_end = sx(target.x + target.vx * 0.8)
            vy_end = sy(target.y + target.vy * 0.8)
            parts.append(
                f'<line x1="{tx:.1f}" y1="{ty:.1f}" x2="{vx_end:.1f}" y2="{vy_end:.1f}" '
                f'stroke="{color}" stroke-width="2.2"/>'
            )
        # Pulse halo + marker
        parts.append(
            f'<circle cx="{tx:.1f}" cy="{ty:.1f}" r="14" fill="{color}" fill-opacity="0.22"/>'
        )
        parts.append(
            f'<circle cx="{tx:.1f}" cy="{ty:.1f}" r="7" fill="{color}" stroke="#ffffff" stroke-width="2"/>'
        )
        parts.append(
            f'<text x="{tx + 10:.1f}" y="{ty - 8:.1f}" fill="#f8fafc" '
            f'font-family="sans-serif" font-size="11" font-weight="bold">'
            f'{escape(label)} ({target.x:.1f}m, {target.y:.1f}m)</text>'
        )

    # 6. Header & Footer HUD
    data = coordinator.data or {}
    humans = data.get("humans_count", 0)
    animals = data.get("animals_count", 0)
    active_links = data.get("active_links", 0)
    total_links = data.get("total_links", 0)
    status = (
        "KALIBRIERUNG LÄUFT..."
        if data.get("calibrating")
        else f"Menschen: {humans} | Tiere: {animals} | Aktive RF-Links: {active_links}/{total_links}"
    )
    parts.append(
        f'<text x="{pad_l}" y="26" fill="#f8fafc" font-family="sans-serif" '
        f'font-size="14" font-weight="bold">RF Presence Sense — Live Radio-Tomographie</text>'
    )
    parts.append(
        f'<text x="{w_px - pad_r}" y="26" text-anchor="end" fill="#94a3b8" '
        f'font-family="sans-serif" font-size="12">{escape(status)}</text>'
    )
    parts.append(
        f'<text x="{pad_l}" y="{h_px - 10}" fill="#64748b" font-family="sans-serif" font-size="10">'
        f'Grundriss: {fw:.1f}m × {fh:.1f}m | ■ Gelb: Router/BT-Host | ● Blau: WLAN | ● Lila: BLE | ● Grün: Mensch | ● Orange: Tier'
        f'</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


class RFHeatmapCamera(Camera):
    """Camera entity serving the live 2D RTI heatmap SVG."""

    _attr_has_entity_name = True
    _attr_name = "Live RF-Heatmap"
    _attr_icon = "mdi:map-marker-radius"

    def __init__(
        self, coordinator: RFPresenceCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__()
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_heatmap_camera"
        self.content_type = "image/svg+xml"
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
        """Return live SVG heatmap bytes."""
        return render_heatmap_svg(self._coordinator).encode("utf-8")
