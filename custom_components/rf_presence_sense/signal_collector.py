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


def _norm_text(value: str) -> str:
    """Normalize German/English room & device text for fuzzy matching."""
    return (
        (value or "")
        .strip()
        .lower()
        .replace("ä", "ae")
        .replace("ö", "oe")
        .replace("ü", "ue")
        .replace("ß", "ss")
    )


def _norm_mac(value: str) -> str:
    """Normalize a MAC-like identifier to uppercase hex without separators."""
    cleaned = "".join(ch for ch in (value or "").upper() if ch in "0123456789ABCDEF")
    return cleaned if len(cleaned) == 12 else ""


# Semantic room groups (canonical token -> keywords in device/area/room names)
_ROOM_SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("bad", "badezimmer", "bath", "bathroom", "wc", "dusche", "bz"),
    ("arbeitszimmer", "buero", "office", "arbeit", "az", "printer", "drucker", "xp-4200", "xp4200", "desk", "schreibtisch"),
    ("wohnzimmer", "wohn", "living", "wz", "sofa", "couch", "govee", "fernseher", "tv"),
    ("kueche", "kitchen", "kue", "fridge", "kuehlschrank", "kuehl", "herd", "ofen", "geschirrspueler"),
    ("schlafzimmer", "schlaf", "bedroom", "sz", "bett", "bed"),
    ("eingangsbereich", "eingang", "hauseingang", "entrance", "briefkasten", "haustuer", "vorraum"),
    ("flur", "diele", "gang", "hall", "hallway", "corridor"),
    ("balkon", "terrasse", "balcony", "garten", "garden"),
    ("kinderzimmer", "kinder", "kids", "kz"),
    ("abstellraum", "abstell", "speisekammer", "keller", "basement", "storage"),
)


def _tokenize(text: str) -> set[str]:
    """Split normalized text into alphanumeric tokens (handles BZ, AZ, WZ cleanly)."""
    norm = _norm_text(text)
    tokens: set[str] = set()
    cur: list[str] = []
    for ch in norm:
        if ch.isalnum():
            cur.append(ch)
        elif cur:
            tokens.add("".join(cur))
            cur = []
    if cur:
        tokens.add("".join(cur))
    return tokens


def infer_room_from_name(
    text: str, rooms: list[Any], area_hint: str = ""
) -> str | None:
    """Match a device/receiver name + optional HA area against configured RoomZone list."""
    if not rooms:
        return None

    # Build normalized signatures for each configured room
    room_sigs: list[tuple[str, str, str, set[str], set[int]]] = []
    for room in rooms:
        rid = _norm_text(str(getattr(room, "room_id", "")))
        rname = _norm_text(str(getattr(room, "name", "")))
        rtokens = _tokenize(f"{rid} {rname}")
        groups: set[int] = set()
        for g_idx, group in enumerate(_ROOM_SYNONYM_GROUPS):
            if any(
                kw in rtokens or (len(kw) >= 4 and (kw in rname or kw in rid))
                for kw in group
            ):
                groups.add(g_idx)
        room_sigs.append((str(room.room_id), rid, rname, rtokens, groups))

    # Try HA Area first (highest confidence), then device/receiver display text
    for candidate in (area_hint, text):
        cand_norm = _norm_text(candidate)
        if not cand_norm:
            continue
        cand_tokens = _tokenize(cand_norm)

        # 1. Exact or substring match against room name / room_id
        for room_id, rid, rname, rtokens, _ in room_sigs:
            if cand_norm == rname or cand_norm == rid:
                return room_id
            if len(rname) >= 3 and rname in cand_norm:
                return room_id
            if len(rid) >= 3 and rid in cand_norm:
                return room_id
            if len(cand_norm) >= 3 and (cand_norm in rname or cand_norm in rid):
                return room_id
            if cand_tokens & rtokens:
                return room_id

        # 2. Synonym group match (e.g. "Thermometer BZ" -> Badezimmer, "XP-4200" -> Arbeitszimmer)
        cand_groups: set[int] = set()
        for g_idx, group in enumerate(_ROOM_SYNONYM_GROUPS):
            for kw in group:
                if len(kw) <= 3:
                    if kw in cand_tokens:
                        cand_groups.add(g_idx)
                        break
                elif kw in cand_norm:
                    cand_groups.add(g_idx)
                    break
        if cand_groups:
            for room_id, _, _, _, r_groups in room_sigs:
                if cand_groups & r_groups:
                    return room_id
            # Fallback: if candidate mentions balkon/terrasse/garten and no balcony room
            # is configured, map it to wohnzimmer (typically adjacent balcony access).
            balkon_idx = next(
                (i for i, g in enumerate(_ROOM_SYNONYM_GROUPS) if "balkon" in g),
                None,
            )
            wz_idx = next(
                (i for i, g in enumerate(_ROOM_SYNONYM_GROUPS) if "wohnzimmer" in g),
                None,
            )
            if balkon_idx is not None and wz_idx is not None and balkon_idx in cand_groups:
                for room_id, _, _, _, r_groups in room_sigs:
                    if wz_idx in r_groups:
                        return room_id

    return None


