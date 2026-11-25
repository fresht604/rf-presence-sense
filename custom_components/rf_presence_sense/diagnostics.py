"""Diagnostics export for RF Presence Sense."""

from __future__ import annotations

import re
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, VERSION
from .coordinator import RFPresenceCoordinator

_MAC_RE = re.compile(r"(?:[0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}")


def _redact_id(value: str) -> str:
    """Anonymize raw MAC addresses while keeping prefix/suffix for debugging."""

    def _mask(match: re.Match[str]) -> str:
        raw = match.group(0)
        return f"{raw[:5]}:**:**:{raw[-2:]}"

    return _MAC_RE.sub(_mask, value)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return anonymized diagnostic snapshot for troubleshooting RF links & geometry."""
    coordinator: RFPresenceCoordinator | None = hass.data.get(DOMAIN, {}).get(
        entry.entry_id
    )
    if coordinator is None:
        return {"version": VERSION, "status": "not_loaded"}

    with coordinator.engine._lock:
        link_stats = [
            {
                "link_id": _redact_id(lid),
                "protocol": link.protocol,
                "p1": [round(link.p1.x, 2), round(link.p1.y, 2), round(link.p1.z, 2)],
                "p2": [round(link.p2.x, 2), round(link.p2.y, 2), round(link.p2.z, 2)],
                "baseline_rssi": (
                    round(link.baseline_rssi, 2)
                    if link.baseline_rssi is not None
                    else None
                ),
                "baseline_std": round(link.baseline_std, 3),
                "last_rssi": (
                    round(link.last_rssi, 2) if link.last_rssi is not None else None
                ),
                "samples_in_window": len(link.samples),
                "stale_timeout_s": link.stale_seconds,
            }
            for lid, link in coordinator.engine.links.items()
        ]

    data = coordinator.data or {}
    return {
        "version": VERSION,
        "geometry": coordinator.engine.get_geometry_summary(),
        "tuning": {
            "sensitivity": coordinator.sensitivity,
            "calibration_duration": coordinator.calibration_duration,
            "presence_aware_baseline": coordinator.presence_aware_baseline,
            "mask_outside_rooms": coordinator.mask_outside_rooms,
            "away_entity_configured": bool(coordinator.away_entity),
            "update_interval_s": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
        },
        "runtime": {
            "recon_seq": coordinator.recon_seq,
            "calibrating": coordinator._calibrating,
            "packets_received": coordinator.collector.packet_count,
            "active_links": data.get("active_links", 0),
            "total_links": data.get("total_links", 0),
            "link_health_pct": data.get("link_health_pct", 0.0),
            "humans_count": data.get("humans_count", 0),
            "animals_count": data.get("animals_count", 0),
            "fingerprint_count": coordinator.engine.fingerprint_count,
            "edge_walk_active": coordinator._edge_walk_state is not None,
        },
        "rooms": [r.to_dict() for r in coordinator.rooms],
        "receivers_count": len(coordinator.receivers),
        "devices_count": len(coordinator.devices),
        "links": link_stats,
    }
