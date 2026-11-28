"""Constants for the RF Presence Sense (Device-Free RTI) integration."""

from __future__ import annotations

DOMAIN = "rf_presence_sense"
NAME = "RF Presence Sense"
VERSION = "2.2.1"

PLATFORMS = ["sensor", "binary_sensor", "camera", "button", "number"]

# Config entry keys
CONF_FLOOR_WIDTH = "floor_width"
CONF_FLOOR_HEIGHT = "floor_height"
CONF_GRID_RESOLUTION = "grid_resolution"
CONF_UPDATE_INTERVAL = "update_interval"
CONF_SENSITIVITY = "sensitivity"
CONF_RECEIVERS = "receivers"
CONF_DEVICES = "devices"
CONF_ROOMS = "rooms"
CONF_ENABLE_BLE = "enable_ble"
CONF_ENABLE_WIFI_ENTITIES = "enable_wifi_entities"
CONF_ACTIVE_POLL_ENTITIES = "active_poll_entities"
CONF_PRESENCE_AWARE_BASELINE = "presence_aware_baseline"
CONF_AWAY_ENTITY = "away_entity"
CONF_CALIBRATION_DURATION = "calibration_duration"
CONF_MASK_OUTSIDE_ROOMS = "mask_outside_rooms"

# Default geometry & RTI parameters
DEFAULT_FLOOR_WIDTH = 10.0  # meters
DEFAULT_FLOOR_HEIGHT = 8.0  # meters
DEFAULT_GRID_RESOLUTION = 0.5  # meters per voxel
DEFAULT_UPDATE_INTERVAL = 4.0  # seconds
MIN_UPDATE_INTERVAL = 2.0  # minimum allowed update interval (seconds)
DEFAULT_SENSITIVITY = 1.0  # multiplier (0.5 = less sensitive, 2.0 = more sensitive)
DEFAULT_CALIBRATION_DURATION = 20  # seconds
DEFAULT_PRESENCE_AWARE_BASELINE = True
DEFAULT_MASK_OUTSIDE_ROOMS = True

# RTI physics defaults (Wilson & Patwari 2010/2011)
DEFAULT_ELLIPSE_EXCESS_M = 0.35  # Fresnel ellipse excess path width (meters)
DEFAULT_CORRELATION_DIST_M = 1.6  # Spatial covariance correlation distance (meters)
DEFAULT_NOISE_VARIANCE = 1.2  # Regularization noise variance (sigma_n^2)
DEFAULT_SIGNAL_VARIANCE = 1.0  # Prior voxel variance (sigma_x^2)
DEFAULT_VARIANCE_WEIGHT = 1.5  # Weight for VRTI sliding-window std dev
DEFAULT_CONSTRUCTIVE_WEIGHT = 0.35  # Weight for constructive multipath spikes
DEFAULT_BASELINE_ALPHA = 0.015  # Slow EWMA baseline adaptation rate
DEFAULT_WINDOW_SECONDS = 12.0  # Sliding window for variance/smoothing
DEFAULT_LONG_WINDOW_SECONDS = 30.0  # Longer micro-motion/breathing variance window
LINK_BASELINE_FREEZE_SHADOWING_DB = 1.2  # Freeze per-link baseline when shadowed >= 1.2 dB
LINK_WARMUP_SAMPLES = 3  # Median warmup samples before a new link locks initial baseline
DEFAULT_LINK_STALE_SECONDS = 180.0  # Default fallback stale timeout (BLE)
STALE_SECONDS_BLE = 180.0  # BLE advertisements stale timeout
STALE_SECONDS_WIFI = 600.0  # Stationary Wi-Fi RSSI entities report less frequently
STALE_SECONDS_ZIGBEE = 600.0  # Stationary Zigbee LQI entities report less frequently

# Human vs. Animal classification thresholds
LOW_HEIGHT_THRESHOLD_M = 0.90  # Links/voxels below this height see pets & human legs
HUMAN_ATTENUATION_THRESHOLD = 1.85  # Effective attenuation units for human body
ANIMAL_ATTENUATION_THRESHOLD = 0.65  # Effective attenuation units for pet body
DETECTION_THRESHOLD_BASE = 1.05  # Base voxel threshold for target detection