def infer_height_from_name(
    name: str, protocol: str = PROTOCOL_WIFI, is_receiver: bool = False
) -> float:
    """Estimate realistic 3D mounting height z (in meters) from device name/type."""
    norm = _norm_text(name)
    if any(k in norm for k in ("pstrip", "powerstrip", "plug", "steckdose", "outlet", "socket")):
        return 0.35
    if any(k in norm for k in ("trv", "thermostat", "heizung", "radiator", "valve")):
        return 0.55
    if any(k in norm for k in ("fridge", "kuehlschrank", "kuehl", "printer", "drucker", "xp-4200", "xp4200", "tisch", "desk")):
        return 1.10
    if any(k in norm for k in ("lamp", "lampe", "light", "govee", "led", "bulb", "decke", "ceiling", "leuchte")):
        return 1.65
    if any(k in norm for k in ("thermometer", "sbdw", "tuer", "door", "fenster", "window", "briefkasten", "sensor", "switch", "schalter", "button", "taster")):
        return 1.40
    if is_receiver:
        return 1.20
    return 1.0 if protocol == PROTOCOL_WIFI else 0.95


def _lookup_mac_fuzzy(nm: str, mac_to_dev: dict[str, Any]) -> Any | None:
    """Resolve a normalized 12-hex MAC with ±3 last-byte fallback for ESP32/Shelly Wi-Fi vs BLE MACs."""
    if not nm:
        return None
    exact = mac_to_dev.get(nm)
    if exact is not None:
        return exact
    if len(nm) != 12:
        return None
    prefix = nm[:10]
    try:
        target_last = int(nm[10:], 16)
    except ValueError:
        return None
    best_dev: Any | None = None
    best_diff = 4
    for cand_mac, dev_entry in mac_to_dev.items():
        if len(cand_mac) != 12 or cand_mac[:10] != prefix:
            continue
        try:
            diff = abs(int(cand_mac[10:], 16) - target_last)
        except ValueError:
            continue
        if diff <= 3 and diff < best_diff:
            best_diff = diff
            best_dev = dev_entry
    return best_dev


