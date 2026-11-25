# RF Presence Sense v2.0.0 — Passive 3D-Positionsbestimmung von Menschen & Tieren über bestehende WLAN- & Bluetooth-Geräte

**Keine extra Hardware. Kein ESP32. Kein Bluetooth-Proxy.**

`RF Presence Sense` ist eine Custom-Integration für Home Assistant, die **ausschließlich bereits eingebundene WLAN- und Bluetooth-Geräte** (Smart Plugs, Lampen, Schalter, Router/APs, BLE-Sensoren, TVs, etc.) nutzt, um **gerätelos (device-free)** Menschen und Tiere in einer **echten 3D-Live-Map** zu lokalisieren.

---

## Neu in v2.0.0

- **Echte 3D-Live-Map (`camera`)**: Isometrische 3D-PNG-Karte (Zero-Dependency Pure-Python) mit 3D-Raumwänden, echten 3D-RF-Links auf Montagehöhe `z`, 3D-Mensch-/Tier-Figuren, verblassenden Bewegungspfaden (Trails), Raumnamen, Ziel-Konfidenz-Labels, Legende & Kalibrierungs-Banner.
- **Interaktive 3D Custom Lovelace Card (`rf-presence-3d-card`)**: Direkt mitgelieferte HTML5-3D-Karte — frei drehbar per Maus/Touch, Zoom, 3D/2D-Umschalter, optionales eigenes Grundrissbild und **Drag-&-Drop-Edit-Modus** für Geräte, Empfänger & Räume!
- **Vollständige Persistenz (`storage.py`)**: 3D-Gerätepositionen, Räume (Rechtecke & Polygone), kalibrierte Baselines und Live-Tuning bleiben über Home-Assistant-Neustarts und Reloads dauerhaft erhalten.
- **Dynamische Raum-Sensoren & `delete_room`**: Neue Räume erhalten sofort automatisch ihre 3 Präsenz-Sensoren (`Präsenz`, `Mensch`, `Tier`).
- **Menü-geführter Options-Flow**: Tuning & Grundriss, stationäre Geräte verwalten, 3D-Positionen bearbeiten und Räume anlegen/löschen direkt in der HA-UI.
- **Live-Steuerung & Automations-Events**: Neue `button`-Entitäten (*Kalibrieren*, *Baseline zurücksetzen*), `number`-Slider (*Empfindlichkeit* live ohne Reload, *Kalibrierdauer*) sowie HA-Events (`rf_presence_sense_room_changed`, `rf_presence_sense_target_detected`, `rf_presence_sense_target_lost`).
- **Genauigkeits-Upgrade**: Voxel-genaue 3D-Höheninterpolation $z(x, y)$ entlang jedes Links, präsenz-bewusste Baseline (kein Ausblenden ruhig sitzender Personen), dynamisches Multi-AP-Tracking (`ap_mac`/`bssid`), Polygon-Räume, HA-Area-Auto-Mapping und realistische Stale-Timeouts (Wi-Fi/Zigbee 600 s, BLE 180 s).
- **Vollständige Mehrsprachigkeit (`de`/`en`) & HA-Diagnose-Export (`diagnostics.py`)**.

---

## Wie funktioniert das physikalisch? (Radio Tomographic Imaging — 3D RTI + VRTI)

1. **Wasser absorbiert 2,4-GHz- und 5-GHz-Funkwellen:**
   Menschen und Tiere bestehen zu ~60–70 % aus Wasser. Betritt ein Lebewesen die **1. Fresnel-Zone** zwischen einem stationären WLAN-Gerät und dem Router (oder einem BLE-Gerät und dem HA-Bluetooth-Adapter), verändert sich das Funkfeld messbar (**Shadowing** + **VRTI-Varianz**).
2. **2D/3D-Tomographie (Woodbury-Dual-Inversion):**
   Die Integration rekonstruiert aus allen aktiven WLAN- und BLE-Links in reinem Python (ohne schwere Abhängigkeiten, ARM64-optimiert, im Executor-Pool ausgelagert) die räumliche Dämpfungsverteilung.