# Resource & stability limits (prevent OOM / event-loop stalls on ARM SBCs)
MAX_VOXELS = 500  # Auto-upscale grid resolution if floorplan exceeds this
MAX_LINKS = 80  # Cap RF links to bound O(M^3) Cholesky & projection memory
MAX_CALIBRATION_SAMPLES = 200  # Bounded deque per link during calibration
MAX_FINGERPRINTS = 120  # Bounded RF calibration fingerprints for hybrid fusion
MAX_REPORTED_FINGERPRINTS = 80  # Compact [x, y] markers exposed to 3D frontend
MAX_REPORTED_PERTURBATIONS = 20  # Top-N perturbed links in HA state attributes
MAX_REPORTED_HEATMAP_PEAKS = 30  # Compact heatmap peaks for 3D Lovelace Card
BLE_MIN_INTERVAL_SECONDS = 0.4  # Minimum interval between BLE samples per link
REBUILD_COOLDOWN_SECONDS = 8.0  # Min cooldown between automatic dynamic projection rebuilds
BUS_EVENT_MAX_PER_WINDOW = 10  # Global cap on HA bus events per window
BUS_EVENT_WINDOW_SECONDS = 60.0  # Sliding window for global HA bus event cap
ROOM_CHANGE_CONFIRM_TICKS = 2  # Consecutive ticks in new room before firing room_changed
LINK_HEALTH_HEARTBEAT_SECONDS = 30.0  # Max interval between link_health state writes

# Tracker defaults
TRACKER_MAX_ASSOC_DIST_M = 2.2
TRACKER_MIN_HITS = 3
TRACKER_MIN_SEPARATION_M = 1.8  # Minimum spatial distance between distinct RTI peaks
TRACKER_SIDELOBE_MAIN_THRESHOLD = 1.6  # Main peak strength activating relative sidelobe pruning
TRACKER_SIDELOBE_RATIO = 0.36  # Discard secondary sidelobes below 36% of dominant peak
TRACKER_MAX_MISSED = 4
TRACKER_MAX_MISSED_STATIONARY = 12  # ~48s hold time at 4s update interval when stationary
TRACKER_STATIONARY_HYSTERESIS = 0.55  # Confirmed targets hold down to 0.55 * threshold
TRACKER_STATIONARY_SPEED_MPS = 0.12  # Speed below this counts as stationary
TRACKER_STATIONARY_DWELL_SECONDS = 10.0  # Min dwell time before state becomes "stationary"
TRACKER_MAX_TRACKS = 16
TRACKER_TRAIL_MAXLEN = 15

# Target types
TARGET_TYPE_HUMAN = "human"
TARGET_TYPE_ANIMAL = "animal"

# Signal protocols
PROTOCOL_WIFI = "wifi"
PROTOCOL_BLE = "ble"
PROTOCOL_ZIGBEE = "zigbee"

PROTOCOL_STALE_SECONDS: dict[str, float] = {
    PROTOCOL_WIFI: STALE_SECONDS_WIFI,
    PROTOCOL_ZIGBEE: STALE_SECONDS_ZIGBEE,
    PROTOCOL_BLE: STALE_SECONDS_BLE,
}

# Service names
SERVICE_CALIBRATE_EMPTY = "calibrate_empty_room"
SERVICE_SET_DEVICE_POSITION = "set_device_position"
SERVICE_DEFINE_ROOM = "define_room"
SERVICE_DELETE_ROOM = "delete_room"
SERVICE_RESET_BASELINE = "reset_baseline"
SERVICE_RECORD_CALIBRATION_POINT = "record_calibration_point"
SERVICE_START_EDGE_WALK = "start_edge_walk"
SERVICE_STOP_EDGE_WALK = "stop_edge_walk"
SERVICE_CLEAR_FINGERPRINTS = "clear_fingerprints"
SERVICE_AUTO_PLACE_FROM_AREAS = "auto_place_from_areas"

# Home Assistant bus event names
EVENT_ROOM_CHANGED = "rf_presence_sense_room_changed"
EVENT_TARGET_DETECTED = "rf_presence_sense_target_detected"
EVENT_TARGET_LOST = "rf_presence_sense_target_lost"

# Persistent storage
STORAGE_VERSION = 1

# Frontend static assets & custom sidebar panel
FRONTEND_URL_BASE = "/rf_presence_sense"
FRONTEND_CARD_FILENAME = "rf-presence-3d-card.js"
PANEL_URL_PATH = "rf-presence-3d"
PANEL_WEBCOMPONENT = "rf-presence-3d-panel"
PANEL_TITLE = "3D RF-Map"
PANEL_ICON = "mdi:cube-scan"