def discover_devices_and_receivers_metadata(
    hass: HomeAssistant,
    device_ids: Iterable[str],
    receiver_ids: Iterable[str],
) -> dict[str, dict[str, str]]:
    """Resolve friendly names & HA Areas for entities, BLE MACs, and Bluetooth Proxys."""
    resolved: dict[str, dict[str, str]] = {}
    try:
        from homeassistant.helpers import (  # pylint: disable=import-outside-toplevel
            area_registry as ar,
            device_registry as dr,
            entity_registry as er,
        )

        ent_reg = er.async_get(hass)
        dev_reg = dr.async_get(hass)
        area_reg = ar.async_get(hass)

        def _area_name(area_id: str | None) -> str:
            if not area_id or area_reg is None:
                return ""
            area_entry = area_reg.async_get_area(area_id)
            return str(area_entry.name) if area_entry and area_entry.name else ""

        # Collect all HA device entries via official Area/Device Registry API first
        # (HA 2025/2026 dev_reg.devices is DeviceRegistryItems, NOT a plain dict!),
        # then merge any remaining devices via .values() duck-typing fallback.
        all_dev_entries: list[Any] = []
        seen_dev_keys: set[Any] = set()

        def _add_dev_entry(entry_obj: Any) -> None:
            key = getattr(entry_obj, "id", None) or id(entry_obj)
            if key not in seen_dev_keys:
                seen_dev_keys.add(key)
                all_dev_entries.append(entry_obj)

        if (
            dev_reg is not None
            and area_reg is not None
            and hasattr(area_reg, "async_list_areas")
            and hasattr(dr, "async_entries_for_area")
        ):
            for area_obj in area_reg.async_list_areas():
                area_id = getattr(area_obj, "id", None)
                if not area_id:
                    continue
                for dev_entry in dr.async_entries_for_area(dev_reg, area_id):
                    _add_dev_entry(dev_entry)

        raw_devs = getattr(dev_reg, "devices", None) if dev_reg is not None else None
        if raw_devs is not None and hasattr(raw_devs, "values"):
            for dev_entry in raw_devs.values():
                _add_dev_entry(dev_entry)

        # Build fast MAC -> device_entry lookup across all registered HA devices
        mac_to_dev: dict[str, Any] = {}
        name_to_dev: dict[str, Any] = {}
        for dev_entry in all_dev_entries:
            for _ctype, cval in getattr(dev_entry, "connections", None) or ():
                nm = _norm_mac(str(cval))
                if nm:
                    mac_to_dev[nm] = dev_entry
            for _dom, ident in getattr(dev_entry, "identifiers", None) or ():
                s_ident = str(ident)
                nm = _norm_mac(s_ident)
                if nm:
                    mac_to_dev[nm] = dev_entry
                # Shelly identifiers often look like "shellyplusplugs-aabbccddeeff"
                if "-" in s_ident:
                    tail_mac = _norm_mac(s_ident.rsplit("-", 1)[-1])
                    if tail_mac:
                        mac_to_dev[tail_mac] = dev_entry
                name_to_dev[s_ident.lower()] = dev_entry
            dev_label = getattr(dev_entry, "name_by_user", None) or getattr(dev_entry, "name", None)
            if dev_label:
                name_to_dev[str(dev_label).lower()] = dev_entry

        all_ids = list(device_ids) + list(receiver_ids)
        for item_id in all_ids:
            info: dict[str, str] = {}
            # 1. Entity registry match
            ent_entry = (
                ent_reg.async_get(item_id)
                if ent_reg is not None and hasattr(ent_reg, "async_get")
                else None
            )
            if ent_entry is not None:
                area = _area_name(getattr(ent_entry, "area_id", None))
                dev_name = ""
                if getattr(ent_entry, "device_id", None) and dev_reg is not None:
                    dev_entry = dev_reg.async_get(ent_entry.device_id)
                    if dev_entry is not None:
                        if not area:
                            area = _area_name(dev_entry.area_id)
                        dev_name = str(
                            getattr(dev_entry, "name_by_user", None)
                            or getattr(dev_entry, "name", None)
                            or ""
                        )
                if area:
                    info["area"] = area
                if dev_name:
                    info["name"] = dev_name

            # 2. MAC / Identifier match in device registry (for BLE devices & Shelly BLE Proxys!)
            if not info.get("area") or not info.get("name"):
                nm = _norm_mac(item_id)
                dev_entry = (
                    _lookup_mac_fuzzy(nm, mac_to_dev)
                    if nm
                    else name_to_dev.get(str(item_id).lower())
                )
                if dev_entry is not None:
                    if not info.get("area"):
                        area = _area_name(getattr(dev_entry, "area_id", None))
                        if area:
                            info["area"] = area
                    if not info.get("name"):
                        dev_name = str(
                            getattr(dev_entry, "name_by_user", None)
                            or getattr(dev_entry, "name", None)
                            or ""
                        )
                        if dev_name:
                            info["name"] = dev_name

            if info:
                resolved[item_id] = info
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("HA device/receiver metadata lookup unavailable: %s", err)
    return resolved


def discover_entity_areas(
    hass: HomeAssistant, entity_ids: Iterable[str]
) -> dict[str, str]:
    """Map Home Assistant entity_ids to their assigned HA Area name (if any)."""
    meta = discover_devices_and_receivers_metadata(hass, entity_ids, ())
    return {eid: info["area"] for eid, info in meta.items() if info.get("area")}


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
