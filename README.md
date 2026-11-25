# RF Presence Sense v2.1.0 — Passive 3D-Positionsbestimmung von Menschen & Tieren über bestehende WLAN- & Bluetooth-Geräte

**Keine extra Hardware. Kein ESP32. Kein Bluetooth-Proxy.**

`RF Presence Sense` ist eine Custom-Integration für Home Assistant, die **ausschließlich bereits eingebundene WLAN- und Bluetooth-Geräte** (Smart Plugs, Lampen, Schalter, Router/APs, BLE-Sensoren, Shelly-Scanner, TVs, etc.) nutzt, um **gerätelos (device-free)** Menschen und Tiere in einer **echten interaktiven 3D-Live-Map** zu lokalisieren.

---

## Neu in v2.1.0

- **Eigenes 3D-Seitenleisten-Panel (`3D RF-Map`)**: Direkt in der linken Home-Assistant-Seitenleiste (Vollbild auf Desktop & Handy) — wie ein echtes **3D-CAD-Objekt** per Maus/Touch frei 360°-drehbar (mit sanfter Trägheit/Inertia), verschiebbar (Rechtsklick/2-Finger-Pan), zoombar (Mausrad/Pinch) und mit **0 % Idle-CPU**!
- **Architektonisches 3D-Wohnungsmodell mit Türen & offenen Durchgängen**:
  - Solide 3D-Bodenplatten nur für definierte Räume + **Raum-Masking** (`mask_outside_rooms`): Bereiche außerhalb der Wohnung (z. B. Nachbarwohnung) werden sauber ausgeblendet und lösen keine Geister-Erkennungen mehr aus.
  - Halbtransparente 3D-Wände (`2,4 m`) mit echten **offenen Durchgängen** (`open_edges`) und **3D-Türöffnungen** (`doors`).
- **Integrierter Einmess-Assistent (`Einmessen / Ablaufen`) & Hybrid RTI + RF-Fingerprinting**:
  - **Modus 1 — Punkt einmessen:** Auf einen Punkt in der 3D-Wohnung tippen → *„Hier einmessen (5 s)“* (`record_calibration_point`).
  - **Modus 2 — Raumkante ablaufen (Edge-Walk):** Raum & Wand wählen → *„Kante ablaufen starten“* (`start_edge_walk`) → langsam an der Wand entlanggehen → *„Kante abschließen“* (`stop_edge_walk`) verteilt bis zu 6 interpolierte Fingerprints entlang der Kante!
  - **Hybrid-Fusion:** Kombiniert klassische Radio-Tomographie (RTI/VRTI) mit Kosinus-/L2-Fingerprint-Ähnlichkeit für deutlich höhere Raum- und Positionsgenauigkeit.
- **Automatisches Zuordnen aus HA-Areas & Namen (`auto_place_from_areas`)**:
  - Ermittelt Klarnamen & Räume aller Geräte **und** Bluetooth-/Shelly-Scanner aus der Home-Assistant-Device-/Area-Registry sowie typischen Namens-Kürzeln (`BZ`, `AZ`, `WZ`, `Küche`, `Fridge`, `XP-4200`, `Briefkasten`, `Govee`, `TRV`, `PStrip`, …) und platziert sie mit realistischer 3D-Höhe direkt im passenden Raum.

---

## Interaktive 3D-Ansicht nutzen

1. **Seitenleiste (empfohlen):** Klicke links in Home Assistant direkt auf **„3D RF-Map“** (`/rf-presence-3d`).
2. **Als Dashboard-Karte (optional):**
   ```yaml
   type: custom:rf-presence-3d-card
   title: RF Presence Sense 3D
   entity: sensor.rf_presence_sense_positionen_ziele
   # Optional: eigenes Grundrissbild auf den 3D-Boden projizieren
   # floorplan_image: /local/grundriss.png
   height: 460
   ```
3. **Als 3D-Kamera-Stream (z. B. HA Companion App / Picture Entity):** `camera.rf_presence_sense_3d_live_rf_map`.

---

## Alle Services

| Service | Beschreibung |
|---|---|
| `rf_presence_sense.calibrate_empty_room` | Leer-Raum-Baseline & Rauschen einmessen & dauerhaft speichern |
| `rf_presence_sense.reset_baseline` | Baselines sofort auf aktuelle Messwerte zurücksetzen |
| `rf_presence_sense.set_device_position` | 3D-Position `(x, y, z)` für Gerät/Empfänger dauerhaft festlegen |
| `rf_presence_sense.define_room` | Raum (Rechteck/Polygon + `open_edges` + `doors`) anlegen/ändern |
| `rf_presence_sense.delete_room` | Raumzone dauerhaft entfernen |
| `rf_presence_sense.record_calibration_point` | Empirischen RF-Fingerprint an `(x, y)` aufnehmen (Hybrid-RTI) |
| `rf_presence_sense.start_edge_walk` | Kanten-Ablaufen entlang einer Raumwand starten |
| `rf_presence_sense.stop_edge_walk` | Kanten-Ablaufen abschließen (bis zu 6 Fingerprints speichern) |
| `rf_presence_sense.clear_fingerprints` | Alle Kalibrierungs-Messpunkte löschen |
| `rf_presence_sense.auto_place_from_areas` | Geräte & BLE-/Shelly-Scanner automatisch anhand HA-Areas & Namen platzieren |

---

## Automations-Events

| Event | Beschreibung & Felder |
|---|---|
| `rf_presence_sense_room_changed` | Ziel wechselt den Raum (`track_id`, `type`, `from_room_id`, `from_room`, `to_room_id`, `to_room`, `x`, `y`, `confidence`) |
| `rf_presence_sense_target_detected` | Neues Lebewesen erkannt (`track_id`, `type`, `room_id`, `room`, `x`, `y`, `confidence`) |
| `rf_presence_sense_target_lost` | Ziel nicht mehr erkannt (`track_id`, `type`, `last_room_id`, `last_room`) |
