"""Signal Collector: harvests RSSI from existing Home Assistant Wi-Fi & BLE devices.

Requires NO extra ESP32 or Bluetooth Proxy hardware. Taps into:
  1. Existing HA RSSI / signal_strength / linkquality sensor entities (Shelly,
     Tasmota, ESPHome, UniFi, Fritz!Box, TP-Link, Meross, Zigbee2MQTT, ZHA, etc.)
  2. Existing HA device_tracker entities exposing 'rssi' / 'signal_strength' attrs
     (with dynamic Multi-AP tracking via ap_mac / bssid / router attributes)
  3. Native HA Bluetooth stack (homeassistant.components.bluetooth) passive
     advertisements from stationary BLE devices already in range.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
import logging
import time
from typing import Any

from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    BLE_MIN_INTERVAL_SECONDS,
    PROTOCOL_BLE,
    PROTOCOL_WIFI,
    PROTOCOL_ZIGBEE,
)

_LOGGER = logging.getLogger(__name__)

RSSI_ATTR_CANDIDATES = (
    "rssi",
    "signal_strength",
    "wifi_signal",
    "wifi_rssi",
    "signal",
    "signal_dbm",
    "linkquality",
)

RSSI_ENTITY_KEYWORDS = (
    "rssi",
    "signal_strength",
    "wifi_signal",
    "signal_level",
    "linkquality",
)

MOBILE_DEVICE_KEYWORDS = (
    "handy",
    "phone",
    "iphone",
    "android",
    "pixel",
    "watch",
    "airpods",
    "buds",
    "jabra",
    "headset",
    "remote",
    "laptop",
    "macbook",
    "ipad",
    "tablet",
    "sena",
    "garmin",
    "fitbit",
    "airtag",
    "smarttag",
    "-pc",
    "tim-pc",
)


def is_likely_stationary(name: str, identifier: str) -> bool:
    """Return True if a discovered Wi-Fi/BLE device looks stationary (not mobile/random-MAC)."""
    norm_name = (name or "").strip().lower()
    norm_id = (identifier or "").strip().lower()
    # Reject unnamed BLE devices whose name is just their MAC address (rotating random MACs)
    if not norm_name or norm_name == norm_id or norm_name.replace("-", ":") == norm_id:
        return False
    # Reject obvious mobile phones, wearables, headsets, remotes, laptops
    combined = f"{norm_name} {norm_id}"
    if any(kw in combined for kw in MOBILE_DEVICE_KEYWORDS):
        return False
    return True


def _parse_float(value: Any) -> float | None:
    """Safely parse a numeric RSSI value."""
    if value is None or isinstance(value, bool):
        return None
    try:
        val = float(value)
    except (TypeError, ValueError):
        return None
    if val != val:  # NaN check
        return None
    return val


def extract_receiver_from_state(state: State | None, fallback: str = "router_main") -> str:
    """Dynamically resolve AP / router receiver ID from live entity attributes."""
    if state is None:
        return fallback
    attrs = state.attributes
    for key in ("ap_mac", "bssid", "router"):
        val = attrs.get(key)
        if val is not None:
            sval = str(val).strip()
            if sval:
                return sval
    return fallback


def extract_rssi_from_state(state: State | None) -> tuple[float | None, str]:
    """Extract an RSSI value (in dBm) and protocol hint from a HA State object.

    Handles both direct dBm sensors, device_tracker attributes, and Zigbee LQI
    (0..255 converted linearly to pseudo-dBm -95..-30).
    """
    if state is None or state.state in ("unknown", "unavailable", "none", ""):
        return (None, PROTOCOL_WIFI)

    attrs = state.attributes
    unit = str(attrs.get("unit_of_measurement", "")).lower()
    dev_class = str(attrs.get("device_class", "")).lower()
    entity_id_lower = state.entity_id.lower()

    # 1. Direct sensor value in dBm
    if dev_class == "signal_strength" or unit == "dbm":
        val = _parse_float(state.state)
        if val is not None and -120.0 <= val <= 0.0:
            proto = (
                PROTOCOL_BLE
                if "ble" in entity_id_lower or "bluetooth" in entity_id_lower
                else PROTOCOL_WIFI
            )
            return (val, proto)

    # 2. Zigbee/Thread LQI (0..255) -> pseudo-dBm
    if "linkquality" in entity_id_lower or unit == "lqi":
        lqi = _parse_float(state.state)
        if lqi is not None and 0.0 <= lqi <= 255.0:
            pseudo_dbm = -95.0 + (lqi / 255.0) * 65.0
            return (pseudo_dbm, PROTOCOL_ZIGBEE)

    # 3. Direct numeric sensor with RSSI-like name
    if any(kw in entity_id_lower for kw in RSSI_ENTITY_KEYWORDS):
        val = _parse_float(state.state)
        if val is not None:
            if -120.0 <= val <= 0.0:
                return (val, PROTOCOL_WIFI)
            if 0.0 <= val <= 100.0 and unit == "%":
                # Convert Wi-Fi quality % (0..100) to approximate dBm (-100..-50)
                return (-100.0 + 0.5 * val, PROTOCOL_WIFI)

    # 4. Attributes on device_tracker or other entities (UniFi, Fritz!Box, OpenWrt, etc.)
    for attr_key in RSSI_ATTR_CANDIDATES:
        if attr_key in attrs:
            val = _parse_float(attrs[attr_key])
            if val is None:
                continue
            if attr_key == "linkquality" and 0.0 <= val <= 255.0:
                return (-95.0 + (val / 255.0) * 65.0, PROTOCOL_ZIGBEE)
            if -120.0 <= val <= 0.0:
                return (val, PROTOCOL_WIFI)

    return (None, PROTOCOL_WIFI)


def discover_entity_areas(
    hass: HomeAssistant, entity_ids: Iterable[str]
) -> dict[str, str]:
    """Map Home Assistant entity_ids to their assigned HA Area name (if any)."""
    areas: dict[str, str] = {}
    try:
        from homeassistant.helpers import (  # pylint: disable=import-outside-toplevel
            area_registry as ar,
            device_registry as dr,
            entity_registry as er,
        )

        ent_reg = er.async_get(hass)
        dev_reg = dr.async_get(hass)
        area_reg = ar.async_get(hass)

        for eid in entity_ids:
            ent_entry = ent_reg.async_get(eid)
            if ent_entry is None:
                continue
            area_id = ent_entry.area_id
            if not area_id and ent_entry.device_id:
                dev_entry = dev_reg.async_get(ent_entry.device_id)
                if dev_entry is not None:
                    area_id = dev_entry.area_id
            if area_id:
                area_entry = area_reg.async_get_area(area_id)
                if area_entry is not None and area_entry.name:
                    areas[eid] = str(area_entry.name)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("HA area lookup unavailable: %s", err)
    return areas


def discover_rssi_entities(hass: HomeAssistant) -> dict[str, dict[str, Any]]:
    """Scan all current HA states for entities providing Wi-Fi/RF RSSI."""
    discovered: dict[str, dict[str, Any]] = {}
    for state in hass.states.async_all():
        rssi, proto = extract_rssi_from_state(state)
        if rssi is not None:
            friendly = state.attributes.get("friendly_name", state.entity_id)
            receiver_hint = extract_receiver_from_state(
                state,
                fallback=str(state.attributes.get("essid") or "router_main"),
            )
            discovered[state.entity_id] = {
                "entity_id": state.entity_id,
                "name": str(friendly),
                "rssi": rssi,
                "protocol": proto,
                "receiver_id": receiver_hint,
            }
    return discovered


def discover_ble_devices(hass: HomeAssistant) -> dict[str, dict[str, Any]]:
    """Discover BLE devices currently visible to Home Assistant's native Bluetooth stack."""
    discovered: dict[str, dict[str, Any]] = {}
    try:
        from homeassistant.components import bluetooth  # pylint: disable=import-outside-toplevel

        for service_info in bluetooth.async_discovered_service_info(
            hass, connectable=False
        ):
            address = service_info.address
            if not address or service_info.rssi is None:
                continue
            name = service_info.name or address
            source = getattr(service_info, "source", None) or "ha_bluetooth"
            discovered[address] = {
                "address": address,
                "name": str(name),
                "rssi": float(service_info.rssi),
                "protocol": PROTOCOL_BLE,
                "receiver_id": str(source),
            }
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Native Bluetooth discovery unavailable: %s", err)
    return discovered