3. **Voxel-genaue 3D-Höhenattribution (Mensch vs. Tier):**
   Für jeden Voxel entlang einer Funkstrecke $p_1(x_1, y_1, z_1) \to p_2(x_2, y_2, z_2)$ wird die exakte Linienhöhe $z(x, y)$ interpoliert:
   - **Tier (Katze/Hund):** Stört primär bodennahe Abschnitte ($z < 0{,}9\text{ m}$) bei kleinerem Funkquerschnitt.
   - **Mensch:** Stört sowohl bodennahe als auch höhere Funkabschnitte ($z \ge 0{,}9\text{ m}$) mit stärkerer Dämpfung und größerem Schatten.

---

## Interaktive 3D Lovelace Card einbinden

Die Karte wird beim Start der Integration automatisch unter `/rf_presence_sense/rf-presence-3d-card.js` bereitgestellt und ins Frontend geladen. Füge im Dashboard einfach eine manuelle Karte hinzu:

```yaml
type: custom:rf-presence-3d-card
title: RF Presence Sense 3D
entity: sensor.rf_presence_sense_positionen_ziele
# Optional: eigenes Grundrissbild auf den 3D-Boden projizieren
# floorplan_image: /local/grundriss.png
height: 450
```

Alternativ kannst du die serverseitig gerenderte 3D-Live-Map-Kamera `camera.rf_presence_sense_3d_live_rf_map` in jeder Standard-*Picture Entity*-Karte nutzen (auch ideal für die HA Companion App).

---

## Automations-Events

Die Integration feuert bei Zustandsübergängen folgende Events auf dem Home-Assistant-Event-Bus:

| Event | Beschreibung & Felder |
|---|---|
| `rf_presence_sense_room_changed` | Ziel wechselt den Raum (`track_id`, `type`, `from_room_id`, `from_room`, `to_room_id`, `to_room`, `x`, `y`, `confidence`) |
| `rf_presence_sense_target_detected` | Neues Lebewesen erkannt (`track_id`, `type`, `room_id`, `room`, `x`, `y`, `confidence`) |
| `rf_presence_sense_target_lost` | Ziel nicht mehr erkannt (`track_id`, `type`, `last_room_id`, `last_room`) |

---

## Bereitgestellte Entitäten

| Plattform | Entität | Beschreibung |
|---|---|---|
| `sensor` | Erkannte Menschen / Tiere / Gesamt | Aktuelle Anzahl erkannter Lebewesen |
| `sensor` | Positionen & Ziele | Status + 3D-Zieldaten, Räume, Links & Trails (Recorder-geschützt) |
| `sensor` | RF-Netz Abdeckung | Anteil aktiver RF-Links (%) & Paket-Zähler |
| `binary_sensor` | Mensch erkannt / Tier erkannt | Globale Präsenz |
| `binary_sensor` | `<Raum>` Präsenz / Mensch / Tier | Dynamische Raum-Präsenz-Sensoren |
| `camera` | 3D Live RF-Map | Isometrische 3D-PNG-Live-Karte mit Labels, Trails & Legende |
| `button` | Leeren Raum kalibrieren / Baseline zurücksetzen | Ein-Klick-Kalibrierung im Dashboard |
| `number` | Empfindlichkeit / Kalibrierdauer | Live-Feintuning ohne Neustart |

---

## Services

- `rf_presence_sense.calibrate_empty_room` — Leer-Raum-Kalibrierung starten & dauerhaft speichern
- `rf_presence_sense.reset_baseline` — Baselines sofort zurücksetzen & speichern
- `rf_presence_sense.set_device_position` — 3D-Position `(x, y, z)` für Gerät/Empfänger dauerhaft festlegen
- `rf_presence_sense.define_room` — Rechteckigen oder Polygon-Raum (`polygon: [[x,y], ...]`) anlegen/aktualisieren
- `rf_presence_sense.delete_room` — Raumzone dauerhaft entfernen