class SignalCollector:
    """Subscribes to HA state changes and native BLE advertisements to feed RSSI samples."""

    def __init__(
        self,
        hass: HomeAssistant,
        on_sample: Callable[[str, str, float, str, float], None],
        enable_ble: bool = True,
        enable_wifi_entities: bool = True,
        ble_min_interval: float = BLE_MIN_INTERVAL_SECONDS,
    ) -> None:
        """Initialize the signal collector.

        on_sample callback signature:
            (device_id, receiver_id, rssi_dbm, protocol, timestamp_monotonic)
        """
        self.hass = hass
        self._on_sample = on_sample
        self._enable_ble = enable_ble
        self._enable_wifi_entities = enable_wifi_entities
        self._ble_min_interval = max(0.0, float(ble_min_interval))

        self._tracked_entities: dict[str, str] = {}  # entity_id -> default receiver_id
        self._tracked_ble_macs: set[str] = set()  # uppercase MACs
        self._last_ble_ts: dict[tuple[str, str], float] = {}
        self._unsub_state: Callable[[], None] | None = None
        self._unsub_ble: Callable[[], None] | None = None
        self._packet_count = 0

    def _should_accept_ble(self, addr: str, source: str, now: float) -> bool:
        """Rate-limit high-frequency BLE advertisements per (MAC, adapter) link."""
        key = (addr, source)
        last_ts = self._last_ble_ts.get(key)
        if last_ts is not None and (now - last_ts) < self._ble_min_interval:
            return False
        if len(self._last_ble_ts) > 256:
            cutoff = now - max(60.0, self._ble_min_interval * 10.0)
            self._last_ble_ts = {
                k: ts for k, ts in self._last_ble_ts.items() if ts >= cutoff
            }
        self._last_ble_ts[key] = now
        return True

    @property
    def packet_count(self) -> int:
        """Total RSSI packets/updates ingested since start."""
        return self._packet_count

    def configure(
        self,
        entity_receiver_map: dict[str, str],
        ble_addresses: set[str],
    ) -> None:
        """Update tracked Wi-Fi entities and BLE MAC addresses and rebind listeners."""
        self.stop()
        self._tracked_entities = dict(entity_receiver_map)
        self._tracked_ble_macs = {addr.upper() for addr in ble_addresses}
        self.start()

    def start(self) -> None:
        """Start listening to HA state changes and native Bluetooth advertisements."""
        if self._enable_wifi_entities and self._tracked_entities:
            entity_ids = list(self._tracked_entities.keys())
            self._unsub_state = async_track_state_change_event(
                self.hass, entity_ids, self._handle_state_event
            )
            # Seed initial values immediately
            now = time.monotonic()
            for eid, default_recv_id in self._tracked_entities.items():
                state = self.hass.states.get(eid)
                rssi, proto = extract_rssi_from_state(state)
                if rssi is not None:
                    recv_id = extract_receiver_from_state(state, fallback=default_recv_id)
                    self._packet_count += 1
                    self._on_sample(eid, recv_id, rssi, proto, now)

        if self._enable_ble and self._tracked_ble_macs:
            self._start_ble_listener()

    def _start_ble_listener(self) -> None:
        """Attach passive callback to HA's built-in Bluetooth integration."""
        try:
            from homeassistant.components import bluetooth  # pylint: disable=import-outside-toplevel
            from homeassistant.components.bluetooth import (  # pylint: disable=import-outside-toplevel
                BluetoothCallbackMatcher,
                BluetoothChange,
                BluetoothScanningMode,
                BluetoothServiceInfoBleak,
            )

            @callback
            def _ble_callback(
                service_info: BluetoothServiceInfoBleak,
                _change: BluetoothChange,
            ) -> None:
                addr = service_info.address.upper()
                if addr not in self._tracked_ble_macs:
                    return
                if service_info.rssi is None:
                    return
                source = str(getattr(service_info, "source", None) or "ha_bluetooth")
                now = time.monotonic()
                if not self._should_accept_ble(addr, source, now):
                    return
                self._packet_count += 1
                self._on_sample(
                    addr,
                    source,
                    float(service_info.rssi),
                    PROTOCOL_BLE,
                    now,
                )

            self._unsub_ble = bluetooth.async_register_callback(
                self.hass,
                _ble_callback,
                BluetoothCallbackMatcher(connectable=False),
                BluetoothScanningMode.PASSIVE,
            )
            _LOGGER.info(
                "RF Presence Sense attached to native HA Bluetooth for %d stationary BLE devices",
                len(self._tracked_ble_macs),
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Could not attach to native HA Bluetooth (Wi-Fi RSSI mode still active): %s",
                err,
            )

    @callback
    def _handle_state_event(self, event: Event) -> None:
        """Handle real-time state change on a tracked Wi-Fi/RF RSSI entity (with Multi-AP)."""
        new_state: State | None = event.data.get("new_state")
        if new_state is None:
            return
        rssi, proto = extract_rssi_from_state(new_state)
        if rssi is None:
            return
        default_recv = self._tracked_entities.get(new_state.entity_id, "router_main")
        recv_id = extract_receiver_from_state(new_state, fallback=default_recv)
        self._packet_count += 1
        self._on_sample(
            new_state.entity_id,
            recv_id,
            rssi,
            proto,
            time.monotonic(),
        )

    def stop(self) -> None:
        """Unsubscribe all listeners and release rate-limit state."""
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        if self._unsub_ble is not None:
            self._unsub_ble()
            self._unsub_ble = None
        self._last_ble_ts.clear()
